"""Measure real synthetic public payloads without changing the live protocol."""

from copy import deepcopy
import gzip
import json
import re
import sys
from time import time
from typing import Any
from unittest.mock import patch
from uuid import uuid4

from django.conf import settings

from apps.game_tracker.adapters.outbound.compact_match import (
    PUBLIC_RESOURCES,
    CompactMatch,
)
from apps.game_tracker.adapters.outbound.match_bootstrap import read_bootstrap
from apps.game_tracker.adapters.outbound.match_fanout import encode_event
from apps.game_tracker.adapters.outbound.shared_compact import SharedCompactStore
from apps.game_tracker.composition import (
    apply_tracker_command,
    prepare_public_match_reads,
    publish_public_live_snapshot,
)
from apps.game_tracker.models import MatchData
from apps.game_tracker.services.tracker_commands.registry import COMMAND_DEFINITIONS
from apps.schedule.models import Match
from loadtest.payload_scenarios import prepare_reserve, scenarios


UUID_VALUE = re.compile(
    r"(?:[a-z_]+:)?[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)


def json_bytes(value: object) -> bytes:
    """Match the compact JSON encoding used by public SSE frames."""
    return json.dumps(value, separators=(",", ":")).encode()


def short_references(value: object, dictionary: dict[str, int]) -> object:
    """Prototype typed UUID references; the dictionary must precede these messages."""
    if isinstance(value, str) and UUID_VALUE.fullmatch(value):
        return {"$ref": dictionary.setdefault(value, len(dictionary))}
    if isinstance(value, dict):
        return {key: short_references(item, dictionary) for key, item in value.items()}
    if isinstance(value, list):
        return [short_references(item, dictionary) for item in value]
    return value


def restore_references(value: object, dictionary: list[str]) -> object:
    """Verify prototype round trips for these synthetic payloads."""
    if isinstance(value, dict):
        if set(value) == {"$ref"}:
            return dictionary[value["$ref"]]
        return {
            key: restore_references(item, dictionary) for key, item in value.items()
        }
    if isinstance(value, list):
        return [restore_references(item, dictionary) for item in value]
    return value


def measure(event: dict[str, Any], dictionary: dict[str, int]) -> dict[str, Any]:
    """Separate payload volume, dictionary overhead and hypothetical compression."""
    event = json.loads(encode_event(event).split(b"data: ", 1)[1])
    wire = encode_event(event)
    count = len(dictionary)
    compact = short_references(event, dictionary)
    assert restore_references(compact, list(dictionary)) == event
    additions = list(dictionary)[count:]
    reads = event.get("public_reads", {})
    mounted = {"live", "summary", "events"}
    filtered = {
        **event,
        "resources": [item for item in event["resources"] if item in mounted],
        "public_reads": {key: value for key, value in reads.items() if key in mounted},
    }
    return {
        "frame_bytes": len(wire),
        "json_bytes": len(json_bytes(event)),
        "gzip_frame_bytes": len(gzip.compress(wire, mtime=0)),
        "top_level_json_bytes": {
            key: len(json_bytes(value)) for key, value in event.items()
        },
        "public_resource_json_bytes": {
            key: len(json_bytes(value)) for key, value in reads.items()
        },
        "timeline_order_json_bytes": {
            key: len(json_bytes(value.get("order", [])))
            for key, value in reads.items()
            if key in {"events", "shots"}
        },
        "live_summary_events_frame_bytes": len(encode_event(filtered)),
        "short_reference_json_bytes": len(json_bytes(compact)),
        "new_dictionary_entries": len(additions),
        "new_dictionary_json_bytes": len(json_bytes(additions)),
        "short_reference_envelope_json_bytes": len(
            json_bytes({"dictionary": additions, "event": compact})
        ),
        "short_reference_roundtrip": True,
    }


def compact_breakdown(wire: bytes) -> dict[str, int]:
    """Separate the actual SSE envelope, dictionary and patch operation bytes."""
    body = wire.split(b"data: ", 1)[1].rstrip(b"\n")
    packet = json.loads(body)
    return {
        "sse_envelope_bytes": len(wire) - len(body),
        "dictionary_json_bytes": len(json_bytes(packet[8])),
        "operations_json_bytes": len(json_bytes(packet[9])),
        "packet_bytes": len(body),
    }


def profile_payloads(fixture: dict[str, Any]) -> dict[str, Any]:
    """Capture real starting data and all registered tracker command publications.

    Raises:
        RuntimeError: The database is not the disposable load-test database.

    """
    if settings.DATABASES["default"]["NAME"] != "korfbal_loadtest":
        raise RuntimeError("Profiling requires the disposable load-test database.")
    match = Match.objects.select_related(
        "home_team__club", "away_team__club", "season"
    ).get(pk=fixture["match_id"])
    reserve = prepare_reserve(match)
    prepare_public_match_reads(match_id=str(match.pk))
    publish_public_live_snapshot(match_id=str(match.pk))
    revision = MatchData.objects.get(match_link=match).live_revision
    bootstrap = read_bootstrap(str(match.pk), revision)
    assert bootstrap is not None
    dictionary: dict[str, int] = {}
    codecs = {
        "all_public": CompactMatch(str(match.pk), PUBLIC_RESOURCES),
        "live_summary_events": CompactMatch(
            str(match.pk), frozenset({"live", "summary", "events"})
        ),
    }

    reference_codecs = {
        name: CompactMatch(str(match.pk), codec.resources)
        for name, codec in codecs.items()
    }
    shared: dict[str, CompactMatch] = {}

    def shared_measure(
        name: str, resources: frozenset[str], event: dict[str, Any]
    ) -> dict[str, Any]:
        previous = shared.get(name)
        cursor = f"{previous.epoch}:{previous.sequence}".encode() if previous else b""
        canonical = SharedCompactStore().advance(
            str(match.pk), resources, event, seed=bool(event.get("snapshot"))
        )
        # A fresh store/codec reads Redis after the publishing worker is gone.
        restored = SharedCompactStore().advance(
            str(match.pk), resources, initial, seed=True
        )
        assert (canonical.epoch, canonical.sequence) == (
            restored.epoch,
            restored.sequence,
        )
        replay = restored.replay(cursor)
        shared[name] = restored
        return {
            "full_snapshot_bytes": len(restored.snapshot()["_wire"]),
            "cross_worker_replay_bytes": None
            if replay is None
            else sum(map(len, replay)),
            "same_epoch_and_sequence": True,
        }

    def compact_measure(event: dict[str, Any]) -> dict[str, Any]:
        sizes, reconnect, cross_worker, reference, breakdown = {}, {}, {}, {}, {}
        for name, codec in codecs.items():
            cross_worker[name] = shared_measure(name, codec.resources, event)
            cursor = f"{codec.epoch}:{codec.sequence}".encode()
            before = reference_codecs[name]
            with patch(
                "apps.game_tracker.adapters.outbound.compact_match.time",
                return_value=time(),
            ):
                frame = (
                    codec.seed(event) if event.get("snapshot") else codec.publish(event)
                )
                with patch(
                    "apps.game_tracker.adapters.outbound.compact_match.compact_live",
                    side_effect=lambda value: value,
                ):
                    old_frame = (
                        before.seed(event)
                        if event.get("snapshot")
                        else before.publish(event)
                    )
            sizes[name] = len(frame["_wire"])
            reference[name] = len(old_frame["_wire"])
            breakdown[name] = compact_breakdown(frame["_wire"])
            replay = codec.replay(cursor)
            reconnect[name] = {
                "full_snapshot_bytes": len(codec.snapshot()["_wire"]),
                "replay_bytes": None if replay is None else sum(map(len, replay)),
                "cursor_line_bytes": len(frame["_wire"].split(b"\n", 1)[0]) + 1,
            }
        return {
            "compact_sse_bytes": sizes,
            "compact_reconnect": reconnect,
            "shared_reconnect": cross_worker,
            "before_clock_cleanup_bytes": reference,
            "compact_breakdown": breakdown,
        }

    compact_bootstrap = read_bootstrap(str(match.pk), revision, budget=1024 * 1024)
    assert compact_bootstrap is not None
    initial = {**compact_bootstrap.event, "live": compact_bootstrap.live_state()}
    samples = [
        {
            "kind": "bootstrap",
            **measure(bootstrap.event, dictionary),
            **compact_measure(initial),
        }
    ]
    covered = set()
    for label, payload in scenarios(match, fixture, reserve):
        covered.add(payload["command"])
        with patch(
            "apps.game_tracker.adapters.outbound.runtime.publish_match_changed"
        ) as publish:
            apply_tracker_command(
                match,
                team=match.home_team,
                payload={
                    **payload,
                    "command_id": str(uuid4()),
                    "expected_revision": MatchData.objects.get(
                        match_link=match
                    ).live_revision,
                },
            )
        if not publish.call_args_list:
            samples.append({
                "kind": label,
                "command": payload["command"],
                "publications": 0,
            })
        for call in publish.call_args_list:
            event = {**deepcopy(call.kwargs), "published_at": time()}
            samples.append({
                "kind": label,
                "command": payload["command"],
                **measure(event, dictionary),
                **compact_measure(event),
            })
    assert covered == {definition.name for definition in COMMAND_DEFINITIONS}
    return {
        "covered_commands": sorted(covered),
        "samples": samples,
        "limitations": [
            (
                "Synthetic fixture; initial snapshot and all registered tracker "
                "commands, not a traffic-weighted network benchmark."
            ),
            (
                "Asynchronous statistics publications and reconnect repetitions "
                "are not included."
            ),
            (
                "UUID references are a reversible prototype using tagged JSON "
                "objects; dictionary additions are counted and must be delivered "
                "before use. A production protocol needs schema/mapping versions, "
                "replay and reset rules."
            ),
            (
                "Filtered frames assume a viewer subscribes only to live, summary "
                "and events; other tabs would need their own resources."
            ),
            (
                "Gzip figures compress each whole frame offline; they do not "
                "validate streaming compression, CPU cost or proxy flushing."
            ),
            (
                "Bootstrap live clock is delivered separately in ready and is "
                "excluded here, as are HTTP/TCP/TLS headers."
            ),
        ],
    }


def main() -> None:
    """Emit aggregate sizes only; retain no player identities or raw payloads."""
    print(json.dumps(profile_payloads(json.load(sys.stdin))))
