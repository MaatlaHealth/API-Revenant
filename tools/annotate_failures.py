"""Turn a pytest JUnit report into GitHub Actions error annotations.

Annotations are visible on the run summary without opening the log, which makes
a failure on one platform diagnosable from outside. Messages are shortened and
contain only what pytest reported.

    python tools/annotate_failures.py backend/report.xml
"""

from __future__ import annotations

import sys
from pathlib import Path
from xml.etree import ElementTree  # noqa: S405 - the file is written by pytest in this job

LIMIT = 20


def clean(text: str, length: int) -> str:
    """One line, safe inside a workflow command."""
    flat = " | ".join(line.strip() for line in text.strip().splitlines() if line.strip())
    return flat[:length].replace("%", "%25").replace("\r", "").replace("\n", "%0A")


def main(path: str) -> int:
    report = Path(path)
    if not report.is_file():
        print(f"::warning::no test report at {path}")
        return 0
    shown = 0
    for case in ElementTree.parse(report).getroot().iter("testcase"):  # noqa: S314
        for kind in ("failure", "error"):
            problem = case.find(kind)
            if problem is None or shown >= LIMIT:
                continue
            name = f"{case.get('classname', '')}::{case.get('name', '')}"
            detail = (problem.get("message") or "") + " | " + (problem.text or "")[-1500:]
            print(f"::error title={clean(name, 200)}::{clean(detail, 1800)}")
            shown += 1
    print(f"{shown} failing test(s) annotated")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "report.xml"))
