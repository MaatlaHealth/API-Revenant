# Spike: SSRF-safe outbound transport

**This is a feasibility spike, not application code.** It proves that the outbound-connection rules in `docs/security.md` can be implemented on the chosen HTTP client. Nothing in `backend/` imports it. It has since been promoted to `backend/revenant/security/outbound.py`, which adds the application's configuration, URL rules and error types; the tests here are carried across unchanged.

## What was selected

| Item | Choice |
|---|---|
| HTTP client | `httpx` (async) |
| Connection layer | `httpcore` connection pool |
| Extension point | `httpcore.AsyncNetworkBackend`, passed to `httpcore.AsyncConnectionPool(network_backend=...)`, wrapped in a custom `httpx.AsyncBaseTransport` |
| Versions tested | Python 3.12.10, httpx 0.28.1, httpcore 1.0.9, anyio 4.16.0, h11 0.16.0, cryptography 50.0.2, pytest 9.2.0 |
| Platforms tested | Windows 11 (local); `ubuntu-latest` and `windows-latest` (GitHub Actions) |

Only public interfaces are used. `httpx.AsyncHTTPTransport` does not expose the network backend, so the spike supplies its own small transport around an `httpcore` pool instead of reaching into private attributes.

## How it works

`httpcore` calls `connect_tcp(host, port)` on its network backend once for every **new** connection. `GuardedBackend`:

1. resolves `host` at that moment (A and AAAA);
2. validates every returned address and refuses the whole answer if any is disallowed;
3. asks the real backend to connect to a validated **IP literal**, so the hostname never reaches the operating-system resolver a second time;
4. confirms the socket's peer address is the address that was validated.

`httpcore` then performs the TLS handshake itself with `server_hostname` set to the original hostname, so SNI and certificate verification are unchanged. The transport removes the `sni_hostname` request extension so a caller cannot override that name, has no proxy support, never retries and does not speak HTTP/2. The client is built with `trust_env=False` and `follow_redirects=False`.

## Running

```
python -m venv .venv
.venv\Scripts\python -m pip install -r spikes/outbound_transport/requirements.txt
cd spikes/outbound_transport
..\..\.venv\Scripts\python -m pytest -v
```

No external network is contacted. Local HTTP and TLS servers bind to `127.0.0.1` on ephemeral ports; the TLS tests generate a throwaway certificate authority and certificates in a temporary directory.

To reproduce the exact tested versions, install `requirements.lock.txt` instead. To confirm the tests detect a weakened guard, run `python spikes/outbound_transport/mutation_check.py`; it exits non-zero if any non-equivalent weakening goes undetected.

## Linux status

**Passed on Linux and Windows in CI on 2026-11-24**, commit `cf9841f`, from `requirements.lock.txt`: [workflow run](https://github.com/MaatlaHealth/API-Revenant/actions/runs/45490838142). On `ubuntu-latest` and on `windows-latest` the dependency install, the complete test suite and the mutation check each completed successfully. With that result the transport was accepted as an application dependency and promoted to `backend/revenant/security/outbound.py`.

This directory is now kept as the reference the application tests are generated from (`tools/sync_outbound_tests.py`); CI still runs it.

## Results (2026-11-24)

`78 passed`.

| Requirement | Tests | Result |
|---|---|---|
| Resolve immediately before connecting | `test_resolves_immediately_before_connecting` | Pass |
| Validate every IPv4 and IPv6 address | 34 blocked-address cases, 7 allowed-address cases, `test_all_public_dual_stack_answer_is_accepted`, 5 IP-literal URL cases, 3 real-resolver cases | Pass |
| Reject mixed allowed/disallowed answers | `test_mixed_or_bad_answers_are_refused_entirely` (7 cases), `test_allowlist_does_not_disable_validation_of_other_addresses` | Pass |
| Connect to the validated IP without another lookup | `test_connects_to_validated_ip_without_second_dns_lookup` (real sockets; any OS lookup of a hostname fails the test), `test_peer_address_must_equal_the_validated_address`, `test_connection_is_dropped_if_the_peer_is_not_the_validated_address` | Pass |
| Preserve hostname for SNI and certificate verification | `test_tls_verifies_certificate_for_original_hostname_while_connected_by_ip`, `..._rejects_certificate_issued_for_a_different_hostname`, `..._rejects_certificate_from_untrusted_authority`, `test_caller_cannot_override_sni_hostname` | Pass |
| Redirects cannot bypass validation | `test_redirects_are_not_followed`, `test_redirect_cannot_bypass_validation_even_if_following_were_enabled` (3 cases) | Pass |
| Ignore proxy environment variables | `test_proxy_environment_variables_are_ignored`, plus a control showing a default client does use the proxy | Pass |
| Revalidate every new connection | `test_every_new_connection_is_resolved_and_validated_again`, `test_keepalive_connection_is_reused_without_a_new_resolution`, `test_expired_keepalive_connection_triggers_revalidation`, two DNS-rebinding tests | Pass |
| Other | `test_unix_sockets_are_refused` | Pass |

The TLS certificate contains the DNS name `api.novapay.test` and **no IP address**, while the socket is connected to `127.0.0.1`. Verification can therefore only succeed if the hostname is what is checked.

### Mutation check

To confirm the tests can fail, the guard was deliberately weakened fourteen ways and the suite re-run each time. Thirteen weakenings were detected:

pick the good address from a mixed answer · validate only the first address · skip validation of IP-literal hosts · connect by hostname · do not unwrap embedded IPv4 · trust proxy environment · allow SNI override · disable TLS hostname checking · skip the peer-address check · drop the not-globally-routable rule · drop the private and global rules · follow redirects by default · allow unix sockets.

One was not detected and is an equivalent mutant: removing the explicit link-local rule changes nothing, because Python's `ipaddress` already classes link-local addresses as private. The first mutation run found a real gap — nothing failed when the peer-address check was removed — and a test was added for it.

## Limitations of this spike

- Tested with the asyncio event loop only, on Windows locally and on Linux and Windows in CI.
- The async path only. A synchronous client is not needed by the design.
- HTTP/1.1 only; HTTP/2 is disabled on purpose.
- The test certificate chain is one authority deep, with ECDSA P-256 keys.
- Connection is attempted to validated addresses in order; there is no "happy eyeballs" racing of IPv4 and IPv6.
- The embedded-IPv4 unwrapping is not load-bearing on Python 3.12, where the standard library already rejects those forms; it is kept so behaviour does not depend on the interpreter version.
- It relies on `httpcore`'s network-backend interface and on `httpcore` passing the origin hostname to TLS. Both are public behaviour today; a major-version change could alter them, so the dependency range must be pinned and these tests kept in the suite.
- Reused keep-alive connections are not re-resolved. They stay bound to the address validated when they were opened and are closed after the keep-alive expiry.
