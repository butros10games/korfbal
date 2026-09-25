"""MFA-protected bounded upload transport; validation belongs to the service."""

import json
from typing import cast

from django.contrib.auth.models import User
from django.http import HttpRequest, JsonResponse

from apps.video_analysis.engine.store import Store
from apps.video_analysis.models import Workspace
from apps.video_analysis.services import uploads


MAX_JSON = 4096


def upload_endpoint(
    request: HttpRequest, action: str, store: Store, workspace: Workspace
) -> JsonResponse:
    """Receive small retryable requests, never a whole multi-gigabyte body.

    Raises:
        ValueError: The JSON value is not an object.

    """
    if request.method != "POST":
        return JsonResponse({"error": "Method not allowed"}, status=405)
    actor = cast(User, request.user)
    if action == "pipeline/upload/part":
        if request.content_type != "application/octet-stream":
            return JsonResponse({"error": "Expected a binary chunk"}, status=400)
        if int(request.META.get("CONTENT_LENGTH") or "0") > uploads.CHUNK_SIZE:
            return JsonResponse({"error": "Upload chunk too large"}, status=413)
        data = request.read(uploads.CHUNK_SIZE + 1)
        result = uploads.part(
            workspace,
            actor,
            store,
            key=request.GET["id"],
            chunk=uploads.Chunk(int(request.GET["index"]), data),
        )
    else:
        if request.content_type != "application/json" or len(request.body) > MAX_JSON:
            return JsonResponse({"error": "Expected bounded JSON"}, status=400)
        payload = json.loads(request.body)
        if not isinstance(payload, dict):
            raise ValueError("Expected an object")
        if action == "pipeline/upload":
            result = uploads.begin(workspace, actor, payload)
        elif action == "pipeline/upload/finish":
            result = uploads.finish(workspace, actor, payload["id"])
        elif action == "pipeline/upload/cancel":
            result = uploads.cancel(workspace, actor, payload["id"])
        else:
            return JsonResponse({"error": "Unknown action"}, status=404)
    return JsonResponse(result)
