"""Credential storage (requirements 5.6, 6.10, 11.3, 11.7, 11.8).

Every "is absent" assertion searches for the credential value itself.
"""

from __future__ import annotations

import json

import pytest
import structlog
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from revenant.config import Settings
from revenant.db.models import AuditRecord, InboundKey, OutboundCredential
from revenant.db.session import Database
from revenant.errors import Conflict, NotConfigured, NotFound, ValidationFailed
from revenant.main import create_app
from revenant.maintenance import rotate_master_key
from revenant.security.credentials import (
    CredentialUnavailable,
    InboundKeyStore,
    MasterKeys,
    OutboundCredentialStore,
    ResolvedCredential,
)
from tests.conftest import MASTER_KEY, OTHER_MASTER_KEY, make_settings

LEGACY_KEY = "legacy-demo-key"
OTHER_LEGACY_KEY = "second-legacy-key-9876"
OUTBOUND_V1 = "replacement-demo-token"
OUTBOUND_V2 = "replacement-demo-token-rotated"


@pytest.fixture
def keys() -> MasterKeys:
    return MasterKeys(MASTER_KEY)


def base(project_id: str) -> str:
    return f"/api/v1/projects/{project_id}/credentials"


def database_dump(database: Database) -> str:
    """Every value of every column of every table, as text."""
    chunks: list[str] = []
    with database.engine.connect() as connection:
        for table in ("projects", "inbound_keys", "outbound_credentials", "audit_records"):
            for row in connection.execute(text(f"SELECT * FROM {table}")):  # noqa: S608
                for value in row:
                    if isinstance(value, (bytes, memoryview)):
                        raw = bytes(value)
                        chunks.append(raw.decode("latin-1"))
                        chunks.append(raw.hex())
                    else:
                        chunks.append(str(value))
    return "\n".join(chunks)


# ============================================================================= master keys


def test_master_keys_require_a_valid_key() -> None:
    for bad in ("", "CHANGE_ME_GENERATE_A_FERNET_KEY", "not-a-key"):
        with pytest.raises(NotConfigured):
            MasterKeys(bad)


def test_master_keys_never_print_key_material(keys: MasterKeys) -> None:
    assert MASTER_KEY not in repr(keys) and MASTER_KEY not in repr(keys.current)


def test_encrypt_round_trip_and_ciphertext_differs(keys: MasterKeys) -> None:
    ciphertext, key_id = keys.encrypt(OUTBOUND_V1)
    assert OUTBOUND_V1.encode() not in ciphertext
    assert keys.decrypt(ciphertext, key_id) == OUTBOUND_V1
    assert keys.encrypt(OUTBOUND_V1)[0] != ciphertext  # random IV: no equality leak


def test_decrypt_with_the_wrong_master_key_fails(keys: MasterKeys) -> None:
    ciphertext, key_id = keys.encrypt(OUTBOUND_V1)
    other = MasterKeys(OTHER_MASTER_KEY)
    with pytest.raises(CredentialUnavailable):
        other.decrypt(ciphertext, key_id)
    with pytest.raises(CredentialUnavailable):
        keys.decrypt(ciphertext[:-4] + b"AAAA", key_id)


def test_verifier_is_keyed_and_deterministic(keys: MasterKeys) -> None:
    assert keys.verifier(LEGACY_KEY) == keys.verifier(LEGACY_KEY)
    assert keys.verifier(LEGACY_KEY) != keys.verifier(LEGACY_KEY + "x")
    assert keys.verifier(LEGACY_KEY) != MasterKeys(OTHER_MASTER_KEY).verifier(LEGACY_KEY)
    assert len(keys.verifier(LEGACY_KEY)) == 32


# ============================================================================= inbound keys


def test_inbound_key_is_stored_only_as_a_hash(
    session: Session, keys: MasterKeys, project_id: str, database: Database
) -> None:
    row, generated = InboundKeyStore(session, keys).add(project_id, "client-a", LEGACY_KEY)
    session.commit()
    assert generated is None
    assert row.verifier == keys.verifier(LEGACY_KEY)
    dump = database_dump(database)
    assert LEGACY_KEY not in dump
    assert LEGACY_KEY.encode().hex() not in dump


def test_inbound_verify_accepts_only_the_registered_key(
    session: Session, keys: MasterKeys, project_id: str
) -> None:
    store = InboundKeyStore(session, keys)
    store.add(project_id, "client-a", LEGACY_KEY)
    assert store.verify(project_id, LEGACY_KEY) == "client-a"
    for wrong in ("", "legacy-demo-ke", LEGACY_KEY + "y", LEGACY_KEY.upper(), " " + LEGACY_KEY):
        assert store.verify(project_id, wrong) is None


def test_inbound_keys_are_scoped_to_their_project(
    session: Session, keys: MasterKeys, project_id: str
) -> None:
    store = InboundKeyStore(session, keys)
    store.add(project_id, "client-a", LEGACY_KEY)
    assert store.verify("another-project", LEGACY_KEY) is None


def test_several_inbound_keys_can_be_active_and_revocation_is_immediate(
    session: Session, keys: MasterKeys, project_id: str
) -> None:
    store = InboundKeyStore(session, keys)
    first, _ = store.add(project_id, "client-a", LEGACY_KEY)
    store.add(project_id, "client-b", OTHER_LEGACY_KEY)
    assert store.verify(project_id, LEGACY_KEY) == "client-a"
    assert store.verify(project_id, OTHER_LEGACY_KEY) == "client-b"
    assert store.active_count(project_id) == 2

    revoked = store.revoke(project_id, first.id)
    assert revoked.status == "revoked" and revoked.revoked_at is not None
    assert store.verify(project_id, LEGACY_KEY) is None           # takes effect at once
    assert store.verify(project_id, OTHER_LEGACY_KEY) == "client-b"  # the other keeps working
    assert store.active_count(project_id) == 1
    assert store.revoke(project_id, first.id).status == "revoked"   # idempotent


def test_generated_inbound_key_is_returned_once_and_works(
    session: Session, keys: MasterKeys, project_id: str, database: Database
) -> None:
    store = InboundKeyStore(session, keys)
    _row, generated = store.add(project_id, "generated")
    session.commit()
    assert generated is not None and len(generated) >= 40
    assert store.verify(project_id, generated) == "generated"
    assert generated not in database_dump(database)


def test_inbound_label_must_be_unique_per_project(
    session: Session, keys: MasterKeys, project_id: str
) -> None:
    store = InboundKeyStore(session, keys)
    store.add(project_id, "client-a", LEGACY_KEY)
    with pytest.raises(Conflict):
        store.add(project_id, "client-a", OTHER_LEGACY_KEY)


@pytest.mark.parametrize("value", ["tiny-val", " padded-value-123456", "trailing-123456789\n",
                                   "x" * 5000, "has\x00null-byte-1234"])
def test_unsuitable_inbound_values_are_rejected_without_echoing_them(
    session: Session, keys: MasterKeys, project_id: str, value: str
) -> None:
    with pytest.raises(ValidationFailed) as excinfo:
        InboundKeyStore(session, keys).add(project_id, "client-a", value)
    assert value not in excinfo.value.message
    assert value not in json.dumps(excinfo.value.details)


def test_revoking_another_projects_key_is_not_found(
    session: Session, keys: MasterKeys, project_id: str
) -> None:
    row, _ = InboundKeyStore(session, keys).add(project_id, "client-a", LEGACY_KEY)
    with pytest.raises(NotFound):
        InboundKeyStore(session, keys).revoke("another-project", row.id)


# ============================================================================= outbound credentials


def test_outbound_credential_is_encrypted_at_rest(
    session: Session, keys: MasterKeys, project_id: str, database: Database
) -> None:
    store = OutboundCredentialStore(session, keys)
    row = store.rotate(project_id, "replacement_token", OUTBOUND_V1)
    session.commit()
    assert (row.version, row.status) == (1, "active")
    dump = database_dump(database)
    assert OUTBOUND_V1 not in dump and OUTBOUND_V1.encode().hex() not in dump
    assert store.active(project_id, "replacement_token") == ResolvedCredential(
        "replacement_token", 1, OUTBOUND_V1
    )


def test_rotation_activates_the_new_version_and_keeps_the_previous(
    session: Session, keys: MasterKeys, project_id: str
) -> None:
    store = OutboundCredentialStore(session, keys)
    store.rotate(project_id, "replacement_token", OUTBOUND_V1)
    second = store.rotate(project_id, "replacement_token", OUTBOUND_V2)
    assert (second.version, second.status) == (2, "active")
    assert [(r.version, r.status) for r in store.all(project_id)] == [
        (1, "previous"), (2, "active")
    ]
    assert store.active(project_id, "replacement_token").value == OUTBOUND_V2


def test_third_rotation_retires_the_oldest_version(
    session: Session, keys: MasterKeys, project_id: str
) -> None:
    store = OutboundCredentialStore(session, keys)
    for value in (OUTBOUND_V1, OUTBOUND_V2, "third-version-value"):
        store.rotate(project_id, "replacement_token", value)
    rows = store.all(project_id)
    assert [(r.version, r.status) for r in rows] == [
        (1, "retired"), (2, "previous"), (3, "active")
    ]
    assert rows[0].retired_at is not None


def test_rollback_reactivates_the_previous_version(
    session: Session, keys: MasterKeys, project_id: str
) -> None:
    store = OutboundCredentialStore(session, keys)
    store.rotate(project_id, "replacement_token", OUTBOUND_V1)
    store.rotate(project_id, "replacement_token", OUTBOUND_V2)
    restored = store.rollback(project_id, "replacement_token")
    assert (restored.version, restored.status) == (1, "active")
    assert store.active(project_id, "replacement_token").value == OUTBOUND_V1
    assert [(r.version, r.status) for r in store.all(project_id)] == [
        (1, "active"), (2, "previous")
    ]
    # Rolling back again swaps them back: the pair stays reversible.
    assert store.rollback(project_id, "replacement_token").version == 2


def test_rollback_without_a_previous_version_is_refused(
    session: Session, keys: MasterKeys, project_id: str
) -> None:
    store = OutboundCredentialStore(session, keys)
    with pytest.raises(NotFound):
        store.rollback(project_id, "replacement_token")
    store.rotate(project_id, "replacement_token", OUTBOUND_V1)
    with pytest.raises(Conflict):
        store.rollback(project_id, "replacement_token")


def test_retire_refuses_the_active_version_and_disables_rollback(
    session: Session, keys: MasterKeys, project_id: str
) -> None:
    store = OutboundCredentialStore(session, keys)
    store.rotate(project_id, "replacement_token", OUTBOUND_V1)
    store.rotate(project_id, "replacement_token", OUTBOUND_V2)
    with pytest.raises(Conflict):
        store.retire(project_id, "replacement_token", 2)
    assert store.retire(project_id, "replacement_token", 1).status == "retired"
    with pytest.raises(Conflict):
        store.rollback(project_id, "replacement_token")
    with pytest.raises(NotFound):
        store.retire(project_id, "replacement_token", 99)


def test_there_is_never_more_than_one_active_version(
    session: Session, keys: MasterKeys, project_id: str
) -> None:
    store = OutboundCredentialStore(session, keys)
    for step in range(6):
        store.rotate(project_id, "replacement_token", f"value-{step}")
        if step % 2:
            store.rollback(project_id, "replacement_token")
        active = [r for r in store.all(project_id) if r.status == "active"]
        assert len(active) == 1


def test_credentials_with_different_names_are_independent(
    session: Session, keys: MasterKeys, project_id: str
) -> None:
    store = OutboundCredentialStore(session, keys)
    store.rotate(project_id, "replacement_token", OUTBOUND_V1)
    store.rotate(project_id, "sandbox_token", OUTBOUND_V2)
    assert store.active(project_id, "replacement_token").value == OUTBOUND_V1
    assert store.active(project_id, "sandbox_token").value == OUTBOUND_V2


def test_missing_or_undecryptable_credential_is_unavailable(
    session: Session, keys: MasterKeys, project_id: str
) -> None:
    store = OutboundCredentialStore(session, keys)
    with pytest.raises(CredentialUnavailable):
        store.active(project_id, "replacement_token")
    store.rotate(project_id, "replacement_token", OUTBOUND_V1)
    session.commit()
    with pytest.raises(CredentialUnavailable):
        OutboundCredentialStore(session, MasterKeys(OTHER_MASTER_KEY)).active(
            project_id, "replacement_token"
        )


def test_resolved_credential_never_prints_its_value() -> None:
    resolved = ResolvedCredential("replacement_token", 3, OUTBOUND_V1)
    assert OUTBOUND_V1 not in repr(resolved) and OUTBOUND_V1 not in str(resolved)
    assert OUTBOUND_V1 not in f"{resolved}"


# ============================================================================= master-key rotation


def test_master_key_rotation(database: Database, project_id: str, settings: Settings) -> None:
    old = MasterKeys(MASTER_KEY)
    with database.session() as session:
        OutboundCredentialStore(session, old).rotate(project_id, "replacement_token", OUTBOUND_V1)
        OutboundCredentialStore(session, old).rotate(project_id, "replacement_token", OUTBOUND_V2)
        InboundKeyStore(session, old).add(project_id, "used", LEGACY_KEY)
        InboundKeyStore(session, old).add(project_id, "unused", OTHER_LEGACY_KEY)

    rotating = make_settings(settings.database_url, secret_key=OTHER_MASTER_KEY,
                             secret_key_previous=MASTER_KEY)
    both = MasterKeys.from_settings(rotating)

    # While both keys are configured everything still works.
    with database.session() as session:
        assert OutboundCredentialStore(session, both).active(
            project_id, "replacement_token").value == OUTBOUND_V2
        # Presenting an inbound key upgrades its stored hash to the new master key.
        assert InboundKeyStore(session, both).verify(project_id, LEGACY_KEY) == "used"

    changed, pending = rotate_master_key(rotating)
    assert changed == 2                       # both stored versions re-encrypted
    assert pending == [f"{project_id}:unused"]  # only the key that was never presented

    # With the old key removed: outbound and the upgraded inbound key still work.
    new_only = MasterKeys(OTHER_MASTER_KEY)
    with database.session() as session:
        store = OutboundCredentialStore(session, new_only)
        assert store.active(project_id, "replacement_token").value == OUTBOUND_V2
        assert store.rollback(project_id, "replacement_token").version == 1
        assert store.active(project_id, "replacement_token").value == OUTBOUND_V1
        inbound = InboundKeyStore(session, new_only)
        assert inbound.verify(project_id, LEGACY_KEY) == "used"
        assert inbound.verify(project_id, OTHER_LEGACY_KEY) is None  # must be re-registered
    assert rotate_master_key(make_settings(settings.database_url,
                                           secret_key=OTHER_MASTER_KEY))[0] == 0


# ============================================================================= API


def test_inbound_api_never_returns_a_supplied_key(
    client: TestClient, admin: dict[str, str], project_id: str
) -> None:
    created = client.post(f"{base(project_id)}/inbound", headers=admin,
                          json={"label": "client-a", "value": LEGACY_KEY})
    assert created.status_code == 201
    body = created.json()
    assert body["label"] == "client-a" and body["status"] == "active"
    assert body["generated_value"] is None
    assert set(body) == {"id", "label", "status", "created_at", "revoked_at", "generated_value"}
    listing = client.get(f"{base(project_id)}/inbound", headers=admin)
    assert listing.json()["total"] == 1
    assert LEGACY_KEY not in created.text and LEGACY_KEY not in listing.text
    assert "verifier" not in listing.text and "key_id" not in listing.text


def test_inbound_api_returns_a_generated_key_exactly_once(
    client: TestClient, admin: dict[str, str], project_id: str
) -> None:
    created = client.post(f"{base(project_id)}/inbound", headers=admin, json={"label": "auto"})
    generated = created.json()["generated_value"]
    assert created.status_code == 201 and len(generated) >= 40
    listing = client.get(f"{base(project_id)}/inbound", headers=admin)
    assert generated not in listing.text
    assert "generated_value" not in listing.text


def test_inbound_api_revoke_and_duplicate_label(
    client: TestClient, admin: dict[str, str], project_id: str
) -> None:
    key_id = client.post(f"{base(project_id)}/inbound", headers=admin,
                         json={"label": "client-a", "value": LEGACY_KEY}).json()["id"]
    duplicate = client.post(f"{base(project_id)}/inbound", headers=admin,
                            json={"label": "client-a", "value": OTHER_LEGACY_KEY})
    assert duplicate.status_code == 409 and duplicate.json()["error"]["code"] == "conflict"
    revoked = client.post(f"{base(project_id)}/inbound/{key_id}/revoke", headers=admin)
    assert revoked.status_code == 200 and revoked.json()["status"] == "revoked"
    assert client.post(f"{base(project_id)}/inbound/nope/revoke",
                       headers=admin).status_code == 404


def test_outbound_api_is_write_only(
    client: TestClient, admin: dict[str, str], project_id: str
) -> None:
    url = f"{base(project_id)}/outbound/replacement_token"
    first = client.put(url, headers=admin, json={"value": OUTBOUND_V1})
    second = client.put(url, headers=admin, json={"value": OUTBOUND_V2})
    assert first.status_code == 200 and second.json()["version"] == 2
    listing = client.get(f"{base(project_id)}/outbound", headers=admin)
    assert [(i["version"], i["status"]) for i in listing.json()["items"]] == [
        (1, "previous"), (2, "active")
    ]
    rolled = client.post(f"{url}/rollback", headers=admin)
    retired = client.post(f"{url}/versions/2/retire", headers=admin)
    assert rolled.json()["version"] == 1 and retired.json()["status"] == "retired"
    for response in (first, second, listing, rolled, retired):
        assert OUTBOUND_V1 not in response.text and OUTBOUND_V2 not in response.text
        assert "ciphertext" not in response.text
    assert set(listing.json()["items"][0]) == {
        "name", "version", "status", "created_at", "retired_at"
    }


@pytest.mark.parametrize("name", ["Has-Caps", "1starts_with_digit", "has space", "a" * 65, "x.y"])
def test_outbound_credential_names_are_restricted(
    client: TestClient, admin: dict[str, str], project_id: str, name: str
) -> None:
    response = client.put(f"{base(project_id)}/outbound/{name}", headers=admin,
                          json={"value": OUTBOUND_V1})
    assert response.status_code == 400
    assert OUTBOUND_V1 not in response.text


def test_credential_api_without_a_master_key_is_refused(
    migrated_url: str, project_id: str, admin: dict[str, str]
) -> None:
    app = create_app(make_settings(migrated_url, secret_key=""), supervised=False)
    with TestClient(app) as keyless:
        response = keyless.put(f"{base(project_id)}/outbound/replacement_token", headers=admin,
                               json={"value": OUTBOUND_V1})
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "not_configured"
    assert OUTBOUND_V1 not in response.text


def test_credential_operations_are_audited_without_values(
    client: TestClient, admin: dict[str, str], project_id: str, database: Database
) -> None:
    key_id = client.post(f"{base(project_id)}/inbound", headers=admin,
                         json={"label": "client-a", "value": LEGACY_KEY}).json()["id"]
    client.post(f"{base(project_id)}/inbound/{key_id}/revoke", headers=admin)
    url = f"{base(project_id)}/outbound/replacement_token"
    client.put(url, headers=admin, json={"value": OUTBOUND_V1})
    client.put(url, headers=admin, json={"value": OUTBOUND_V2})
    client.post(f"{url}/rollback", headers=admin)
    client.post(f"{url}/versions/2/retire", headers=admin)

    with database.session() as session:
        records = session.query(AuditRecord).order_by(AuditRecord.created_at).all()
        actions = [record.action for record in records]
        assert sorted(actions) == sorted([
            "credential.inbound.add", "credential.inbound.revoke",
            "credential.outbound.rotate", "credential.outbound.rotate",
            "credential.outbound.rollback", "credential.outbound.retire",
        ])
        assert all(record.actor == "admin" and record.project_id == project_id
                   for record in records)
        rotate = next(r for r in records if r.action == "credential.outbound.rotate")
        assert rotate.meta["name"] == "replacement_token"
    dump = database_dump(database)
    for secret in (LEGACY_KEY, OUTBOUND_V1, OUTBOUND_V2):
        assert secret not in dump


def test_failed_request_writes_no_audit_record_and_no_credential(
    client: TestClient, admin: dict[str, str], project_id: str, database: Database
) -> None:
    response = client.post(f"{base(project_id)}/inbound", headers=admin,
                           json={"label": "client-a", "value": "short"})
    assert response.status_code == 400
    with database.session() as session:
        assert session.query(AuditRecord).count() == 0
        assert session.query(InboundKey).count() == 0
        assert session.query(OutboundCredential).count() == 0


def test_no_credential_value_reaches_the_logs(
    client: TestClient, admin: dict[str, str], project_id: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Captured twice: as structured events, and as the rendered JSON lines."""
    capsys.readouterr()
    with structlog.testing.capture_logs() as events:
        client.post(f"{base(project_id)}/inbound", headers=admin,
                    json={"label": "client-a", "value": LEGACY_KEY})
        client.post(f"{base(project_id)}/inbound", headers=admin,
                    json={"label": "bad", "value": "short"})
        client.put(f"{base(project_id)}/outbound/replacement_token", headers=admin,
                   json={"value": OUTBOUND_V1})
        client.put(f"{base(project_id)}/outbound/Bad-Name", headers=admin,
                   json={"value": OUTBOUND_V2})
    captured = json.dumps(events, default=str)
    client.put(f"{base(project_id)}/outbound/replacement_token", headers=admin,
               json={"value": OUTBOUND_V2})
    rendered = capsys.readouterr()
    for secret in (LEGACY_KEY, OUTBOUND_V1, OUTBOUND_V2):
        assert secret not in captured
        assert secret not in rendered.out and secret not in rendered.err
    assert len(events) >= 4
