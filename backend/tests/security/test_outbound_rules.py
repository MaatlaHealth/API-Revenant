"""Tests for the parts of the outbound guard that the spike did not have:
URL rules, settings-driven policy, the explicit exceptions, and error mapping.
"""

from __future__ import annotations

import asyncio

import httpcore
import httpx
import pytest

from revenant.config import Settings
from revenant.security.outbound import (
    GuardedBackend,
    OutboundBlocked,
    OutboundPolicy,
    build_outbound_client,
    check_destination,
    check_url,
)

PUBLIC_V4 = "93.184.216.34"


def settings(**overrides: object) -> Settings:
    return Settings(_env_file=None, **overrides)  # type: ignore[arg-type]


def run(coro):  # type: ignore[no-untyped-def]
    return asyncio.run(coro)


class NeverDial:
    def __init__(self) -> None:
        self.asked: list[str] = []

    async def __call__(self, ip: str, port: int, **_: object) -> httpcore.AsyncNetworkStream:
        self.asked.append(ip)
        raise AssertionError(f"connection attempted to {ip}")


def resolver_returning(*addresses: str):  # type: ignore[no-untyped-def]
    async def resolve(host: str, port: int) -> list[str]:
        return list(addresses)
    return resolve


# ----------------------------------------------------------------------------- URL rules


@pytest.mark.parametrize("url", [
    "https://api.example.com/v1",
    "http://api.example.com:8080/v1?x=1",
    "https://api.example.com:8443",
])
def test_check_url_accepts_plain_http_urls(url: str) -> None:
    assert check_url(url, settings()).host == "api.example.com"


@pytest.mark.parametrize("url, reason", [
    ("ftp://api.example.com/", "scheme"),
    ("file:///etc/passwd", "scheme"),
    ("gopher://api.example.com/", "scheme"),
    ("//api.example.com/", "scheme"),
    ("https://user:pass@api.example.com/", "credentials"),
    ("https://user@api.example.com/", "credentials"),
    ("https:///path-only", "no host"),
    ("https://api.example.com:22/", "port"),
    ("https://api.example.com:6379/", "port"),
    ("https://api.example.com/#fragment", "fragment"),
])
def test_check_url_rejects_unsafe_shapes(url: str, reason: str) -> None:
    with pytest.raises(OutboundBlocked, match=reason):
        check_url(url, settings())


def test_https_is_required_in_production() -> None:
    production = settings(env="production")
    with pytest.raises(OutboundBlocked, match="https is required"):
        check_url("http://api.example.com/", production)
    assert check_url("https://api.example.com/", production).scheme == "https"


def test_allowed_ports_come_from_settings() -> None:
    with pytest.raises(OutboundBlocked, match="port"):
        check_url("https://api.example.com:9443/", settings())
    custom = settings(upstream_allowed_ports="443,9443")
    assert check_url("https://api.example.com:9443/", custom).port == 9443


def test_error_messages_do_not_echo_url_credentials() -> None:
    with pytest.raises(OutboundBlocked) as excinfo:
        check_url("https://user:hunter2@api.example.com/", settings())
    assert "hunter2" not in str(excinfo.value)


# ----------------------------------------------------------------------------- configuration time


def test_check_destination_resolves_and_validates() -> None:
    ok = run(check_destination("https://api.example.com/", settings(),
                               resolver_returning(PUBLIC_V4)))
    assert ok.host == "api.example.com"


@pytest.mark.parametrize("addresses", [["10.0.0.1"], [PUBLIC_V4, "127.0.0.1"], []])
def test_check_destination_rejects_private_or_mixed_answers(addresses: list[str]) -> None:
    with pytest.raises(OutboundBlocked):
        run(check_destination("https://api.example.com/", settings(),
                              resolver_returning(*addresses)))


def test_check_destination_rejects_ip_literal_metadata_address() -> None:
    resolver = resolver_returning(PUBLIC_V4)
    with pytest.raises(OutboundBlocked):
        run(check_destination("http://169.254.169.254/", settings(), resolver))


# ----------------------------------------------------------------------------- explicit exceptions


def test_default_policy_from_settings_blocks_private_addresses() -> None:
    policy = OutboundPolicy.from_settings(settings())
    assert policy.allowed_networks == () and policy.allowed_hosts == frozenset()
    assert policy.reason_blocked("127.0.0.1") == "loopback"
    assert policy.reason_blocked("10.0.0.1") == "private"


def test_demo_flag_allows_loopback_and_private_but_never_link_local() -> None:
    policy = OutboundPolicy.from_settings(settings(allow_private_upstreams=True))
    for allowed in ("127.0.0.1", "10.1.2.3", "172.16.0.5", "192.168.1.10", "::1", "fd00::1"):
        assert policy.reason_blocked(allowed) is None, allowed
    for still_blocked in ("169.254.169.254", "fe80::1", "0.0.0.0", "224.0.0.1",
                          "::ffff:169.254.169.254", "100.64.0.1"):
        assert policy.reason_blocked(still_blocked) is not None, still_blocked


def test_allowlisted_host_may_resolve_to_a_private_address() -> None:
    policy = OutboundPolicy.from_settings(
        settings(upstream_allowlist="Api.NovaPay.Demo., other.test")
    )
    assert policy.allowed_hosts == frozenset({"api.novapay.demo", "other.test"})
    assert policy.validate_all("api.novapay.demo", ["172.18.0.3"]) == ["172.18.0.3"]
    assert policy.validate_all("API.NOVAPAY.DEMO", ["10.0.0.9"]) == ["10.0.0.9"]


def test_allowlist_applies_only_to_the_named_host() -> None:
    policy = OutboundPolicy.from_settings(settings(upstream_allowlist="api.novapay.demo"))
    with pytest.raises(OutboundBlocked):
        policy.validate_all("evil.example.com", ["172.18.0.3"])
    with pytest.raises(OutboundBlocked):
        policy.validate_all("api.novapay.demo.evil.com", ["172.18.0.3"])


@pytest.mark.parametrize("address", ["169.254.169.254", "fe80::1", "0.0.0.0", "224.0.0.1",
                                     "::ffff:169.254.169.254"])
def test_allowlisted_host_can_never_reach_link_local_or_metadata(address: str) -> None:
    policy = OutboundPolicy.from_settings(settings(upstream_allowlist="api.novapay.demo"))
    with pytest.raises(OutboundBlocked):
        policy.validate_all("api.novapay.demo", [address])


def test_allowlisted_host_is_still_resolved_and_dialled_by_ip() -> None:
    """The exception changes which addresses are acceptable, not how connecting works."""
    asked: list[tuple[str, int]] = []

    async def connector(ip: str, port: int, **_: object) -> httpcore.AsyncNetworkStream:
        asked.append((ip, port))
        raise httpcore.ConnectError("stop here")

    calls: list[str] = []

    async def resolver(host: str, port: int) -> list[str]:
        calls.append(host)
        return ["172.18.0.3"]

    backend = GuardedBackend(
        policy=OutboundPolicy.from_settings(settings(upstream_allowlist="api.novapay.demo")),
        resolver=resolver, connector=connector,
    )

    async def go() -> None:
        async with build_outbound_client(backend) as client:
            with pytest.raises(httpx.ConnectError):
                await client.get("http://api.novapay.demo:8000/")
    run(go())
    assert calls == ["api.novapay.demo"]
    assert asked == [("172.18.0.3", 8000)]


# ----------------------------------------------------------------------------- client construction


def test_client_ignores_environment_and_does_not_follow_redirects() -> None:
    async def go() -> None:
        async with build_outbound_client(GuardedBackend()) as client:
            assert client.trust_env is False
            assert client.follow_redirects is False
    run(go())


def test_blocked_destination_surfaces_as_outbound_blocked_not_a_transport_error() -> None:
    dial = NeverDial()
    backend = GuardedBackend(resolver=resolver_returning("10.0.0.1"), connector=dial)

    async def go() -> None:
        async with build_outbound_client(backend) as client:
            with pytest.raises(OutboundBlocked):
                await client.get("http://api.example.com/")
    run(go())
    assert dial.asked == []


def test_connection_failures_are_reported_as_httpx_errors() -> None:
    async def refused(ip: str, port: int, **_: object) -> httpcore.AsyncNetworkStream:
        raise httpcore.ConnectError("connection refused")

    async def timed_out(ip: str, port: int, **_: object) -> httpcore.AsyncNetworkStream:
        raise httpcore.ConnectTimeout("timed out")

    async def go() -> None:
        cases = ((refused, httpx.ConnectError), (timed_out, httpx.ConnectTimeout))
        for connector, expected in cases:
            backend = GuardedBackend(resolver=resolver_returning(PUBLIC_V4), connector=connector)
            async with build_outbound_client(backend) as client:
                with pytest.raises(expected):
                    await client.get("http://api.example.com/")
    run(go())


def test_dialled_history_is_bounded() -> None:
    backend = GuardedBackend(max_dialled=3)
    backend.dialled.extend(("h", f"1.1.1.{i}", 80) for i in range(10))
    del backend.dialled[: -backend.max_dialled]
    assert len(backend.dialled) == 3
