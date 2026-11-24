"""Mutation check for the outbound-transport spike.

Weakens the guard in one specific way at a time, runs the test suite against
the weakened copy, and reports whether the weakening was detected.

Exit status is non-zero if any mutation that is not marked EQUIVALENT goes
undetected. Run from anywhere:

    python spikes/outbound_transport/mutation_check.py
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent

MUTATIONS: dict[str, tuple[str, str]] = {
    "pick the good address from a mixed answer": (
        """        for raw in addresses:
            reason = self.reason_blocked(raw)
            if reason is not None:
                raise OutboundBlocked(
                    f"{host}: resolved address {raw} is not allowed ({reason}); "
                    f"refusing all {len(addresses)} address(es)"
                )
        return list(addresses)""",
        """        good = [raw for raw in addresses if self.reason_blocked(raw) is None]
        if not good:
            raise OutboundBlocked(f"{host}: nothing allowed")
        return good""",
    ),
    "validate only the first address": (
        "        for raw in addresses:\n            reason = self.reason_blocked(raw)",
        "        for raw in addresses[:1]:\n            reason = self.reason_blocked(raw)",
    ),
    "skip validation of IP-literal hosts": (
        "            addresses = [str(literal)]\n",
        "            return await self.connector(str(literal), port, timeout=timeout)\n",
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
    "drop the private-address rule and the global rule": (
        '        if addr.is_private:\n            return "private"\n        if addr.is_reserved:\n'
        '            return "reserved"\n        if not addr.is_global:\n'
        '            return "not globally routable"\n',
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
    # Python's ipaddress already classes link-local addresses as private, so removing
    # the explicit rule changes no behaviour. Kept to document that it was checked.
    "EQUIVALENT: drop the explicit link-local rule": (
        '        if addr.is_link_local:\n            return "link-local"\n',
        "",
    ),
}

WEAKEN = '''

def _weaken(ctx):
    ctx.check_hostname = False
    return ctx
'''


def main() -> int:
    original = (HERE / "guarded_transport.py").read_text(encoding="utf-8")
    rows: list[tuple[str, bool, str]] = []
    with tempfile.TemporaryDirectory(prefix="spike-mutants-") as tmp:
        for name, (old, new) in MUTATIONS.items():
            if old not in original:
                print(f"ANCHOR NOT FOUND for mutation: {name}")
                return 2
            target = Path(tmp) / re.sub(r"[^a-z0-9]+", "_", name.lower())
            target.mkdir(parents=True)
            mutated = original.replace(old, new)
            if "_weaken(" in mutated:
                mutated += WEAKEN
            (target / "guarded_transport.py").write_text(mutated, encoding="utf-8")
            shutil.copy(HERE / "test_guarded_transport.py", target / "test_guarded_transport.py")
            proc = subprocess.run(
                [sys.executable, "-m", "pytest", "-q", "-x", "--no-header",
                 "-p", "no:cacheprovider", f"--basetemp={target / 'tmp'}"],
                cwd=target, capture_output=True, text=True, timeout=600,
            )
            failed = next((line for line in proc.stdout.splitlines() if line.startswith("FAILED")), "")
            last = proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else proc.stderr[-200:]
            rows.append((name, proc.returncode != 0, failed or last))

    width = max(len(name) for name, _, _ in rows)
    missed_real = 0
    for name, detected, detail in rows:
        equivalent = name.startswith("EQUIVALENT")
        if detected:
            label = "DETECTED"
        elif equivalent:
            label = "EQUIV   "
        else:
            label = "MISSED  "
            missed_real += 1
        print(f"{label}  {name.ljust(width)}  {detail[:100]}")
    real = sum(1 for name, _, _ in rows if not name.startswith("EQUIVALENT"))
    print(f"\n{real - missed_real} of {real} non-equivalent mutations detected")
    return 1 if missed_real else 0


if __name__ == "__main__":
    raise SystemExit(main())
