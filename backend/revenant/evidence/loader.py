"""Turn uploaded bytes into a document, safely (docs/security.md, "Uploads")."""

from __future__ import annotations

import json
from pathlib import PurePosixPath
from typing import Any

import yaml

ALLOWED_EXTENSIONS = frozenset({".json", ".yaml", ".yml", ".md", ".txt"})
TEXT_EXTENSIONS = frozenset({".md", ".txt"})
MAX_DEPTH = 64
MAX_NODES = 200_000


class EvidenceRejected(Exception):
    """The upload is refused outright (wrong type, too large, not text)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class UnparseableDocument(Exception):
    """The file is an allowed type but cannot be read as a document.

    It is still stored, with these problems in its parse report.
    """

    def __init__(self, problems: list[str]) -> None:
        super().__init__("; ".join(problems))
        self.problems = problems


def safe_filename(name: str | None) -> str:
    """The base name only. Filenames are metadata; they never become paths."""
    base = PurePosixPath((name or "upload").replace("\\", "/")).name
    cleaned = "".join(ch for ch in base if ch.isprintable() and ch not in '<>:"|?*')
    return (cleaned or "upload")[:255]


def extension_of(filename: str) -> str:
    return PurePosixPath(filename).suffix.lower()


def check_upload(filename: str, raw: bytes, max_bytes: int) -> str:
    """Validate type and size and return the text. Raises ``EvidenceRejected``."""
    extension = extension_of(filename)
    if extension not in ALLOWED_EXTENSIONS:
        allowed = ", ".join(sorted(ALLOWED_EXTENSIONS))
        raise EvidenceRejected(
            "unsupported_evidence", f"File type {extension or '(none)'} is not allowed: {allowed}."
        )
    if len(raw) > max_bytes:
        raise EvidenceRejected(
            "payload_too_large", f"The file is larger than the {max_bytes} byte limit."
        )
    if not raw.strip():
        raise EvidenceRejected("unsupported_evidence", "The file is empty.")
    if b"\x00" in raw:
        raise EvidenceRejected("unsupported_evidence", "The file is not text.")
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise EvidenceRejected("unsupported_evidence", "The file is not UTF-8 text.") from exc


def _check_shape(node: Any) -> None:
    """Bound depth and size without recursion, so hostile input cannot exhaust the stack."""
    stack: list[tuple[Any, int]] = [(node, 1)]
    seen = 0
    while stack:
        current, depth = stack.pop()
        seen += 1
        if seen > MAX_NODES:
            raise UnparseableDocument([f"document has more than {MAX_NODES} values"])
        if depth > MAX_DEPTH:
            raise UnparseableDocument([f"document is nested more than {MAX_DEPTH} levels deep"])
        if isinstance(current, dict):
            for key, value in current.items():
                if not isinstance(key, str):
                    raise UnparseableDocument(["document has a non-text object key"])
                stack.append((value, depth + 1))
        elif isinstance(current, list):
            stack.extend((item, depth + 1) for item in current)


def load_structured(text: str, filename: str) -> Any:
    """Parse JSON or YAML with safe loaders. Raises ``UnparseableDocument``."""
    extension = extension_of(filename)
    try:
        if extension == ".json":
            document = json.loads(text)
        else:
            # safe_load: no Python object construction, no arbitrary tags.
            document = yaml.safe_load(text)
    except RecursionError as exc:
        raise UnparseableDocument(["document is nested too deeply to read"]) from exc
    except (json.JSONDecodeError, yaml.YAMLError) as exc:
        position = ""
        line = getattr(exc, "lineno", None) or getattr(
            getattr(exc, "problem_mark", None), "line", None
        )
        if isinstance(line, int):
            position = f" near line {line + (0 if extension == '.json' else 1)}"
        kind = "JSON" if extension == ".json" else "YAML"
        raise UnparseableDocument([f"not valid {kind}{position}"]) from exc
    _check_shape(document)
    return document


def detect_kind(document: Any, filename: str) -> str | None:
    """What kind of evidence a parsed document is, or None if unrecognised."""
    if extension_of(filename) in TEXT_EXTENSIONS:
        return "markdown"
    if not isinstance(document, dict):
        return None
    if "revenant_fixtures" in document:
        return "fixtures"
    if isinstance(document.get("openapi"), str) and "paths" in document:
        return "openapi"
    if str(document.get("swagger", "")).startswith("2") and "paths" in document:
        return "swagger"
    if "$schema" in document or ("type" in document and "properties" in document):
        return "json_schema"
    return None
