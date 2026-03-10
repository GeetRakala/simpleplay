from __future__ import annotations

import json
import os
import queue
import socket
import subprocess
import threading
import time
import uuid

from .models import Track


class PlayerError(RuntimeError):
    pass


class MPVController:
    def __init__(self, event_queue: "queue.Queue[dict]") -> None:
        self.event_queue = event_queue
        self.process: subprocess.Popen[bytes] | None = None
        self.socket_path: str | None = None
        self.client: socket.socket | None = None
        self._reader_thread: threading.Thread | None = None
        self._send_lock = threading.Lock()
        self._stop_event = threading.Event()

    def start(self) -> None:
        if self.process and self.process.poll() is None:
            return

        self._stop_event.clear()
        self.socket_path = f"/tmp/simpleplay-mpv-{uuid.uuid4().hex}.sock"
        try:
            os.unlink(self.socket_path)
        except FileNotFoundError:
            pass

        self.process = subprocess.Popen(
            [
                "mpv",
                "--idle=yes",
                "--no-video",
                "--force-window=no",
                "--no-terminal",
                "--really-quiet",
                "--term-status-msg=",
                f"--input-ipc-server={self.socket_path}",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        deadline = time.time() + 5
        while time.time() < deadline:
            if self.process.poll() is not None:
                raise PlayerError("mpv exited before opening its IPC socket.")
            if self.socket_path and os.path.exists(self.socket_path):
                break
            time.sleep(0.05)
        else:
            self.shutdown()
            raise PlayerError("mpv IPC socket was not created.")

        self.client = socket.socket(socket.AF_UNIX)
        self.client.settimeout(0.25)
        try:
            self.client.connect(self.socket_path)
        except OSError as exc:
            self.shutdown()
            raise PlayerError(f"Could not connect to mpv IPC socket: {exc}") from exc

        self._observe("core-idle", 1)
        self._observe("time-pos", 2)
        self._observe("duration", 3)
        self._observe("pause", 4)
        self._observe("playlist-pos", 5)

        self._reader_thread = threading.Thread(target=self._reader_loop, daemon=True)
        self._reader_thread.start()

    def load(self, url: str, media_title: str | None = None) -> None:
        self.command(["loadfile", url, "replace"])
        if media_title:
            self.command(["set_property", "force-media-title", media_title])
        self.command(["set_property", "pause", False])

    def set_media_title(self, media_title: str) -> None:
        self.command(["set_property", "force-media-title", media_title])

    def play_playlist_index(self, index: int) -> None:
        self.command(["playlist-play-index", index])

    def sync_playlist(self, history: list[Track], up_next: list[Track]) -> None:
        self.command(["playlist-clear"])
        for index, track in enumerate(history):
            self.command(
                [
                    "loadfile",
                    track.watch_url,
                    "insert-at",
                    index,
                    {"force-media-title": track.title},
                ]
            )
        for track in up_next:
            self.command(
                [
                    "loadfile",
                    track.watch_url,
                    "append",
                    -1,
                    {"force-media-title": track.title},
                ]
            )

    def toggle_pause(self) -> None:
        self.command(["cycle", "pause"])

    def seek(self, seconds: int) -> None:
        self.command(["seek", seconds, "relative"])

    def stop(self) -> None:
        self.command(["stop"])

    def command(self, args: list[object]) -> None:
        if not self.client:
            raise PlayerError("mpv client is not connected.")

        payload = json.dumps({"command": args}).encode("utf-8") + b"\n"
        with self._send_lock:
            try:
                self.client.sendall(payload)
            except OSError as exc:
                raise PlayerError(f"Could not send command to mpv: {exc}") from exc

    def shutdown(self) -> None:
        self._stop_event.set()

        if self.client:
            try:
                with self._send_lock:
                    self.client.sendall(b'{"command":["quit"]}\n')
            except OSError:
                pass

        if self.client:
            try:
                self.client.close()
            except OSError:
                pass
            self.client = None

        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=2)
        self.process = None

        if self.socket_path:
            try:
                os.unlink(self.socket_path)
            except FileNotFoundError:
                pass
            self.socket_path = None

    def _observe(self, property_name: str, request_id: int) -> None:
        self.command(["observe_property", request_id, property_name])

    def _reader_loop(self) -> None:
        if not self.client:
            return

        buffer = b""
        while not self._stop_event.is_set():
            try:
                chunk = self.client.recv(65536)
            except TimeoutError:
                if self.process and self.process.poll() is not None:
                    break
                continue
            except OSError:
                break

            if not chunk:
                if self.process and self.process.poll() is not None:
                    break
                continue

            buffer += chunk
            while b"\n" in buffer:
                raw_line, buffer = buffer.split(b"\n", 1)
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    payload = json.loads(line.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    continue
                self.event_queue.put({"type": "player-event", "payload": payload})

        if not self._stop_event.is_set():
            returncode = None if not self.process else self.process.poll()
            self.event_queue.put({"type": "player-exit", "returncode": returncode})
