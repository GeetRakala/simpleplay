from __future__ import annotations

import unittest

from simpleplay.models import Track
from simpleplay.youtube import YouTubeClient, _clean_yt_dlp_error, install_hint_for_binary


SEARCH_HTML = """
<script>
var ytInitialData = {
  "contents": {
    "twoColumnSearchResultsRenderer": {
      "primaryContents": {
        "sectionListRenderer": {
          "contents": [
            {
              "itemSectionRenderer": {
                "contents": [
                  {
                    "videoRenderer": {
                      "videoId": "abc123",
                      "title": {"runs": [{"text": "First Song"}]},
                      "ownerText": {"runs": [{"text": "Artist One"}]},
                      "lengthText": {"simpleText": "3:45"}
                    }
                  },
                  {
                    "videoRenderer": {
                      "videoId": "def456",
                      "title": {"runs": [{"text": "Second Song"}]},
                      "ownerText": {"runs": [{"text": "Artist Two"}]},
                      "lengthText": {"simpleText": "4:05"}
                    }
                  }
                ]
              }
            }
          ]
        }
      }
    }
  }
};
</script>
"""

RELATED_HTML = """
<script>
var ytInitialData = {
  "contents": {
    "twoColumnWatchNextResults": {
      "secondaryResults": {
        "secondaryResults": {
          "results": [
            {
              "compactVideoRenderer": {
                "videoId": "seed123",
                "title": {"simpleText": "Seed Song"},
                "shortBylineText": {"runs": [{"text": "Seed Artist"}]},
                "lengthText": {"simpleText": "2:58"}
              }
            },
            {
              "compactVideoRenderer": {
                "videoId": "next456",
                "title": {"simpleText": "Next Song"},
                "shortBylineText": {"runs": [{"text": "Next Artist"}]},
                "lengthText": {"simpleText": "3:12"}
              }
            }
          ]
        }
      }
    }
  }
};
</script>
"""


class YouTubeClientTests(unittest.TestCase):
    def test_search_uses_fast_page_and_caches_results(self) -> None:
        client = YouTubeClient()
        calls: list[str] = []

        client._fetch_html = lambda url: calls.append(url) or SEARCH_HTML  # type: ignore[method-assign]

        first = client.search("First Song", limit=2)
        second = client.search("First Song", limit=2)

        self.assertEqual(len(calls), 1)
        self.assertEqual([track.video_id for track in first], ["abc123", "def456"])
        self.assertEqual([track.video_id for track in second], ["abc123", "def456"])

    def test_fetch_mix_uses_fast_related_results(self) -> None:
        client = YouTubeClient()
        seed = Track(video_id="seed123", title="Seed Song")

        client._fetch_html = lambda url: RELATED_HTML  # type: ignore[method-assign]

        tracks = client.fetch_mix(seed, limit=5)

        self.assertEqual([track.video_id for track in tracks], ["next456"])
        self.assertEqual(tracks[0].channel, "Next Artist")
        self.assertEqual(tracks[0].duration, 192)

    def test_search_falls_back_to_yt_dlp_package(self) -> None:
        client = YouTubeClient()
        client._search_fast = lambda query, limit: []  # type: ignore[method-assign]
        client._extract_info = lambda target, **kwargs: {  # type: ignore[method-assign]
            "entries": [
                {
                    "id": "pkg123",
                    "title": "Package Song",
                    "channel": "Package Artist",
                    "duration": 201,
                    "url": "https://www.youtube.com/watch?v=pkg123",
                }
            ]
        }

        tracks = client.search("Package Song", limit=1)

        self.assertEqual([track.video_id for track in tracks], ["pkg123"])
        self.assertEqual(tracks[0].channel, "Package Artist")

    def test_resolve_stream_url_uses_yt_dlp_payload(self) -> None:
        client = YouTubeClient()
        track = Track(video_id="abc123", title="Song")
        client._extract_info = lambda target, **kwargs: {  # type: ignore[method-assign]
            "requested_formats": [{"url": "https://cdn.example.com/audio"}]
        }

        url = client.resolve_stream_url(track)

        self.assertEqual(url, "https://cdn.example.com/audio")


class BinaryHintTests(unittest.TestCase):
    def test_macos_mpv_hint_mentions_homebrew(self) -> None:
        hint = install_hint_for_binary("mpv", platform="darwin")

        self.assertIn("brew install mpv", hint)

    def test_linux_mpv_hint_mentions_common_package_managers(self) -> None:
        hint = install_hint_for_binary("mpv", platform="linux")

        self.assertIn("sudo apt install mpv", hint)
        self.assertIn("sudo dnf install mpv", hint)
        self.assertIn("sudo pacman -S mpv", hint)

    def test_windows_mpv_hint_mentions_winget(self) -> None:
        hint = install_hint_for_binary("mpv", platform="win32")

        self.assertIn("winget search mpv", hint)
        self.assertIn("winget install <mpv-package-id>", hint)


class ErrorCleanupTests(unittest.TestCase):
    def test_unavailable_video_error_is_sanitized(self) -> None:
        message = _clean_yt_dlp_error(
            "ERROR: [youtube] abc123def45: Video unavailable. This video is unavailable",
            fallback="yt-dlp failed.",
        )

        self.assertEqual(message, "Video is unavailable on YouTube.")
