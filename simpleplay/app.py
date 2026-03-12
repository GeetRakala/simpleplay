from __future__ import annotations

import curses
import queue
import threading
import time
from collections import deque
from typing import Iterable

from .models import LoopMode, StreamCacheEntry, Track, format_duration
from .player import MPVController, PlayerError
from .youtube import DEFAULT_MIX_LIMIT, DEFAULT_SEARCH_LIMIT, YouTubeClient, YouTubeError


HELP_TEXT = "j/k move  Enter play  / search  space/p pause  h/l seek  H/L volume  n next  b prev  r loop  q quit"
INTRO_TEXT = "Minimal terminal playback for YouTube music."
COLOR_PRIMARY = 1
COLOR_ACCENT = 2
COLOR_MUTED = 3
COLOR_ACTIVE = 4
COLOR_WARNING = 5
COLOR_ERROR = 6
COLOR_PROGRESS_FILL = 7
COLOR_PROGRESS_EMPTY = 8
SEARCH_PREFETCH_COUNT = 4
SEARCH_QUEUE_SEED_LIMIT = 8
SEARCH_DEBOUNCE_SECONDS = 0.25


class SimplePlayApp:
    def __init__(self, initial_query: str = "") -> None:
        self.initial_query = initial_query.strip()
        self.events: "queue.Queue[dict]" = queue.Queue()
        self.youtube = YouTubeClient()
        self.player = MPVController(self.events)

        self.results: list[Track] = []
        self.list_mode = "search"
        self.selected_index = 0
        self.list_offset = 0
        self.search_query = self.initial_query
        self.search_mode = False
        self.loading_search = False
        self.pending_play_video_id: str | None = None
        self.status_message = INTRO_TEXT

        self.current_track: Track | None = None
        self.current_position = 0.0
        self.current_duration: float | None = None
        self.paused = False
        self.volume = 100.0
        self.loop_mode = LoopMode.OFF

        self.history: list[Track] = []
        self.forward_stack: list[Track] = []
        self.up_next: deque[Track] = deque()

        self.stream_cache: dict[str, StreamCacheEntry] = {}
        self.related_cache: dict[str, list[Track]] = {}
        self.pending_streams: set[str] = set()
        self.pending_related: set[str] = set()
        self.playlist_tracks: list[Track] = []
        self.player_idle = True
        self.awaiting_autoplay = False
        self.ignore_playlist_pos_until = 0.0
        self.search_debounce_deadline: float | None = None
        self.pending_search_query = ""
        self.last_completed_search_query = ""

        self.search_token = 0
        self.should_exit = False

    def run(self) -> None:
        self.player.start()
        try:
            curses.wrapper(self._curses_main)
        finally:
            self.player.shutdown()

    def _curses_main(self, stdscr: "curses._CursesWindow") -> None:
        self._configure_curses(stdscr)
        if self.initial_query:
            self._start_search(self.initial_query)

        while not self.should_exit:
            self._process_events()
            self._maybe_start_pending_playback()
            self._maybe_start_live_search()
            self._draw(stdscr)
            key = stdscr.getch()
            if key == -1:
                continue
            self._handle_key(key)

    def _configure_curses(self, stdscr: "curses._CursesWindow") -> None:
        curses.start_color()
        curses.use_default_colors()
        if curses.has_colors():
            for pair_id, color in (
                (COLOR_PRIMARY, curses.COLOR_WHITE),
                (COLOR_ACCENT, curses.COLOR_CYAN),
                (COLOR_MUTED, curses.COLOR_BLUE),
                (COLOR_ACTIVE, curses.COLOR_GREEN),
                (COLOR_WARNING, curses.COLOR_YELLOW),
                (COLOR_ERROR, curses.COLOR_RED),
                (COLOR_PROGRESS_FILL, curses.COLOR_GREEN),
                (COLOR_PROGRESS_EMPTY, curses.COLOR_BLUE),
            ):
                try:
                    curses.init_pair(pair_id, color, -1)
                except curses.error:
                    continue
        stdscr.nodelay(False)
        stdscr.timeout(100)
        try:
            curses.curs_set(1 if self.search_mode else 0)
        except curses.error:
            pass

    def _handle_key(self, key: int) -> None:
        if key == 3:
            self.should_exit = True
            return

        if self.search_mode:
            self._handle_search_key(key)
            return

        if key in (ord("q"), ord("Q")):
            self.should_exit = True
            return
        if key in (ord("/"),):
            self.search_query = ""
            self.search_mode = True
            self.search_debounce_deadline = None
            self.status_message = "Search mode."
            return
        if key in (curses.KEY_DOWN, ord("j")):
            self._move_selection(1)
            return
        if key in (curses.KEY_UP, ord("k")):
            self._move_selection(-1)
            return
        if key in (10, 13, curses.KEY_ENTER):
            self._play_selected()
            return
        if key in (ord(" "), ord("p"), ord("P")):
            self._toggle_pause()
            return
        if key == ord("h"):
            self._seek(-10)
            return
        if key == ord("l"):
            self._seek(10)
            return
        if key == ord("H"):
            self._adjust_volume(-5)
            return
        if key == ord("L"):
            self._adjust_volume(5)
            return
        if key == ord("n"):
            self._play_next(auto=False)
            return
        if key == ord("b"):
            self._play_previous()
            return
        if key == ord("r"):
            self.loop_mode = self.loop_mode.cycle()
            self.status_message = f"Loop mode: {self.loop_mode.value}"
            return
        if key == ord("g"):
            self.selected_index = 0
            return
        if key == ord("G") and self.results:
            self.selected_index = len(self.results) - 1
            return

    def _handle_search_key(self, key: int) -> None:
        if key in (27,):
            self.search_mode = False
            self.search_debounce_deadline = None
            self.status_message = "Search cancelled."
            return
        if key in (10, 13, curses.KEY_ENTER):
            query = self.search_query.strip()
            self.search_mode = False
            self.search_debounce_deadline = None
            if query:
                if self._search_needs_refresh(query):
                    self._start_search(query)
            else:
                self.status_message = "Enter a search query."
            return
        if key in (curses.KEY_BACKSPACE, 127, 8):
            self.search_query = self.search_query[:-1]
            self._schedule_live_search()
            return
        if key == 21:
            self.search_query = ""
            self.search_debounce_deadline = None
            return
        if 32 <= key <= 126:
            self.search_query += chr(key)
            self._schedule_live_search()

    def _move_selection(self, delta: int) -> None:
        if not self.results:
            return
        self.selected_index = max(0, min(len(self.results) - 1, self.selected_index + delta))
        if self.list_mode == "search":
            self._prime_track(self.results[self.selected_index])

    def _play_selected(self) -> None:
        if not self.results:
            self.status_message = self._empty_results_message()
            return
        if self.list_mode == "queue":
            self._play_selected_queue_track()
            return

        track = self.results[self.selected_index]
        self.list_mode = "queue"
        self._switch_track(track, add_previous_to_history=True, clear_forward=True, reset_queue=True)

    def _play_selected_queue_track(self) -> None:
        if not self.results:
            self.status_message = "Up next is empty."
            return

        queue_tracks = list(self.up_next)
        if not queue_tracks:
            self.status_message = "Up next is empty."
            return

        selected_index = max(0, min(self.selected_index, len(queue_tracks) - 1))
        chosen_track = queue_tracks.pop(selected_index)
        reordered_queue = queue_tracks[selected_index:] + queue_tracks[:selected_index]
        self.up_next = deque(reordered_queue)
        self._switch_track(chosen_track, add_previous_to_history=True, clear_forward=True, reset_queue=False)

    def _toggle_pause(self) -> None:
        if not self.current_track:
            self.status_message = "Nothing is playing."
            return
        try:
            self.player.toggle_pause()
        except PlayerError as exc:
            self.status_message = str(exc)

    def _seek(self, seconds: int) -> None:
        if not self.current_track:
            self.status_message = "Nothing is playing."
            return
        try:
            self.player.seek(seconds)
            self.status_message = f"Seeked {seconds:+d}s"
        except PlayerError as exc:
            self.status_message = str(exc)

    def _adjust_volume(self, delta: int) -> None:
        try:
            self.player.change_volume(delta)
        except PlayerError as exc:
            self.status_message = str(exc)
            return

        self.volume = max(0.0, self.volume + delta)
        self.status_message = f"Volume: {round(self.volume)}%"

    def _play_previous(self) -> None:
        if self.current_position > 5 and self.current_track:
            self._replay_current()
            return
        if not self.history:
            self.status_message = "No previous track."
            return

        target = self.history.pop()
        self._switch_track(
            target,
            add_previous_to_history=False,
            push_previous_to_forward=True,
            clear_forward=False,
            reset_queue=False,
        )

    def _play_next(self, *, auto: bool) -> None:
        if not self.current_track and self.results:
            self._play_selected()
            return
        if not self.current_track:
            self.status_message = "Nothing queued."
            return

        if not auto and self.forward_stack:
            target = self.forward_stack.pop()
            self._switch_track(
                target,
                add_previous_to_history=True,
                clear_forward=False,
                reset_queue=False,
            )
            return

        next_track = self._pop_next_track()
        if not next_track and self.current_track:
            self._fill_queue_from_related(self.current_track)
            next_track = self._pop_next_track()

        if next_track:
            self._switch_track(next_track, add_previous_to_history=True, clear_forward=True, reset_queue=False)
            return

        if self.loop_mode is LoopMode.ALL:
            target = self.history[0] if self.history else self.current_track
            if target:
                self._switch_track(
                    target,
                    add_previous_to_history=False,
                    clear_forward=False,
                    reset_queue=False,
                )
                return

        self.status_message = "Queue ended."

    def _replay_current(self) -> None:
        if not self.current_track:
            return
        self.current_position = 0.0
        self.current_duration = self.current_track.duration
        self._load_track(self.current_track)

    def _switch_track(
        self,
        track: Track,
        *,
        add_previous_to_history: bool,
        push_previous_to_forward: bool = False,
        clear_forward: bool,
        reset_queue: bool,
    ) -> None:
        previous = self.current_track
        if previous and add_previous_to_history:
            self.history.append(previous)
        if previous and push_previous_to_forward:
            self.forward_stack.append(previous)
        if clear_forward:
            self.forward_stack.clear()
        if reset_queue:
            self.up_next.clear()

        self.current_track = track
        self.current_position = 0.0
        self.current_duration = float(track.duration) if track.duration is not None else None
        self.paused = False
        self.awaiting_autoplay = False
        self.status_message = f"Loading: {track.title}"

        self._load_track(track)
        self._start_related_prefetch(track)
        self._seed_queue_from_search_results(track, reset_queue=reset_queue)
        self._fill_queue_from_related(track)
        self._warm_queue_streams()
        self._sync_queue_results()

    def _load_track(self, track: Track) -> None:
        cache = self.stream_cache.get(track.video_id)
        if cache and cache.is_fresh():
            self._start_playback(track, cache.url)
            return

        self.pending_play_video_id = track.video_id
        self.status_message = f"Loading: {track.title}"
        self._start_stream_resolve(track)

    def _start_playback(self, track: Track, url: str) -> None:
        try:
            self.player.load(url, media_title=track.title)
        except PlayerError as exc:
            if self.pending_play_video_id == track.video_id:
                self.pending_play_video_id = None
            self.status_message = str(exc)
            return
        self.stream_cache[track.video_id] = StreamCacheEntry(url=url)
        self.pending_play_video_id = None
        self.status_message = f"Playing: {track.title}"
        self._sync_mpv_playlist()

    def _pop_next_track(self) -> Track | None:
        while self.up_next:
            candidate = self.up_next.popleft()
            if self.current_track and candidate.video_id == self.current_track.video_id:
                continue
            return candidate
        return None

    def _start_search(self, query: str) -> None:
        self.search_token += 1
        token = self.search_token
        self.pending_search_query = query
        self.loading_search = True
        self.status_message = f"Searching YouTube for: {query}"

        def worker() -> None:
            try:
                tracks = self.youtube.search(query, limit=DEFAULT_SEARCH_LIMIT)
            except YouTubeError as exc:
                self.events.put({"type": "search-error", "token": token, "message": str(exc)})
                return
            self.events.put({"type": "search-results", "token": token, "query": query, "tracks": tracks})

        threading.Thread(target=worker, daemon=True).start()

    def _schedule_live_search(self) -> None:
        query = self.search_query.strip()
        if not query:
            self.search_debounce_deadline = None
            return
        self.search_debounce_deadline = time.time() + SEARCH_DEBOUNCE_SECONDS

    def _maybe_start_live_search(self) -> None:
        if not self.search_mode:
            return
        if self.search_debounce_deadline is None or time.time() < self.search_debounce_deadline:
            return

        self.search_debounce_deadline = None
        query = self.search_query.strip()
        if not query or not self._search_needs_refresh(query):
            return
        self._start_search(query)

    def _search_needs_refresh(self, query: str) -> bool:
        normalized = query.strip()
        if not normalized:
            return False
        if self.loading_search and normalized == self.pending_search_query:
            return False
        return normalized != self.last_completed_search_query

    def _maybe_start_pending_playback(self) -> None:
        if not self.pending_play_video_id or not self.current_track:
            return
        if self.current_track.video_id != self.pending_play_video_id:
            self.pending_play_video_id = None
            return

        cache = self.stream_cache.get(self.current_track.video_id)
        if cache and cache.is_fresh():
            self._start_playback(self.current_track, cache.url)
        return

    def _prime_search_results(self, tracks: Iterable[Track]) -> None:
        for track in list(tracks)[:SEARCH_PREFETCH_COUNT]:
            self._prime_track(track)

    def _prime_track(self, track: Track) -> None:
        self._start_related_prefetch(track)
        cache = self.stream_cache.get(track.video_id)
        if cache and cache.is_fresh():
            return
        self._start_stream_resolve(track)

    def _start_related_prefetch(self, track: Track) -> None:
        if track.video_id in self.related_cache or track.video_id in self.pending_related:
            return
        self.pending_related.add(track.video_id)

        def worker() -> None:
            try:
                tracks = self.youtube.fetch_mix(track, limit=DEFAULT_MIX_LIMIT)
            except YouTubeError as exc:
                self.events.put({"type": "related-error", "video_id": track.video_id, "message": str(exc)})
                return
            self.events.put({"type": "related-results", "video_id": track.video_id, "tracks": tracks})

        threading.Thread(target=worker, daemon=True).start()

    def _start_stream_resolve(self, track: Track) -> None:
        if track.video_id in self.pending_streams:
            return
        self.pending_streams.add(track.video_id)

        def worker() -> None:
            try:
                url = self.youtube.resolve_stream_url(track)
            except YouTubeError as exc:
                self.events.put({"type": "stream-error", "video_id": track.video_id, "message": str(exc)})
                return
            self.events.put({"type": "stream-ready", "video_id": track.video_id, "url": url})

        threading.Thread(target=worker, daemon=True).start()

    def _fill_queue_from_related(self, track: Track) -> None:
        related = self.related_cache.get(track.video_id)
        if not related:
            return
        self._enqueue_tracks(related)

    def _seed_queue_from_search_results(self, track: Track, *, reset_queue: bool) -> None:
        if not reset_queue or self.up_next or self.related_cache.get(track.video_id):
            return
        if self.list_mode != "queue" or not self.results:
            return

        seeds: list[Track] = []
        for candidate in self.results:
            if candidate.video_id == track.video_id:
                continue
            seeds.append(candidate)
            if len(seeds) >= SEARCH_QUEUE_SEED_LIMIT:
                break
        if seeds:
            self._enqueue_tracks(seeds)

    def _enqueue_tracks(self, tracks: Iterable[Track]) -> None:
        seen = self._seen_video_ids()
        added = 0
        for track in tracks:
            if track.video_id in seen:
                continue
            self.up_next.append(track)
            seen.add(track.video_id)
            added += 1
            if len(self.up_next) >= 40:
                break
        if added:
            self._warm_queue_streams()
            self._sync_queue_results()
            self._sync_mpv_playlist()
            self._maybe_resume_autoplay()

    def _drop_track_from_queue(self, video_id: str) -> None:
        if not self.up_next:
            return

        filtered = [track for track in self.up_next if track.video_id != video_id]
        if len(filtered) == len(self.up_next):
            return

        self.up_next = deque(filtered)
        self._sync_queue_results()

    def _seen_video_ids(self) -> set[str]:
        seen = {track.video_id for track in self.history}
        seen.update(track.video_id for track in self.forward_stack)
        seen.update(track.video_id for track in self.up_next)
        if self.current_track:
            seen.add(self.current_track.video_id)
        return seen

    def _warm_queue_streams(self) -> None:
        for track in list(self.up_next)[:3]:
            self._start_related_prefetch(track)
            cache = self.stream_cache.get(track.video_id)
            if cache and cache.is_fresh():
                continue
            self._start_stream_resolve(track)

    def _process_events(self) -> None:
        while True:
            try:
                event = self.events.get_nowait()
            except queue.Empty:
                return
            self._process_event(event)

    def _process_event(self, event: dict) -> None:
        kind = event.get("type")
        if kind == "search-results":
            if event["token"] != self.search_token:
                return
            self.loading_search = False
            self.pending_search_query = ""
            self.last_completed_search_query = event["query"].strip()
            self.list_mode = "search"
            self.results = event["tracks"]
            self.selected_index = 0
            self.list_offset = 0
            self.status_message = f"Loaded {len(self.results)} result(s) for: {event['query']}"
            self._prime_search_results(self.results)
            return

        if kind == "search-error":
            if event["token"] != self.search_token:
                return
            self.loading_search = False
            self.pending_search_query = ""
            self.status_message = event["message"]
            return

        if kind == "related-results":
            self.pending_related.discard(event["video_id"])
            tracks = event["tracks"]
            self.related_cache[event["video_id"]] = tracks
            if self.current_track and self.current_track.video_id == event["video_id"]:
                self._enqueue_tracks(tracks)
            return

        if kind == "related-error":
            self.pending_related.discard(event["video_id"])
            return

        if kind == "stream-ready":
            video_id = event["video_id"]
            self.pending_streams.discard(video_id)
            self.stream_cache[video_id] = StreamCacheEntry(url=event["url"])
            if self.pending_play_video_id == video_id and self.current_track and self.current_track.video_id == video_id:
                self._maybe_start_pending_playback()
            elif self.current_track and not self.pending_play_video_id:
                self._sync_mpv_playlist()
            return

        if kind == "stream-error":
            video_id = event["video_id"]
            self.pending_streams.discard(video_id)
            self._drop_track_from_queue(video_id)
            if self.pending_play_video_id == video_id and self.current_track and self.current_track.video_id == video_id:
                self.pending_play_video_id = None
                self.status_message = event["message"]
            elif self.current_track and not self.pending_play_video_id:
                self._sync_mpv_playlist()
            return

        if kind == "player-event":
            self._handle_player_payload(event["payload"])
            return

        if kind == "player-exit" and not self.should_exit:
            self.status_message = "mpv exited unexpectedly."
            self.should_exit = True

    def _handle_player_payload(self, payload: dict) -> None:
        event_name = payload.get("event")
        if event_name == "property-change":
            name = payload.get("name")
            data = payload.get("data")
            if name == "pause":
                self.paused = bool(data)
            elif name == "core-idle":
                self.player_idle = bool(data)
                self._maybe_resume_autoplay()
            elif name == "time-pos":
                self.current_position = float(data or 0.0)
            elif name == "duration":
                self.current_duration = None if data is None else float(data)
            elif name == "volume":
                if data is not None:
                    self.volume = float(data)
            elif name == "playlist-pos":
                if self._should_ignore_playlist_pos():
                    return
                if data is None:
                    return
                self._handle_playlist_position_change(int(data))
            return

        if event_name == "end-file":
            if payload.get("reason") != "eof":
                return
            self.awaiting_autoplay = True
            if self.loop_mode is LoopMode.ONE:
                self._replay_current()
                return
            if self.loop_mode is LoopMode.ALL and not self.up_next:
                target = self.history[0] if self.history else self.current_track
                if target:
                    self._switch_track(
                        target,
                        add_previous_to_history=False,
                        clear_forward=False,
                        reset_queue=False,
                    )
                return
            self._maybe_resume_autoplay()

    def _draw(self, stdscr: "curses._CursesWindow") -> None:
        stdscr.erase()
        height, width = stdscr.getmaxyx()

        self._fix_selection()
        self._sync_cursor_visibility()
        layout = self._layout(height)

        self._draw_header(stdscr, layout["header_y"], width)
        self._draw_progress_bar(stdscr, layout["progress_y"], width)
        self._draw_now_playing(stdscr, layout["title_y"], layout["meta_y"], width)
        self._draw_search_line(stdscr, layout["search_y"], width)
        self._safe_addnstr(
            stdscr,
            layout["label_y"],
            0,
            self._results_label(),
            width,
            self._results_label_attr(),
        )

        body_height = max(1, height - layout["body_top"] - layout["footer_rows"])
        self._draw_results(stdscr, layout["body_top"], body_height, width)
        self._draw_footer(stdscr, height, width)

        if self.search_mode:
            cursor_x = min(width - 1, len("Search  /") + len(self.search_query))
            try:
                stdscr.move(layout["search_y"], cursor_x)
            except curses.error:
                pass
        stdscr.refresh()

    def _draw_results(self, stdscr: "curses._CursesWindow", top: int, height: int, width: int) -> None:
        if not self.results:
            message = self._empty_results_message()
            self._safe_addnstr(stdscr, top, 0, message, width, self._muted_attr())
            return

        self._adjust_list_offset(height)
        end = min(len(self.results), self.list_offset + height)
        for row, idx in enumerate(range(self.list_offset, end)):
            track = self.results[idx]
            self._draw_track_row(stdscr, top + row, width, track, idx == self.selected_index)

    def _draw_footer(self, stdscr: "curses._CursesWindow", height: int, width: int) -> None:
        self._safe_addnstr(stdscr, height - 1, 0, HELP_TEXT, width, self._help_attr())

    def _progress_parts(self, width: int) -> tuple[str, int, int, str]:
        position = format_duration(self.current_position)
        duration = format_duration(self.current_duration)
        bar_width = max(12, width - len(position) - len(duration) - 4)

        ratio = 0.0
        if self.current_duration and self.current_duration > 0:
            ratio = min(max(self.current_position / self.current_duration, 0.0), 1.0)

        filled = int(bar_width * ratio)
        filled = max(0, min(filled, bar_width))
        return position, filled, bar_width, duration

    def _fix_selection(self) -> None:
        if not self.results:
            self.selected_index = 0
            self.list_offset = 0
            return
        self.selected_index = max(0, min(self.selected_index, len(self.results) - 1))

    def _adjust_list_offset(self, visible_rows: int) -> None:
        if self.selected_index < self.list_offset:
            self.list_offset = self.selected_index
        elif self.selected_index >= self.list_offset + visible_rows:
            self.list_offset = self.selected_index - visible_rows + 1

    def _player_state_label(self) -> str:
        if self.pending_play_video_id:
            return "loading"
        if self.current_track and self.paused:
            return "paused"
        if self.current_track:
            return "playing"
        return "idle"

    def _results_label(self) -> str:
        if self.list_mode == "queue":
            return "Up Next"
        return "Search Results"

    def _status_attr(self) -> int:
        if self.status_message.startswith("Playing:"):
            return self._white_bold_attr()
        if self.status_message.startswith(("Loading:", "Searching")):
            return self._warning_attr(curses.A_BOLD)
        if self.status_message.startswith(("Search mode", "Loaded", "Loop mode")):
            return self._accent_attr(curses.A_BOLD)
        if self.status_message.startswith(("No ", "Could not", "mpv ", "Required ", "Enter ", "Queue ended")):
            return self._error_attr(curses.A_BOLD)
        return self._primary_attr()

    def _white_bold_attr(self) -> int:
        attr = curses.A_BOLD
        if curses.has_colors():
            try:
                attr |= curses.color_pair(1)
            except curses.error:
                pass
        return attr

    def _empty_results_message(self) -> str:
        if self.list_mode == "queue":
            if self.current_track and self.current_track.video_id in self.pending_related:
                return "Loading up next..."
            return "Up next is empty."
        return "No results yet."

    def _layout(self, height: int) -> dict[str, int]:
        if height >= 20:
            return {
                "header_y": 0,
                "progress_y": 2,
                "title_y": 4,
                "meta_y": 5,
                "search_y": 7,
                "label_y": 9,
                "body_top": 11,
                "footer_rows": 2,
            }
        return {
            "header_y": 0,
            "progress_y": 1,
            "title_y": 2,
            "meta_y": 3,
            "search_y": 4,
            "label_y": 5,
            "body_top": 6,
            "footer_rows": 1,
        }

    def _draw_search_line(self, stdscr: "curses._CursesWindow", y: int, width: int) -> None:
        x = 0
        self._safe_addnstr(stdscr, y, x, "Search", width, self._section_attr())
        x += len("Search")
        self._safe_addnstr(stdscr, y, x, "  ", width, self._primary_attr())
        x += 2
        prompt_attr = self._accent_attr(curses.A_BOLD) if self.search_mode else self._muted_attr()
        self._safe_addnstr(stdscr, y, x, "/", width, prompt_attr)
        x += 1
        if self.search_query:
            query = self.search_query
            query_attr = self._primary_attr()
        elif self.search_mode:
            query = "type a query"
            query_attr = self._muted_attr()
        else:
            query = "start a new search"
            query_attr = self._muted_attr()
        self._safe_addnstr(stdscr, y, x, query, width, query_attr)

    def _draw_progress_bar(self, stdscr: "curses._CursesWindow", y: int, width: int) -> None:
        if not self.current_track:
            return

        position, filled, bar_width, duration = self._progress_parts(width)

        x = 0
        self._safe_addnstr(stdscr, y, x, position, width, self._muted_attr())
        x += len(position) + 1
        fill_attr = self._progress_fill_attr(curses.A_BOLD)
        if self.paused:
            fill_attr = self._warning_attr(curses.A_BOLD)
        self._safe_addnstr(stdscr, y, x, "#" * filled, width, fill_attr)
        x += filled
        self._safe_addnstr(stdscr, y, x, "-" * (bar_width - filled), width, self._progress_empty_attr())
        x += bar_width - filled
        self._safe_addnstr(stdscr, y, x, f" {duration}", width, self._muted_attr())

    def _draw_header(self, stdscr: "curses._CursesWindow", y: int, width: int) -> None:
        self._safe_addnstr(stdscr, y, 0, "simpleplay", width, self._header_attr())

        state_parts = [self._player_state_label()]
        state_parts.append(f"vol {round(self.volume)}%")
        if self.loop_mode is not LoopMode.OFF:
            state_parts.append(f"loop {self.loop_mode.value}")
        if self.loading_search:
            state_parts.append("search")
        right_text = "   ".join(state_parts)
        right_x = max(0, width - len(right_text) - 1)
        self._safe_addnstr(stdscr, y, right_x, right_text, width, self._muted_attr())

    def _draw_now_playing(self, stdscr: "curses._CursesWindow", title_y: int, meta_y: int, width: int) -> None:
        if self.current_track:
            self._safe_addnstr(stdscr, title_y, 0, "Playing:", width, self._section_attr())
            self._safe_addnstr(
                stdscr,
                title_y,
                len("Playing:") + 2,
                self.current_track.title,
                width,
                self._white_bold_attr(),
            )

            meta_text, meta_attr = self._now_playing_meta()
            self._safe_addnstr(stdscr, meta_y, 0, meta_text, width, meta_attr)
            return

        title = self.status_message or INTRO_TEXT
        self._safe_addnstr(stdscr, title_y, 0, title, width, self._white_bold_attr())
        if self.search_mode:
            self._safe_addnstr(stdscr, meta_y, 0, "Enter to search  Esc to cancel", width, self._muted_attr())

    def _now_playing_meta(self) -> tuple[str, int]:
        if self.status_message and not self.status_message.startswith("Playing:"):
            return self.status_message, self._secondary_status_attr()

        parts = []
        if self.current_track and self.current_track.channel:
            parts.append(self.current_track.channel)
        if self.paused:
            parts.append("paused")
        return "  ".join(parts) if parts else " ", self._muted_attr()

    def _draw_track_row(
        self,
        stdscr: "curses._CursesWindow",
        y: int,
        width: int,
        track: Track,
        selected: bool,
    ) -> None:
        prefix = "> " if selected else "  "
        prefix_attr = self._accent_attr(curses.A_BOLD) if selected else self._muted_attr()
        title_attr = self._white_bold_attr() if selected else self._primary_attr()
        duration = format_duration(track.duration)
        right_text = duration
        right_x = max(len(prefix) + 8, width - len(right_text) - 1)

        available = max(1, right_x - len(prefix) - 1)
        title = self._truncate(track.title, available)

        self._safe_addnstr(stdscr, y, 0, prefix, width, prefix_attr)
        self._safe_addnstr(stdscr, y, len(prefix), title, width, title_attr)
        self._safe_addnstr(stdscr, y, right_x, right_text, width, self._muted_attr())

    def _truncate(self, value: str, max_width: int) -> str:
        if len(value) <= max_width:
            return value
        if max_width <= 3:
            return value[:max_width]
        return value[: max_width - 3] + "..."

    def _header_attr(self) -> int:
        return self._accent_attr(curses.A_BOLD)

    def _results_label_attr(self) -> int:
        return self._section_attr()

    def _help_attr(self) -> int:
        return self._muted_attr(curses.A_DIM)

    def _primary_attr(self, extra: int = 0) -> int:
        return self._color_attr(COLOR_PRIMARY, extra)

    def _accent_attr(self, extra: int = 0) -> int:
        return self._color_attr(COLOR_ACCENT, extra)

    def _muted_attr(self, extra: int = 0) -> int:
        return extra | curses.A_DIM

    def _active_attr(self, extra: int = 0) -> int:
        return self._color_attr(COLOR_ACTIVE, extra)

    def _warning_attr(self, extra: int = 0) -> int:
        return self._color_attr(COLOR_WARNING, extra)

    def _error_attr(self, extra: int = 0) -> int:
        return self._color_attr(COLOR_ERROR, extra)

    def _progress_empty_attr(self, extra: int = 0) -> int:
        return self._color_attr(COLOR_PROGRESS_EMPTY, extra)

    def _progress_fill_attr(self, extra: int = 0) -> int:
        return self._color_attr(COLOR_PROGRESS_FILL, extra)

    def _section_attr(self) -> int:
        return self._accent_attr(curses.A_BOLD)

    def _secondary_status_attr(self) -> int:
        if self.status_message.startswith(("No ", "Could not", "Required ", "mpv ", "Queue ended")):
            return self._error_attr(curses.A_BOLD)
        if self.status_message.startswith(("Loading:", "Searching", "Volume:")):
            return self._accent_attr(curses.A_BOLD)
        return self._muted_attr()

    def _color_attr(self, pair_id: int, extra: int = 0) -> int:
        attr = extra
        if curses.has_colors():
            try:
                attr |= curses.color_pair(pair_id)
            except curses.error:
                pass
        return attr

    def _sync_queue_results(self) -> None:
        if self.list_mode != "queue":
            return

        selected_video_id: str | None = None
        if self.results and 0 <= self.selected_index < len(self.results):
            selected_video_id = self.results[self.selected_index].video_id

        self.results = list(self.up_next)
        if not self.results:
            self.selected_index = 0
            self.list_offset = 0
            return

        if selected_video_id:
            for index, track in enumerate(self.results):
                if track.video_id == selected_video_id:
                    self.selected_index = index
                    break
            else:
                self.selected_index = 0
        else:
            self.selected_index = 0

    def _safe_addnstr(
        self,
        stdscr: "curses._CursesWindow",
        y: int,
        x: int,
        value: str,
        width: int,
        attr: int = 0,
    ) -> None:
        if y < 0 or x < 0:
            return
        clipped = value[: max(0, width - x - 1)]
        try:
            stdscr.addnstr(y, x, clipped, max(0, width - x - 1), attr)
        except curses.error:
            return

    def _sync_cursor_visibility(self) -> None:
        try:
            curses.curs_set(1 if self.search_mode else 0)
        except curses.error:
            pass

    def _sync_mpv_playlist(self) -> None:
        if not self.current_track or self.pending_play_video_id:
            return

        entries = self._mirrored_playlist_entries()
        if entries is None:
            self.playlist_tracks = []
            return

        history_entries, up_next_entries = entries
        self.playlist_tracks = (
            [track for track, _ in history_entries]
            + [self.current_track]
            + [track for track, _ in up_next_entries]
        )
        self.ignore_playlist_pos_until = time.time() + 0.35
        try:
            self.player.sync_playlist(history_entries, up_next_entries)
            self.player.set_media_title(self.current_track.title)
        except PlayerError as exc:
            self.status_message = str(exc)

    def _mirrored_playlist_entries(
        self,
    ) -> tuple[list[tuple[Track, str]], list[tuple[Track, str]]] | None:
        if not self.current_track:
            return None

        history_entries: list[tuple[Track, str]] = []
        for track in self.history:
            url = self._playlist_stream_url(track)
            if not url:
                return None
            history_entries.append((track, url))

        if not self._playlist_stream_url(self.current_track):
            return None

        up_next_entries: list[tuple[Track, str]] = []
        for track in self.up_next:
            url = self._playlist_stream_url(track)
            if not url:
                break
            up_next_entries.append((track, url))
        return history_entries, up_next_entries

    def _playlist_stream_url(self, track: Track) -> str | None:
        cache = self.stream_cache.get(track.video_id)
        if not cache:
            return None
        return cache.url

    def _should_ignore_playlist_pos(self) -> bool:
        return time.time() < self.ignore_playlist_pos_until

    def _handle_playlist_position_change(self, index: int) -> None:
        logical_tracks = self.history + ([self.current_track] if self.current_track else []) + list(self.up_next)
        if index < 0 or index >= len(self.playlist_tracks) or index >= len(logical_tracks):
            return

        new_track = logical_tracks[index]
        if self.current_track and new_track.video_id == self.current_track.video_id and index == len(self.history):
            return

        self.history = logical_tracks[:index]
        self.current_track = new_track
        self.up_next = deque(logical_tracks[index + 1 :])
        self.forward_stack.clear()
        self.current_position = 0.0
        self.current_duration = float(new_track.duration) if new_track.duration is not None else None
        self.paused = False
        self.pending_play_video_id = None
        self.awaiting_autoplay = False
        self.list_mode = "queue"
        self.status_message = f"Playing: {new_track.title}"
        self._sync_queue_results()
        self._start_related_prefetch(new_track)
        self._fill_queue_from_related(new_track)
        self._warm_queue_streams()
        self._sync_mpv_playlist()

    def _maybe_resume_autoplay(self) -> None:
        if not self.awaiting_autoplay or not self.player_idle or not self.up_next:
            return
        self.awaiting_autoplay = False
        self._play_next(auto=True)
