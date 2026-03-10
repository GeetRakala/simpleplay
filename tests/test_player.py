from __future__ import annotations

import queue
import unittest

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
