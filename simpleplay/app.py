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


HELP_TEXT = "j/k move  Enter play  / search  space/p pause  h/l seek  n next  b prev  r loop  q quit"


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
        self.status_message = "Press / to search."

        self.current_track: Track | None = None
        self.current_position = 0.0
        self.current_duration: float | None = None
        self.paused = False
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
            self._draw(stdscr)
            key = stdscr.getch()
            if key == -1:
                continue
            self._handle_key(key)

    def _configure_curses(self, stdscr: "curses._CursesWindow") -> None:
        curses.use_default_colors()
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
            self.status_message = "Search cancelled."
            return
        if key in (10, 13, curses.KEY_ENTER):
            query = self.search_query.strip()
            self.search_mode = False
            if query:
                self._start_search(query)
            else:
                self.status_message = "Enter a search query."
            return
        if key in (curses.KEY_BACKSPACE, 127, 8):
            self.search_query = self.search_query[:-1]
            return
        if key == 21:
            self.search_query = ""
            return
        if 32 <= key <= 126:
            self.search_query += chr(key)

    def _move_selection(self, delta: int) -> None:
        if not self.results:
            return
        self.selected_index = max(0, min(len(self.results) - 1, self.selected_index + delta))

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
        self._fill_queue_from_related(track)
        self._warm_queue_streams()
        self._sync_queue_results()

    def _load_track(self, track: Track) -> None:
        cache = self.stream_cache.get(track.video_id)
        if cache and cache.is_fresh():
            try:
                self.player.load(cache.url, media_title=track.title)
            except PlayerError as exc:
                self.status_message = str(exc)
            else:
                self.pending_play_video_id = None
                self.status_message = f"Playing: {track.title}"
                self._sync_mpv_playlist()
            return

        self.pending_play_video_id = track.video_id
        self.status_message = f"Resolving audio stream: {track.title}"
        self._start_stream_resolve(track)

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

    def _seen_video_ids(self) -> set[str]:
        seen = {track.video_id for track in self.history}
        seen.update(track.video_id for track in self.forward_stack)
        seen.update(track.video_id for track in self.up_next)
        if self.current_track:
            seen.add(self.current_track.video_id)
        return seen

    def _warm_queue_streams(self) -> None:
        for track in list(self.up_next)[:3]:
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
            self.list_mode = "search"
            self.results = event["tracks"]
            self.selected_index = 0
            self.list_offset = 0
            self.status_message = f"Loaded {len(self.results)} result(s) for: {event['query']}"
            return

        if kind == "search-error":
            if event["token"] != self.search_token:
                return
            self.loading_search = False
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
            if self.current_track and self.current_track.video_id == event["video_id"]:
                self.status_message = event["message"]
            return

        if kind == "stream-ready":
            video_id = event["video_id"]
            self.pending_streams.discard(video_id)
            self.stream_cache[video_id] = StreamCacheEntry(url=event["url"])
            if self.pending_play_video_id == video_id and self.current_track and self.current_track.video_id == video_id:
                try:
                    self.player.load(event["url"], media_title=self.current_track.title)
                except PlayerError as exc:
                    self.status_message = str(exc)
                else:
                    self.pending_play_video_id = None
                    self.status_message = f"Playing: {self.current_track.title}"
                    self._sync_mpv_playlist()
            return

        if kind == "stream-error":
            video_id = event["video_id"]
            self.pending_streams.discard(video_id)
            if self.pending_play_video_id == video_id:
                self.pending_play_video_id = None
                self.status_message = event["message"]
                self._play_next(auto=True)
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
            elif name == "time-pos":
                self.current_position = float(data or 0.0)
            elif name == "duration":
                self.current_duration = None if data is None else float(data)
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

        header = f"simpleplay  loop:{self.loop_mode.value}  state:{self._player_state_label()}"
        if self.loading_search:
            header += "  search:busy"
        if self.pending_play_video_id:
            header += "  resolve:busy"
        self._safe_addnstr(stdscr, 0, 0, header, width, curses.A_BOLD)

        search_prefix = "/" if self.search_mode else " "
        search_line = f"search {search_prefix}{self.search_query}"
        self._safe_addnstr(stdscr, 1, 0, search_line, width)
        self._safe_addnstr(stdscr, 2, 0, self.status_message, width, curses.A_DIM)

        body_top = 4
        footer_rows = 5
        body_height = max(3, height - body_top - footer_rows)
        self._draw_results(stdscr, body_top, body_height, width)
        self._draw_footer(stdscr, height, width)

        if self.search_mode:
            cursor_x = min(width - 1, len("search /") + len(self.search_query))
            try:
                stdscr.move(1, cursor_x)
            except curses.error:
                pass
        stdscr.refresh()

    def _draw_results(self, stdscr: "curses._CursesWindow", top: int, height: int, width: int) -> None:
        if not self.results:
            message = self._empty_results_message()
            self._safe_addnstr(stdscr, top, 0, message, width)
            return

        self._adjust_list_offset(height)
        end = min(len(self.results), self.list_offset + height)
        for row, idx in enumerate(range(self.list_offset, end)):
            track = self.results[idx]
            prefix = ">" if idx == self.selected_index else " "
            duration = format_duration(track.duration)
            channel = track.channel or "unknown channel"
            line = f"{prefix} {track.title}  [{duration}]  {channel}"
            attr = curses.A_REVERSE if idx == self.selected_index else curses.A_NORMAL
            self._safe_addnstr(stdscr, top + row, 0, line, width, attr)

    def _draw_footer(self, stdscr: "curses._CursesWindow", height: int, width: int) -> None:
        info_top = max(0, height - 4)
        current_line = "now: idle"
        if self.current_track:
            paused = " [paused]" if self.paused else ""
            current_line = f"now: {self.current_track.title}{paused}"
        self._safe_addnstr(stdscr, info_top, 0, current_line, width, curses.A_BOLD)

        progress_line = self._render_progress(width)
        self._safe_addnstr(stdscr, info_top + 1, 0, progress_line, width)

        next_titles = ", ".join(track.title for track in list(self.up_next)[:3]) or "autoplay queue empty"
        self._safe_addnstr(stdscr, info_top + 2, 0, f"up next: {next_titles}", width)
        self._safe_addnstr(stdscr, info_top + 3, 0, HELP_TEXT, width, curses.A_DIM)

    def _render_progress(self, width: int) -> str:
        position = format_duration(self.current_position)
        duration = format_duration(self.current_duration)
        bar_width = max(10, min(30, width - len(position) - len(duration) - 8))

        ratio = 0.0
        if self.current_duration and self.current_duration > 0:
            ratio = min(max(self.current_position / self.current_duration, 0.0), 1.0)

        filled = int(bar_width * ratio)
        bar = "[" + ("#" * filled) + ("-" * (bar_width - filled)) + "]"
        return f"{bar} {position} / {duration}"

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

    def _empty_results_message(self) -> str:
        if self.list_mode == "queue":
            if self.current_track and self.current_track.video_id in self.pending_related:
                return "Loading up next..."
            return "Up next is empty."
        return "No results yet."

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

        self.playlist_tracks = self.history + [self.current_track] + list(self.up_next)
        self.ignore_playlist_pos_until = time.time() + 0.35
        try:
            self.player.sync_playlist(self.history, list(self.up_next))
            self.player.set_media_title(self.current_track.title)
        except PlayerError as exc:
            self.status_message = str(exc)

    def _should_ignore_playlist_pos(self) -> bool:
        return time.time() < self.ignore_playlist_pos_until

    def _handle_playlist_position_change(self, index: int) -> None:
        if index < 0 or index >= len(self.playlist_tracks):
            return

        new_track = self.playlist_tracks[index]
        if self.current_track and new_track.video_id == self.current_track.video_id and index == len(self.history):
            return

        self.history = self.playlist_tracks[:index]
        self.current_track = new_track
        self.up_next = deque(self.playlist_tracks[index + 1 :])
        self.forward_stack.clear()
        self.current_position = 0.0
        self.current_duration = float(new_track.duration) if new_track.duration is not None else None
        self.paused = False
        self.pending_play_video_id = None
        self.awaiting_autoplay = False
        self.list_mode = "queue"
        self.status_message = f"Playing: {new_track.title}"
        self.playlist_tracks = self.history + [self.current_track] + list(self.up_next)
        self._sync_queue_results()
        self._start_related_prefetch(new_track)
        self._fill_queue_from_related(new_track)
        self._warm_queue_streams()

        try:
            self.player.set_media_title(new_track.title)
        except PlayerError as exc:
            self.status_message = str(exc)

    def _maybe_resume_autoplay(self) -> None:
        if not self.awaiting_autoplay or not self.player_idle or not self.up_next:
            return
        next_index = len(self.history) + 1
        if next_index >= len(self.playlist_tracks):
            self.playlist_tracks = self.history + ([self.current_track] if self.current_track else []) + list(self.up_next)
        try:
            self.player.play_playlist_index(len(self.history) + 1)
        except PlayerError as exc:
            self.status_message = str(exc)
            return
        self.awaiting_autoplay = False
