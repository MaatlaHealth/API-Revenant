"""Repository consistency checks for the specification and configuration.

Not application code. Run from the repository root:

    python tools/check_spec.py

Checks:
  1. Acceptance criteria in the Kiro requirements are numbered consecutively.
  2. Every criterion is referenced by at least one task; no task references a
     criterion that does not exist.
  3. Kiro hooks JSON, TOML and YAML configuration files parse.
  4. render.yaml keeps the single-worker, single-instance, no-access-log settings.
  5. .env.example lease defaults satisfy the documented inequalities.
  6. No configuration for a backend host other than Render is present.
  7. Every document that names one test-target permission names all three.
"""

from __future__ import annotations

import json
import re
import sys
import tomllib
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
SPEC = ROOT / ".kiro" / "specs" / "api-revenant"
PERMISSIONS = ("allow_replay_traffic", "allow_state_changing_traffic", "allow_generated_traffic")
RETIRED_NAMES = ("allow_state_changing_replay",)

errors: list[str] = []


def key(ref: str) -> tuple[int, ...]:
    return tuple(int(part) for part in ref.split("."))


def check_requirements_and_tasks() -> None:
    have: set[str] = set()
    current = 0
    previous = 0
    per: dict[int, int] = {}
    for line in (SPEC / "requirements.md").read_text(encoding="utf-8").splitlines():
        heading = re.match(r"## Requirement (\d+)", line)
        if heading:
            current, previous = int(heading.group(1)), 0
            continue
        item = re.match(r"(\d+)\. ", line)
        if item and current:
            number = int(item.group(1))
            if number != previous + 1:
                errors.append(f"requirement {current}: criterion {number} follows {previous}")
            previous = number
            per[current] = number
            have.add(f"{current}.{number}")

    refs: set[str] = set()
    for line in (SPEC / "tasks.md").read_text(encoding="utf-8").splitlines():
        if "_Requirements:" in line:
            refs.update(re.findall(r"\d+\.\d+", line))

    dangling = sorted(refs - have, key=key)
    uncovered = sorted(have - refs, key=key)
    if dangling:
        errors.append(f"tasks reference missing criteria: {dangling}")
    if uncovered:
        errors.append(f"criteria with no task: {uncovered}")
    print(f"criteria per requirement: {per}")
    print(f"criteria: {len(have)} | referenced by tasks: {len(refs & have)} "
          f"| dangling: {len(dangling)} | uncovered: {len(uncovered)}")


def check_configs() -> None:
    json.loads((ROOT / ".kiro" / "hooks" / "quality-guards.json").read_text(encoding="utf-8"))
    for name in ("netlify.toml", "backend/pyproject.toml"):
        tomllib.loads((ROOT / name).read_text(encoding="utf-8"))
    for name in ("docker-compose.yml", ".github/workflows/ci.yml"):
        yaml.safe_load((ROOT / name).read_text(encoding="utf-8"))

    render = yaml.safe_load((ROOT / "render.yaml").read_text(encoding="utf-8"))
    service = render["services"][0]
    env = {item["key"]: item.get("value") for item in service["envVars"]}
    start = service["startCommand"]
    if "--workers 1" not in start:
        errors.append("render.yaml: start command must pin --workers 1")
    if "--no-access-log" not in start:
        errors.append("render.yaml: start command must pass --no-access-log")
    if env.get("WEB_CONCURRENCY") != "1":
        errors.append("render.yaml: WEB_CONCURRENCY must be '1'")
    if service.get("numInstances") != 1:
        errors.append("render.yaml: numInstances must be 1")
    if "REVENANT_PUBLIC_HOSTS" not in env:
        errors.append("render.yaml: REVENANT_PUBLIC_HOSTS must be declared")
    print("configuration files parse; render.yaml single-worker settings present")


def check_lease_defaults() -> None:
    values: dict[str, str] = {}
    for line in (ROOT / ".env.example").read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            name, value = line.split("=", 1)
            values[name.strip()] = value.strip()
    grace = int(values["REVENANT_LEASE_GRACE_SECONDS"])
    lifetime = int(values["REVENANT_REQUEST_LIFETIME_SECONDS"])
    margin = int(values["REVENANT_LEASE_CLOCK_MARGIN_SECONDS"])
    ttl = int(values["REVENANT_LEASE_TTL_SECONDS"])
    wait = int(values["REVENANT_LEASE_WAIT_SECONDS"])
    if ttl < grace + lifetime + margin:
        errors.append(f".env.example: TTL {ttl} < grace+lifetime+margin {grace + lifetime + margin}")
    if wait < ttl + 10:
        errors.append(f".env.example: WAIT {wait} < TTL+10 {ttl + 10}")
    if values.get("WEB_CONCURRENCY") != "1":
        errors.append(".env.example: WEB_CONCURRENCY must be 1")
    print(f"lease defaults: TTL {ttl} >= {grace}+{lifetime}+{margin}; WAIT {wait} >= TTL+10")


def tracked_text_files() -> list[Path]:
    skip_dirs = {".git", "node_modules", ".venv", "__pycache__", ".next"}
    suffixes = {".md", ".yaml", ".yml", ".toml", ".json", ".txt", ".example"}
    files = []
    for path in ROOT.rglob("*"):
        if path.is_file() and not (set(path.relative_to(ROOT).parts) & skip_dirs):
            if path.suffix in suffixes or path.name == ".env.example":
                files.append(path)
    return files


def check_text_rules() -> None:
    other_hosts = re.compile(r"railway|fly\.io|heroku", re.IGNORECASE)
    for path in tracked_text_files():
        rel = path.relative_to(ROOT).as_posix()
        text = path.read_text(encoding="utf-8", errors="replace")
        if other_hosts.search(text):
            errors.append(f"{rel}: mentions a backend host other than Render")
        for retired in RETIRED_NAMES:
            if retired in text:
                errors.append(f"{rel}: uses retired permission name {retired}")
        named = [p for p in PERMISSIONS if p in text]
        if named and len(named) != len(PERMISSIONS) and path.suffix == ".md":
            missing = [p for p in PERMISSIONS if p not in named]
            errors.append(f"{rel}: names {named} but not {missing}")
    for forbidden in ("railway.toml", "railway.json", "fly.toml", "Procfile"):
        if (ROOT / forbidden).exists():
            errors.append(f"{forbidden}: configuration for an unsupported backend host")
    print("text rules: Render-only, no retired permission names, permissions named together")


def main() -> int:
    check_requirements_and_tasks()
    check_configs()
    check_lease_defaults()
    check_text_rules()
    if errors:
        print("\nFAILED")
        for error in errors:
            print(f"  - {error}")
        return 1
    print("\nOK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
