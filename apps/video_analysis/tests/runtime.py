"""Pipeline capability bindings for tests: production wiring with explicit fakes."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from apps.video_analysis import composition
from apps.video_analysis.application.ports import PipelineRuntime


def pipeline_runtime(**fakes: Any) -> PipelineRuntime:  # noqa: ANN401
    """Return the production runtime with selected capabilities replaced."""
    return replace(composition.pipeline_runtime(), **fakes)
