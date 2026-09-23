"""Clip wire protocol behind the shared staff/MFA/CSRF boundary."""

import json
from typing import cast

from django.contrib.auth.models import User
from django.http import HttpRequest, HttpResponseBase, JsonResponse
from django.middleware.csrf import get_token

from apps.video_analysis.engine.store import Store
from apps.video_analysis.models import Workspace
from apps.video_analysis.services import clips


MAX_BODY = 100_000


def clip_endpoint(
    request: HttpRequest, action: str, store: Store, workspace: Workspace
) -> HttpResponseBase:
    """Dispatch only the bounded clip protocol after native authorization.

    Raises:
        TypeError: JSON body is not an object.

    """
    if request.method == "GET" and action == "clips":
        return JsonResponse(
            dict(clips.listing(store, workspace), csrf=get_token(request))
        )
    if request.method == "GET" and action == "clips/result":
        return JsonResponse(
            clips.result(
                store, workspace, request.GET.get("run", ""), request.GET.get("chunk")
            )
        )
    if request.method != "POST" or action not in {"clips", "clips/cancel"}:
        return JsonResponse({"error": "Method not allowed"}, status=405)
    if len(request.body) > MAX_BODY or request.content_type != "application/json":
        return JsonResponse({"error": "Expected a bounded JSON body"}, status=400)
    payload = json.loads(request.body)
    if not isinstance(payload, dict):
        raise TypeError("Expected object")
    if action == "clips/cancel":
        clips.cancel(store, workspace, payload["run_id"])
        return JsonResponse({"ok": True})
    job = clips.start(store, workspace, cast(User, request.user), payload)
    return JsonResponse({"job_id": str(job.pk), "queued": True}, status=202)
