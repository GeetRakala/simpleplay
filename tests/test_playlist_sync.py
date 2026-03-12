from __future__ import annotations

from collections import deque
import unittest

from simpleplay.app import SimplePlayApp
from simpleplay.models import Track


class PlaylistSyncTests(unittest.TestCase):
    def test_playlist_position_change_updates_current_history_and_queue(self) -> None:
        app = SimplePlayApp()
        first = Track(video_id="one", title="One")
        second = Track(video_id="two", title="Two")
        third = Track(video_id="three", title="Three")

        app.playlist_tracks = [first, second, third]
        app.history = [first]
        app.current_track = second
        app.up_next = deque([third])
        app.results = list(app.up_next)

        app.player.set_media_title = lambda title: None  # type: ignore[method-assign]
        app._start_related_prefetch = lambda track: None  # type: ignore[method-assign]
        app._fill_queue_from_related = lambda track: None  # type: ignore[method-assign]
        app._warm_queue_streams = lambda: None  # type: ignore[method-assign]

        app._handle_playlist_position_change(0)

        self.assertEqual(app.current_track.video_id, "one")
        self.assertEqual(app.history, [])
        self.assertEqual([track.video_id for track in app.up_next], ["two", "three"])
        self.assertEqual([track.video_id for track in app.results], ["two", "three"])

    def test_playlist_position_change_preserves_unmirrored_queue_tail(self) -> None:
        app = SimplePlayApp()
        first = Track(video_id="one", title="One")
        second = Track(video_id="two", title="Two")
        third = Track(video_id="three", title="Three")
        fourth = Track(video_id="four", title="Four")

        app.playlist_tracks = [first, second, third]
        app.history = [first]
        app.current_track = second
        app.up_next = deque([third, fourth])
        app.results = list(app.up_next)

        app._sync_mpv_playlist = lambda: None  # type: ignore[method-assign]
        app._start_related_prefetch = lambda track: None  # type: ignore[method-assign]
        app._fill_queue_from_related = lambda track: None  # type: ignore[method-assign]
        app._warm_queue_streams = lambda: None  # type: ignore[method-assign]

        app._handle_playlist_position_change(2)

        self.assertEqual(app.current_track.video_id, "three")
        self.assertEqual([track.video_id for track in app.history], ["one", "two"])
        self.assertEqual([track.video_id for track in app.up_next], ["four"])
        self.assertEqual([track.video_id for track in app.results], ["four"])
