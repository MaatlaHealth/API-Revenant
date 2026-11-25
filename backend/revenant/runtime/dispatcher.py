"""``HostDispatcher``: the outermost ASGI application (docs/architecture.md, 4.2).

For every HTTP request it makes one decision, from the request host and nothing
else: a registered legacy host goes to the runtime, a management host goes to
the management API, and anything else is refused. The host selects which
surface answers. It is never authentication: the runtime still requires a
legacy-client key and the management API still requires the admin token.
"""

from __future__ import annotations

import ipaddress
import json
from typing import Any

from revenant.config import Settings
from revenant.runtime.hosts import HostError, normalise_host
from revenant.runtime.proxy import RuntimeProxy
from revenant.runtime.state import RuntimeState

PROBES = ("/healthz", "/readyz")
Scope = dict[str, Any]


async def _plain(send: Any, status: int, reason: str) -> None:
    """A minimal response with no request data in it."""
    body = json.dumps({"error": reason}).encode("ascii")
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", b"application/json"),
                            (b"content-length", str(len(body)).encode("ascii"))]})
    await send({"type": "http.response.body", "body": body})


class HostDispatcher:
    def __init__(self, management: Any, proxy: RuntimeProxy, state: RuntimeState,
                 settings: Settings) -> None:
        self.management = management
        self.proxy = proxy
        self.state = state
        self.public_hosts = settings.public_host_set
        self.trust_forwarded = settings.trust_forwarded_host
        self.trusted = settings.trusted_proxy_networks if settings.trust_forwarded_host else []

    def _host(self, scope: Scope) -> str:
        """The normalised request host. Raises ``HostError`` for anything malformed."""
        raw_path = scope.get("raw_path") or scope.get("path", "").encode("latin-1")
        if not raw_path.startswith(b"/"):
            # Absolute-form and authority-form targets are refused, so a target
            # URL can never contradict the Host header.
            raise HostError("the request target is not in origin form")
        headers = scope.get("headers", [])
        if self.trust_forwarded and self._from_trusted_proxy(scope):
            forwarded = [value for name, value in headers if name.lower() == b"x-forwarded-host"]
            if forwarded:
                if len(forwarded) != 1:
                    raise HostError("more than one forwarded host")
                return self._parse(forwarded[0])
        hosts = [value for name, value in headers if name.lower() == b"host"]
        if len(hosts) != 1:
            raise HostError("exactly one Host header is required")
        return self._parse(hosts[0])

    @staticmethod
    def _parse(value: bytes) -> str:
        try:
            text = value.decode("ascii")
        except UnicodeDecodeError as exc:
            raise HostError("non-ASCII host") from exc
        return normalise_host(text)

    def _from_trusted_proxy(self, scope: Scope) -> bool:
        client = scope.get("client")
        if not client:
            return False
        try:
            peer = ipaddress.ip_address(client[0])
        except ValueError:
            return False
        return any(peer in network for network in self.trusted
                   if network.version == peer.version)

    async def __call__(self, scope: Scope, receive: Any, send: Any) -> None:
        if scope["type"] == "lifespan":
            await self.management(scope, receive, send)
            return
        try:
            host = self._host(scope)
        except HostError:
            if scope["type"] == "http":
                await _plain(send, 400, "malformed_host")
            return
        project_id = self.state.hosts.get(host)  # exact equality; no other form of match
        if project_id is not None:
            if scope["type"] == "http":
                # The scope is passed through untouched. The runtime holds no
                # reference to the management application.
                await self.proxy.serve_host(scope, receive, send, project_id)
            return
        if not self.public_hosts or host in self.public_hosts:
            await self.management(scope, receive, send)
            return
        if scope["type"] == "http" and scope["method"] == "GET" and scope["path"] in PROBES:
            # Platform probes do not always send the public hostname.
            await self.management(scope, receive, send)
            return
        if scope["type"] == "http":
            await _plain(send, 421, "misdirected_request")
