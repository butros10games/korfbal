"""Loopback-only review website with media seeking and bounded Luna jobs."""

from __future__ import annotations

from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import logging
import mimetypes
from pathlib import Path
import secrets
import subprocess
import threading
from typing import Any
from urllib.parse import parse_qs, urlsplit
import zipfile

from . import monitor, training, vision
from .luna import analyze_frame
from .media import sample_frame
from .store import ConflictError, Store, frame_version, number


# The review interface is the KorfConnect app's /video-analysis page; this local
# server only exposes the workspace API and media.
APP_NOTICE = (
    b"<!doctype html><meta charset=utf-8><title>Korfbal video review</title>"
    b"<p>This server provides the video review API. Open Videoanalyse in the "
    b"KorfConnect app to review footage.</p>"
)
MAX_BODY = 100_000
CHUNK_SIZE = 1024 * 1024


class ReviewServer(ThreadingHTTPServer):
    """Serve one isolated local dataset; never bind publicly without authentication."""

    def __init__(self, store: Store, port: int, provider: str) -> None:
        """Bind loopback with a dataset CSRF token and one worker slot."""
        self.store = store
        self.provider = provider
        # Keep open editors valid across a code-only restart of this dataset.
        with store.transaction():
            token_file = store.root / ".csrf-token"
            if not token_file.exists():
                token_file.touch(mode=0o600)
                token_file.write_text(secrets.token_urlsafe(32))
            self.csrf = token_file.read_text().strip()
        self.job: dict[str, Any] = {"running": False, "message": ""}
        self.job_lock = threading.Lock()
        super().__init__(("127.0.0.1", port), ReviewHandler)

    def analyze(self, payload: dict[str, Any]) -> None:
        """Start a single user-requested image annotation, never an unbounded batch.

        Raises:
            ConflictError: If another client has saved a newer revision.
            TypeError: If a request field has an unsupported type.

        """
        match_id, frame_id = payload.get("match_id"), payload.get("frame_id")
        if not isinstance(match_id, str) or not isinstance(frame_id, str):
            raise TypeError("Select a match and frame")
        with self.job_lock:
            if self.job["running"]:
                raise ConflictError("Luna is already reviewing a frame")
            self.job = {
                "running": True,
                "message": "Luna is examining this frame…",
                "frame_id": frame_id,
            }
        threading.Thread(
            target=self._run_job, args=(match_id, frame_id), daemon=True
        ).start()

    def propose(self, payload: dict[str, Any]) -> None:
        """Run a bounded detector review with registered weights in a worker.

        Raises:
            ConflictError: If the operation or input is invalid.
            TypeError: If the operation or input is invalid.
            ValueError: If the operation or input is invalid.

        """
        match_id = payload.get("match_id")
        if not isinstance(match_id, str):
            raise TypeError("Select a match")
        selected = payload.get("model", "pretrained")
        weights = "yolo26n.pt"
        if selected != "pretrained":
            root = vision.artifact(self.store, "runs", selected)
            run = json.loads((root / "run.json").read_text())
            if run["kind"] != "train" or run["status"] != "completed":
                raise ValueError("Select a completed training run")
            weights = str(root / "fit" / "weights" / "best.pt")
        with self.job_lock:
            if self.job["running"]:
                raise ConflictError("An analysis job is already running")
            self.job = {
                "running": True,
                "message": "Detector is checking up to 25 pending frames…",
            }
        threading.Thread(
            target=self._run_detector, args=(weights, match_id), daemon=True
        ).start()

    def _run_detector(self, weights: str, match_id: str) -> None:
        try:
            result = training.proposals(
                self.store,
                weights,
                options=training.RunOptions(match_id=match_id, limit=25),
            )
            message = (
                f"Detector review ready: {result['frames']} frames. "
                "Reload to compare proposals."
            )
        except Exception as error:
            logging.getLogger(__name__).exception("Detector job failed")
            message = str(error)
        with self.job_lock:
            self.job = {"running": False, "message": message}

    def _run_job(self, match_id: str, frame_id: str) -> None:
        try:
            analyze_frame(self.store, match_id, frame_id, self.provider)
            message = "Luna's proposal is ready. Reload the queue to review it."
        except (ValueError, TypeError, OSError, subprocess.SubprocessError) as error:
            message = str(error)
        with self.job_lock:
            self.job = {"running": False, "message": message, "frame_id": frame_id}


class ReviewHandler(BaseHTTPRequestHandler):
    """JSON endpoints and range-capable files with local request validation."""

    server: ReviewServer

    def log_message(self, *args: object, **kwargs: object) -> None:
        """Suppress raw URLs and browser headers in shared terminal logs."""

    def _local_request(self) -> bool:
        allowed = {
            f"127.0.0.1:{self.server.server_port}",
            f"localhost:{self.server.server_port}",
        }
        host = self.headers.get("Host", "")
        origin = self.headers.get("Origin")
        return host in allowed and (
            not origin or origin in {f"http://{h}" for h in allowed}
        )

    def _headers(self, status: int, mime: str, length: int) -> None:
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; "
            "style-src 'self' 'unsafe-inline'; img-src 'self'; media-src 'self'; "
            "connect-src 'self'; frame-ancestors 'none'; "
            "base-uri 'none'; form-action 'self'",
        )

    def _json(self, status: int, payload: object) -> None:
        body = json.dumps(payload, allow_nan=False).encode()
        self._headers(status, "application/json", len(body))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        """Serve the review shell, persisted state, exports, and registered media."""
        if not self._local_request():
            self._json(
                HTTPStatus.FORBIDDEN,
                {"error": "This reviewer accepts local requests only."},
            )
            return
        try:
            self._get()
        except (ValueError, FileNotFoundError):
            self._json(HTTPStatus.NOT_FOUND, {"error": "Resource not found"})
        except (BrokenPipeError, ConnectionResetError):
            # Browsers cancel video range requests while seeking.
            return

    def _get(self) -> None:
        url = urlsplit(self.path)
        if url.path == "/api/state":
            data = self.server.store.read()
            for match in data["matches"]:
                for frame in match["frames"]:
                    frame["frame_version"] = frame_version(frame)
            self._json(
                HTTPStatus.OK,
                {
                    **data,
                    "csrf": self.server.csrf,
                    "provider": self.server.provider,
                },
            )
        elif url.path == "/api/monitor":
            self._json(HTTPStatus.OK, monitor.summary(self.server.store))
        elif url.path.startswith("/api/vision"):
            self._vision_get(url.path, url.query)
        elif url.path == "/api/job":
            with self.server.job_lock:
                self._json(HTTPStatus.OK, self.server.job)
        elif url.path == "/api/export":
            body = self.server.store.export()
            self._headers(HTTPStatus.OK, "application/zip", len(body))
            self.send_header(
                "Content-Disposition", 'attachment; filename="korfbal-reviewed.zip"'
            )
            self.end_headers()
            self.wfile.write(body)
        elif url.path == "/media":
            relative = parse_qs(url.query).get("path", [""])[0]
            self._media(relative)
        elif url.path == "/":
            self._headers(HTTPStatus.OK, "text/html; charset=utf-8", len(APP_NOTICE))
            self.end_headers()
            self.wfile.write(APP_NOTICE)
        else:
            raise ValueError("Unknown route")

    def _media(self, relative: str) -> None:
        """Serve only media registered in this dataset.

        Raises:
            ValueError: If the file is not registered.

        """
        data = self.server.store.read()
        registered = {m.get("video") for m in data["matches"]}
        registered.update(f["image"] for m in data["matches"] for f in m["frames"])
        if relative not in registered:
            raise ValueError("Unregistered file")
        self._file(self.server.store.media(relative))

    def _vision_get(self, path: str, query_string: str) -> None:
        """Serve independent training artifacts and progress.

        Raises:
            ValueError: If the operation or input is invalid.

        """
        if path == "/api/vision":
            self._json(HTTPStatus.OK, vision.inventory(self.server.store))
        elif path == "/api/vision/prediction":
            query = parse_qs(query_string)
            self._json(
                HTTPStatus.OK,
                vision.read_prediction(
                    self.server.store,
                    query.get("run", [""])[0],
                    query.get("match", [""])[0],
                    query.get("frame", [""])[0],
                ),
            )
        elif path == "/api/vision/queue":
            run = parse_qs(query_string).get("run", [""])[0]
            self._json(
                HTTPStatus.OK, {"frames": vision.review_queue(self.server.store, run)}
            )
        elif path == "/api/vision/download":
            name = parse_qs(query_string).get("id", [""])[0]
            root = vision.artifact(self.server.store, "snapshots", name)
            vision.verify_snapshot(root)
            output = io.BytesIO()
            with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
                for member in root.rglob("*"):
                    if member.is_file():
                        archive.write(member, str(member.relative_to(root.parent)))
            body = output.getvalue()
            self._headers(HTTPStatus.OK, "application/zip", len(body))
            self.send_header(
                "Content-Disposition", f'attachment; filename="{name}.zip"'
            )
            self.end_headers()
            self.wfile.write(body)
        else:
            raise ValueError("Unknown workflow route")

    def _file(self, path: Path) -> None:
        size = path.stat().st_size
        start, end = 0, size - 1
        status = HTTPStatus.OK
        requested = self.headers.get("Range")
        if requested:
            try:
                start, end = parse_range(requested, size)
            except ValueError:
                self._headers(
                    HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE, "text/plain", 0
                )
                self.send_header("Content-Range", f"bytes */{size}")
                self.end_headers()
                return
            status = HTTPStatus.PARTIAL_CONTENT
        length = end - start + 1
        mime = mimetypes.guess_type(path)[0] or "application/octet-stream"
        self._headers(status, mime, length)
        self.send_header("Accept-Ranges", "bytes")
        if status == HTTPStatus.PARTIAL_CONTENT:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        with path.open("rb") as handle:
            handle.seek(start)
            while length > 0:
                chunk = handle.read(min(CHUNK_SIZE, length))
                if not chunk:
                    break
                self.wfile.write(chunk)
                length -= len(chunk)

    def do_POST(self) -> None:
        """Validate CSRF and JSON before persisting a review or requesting Luna.

        Raises:
            TypeError: If a request field has an unsupported type.
            ValueError: If input data or the requested operation is invalid.

        """
        if not self._local_request() or not secrets.compare_digest(
            self.headers.get("X-Review-Token", ""), self.server.csrf
        ):
            self._json(
                HTTPStatus.FORBIDDEN,
                {"error": "Reload the local reviewer before saving."},
            )
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if (
                not 0 < length <= MAX_BODY
                or self.headers.get("Content-Type") != "application/json"
            ):
                raise ValueError("Expected a bounded JSON request")
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict):
                raise TypeError("Expected a JSON object")
            result = {"ok": True}
            if self.path == "/api/review":
                saved = self.server.store.update(payload)
                result["revision"] = saved["revision"]
            elif self.path.startswith("/api/vision/") or self.path == "/api/sequence":
                result.update(self._vision_post(payload))
            elif self.path == "/api/sample":
                result.update(
                    sample_frame(
                        self.server.store,
                        payload.get("match_id"),
                        payload.get("seconds"),
                    )
                )
            elif self.path == "/api/analyze":
                self.server.analyze(payload)
            else:
                raise ValueError("Unknown action")
            self._json(HTTPStatus.OK, result)
        except ConflictError as error:
            self._json(HTTPStatus.CONFLICT, {"error": str(error)})
        except (
            ValueError,
            TypeError,
            KeyError,
            OSError,
            subprocess.SubprocessError,
        ) as error:
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(error)})

    def _vision_post(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Dispatch bounded workflow changes outside frame revisions.

        Raises:
            ValueError: If the operation or input is invalid.

        """
        result: dict[str, Any] = {}
        if self.path == "/api/vision/split":
            vision.assign_split(self.server.store, payload["group"], payload["split"])
        elif self.path == "/api/vision/freeze":
            manifest = vision.freeze(
                self.server.store,
                payload["name"],
                payload.get("profile", "people"),
            )
            result["snapshot"] = manifest["id"]
        elif self.path == "/api/vision/propose":
            self.server.propose(payload)
        elif self.path == "/api/sequence":
            match_id = payload["match_id"]
            seconds = payload.get("seconds")
            start = number(seconds, 0, 86400)
            frames = [
                sample_frame(
                    self.server.store, match_id, max(0, start + offset * 0.08)
                )["frame_id"]
                for offset in range(-4, 5)
            ]
            result["frame_ids"] = list(dict.fromkeys(frames))
        else:
            raise ValueError("Unknown workflow action")
        return result


def parse_range(value: str, size: int) -> tuple[int, int]:
    """Parse a single bounded HTTP byte range, including suffix requests.

    Raises:
        ValueError: If input data or the requested operation is invalid.

    """
    if not value.startswith("bytes=") or "," in value:
        raise ValueError("Only single byte ranges are supported")
    first, separator, last = value[6:].partition("-")
    if not separator or not (first or last):
        raise ValueError("Invalid range")
    if first:
        start = int(first)
        end = min(int(last), size - 1) if last else size - 1
    else:
        suffix = int(last)
        if suffix <= 0:
            raise ValueError("Invalid suffix")
        start, end = max(0, size - suffix), size - 1
    if not 0 <= start <= end < size:
        raise ValueError("Range outside file")
    return start, end
