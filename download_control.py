"""Shared pause/cancel controls and global download bandwidth limiting."""
from __future__ import annotations

import threading
import time

from update_download import DownloadCancelled


class DownloadRateLimiter:
    """A shared token bucket: the limit applies to all controlled downloads combined."""

    def __init__(self, limit_mbps: int = 0) -> None:
        self._lock = threading.Lock()
        self._limit_bps = max(0, int(limit_mbps or 0)) * 1024 * 1024
        self._tokens = float(self._limit_bps)
        self._updated = time.monotonic()

    def set_limit_mbps(self, limit_mbps: int) -> None:
        with self._lock:
            self._limit_bps = max(0, min(1000, int(limit_mbps or 0))) * 1024 * 1024
            self._tokens = min(self._tokens, float(self._limit_bps))
            self._updated = time.monotonic()

    @property
    def limit_mbps(self) -> int:
        with self._lock:
            return int(self._limit_bps / (1024 * 1024))

    def acquire(self, amount: int, cancel_event: threading.Event,
                pause_event: threading.Event | None = None) -> None:
        remaining = max(0, int(amount))
        while remaining > 0:
            if cancel_event.is_set():
                raise DownloadCancelled("Загрузка отменена пользователем.")
            while pause_event is not None and pause_event.is_set():
                if cancel_event.is_set():
                    raise DownloadCancelled("Загрузка отменена пользователем.")
                time.sleep(0.08)
            with self._lock:
                limit = self._limit_bps
                if limit <= 0:
                    return
                now = time.monotonic()
                elapsed = max(0.0, now - self._updated)
                self._updated = now
                self._tokens = min(float(limit), self._tokens + elapsed * limit)
                take = min(float(remaining), self._tokens)
                self._tokens -= take
                remaining -= int(take)
                if remaining <= 0:
                    return
                wait_for = max(0.01, min(0.12, (min(float(remaining), float(limit)) - self._tokens) / limit))
            time.sleep(wait_for)


class DownloadTaskControl:
    """Per-task control shared by the task model and its worker thread."""

    def __init__(self) -> None:
        self.cancel_event = threading.Event()
        self.pause_event = threading.Event()

    def cancel(self) -> None:
        self.cancel_event.set()

    def pause(self) -> None:
        self.pause_event.set()

    def resume(self) -> None:
        self.pause_event.clear()

    def checkpoint(self, amount: int = 0, limiter: DownloadRateLimiter | None = None) -> None:
        if self.cancel_event.is_set():
            raise DownloadCancelled("Загрузка отменена пользователем.")
        while self.pause_event.is_set():
            if self.cancel_event.is_set():
                raise DownloadCancelled("Загрузка отменена пользователем.")
            time.sleep(0.08)
        if limiter is not None and amount > 0:
            limiter.acquire(amount, self.cancel_event, self.pause_event)
        if self.cancel_event.is_set():
            raise DownloadCancelled("Загрузка отменена пользователем.")
