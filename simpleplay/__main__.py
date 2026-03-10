from __future__ import annotations

import argparse
import sys

from .app import SimplePlayApp
from .player import PlayerError
from .youtube import YouTubeError, require_binary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Search and play YouTube music from the terminal.")
    parser.add_argument("query", nargs="*", help="Optional initial search query")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    initial_query = " ".join(args.query).strip()

    try:
        require_binary("mpv")
        app = SimplePlayApp(initial_query=initial_query)
        app.run()
    except KeyboardInterrupt:
        return 0
    except (YouTubeError, PlayerError) as exc:
        print(f"simpleplay: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
