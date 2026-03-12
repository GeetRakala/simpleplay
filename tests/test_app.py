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

    def test_uppercase_h_lowers_volume(self) -> None:
        app = SimplePlayApp()
        changes: list[int] = []

        app.player.change_volume = lambda delta: changes.append(delta)  # type: ignore[method-assign]

        app._handle_key(ord("H"))

        self.assertEqual(changes, [-5])
        self.assertEqual(app.volume, 95.0)
        self.assertEqual(app.status_message, "Volume: 95%")

    def test_uppercase_l_raises_volume(self) -> None:
        app = SimplePlayApp()
        changes: list[int] = []

        app.player.change_volume = lambda delta: changes.append(delta)  # type: ignore[method-assign]

        app._handle_key(ord("L"))

        self.assertEqual(changes, [5])
        self.assertEqual(app.volume, 105.0)
        self.assertEqual(app.status_message, "Volume: 105%")

    def test_load_track_starts_pending_playback_before_stream_resolve(self) -> None:
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
        synced: list[bool] = []

        app.current_track = track
        app.pending_play_video_id = track.video_id
        app.stream_cache[track.video_id] = StreamCacheEntry(url="https://example.com/audio")
        app.player.load = lambda url, media_title=None: loads.append((url, media_title))  # type: ignore[method-assign]
        app._sync_mpv_playlist = lambda: synced.append(True)  # type: ignore[method-assign]

        app._maybe_start_pending_playback()

        self.assertEqual(loads, [("https://example.com/audio", "Song")])
        self.assertEqual(synced, [True])
        self.assertIsNone(app.pending_play_video_id)
        self.assertEqual(app.status_message, "Playing: Song")

    def test_pending_playback_waits_for_resolved_stream_url(self) -> None:
        app = SimplePlayApp()
        track = Track(video_id="abc123", title="Song")
        loads: list[tuple[str, str | None]] = []

        app.current_track = track
        app.pending_play_video_id = track.video_id
        app.status_message = "Loading: Song"
        app.player.load = lambda url, media_title=None: loads.append((url, media_title))  # type: ignore[method-assign]

        app._maybe_start_pending_playback()

        self.assertEqual(loads, [])
        self.assertEqual(app.pending_play_video_id, track.video_id)
        self.assertEqual(app.status_message, "Loading: Song")

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

    def test_stream_error_for_current_track_clears_pending_playback_and_surfaces_message(self) -> None:
        app = SimplePlayApp()
        track = Track(video_id="abc123", title="Song")
        loads: list[tuple[str, str | None]] = []

        app.current_track = track
        app.pending_play_video_id = track.video_id
        app.player.load = lambda url, media_title=None: loads.append((url, media_title))  # type: ignore[method-assign]

        app._process_event(
            {
                "type": "stream-error",
                "video_id": track.video_id,
                "message": "Video is unavailable on YouTube.",
            }
        )

        self.assertEqual(loads, [])
        self.assertIsNone(app.pending_play_video_id)
        self.assertEqual(app.status_message, "Video is unavailable on YouTube.")

    def test_stream_error_removes_unplayable_track_from_queue(self) -> None:
        app = SimplePlayApp()
        current = Track(video_id="now", title="Now")
        broken = Track(video_id="bad", title="Broken")
        healthy = Track(video_id="good", title="Healthy")

        app.current_track = current
        app.list_mode = "queue"
        app.up_next.extend([broken, healthy])
        app.results = [broken, healthy]

        app._process_event(
            {
                "type": "stream-error",
                "video_id": broken.video_id,
                "message": "Video is unavailable on YouTube.",
            }
        )

        self.assertEqual([track.video_id for track in app.up_next], ["good"])
        self.assertEqual([track.video_id for track in app.results], ["good"])

    def test_core_idle_change_resumes_autoplay_when_waiting(self) -> None:
        app = SimplePlayApp()
        current = Track(video_id="now", title="Now")
        healthy = Track(video_id="good", title="Healthy")
        resumed: list[bool] = []

        app.current_track = current
        app.awaiting_autoplay = True
        app.player_idle = False
        app.up_next.append(healthy)
        app._play_next = lambda *, auto: resumed.append(auto)  # type: ignore[method-assign]

        app._handle_player_payload({"event": "property-change", "name": "core-idle", "data": True})

        self.assertEqual(resumed, [True])

    def test_volume_property_change_updates_state(self) -> None:
        app = SimplePlayApp()

        app._handle_player_payload({"event": "property-change", "name": "volume", "data": 72.0})

        self.assertEqual(app.volume, 72.0)

    def test_sync_mpv_playlist_uses_resolved_queue_prefix(self) -> None:
        app = SimplePlayApp()
        history = Track(video_id="hist", title="History")
        current = Track(video_id="now", title="Now")
        first = Track(video_id="next1", title="Next 1")
        second = Track(video_id="next2", title="Next 2")
        synced: list[tuple[list[tuple[Track, str]], list[tuple[Track, str]]]] = []
        titles: list[str] = []

        app.history = [history]
        app.current_track = current
        app.up_next.extend([first, second])
        app.stream_cache = {
            history.video_id: StreamCacheEntry(url="https://example.com/history"),
            current.video_id: StreamCacheEntry(url="https://example.com/current"),
            first.video_id: StreamCacheEntry(url="https://example.com/next1"),
        }
        app.player.sync_playlist = lambda history_entries, up_next_entries: synced.append(  # type: ignore[method-assign]
            (history_entries, up_next_entries)
        )
        app.player.set_media_title = lambda title: titles.append(title)  # type: ignore[method-assign]

        app._sync_mpv_playlist()

        self.assertEqual(app.playlist_tracks, [history, current, first])
        self.assertEqual(
            synced,
            [
                (
                    [(history, "https://example.com/history")],
                    [(first, "https://example.com/next1")],
                )
            ],
        )
        self.assertEqual(titles, ["Now"])
