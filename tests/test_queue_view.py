from __future__ import annotations

from collections import deque
import unittest

from simpleplay.app import SimplePlayApp
from simpleplay.models import Track


class QueueViewTests(unittest.TestCase):
    def test_selecting_from_queue_repopulates_results(self) -> None:
        app = SimplePlayApp()
        current = Track(video_id="current", title="Current")
        first = Track(video_id="first", title="First")
        second = Track(video_id="second", title="Second")
        third = Track(video_id="third", title="Third")

        app.current_track = current
        app.list_mode = "queue"
        app.up_next = deque([first, second, third])
        app.results = list(app.up_next)
        app.selected_index = 1

        app._load_track = lambda track: None  # type: ignore[method-assign]
        app._start_related_prefetch = lambda track: None  # type: ignore[method-assign]
        app._fill_queue_from_related = lambda track: None  # type: ignore[method-assign]
        app._warm_queue_streams = lambda: None  # type: ignore[method-assign]

        app._play_selected()

        self.assertEqual(app.current_track.video_id, "second")
        self.assertEqual([track.video_id for track in app.results], ["third", "first"])
        self.assertEqual([track.video_id for track in app.up_next], ["third", "first"])
