"""Endpoint pairing (docs/architecture.md, section 3, step 3).

A pair is ``observed`` when a recording of the replacement endpoint is paired
with a fixture of the legacy endpoint. Otherwise it is scored on method, path
shape and field-name overlap and is ``inferred``. Pairs below the threshold, and
ties, are not forced: the legacy endpoint is reported as a gap.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from revenant.reconstruction import names
from revenant.reconstruction.contract import EndpointContract, FixtureInfo

THRESHOLD = 0.5
_VERSION = re.compile(r"^v\d+([.\-]\d+)*$", re.IGNORECASE)


@dataclass
class Pair:
    legacy: EndpointContract
    replacement: EndpointContract
    confidence: str
    score: float
    fixture_pairs: list[tuple[FixtureInfo, FixtureInfo]]  # (legacy fixture, recording)


def _shape(path: str) -> list[str]:
    parts = [part for part in path.strip("/").split("/") if part and not _VERSION.match(part)]
    return ["{}" if part.startswith("{") else names.singular(part.lower()) for part in parts]


def _path_similarity(left: str, right: str) -> float:
    a, b = _shape(left), _shape(right)
    if not a or not b:
        return 1.0 if a == b else 0.0
    return sum(1 for x, y in zip(a, b, strict=False) if x == y) / max(len(a), len(b))


def _field_names(endpoint: EndpointContract) -> set[tuple[str, ...]]:
    paths = [*endpoint.request_fields.values(), *endpoint.success_fields().values()]
    return {names.tokens(names.leaf(info.path)) for info in paths if info.leaf}


def score(legacy: EndpointContract, replacement: EndpointContract) -> float:
    method = 1.0 if legacy.method == replacement.method else 0.0
    a, b = _field_names(legacy), _field_names(replacement)
    overlap = len(a & b) / len(a | b) if a | b else 0.0
    return round(0.3 * method + 0.4 * _path_similarity(legacy.path, replacement.path)
                 + 0.3 * overlap, 4)


def pair_endpoints(
    legacy: list[EndpointContract], replacement: list[EndpointContract]
) -> tuple[list[Pair], list[tuple[EndpointContract, str]]]:
    """Returns the pairs and, for each unpaired legacy endpoint, the reason."""
    by_fixture = {fixture.id: endpoint for endpoint in legacy for fixture in endpoint.fixtures}
    pairs: dict[str, Pair] = {}
    taken: set[str] = set()
    conflicts: dict[str, str] = {}

    # 1. Recordings paired with fixtures are direct evidence of the correspondence.
    linked: dict[tuple[str, str], list[tuple[FixtureInfo, FixtureInfo]]] = {}
    for endpoint in replacement:
        for recording in endpoint.fixtures:
            owner = by_fixture.get(recording.pair_with or "")
            if owner is not None:
                original = next(f for f in owner.fixtures if f.id == recording.pair_with)
                linked.setdefault((owner.id, endpoint.id), []).append((original, recording))
    legacy_by_id = {item.id: item for item in legacy}
    replacement_by_id = {item.id: item for item in replacement}
    targets: dict[str, set[str]] = {}
    for legacy_id, replacement_id in linked:
        targets.setdefault(legacy_id, set()).add(replacement_id)
    for legacy_id in sorted(targets, key=lambda i: legacy_by_id[i].name):
        if len(targets[legacy_id]) > 1:
            found = ", ".join(sorted(replacement_by_id[i].name for i in targets[legacy_id]))
            conflicts[legacy_id] = f"its fixtures are paired with recordings of several " \
                                   f"replacement endpoints ({found})"
            continue
        replacement_id = next(iter(targets[legacy_id]))
        if replacement_id in taken:
            conflicts[legacy_id] = (
                f"{replacement_by_id[replacement_id].name} is already paired with another "
                "legacy endpoint by recordings")
            continue
        taken.add(replacement_id)
        both = sorted(linked[(legacy_id, replacement_id)], key=lambda p: p[0].name)
        pairs[legacy_id] = Pair(legacy_by_id[legacy_id], replacement_by_id[replacement_id],
                                "observed", 1.0, both)

    # 2. Everything else is scored. Best scores first; a tie is not broken by guessing.
    candidates = sorted(
        ((score(left, right), left.name, right.name, left, right)
         for left in legacy if left.id not in pairs and left.id not in conflicts
         for right in replacement if right.id not in taken),
        key=lambda row: (-row[0], row[1], row[2]))
    best: dict[str, list[tuple[float, EndpointContract]]] = {}
    for value, _ln, _rn, left, right in candidates:
        if value >= THRESHOLD:
            best.setdefault(left.id, []).append((value, right))
    claimed: dict[str, list[str]] = {}
    for legacy_id, options in best.items():
        top = [right for value, right in options if value == options[0][0]]
        if len(top) > 1:
            found = ", ".join(sorted(item.name for item in top))
            conflicts[legacy_id] = f"several replacement endpoints match equally well ({found})"
        else:
            claimed.setdefault(top[0].id, []).append(legacy_id)
    for replacement_id, claimants in claimed.items():
        if len(claimants) > 1:
            for legacy_id in claimants:
                conflicts[legacy_id] = (
                    f"{replacement_by_id[replacement_id].name} is the best match for several "
                    "legacy endpoints")
            continue
        legacy_id = claimants[0]
        value = best[legacy_id][0][0]
        pairs[legacy_id] = Pair(legacy_by_id[legacy_id], replacement_by_id[replacement_id],
                                "inferred", value, [])

    unpaired = [
        (endpoint, conflicts.get(endpoint.id, "no replacement endpoint is similar enough"))
        for endpoint in legacy if endpoint.id not in pairs
    ]
    ordered = sorted(pairs.values(), key=lambda pair: (pair.legacy.path, pair.legacy.method))
    return ordered, unpaired
