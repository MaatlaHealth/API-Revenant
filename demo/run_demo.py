"""Run the whole demonstration on this machine, without Docker.

GravePay and NovaPay are FICTIONAL services created for this demonstration.

What it does, using only the public management API:

  1. starts the NovaPay simulator and the API Revenant backend on local ports,
     with a temporary SQLite database;
  2. BEFORE: runs the unchanged GravePay client against NovaPay directly. It fails;
  3. seeds the demonstration project, reconstructs, approves mappings, compiles
     an adapter, stores the two credentials and starts the runtime;
  4. AFTER: runs the same client, byte for byte, against the runtime. It succeeds.

Run from the repository root, in an environment where the backend is installed:

    python demo/run_demo.py
"""

from __future__ import annotations

import base64
import hashlib
import os
import secrets
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx

from revenant.demo_setup import set_up

ROOT = Path(__file__).resolve().parent.parent
CLIENT = ROOT / "demo" / "legacy-client" / "gravepay_client.py"
LEGACY_KEY = "legacy-demo-key"            # fictional
NOVAPAY_TOKEN = "replacement-demo-token"  # fictional
def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def wait_for(url: str, process: subprocess.Popen[bytes], seconds: float = 60) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise SystemExit(f"a service stopped while starting (exit {process.returncode})")
        try:
            if httpx.get(url, timeout=2).status_code < 500:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.2)
    raise SystemExit(f"timed out waiting for {url}")


def run_client(base_url: str) -> int:
    env = {**os.environ, "GRAVEPAY_BASE_URL": base_url, "GRAVEPAY_API_KEY": LEGACY_KEY,
           "NO_PROXY": "127.0.0.1,localhost"}
    result = subprocess.run([sys.executable, str(CLIENT)], env=env, text=True,  # noqa: S603
                            capture_output=True, timeout=120)
    for line in result.stdout.splitlines():
        print(f"      {line}")
    return result.returncode


def main() -> int:
    checksum = hashlib.sha256(CLIENT.read_bytes().replace(b"\r\n", b"\n")).hexdigest()
    recorded = (CLIENT.parent / "SHA256").read_text("ascii").split()[0]
    print("API Revenant demonstration (GravePay and NovaPay are fictional services)")
    print(f"Legacy client checksum  {checksum}")
    print(f"Recorded checksum       {recorded}  "
          f"{'(match)' if checksum == recorded else '(DIFFERENT)'}")
    if checksum != recorded:
        return 2

    work = Path(tempfile.mkdtemp(prefix="revenant-demo-"))
    simulator_port, backend_port = free_port(), free_port()
    upstream = f"http://127.0.0.1:{simulator_port}"
    backend = f"http://127.0.0.1:{backend_port}"
    admin_token = secrets.token_urlsafe(32)
    env = {
        **os.environ,
        "REVENANT_ENV": "demo",
        "DATABASE_URL": f"sqlite:///{(work / 'demo.db').as_posix()}",
        "REVENANT_SECRET_KEY": base64.urlsafe_b64encode(secrets.token_bytes(32)).decode(),
        "REVENANT_ADMIN_TOKEN": admin_token,
        "REVENANT_ALLOW_PRIVATE_UPSTREAMS": "true",
        "REVENANT_UPSTREAM_ALLOWED_PORTS": str(simulator_port),
        "REVENANT_PUBLIC_HOSTS": "127.0.0.1,localhost",
        "REVENANT_LOG_LEVEL": "WARNING",
        "NOVAPAY_TOKEN": NOVAPAY_TOKEN,
        "NO_PROXY": "127.0.0.1,localhost",
    }
    subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"],  # noqa: S603
                   cwd=ROOT / "backend", env=env, check=True, capture_output=True)
    services = [
        subprocess.Popen([sys.executable, "-m", "uvicorn", "app:app", "--host", "127.0.0.1",  # noqa: S603
                          "--port", str(simulator_port), "--log-level", "warning"],
                         cwd=ROOT / "demo" / "novapay" / "simulator", env=env),
        subprocess.Popen([sys.executable, "-m", "uvicorn", "revenant.main:app", "--host",  # noqa: S603
                          "127.0.0.1", "--port", str(backend_port), "--workers", "1",
                          "--no-access-log", "--log-level", "warning"],
                         cwd=ROOT / "backend", env=env),
    ]
    try:
        wait_for(f"{upstream}/__sandbox/default/state", services[0])
        wait_for(f"{backend}/readyz", services[1])

        print("\nBEFORE  the unchanged client against NovaPay directly")
        before = run_client(upstream)
        print(f"      exit status {before}  ->  {'failed, as expected' if before else 'UNEXPECTED'}")

        print("\nSET-UP  through the management API")
        with httpx.Client(base_url=backend, timeout=60,
                          headers={"Authorization": f"Bearer {admin_token}"}) as api:
            runtime = set_up(api, upstream=upstream, legacy_key=LEGACY_KEY,
                             replacement_token=NOVAPAY_TOKEN, legacy_hosts=[],
                             say=lambda line: print(f"      {line}"))
            root = f"/api/v1/projects/{runtime['project_id']}"

            print("\nAFTER   the same client through the API Revenant runtime")
            after = run_client(f"{backend}{runtime['path_entry'].rstrip('/')}")
            print(f"      exit status {after}  ->  {'succeeded' if after == 0 else 'FAILED'}")
            time.sleep(1.5)  # let the trace writer store its batch
            events = api.get(f"{root}/runtime/events", params={"kind": "request"}).json()
            print(f"\n{events['total']} requests recorded by the runtime:")
            for event in reversed(events["items"]):
                print(f"      {event['method']:5} {event['legacy_path']:28} -> "
                      f"{event['upstream_path'] or '-':24} {event['legacy_status']}")
        ok = before != 0 and after == 0
        print("\nRESULT  " + ("the unchanged client failed before and succeeded after."
                              if ok else "the demonstration did not behave as intended."))
        return 0 if ok else 1
    finally:
        for process in services:
            process.terminate()
        for process in services:
            try:
                process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                process.kill()


if __name__ == "__main__":
    sys.exit(main())
