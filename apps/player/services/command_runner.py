"""Outbound command execution port."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
import subprocess  # nosec B404
from typing import Protocol


@dataclass(frozen=True, slots=True)
class CommandRunOptions:
    """Options for a command-runner invocation."""

    check: bool
    capture_output: bool = False
    text: bool = False
    timeout: int | None = None
    shell: bool = False


class CommandRunner(Protocol):
    """Protocol for running fixed argv-list commands."""

    def run(
        self,
        cmd: Sequence[str],
        options: CommandRunOptions,
    ) -> subprocess.CompletedProcess[str]:
        """Run the command and return the completed process."""


class SubprocessCommandRunner:
    """Production command runner backed by subprocess."""

    def run(
        self,
        cmd: Sequence[str],
        options: CommandRunOptions,
    ) -> subprocess.CompletedProcess[str]:
        """Run the command with subprocess."""
        return subprocess.run(  # nosec B603
            list(cmd),
            check=options.check,
            capture_output=options.capture_output,
            text=options.text,
            timeout=options.timeout,
            shell=options.shell,
        )


DEFAULT_COMMAND_RUNNER = SubprocessCommandRunner()
