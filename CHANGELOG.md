# Changelog

## 0.1.3 - 2026-03-12

- restore `mpv` next/previous controls by mirroring the resolved prefix of the queue back into `mpv`
- keep `mpv` playlist navigation aligned with the app queue when unresolved tracks still exist later in the queue

## 0.1.2 - 2026-03-12

- add vim-like volume control with `H` and `L`, plus live volume display in the header
- stop falling back to raw YouTube watch URLs for playback so unavailable videos fail cleanly in-app
- sanitize and silence noisy `yt-dlp` extractor errors during stream resolution
- fall back to the `yt-dlp` CLI when the Python `yt-dlp` package is not installed

## 0.1.1 - 2026-03-11

- stop background related-track fetch errors from overwriting the now-playing status line
- sanitize noisy `yt-dlp` unavailable-video errors into shorter UI messages

## 0.1.0 - 2026-03-10

- initial public release
