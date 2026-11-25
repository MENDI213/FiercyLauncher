from __future__ import annotations

import threading
import time

import pytest

from download_control import DownloadRateLimiter, DownloadTaskControl
from update_download import DownloadCancelled


def test_checkpoint_waits_while_paused_and_resumes():
    control = DownloadTaskControl()
    control.pause()
    completed = threading.Event()
    errors: list[BaseException] = []

    def worker():
        try:
            control.checkpoint()
            completed.set()
        except BaseException as exc:  # pragma: no cover - defensive thread capture
            errors.append(exc)

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    time.sleep(0.12)
    assert not completed.is_set()
    control.resume()
    assert completed.wait(1.0)
    thread.join(timeout=1.0)
    assert not errors


def test_cancel_while_paused_raises_download_cancelled():
    control = DownloadTaskControl()
    control.pause()
    caught = threading.Event()
    errors: list[BaseException] = []

    def worker():
        try:
            control.checkpoint()
        except DownloadCancelled:
            caught.set()
        except BaseException as exc:  # pragma: no cover - defensive thread capture
            errors.append(exc)

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    time.sleep(0.08)
    control.cancel()
    assert caught.wait(1.0)
    thread.join(timeout=1.0)
    assert not errors


def test_unlimited_rate_limiter_does_not_block():
    limiter = DownloadRateLimiter(0)
    control = DownloadTaskControl()
    started = time.monotonic()
    limiter.acquire(4 * 1024 * 1024, control.cancel_event, control.pause_event)
    assert time.monotonic() - started < 0.25


def test_rate_limiter_can_be_changed_while_in_use():
    limiter = DownloadRateLimiter(2)
    assert limiter.limit_mbps == 2
    limiter.set_limit_mbps(1)
    assert limiter.limit_mbps == 1
    limiter.set_limit_mbps(0)
    assert limiter.limit_mbps == 0


def test_cancelled_control_raises_immediately():
    control = DownloadTaskControl()
    control.cancel()
    with pytest.raises(DownloadCancelled):
        control.checkpoint()
