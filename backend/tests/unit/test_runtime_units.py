"""Host normalisation, host dispatch and query-parameter credentials
(requirements 6.13 to 6.15, 7.20 to 7.25)."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from revenant.runtime.dispatcher import HostDispatcher
from revenant.runtime.hosts import HostError, normalise_host, registrable_host
from revenant.runtime.proxy import extract_query_credential, leaks, scrub
from revenant.runtime.state import InboundKeys, RuntimeState
from revenant.security.credentials import MasterKeys
from tests.conftest import MASTER_KEY, OTHER_MASTER_KEY, make_settings

# ============================================================================= hosts


@pytest.mark.parametrize(("raw", "expected"), [
    ("api.gravepay.demo", "api.gravepay.demo"), ("API.GravePay.DEMO", "api.gravepay.demo"),
    ("api.gravepay.demo:8000", "api.gravepay.demo"), ("api.gravepay.demo.", "api.gravepay.demo"),
    ("api.gravepay.demo.:443", "api.gravepay.demo"), ("localhost", "localhost"),
    ("xn--bcher-kva.example", "xn--bcher-kva.example"), ("a-b.example", "a-b.example"),
    ("127.0.0.1:8000", "127.0.0.1"), ("[::1]:8000", "[::1]"), ("[::1]", "[::1]"),
    ("a" * 63 + ".example", "a" * 63 + ".example"),
])
def test_well_formed_hosts_are_normalised(raw: str, expected: str) -> None:
    assert normalise_host(raw) == expected
    assert normalise_host(expected) == expected  # normalising twice changes nothing


@pytest.mark.parametrize("raw", [
    "", " ", "api gravepay.demo", "api.gravepay.demo ", "api\tgravepay.demo", "api\x00.demo",
    "api.gravepay.demo/x", "api.gravepay.demo\\x", "user@api.gravepay.demo",
    "api.gravepay.demo?x", "api.gravepay.demo#x", "a.demo,b.demo", "api%2egravepay.demo",
    "api_gravepay.demo", "bücher.example", "api.gravepay.demo:", "api.gravepay.demo:0",
    "api.gravepay.demo:65536", "api.gravepay.demo:80a", "api.gravepay.demo:-1",
    "api.gravepay.demo:8000:1", "a..b", ".a.example", "-a.example", "a-.example",
    "a" * 64 + ".example", ".".join(["a" * 60] * 5), ".", "..", "[::1", "[::1]x", "[1.2.3.4]",
    "[zz::1]", "::1",
])
def test_malformed_hosts_are_rejected(raw: str) -> None:
    with pytest.raises(HostError) as excinfo:
        normalise_host(raw)
    # The message never repeats the value that was sent.
    assert raw not in str(excinfo.value) or len(raw) < 3


def test_registration_uses_the_same_normalisation_and_refuses_unsafe_hosts() -> None:
    management = frozenset({"revenant.example"})
    assert registrable_host(" API.GravePay.Demo.:8000 ", management) == "api.gravepay.demo"
    assert registrable_host("bücher.example") == "xn--bcher-kva.example"
    assert registrable_host("BÜCHER.example") == registrable_host("xn--bcher-kva.example")
    for bad in ("*.gravepay.demo", "*", "10.0.0.1", "[::1]", "localhost", "app.localhost",
                "revenant.example", "REVENANT.example.", "a_b.example", "exa mple.com", ""):
        with pytest.raises(HostError):
            registrable_host(bad, management)


host_text = st.text(alphabet=st.sampled_from(list("abcXYZ019-._:[]@/% \t*ü")), max_size=24)


@settings(max_examples=400, deadline=None, derandomize=True)
@given(raw=host_text)
def test_a_host_accepted_at_registration_always_matches_at_dispatch(raw: str) -> None:
    try:
        registered = registrable_host(raw)
    except HostError:
        return
    # What a client would send for that registration reaches the same key.
    for sent in (registered, registered.upper(), registered + ".", registered + ":8000"):
        assert normalise_host(sent) == registered
    assert "*" not in registered and registered == registered.lower()


# ============================================================================= dispatch


class Recorder:
    def __init__(self, name: str) -> None:
        self.name = name
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        self.calls.append(scope)
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": self.name.encode()})

    async def serve_host(self, scope: dict[str, Any], receive: Any, send: Any,
                         project_id: str) -> None:
        self.calls.append({**scope, "project_id": project_id})
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": self.name.encode()})


def dispatch(headers: list[tuple[bytes, bytes]], *, path: str = "/v1/charges",
             peer: str = "9.9.9.9", raw_path: bytes | None = None,
             **overrides: Any) -> tuple[int, str, Recorder, Recorder]:
    management, runtime = Recorder("management"), Recorder("runtime")
    state = RuntimeState()
    state.set_hosts({"api.gravepay.demo": "project-1"})
    config = make_settings("sqlite://", public_hosts="revenant.example,127.0.0.1", **overrides)
    dispatcher = HostDispatcher(management, runtime, state, config)  # type: ignore[arg-type]
    sent: list[dict[str, Any]] = []

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    scope = {"type": "http", "method": "GET", "path": path,
             "raw_path": raw_path if raw_path is not None else path.encode(),
             "query_string": b"a=1&a=2", "headers": headers, "client": (peer, 5000)}
    asyncio.run(dispatcher(scope, None, send))
    body = b"".join(m.get("body", b"") for m in sent).decode()
    return sent[0]["status"], body, management, runtime


def host(value: str) -> list[tuple[bytes, bytes]]:
    return [(b"host", value.encode())]


def test_three_way_dispatch_by_exact_host() -> None:
    status, body, management, runtime = dispatch(host("API.GravePay.Demo:8000"))
    assert (status, body) == (200, "runtime") and not management.calls
    # The scope reaches the runtime untouched, with only the project id added.
    scope = runtime.calls[0]
    assert scope["project_id"] == "project-1" and scope["raw_path"] == b"/v1/charges"
    assert scope["query_string"] == b"a=1&a=2" and scope["headers"] == host(
        "API.GravePay.Demo:8000")

    status, body, management, runtime = dispatch(host("revenant.example"))
    assert (status, body) == (200, "management") and not runtime.calls

    for other in ("gravepay.demo", "x.api.gravepay.demo", "api.gravepay.demo.evil.example",
                  "evil.example", "api.gravepay.dem", "10.0.0.1"):
        status, body, management, runtime = dispatch(host(other))
        assert status == 421 and not management.calls and not runtime.calls, other


def test_probes_are_answered_for_any_well_formed_host_and_nothing_else_is() -> None:
    for path in ("/healthz", "/readyz"):
        assert dispatch(host("probe.internal"), path=path)[:2] == (200, "management")
    assert dispatch(host("probe.internal"), path="/healthz/x")[0] == 421
    assert dispatch(host("probe.internal"), path="/api/v1/projects")[0] == 421
    # On a legacy host even the probe paths belong to the runtime.
    assert dispatch(host("api.gravepay.demo"), path="/healthz")[1] == "runtime"


def test_malformed_requests_reach_neither_surface() -> None:
    cases: list[list[tuple[bytes, bytes]]] = [
        [], host("api.gravepay.demo") * 2, host("api_gravepay.demo"), host("a b"),
        [(b"host", "bücher.example".encode())], host("api.gravepay.demo:99999"),
        [(b"host", b"api.gravepay.demo"), (b"Host", b"revenant.example")],
    ]
    for headers in cases:
        status, _, management, runtime = dispatch(headers, path="/healthz")
        assert status == 400 and not management.calls and not runtime.calls, headers
    # An absolute-form target could contradict the Host header: refused.
    status, _, management, runtime = dispatch(host("revenant.example"),
                                              raw_path=b"http://api.gravepay.demo/v1/charges")
    assert status == 400 and not management.calls and not runtime.calls


FORWARDED = [b"x-forwarded-host", b"forwarded", b"x-original-host", b"x-host",
             b"x-forwarded-server"]


def test_forwarded_host_headers_never_route_by_default() -> None:
    for name in FORWARDED:
        headers = [*host("evil.example"), (name, b"api.gravepay.demo")]
        assert dispatch(headers)[0] == 421, name
        headers = [*host("revenant.example"), (name, b"api.gravepay.demo")]
        assert dispatch(headers)[1] == "management", name
        # Not even from an address that would be trusted if trust were enabled.
        assert dispatch(headers, peer="10.0.0.5", trusted_proxies="10.0.0.0/8")[1] == "management"


def test_forwarded_host_is_used_only_from_a_trusted_proxy() -> None:
    trust = {"trust_forwarded_host": True, "trusted_proxies": "10.0.0.0/8, fd00::/8"}
    forwarded = [*host("internal-lb"), (b"x-forwarded-host", b"API.GravePay.Demo")]
    assert dispatch(forwarded, peer="10.1.2.3", **trust)[1] == "runtime"
    assert dispatch(forwarded, peer="fd00::7", **trust)[1] == "runtime"
    # From any other peer the header is ignored and the real Host decides.
    assert dispatch(forwarded, peer="203.0.113.9", **trust)[0] == 421
    assert dispatch([*host("revenant.example"), (b"x-forwarded-host", b"api.gravepay.demo")],
                    peer="203.0.113.9", **trust)[1] == "management"
    # Only that one header, exactly once, and it must be well formed.
    for name in FORWARDED[1:]:
        assert dispatch([*host("internal-lb"), (name, b"api.gravepay.demo")],
                        peer="10.1.2.3", **trust)[0] == 421, name
    twice = [*host("internal-lb"), (b"x-forwarded-host", b"api.gravepay.demo"),
             (b"x-forwarded-host", b"revenant.example")]
    assert dispatch(twice, peer="10.1.2.3", **trust)[0] == 400
    bad = [*host("internal-lb"), (b"x-forwarded-host", b"api.gravepay.demo, evil.example")]
    assert dispatch(bad, peer="10.1.2.3", **trust)[0] == 400


# ============================================================================= query credentials


@pytest.mark.parametrize(("raw", "sanitised", "value", "count"), [
    ("api_key=s3cret&page=2&limit=20", "page=2&limit=20", "s3cret", 1),
    ("page=2&api_key=s3cret&limit=20", "page=2&limit=20", "s3cret", 1),
    ("page=2&limit=20&api_key=s3cret", "page=2&limit=20", "s3cret", 1),
    ("b=2&a=1&b=3&api_key=k&a=%20x+y&&c", "b=2&a=1&b=3&a=%20x+y&&c", "k", 1),
    ("api%5Fkey=a%2Bb+c%26d", "", "a+b c&d", 1),
    ("api_key=one&x=1&api_key=two", "x=1", "two", 2),
    ("api_key=one&api%5fkey=one", "", "one", 2),
    ("API_KEY=s3cret&api_keys=x&xapi_key=y", "API_KEY=s3cret&api_keys=x&xapi_key=y", None, 0),
    ("api_key", "", "", 1), ("api_key=", "", "", 1), ("", "", None, 0),
    ("page=2", "page=2", None, 0), ("x=api_key%3Ds3cret", "x=api_key%3Ds3cret", None, 0),
])
def test_the_carrier_is_removed_and_everything_else_is_untouched(
    raw: str, sanitised: str, value: str | None, count: int
) -> None:
    assert extract_query_credential(raw, "api_key") == (sanitised, value, count)


segments = st.lists(st.from_regex(r"[a-z%0-9+]{0,6}(=[a-zA-Z0-9%+._-]{0,8})?", fullmatch=True)
                    .filter(lambda s: not s.split("=")[0].replace("%5f", "_").replace(
                        "%5F", "_").startswith("api_key")), max_size=6)


@settings(max_examples=300, deadline=None, derandomize=True)
@given(before=segments, after=segments,
       secret=st.text(alphabet="abcdefXYZ0189", min_size=12, max_size=24))
def test_any_query_with_one_carrier_loses_exactly_that_segment(
    before: list[str], after: list[str], secret: str
) -> None:
    raw = "&".join([*before, f"api_key={secret}", *after])
    sanitised, value, count = extract_query_credential(raw, "api_key")
    assert (value, count) == (secret, 1)
    assert sanitised == "&".join([*before, *after])   # byte-identical, same order
    assert secret not in sanitised


def test_leak_detection_covers_raw_and_encoded_forms() -> None:
    secret = "key/with+odd chars&="
    assert leaks(secret, "https://up.example/x", '{"a": "key/with+odd chars&="}')
    assert leaks(secret, "https://up.example/x?k=key%2Fwith%2Bodd%20chars%26%3D")
    assert leaks(secret, "https://up.example/key/with%2Bodd%20chars%26%3D")
    assert not leaks(secret, "https://up.example/x", '{"a": "something else"}')
    assert not leaks(None, secret) and not leaks("", secret)


def test_scrubbing_replaces_a_credential_wherever_it_appears() -> None:
    secret = 'tok"en/1 2'
    value = {"headers": {"x": f"Bearer {secret}"}, "list": [secret, {"deep": f"a{secret}b"}],
             "encoded": "tok%22en%2F1%202", "n": 5}
    cleaned = scrub(value, [secret])
    assert secret not in str(cleaned) and "tok%22en" not in str(cleaned)
    assert cleaned["n"] == 5 and cleaned["headers"]["x"] == "Bearer [REDACTED]"
    assert scrub(value, []) is value


# ============================================================================= inbound keys


def test_inbound_verification_in_memory_compares_every_key() -> None:
    keys = MasterKeys(MASTER_KEY, OTHER_MASTER_KEY)
    assert keys.previous is not None
    stored = InboundKeys((
        ("first", keys.verifier("first-key-value-0001"), keys.current.key_id),
        ("old", keys.verifier("old-key-value-0002", keys.previous), keys.previous.key_id),
        ("orphan", keys.verifier("orphan-key-value-03"), "unknown-key-id"),
    ))
    assert stored.verify(keys, "first-key-value-0001") == "first"
    assert stored.verify(keys, "old-key-value-0002") == "old"       # previous master key
    assert stored.verify(keys, "orphan-key-value-03") is None       # its master key is gone
    for wrong in ("", "first-key-value-0002", "FIRST-KEY-VALUE-0001", "x" * 500):
        assert stored.verify(keys, wrong) is None
    assert InboundKeys().verify(keys, "first-key-value-0001") is None
    # No key value is held: only verifiers.
    assert "first-key-value-0001" not in repr(stored)
