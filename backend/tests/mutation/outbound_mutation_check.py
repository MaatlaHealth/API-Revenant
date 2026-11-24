"""Mutation check for ``revenant.security.outbound``.

Weakens the outbound guard in one specific way at a time, runs the security
tests against the weakened module, and reports whether the weakening was
detected. This is how we know the tests can fail.

It is a script, not a pytest module (its name does not start with ``test_``),
because it runs the suite many times. Run from the ``backend`` directory:

    python tests/mutation/outbound_mutation_check.py

Exit status is non-zero if any mutation not marked EQUIVALENT goes undetected.
The original file is always restored, even if the run is interrupted.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[2]
TARGET = BACKEND / "revenant" / "security" / "outbound.py"
TESTS = ["tests/security"]

MUTATIONS: dict[str, tuple[str, str]] = {
    "pick the good address from a mixed answer": (
        """        for raw in addresses:
            reason = self.reason_blocked(raw, host)
            if reason is not None:
                raise OutboundBlocked(
                    f"{host}: resolved address {raw} is not allowed ({reason}); "
                    f"refusing all {len(addresses)} address(es)"
                )
        return list(addresses)""",
        """        good = [raw for raw in addresses if self.reason_blocked(raw, host) is None]
        if not good:
            raise OutboundBlocked(f"{host}: nothing allowed")
        return good""",
    ),
    "validate only the first address": (
        "        for raw in addresses:\n            reason = self.reason_blocked(raw, host)",
        "        for raw in addresses[:1]:\n            reason = self.reason_blocked(raw, host)",
    ),
    "skip validation of IP-literal hosts": (
        "        addresses = [str(literal)]\n    # 2 + 3.",
        "        return [str(literal)]\n    # 2 + 3.",
    ),
    "connect by hostname (second DNS lookup)": (
        "                stream = await self.connector(\n                    ip, port,",
        "                stream = await self.connector(\n                    host, port,",
    ),
    "do not unwrap IPv4-mapped / NAT64 addresses": (
        "            inner = _embedded_ipv4(addr)",
        "            inner = None",
    ),
    "trust proxy environment variables": (
        "        trust_env=False,",
        "        trust_env=True,",
    ),
    "allow caller to override SNI": (
        'extensions = {k: v for k, v in request.extensions.items() if k != "sni_hostname"}',
        "extensions = dict(request.extensions)",
    ),
    "disable TLS hostname checking": (
        "            ssl_context=ssl_context or ssl.create_default_context(),",
        "            ssl_context=_weaken(ssl_context or ssl.create_default_context()),",
    ),
    "skip peer-address check": (
        "    if peer is None or ipaddress.ip_address(peer[0]) != ipaddress.ip_address(ip):",
        "    if False:",
    ),
    "drop the not-globally-routable rule (CGNAT etc.)": (
        '        if not addr.is_global:\n            return "not globally routable"\n',
        "",
    ),
    "drop the loopback, private and global rules": (
        '        if addr.is_loopback:\n            return "loopback"\n'
        '        if addr.is_private:\n            return "private"\n'
        '        if addr.is_reserved:\n            return "reserved"\n'
        '        if not addr.is_global:\n            return "not globally routable"\n',
        "",
    ),
    "follow redirects by default": (
        "    follow_redirects: bool = False,\n) -> httpx.AsyncClient:",
        "    follow_redirects: bool = True,\n) -> httpx.AsyncClient:",
    ),
    "allow unix sockets": (
        '        raise OutboundBlocked("unix sockets are not permitted for outbound connections")',
        "        return await httpcore.AnyIOBackend().connect_unix_socket(path, timeout=timeout)",
    ),
    "let an allowlisted host reach link-local / metadata addresses": (
        "        hard = _never_allowed(addr)\n"
        "        if hard is not None:\n"
        "            return hard\n",
        "",
    ),
    "apply the host allowlist to every host": (
        "        if host is not None and host.lower().rstrip(\".\") in self.allowed_hosts:",
        "        if self.allowed_hosts:",
    ),
    # Adding link-local to the demo networks changes nothing while the never-allowed
    # check runs first; that check has its own mutation above. Kept to document it.
    "EQUIVALENT: let the demo flag list link-local addresses": (
        '    ipaddress.ip_network("fc00::/7"),\n)',
        '    ipaddress.ip_network("fc00::/7"),\n    ipaddress.ip_network("169.254.0.0/16"),\n)',
    ),
    "allow any URL scheme": (
        "    if parsed.scheme not in _SAFE_SCHEMES:",
        "    if False:",
    ),
    "allow plain http in production": (
        '    if settings.is_production and parsed.scheme != "https":',
        "    if False:",
    ),
    "allow credentials in the URL": (
        "    if parsed.userinfo:",
        "    if False:",
    ),
    "allow any port": (
        "    if port not in settings.allowed_ports:",
        "    if False:",
    ),
}

WEAKEN = '''

def _weaken(ctx):  # type: ignore[no-untyped-def]
    ctx.check_hostname = False
    return ctx
'''


def run_tests(basetemp: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - fixed arguments, no shell
        [sys.executable, "-m", "pytest", "-q", "-x", "--no-header", f"--basetemp={basetemp}",
         *TESTS],
        cwd=BACKEND, capture_output=True, text=True, timeout=900,
    )


def main() -> int:
    original = TARGET.read_text(encoding="utf-8")
    for name, (old, _new) in MUTATIONS.items():
        if original.count(old) != 1:
            print(f"ANCHOR NOT FOUND EXACTLY ONCE for mutation: {name}")
            return 2

    rows: list[tuple[str, bool, str]] = []
    with tempfile.TemporaryDirectory(prefix="outbound-mutants-") as tmp:
        baseline = run_tests(Path(tmp) / "baseline")
        if baseline.returncode != 0:
            print("The unmodified module does not pass its tests; fix that first.")
            print(baseline.stdout[-2000:])
            return 2
        try:
            for index, (name, (old, new)) in enumerate(MUTATIONS.items()):
                mutated = original.replace(old, new)
                if "_weaken(" in mutated:
                    mutated += WEAKEN
                TARGET.write_text(mutated, encoding="utf-8")
                proc = run_tests(Path(tmp) / f"m{index}")
                failed = next(
                    (line for line in proc.stdout.splitlines() if line.startswith("FAILED")), ""
                )
                last = proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else ""
                rows.append((name, proc.returncode != 0, failed or last))
        finally:
            TARGET.write_text(original, encoding="utf-8")

    width = max(len(name) for name, _, _ in rows)
    missed = 0
    for name, detected, detail in rows:
        equivalent = name.startswith("EQUIVALENT")
        label = "DETECTED" if detected else ("EQUIV   " if equivalent else "MISSED  ")
        if not detected and not equivalent:
            missed += 1
        print(f"{label}  {name.ljust(width)}  {detail[:100]}")
    real = sum(1 for name, _, _ in rows if not name.startswith("EQUIVALENT"))
    print(f"\n{real - missed} of {real} non-equivalent mutations detected")
    return 1 if missed else 0


if __name__ == "__main__":
    raise SystemExit(main())
