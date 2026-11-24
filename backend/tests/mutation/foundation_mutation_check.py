"""Mutation check for authentication, credential storage, redaction and errors.

Same idea as ``outbound_mutation_check.py``: weaken one security control at a
time, run the tests, and require that the weakening is detected. Run from the
``backend`` directory:

    python tests/mutation/foundation_mutation_check.py

Each mutation rewrites one source file and restores it afterwards, even if the
run is interrupted. Exit status is non-zero if any mutation goes undetected.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[2]
TESTS = ["tests/unit", "tests/integration", "tests/security/test_credentials.py",
         "tests/security/test_redaction.py"]

DEPS = "revenant/api/deps.py"
ROUTER = "revenant/api/credentials.py"
CREDENTIALS = "revenant/security/credentials.py"
LOGS = "revenant/logs.py"
ERRORS = "revenant/api/errors.py"
CONFIG = "revenant/config.py"

_ACTIVE_FOR_PROJECT = 'InboundKey.project_id == project_id, InboundKey.status == "active"'

# name -> (file, text to find exactly once, replacement)
MUTATIONS: dict[str, tuple[str, str, str]] = {
    "accept any admin token": (
        DEPS, "    if not expected or not presented or not matches:", "    if False:",
    ),
    "accept an empty token when none is configured": (
        DEPS, "    if not expected or not presented or not matches:", "    if not matches:",
    ),
    "compare only a prefix of the admin token": (
        DEPS,
        'hmac.compare_digest(presented.encode("utf-8"), expected.encode("utf-8"))',
        'expected.startswith(presented)',
    ),
    "look up the project before checking the token": (
        ROUTER, "    dependencies=[Depends(require_admin)],\n", "",
    ),
    "verify inbound keys from every project": (
        CREDENTIALS, _ACTIVE_FOR_PROJECT, 'InboundKey.status == "active"',
    ),
    "verify revoked inbound keys": (
        CREDENTIALS, _ACTIVE_FOR_PROJECT, "InboundKey.project_id == project_id",
    ),
    "store inbound keys in plaintext": (
        CREDENTIALS,
        "            verifier=self.keys.verifier(plaintext),",
        '            verifier=plaintext.encode("utf-8"),',
    ),
    "store outbound credentials unencrypted": (
        CREDENTIALS,
        '        return self.current.fernet.encrypt(plaintext.encode("utf-8")), '
        "self.current.key_id",
        '        return plaintext.encode("utf-8"), self.current.key_id',
    ),
    "skip validation of credential values": (
        CREDENTIALS, "        _check_value(plaintext, minimum=MIN_INBOUND_KEY_LENGTH)\n", "",
    ),
    "print the value of a resolved credential": (
        CREDENTIALS, "value=[REDACTED])", "value={self.value})",
    ),
    "allow retiring the active credential version": (
        CREDENTIALS, '        if row.status == "active":\n            raise Conflict("The active',
        '        if False:\n            raise Conflict("The active',
    ),
    "do not redact log events": (
        LOGS, "            redact_log_event,\n", "",
    ),
    "log the query string": (
        LOGS, '            "path": scope.get("path"),',
        '            "path": scope.get("path"),\n'
        '            "query": scope.get("query_string", b"").decode("latin-1"),',
    ),
    "log exception messages": (
        LOGS, '    return {"error_type": type(exc).__name__}',
        '    return {"error_type": type(exc).__name__, "error": str(exc)}',
    ),
    "return unexpected error messages to the caller": (
        ERRORS,
        '        log.error("unhandled_error", **error_fields(exc))\n'
        '        return envelope(500, "internal_error", "An internal error occurred.")',
        '        log.error("unhandled_error", **error_fields(exc))\n'
        '        return envelope(500, "internal_error", str(exc))',
    ),
    "echo submitted values in validation errors": (
        ERRORS, '             "issue": error["msg"]}',
        '             "issue": error["msg"], "input": str(error.get("input"))}',
    ),
    "allow more than one worker": (
        CONFIG, "        if value is not None and value != 1:", "        if False:",
    ),
    "allow example secrets in production": (
        CONFIG, "        if not secret_key or _is_placeholder(secret_key):", "        if False:",
    ),
    "allow private upstreams in production": (
        CONFIG, "        if settings.allow_private_upstreams:", "        if False:",
    ),
    "allow a wildcard CORS origin": (
        CONFIG, '    if "*" in settings.cors_origin_list:', "    if False:",
    ),
}


def run_tests(basetemp: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - fixed arguments, no shell
        [sys.executable, "-m", "pytest", "-q", "-x", "--no-header", f"--basetemp={basetemp}",
         *TESTS],
        cwd=BACKEND, capture_output=True, text=True, timeout=900,
    )


def main() -> int:
    originals: dict[str, str] = {}
    for name, (rel, old, _new) in MUTATIONS.items():
        if rel not in originals:
            originals[rel] = (BACKEND / rel).read_text(encoding="utf-8")
        if originals[rel].count(old) != 1:
            print(f"ANCHOR NOT FOUND EXACTLY ONCE in {rel} for mutation: {name}")
            return 2

    rows: list[tuple[str, bool, str]] = []
    with tempfile.TemporaryDirectory(prefix="foundation-mutants-") as tmp:
        baseline = run_tests(Path(tmp) / "baseline")
        if baseline.returncode != 0:
            print("The unmodified code does not pass its tests; fix that first.")
            print(baseline.stdout[-2000:])
            return 2
        for index, (name, (rel, old, new)) in enumerate(MUTATIONS.items()):
            target = BACKEND / rel
            try:
                target.write_text(originals[rel].replace(old, new), encoding="utf-8")
                proc = run_tests(Path(tmp) / f"m{index}")
            finally:
                target.write_text(originals[rel], encoding="utf-8")
            failed = next(
                (line for line in proc.stdout.splitlines() if line.startswith(("FAILED", "ERROR"))),
                "",
            )
            last = proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else ""
            rows.append((name, proc.returncode != 0, failed or last))

    width = max(len(name) for name, _, _ in rows)
    missed = 0
    for name, detected, detail in rows:
        if not detected:
            missed += 1
        print(f"{'DETECTED' if detected else 'MISSED  '}  {name.ljust(width)}  {detail[:90]}")
    print(f"\n{len(rows) - missed} of {len(rows)} mutations detected")
    return 1 if missed else 0


if __name__ == "__main__":
    raise SystemExit(main())
