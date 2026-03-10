from __future__ import annotations

import json
import re
import shutil
import sys
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
    hint = install_hint_for_binary(name)
    if hint:
        raise YouTubeError(f"Required binary not found: {name}\n\n{hint}")
    raise YouTubeError(f"Required binary not found: {name}")


def install_hint_for_binary(name: str, platform: str | None = None) -> str:
    if name != "mpv":
        return ""

    platform = sys.platform if platform is None else platform

    if platform == "darwin":
        return (
            "Install mpv with Homebrew:\n"
            "  brew install mpv\n\n"
            "More options: https://mpv.io/installation"
        )

    if platform.startswith("linux"):
        return (
            "Install mpv with your package manager:\n"
            "  Debian/Ubuntu: sudo apt install mpv\n"
            "  Fedora: sudo dnf install mpv\n"
            "  Arch: sudo pacman -S mpv\n\n"
            "More options: https://mpv.io/installation"
        )

    if platform == "win32":
        return (
            "Install mpv with WinGet:\n"
            "  winget search mpv\n"
            "  winget install <mpv-package-id>\n\n"
            "More options: https://mpv.io/installation"
        )

    return "Install mpv from https://mpv.io/installation"


class YouTubeClient:
    def __init__(self, timeout_seconds: int = 45) -> None:
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

        payload = self._extract_info(f"ytsearch{limit}:{query}", flat=True, playlist_end=limit)
        tracks = _parse_tracks(payload.get("entries"), source="search")
        if tracks:
            self._search_cache[cache_key] = tracks
            return tracks
        raise YouTubeError("No search results returned from YouTube.")

    def fetch_mix(self, track: Track, limit: int = DEFAULT_MIX_LIMIT) -> list[Track]:
        try:
            tracks = self._fetch_related_fast(track, limit)
        except YouTubeError:
            tracks = []
        if tracks:
            return tracks

        payload = self._extract_info(mix_url_for(track.video_id), flat=True, playlist_end=limit + 1)
        tracks = _parse_tracks(
            payload.get("entries"),
            source="mix",
            exclude_video_ids={track.video_id},
            limit=limit,
        )
        if tracks:
            return tracks
        raise YouTubeError("No similar tracks returned from YouTube mix.")

    def resolve_stream_url(self, track: Track) -> str:
        payload = self._extract_info(track.watch_url, format_selector="ba/b", no_playlist=True)
        for candidate in _stream_url_candidates(payload):
            if candidate.startswith(("http://", "https://")):
                return candidate
        raise YouTubeError("Could not resolve an audio stream URL.")

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

    def _extract_info(
        self,
        target: str,
        *,
        flat: bool = False,
        format_selector: str | None = None,
        no_playlist: bool = False,
        playlist_end: int | None = None,
    ) -> dict[str, Any]:
        YoutubeDL, DownloadError = _load_yt_dlp()
        options: dict[str, Any] = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "socket_timeout": self.timeout_seconds,
        }
        if flat:
            options["extract_flat"] = True
        if format_selector:
            options["format"] = format_selector
        if no_playlist:
            options["noplaylist"] = True
        if playlist_end is not None:
            options["playlistend"] = playlist_end

        try:
            with YoutubeDL(options) as ydl:
                payload = ydl.extract_info(target, download=False)
        except DownloadError as exc:
            raise YouTubeError(_clean_yt_dlp_error(str(exc), fallback="yt-dlp failed.")) from exc
        except OSError as exc:
            raise YouTubeError(f"yt-dlp failed: {exc}") from exc

        if isinstance(payload, dict):
            return payload
        raise YouTubeError("yt-dlp returned invalid data.")


def mix_url_for(video_id: str) -> str:
    return f"https://www.youtube.com/watch?v={video_id}&list=RD{video_id}"


def search_url_for(query: str) -> str:
    encoded = urllib.parse.quote_plus(query)
    return f"https://www.youtube.com/results?search_query={encoded}&sp=EgIQAQ%253D%253D"


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


def _load_yt_dlp() -> tuple[type[Any], type[Exception]]:
    try:
        from yt_dlp import YoutubeDL
        from yt_dlp.utils import DownloadError
    except ModuleNotFoundError as exc:
        raise YouTubeError("Python package 'yt-dlp' is not installed. Reinstall simpleplay with pip.") from exc
    return YoutubeDL, DownloadError


def _stream_url_candidates(payload: dict[str, Any]) -> Sequence[str]:
    candidates: list[str] = []

    requested_formats = payload.get("requested_formats")
    if isinstance(requested_formats, list):
        for item in requested_formats:
            if not isinstance(item, dict):
                continue
            url = str(item.get("url") or "").strip()
            if url:
                candidates.append(url)

    requested_downloads = payload.get("requested_downloads")
    if isinstance(requested_downloads, list):
        for item in requested_downloads:
            if not isinstance(item, dict):
                continue
            url = str(item.get("url") or "").strip()
            if url:
                candidates.append(url)

    for key in ("url", "manifest_url"):
        url = str(payload.get(key) or "").strip()
        if url:
            candidates.append(url)

    return candidates


def _clean_yt_dlp_error(value: str, *, fallback: str) -> str:
    lines = [line.strip() for line in value.splitlines() if line.strip()]
    if not lines:
        return fallback
    return lines[-1]


def _normalize_query(value: str) -> str:
    return " ".join(value.strip().lower().split())
