"""Shared runner for mutation checks.

A mutation check weakens one control at a time, runs the tests, and requires
that the weakening is detected. Each mutation rewrites one source file and
restores it afterwards, even if the run is interrupted.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[2]

# name -> (file relative to backend/, text to find exactly once, replacement)
Mutations = dict[str, tuple[str, str, str]]


def _run_tests(tests: list[str], basetemp: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - fixed arguments, no shell
        [sys.executable, "-m", "pytest", "-q", "-x", "--no-header", f"--basetemp={basetemp}",
         *tests],
        cwd=BACKEND, capture_output=True, text=True, timeout=900,
    )


def run(mutations: Mutations, tests: list[str]) -> int:
    """Run every mutation. Returns a process exit status."""
    originals: dict[str, str] = {}
    for name, (rel, old, _new) in mutations.items():
        if rel not in originals:
            originals[rel] = (BACKEND / rel).read_text(encoding="utf-8")
        if originals[rel].count(old) != 1:
            print(f"ANCHOR NOT FOUND EXACTLY ONCE in {rel} for mutation: {name}")
            return 2

    rows: list[tuple[str, bool, str]] = []
    with tempfile.TemporaryDirectory(prefix="mutants-") as tmp:
        baseline = _run_tests(tests, Path(tmp) / "baseline")
        if baseline.returncode != 0:
            print("The unmodified code does not pass its tests; fix that first.")
            print(baseline.stdout[-2000:])
            return 2
        for index, (name, (rel, old, new)) in enumerate(mutations.items()):
            target = BACKEND / rel
            try:
                target.write_text(originals[rel].replace(old, new), encoding="utf-8")
                proc = _run_tests(tests, Path(tmp) / f"m{index}")
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
