from __future__ import annotations

import unittest

from simpleplay.models import Track
from simpleplay.youtube import YouTubeClient


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
