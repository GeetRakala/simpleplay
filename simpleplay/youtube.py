from __future__ import annotations

import json
import shutil
import subprocess
from typing import Any, Iterable, Sequence

from .models import Track


DEFAULT_SEARCH_LIMIT = 12
DEFAULT_MIX_LIMIT = 25


class YouTubeError(RuntimeError):
    pass


def require_binary(name: str) -> None:
    if shutil.which(name):
        return
    raise YouTubeError(f"Required binary not found: {name}")


class YouTubeClient:
    def __init__(self, yt_dlp_bin: str = "yt-dlp", timeout_seconds: int = 45) -> None:
        self.yt_dlp_bin = yt_dlp_bin
        self.timeout_seconds = timeout_seconds

    def search(self, query: str, limit: int = DEFAULT_SEARCH_LIMIT) -> list[Track]:
        require_binary(self.yt_dlp_bin)
        process = self._run(
            [
                self.yt_dlp_bin,
                "--ignore-config",
                "--quiet",
                "--dump-single-json",
                "--flat-playlist",
                f"ytsearch{limit}:{query}",
            ]
        )
        payload = _parse_json(process.stdout)
        tracks = _parse_tracks(payload.get("entries"), source="search")
        if tracks:
            return tracks
        raise YouTubeError(_best_error(process, fallback="No search results returned from YouTube."))

    def fetch_mix(self, track: Track, limit: int = DEFAULT_MIX_LIMIT) -> list[Track]:
        require_binary(self.yt_dlp_bin)
        process = self._run(
            [
                self.yt_dlp_bin,
                "--ignore-config",
                "--quiet",
                "--dump-single-json",
                "--flat-playlist",
                "--playlist-end",
                str(limit + 1),
                mix_url_for(track.video_id),
            ]
        )
        payload = _parse_json(process.stdout)
        tracks = _parse_tracks(
            payload.get("entries"),
            source="mix",
            exclude_video_ids={track.video_id},
            limit=limit,
        )
        if tracks:
            return tracks
        raise YouTubeError(_best_error(process, fallback="No similar tracks returned from YouTube mix."))

    def resolve_stream_url(self, track: Track) -> str:
        require_binary(self.yt_dlp_bin)
        process = self._run(
            [
                self.yt_dlp_bin,
                "--ignore-config",
                "--quiet",
                "--no-warnings",
                "--no-playlist",
                "-f",
                "ba/b",
                "--get-url",
                track.watch_url,
            ]
        )

        for line in process.stdout.splitlines():
            value = line.strip()
            if value.startswith("http://") or value.startswith("https://"):
                return value

        raise YouTubeError(_best_error(process, fallback="Could not resolve an audio stream URL."))

    def _run(self, args: Sequence[str]) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(
                list(args),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.timeout_seconds,
                check=False,
            )
        except FileNotFoundError as exc:
            raise YouTubeError(str(exc)) from exc
        except subprocess.TimeoutExpired as exc:
            raise YouTubeError("yt-dlp command timed out.") from exc


def mix_url_for(video_id: str) -> str:
    return f"https://www.youtube.com/watch?v={video_id}&list=RD{video_id}"


def _parse_json(output: str) -> dict[str, Any]:
    text = output.strip()
    if not text:
        return {}
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise YouTubeError("yt-dlp returned invalid JSON output.") from exc


def _parse_tracks(
    entries: Iterable[dict[str, Any] | None] | None,
    *,
    source: str,
    exclude_video_ids: set[str] | None = None,
    limit: int | None = None,
) -> list[Track]:
    tracks: list[Track] = []
    seen = set(exclude_video_ids or set())

    for entry in entries or []:
        if not entry:
            continue

        video_id = str(entry.get("id") or "").strip()
        title = str(entry.get("title") or "").strip()
        if not video_id or not title or video_id in seen:
            continue

        channel = str(entry.get("channel") or entry.get("uploader") or "").strip()
        duration = _coerce_duration(entry.get("duration"))
        watch_url = str(entry.get("url") or "").strip()
        if not watch_url.startswith("http"):
            watch_url = f"https://www.youtube.com/watch?v={video_id}"

        tracks.append(
            Track(
                video_id=video_id,
                title=title,
                channel=channel,
                duration=duration,
                watch_url=watch_url,
                source=source,
            )
        )
        seen.add(video_id)

        if limit is not None and len(tracks) >= limit:
            break

    return tracks


def _coerce_duration(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _best_error(process: subprocess.CompletedProcess[str], *, fallback: str) -> str:
    stderr = process.stderr.strip()
    stdout = process.stdout.strip()

    for source in (stderr, stdout):
        if source:
            lines = [line.strip() for line in source.splitlines() if line.strip()]
            if lines:
                return lines[-1]
    return fallback
