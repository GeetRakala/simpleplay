from __future__ import annotations

import queue
import unittest

from simpleplay.models import Track
from simpleplay.player import MPVController


class MPVControllerTests(unittest.TestCase):
    def test_load_forces_pause_off(self) -> None:
        controller = MPVController(queue.Queue())
        commands: list[list[object]] = []
        controller.command = commands.append  # type: ignore[method-assign]

        controller.load("https://example.com/audio", media_title="Track Title")

        self.assertEqual(
            commands,
            [
                ["loadfile", "https://example.com/audio", "replace"],
                ["set_property", "force-media-title", "Track Title"],
                ["set_property", "pause", False],
            ],
        )

    def test_sync_playlist_builds_history_and_queue_entries(self) -> None:
        controller = MPVController(queue.Queue())
        commands: list[list[object]] = []
        controller.command = commands.append  # type: ignore[method-assign]

        history = [(Track(video_id="h1", title="History 1"), "https://example.com/history")]
        up_next = [(Track(video_id="n1", title="Next 1"), "https://example.com/next")]

        controller.sync_playlist(history, up_next)

        self.assertEqual(
            commands,
            [
                ["playlist-clear"],
                [
                    "loadfile",
                    "https://example.com/history",
                    "insert-at",
                    0,
                    {"force-media-title": "History 1"},
                ],
                [
                    "loadfile",
                    "https://example.com/next",
                    "append",
                    -1,
                    {"force-media-title": "Next 1"},
                ],
            ],
        )

    def test_change_volume_sends_relative_volume_command(self) -> None:
        controller = MPVController(queue.Queue())
        commands: list[list[object]] = []
        controller.command = commands.append  # type: ignore[method-assign]

        controller.change_volume(5)

        self.assertEqual(commands, [["add", "volume", 5]])
