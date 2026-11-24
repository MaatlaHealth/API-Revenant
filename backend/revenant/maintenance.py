"""Operator maintenance commands.

    python -m revenant.maintenance rotate-master-key

Run after setting REVENANT_SECRET_KEY to a new key and
REVENANT_SECRET_KEY_PREVIOUS to the old one. It re-encrypts every stored
replacement-provider credential under the new key and lists legacy-client keys
that are still hashed under the old key. Those cannot be converted without
their plaintext: each is upgraded automatically the next time it is presented,
and any that remain must be re-registered before the previous key is removed.
"""

from __future__ import annotations

import argparse
import sys

from revenant.config import Settings
from revenant.db.session import Database
from revenant.security.credentials import InboundKeyStore, MasterKeys, OutboundCredentialStore


def rotate_master_key(settings: Settings) -> tuple[int, list[str]]:
    """Returns (credentials re-encrypted, labels of inbound keys still on the old key)."""
    keys = MasterKeys.from_settings(settings)
    database = Database(settings.database_url)
    try:
        with database.session() as session:
            changed = OutboundCredentialStore(session, keys).reencrypt_all()
            pending = [
                f"{row.project_id}:{row.label}"
                for row in InboundKeyStore(session, keys).needing_reregistration()
            ]
    finally:
        database.dispose()
    return changed, pending


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m revenant.maintenance")
    parser.add_subparsers(dest="command", required=True).add_parser("rotate-master-key")
    parser.parse_args(argv)

    changed, pending = rotate_master_key(Settings())
    print(f"re-encrypted {changed} outbound credential version(s) under the current master key")
    if pending:
        print(f"{len(pending)} inbound key(s) are still hashed under the previous master key:")
        for item in pending:
            print(f"  {item}")
        print("They upgrade when next used; re-register any that remain before removing "
              "REVENANT_SECRET_KEY_PREVIOUS.")
    else:
        print("no inbound keys depend on the previous master key")
    return 0


if __name__ == "__main__":
    sys.exit(main())
