"""Operation effect: read-only, state-changing or unknown (docs/architecture.md, 5.4).

HTTP methods are a floor, not the rule: a declaration can make an operation
stricter than its methods suggest, never looser.
"""

from __future__ import annotations

from revenant.transform.model import EFFECTS, SAFE_METHODS


def effective_effect(legacy_method: str, replacement_method: str, declared: str | None) -> str:
    """The classification used for every permission decision."""
    if legacy_method.upper() not in SAFE_METHODS or replacement_method.upper() not in SAFE_METHODS:
        return "state_changing"
    if declared == "state_changing":
        return "state_changing"
    if declared == "read_only":
        return "read_only"
    return "unknown"


def read_only_objections(legacy_method: str, replacement_method: str,
                         markers: list[tuple[str, str]]) -> list[str]:
    """Why an operation may not be declared read-only; empty if it may.

    ``markers`` are ``(endpoint name, effect)`` taken from ``x-revenant-effect``
    in the specifications.
    """
    problems = [f"{side} method {method} is not GET, HEAD or OPTIONS"
                for side, method in (("the legacy", legacy_method),
                                     ("the replacement", replacement_method))
                if method.upper() not in SAFE_METHODS]
    problems += [f"the specification marks {name} as state-changing"
                 for name, effect in markers if effect == "state_changing"]
    return problems


def check_effect(value: str) -> str:
    if value not in EFFECTS:
        raise ValueError(f"effect must be one of {', '.join(EFFECTS)}")
    return value
