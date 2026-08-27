"""Subprocess-backed command execution adapter."""

from __future__ import annotations

from collections.abc import Sequence
import subprocess  # nosec B404

from apps.player.application.ports import CommandRunOptions


class SubprocessCommandRunner:
    """Run fixed argument lists through the operating system."""

    def run(
        self,
        cmd: Sequence[str],
        options: CommandRunOptions,
    ) -> subprocess.CompletedProcess[str]:
        """Run a command with explicitly supplied safety options."""
        return subprocess.run(  # nosec B603
            list(cmd),
            check=options.check,
            capture_output=options.capture_output,
            text=options.text,
            timeout=options.timeout,
            shell=False,
        )
