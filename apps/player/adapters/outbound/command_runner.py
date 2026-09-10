"""Subprocess-backed command execution adapter."""

from __future__ import annotations

from collections.abc import Sequence
from contextlib import suppress
import os
import signal
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
        if options.kill_process_tree and os.name == "posix":
            return self._run_isolated(cmd, options)
        return subprocess.run(  # nosec B603
            list(cmd),
            check=options.check,
            capture_output=options.capture_output,
            text=options.text,
            timeout=options.timeout,
            shell=False,
        )

    @staticmethod
    def _run_isolated(
        cmd: Sequence[str], options: CommandRunOptions
    ) -> subprocess.CompletedProcess[str]:
        """Terminate decoder descendants as well as the downloader on timeout.

        Raises:
            TimeoutExpired: The command exceeded its deadline.

        """
        with subprocess.Popen(
            list(cmd),
            stdout=subprocess.PIPE if options.capture_output else None,
            stderr=subprocess.PIPE if options.capture_output else None,
            text=options.text,
            start_new_session=True,
            shell=False,
        ) as process:
            try:
                stdout, stderr = process.communicate(timeout=options.timeout)
            except subprocess.TimeoutExpired:
                with suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
                process.communicate()
                raise
            result = subprocess.CompletedProcess(
                list(cmd), process.returncode, stdout, stderr
            )
            if options.check:
                result.check_returncode()
            return result
