"""Fixture value correlation: which source field carries the value of a target field.

Input is a list of observations, one per fixture pair: the scalars of the source
message and of the target message, by path. A source correlates with a target
when, in every observation, both are present or both absent, and the values
match item by item under one conversion.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

Observation = tuple[dict[str, list[Any]], dict[str, list[Any]]]  # (source, target)


def _number(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


def format_iso(seconds: float) -> str:
    """``YYYY-MM-DDTHH:MM:SSZ``, or an empty string for a number that is not a date."""
    try:
        return datetime.fromtimestamp(seconds, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (OverflowError, OSError, ValueError):
        return ""


def parse_iso(text: str) -> int | None:
    """Whole seconds since the epoch for ``YYYY-MM-DDTHH:MM:SSZ``; None otherwise."""
    try:
        moment = datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError:
        return None
    return int(moment.timestamp())


def _identity(source: Any, target: Any) -> bool:
    if _number(source) and _number(target):
        return bool(source == target)
    return type(source) is type(target) and bool(source == target)


def _epoch_to_iso(scale: int) -> Callable[[Any, Any], bool]:
    def check(source: Any, target: Any) -> bool:
        return (isinstance(source, int) and not isinstance(source, bool)
                and isinstance(target, str) and source % scale == 0
                and format_iso(source // scale) == target)
    return check


def _iso_to_epoch(scale: int) -> Callable[[Any, Any], bool]:
    def check(source: Any, target: Any) -> bool:
        return (isinstance(source, str) and isinstance(target, int)
                and not isinstance(target, bool) and parse_iso(source) is not None
                and parse_iso(source) == target // scale and target % scale == 0)
    return check


# In order of preference. Names are the ``as`` values of the ``convert`` rule.
CONVERSIONS: list[tuple[str, Callable[[Any, Any], bool]]] = [
    ("identity", _identity),
    ("epoch_s_to_iso8601", _epoch_to_iso(1)),
    ("iso8601_to_epoch_s", _iso_to_epoch(1)),
    ("epoch_ms_to_iso8601", _epoch_to_iso(1000)),
    ("iso8601_to_epoch_ms", _iso_to_epoch(1000)),
    ("string", lambda s, t: _number(s) and isinstance(s, int) and isinstance(t, str)
     and str(s) == t),
    ("integer", lambda s, t: isinstance(s, str) and isinstance(t, int)
     and not isinstance(t, bool) and str(t) == s),
]


def informative(value: Any) -> bool:
    """Values that can be told apart. Booleans, nulls and empty strings prove nothing."""
    return not (value is None or isinstance(value, bool) or value == "")


@dataclass(frozen=True)
class Candidate:
    source: str
    conversion: str
    observations: tuple[int, ...]  # indexes of the observations that confirm it
    distinct: int                  # number of distinct values seen
    weak: bool                     # rests on one small number, which is easily a coincidence


def _weak_value(values: list[Any]) -> bool:
    distinct = {repr(value) for value in values}
    return len(distinct) == 1 and _number(values[0]) and abs(values[0]) < 10


def candidates(observations: list[Observation], target: str) -> list[Candidate]:
    """Every source that correlates with ``target``, best conversion per source."""
    sources = sorted({path for source, _ in observations for path in source})
    found: list[Candidate] = []
    for source in sources:
        for name, check in CONVERSIONS:
            confirming: list[int] = []
            seen: list[Any] = []
            consistent = True
            for index, (source_values, target_values) in enumerate(observations):
                left, right = source_values.get(source), target_values.get(target)
                if left is None and right is None:
                    continue
                if left is None or right is None or len(left) != len(right) or not all(
                        informative(a) and check(a, b) for a, b in zip(left, right, strict=True)):
                    consistent = False
                    break
                confirming.append(index)
                seen.extend(left)
            if consistent and confirming:
                found.append(Candidate(source, name, tuple(confirming),
                                       len({repr(value) for value in seen}), _weak_value(seen)))
                break
    return found


def enum_map(observations: list[Observation], source: str, target: str,
             source_enum: tuple[Any, ...], target_enum: tuple[Any, ...]
             ) -> tuple[dict[str, Any], tuple[int, ...]] | None:
    """The value mapping between two enumerated fields, as far as fixtures show it.

    Returns None if the fields do not move together or the mapping is not a
    consistent one-to-one function.
    """
    mapping: dict[str, Any] = {}
    confirming: list[int] = []
    for index, (source_values, target_values) in enumerate(observations):
        left, right = source_values.get(source), target_values.get(target)
        if left is None and right is None:
            continue
        if left is None or right is None or len(left) != len(right):
            return None
        for a, b in zip(left, right, strict=True):
            if a not in source_enum or b not in target_enum or not isinstance(a, str):
                return None
            if mapping.setdefault(a, b) != b:
                return None
        confirming.append(index)
    if not mapping or len(set(map(repr, mapping.values()))) != len(mapping):
        return None
    return mapping, tuple(confirming)
