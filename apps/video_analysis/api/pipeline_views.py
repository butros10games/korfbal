"""Authenticated pipeline transport; called only after the shared MFA check."""

import json
from typing import cast

from django.contrib.auth.models import User
from django.http import HttpRequest, JsonResponse
from django.middleware.csrf import get_token

from apps.video_analysis.engine.store import Store
from apps.video_analysis.models import Workspace
from apps.video_analysis.services import pipeline


MAX_BODY = 100_000


def pipeline_endpoint(
    request: HttpRequest, action: str, store: Store, workspace: Workspace
) -> JsonResponse:
    """Read bounded queues and accept explicit version-checked workflow commands."""
    if request.method == "GET":
        if action == "pipeline/clip":
            result = pipeline.clip_detail(workspace, int(request.GET["id"]))
        elif action == "pipeline":
            result = pipeline.listing(
                workspace,
                max(0, int(request.GET.get("after", "0"))),
                request.GET.get("before", ""),
            )
        else:
            return JsonResponse({"error": "Unknown action"}, status=404)
        return JsonResponse(dict(result, csrf=get_token(request)))
    if request.method != "POST":
        return JsonResponse({"error": "Method not allowed"}, status=405)
    if len(request.body) > MAX_BODY or request.content_type != "application/json":
        return JsonResponse({"error": "Expected bounded JSON"}, status=400)
    payload = json.loads(request.body)
    if not isinstance(payload, dict):
        return JsonResponse({"error": "Expected an object"}, status=400)
    return mutate(request, action, store, workspace, payload)


def mutate(
    request: HttpRequest, action: str, store: Store, workspace: Workspace, payload: dict
) -> JsonResponse:
    """Keep mutation dispatch explicit and within the authenticated workspace."""
    actor = cast(User, request.user)
    if action == "pipeline":
        run = pipeline.submit(workspace, actor, store, payload)
        return JsonResponse({"id": str(run.pk), "status": run.status}, status=202)
    if action == "pipeline/control":
        pipeline.control(workspace, payload)
    elif action == "pipeline/review":
        pipeline.review_clip(workspace, actor, payload)
    else:
        return JsonResponse({"error": "Unknown action"}, status=404)
    return JsonResponse({"ok": True})
