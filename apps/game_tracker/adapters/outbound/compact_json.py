"""Version-one compact JSON values and deterministic incremental operations."""

from typing import Any


MAX_DICTIONARY = 8192


class DictionaryFullError(Exception):
    """A mapping must be replaced by a bounded full snapshot."""


class Dictionary:
    """Intern keys, player/event identities and enum strings within a match stream."""

    def __init__(self) -> None:
        """Start a new mapping; integer slots never change meaning within it."""
        self.strings: list[str] = []
        self.index: dict[str, int] = {}

    def intern(self, value: str) -> int:
        """Return a stable short reference or signal a required mapping reset.

        Raises:
            DictionaryFullError: The bounded mapping is full.

        """
        if value not in self.index:
            if len(self.strings) >= MAX_DICTIONARY:
                raise DictionaryFullError
            self.index[value] = len(self.strings)
            self.strings.append(value)
        return self.index[value]

    def encode(self, value: object) -> object:
        """Tag JSON containers and strings; numbers/bools/null remain literals."""
        if isinstance(value, str):
            return [0, self.intern(value)]
        if isinstance(value, list):
            return [1, *(self.encode(item) for item in value)]
        if isinstance(value, dict):
            result: list[object] = [2]
            for key, item in value.items():
                result.extend((self.intern(key), self.encode(item)))
            return result
        return value


def identity(value: object) -> object:
    """Match retained timeline rows across insertions without repeating their data."""
    if isinstance(value, dict):
        for field in ("event_id", "id_uuid"):
            if field in value:
                return value[field]
    return value


def changes(
    previous: object,
    current: object,
    dictionary: Dictionary,
    path: list[int] | None = None,
) -> list[list[Any]]:
    """Diff authoritative JSON, including array insertions/deletions and row edits."""
    path = [] if path is None else path
    if previous == current:
        return []
    if isinstance(previous, dict) and isinstance(current, dict):
        operations: list[list[Any]] = [
            [1, [*path, dictionary.intern(key)]]
            for key in previous
            if key not in current
        ]
        for key, value in current.items():
            child = [*path, dictionary.intern(key)]
            if key not in previous:
                operations.append([0, child, dictionary.encode(value)])
            else:
                operations.extend(changes(previous[key], value, dictionary, child))
        return operations
    if isinstance(previous, list) and isinstance(current, list):
        return array_changes(previous, current, dictionary, path)
    return [[0, path, dictionary.encode(current)]]


def array_changes(
    previous: list[Any], current: list[Any], dictionary: Dictionary, path: list[int]
) -> list[list[Any]]:
    """Splice only the changed interval, then patch retained rows individually."""
    prefix = 0
    while prefix < min(len(previous), len(current)) and identity(
        previous[prefix]
    ) == identity(current[prefix]):
        prefix += 1
    suffix = 0
    while suffix < min(len(previous), len(current)) - prefix and identity(
        previous[-suffix - 1]
    ) == identity(current[-suffix - 1]):
        suffix += 1
    old_end, new_end = len(previous) - suffix, len(current) - suffix
    operations: list[list[Any]] = []
    if old_end != prefix or new_end != prefix:
        operations.append([
            2,
            path,
            prefix,
            old_end - prefix,
            dictionary.encode(current[prefix:new_end]),
        ])
    for index in range(prefix):
        operations.extend(
            changes(previous[index], current[index], dictionary, [*path, index])
        )
    for offset in range(suffix):
        operations.extend(
            changes(
                previous[old_end + offset],
                current[new_end + offset],
                dictionary,
                [*path, new_end + offset],
            )
        )
    return operations
