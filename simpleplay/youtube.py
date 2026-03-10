from __future__ import annotations

import json
import re
import shutil
import subprocess
import urllib.parse
import urllib.request
from typing import Any, Iterable, Sequence

from .models import Track


DEFAULT_SEARCH_LIMIT = 12
DEFAULT_MIX_LIMIT = 25
YOUTUBE_HEADERS = {
    "Accept-Language": "en-US,en;q=0.9",
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/123.0.0.0 Safari/537.36"
    ),
}
INITIAL_DATA_MARKERS = (
    "var ytInitialData = ",
    'window["ytInitialData"] = ',
    "ytInitialData = ",
)


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
        self._search_cache: dict[tuple[str, int], list[Track]] = {}

    def search(self, query: str, limit: int = DEFAULT_SEARCH_LIMIT) -> list[Track]:
        normalized = _normalize_query(query)
        cache_key = (normalized, limit)
        cached = self._search_cache.get(cache_key)
        if cached is not None:
            return list(cached)

        try:
            tracks = self._search_fast(query, limit)
        except YouTubeError:
            tracks = []
        if tracks:
            self._search_cache[cache_key] = tracks
            return list(tracks)

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
            self._search_cache[cache_key] = tracks
            return tracks
        raise YouTubeError(_best_error(process, fallback="No search results returned from YouTube."))

    def fetch_mix(self, track: Track, limit: int = DEFAULT_MIX_LIMIT) -> list[Track]:
        try:
            tracks = self._fetch_related_fast(track, limit)
        except YouTubeError:
            tracks = []
        if tracks:
            return tracks

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

    def _search_fast(self, query: str, limit: int) -> list[Track]:
        html = self._fetch_html(search_url_for(query))
        payload = _extract_initial_data(html)
        return _parse_renderer_tracks(payload, source="search", limit=limit)

    def _fetch_related_fast(self, track: Track, limit: int) -> list[Track]:
        html = self._fetch_html(track.watch_url)
        payload = _extract_initial_data(html)
        return _parse_renderer_tracks(
            payload,
            source="mix",
            exclude_video_ids={track.video_id},
            limit=limit,
        )

    def _fetch_html(self, url: str) -> str:
        request = urllib.request.Request(url, headers=YOUTUBE_HEADERS)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                return response.read().decode("utf-8", errors="replace")
        except OSError as exc:
            raise YouTubeError(f"Could not fetch YouTube page: {exc}") from exc


def mix_url_for(video_id: str) -> str:
    return f"https://www.youtube.com/watch?v={video_id}&list=RD{video_id}"


def search_url_for(query: str) -> str:
    encoded = urllib.parse.quote_plus(query)
    return f"https://www.youtube.com/results?search_query={encoded}&sp=EgIQAQ%253D%253D"


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


def _parse_renderer_tracks(
    payload: dict[str, Any],
    *,
    source: str,
    exclude_video_ids: set[str] | None = None,
    limit: int | None = None,
) -> list[Track]:
    tracks: list[Track] = []
    seen = set(exclude_video_ids or set())

    for item in _walk(payload):
        renderer = None
        for key in ("videoRenderer", "compactVideoRenderer", "playlistPanelVideoRenderer"):
            candidate = item.get(key)
            if isinstance(candidate, dict):
                renderer = candidate
                break
        if not renderer:
            continue

        track = _track_from_renderer(renderer, source=source)
        if not track or track.video_id in seen:
            continue

        tracks.append(track)
        seen.add(track.video_id)

        if limit is not None and len(tracks) >= limit:
            break

    return tracks


def _track_from_renderer(renderer: dict[str, Any], *, source: str) -> Track | None:
    video_id = str(renderer.get("videoId") or "").strip()
    title = _text_from_runs(renderer.get("title"))
    if not video_id or not title:
        return None

    channel = (
        _text_from_runs(renderer.get("shortBylineText"))
        or _text_from_runs(renderer.get("longBylineText"))
        or _text_from_runs(renderer.get("ownerText"))
    )
    duration = _duration_from_renderer(renderer)
    watch_url = _watch_url_from_renderer(renderer, video_id)
    return Track(
        video_id=video_id,
        title=title,
        channel=channel,
        duration=duration,
        watch_url=watch_url,
        source=source,
    )


def _watch_url_from_renderer(renderer: dict[str, Any], video_id: str) -> str:
    endpoint = renderer.get("navigationEndpoint")
    if isinstance(endpoint, dict):
        metadata = endpoint.get("commandMetadata")
        if isinstance(metadata, dict):
            web = metadata.get("webCommandMetadata")
            if isinstance(web, dict):
                value = str(web.get("url") or "").strip()
                if value.startswith("/"):
                    return f"https://www.youtube.com{value}"
                if value.startswith("http"):
                    return value
    return f"https://www.youtube.com/watch?v={video_id}"


def _duration_from_renderer(renderer: dict[str, Any]) -> int | None:
    raw = _text_from_runs(renderer.get("lengthText"))
    if raw:
        return _duration_from_text(raw)

    overlays = renderer.get("thumbnailOverlays")
    if isinstance(overlays, list):
        for item in overlays:
            if not isinstance(item, dict):
                continue
            overlay = item.get("thumbnailOverlayTimeStatusRenderer")
            if not isinstance(overlay, dict):
                continue
            raw = _text_from_runs(overlay.get("text"))
            if raw:
                return _duration_from_text(raw)
    return None


def _duration_from_text(value: str) -> int | None:
    parts = [part for part in re.split(r"[:.]", value.strip()) if part.isdigit()]
    if not parts:
        return None
    try:
        numbers = [int(part) for part in parts]
    except ValueError:
        return None
    total = 0
    for number in numbers:
        total = (total * 60) + number
    return total


def _text_from_runs(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if not isinstance(value, dict):
        return ""
    simple = value.get("simpleText")
    if isinstance(simple, str):
        return simple.strip()
    runs = value.get("runs")
    if not isinstance(runs, list):
        return ""
    parts: list[str] = []
    for item in runs:
        if not isinstance(item, dict):
            continue
        text = item.get("text")
        if isinstance(text, str):
            parts.append(text)
    return "".join(parts).strip()


def _extract_initial_data(html: str) -> dict[str, Any]:
    for marker in INITIAL_DATA_MARKERS:
        marker_index = html.find(marker)
        if marker_index < 0:
            continue
        start_index = html.find("{", marker_index + len(marker))
        if start_index < 0:
            continue
        blob = _extract_balanced_object(html, start_index)
        if not blob:
            continue
        try:
            payload = json.loads(blob)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            return payload
    raise YouTubeError("Could not parse YouTube page data.")


def _extract_balanced_object(text: str, start_index: int) -> str:
    depth = 0
    in_string = False
    escaped = False

    for index in range(start_index, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue

        if char == '"':
            in_string = True
            continue
        if char == "{":
            depth += 1
            continue
        if char != "}":
            continue
        depth -= 1
        if depth == 0:
            return text[start_index : index + 1]
    return ""


def _walk(value: Any) -> Iterable[dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk(child)
        return
    if isinstance(value, list):
        for item in value:
            yield from _walk(item)


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


def _normalize_query(value: str) -> str:
    return " ".join(value.strip().lower().split())
