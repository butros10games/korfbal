"""Shared, opt-in SSE snapshots/patches over authoritative public read models."""

from collections import deque
import json
from time import time
from typing import Any
from uuid import uuid4

from .compact_json import Dictionary, DictionaryFullError, changes


PUBLIC_RESOURCES = frozenset({"live", "summary", "events", "shots", "stats"})
RESOURCE_NAMES = (
    "live",
    "tracker",
    "summary",
    "events",
    "shots",
    "stats",
    "impacts",
    "player_groups",
    "mvp",
)
MAX_FRAME_BYTES = 512 * 1024
MAX_CURSOR_BYTES = 128
MAX_REPLAY_FRAMES = 32
MAX_REPLAY_BYTES = 64 * 1024


def timeline(
    current: dict[str, Any] | None, incoming: dict[str, Any], resource: str
) -> dict[str, Any] | None:
    """Materialize legacy deltas only when their exact public base is available."""
    if incoming.get("mode") != "delta":
        return incoming
    if current is None or not (
        incoming["base_revision"]
        <= current.get("live_revision", -1)
        <= incoming["live_revision"]
    ):
        return None
    rows = {str(row["event_id"]): row for row in current.get(resource, [])}
    for item_id in incoming["deleted_ids"]:
        rows.pop(item_id, None)
    rows.update({str(row["event_id"]): row for row in incoming["upsert"]})
    order = incoming["order"]
    if len(set(order)) != len(order) or any(item_id not in rows for item_id in order):
        return None
    return {
        **{
            key: value
            for key, value in incoming.items()
            if key not in {"mode", "base_revision", "upsert", "deleted_ids", "order"}
        },
        resource: [rows[item_id] for item_id in order],
    }


def compact_live(live: dict[str, Any]) -> dict[str, Any]:
    """Clock anchors come from the frame timestamp, never the patch dictionary."""
    timer = live.get("timer")
    if not isinstance(timer, dict) or "server_time" not in timer:
        return live
    return {
        **live,
        "timer": {key: value for key, value in timer.items() if key != "server_time"},
    }


def materialize(
    previous: dict[str, Any], event: dict[str, Any], resources: frozenset[str]
) -> dict[str, Any]:
    """Retain only subscribed public state; unknown bases trigger normal recovery."""
    document = dict(previous)
    reads = dict(previous.get("public_reads", {}))
    for resource in sorted(resources):
        if resource not in event["resources"]:
            continue
        incoming = (
            event.get("live")
            if resource == "live"
            else event.get("public_reads", {}).get(resource)
        )
        if resource == "live":
            if incoming is None:
                document.pop("live", None)
            else:
                document["live"] = compact_live(incoming)
            continue
        value = (
            timeline(reads.get(resource), incoming, resource)
            if incoming is not None and resource in {"events", "shots"}
            else incoming
        )
        if value is None:
            reads.pop(resource, None)
        else:
            reads[resource] = value
    # A verified contiguous notification confirms unchanged resources too.
    for key, value in reads.items():
        if "live_revision" in value:
            reads[key] = {**value, "live_revision": event["revision"]}
    if "live" in document:
        document["live"] = {**document["live"], "live_revision": event["revision"]}
    document["public_reads"] = reads
    return document


class CompactMatch:
    """One encoder/document per worker, match and bounded public subscription mask."""

    def __init__(self, match_id: str, resources: frozenset[str]) -> None:
        """Initialize an empty mapping and document until authoritative data arrives."""
        self.match_id, self.resources = match_id, resources
        self.dictionary = Dictionary()
        self.epoch = uuid4().hex
        self.history: deque[tuple[int, bytes]] = deque()
        self.history_bytes = 0
        self.sequence = 0
        self.revision = -1
        self.document: dict[str, Any] = {}
        self.full: dict[str, Any] | None = None

    def publish(self, event: dict[str, Any]) -> dict[str, Any]:
        """Encode one authoritative change, sharing the result across all viewers."""
        if event["revision"] < self.revision:
            return self.snapshot()
        previous = self.document
        if event["revision"] > self.revision + 1 and not event.get("snapshot"):
            event = {**event, "resources": sorted(RESOURCE_NAMES)}
            previous = {}
        if event.get("snapshot"):
            previous = {}
        document = materialize(previous, event, self.resources)
        base, start = self.sequence, len(self.dictionary.strings)
        self.sequence += 1
        self.revision = event["revision"]
        self.full = None
        try:
            operations = changes(self.document, document, self.dictionary)
            self.document = document
            result = self._frame(base, start, operations, event)
            if len(result["_wire"]) > MAX_FRAME_BYTES:
                result = self._reset(document)
        except DictionaryFullError:
            result = self._reset(document)
        wire = result["_wire"]
        self.history.append((self.sequence, wire))
        self.history_bytes += len(wire)
        while (
            len(self.history) > MAX_REPLAY_FRAMES
            or self.history_bytes > MAX_REPLAY_BYTES
        ):
            self.history_bytes -= len(self.history.popleft()[1])
        return result

    def replay(self, cursor: bytes) -> list[bytes] | None:
        """Return contiguous shared frames, or require an authoritative reset."""
        if len(cursor) > MAX_CURSOR_BYTES:
            return None
        epoch, separator, sequence = cursor.rpartition(b":")
        if not separator or epoch != self.epoch.encode() or not sequence.isdigit():
            return None
        previous = int(sequence)
        if previous > self.sequence:
            return None
        if previous == self.sequence:
            return []
        frames = [(number, wire) for number, wire in self.history if number > previous]
        if not frames or frames[0][0] != previous + 1 or frames[-1][0] != self.sequence:
            return None
        return [wire for _, wire in frames]

    def _reset(self, document: dict[str, Any]) -> dict[str, Any]:
        self.document = document
        self.dictionary = Dictionary()
        self.epoch = uuid4().hex
        self.history.clear()
        self.history_bytes = 0
        return self.snapshot()

    def missing(self) -> frozenset[str]:
        """Identify subscribed bases that still need a shared cache snapshot."""
        known = set(self.document.get("public_reads", {}))
        if "live" in self.document:
            known.add("live")
        return self.resources - known

    def seed(self, event: dict[str, Any]) -> dict[str, Any]:
        """Seed once per revision; late cache reads must not roll the stream back."""
        supplied = set(event.get("public_reads", {}))
        if "live" in event:
            supplied.add("live")
        if event["revision"] > self.revision:
            # Handshakes can see the database revision before its caches are ready.
            # Never discard an established cohort's base on that partial read;
            # broker delivery and durable reconciliation own revision advancement.
            if self.revision < 0 or self.resources <= supplied:
                self.publish(event)
        elif event["revision"] == self.revision and event.get("snapshot"):
            # A previously missing resource may become available after publication.
            supplied = set(event.get("public_reads", {})) & self.resources
            missing = supplied - set(self.document.get("public_reads", {}))
            if "live" in event and "live" not in self.document:
                missing.add("live")
            if missing:
                self.publish({**event, "snapshot": False, "resources": sorted(missing)})
        return self.snapshot()

    def snapshot(self) -> dict[str, Any]:
        """Share a complete reset for new/reconnecting or coalesced slow viewers."""
        if self.full is None:
            event = {
                "revision": max(0, self.revision),
                "resources": sorted(RESOURCE_NAMES),
                "snapshot": True,
            }
            try:
                encoded = self.dictionary.encode(self.document)
                result = self._frame(None, 0, [[0, [], encoded]], event)
                if len(result["_wire"]) > MAX_FRAME_BYTES:
                    result = self._empty_snapshot(event)
            except DictionaryFullError:
                # Bounded invalidation preserves authoritative HTTP fallback.
                result = self._empty_snapshot(event)
            self.full = result
        return self.full

    def _empty_snapshot(self, event: dict[str, Any]) -> dict[str, Any]:
        self.dictionary = Dictionary()
        self.epoch = uuid4().hex
        self.history.clear()
        self.history_bytes = 0
        self.document = {"public_reads": {}}
        return self._frame(
            None, 0, [[0, [], self.dictionary.encode(self.document)]], event
        )

    def _frame(
        self,
        base: int | None,
        start: int,
        operations: list[list[Any]],
        event: dict[str, Any],
    ) -> dict[str, Any]:
        packet = [
            1,
            self.match_id,
            self.epoch,
            self.sequence,
            base,
            self.revision,
            [RESOURCE_NAMES.index(str(resource)) for resource in event["resources"]],
            start,
            self.dictionary.strings[start:],
            operations,
            bool(event.get("snapshot")),
            event.get("published_at"),
            time(),
        ]
        data = json.dumps(packet, separators=(",", ":"))
        wire = (
            f"id: {self.epoch}:{self.sequence}\nevent: match.compact\ndata: {data}\n\n"
        ).encode()
        return {
            "type": "match.changed",
            "match_id": self.match_id,
            "revision": self.revision,
            "resources": event["resources"],
            "_compact": True,
            "_wire": wire,
        }
