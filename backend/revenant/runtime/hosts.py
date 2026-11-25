"""Strict host normalisation (docs/architecture.md, section 4.2).

One function is used both when a legacy host is registered and when a request
is dispatched, so the two can never disagree. Matching is exact equality on the
normalised form: no wildcards, no suffixes, no default host.
"""

from __future__ import annotations

import ipaddress
import re

import idna

_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_FORBIDDEN = set("/\\@?#,%_ \t\r\n")
MAX_NAME = 253


class HostError(ValueError):
    """The host is malformed, or cannot be registered. The message never
    repeats the offending value."""


def _is_ip(text: str) -> bool:
    try:
        ipaddress.ip_address(text)
    except ValueError:
        return False
    return True


def is_ip_literal(host: str) -> bool:
    return host.startswith("[") or _is_ip(host)


def normalise_host(raw: str) -> str:
    """ASCII-lowercase, port removed, one trailing dot removed. Raises ``HostError``.

    Nothing is percent-decoded and no Unicode processing happens here.
    """
    if not raw or not raw.isascii():
        raise HostError("empty or non-ASCII host")
    if any(ch in _FORBIDDEN or ord(ch) < 33 or ord(ch) == 127 for ch in raw):
        raise HostError("forbidden character in host")
    host, port = raw, None
    if raw.startswith("["):  # bracketed IPv6 literal, optionally with a port
        end = raw.find("]")
        if end == -1:
            raise HostError("unterminated IPv6 literal")
        host, rest = raw[: end + 1], raw[end + 1:]
        if rest:
            if not rest.startswith(":"):
                raise HostError("unexpected text after IPv6 literal")
            port = rest[1:]
        try:
            if ipaddress.ip_address(host[1:-1]).version != 6:
                raise HostError("not an IPv6 literal")
        except ValueError as exc:
            raise HostError("not an IPv6 literal") from exc
    elif raw.count(":") == 1:
        host, port = raw.split(":")
    elif ":" in raw:
        raise HostError("unexpected colon in host")
    if port is not None and not (port.isdigit() and len(port) <= 5 and 1 <= int(port) <= 65535):
        raise HostError("invalid port")
    host = host.lower()
    if host.startswith("["):
        return host
    if host.endswith("."):
        host = host[:-1]
    if not host or len(host) > MAX_NAME:
        raise HostError("empty or over-long host name")
    if _is_ip(host):
        return host
    if not all(_LABEL.match(label) for label in host.split(".")):
        raise HostError("invalid label in host name")
    return host


def registrable_host(raw: str, management_hosts: frozenset[str] = frozenset()) -> str:
    """The normalised form of a legacy host that may be registered.

    Unicode names are converted to A-labels first. Wildcards, IP literals,
    ``localhost`` and management hosts are refused.
    """
    text = raw.strip()
    if "*" in text:
        raise HostError("wildcards are not allowed")
    if not text.isascii():
        try:
            text = idna.encode(text.rstrip("."), uts46=True).decode("ascii")
        except idna.IDNAError as exc:
            raise HostError("not a valid internationalised name") from exc
    host = normalise_host(text)
    if is_ip_literal(host):
        raise HostError("an IP address cannot be a legacy host")
    if host == "localhost" or host.endswith(".localhost"):
        raise HostError("localhost cannot be a legacy host")
    if host in management_hosts:
        raise HostError("this is one of the management hosts")
    return host
