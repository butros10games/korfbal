"""Roster naming for clip runs, behind the shared staff/MFA/CSRF boundary."""

import json
from typing import cast

from django.contrib.auth.models import User
from django.http import HttpRequest, HttpResponseBase, JsonResponse
from django.middleware.csrf import get_token

from apps.video_analysis.api.streaming import stored_response
from apps.video_analysis.engine.store import Store
from apps.video_analysis.models import Workspace
from apps.video_analysis.services import identity_review


MAX_BODY = 20_000
WRITES = {"clips/identity/answer", "clips/identity/prepare"}


def identity_endpoint(
    request: HttpRequest, action: str, store: Store, workspace: Workspace
) -> HttpResponseBase:
    """Read naming state and crops, or queue answers; solving happens elsewhere.

    Raises:
        TypeError: JSON body is not an object.

    """
    if request.method == "GET" and action == "clips/identity":
        return JsonResponse(
            dict(
                identity_review.read(store, workspace, request.GET.get("run", "")),
                csrf=get_token(request),
            )
        )
    if request.method == "GET" and action == "clips/identity/crop":
        # Private crops follow the same direct-S3-or-proxied path as frames.
        path = identity_review.crop_path(
            store, workspace, request.GET.get("run", ""), request.GET.get("name", "")
        )
        return stored_response(request, store, path)
    if request.method != "POST" or action not in WRITES:
        return JsonResponse({"error": "Method not allowed"}, status=405)
    if len(request.body) > MAX_BODY or request.content_type != "application/json":
        return JsonResponse({"error": "Expected a bounded JSON body"}, status=400)
    payload = json.loads(request.body)
    if not isinstance(payload, dict):
        raise TypeError("Expected object")
    if action == "clips/identity/prepare":
        return JsonResponse(
            identity_review.prepare(store, workspace, str(payload.get("run_id", ""))),
            status=202,
        )
    return answer(request, workspace, payload)


def answer(
    request: HttpRequest, workspace: Workspace, payload: dict
) -> HttpResponseBase:
    """Queue one answer; a stale revision gets a structured 409 to refresh to.

    Returns:
        202 when queued, 200 for a known retry, 409 for a stale revision.

    """
    try:
        status, body = identity_review.submit(
            workspace, cast(User, request.user), payload
        )
    except identity_review.RevisionConflictError as conflict:
        return JsonResponse(
            {
                "error": str(conflict),
                "code": "revision_conflict",
                "revision": conflict.revision,
                "pending": conflict.pending,
                "expected_revision": conflict.revision + conflict.pending,
            },
            status=409,
        )
    return JsonResponse(body, status=status)
