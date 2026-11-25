"""Mutation check for the runtime: host dispatch, the lease, the proxy request
path, credential separation and the trace pipeline.

Run from the ``backend`` directory:

    python tests/mutation/runtime_mutation_check.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _runner import Mutations, run  # noqa: E402

# Fast tests first: most mutations are caught before a server has to be started.
TESTS = ["tests/unit/test_runtime_units.py", "tests/resilience/test_lease.py",
         "tests/resilience/test_traces.py", "tests/integration/test_runtime_e2e.py",
         "tests/resilience/test_runtime_resilience.py"]

HOSTS = "revenant/runtime/hosts.py"
DISPATCHER = "revenant/runtime/dispatcher.py"
LEASE = "revenant/runtime/lease.py"
PROXY = "revenant/runtime/proxy.py"
TRACES = "revenant/runtime/traces.py"
SERVICE = "revenant/runtime/service.py"

MUTATIONS: Mutations = {
    # ---- host handling ------------------------------------------------------------------------
    "accept underscores and other forbidden characters in hosts": (
        HOSTS, "    if any(ch in _FORBIDDEN or ord(ch) < 33 or ord(ch) == 127 for ch in raw):",
        "    if False:",
    ),
    "accept any port": (
        HOSTS, "    if port is not None and not (port.isdigit() and len(port) <= 5 and 1 <= "
               "int(port) <= 65535):", "    if False:",
    ),
    "accept invalid labels": (
        HOSTS, '    if not all(_LABEL.match(label) for label in host.split(".")):',
        "    if False:",
    ),
    "do not lowercase hosts": (
        HOSTS, "    host = host.lower()\n", "",
    ),
    # Not listed: removing the explicit wildcard check in ``registrable_host``.
    # The label grammar already rejects "*", so that change alters no behaviour
    # (an equivalent mutant); the check is kept for its clearer error message.
    "allow an IP address as a legacy host": (
        HOSTS, "    if is_ip_literal(host):", "    if False:",
    ),
    "allow a management host as a legacy host": (
        HOSTS, "    if host in management_hosts:", "    if False:",
    ),
    "match legacy hosts by suffix": (
        DISPATCHER, "        project_id = self.state.hosts.get(host)  # exact equality; no other "
                    "form of match",
        "        project_id = next((owner for name, owner in self.state.hosts.items() "
        "if host.endswith(name)), None)",
    ),
    "serve the management API on unknown hosts": (
        DISPATCHER, "        if not self.public_hosts or host in self.public_hosts:",
        "        if True:",
    ),
    "accept several Host headers": (
        DISPATCHER, "        if len(hosts) != 1:", "        if not hosts:",
    ),
    "accept absolute-form request targets": (
        DISPATCHER, '        if not raw_path.startswith(b"/"):', "        if False:",
    ),
    "trust a forwarded host from any peer": (
        DISPATCHER, "        if self.trust_forwarded and self._from_trusted_proxy(scope):",
        "        if True:",
    ),
    "trust a forwarded host whenever the flag is set": (
        DISPATCHER, "        if self.trust_forwarded and self._from_trusted_proxy(scope):",
        "        if self.trust_forwarded:",
    ),
    "answer probes on any path for unknown hosts": (
        DISPATCHER, 'scope["method"] == "GET" and scope["path"] in PROBES:',
        'scope["method"] == "GET":',
    ),
    # ---- the lease ------------------------------------------------------------------------------
    "take a live lease": (
        LEASE, "        return row.released or heartbeat is None or (\n"
               "            now - heartbeat).total_seconds() > self.timing.ttl",
        "        return True",
    ),
    "acquire without comparing what was read": (
        LEASE, "                .where(InstanceLease.id == 1, InstanceLease.epoch == seen.epoch, "
               "holder,\n                       InstanceLease.beat == seen.beat, "
               "InstanceLease.released == seen.released)",
        "                .where(InstanceLease.id == 1)",
    ),
    "extend the deadlines when a renewal fails": (
        LEASE, "            self.database_ok = False\n            return True",
        "            self.database_ok = False\n            self._extend(sent)\n"
        "            return True",
    ),
    "keep serving after ownership is lost": (
        LEASE, '                self.state, self.accepting = "superseded", False\n',
        "                pass\n",
    ),
    "admit after the grace period": (
        LEASE, "        if self.suspect or self.clock() >= self.admit_until:",
        "        if self.suspect:",
    ),
    "admit while a pause is suspected": (
        LEASE, "        if self.suspect or self.clock() >= self.admit_until:",
        "        if self.clock() >= self.admit_until:",
    ),
    "send upstream after stop_by": (
        LEASE, "                and self.clock() < self.stop_by)", "                )",
    ),
    "keep accepting during a handover": (
        LEASE, '        if self.state == "handover":\n            return "handover"\n', "",
    ),
    "request a handover from a sibling worker": (
        LEASE, "                if (row.host, row.parent_pid) == (self.host, self.parent_pid):",
        "                if False:",
    ),
    "ask a newer holder for another handover": (
        LEASE, "            elif (row.holder_id, row.epoch) != asked:", "            elif False:",
    ),
    "never enforce the handover deadline": (
        LEASE, "                and now - self._handover_seen > self.timing.handover_deadline):",
        "                and False):",
    ),
    "wait for a lease forever": (
        LEASE, "            if self.clock() >= deadline:", "            if False:",
    ),
    "accept a TTL shorter than grace plus lifetime plus margin": (
        LEASE, "        if self.ttl < self.grace + self.lifetime + self.margin:",
        "        if self.ttl < self.grace:",
    ),
    # ---- the request path ---------------------------------------------------------------------
    "skip admission": (
        PROXY, "        refusal = self.lease.admit()\n        if refusal is not None:\n"
               '            headers = {"X-Revenant-Runtime": refusal}',
        "        refusal = None\n        if refusal is not None:\n"
        '            headers = {"X-Revenant-Runtime": refusal}',
    ),
    "skip the second lease check": (
        PROXY, "        if not self.lease.may_send():", "        if False:",
    ),
    "serve a stopped runtime": (
        PROXY, "        if loaded is None:\n            reported = self.state.status.get("
               "project_id, (\"stopped\", None))[0]",
        "        if loaded is None and False:\n            reported = self.state.status.get("
        "project_id, (\"stopped\", None))[0]",
    ),
    "do not authenticate legacy clients": (
        PROXY, "            if label is None:\n                raise Refused(401, "
               '"auth_rejected")', "            if False:\n                raise Refused(401, "
                                   '"auth_rejected")',
    ),
    "accept a duplicated credential parameter": (
        PROXY, "            if not duplicated and context.presented and keys and "
               "self.state.keys:", "            if context.presented and keys and "
                                   "self.state.keys:",
    ),
    "leave the credential parameter in the query": (
        PROXY, "        if unquote_plus(key) == param:\n            count += 1\n"
               "            value = unquote_plus(rest)",
        "        if unquote_plus(key) == param:\n            count += 1\n"
        "            value = unquote_plus(rest)\n            kept.append(segment)",
    ),
    "match the credential parameter without decoding its name": (
        PROXY, "        if unquote_plus(key) == param:", "        if key == param:",
    ),
    "skip the pre-send leak check": (
        PROXY, "        if leaks(context.presented, url, json.dumps(upstream.query),",
        "        if False and leaks(context.presented, url, json.dumps(upstream.query),",
    ),
    "check leaks in raw form only": (
        PROXY, "    return any(form in part or form in unquote(part) for form in forms for part "
               "in parts)", "    return any(presented in part for part in parts)",
    ),
    "tell the legacy client that the replacement credential was rejected": (
        PROXY, '            raise Refused(502, "upstream_auth_failed")',
        '            raise Refused(401, "upstream_auth_failed")',
    ),
    "forward credential headers to the rules": (
        PROXY, "                 if name not in CREDENTIAL_HEADERS}", "                 }",
    ),
    "snapshot the upstream request after attaching the credential": (
        PROXY, '            "headers": dict(upstream.headers), "body": upstream.body}',
        '            "headers": {**upstream.headers, **self._attach(document, loaded, {}, {})[0]},'
        ' "body": upstream.body}',
    ),
    "do not scrub credentials from traces": (
        PROXY, '            "trace": scrub(trace, secrets), "adapter_version_id": '
               "loaded.version_id,", '            "trace": trace, "adapter_version_id": '
                                     "loaded.version_id,",
    ),
    "record the exception text of upstream failures": (
        PROXY, '            context.trace["error"] = {"type": type(exc).__name__}\n'
               '            self._upstream(loaded, "connect_error")',
        '            context.trace["error"] = {"type": str(exc)}\n'
        '            self._upstream(loaded, "connect_error")',
    ),
    "no request lifetime": (
        PROXY, "                async with asyncio.timeout(self.lifetime):",
        "                async with asyncio.timeout(None):",
    ),
    "report a body timeout as an upstream timeout": (
        PROXY, '                if context.phase == "body":', "                if False:",
    ),
    "do not enforce the body size limit": (
        PROXY, "            if size > limit:", "            if False:",
    ),
    "do not enforce the concurrency cap": (
        PROXY, "                self.settings.runtime_max_concurrency):\n"
               '            raise Refused(503, "concurrency_limit", {"Retry-After": "1"})',
        "                self.settings.runtime_max_concurrency) and False:\n"
        '            raise Refused(503, "concurrency_limit", {"Retry-After": "1"})',
    ),
    "do not cancel requests when ownership is lost": (
        PROXY, "                self._cancel_reason[task] = reason\n                task.cancel()",
        "                self._cancel_reason[task] = reason",
    ),
    # ---- the trace pipeline -------------------------------------------------------------------
    "wait for room in the trace queue": (
        TRACES, "        except asyncio.QueueFull:\n            self._lose(event, "
                '"queue_full")\n            return "dropped"',
        "        except asyncio.QueueFull:\n            self._lose(event, \"queue_full\")\n"
        '            return "queued"',
    ),
    "do not count events refused at the queue": (
        TRACES, "        self.dropped[reason] += count\n", "",
    ),
    "retry persistence forever": (
        TRACES, "                    if attempt == len(delays):", "                    if False:",
    ),
    "count a failed batch as persisted": (
        TRACES, "                        for event in batch:\n"
                '                            self._lose(event, "persist_failed")',
        "                        self.persisted += len(batch)",
    ),
    "do not record trace losses": (
        TRACES, "                    losses = self._pending_losses()",
        "                    losses: list[dict[str, Any]] = []",
    ),
    "do not cap trace size": (
        TRACES, "    if len(json.dumps(trace, default=str)) <= MAX_TRACE_BYTES:\n"
                "        return event", "    if True:\n        return event",
    ),
    "do not count what is left at shutdown": (
        TRACES, '            self._lose(self.queue.get_nowait(), "shutdown")',
        "            self.queue.get_nowait()",
    ),
    "do not restart a stopped writer": (
        TRACES, "        while not self._closing:\n            self.writer = \"running\"",
        "        for _once in (1,):\n            self.writer = \"running\"",
    ),
    # ---- control ------------------------------------------------------------------------------
    "start a runtime without its credential": (
        SERVICE, "        except CredentialUnavailable as exc:\n"
                 '            raise LoadError(f"credential_unavailable: {exc}") from exc',
        "        except CredentialUnavailable:\n            credential = None",
    ),
    "start an authenticated runtime with no active key": (
        SERVICE, '    if document.auth.inbound.type != "none" and not verifiers.entries:',
        "    if False:",
    ),
    "let one failed recovery stop the others": (
        SERVICE, "            except Exception as exc:  # noqa: BLE001 - one runtime must not "
                 "stop the others", "            except LookupError as exc:",
    ),
    "recover on the latest adapter instead of the pinned one": (
        SERVICE, "                adapter = session.get(AdapterVersion, "
                 'instance.adapter_version_id or "")',
        "                adapter = session.scalar(select(AdapterVersion).where("
        "AdapterVersion.project_id == project.id).order_by("
        "AdapterVersion.version.desc()).limit(1))",
    ),
    "register a host that belongs to another project": (
        SERVICE, "            if owner is not None and owner.project_id != project.id:",
        "            if False:",
    ),
    "revoke the last key of a running runtime": (
        SERVICE, "        if others == 0:", "        if False:",
    ),
}

if __name__ == "__main__":
    sys.exit(run(MUTATIONS, TESTS))
