from __future__ import annotations

import unittest

from simpleplay.models import LoopMode, StreamCacheEntry, Track, format_duration


class FormatDurationTests(unittest.TestCase):
    def test_formats_short_values(self) -> None:
        self.assertEqual(format_duration(65), "1:05")

    def test_formats_hour_values(self) -> None:
        self.assertEqual(format_duration(3661), "1:01:01")

    def test_formats_missing_values(self) -> None:
        self.assertEqual(format_duration(None), "--:--")


class LoopModeTests(unittest.TestCase):
    def test_cycle_order(self) -> None:
        self.assertEqual(LoopMode.OFF.cycle(), LoopMode.ALL)
        self.assertEqual(LoopMode.ALL.cycle(), LoopMode.ONE)
        self.assertEqual(LoopMode.ONE.cycle(), LoopMode.OFF)


class TrackTests(unittest.TestCase):
    def test_default_watch_url_is_derived(self) -> None:
        track = Track(video_id="abc123", title="Song")
        self.assertEqual(track.watch_url, "https://www.youtube.com/watch?v=abc123")

    def test_stream_cache_entry_is_fresh_initially(self) -> None:
        entry = StreamCacheEntry(url="https://example.com/audio")
        self.assertTrue(entry.is_fresh())
