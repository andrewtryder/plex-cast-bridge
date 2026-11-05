"""A virtual speaker for testing locally on Mac without a physical Cast device."""
import logging
import os
import random
import shutil
import signal
import subprocess
import threading
from collections import deque
from pathlib import Path

import requests

log = logging.getLogger(__name__)


class LocalSpeaker:
    """Emulates a Google Home speaker locally on macOS or Linux.

    Downloads selected Plex tracks and plays them through the host audio output
    (using macOS native 'afplay' or Linux audio players). Auto-advances through
    the queue on track completion, mimicking Google Cast MediaStatusListener.
    """

    def __init__(self, directory: Path = Path("test-speaker"), enable_playback: bool = True):
        self.directory = Path(directory)
        self.lock = threading.RLock()
        self.queue = deque()
        self.original = []
        self.current = None
        self.repeat = False
        self.state = "IDLE"
        self.generation = 0
        self.volume = 0.25  # default 25% safe volume
        self.enable_playback = enable_playback and bool(shutil.which("afplay"))
        self._proc = None
        self._watcher_thread = None

    def replace(self, tracks, repeat: bool = False):
        with self.lock:
            self.generation += 1
            self._stop_process()
            self.queue = deque(tracks)
            self.original = list(tracks)
            self.repeat = repeat
            self._next()

    def _stop_process(self):
        if self._proc and self._proc.poll() is None:
            try:
                self._proc.terminate()
                self._proc.wait(timeout=0.5)
            except Exception:
                try:
                    self._proc.kill()
                except Exception:
                    pass
        self._proc = None

    def _next(self):
        if not self.queue and self.repeat and self.original:
            tracks = self.original[:]
            random.shuffle(tracks)
            self.queue.extend(tracks)

        if not self.queue:
            self._stop_process()
            self.current = None
            self.state = "IDLE"
            return

        track = self.queue.popleft()
        media = track.media[0]
        part = media.parts[0]
        url = track._server.url(part.key, includeToken=True)
        extension = str(getattr(media, "container", "mp3")).lower()
        if not extension.isalnum():
            extension = "audio"

        self.directory.mkdir(parents=True, exist_ok=True)
        destination = self.directory / f"current.{extension}"
        temporary = self.directory / ".download.tmp"
        self.state = "BUFFERING"

        try:
            with requests.get(url, stream=True, timeout=(10, 60)) as response:
                response.raise_for_status()
                with temporary.open("wb") as output:
                    for chunk in response.iter_content(chunk_size=65536):
                        output.write(chunk)
            temporary.replace(destination)
        except Exception:
            temporary.unlink(missing_ok=True)
            self.state = "IDLE"
            raise

        self.current = track
        self.output = str(destination)
        self.state = "PLAYING"

        log.info(
            "Emulated Speaker playing: '%s' by '%s' (Queue remaining: %d)",
            track.title,
            getattr(track, "grandparentTitle", "Unknown"),
            len(self.queue),
        )

        if self.enable_playback:
            self._start_playback(destination, self.generation)
        else:
            self.state = "READY"

    def _start_playback(self, audio_path: Path, current_gen: int):
        self._stop_process()
        try:
            # afplay -v takes a float (0 to 1)
            vol_arg = str(max(0.0, min(1.0, float(self.volume))))
            self._proc = subprocess.Popen(
                ["afplay", "-v", vol_arg, str(audio_path)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )

            def watcher():
                proc = self._proc
                if not proc:
                    return
                ret = proc.wait()
                # If finished naturally (code 0) and generation hasn't changed, advance to next track
                with self.lock:
                    if ret == 0 and self.generation == current_gen and self.state == "PLAYING":
                        log.info("Track finished on emulated speaker. Advancing to next track...")
                        self._next()

            self._watcher_thread = threading.Thread(target=watcher, daemon=True)
            self._watcher_thread.start()
        except Exception as exc:
            log.warning("Could not launch afplay audio output: %s", exc)
            self.state = "READY"

    def skip(self):
        with self.lock:
            self.generation += 1
            self._stop_process()
            self._next()

    def stop(self):
        with self.lock:
            self.generation += 1
            self._stop_process()
            self.queue.clear()
            self.original = []
            self.current = None
            self.state = "IDLE"

    def pause(self):
        with self.lock:
            if self.current and self._proc and self._proc.poll() is None:
                try:
                    os.kill(self._proc.pid, signal.SIGSTOP)
                    self.state = "PAUSED"
                    log.info("Emulated Speaker paused.")
                except Exception as exc:
                    log.warning("Failed to pause audio: %s", exc)
            elif self.current:
                self.state = "PAUSED"

    def resume(self):
        with self.lock:
            if self.current and self._proc and self._proc.poll() is None:
                try:
                    os.kill(self._proc.pid, signal.SIGCONT)
                    self.state = "PLAYING"
                    log.info("Emulated Speaker resumed.")
                except Exception as exc:
                    log.warning("Failed to resume audio: %s", exc)
            elif self.current:
                self.state = "PLAYING"

    def set_volume(self, volume: float):
        with self.lock:
            self.volume = max(0.0, min(1.0, float(volume)))
            log.info("Emulated Speaker volume set to %.0f%%", self.volume * 100)

    def adjust_volume(self, delta: float):
        with self.lock:
            self.set_volume(self.volume + float(delta))

    def status(self):
        with self.lock:
            track = self.current
            return {
                "speaker": "Mac Speaker (Emulated)",
                "state": self.state,
                "current_track": (
                    {
                        "title": track.title,
                        "artist": getattr(track, "grandparentTitle", ""),
                        "album": getattr(track, "parentTitle", ""),
                    }
                    if track
                    else None
                ),
                "queue_length": len(self.queue),
                "repeat": self.repeat,
                "output_file": getattr(self, "output", None),
                "volume": self.volume,
            }
