"""Feasibility spike: SSRF-safe outbound transport for httpx / httpcore.

This is NOT application code. It exists to prove, with executable tests, that
the outbound-connection rules in docs/security.md can be implemented on the
chosen HTTP client using only its public extension points:

  * httpcore.AsyncNetworkBackend      (public "network backend" interface)
  * httpcore.AsyncConnectionPool(network_backend=...)
  * httpx.AsyncBaseTransport          (public custom-transport interface)

Design in one paragraph: httpcore asks its network backend to open every new
TCP connection via ``connect_tcp(host, port)``. ``GuardedBackend`` resolves
``host`` at that moment, validates every returned address, refuses the whole
answer if any address is disallowed, and then asks the real backend to connect
to a validated *IP literal*. httpcore itself still performs the TLS handshake
with ``server_hostname`` set to the original hostname, so SNI and certificate
verification are unaffected by connecting to an IP.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
import ssl
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass, field

import httpcore
import httpx

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address
IPNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network

# An async function (host, port) -> list of address strings (IPv4 and IPv6).
Resolver = Callable[[str, int], Awaitable[Sequence[str]]]
# An async function that opens a TCP connection to an IP literal.
Connector = Callable[..., Awaitable[httpcore.AsyncNetworkStream]]

_NAT64 = ipaddress.ip_network("64:ff9b::/96")


class OutboundBlocked(Exception):
    """The destination failed validation. No connection was attempted."""


# --------------------------------------------------------------------------- policy


def _embedded_ipv4(addr: ipaddress.IPv6Address) -> ipaddress.IPv4Address | None:
    """Return the IPv4 address carried inside an IPv6 address, if any."""
    if addr.ipv4_mapped is not None:  # ::ffff:a.b.c.d
        return addr.ipv4_mapped
    if addr in _NAT64:  # 64:ff9b::a.b.c.d
        return ipaddress.IPv4Address(int(addr) & 0xFFFFFFFF)
    if addr.sixtofour is not None:  # 2002:AABB:CCDD::
        return addr.sixtofour
    if addr.teredo is not None:  # 2001:0::/32, client address is second element
        return addr.teredo[1]
    if int(addr) >> 32 == 0 and int(addr) > 1:  # deprecated IPv4-compatible ::a.b.c.d
        return ipaddress.IPv4Address(int(addr))
    return None


@dataclass(frozen=True)
class OutboundPolicy:
    """Which addresses an outbound connection may use.

    ``allowed_networks`` models the explicit allowlist exception
    (REVENANT_UPSTREAM_ALLOWLIST / the local-demo flag). It is empty by default.
    It never disables resolve-at-connect, redirect handling or TLS verification.
    """

    allowed_networks: tuple[IPNetwork, ...] = ()

    def reason_blocked(self, raw: str) -> str | None:
        """Return why ``raw`` is not allowed, or None if it is allowed."""
        if "%" in raw:
            return "scoped (zone-id) address"
        try:
            addr = ipaddress.ip_address(raw)
        except ValueError:
            return "not an IP address"

        if any(addr in net for net in self.allowed_networks if net.version == addr.version):
            return None

        if isinstance(addr, ipaddress.IPv6Address):
            inner = _embedded_ipv4(addr)
            if inner is not None:
                inner_reason = self.reason_blocked(str(inner))
                return None if inner_reason is None else f"embeds IPv4 {inner}: {inner_reason}"

        if addr.is_loopback:
            return "loopback"
        if addr.is_link_local:
            return "link-local"
        if addr.is_multicast:
            return "multicast"
        if addr.is_unspecified:
            return "unspecified"
        if addr.is_private:
            return "private"
        if addr.is_reserved:
            return "reserved"
        if not addr.is_global:
            return "not globally routable"
        return None

    def validate_all(self, host: str, addresses: Sequence[str]) -> list[str]:
        """Every address must pass. One bad address refuses the whole answer."""
        if not addresses:
            raise OutboundBlocked(f"{host}: name resolved to no addresses")
        for raw in addresses:
            reason = self.reason_blocked(raw)
            if reason is not None:
                raise OutboundBlocked(
                    f"{host}: resolved address {raw} is not allowed ({reason}); "
                    f"refusing all {len(addresses)} address(es)"
                )
        return list(addresses)


# --------------------------------------------------------------------------- resolve / connect


async def system_resolver(host: str, port: int) -> list[str]:
    """Resolve A and AAAA records now, using the operating system resolver."""
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    seen: list[str] = []
    for _family, _type, _proto, _canon, sockaddr in infos:
        address = str(sockaddr[0])
        if address not in seen:
            seen.append(address)
    return seen


async def real_connector(
    ip: str,
    port: int,
    timeout: float | None = None,
    local_address: str | None = None,
    socket_options: Iterable[httpcore.SOCKET_OPTION] | None = None,
) -> httpcore.AsyncNetworkStream:
    """Connect to an IP literal and confirm the socket's peer is that address."""
    ipaddress.ip_address(ip)  # must already be a literal; raises otherwise
    stream = await httpcore.AnyIOBackend().connect_tcp(
        ip, port, timeout=timeout, local_address=local_address, socket_options=socket_options
    )
    peer = stream.get_extra_info("server_addr")
    if peer is None or ipaddress.ip_address(peer[0]) != ipaddress.ip_address(ip):
        await stream.aclose()
        raise OutboundBlocked(f"connected peer {peer!r} is not the validated address {ip}")
    return stream


@dataclass
class GuardedBackend(httpcore.AsyncNetworkBackend):
    """Network backend that resolves, validates and connects by IP.

    httpcore calls ``connect_tcp`` once for every NEW connection, so every new
    connection is resolved and validated again. Reused keep-alive connections
    do not call it; they are already bound to a validated peer.
    """

    policy: OutboundPolicy = field(default_factory=OutboundPolicy)
    resolver: Resolver = system_resolver
    connector: Connector = real_connector
    # Evidence for tests and for health reporting: what was dialled.
    dialled: list[tuple[str, str, int]] = field(default_factory=list)

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        try:
            literal = ipaddress.ip_address(host.strip("[]"))
        except ValueError:
            # 1. Resolve immediately before connecting.
            addresses = list(await self.resolver(host, port))
        else:
            # A URL whose host is already an IP literal is validated the same way.
            addresses = [str(literal)]

        # 2 + 3. Validate every address; refuse mixed answers.
        validated = self.policy.validate_all(host, addresses)

        # 4. Connect to a validated IP literal. The hostname is never passed on,
        #    so there is no second resolution.
        last_error: Exception | None = None
        for ip in validated:
            try:
                stream = await self.connector(
                    ip, port, timeout=timeout, local_address=local_address,
                    socket_options=socket_options,
                )
            except (httpcore.ConnectError, httpcore.ConnectTimeout, OSError) as exc:
                last_error = exc
                continue
            self.dialled.append((host, ip, port))
            return stream
        assert last_error is not None
        raise last_error

    async def connect_unix_socket(self, path: str, timeout: float | None = None,
                                  socket_options: Iterable[httpcore.SOCKET_OPTION] | None = None,
                                  ) -> httpcore.AsyncNetworkStream:
        raise OutboundBlocked("unix sockets are not permitted for outbound connections")

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


# --------------------------------------------------------------------------- httpx transport


class _ResponseStream(httpx.AsyncByteStream):
    def __init__(self, stream: AsyncIterator[bytes]) -> None:
        self._stream = stream

    async def __aiter__(self) -> AsyncIterator[bytes]:
        async for chunk in self._stream:
            yield chunk

    async def aclose(self) -> None:
        aclose = getattr(self._stream, "aclose", None)
        if aclose is not None:
            await aclose()


class GuardedTransport(httpx.AsyncBaseTransport):
    """httpx transport backed by an httpcore pool that uses ``GuardedBackend``.

    Deliberately has no proxy support and no HTTP/2, never retries, and removes
    the ``sni_hostname`` request extension so a caller cannot change the name
    the certificate is verified against.
    """

    def __init__(
        self,
        backend: GuardedBackend,
        ssl_context: ssl.SSLContext | None = None,
        keepalive_expiry: float = 60.0,
        max_connections: int = 20,
    ) -> None:
        self.backend = backend
        self._pool = httpcore.AsyncConnectionPool(
            ssl_context=ssl_context or ssl.create_default_context(),
            max_connections=max_connections,
            max_keepalive_connections=max_connections,
            keepalive_expiry=keepalive_expiry,
            http1=True,
            http2=False,
            retries=0,
            network_backend=backend,
        )

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        extensions = {k: v for k, v in request.extensions.items() if k != "sni_hostname"}
        core_request = httpcore.Request(
            method=request.method,
            url=httpcore.URL(
                scheme=request.url.raw_scheme,
                host=request.url.raw_host,
                port=request.url.port,
                target=request.url.raw_path,
            ),
            headers=request.headers.raw,
            content=request.stream,
            extensions=extensions,
        )
        core_response = await self._pool.handle_async_request(core_request)
        return httpx.Response(
            status_code=core_response.status,
            headers=core_response.headers,
            stream=_ResponseStream(core_response.stream),
            extensions=core_response.extensions,
        )

    async def aclose(self) -> None:
        await self._pool.aclose()


def build_client(
    backend: GuardedBackend,
    ssl_context: ssl.SSLContext | None = None,
    keepalive_expiry: float = 60.0,
    follow_redirects: bool = False,
) -> httpx.AsyncClient:
    """The only supported way to build an outbound client.

    ``follow_redirects`` exists as a parameter solely so the tests can prove
    that even if someone turned it on, a redirect could not bypass validation.
    The application never enables it.
    """
    return httpx.AsyncClient(
        transport=GuardedTransport(backend, ssl_context, keepalive_expiry),
        trust_env=False,  # ignore HTTP_PROXY / HTTPS_PROXY / ALL_PROXY / NO_PROXY
        follow_redirects=follow_redirects,
        timeout=httpx.Timeout(5.0),
    )
