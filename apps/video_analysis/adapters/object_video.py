"""Bounded multipart intake and seekable object reads without local recordings."""

from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager, suppress
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import subprocess
from threading import Thread
import uuid

from botocore.client import BaseClient

from apps.video_analysis.engine.media import binary
from apps.video_analysis.engine.server import parse_range


PART_BYTES = 8 * 1024**2
MAX_BYTES = 6_000_000_000
MAX_WIDTH = 7680
MAX_HEIGHT = 4320


@contextmanager
def object_url(size: int, read: Callable[[int, int], Iterator[bytes]]) -> Iterator[str]:
    """Expose one private object to local media tools; never expose S3 credentials.

    Yields:
        A loopback-only, unguessable URL supporting seeks through bounded S3 ranges.

    """
    route = "/" + uuid.uuid4().hex

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args: object, **kwargs: object) -> None:
            pass

        def do_HEAD(self) -> None:
            self.serve(head=True)

        def do_GET(self) -> None:
            self.serve(head=False)

        def serve(self, *, head: bool) -> None:
            if self.path != route:
                self.send_error(404)
                return
            start, end = 0, size - 1
            selected = self.headers.get("Range")
            if selected:
                try:
                    start, end = parse_range(selected, size)
                except ValueError:
                    self.send_error(416)
                    return
            self.send_response(206 if selected else 200)
            self.send_header("Content-Length", str(end - start + 1))
            self.send_header("Accept-Ranges", "bytes")
            if selected:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.end_headers()
            if not head:
                send_ranges(self, read, start, end)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
    )
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}{route}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def probe_object(
    client: BaseClient, bucket: str, key: str, size: int, suffix: str
) -> dict:
    """Probe only MP4/WebM containers through an authenticated range reader.

    Raises:
        ValueError: The signature, duration or dimensions are unsupported.

    """

    def read(start: int, end: int) -> Iterator[bytes]:
        response = client.get_object(
            Bucket=bucket, Key=key, Range=f"bytes={start}-{end}"
        )
        with response["Body"] as body:
            yield from iter(lambda: body.read(64 * 1024), b"")

    signature = b"".join(read(0, min(15, size - 1)))
    if not (
        (suffix == ".mp4" and signature[4:8] == b"ftyp")
        or (suffix == ".webm" and signature[:4] == b"\x1aE\xdf\xa3")
    ):
        raise ValueError("Invalid video container signature")
    with object_url(size, read) as url:
        result = subprocess.run(
            [
                binary("ffprobe"),
                "-v",
                "error",
                "-protocol_whitelist",
                "http,tcp",
                "-f",
                "mov" if suffix == ".mp4" else "matroska",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=width,height,avg_frame_rate:format=duration",
                "-of",
                "json",
                url,
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=60,
        )
    raw = json.loads(result.stdout)
    stream = raw["streams"][0]
    duration = float(raw["format"]["duration"])
    if not (
        0 < duration <= 8 * 3600
        and 0 < stream["width"] <= MAX_WIDTH
        and 0 < stream["height"] <= MAX_HEIGHT
    ):
        raise ValueError("Choose a playable video up to eight hours and 8K")
    return {
        "duration_seconds": duration,
        "width": stream["width"],
        "height": stream["height"],
        "fps": stream["avg_frame_rate"],
    }


def upload_video(
    client: BaseClient, bucket: str, key: str, chunks: Iterable[bytes], suffix: str
) -> tuple[int, str, dict]:
    """Abort partial uploads and reject invalid media using bounded memory.

    Raises:
        ValueError: The recording is empty, too large, corrupt or unsupported.

    """
    upload = client.create_multipart_upload(
        Bucket=bucket,
        Key=key,
        ContentType="video/mp4" if suffix == ".mp4" else "video/webm",
    )
    upload_id = upload["UploadId"]
    parts, pending = [], bytearray()
    digest, size, completed = hashlib.sha256(), 0, False
    try:

        def send() -> None:
            response = client.upload_part(
                Bucket=bucket,
                Key=key,
                UploadId=upload_id,
                PartNumber=len(parts) + 1,
                Body=bytes(pending),
            )
            parts.append({"PartNumber": len(parts) + 1, "ETag": response["ETag"]})
            pending.clear()

        for chunk in chunks:
            size += len(chunk)
            if size > MAX_BYTES:
                raise ValueError("Recording exceeds the 6 GB limit")
            digest.update(chunk)
            for offset in range(0, len(chunk), PART_BYTES):
                block = memoryview(chunk)[offset : offset + PART_BYTES]
                take = min(PART_BYTES - len(pending), len(block))
                pending.extend(block[:take])
                if len(pending) == PART_BYTES:
                    send()
                pending.extend(block[take:])
        if not size:
            raise ValueError("Recording is empty")
        if pending:
            send()
        client.complete_multipart_upload(
            Bucket=bucket, Key=key, UploadId=upload_id, MultipartUpload={"Parts": parts}
        )
        completed = True
        verify_stream(client, bucket, key, size, digest.hexdigest())
        metadata = probe_object(client, bucket, key, size, suffix)
        return size, digest.hexdigest(), metadata
    except BaseException:
        if completed:
            client.delete_object(Bucket=bucket, Key=key)
        else:
            client.abort_multipart_upload(Bucket=bucket, Key=key, UploadId=upload_id)
        raise


def verify_stream(
    client: BaseClient, bucket: str, key: str, size: int, checksum: str
) -> None:
    """Verify durable bytes; multipart ETags are not content hashes.

    Raises:
        ValueError: Stored bytes differ from the source stream.

    """
    response = client.get_object(Bucket=bucket, Key=key)
    verified, count = hashlib.sha256(), 0
    with response["Body"] as body:
        for chunk in iter(lambda: body.read(1024**2), b""):
            count += len(chunk)
            if count > size:
                raise ValueError("Object storage verification failed")
            verified.update(chunk)
    if count != size or verified.hexdigest() != checksum:
        raise ValueError("Object storage verification failed")


def send_ranges(
    handler: BaseHTTPRequestHandler,
    read: Callable[[int, int], Iterator[bytes]],
    start: int,
    end: int,
) -> None:
    """Fetch small ranges so a decoder seek never eagerly downloads a whole video."""
    with suppress(BrokenPipeError, ConnectionResetError):
        for offset in range(start, end + 1, 1024**2):
            for block in read(offset, min(offset + 1024**2 - 1, end)):
                handler.wfile.write(block)
