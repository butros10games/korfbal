"""Celery tasks for the player app."""

from __future__ import annotations

from datetime import datetime, timedelta
import logging
from pathlib import Path
import subprocess  # nosec B404 - required for invoking spotDL CLI safely (shell=False + input validation)
import tempfile
from typing import Any

from celery import shared_task
from django.conf import settings
from django.core.cache import cache
from django.core.files import File
from django.db import transaction
from django.utils import timezone

from apps.awards.models.mvp import MatchMvpVote
from apps.awards.services import mvp as mvp_service
from apps.game_tracker.models import MatchData
from apps.player.models.cached_song import CachedSong, CachedSongStatus
from apps.player.models.player import Player
from apps.player.models.player_song import PlayerSong, PlayerSongStatus
from apps.player.models.push_subscription import PlayerPushSubscription
from apps.player.services.expo_push import ExpoPushPayload, send_expo_push_tokens
from apps.player.services.web_push import WebPushPayload, send_to_model_subscription
from apps.player.spotify import canonicalize_spotify_track_url
from apps.schedule.models.match import Match


logger = logging.getLogger(__name__)


def _cached_song_is_ready(cached: CachedSong) -> bool:
    """Return True when the cached song has a usable audio file."""
    return cached.status == CachedSongStatus.READY and bool(cached.audio_file)


def _cached_song_in_progress_is_not_stale(
    *,
    cached: CachedSong,
    now: datetime,
    stale_in_progress_seconds: int,
) -> bool:
    """Return True if another worker is likely still processing this CachedSong."""
    if cached.status not in {CachedSongStatus.DOWNLOADING, CachedSongStatus.UPLOADING}:
        return False

    age_seconds = (now - cached.updated_at).total_seconds()
    if age_seconds < stale_in_progress_seconds:
        return True

    logger.warning(
        "CachedSong %s appears stuck in %s for %.0fs; reclaiming",
        cached.id_uuid,
        cached.status,
        age_seconds,
    )
    return False


def _lock_cached_song_for_download(
    *,
    cached_song_id: str,
    now: datetime,
    stale_in_progress_seconds: int,
) -> CachedSong | None:
    """Lock and transition a CachedSong into DOWNLOADING, if appropriate.

    Returns the locked CachedSong instance if the caller should proceed with the
    download, else None.
    """
    locked = (
        CachedSong.objects.select_for_update().filter(id_uuid=cached_song_id).first()
    )
    if locked is None:
        return None

    if _cached_song_is_ready(locked):
        return None

    # Reconcile inconsistent states: file exists but status isn't READY.
    if locked.audio_file and locked.status != CachedSongStatus.READY:
        locked.status = CachedSongStatus.READY
        locked.error_message = ""
        locked.save(update_fields=["status", "error_message", "updated_at"])
        return None

    if _cached_song_in_progress_is_not_stale(
        cached=locked,
        now=now,
        stale_in_progress_seconds=stale_in_progress_seconds,
    ):
        return None

    locked.status = CachedSongStatus.DOWNLOADING
    locked.error_message = ""
    locked.save(update_fields=["status", "error_message", "updated_at"])
    return locked


def _participant_players_for_match_data(match_data: MatchData) -> list[Player]:
    return list(
        Player.objects
        .select_related("user")
        .filter(matchplayer__match_data=match_data)
        .distinct()
    )


def _match_title(match: Match) -> str:
    home = getattr(match.home_team, "name", "") or "Thuis"
    away = getattr(match.away_team, "name", "") or "Uit"
    return f"{home} - {away}".strip(" -")


def _push_url_for_match(match: Match) -> str:
    return f"/matches/{match.id_uuid}"


def _send_payload_to_users(*, user_ids: list[int], payload: WebPushPayload) -> None:
    if not user_ids:
        return

    subs = PlayerPushSubscription.objects.filter(
        user_id__in=user_ids,
        is_active=True,
    )

    expo_tokens: list[str] = []

    for sub in subs:
        if sub.platform == "expo":
            expo_tokens.append(sub.endpoint)
        else:
            send_to_model_subscription(sub=sub, payload=payload)

    if expo_tokens:
        send_expo_push_tokens(
            tokens=expo_tokens,
            payload=ExpoPushPayload(
                title=payload.title,
                body=payload.body,
                url=payload.url,
            ),
        )


@shared_task(bind=True)
def handle_match_finished(
    self: Any,  # noqa: ANN401
    *,
    match_id: str,
    match_data_id: str,
) -> None:
    """Entry-point task: send match finished push + schedule MVP tasks."""
    # Best-effort idempotency guard.
    cache_key = f"push:match_finished:{match_data_id}"
    if not cache.add(cache_key, "1", timeout=60 * 60 * 24):
        return

    match = (
        Match.objects
        .select_related(
            "home_team",
            "away_team",
            "home_team__club",
            "away_team__club",
        )
        .filter(id_uuid=match_id)
        .first()
    )
    match_data = (
        MatchData.objects
        .select_related(
            "match_link",
            "match_link__home_team",
            "match_link__away_team",
        )
        .filter(id_uuid=match_data_id)
        .first()
    )
    if match is None or match_data is None:
        return

    if match_data.status != "finished":
        return

    # 1) Notify participants that match finished.
    participants = _participant_players_for_match_data(match_data)
    user_ids: list[int] = []
    for player in participants:
        pk = getattr(getattr(player, "user", None), "pk", None)
        if isinstance(pk, int):
            user_ids.append(pk)

    home = getattr(match.home_team, "name", "") or "Thuis"
    away = getattr(match.away_team, "name", "") or "Uit"
    body = f"{home} {match_data.home_score} - {match_data.away_score} {away}"

    match_payload = WebPushPayload(
        title="Wedstrijd afgelopen",
        body=body,
        url=_push_url_for_match(match),
        tag=f"match-finished:{match_data.id_uuid}",
    )
    _send_payload_to_users(user_ids=user_ids, payload=match_payload)

    # 2) Ensure MVP window exists and schedule reminder + publish.
    try:
        mvp = mvp_service.get_or_create_match_mvp(match, match_data)
    except Exception:
        logger.warning("Failed to ensure MatchMvp for %s", match_id, exc_info=True)
        return

    # Reminder 1 hour before closing.
    reminder_at = mvp.closes_at - timedelta(hours=1)
    if reminder_at > timezone.now():
        send_mvp_vote_reminder.apply_async(
            kwargs={"match_id": match_id}, eta=reminder_at
        )

    # Publish shortly after closing.
    publish_at = mvp.closes_at + timedelta(minutes=1)
    if publish_at > timezone.now():
        publish_mvp_and_notify.apply_async(
            kwargs={"match_id": match_id}, eta=publish_at
        )


@shared_task(bind=True)
def send_mvp_vote_reminder(self: Any, *, match_id: str) -> None:  # noqa: ANN401
    """Send a reminder to participants who haven't voted yet."""
    match = (
        Match.objects
        .select_related("home_team", "away_team")
        .filter(id_uuid=match_id)
        .first()
    )
    match_data = MatchData.objects.filter(match_link_id=match_id).first()
    if match is None or match_data is None:
        return

    try:
        mvp = mvp_service.get_or_create_match_mvp(match, match_data)
    except Exception:
        return

    now = timezone.now()
    if now < (mvp.closes_at - timedelta(hours=1)):
        # Too early (can happen in eager test mode).
        return
    if now >= mvp.closes_at:
        return

    participants = _participant_players_for_match_data(match_data)
    if not participants:
        return

    participant_ids = [p.id_uuid for p in participants]
    voted_ids = set(
        MatchMvpVote.objects.filter(
            match_id=match_id, voter_id__in=participant_ids
        ).values_list("voter_id", flat=True)
    )

    missing_vote_user_ids = [
        int(p.user.pk)
        for p in participants
        if isinstance(getattr(p.user, "pk", None), int) and p.id_uuid not in voted_ids
    ]

    payload = WebPushPayload(
        title="MVP stemmen",
        body=f"Nog 1 uur om te stemmen voor {_match_title(match)}",
        url=_push_url_for_match(match),
        tag=f"mvp-reminder:{match_id}",
    )
    _send_payload_to_users(user_ids=missing_vote_user_ids, payload=payload)


@shared_task(bind=True)
def publish_mvp_and_notify(self: Any, *, match_id: str) -> None:  # noqa: ANN401
    """Publish MVP if possible and notify participants."""
    match = (
        Match.objects
        .select_related("home_team", "away_team")
        .filter(id_uuid=match_id)
        .first()
    )
    match_data = MatchData.objects.filter(match_link_id=match_id).first()
    if match is None or match_data is None:
        return

    before = mvp_service.get_or_create_match_mvp(match, match_data)
    was_published = bool(before.published_at)

    after = mvp_service.ensure_mvp_published(match, match_data)
    if not after.published_at:
        return

    if was_published:
        return

    participants = _participant_players_for_match_data(match_data)
    user_ids: list[int] = []
    for player in participants:
        pk = getattr(getattr(player, "user", None), "pk", None)
        if isinstance(pk, int):
            user_ids.append(pk)

    winner_name = None
    if after.mvp_player is not None:
        winner_name = (
            after.mvp_player.user.get_full_name() or after.mvp_player.user.username
        )

    body = (
        f"MVP voor {_match_title(match)}: {winner_name}"
        if winner_name
        else f"MVP voor {_match_title(match)} is bekend."
    )

    payload = WebPushPayload(
        title="MVP bekend",
        body=body,
        url=_push_url_for_match(match),
        tag=f"mvp-published:{match_id}",
    )
    _send_payload_to_users(user_ids=user_ids, payload=payload)


def _validate_spotify_url_for_spotdl(spotify_url: str) -> None:
    """Validate Spotify URL before passing it to a subprocess.

    We intentionally run the spotDL CLI via subprocess (shell=False) but still
    treat user-provided URLs as untrusted input.

    Accepted formats:
    - https://open.spotify.com/track/<id>
    - https://open.spotify.com/intl-xx/track/<id>

    Raises:
        ValueError: if the URL format is not an accepted Spotify track URL.

    """
    min_printable_ascii = 32

    value = (spotify_url or "").strip()
    if not value:
        raise ValueError("Spotify URL is required")

    # Avoid control characters / whitespace that could create surprises in logs or
    # tooling.
    if any(ch.isspace() for ch in value) or any(
        ord(ch) < min_printable_ascii for ch in value
    ):
        raise ValueError("Spotify URL contains invalid whitespace/control characters")

    # Ensure it is a canonical track URL (also validates structure).
    _ = canonicalize_spotify_track_url(value)


def _pick_downloaded_audio(output_dir: Path) -> Path:
    """Pick the most likely downloaded audio file.

    Raises:
        FileNotFoundError: When spotDL produces no audio file.

    """
    candidates: list[Path] = []
    for pattern in ("*.mp3", "*.m4a", "*.opus", "*.ogg", "*.wav", "*.flac"):
        candidates.extend(output_dir.rglob(pattern))

    if not candidates:
        raise FileNotFoundError("spotDL finished but no audio file was produced")

    # Prefer the largest file (often the actual audio).
    candidates.sort(key=lambda p: p.stat().st_size, reverse=True)
    return candidates[0]


def _redact_spotdl_command(cmd: list[str]) -> str:
    """Return a log-safe command string.

    We invoke spotDL with Spotify client credentials in some environments.
    Never log secrets.
    """
    redacted: list[str] = []
    redact_next = False
    redact_flags = {
        "--client-id",
        "--client-secret",
        "--auth-token",
    }
    for part in cmd:
        if redact_next:
            redacted.append("***")
            redact_next = False
            continue

        redacted.append(part)
        if part in redact_flags:
            redact_next = True

    return " ".join(redacted)


def _spotdl_base_args() -> list[str]:
    """Build base spotDL argv list (without the action/query arguments)."""
    args = ["spotdl"]

    client_id = str(getattr(settings, "SPOTIFY_CLIENT_ID", "") or "").strip()
    client_secret = str(getattr(settings, "SPOTIFY_CLIENT_SECRET", "") or "").strip()

    if client_id and client_secret:
        args.extend(["--client-id", client_id, "--client-secret", client_secret])
    elif client_id or client_secret:
        # Misconfiguration is common and yields confusing failures.
        logger.warning(
            "SPOTIFY_CLIENT_ID/SPOTIFY_CLIENT_SECRET are partially configured; "
            "spotDL may fail. Configure both to use custom Spotify credentials."
        )

    return args


def _run_spotdl(spotify_url: str, output_dir: Path) -> Path:
    """Download a Spotify link using spotDL into the given directory.

    Raises:
        RuntimeError: When spotDL exits non-zero.

    """
    _validate_spotify_url_for_spotdl(spotify_url)
    output_dir.mkdir(parents=True, exist_ok=True)

    output_template = str(output_dir / "{title}.{output-ext}")

    timeout_seconds = int(getattr(settings, "SPOTDL_DOWNLOAD_TIMEOUT_SECONDS", 60 * 15))

    attempted: list[tuple[list[str], str]] = []

    base = _spotdl_base_args()
    commands: list[list[str]] = [
        [
            *base,
            "download",
            spotify_url,
            "--output",
            output_template,
            "--format",
            "mp3",
            "--threads",
            "1",
        ],
        [
            *base,
            spotify_url,
            "--output",
            output_template,
            "--format",
            "mp3",
            "--threads",
            "1",
        ],
    ]

    last_error = ""
    for cmd in commands:
        logger.info("Running spotDL: %s", _redact_spotdl_command(cmd))
        try:
            proc = subprocess.run(  # nosec B603 - shell=False with validated URL; command is fixed argv list
                cmd,
                check=False,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                shell=False,
            )
        except subprocess.TimeoutExpired as exc:
            # spotDL can occasionally finish the file but hang on cleanup/metadata.
            # If a plausible audio file exists, accept it.
            try:
                downloaded = _pick_downloaded_audio(output_dir)
                logger.warning(
                    "spotDL timed out after %s seconds but produced %s; accepting file",
                    timeout_seconds,
                    downloaded,
                )
                return downloaded
            except FileNotFoundError:
                attempted.append((cmd, "TIMEOUT"))
                last_error = (
                    f"Download timed out after {timeout_seconds} seconds. Please retry."
                )
                logger.warning(
                    "spotDL timed out after %s seconds (no output file found)",
                    timeout_seconds,
                    exc_info=exc,
                )
                continue

        attempted.append((cmd, (proc.stdout or "")[-2000:]))
        if proc.returncode == 0:
            return _pick_downloaded_audio(output_dir)
        last_error = (proc.stderr or proc.stdout or "").strip()[-4000:]

    # Log full details for debugging, but keep the raised message user-friendly.
    redacted_details = "\n".join(_redact_spotdl_command(cmd) for cmd, _ in attempted)
    logger.warning(
        "spotDL failed. Tried:\n%s\nLast error:\n%s",
        redacted_details,
        last_error,
    )
    if "timed out" in last_error.lower():
        raise RuntimeError(last_error)
    raise RuntimeError("Download failed. Please retry.")


@shared_task(bind=True)
def download_cached_song(self: Any, cached_song_id: str) -> None:  # noqa: ANN401
    """Download a cached song (from a Spotify URL) and upload it to storage."""
    cached = CachedSong.objects.filter(id_uuid=cached_song_id).first()
    if cached is None:
        logger.warning("CachedSong %s not found", cached_song_id)
        return

    timeout_seconds = int(getattr(settings, "SPOTDL_DOWNLOAD_TIMEOUT_SECONDS", 60 * 15))
    stale_in_progress_seconds = int(
        getattr(
            settings,
            "SPOTDL_STALE_IN_PROGRESS_SECONDS",
            timeout_seconds + 60,
        )
    )

    now = timezone.now()

    if _cached_song_is_ready(cached):
        return

    # Avoid duplicate work if another worker is already processing it.
    if _cached_song_in_progress_is_not_stale(
        cached=cached,
        now=now,
        stale_in_progress_seconds=stale_in_progress_seconds,
    ):
        return

    try:
        with transaction.atomic():
            locked = _lock_cached_song_for_download(
                cached_song_id=cached_song_id,
                now=now,
                stale_in_progress_seconds=stale_in_progress_seconds,
            )
            if locked is None:
                return

            # Use the locked instance for the remainder of the task.
            cached = locked

        with tempfile.TemporaryDirectory(prefix="spotdl_") as tmp:
            output_dir = Path(tmp)
            if getattr(settings, "TESTING", False):
                downloaded = output_dir / "dummy.mp3"
                downloaded.write_bytes(b"ID3")
            else:
                downloaded = _run_spotdl(cached.spotify_url, output_dir)

            with transaction.atomic():
                cached.status = CachedSongStatus.UPLOADING
                cached.save(update_fields=["status", "updated_at"])

            suffix = downloaded.suffix or ".mp3"
            target_name = f"{cached.id_uuid}{suffix}"
            with downloaded.open("rb") as handle:
                cached.audio_file.save(target_name, File(handle), save=False)

            with transaction.atomic():
                cached.status = CachedSongStatus.READY
                cached.error_message = ""
                cached.save(
                    update_fields=[
                        "status",
                        "error_message",
                        "audio_file",
                        "updated_at",
                    ]
                )

    except Exception as exc:
        logger.exception("Failed to download CachedSong %s", cached_song_id)
        cached.status = CachedSongStatus.FAILED
        cached.error_message = str(exc)
        cached.save(update_fields=["status", "error_message", "updated_at"])
        raise


@shared_task(bind=True)
def download_player_song(self: Any, song_id: str) -> None:  # noqa: ANN401
    """Backward compatible wrapper.

    Historically we queued downloads per PlayerSong. Now PlayerSong can point to a
    shared CachedSong. This wrapper resolves the cached song and queues it.
    """
    song = (
        PlayerSong.objects.select_related("cached_song").filter(id_uuid=song_id).first()
    )
    if song is None:
        logger.warning("PlayerSong %s not found", song_id)
        return

    if song.cached_song is not None:
        download_cached_song.apply(args=[str(song.cached_song.id_uuid)])
        return

    # Legacy: if this PlayerSong still stores its own audio, keep the old behavior.
    if song.status == PlayerSongStatus.READY and song.audio_file:
        return

    try:
        with transaction.atomic():
            song.status = PlayerSongStatus.DOWNLOADING
            song.error_message = ""
            song.save(update_fields=["status", "error_message", "updated_at"])

        with tempfile.TemporaryDirectory(prefix="spotdl_") as tmp:
            output_dir = Path(tmp)
            if getattr(settings, "TESTING", False):
                downloaded = output_dir / "dummy.mp3"
                downloaded.write_bytes(b"ID3")
            else:
                downloaded = _run_spotdl(song.spotify_url, output_dir)

            with transaction.atomic():
                song.status = PlayerSongStatus.UPLOADING
                song.save(update_fields=["status", "updated_at"])

            suffix = downloaded.suffix or ".mp3"
            target_name = f"{song.player.id_uuid}/{song.id_uuid}{suffix}"
            with downloaded.open("rb") as handle:
                song.audio_file.save(target_name, File(handle), save=False)

            with transaction.atomic():
                song.status = PlayerSongStatus.READY
                song.error_message = ""
                song.save(
                    update_fields=[
                        "status",
                        "error_message",
                        "audio_file",
                        "updated_at",
                    ]
                )

    except Exception as exc:
        logger.exception("Failed to download PlayerSong %s", song_id)
        song.status = PlayerSongStatus.FAILED
        song.error_message = str(exc)
        song.save(update_fields=["status", "error_message", "updated_at"])
        raise
