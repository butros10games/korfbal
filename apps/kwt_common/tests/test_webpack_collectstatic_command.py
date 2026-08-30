"""Failure and orchestration tests for webpack_collectstatic."""

from __future__ import annotations

from pathlib import Path
import subprocess

import pytest

from apps.kwt_common.management.commands import webpack_collectstatic


def test_command_fails_clearly_when_npx_is_missing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The command reports a usable error rather than an OS execution failure."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(webpack_collectstatic.shutil, "which", lambda _name: None)

    with pytest.raises(RuntimeError, match="npx executable not found"):
        webpack_collectstatic.Command().handle()


def test_command_fails_before_execution_when_config_is_missing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Webpack is never invoked without the repository-controlled config."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        webpack_collectstatic.shutil,
        "which",
        lambda _name: "/usr/bin/npx",
    )

    with pytest.raises(RuntimeError, match=r"webpack\.config\.js not found"):
        webpack_collectstatic.Command().handle()


def test_command_runs_validated_webpack_then_collectstatic(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Assets are copied before validated webpack and collectstatic calls."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "webpack.config.js").write_text("export default {};", encoding="utf-8")
    (tmp_path / "static_build" / "css").mkdir(parents=True)
    (tmp_path / "static_build" / "css" / "app.css").write_text(
        "body {}",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        webpack_collectstatic.shutil,
        "which",
        lambda _name: "/usr/bin/npx",
    )
    subprocess_calls: list[tuple[list[str], bool]] = []
    django_calls: list[tuple[str, bool]] = []
    monkeypatch.setattr(
        webpack_collectstatic.subprocess,
        "run",
        lambda command, *, check: subprocess_calls.append((command, check)),
    )
    monkeypatch.setattr(
        webpack_collectstatic,
        "call_command",
        lambda command, *, interactive: django_calls.append((command, interactive)),
    )

    webpack_collectstatic.Command().handle()

    assert (tmp_path / "static_workfile" / "css" / "app.css").read_text(
        encoding="utf-8"
    ) == "body {}"
    assert subprocess_calls == [
        (
            [
                "/usr/bin/npx",
                "webpack",
                "--config",
                str(tmp_path / "webpack.config.js"),
            ],
            True,
        )
    ]
    assert django_calls == [("collectstatic", False)]


def test_command_translates_webpack_process_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A failed webpack subprocess becomes a management-command error."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "webpack.config.js").touch()
    monkeypatch.setattr(
        webpack_collectstatic.shutil,
        "which",
        lambda _name: "/usr/bin/npx",
    )

    def fail(_command: list[str], *, check: bool) -> None:
        raise subprocess.CalledProcessError(1, "npx")

    monkeypatch.setattr(webpack_collectstatic.subprocess, "run", fail)

    with pytest.raises(RuntimeError, match="Webpack build failed"):
        webpack_collectstatic.Command().handle()
