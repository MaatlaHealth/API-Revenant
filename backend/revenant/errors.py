"""Domain errors shared across the backend."""

from __future__ import annotations


class ConfigurationError(Exception):
    """The process configuration is unsafe or incomplete. Raised at start-up."""

    def __init__(self, problems: list[str]) -> None:
        self.problems = list(problems)
        super().__init__("; ".join(self.problems))


class OutboundBlocked(Exception):
    """An outbound destination failed validation. No connection was attempted."""
