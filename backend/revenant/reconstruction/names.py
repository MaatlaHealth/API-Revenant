"""Name normalisation for the heuristic proposers.

A match found here is never evidence on its own: mappings that rest on it are
classified ``inferred`` and need approval.
"""

from __future__ import annotations

import re

_SPLIT = re.compile(r"[^A-Za-z0-9]+|(?<=[a-z0-9])(?=[A-Z])")
_SYNONYMS = {"number": "no", "num": "no", "identifier": "id", "ref": "reference"}
# Trailing words that often differ between two contracts for the same field.
_SUFFIXES = ("id", "code", "at")


def tokens(name: str) -> tuple[str, ...]:
    """``customerId`` and ``customer_id`` both become ``("customer",)``."""
    parts = [_SYNONYMS.get(part.lower(), part.lower()) for part in _SPLIT.split(name) if part]
    while len(parts) > 1 and parts[-1] in _SUFFIXES:
        parts.pop()
    return tuple(parts)


def singular(word: str) -> str:
    if word.endswith("ies") and len(word) > 4:
        return word[:-3] + "y"
    if word.endswith("s") and not word.endswith("ss") and len(word) > 3:
        return word[:-1]
    return word


def segments(path: str) -> list[str]:
    """Field names along a pointer, without array markers: ``$.a[].b`` -> ``[a, b]``."""
    return [part for part in path.replace("[]", "").split(".")[1:] if part]


def leaf(path: str) -> str:
    parts = segments(path)
    return parts[-1] if parts else ""


def same_name(left: str, right: str) -> bool:
    return bool(tokens(left)) and tokens(left) == tokens(right)


def same_path(left: str, right: str) -> bool:
    """Every segment matches after normalisation, and arrays sit in the same places."""
    a, b = segments(left), segments(right)
    if len(a) != len(b) or left.count("[]") != right.count("[]"):
        return False
    return all(same_name(x, y) for x, y in zip(a, b, strict=True))
