"""Credential storage (docs/architecture.md, section 4.7).

Two unrelated credential domains, stored differently and never mixed:

* **Legacy-client (inbound) keys** only ever need verifying, so they are stored
  as a keyed hash (HMAC-SHA-256 under a key derived from the master key). The
  plaintext is not stored and cannot be recovered.
* **Replacement-provider (outbound) credentials** must be sent upstream, so
  they are encrypted (Fernet) and versioned, with rollback.

Nothing in this module logs, returns in an error, or stores a plaintext value.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import datetime

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from sqlalchemy import select
from sqlalchemy.orm import Session

from revenant.config import Settings, is_fernet_key
from revenant.db.base import utcnow
from revenant.db.models import InboundKey, OutboundCredential
from revenant.errors import Conflict, NotConfigured, NotFound, ValidationFailed

_VERIFIER_INFO = b"revenant/inbound-key-verifier/v1"
MIN_INBOUND_KEY_LENGTH = 12
MAX_CREDENTIAL_LENGTH = 4096


class CredentialUnavailable(Exception):
    """A stored credential cannot be decrypted with the configured master keys."""


# --------------------------------------------------------------------------- master keys


@dataclass(frozen=True)
class _KeyMaterial:
    key_id: str
    fernet: Fernet
    verifier_key: bytes

    @classmethod
    def from_encoded(cls, encoded: str) -> _KeyMaterial:
        raw = base64.urlsafe_b64decode(encoded.encode("ascii"))
        verifier_key = HKDF(
            algorithm=hashes.SHA256(), length=32, salt=None, info=_VERIFIER_INFO
        ).derive(raw)
        return cls(
            key_id=hashlib.sha256(raw).hexdigest()[:12],
            fernet=Fernet(encoded.encode("ascii")),
            verifier_key=verifier_key,
        )

    def __repr__(self) -> str:  # never print key material
        return f"_KeyMaterial(key_id={self.key_id!r})"


class MasterKeys:
    """The current master key and, while rotating, the previous one."""

    def __init__(self, current: str, previous: str = "") -> None:
        if not current or not is_fernet_key(current):
            raise NotConfigured(
                "REVENANT_SECRET_KEY is not configured; credentials cannot be stored."
            )
        self.current = _KeyMaterial.from_encoded(current)
        self.previous = (
            _KeyMaterial.from_encoded(previous) if previous and is_fernet_key(previous) else None
        )

    @classmethod
    def from_settings(cls, settings: Settings) -> MasterKeys:
        return cls(
            settings.secret_key.get_secret_value(),
            settings.secret_key_previous.get_secret_value(),
        )

    def __repr__(self) -> str:
        return f"MasterKeys(current={self.current.key_id!r}, previous={self.previous is not None})"

    def _all(self) -> list[_KeyMaterial]:
        return [self.current] + ([self.previous] if self.previous else [])

    def by_id(self, key_id: str) -> _KeyMaterial | None:
        return next((key for key in self._all() if key.key_id == key_id), None)

    def encrypt(self, plaintext: str) -> tuple[bytes, str]:
        return self.current.fernet.encrypt(plaintext.encode("utf-8")), self.current.key_id

    def decrypt(self, ciphertext: bytes, key_id: str) -> str:
        key = self.by_id(key_id)
        if key is None:
            raise CredentialUnavailable("encrypted under a master key that is not configured")
        try:
            return key.fernet.decrypt(ciphertext).decode("utf-8")
        except InvalidToken as exc:
            raise CredentialUnavailable("stored credential cannot be decrypted") from exc

    def verifier(self, value: str, key: _KeyMaterial | None = None) -> bytes:
        material = key or self.current
        return hmac.new(material.verifier_key, value.encode("utf-8"), hashlib.sha256).digest()


def _check_value(value: str, *, minimum: int) -> None:
    if len(value) < minimum:
        raise ValidationFailed(
            "The credential value is too short.",
            details=[{"field": "value", "issue": f"must be at least {minimum} characters"}],
        )
    if len(value) > MAX_CREDENTIAL_LENGTH:
        raise ValidationFailed(
            "The credential value is too long.",
            details=[{"field": "value", "issue": f"must be at most {MAX_CREDENTIAL_LENGTH}"}],
        )
    if value != value.strip() or any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
        raise ValidationFailed(
            "The credential value contains whitespace at an end or control characters.",
            details=[{"field": "value", "issue": "must not have surrounding whitespace"}],
        )


# --------------------------------------------------------------------------- inbound keys


class InboundKeyStore:
    """Legacy-client keys for one database session."""

    def __init__(self, session: Session, keys: MasterKeys) -> None:
        self.session = session
        self.keys = keys

    def all(self, project_id: str) -> list[InboundKey]:
        return list(
            self.session.scalars(
                select(InboundKey)
                .where(InboundKey.project_id == project_id)
                .order_by(InboundKey.created_at, InboundKey.label)
            )
        )

    def add(
        self, project_id: str, label: str, value: str | None = None
    ) -> tuple[InboundKey, str | None]:
        """Register a key. Returns the row and, if the server generated it, the key itself.

        A generated key is returned exactly once, here; it is not stored.
        """
        generated = value is None
        plaintext = secrets.token_urlsafe(32) if value is None else value
        _check_value(plaintext, minimum=MIN_INBOUND_KEY_LENGTH)
        exists = self.session.scalar(
            select(InboundKey.id).where(
                InboundKey.project_id == project_id, InboundKey.label == label
            )
        )
        if exists is not None:
            raise Conflict(
                "An inbound key with this label already exists.",
                details=[{"field": "label", "issue": "already exists"}],
            )
        row = InboundKey(
            project_id=project_id,
            label=label,
            verifier=self.keys.verifier(plaintext),
            key_id=self.keys.current.key_id,
            status="active",
        )
        self.session.add(row)
        self.session.flush()
        return row, (plaintext if generated else None)

    def revoke(self, project_id: str, key_id: str) -> InboundKey:
        row = self.session.get(InboundKey, key_id)
        if row is None or row.project_id != project_id:
            raise NotFound("Inbound key not found.")
        if row.status != "revoked":
            row.status = "revoked"
            row.revoked_at = utcnow()
            self.session.flush()
        return row

    def active_count(self, project_id: str) -> int:
        return len([row for row in self.all(project_id) if row.status == "active"])

    def verify(self, project_id: str, presented: str) -> str | None:
        """Return the label of the active key that matches, or None.

        Every active key is compared, in constant time, whether or not an
        earlier one matched, so timing does not reveal which key or how many.
        """
        matched: InboundKey | None = None
        candidates = self.session.scalars(
            select(InboundKey).where(
                InboundKey.project_id == project_id, InboundKey.status == "active"
            )
        ).all()
        for row in candidates:
            material = self.keys.by_id(row.key_id)
            if material is None:
                # Hashed under a master key that is no longer configured.
                hmac.compare_digest(row.verifier, b"\x00" * len(row.verifier))
                continue
            if hmac.compare_digest(row.verifier, self.keys.verifier(presented, material)):
                matched = matched or row
        if matched is None:
            return None
        if matched.key_id != self.keys.current.key_id:
            # Master-key rotation: upgrade the stored hash now that we hold the plaintext.
            matched.verifier = self.keys.verifier(presented)
            matched.key_id = self.keys.current.key_id
            self.session.flush()
        return matched.label

    def needing_reregistration(self) -> list[InboundKey]:
        """Active keys still hashed under a master key other than the current one."""
        return list(
            self.session.scalars(
                select(InboundKey).where(
                    InboundKey.status == "active",
                    InboundKey.key_id != self.keys.current.key_id,
                )
            )
        )


# --------------------------------------------------------------------------- outbound credentials


@dataclass(frozen=True)
class ResolvedCredential:
    """A decrypted outbound credential, held in memory only."""

    name: str
    version: int
    value: str

    def __repr__(self) -> str:
        return f"ResolvedCredential(name={self.name!r}, version={self.version}, value=[REDACTED])"

    __str__ = __repr__


class OutboundCredentialStore:
    """Replacement-provider credentials for one database session."""

    def __init__(self, session: Session, keys: MasterKeys) -> None:
        self.session = session
        self.keys = keys

    def _versions(self, project_id: str, name: str) -> list[OutboundCredential]:
        return list(
            self.session.scalars(
                select(OutboundCredential)
                .where(
                    OutboundCredential.project_id == project_id,
                    OutboundCredential.name == name,
                )
                .order_by(OutboundCredential.version)
            )
        )

    def all(self, project_id: str) -> list[OutboundCredential]:
        return list(
            self.session.scalars(
                select(OutboundCredential)
                .where(OutboundCredential.project_id == project_id)
                .order_by(OutboundCredential.name, OutboundCredential.version)
            )
        )

    def rotate(self, project_id: str, name: str, value: str) -> OutboundCredential:
        """Store a new version and make it active. The old active becomes ``previous``."""
        _check_value(value, minimum=1)
        versions = self._versions(project_id, name)
        now = utcnow()
        for row in versions:
            if row.status == "previous":
                _retire(row, now)
        for row in versions:
            if row.status == "active":
                row.status = "previous"
        # The demotion must reach the database before the new active row does,
        # because at most one active version may exist.
        self.session.flush()
        ciphertext, key_id = self.keys.encrypt(value)
        row = OutboundCredential(
            project_id=project_id,
            name=name,
            version=(versions[-1].version + 1) if versions else 1,
            ciphertext=ciphertext,
            key_id=key_id,
            status="active",
        )
        self.session.add(row)
        self.session.flush()
        return row

    def rollback(self, project_id: str, name: str) -> OutboundCredential:
        """Re-activate the ``previous`` version; the current active becomes ``previous``."""
        versions = self._versions(project_id, name)
        if not versions:
            raise NotFound("Outbound credential not found.")
        previous = next((row for row in versions if row.status == "previous"), None)
        if previous is None:
            raise Conflict("There is no previous version to roll back to.")
        active = next((row for row in versions if row.status == "active"), None)
        if active is not None:
            active.status = "previous"
            previous.status = "retired"  # placeholder so two rows are never active
            self.session.flush()
        previous.status = "active"
        previous.retired_at = None
        self.session.flush()
        return previous

    def retire(self, project_id: str, name: str, version: int) -> OutboundCredential:
        row = next(
            (item for item in self._versions(project_id, name) if item.version == version), None
        )
        if row is None:
            raise NotFound("Outbound credential version not found.")
        if row.status == "active":
            raise Conflict("The active version cannot be retired; rotate or roll back first.")
        if row.status != "retired":
            _retire(row, utcnow())
            self.session.flush()
        return row

    def active(self, project_id: str, name: str) -> ResolvedCredential:
        """Decrypt the active version. Raises ``CredentialUnavailable`` if it cannot be used."""
        row = next(
            (item for item in self._versions(project_id, name) if item.status == "active"), None
        )
        if row is None:
            raise CredentialUnavailable(f"no active version of credential {name!r}")
        return ResolvedCredential(
            name=name, version=row.version, value=self.keys.decrypt(row.ciphertext, row.key_id)
        )

    def reencrypt_all(self) -> int:
        """Re-encrypt every stored version under the current master key. Returns the count."""
        changed = 0
        rows = self.session.scalars(
            select(OutboundCredential).where(
                OutboundCredential.key_id != self.keys.current.key_id
            )
        ).all()
        for row in rows:
            plaintext = self.keys.decrypt(row.ciphertext, row.key_id)
            row.ciphertext, row.key_id = self.keys.encrypt(plaintext)
            changed += 1
        self.session.flush()
        return changed


def _retire(row: OutboundCredential, when: datetime) -> None:
    row.status = "retired"
    row.retired_at = when
