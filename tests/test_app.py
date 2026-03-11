from __future__ import annotations

import unittest

from simpleplay.app import SimplePlayApp
from simpleplay.models import StreamCacheEntry, Track


class SimplePlayAppTests(unittest.TestCase):
    def test_starts_out_of_search_mode_without_initial_query(self) -> None:
        app = SimplePlayApp()
        self.assertFalse(app.search_mode)

    def test_starts_out_of_search_mode_with_initial_query(self) -> None:
        app = SimplePlayApp(initial_query="daft punk")
        self.assertFalse(app.search_mode)

    def test_slash_starts_fresh_search(self) -> None:
        app = SimplePlayApp()
        app.search_query = "old query"

        app._handle_key(ord("/"))

        self.assertTrue(app.search_mode)
        self.assertEqual(app.search_query, "")

    def test_typing_in_search_mode_schedules_live_search(self) -> None:
        app = SimplePlayApp()
        app.search_mode = True

        app._handle_search_key(ord("d"))

        self.assertEqual(app.search_query, "d")
        self.assertIsNotNone(app.search_debounce_deadline)

    def test_live_search_starts_when_debounce_expires(self) -> None:
        app = SimplePlayApp()
        started: list[str] = []
        app.search_mode = True
        app.search_query = "daft punk"
        app.search_debounce_deadline = 0.0
        app._start_search = lambda query: started.append(query)  # type: ignore[method-assign]

        app._maybe_start_live_search()

        self.assertEqual(started, ["daft punk"])

    def test_enter_skips_duplicate_search_when_results_are_current(self) -> None:
        app = SimplePlayApp()
        started: list[str] = []
        app.search_mode = True
        app.search_query = "daft punk"
        app.last_completed_search_query = "daft punk"
        app._start_search = lambda query: started.append(query)  # type: ignore[method-assign]

        app._handle_search_key(10)

        self.assertFalse(app.search_mode)
        self.assertEqual(started, [])

    def test_load_track_starts_pending_playback_before_watch_url_fallback(self) -> None:
        app = SimplePlayApp()
        track = Track(video_id="abc123", title="Song")
        resolved: list[str] = []

        app._start_stream_resolve = lambda current: resolved.append(current.video_id)  # type: ignore[method-assign]

        app._load_track(track)

        self.assertEqual(resolved, ["abc123"])
        self.assertEqual(app.pending_play_video_id, "abc123")
        self.assertEqual(app.status_message, "Loading: Song")

    def test_pending_playback_uses_cached_stream_when_ready(self) -> None:
        app = SimplePlayApp()
        track = Track(video_id="abc123", title="Song")
        loads: list[tuple[str, str | None]] = []

        app.current_track = track
        app.pending_play_video_id = track.video_id
        app.stream_cache[track.video_id] = StreamCacheEntry(url="https://example.com/audio")
        app.player.load = lambda url, media_title=None: loads.append((url, media_title))  # type: ignore[method-assign]
        app._sync_mpv_playlist = lambda: None  # type: ignore[method-assign]

        app._maybe_start_pending_playback()

        self.assertEqual(loads, [("https://example.com/audio", "Song")])
        self.assertIsNone(app.pending_play_video_id)
        self.assertEqual(app.status_message, "Playing: Song")

    def test_pending_playback_falls_back_to_watch_url_after_grace_period(self) -> None:
        app = SimplePlayApp()
        track = Track(video_id="abc123", title="Song")
        loads: list[tuple[str, str | None]] = []

        app.current_track = track
        app.pending_play_video_id = track.video_id
        app.pending_play_fallback_at = 0.0
        app.player.load = lambda url, media_title=None: loads.append((url, media_title))  # type: ignore[method-assign]
        app._sync_mpv_playlist = lambda: None  # type: ignore[method-assign]

        app._maybe_start_pending_playback()

        self.assertEqual(loads, [(track.watch_url, "Song")])
        self.assertIsNone(app.pending_play_video_id)
        self.assertEqual(app.status_message, "Playing: Song")

    def test_play_selected_seeds_queue_from_search_results(self) -> None:
        app = SimplePlayApp()
        first = Track(video_id="one", title="One")
        second = Track(video_id="two", title="Two")
        third = Track(video_id="three", title="Three")

        app.list_mode = "search"
        app.results = [first, second, third]
        app.selected_index = 1
        app._load_track = lambda track: None  # type: ignore[method-assign]
        app._start_related_prefetch = lambda track: None  # type: ignore[method-assign]
        app._fill_queue_from_related = lambda track: None  # type: ignore[method-assign]
        app._warm_queue_streams = lambda: None  # type: ignore[method-assign]

        app._play_selected()

        self.assertEqual(app.current_track.video_id, "two")
        self.assertEqual(list(app.up_next), [first, third])
        self.assertEqual(app.results, [first, third])

    def test_related_error_does_not_override_current_playback_status(self) -> None:
        app = SimplePlayApp()
        track = Track(video_id="abc123", title="Song")

        app.current_track = track
        app.status_message = "Playing: Song"
        app.pending_related.add(track.video_id)

        app._process_event(
            {
                "type": "related-error",
                "video_id": track.video_id,
                "message": "[ERROR] video not found",
            }
        )

        self.assertEqual(app.status_message, "Playing: Song")
        self.assertNotIn(track.video_id, app.pending_related)
