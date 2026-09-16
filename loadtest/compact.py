"""Independent compact SSE decoder for synthetic load and shared contract fixtures."""

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from apps.game_tracker.adapters.outbound.compact_match import RESOURCE_NAMES


OBJECT_TAG = 2
SPLICE = 2
MAX_DICTIONARY = 8192


class CompactDecodeError(Exception):
    """A synthetic viewer received an unusable compact event."""


def number(value: object) -> int:
    """Validate nonnegative integer slots and revisions.

    Raises:
        CompactDecodeError: The value is not a valid protocol integer.

    """
    if type(value) is not int or value < 0:
        raise CompactDecodeError("Invalid compact integer")
    return value


def decode_value(value: object, strings: list[str]) -> object:
    """Decode tagged JSON strings, arrays and objects.

    Raises:
        CompactDecodeError: The compact packet cannot be applied.

    """
    if not isinstance(value, list):
        return value
    if value[0] == 0:
        return strings[number(value[1])]
    if value[0] == 1:
        return [decode_value(item, strings) for item in value[1:]]
    if value[0] != OBJECT_TAG:
        raise CompactDecodeError("Invalid compact value")
    return {
        strings[number(value[index])]: decode_value(value[index + 1], strings)
        for index in range(1, len(value), 2)
    }


def change_at(
    value: object,
    path: list[int],
    strings: list[str],
    update: Callable[[object], object],
    *,
    remove: bool = False,
) -> object:
    """Apply copy-on-write paths so cached prior events remain immutable.

    Raises:
        CompactDecodeError: The compact packet cannot be applied.

    """
    if not path:
        return update(value)
    slot, *tail = path
    if isinstance(value, list):
        result = list(value)
        result[slot] = change_at(result[slot], tail, strings, update, remove=remove)
        return result
    if not isinstance(value, dict):
        raise CompactDecodeError("Invalid compact path")
    result = dict(value)
    key = strings[slot]
    if tail:
        result[key] = change_at(result[key], tail, strings, update, remove=remove)
    elif remove:
        del result[key]
    else:
        result[key] = update(result.get(key))
    return result


def apply(value: object, operation: list[Any], strings: list[str]) -> object:
    """Apply a field replacement, field deletion or array splice.

    Raises:
        CompactDecodeError: The compact packet cannot be applied.

    """
    kind, path, *args = operation
    if kind == 0:
        decoded = decode_value(args[0], strings)
        return change_at(value, path, strings, lambda _previous: decoded)
    if kind == 1:
        return change_at(value, path, strings, lambda _previous: None, remove=True)
    if kind != SPLICE:
        raise CompactDecodeError("Invalid compact operation")
    start, count, inserted = args
    items = decode_value(inserted, strings)
    assert isinstance(items, list)

    def splice(previous: object) -> object:
        if not isinstance(previous, list) or start < 0 or start + count > len(previous):
            raise CompactDecodeError("Invalid compact splice")
        return previous[:start] + items + previous[start + count :]

    return change_at(value, path, strings, splice)


def add_live(
    event: dict[str, Any], document: dict[str, Any], names: list[str], emitted: float
) -> None:
    """Keep per-message clock anchors out of the shared patch document."""
    if "live" in names and "live" in document:
        live = document["live"]
        timer = live["timer"]
        if timer["type"] != "deactivated":
            timer = {
                **timer,
                "server_time": datetime.fromtimestamp(emitted, UTC).isoformat(),
            }
        event["live"] = {**live, "timer": timer}


class CompactDecoder:
    """Keep mapping and sequence checks independent from the backend encoder."""

    def __init__(self, match_id: str) -> None:
        """Scope one synthetic viewer to one public match."""
        self.match_id = match_id
        self.epoch: str | None = None
        self.sequence = -1
        self.revision = -1
        self.strings: list[str] = []
        self.document: object = {}

    def decode(self, packet: list[Any]) -> dict[str, Any] | None:
        """Return a canonical event, rejecting missing mappings/patch bases.

        Raises:
            CompactDecodeError: The compact packet cannot be applied.

        """
        (
            version,
            match_id,
            epoch,
            sequence,
            base,
            revision,
            resources,
            start,
            additions,
            operations,
            snapshot,
            published,
            emitted,
        ) = packet
        if version != 1 or match_id != self.match_id:
            raise CompactDecodeError("Invalid compact stream identity")
        if revision < self.revision or (
            epoch == self.epoch
            and (
                sequence < self.sequence
                or (sequence == self.sequence and base is not None)
            )
        ):
            return None
        if base is None:
            if start != 0 or not snapshot:
                raise CompactDecodeError("Invalid compact reset")
            strings, document = additions, {}
        else:
            if (
                epoch != self.epoch
                or base != self.sequence
                or sequence != self.sequence + 1
                or start != len(self.strings)
            ):
                raise CompactDecodeError("Missing compact base")
            strings, document = self.strings + additions, self.document
        if len(strings) > MAX_DICTIONARY:
            raise CompactDecodeError("Oversized compact dictionary")
        for operation in operations:
            document = apply(document, operation, strings)
        assert isinstance(document, dict)
        names = [RESOURCE_NAMES[number(index)] for index in resources]
        event = {
            "match_id": match_id,
            "revision": revision,
            "resources": names,
            "snapshot": snapshot,
            "public_reads": {
                key: value
                for key, value in document.get("public_reads", {}).items()
                if key in names
            },
        }
        add_live(event, document, names, emitted)
        if published is not None:
            event["published_at"] = published
        self.epoch, self.sequence, self.revision = epoch, sequence, revision
        self.strings, self.document = strings, document
        return event
