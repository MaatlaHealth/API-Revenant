"""What the runtime holds in memory: loaded adapters, resolved credentials,
inbound key verifiers and the host table.

The request path reads only this object. It performs no database access, so a
database outage does not stop loaded runtimes (within the lease grace period).
"""

from __future__ import annotations

import hmac
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from types import MappingProxyType
from typing import Any

from revenant.adapter.document import AdapterDocument
from revenant.security.credentials import MasterKeys, ResolvedCredential


@dataclass(frozen=True)
class InboundKeys:
    """Verifiers of the active legacy-client keys of one project. No key values."""

    entries: tuple[tuple[str, bytes, str], ...] = ()  # (label, verifier, master key id)

    def verify(self, keys: MasterKeys, presented: str) -> str | None:
        """The label of the matching key, or None. Every entry is compared, in
        constant time, so timing reveals neither which key matched nor how many exist."""
        matched: str | None = None
        for label, verifier, key_id in self.entries:
            material = keys.by_id(key_id)
            candidate = keys.verifier(presented, material) if material else b"\x00" * len(verifier)
            if hmac.compare_digest(verifier, candidate) and material is not None:
                matched = matched or label
        return matched


@dataclass(frozen=True)
class Loaded:
    """One compiled adapter, ready to serve. Immutable: a swap replaces the object,
    so a request in progress finishes on the version it started with."""

    project_id: str
    slug: str
    version: int
    version_id: str
    generation: int
    document: AdapterDocument
    credential: ResolvedCredential | None
    loaded_at: datetime

    def __repr__(self) -> str:  # never print the credential
        return f"Loaded(slug={self.slug!r}, version={self.version})"


@dataclass
class RuntimeState:
    keys: MasterKeys | None = None
    loaded: dict[str, Loaded] = field(default_factory=dict)
    slugs: dict[str, str] = field(default_factory=dict)             # slug -> project id
    status: dict[str, tuple[str, str | None]] = field(default_factory=dict)
    inbound: dict[str, InboundKeys] = field(default_factory=dict)
    upstream: dict[str, dict[str, Any]] = field(default_factory=dict)
    # Replaced as a whole when registrations change; never mutated in place.
    hosts: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))
    host_conflicts: tuple[str, ...] = ()
    recovered: bool = False
    database_ok: bool = True

    def set_hosts(self, hosts: dict[str, str]) -> None:
        self.hosts = MappingProxyType(dict(hosts))

    def forget(self, project_id: str) -> None:
        for mapping in (self.loaded, self.status, self.inbound, self.upstream):
            mapping.pop(project_id, None)
        self.slugs = {slug: owner for slug, owner in self.slugs.items() if owner != project_id}
        self.set_hosts({host: owner for host, owner in self.hosts.items()
                        if owner != project_id})
