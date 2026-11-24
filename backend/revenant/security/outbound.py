"""SSRF-safe outbound HTTP.

This module is the only place in the application allowed to open outbound
connections. It was promoted from ``spikes/outbound_transport`` after that
spike's complete test suite and mutation check passed on Linux in CI.

How it works: httpcore asks its network backend to open every NEW TCP
connection through ``connect_tcp(host, port)``. ``GuardedBackend`` resolves
``host`` at that moment, validates every returned address, refuses the whole
answer if any address is disallowed, and then asks the real backend to connect
to a validated IP literal. httpcore itself still performs the TLS handshake
with ``server_hostname`` set to the original hostname, so SNI and certificate
verification are unaffected by connecting to an IP.

Rules implemented here (docs/security.md, "Outbound requests"):

* resolve immediately before connecting, for every new connection;
* validate every IPv4 and IPv6 address; refuse mixed answers outright;
* connect to a validated address with no second name resolution;
* keep hostname-based SNI and certificate verification;
* never follow redirects; ignore proxy environment variables; no unix sockets.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
import ssl
from collections.abc import AsyncIterable, AsyncIterator, Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass, field
from typing import Protocol

import httpcore
import httpx

from revenant.config import Settings
from revenant.errors import OutboundBlocked

__all__ = [
    "GuardedBackend",
    "GuardedTransport",
    "OutboundBlocked",
    "OutboundPolicy",
    "build_outbound_client",
    "check_destination",
    "check_url",
    "real_connector",
    "system_resolver",
]

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address
IPNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network

# An async function (host, port) -> list of address strings (IPv4 and IPv6).
Resolver = Callable[[str, int], Awaitable[Sequence[str]]]


class Connector(Protocol):
    """An async function that opens a TCP connection to an IP literal."""

    async def __call__(
        self,
        ip: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.AsyncNetworkStream: ...


_NAT64 = ipaddress.ip_network("64:ff9b::/96")

# What REVENANT_ALLOW_PRIVATE_UPSTREAMS permits (local demonstration only).
# Link-local addresses, which include cloud metadata services, are NOT in this list.
_PRIVATE_DEMO_NETWORKS: tuple[IPNetwork, ...] = (
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
)

_SAFE_SCHEMES = ("http", "https")
_DEFAULT_PORTS = {"http": 80, "https": 443}


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


def _never_allowed(addr: IPAddress) -> str | None:
    """Addresses that no allowlist or demo flag can permit."""
    if addr.is_link_local:
        return "link-local"
    if addr.is_multicast:
        return "multicast"
    if addr.is_unspecified:
        return "unspecified"
    return None


@dataclass(frozen=True)
class OutboundPolicy:
    """Which addresses an outbound connection may use.

    ``allowed_networks`` and ``allowed_hosts`` are the explicit exceptions
    documented in docs/security.md. They never disable resolve-at-connect,
    redirect refusal or TLS verification, and they never permit link-local,
    multicast or unspecified addresses.
    """

    allowed_networks: tuple[IPNetwork, ...] = ()
    allowed_hosts: frozenset[str] = frozenset()

    @classmethod
    def from_settings(cls, settings: Settings) -> OutboundPolicy:
        return cls(
            allowed_networks=_PRIVATE_DEMO_NETWORKS if settings.allow_private_upstreams else (),
            allowed_hosts=settings.allowlisted_hosts,
        )

    def reason_blocked(self, raw: str, host: str | None = None) -> str | None:
        """Return why ``raw`` is not allowed, or None if it is allowed."""
        if "%" in raw:
            return "scoped (zone-id) address"
        try:
            addr = ipaddress.ip_address(raw)
        except ValueError:
            return "not an IP address"

        if isinstance(addr, ipaddress.IPv6Address):
            inner = _embedded_ipv4(addr)
            if inner is not None:
                inner_reason = self.reason_blocked(str(inner), host)
                return None if inner_reason is None else f"embeds IPv4 {inner}: {inner_reason}"

        hard = _never_allowed(addr)
        if hard is not None:
            return hard

        if any(addr in net for net in self.allowed_networks if net.version == addr.version):
            return None
        if host is not None and host.lower().rstrip(".") in self.allowed_hosts:
            return None

        if addr.is_loopback:
            return "loopback"
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
            reason = self.reason_blocked(raw, host)
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


async def _resolve_and_validate(
    host: str, port: int, policy: OutboundPolicy, resolver: Resolver
) -> list[str]:
    try:
        literal = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        # 1. Resolve immediately before connecting.
        addresses = list(await resolver(host, port))
    else:
        # A URL whose host is already an IP literal is validated the same way.
        addresses = [str(literal)]
    # 2 + 3. Validate every address; refuse mixed answers.
    return policy.validate_all(host, addresses)


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
    # What was dialled, most recent last. Bounded; used by tests and health reporting.
    dialled: list[tuple[str, str, int]] = field(default_factory=list)
    max_dialled: int = 100

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        validated = await _resolve_and_validate(host, port, self.policy, self.resolver)

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
            del self.dialled[: -self.max_dialled]
            return stream
        assert last_error is not None  # noqa: S101 - validated is never empty
        raise last_error

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        raise OutboundBlocked("unix sockets are not permitted for outbound connections")

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


# --------------------------------------------------------------------------- httpx transport

_EXCEPTION_MAP: tuple[tuple[type[Exception], type[httpx.TransportError]], ...] = (
    (httpcore.ConnectTimeout, httpx.ConnectTimeout),
    (httpcore.ReadTimeout, httpx.ReadTimeout),
    (httpcore.WriteTimeout, httpx.WriteTimeout),
    (httpcore.PoolTimeout, httpx.PoolTimeout),
    (httpcore.TimeoutException, httpx.TimeoutException),
    (httpcore.ConnectError, httpx.ConnectError),
    (httpcore.ReadError, httpx.ReadError),
    (httpcore.WriteError, httpx.WriteError),
    (httpcore.LocalProtocolError, httpx.LocalProtocolError),
    (httpcore.RemoteProtocolError, httpx.RemoteProtocolError),
    (httpcore.ProtocolError, httpx.ProtocolError),
    (httpcore.UnsupportedProtocol, httpx.UnsupportedProtocol),
    (httpcore.NetworkError, httpx.NetworkError),
)


def _to_httpx_error(exc: Exception) -> Exception:
    """Translate an httpcore exception into its httpx equivalent."""
    for core_type, httpx_type in _EXCEPTION_MAP:
        if isinstance(exc, core_type):
            return httpx_type(str(exc))
    return exc


class _ResponseStream(httpx.AsyncByteStream):
    def __init__(self, stream: AsyncIterable[bytes]) -> None:
        self._stream = stream

    async def __aiter__(self) -> AsyncIterator[bytes]:
        try:
            async for chunk in self._stream:
                yield chunk
        except Exception as exc:
            raise _to_httpx_error(exc) from exc

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
        try:
            core_response = await self._pool.handle_async_request(core_request)
        except OutboundBlocked:
            raise
        except Exception as exc:
            raise _to_httpx_error(exc) from exc
        body = core_response.stream
        if not isinstance(body, AsyncIterable):  # pragma: no cover - the async pool streams async
            raise OutboundBlocked("unexpected synchronous response stream")
        return httpx.Response(
            status_code=core_response.status,
            headers=core_response.headers,
            # The httpcore stream itself, so closing the response releases the connection.
            stream=_ResponseStream(body),
            extensions=core_response.extensions,
        )

    async def aclose(self) -> None:
        await self._pool.aclose()


def build_outbound_client(
    backend: GuardedBackend,
    ssl_context: ssl.SSLContext | None = None,
    keepalive_expiry: float = 60.0,
    timeout: float = 30.0,
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
        timeout=httpx.Timeout(timeout),
    )


# --------------------------------------------------------------------------- URL rules


def check_url(url: str, settings: Settings) -> httpx.URL:
    """Validate the shape of an outbound URL. Does not resolve the host.

    Applied when a URL is configured and again on every request.
    """
    try:
        parsed = httpx.URL(url)
    except (httpx.InvalidURL, TypeError) as exc:
        raise OutboundBlocked("URL is not valid") from exc

    if parsed.scheme not in _SAFE_SCHEMES:
        raise OutboundBlocked(f"scheme {parsed.scheme!r} is not allowed; use http or https")
    if settings.is_production and parsed.scheme != "https":
        raise OutboundBlocked("https is required in production")
    if parsed.userinfo:
        raise OutboundBlocked("credentials in the URL are not allowed")
    if not parsed.host:
        raise OutboundBlocked("URL has no host")
    port = parsed.port or _DEFAULT_PORTS[parsed.scheme]
    if port not in settings.allowed_ports:
        raise OutboundBlocked(f"port {port} is not in the allowed set")
    if parsed.fragment:
        raise OutboundBlocked("URL fragments are not allowed")
    return parsed


async def check_destination(
    url: str,
    settings: Settings,
    resolver: Resolver = system_resolver,
) -> httpx.URL:
    """Configuration-time check: URL rules, then resolve and validate once.

    This is an early warning for the operator. It is not what protects a
    connection: the same validation runs again, from a fresh resolution, every
    time ``GuardedBackend`` opens a connection.
    """
    parsed = check_url(url, settings)
    port = parsed.port or _DEFAULT_PORTS[parsed.scheme]
    policy = OutboundPolicy.from_settings(settings)
    await _resolve_and_validate(parsed.host, port, policy, resolver)
    return parsed
