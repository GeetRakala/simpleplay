from __future__ import annotations

import json
import unittest

from simpleplay.youtube import _parse_tracks, mix_url_for


class YouTubeParsingTests(unittest.TestCase):
    def test_parse_tracks_filters_missing_entries(self) -> None:
        payload = [
            None,
            {"id": "track-1", "title": "Song 1", "channel": "Artist", "duration": 210.0, "url": "https://www.youtube.com/watch?v=track-1"},
            {"id": "", "title": "Broken"},
            {"id": "track-1", "title": "Duplicate"},
            {"id": "track-2", "title": "Song 2", "uploader": "Uploader"},
        ]

        tracks = _parse_tracks(payload, source="search")

        self.assertEqual(len(tracks), 2)
        self.assertEqual(tracks[0].video_id, "track-1")
        self.assertEqual(tracks[0].channel, "Artist")
        self.assertEqual(tracks[1].watch_url, "https://www.youtube.com/watch?v=track-2")

    def test_parse_tracks_applies_exclusions_and_limit(self) -> None:
        payload = [
            {"id": "seed", "title": "Seed"},
            {"id": "next-1", "title": "Next 1"},
            {"id": "next-2", "title": "Next 2"},
        ]

        tracks = _parse_tracks(payload, source="mix", exclude_video_ids={"seed"}, limit=1)

        self.assertEqual([track.video_id for track in tracks], ["next-1"])

    def test_mix_url_shape(self) -> None:
        self.assertEqual(
            mix_url_for("abc123"),
            "https://www.youtube.com/watch?v=abc123&list=RDabc123",
        )
