from __future__ import annotations

import hashlib
import re
from pathlib import Path

import update_download


class FakeResponse:
    def __init__(self, payload: bytes, status_code: int, headers: dict[str, str], url: str):
        self.payload = payload
        self.status_code = status_code
        self.headers = headers
        self.url = url

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def iter_content(self, chunk_size=1024 * 1024):
        for offset in range(0, len(self.payload), chunk_size):
            yield self.payload[offset:offset + chunk_size]


class FakeSession:
    """Persistent-session stand-in which delegates to the patched requests.get."""
    def __init__(self):
        self.headers = {}

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def get(self, url, **kwargs):
        return update_download.requests.get(url, **kwargs)

    def close(self):
        pass


def test_parallel_range_download_and_sha256(monkeypatch, tmp_path: Path):
    payload = (b"FiercyLauncher-range-download-test-" * 180_000)[:7_500_000]
    total = len(payload)
    url = "https://github.com/MENDI213/FiercyLauncher/releases/download/v9.9.9/test.zip"

    def fake_get(request_url, headers=None, **_kwargs):
        range_header = (headers or {}).get("Range", "")
        if range_header:
            match = re.fullmatch(r"bytes=(\d+)-(\d+)", range_header)
            assert match
            start, end = map(int, match.groups())
            body = payload[start:end + 1]
            return FakeResponse(
                body, 206,
                {"Content-Range": f"bytes {start}-{end}/{total}",
                 "Content-Length": str(len(body))},
                request_url,
            )
        return FakeResponse(payload, 200, {"Content-Length": str(total)}, request_url)

    monkeypatch.setattr(update_download.requests, "get", fake_get)
    monkeypatch.setattr(update_download.requests, "Session", FakeSession)
    target = tmp_path / "release.zip"
    result = update_download.download_verified_asset(
        url,
        hashlib.sha256(payload).hexdigest(),
        target,
        expected_size=total,
        max_workers=4,
    )
    assert result == target
    assert target.read_bytes() == payload
    assert (tmp_path / "release.zip.sha256").read_text("ascii").strip() == hashlib.sha256(payload).hexdigest()


def test_non_range_server_falls_back_to_single_stream(monkeypatch, tmp_path: Path):
    payload = b"single stream fallback" * 4096
    total = len(payload)
    url = "https://github.com/MENDI213/FiercyLauncher/releases/download/v9.9.9/test.zip"
    calls = {"range": 0, "normal": 0}

    def fake_get(request_url, headers=None, **_kwargs):
        if (headers or {}).get("Range"):
            calls["range"] += 1
            return FakeResponse(payload, 200, {"Content-Length": str(total)}, request_url)
        calls["normal"] += 1
        return FakeResponse(payload, 200, {"Content-Length": str(total)}, request_url)

    monkeypatch.setattr(update_download.requests, "get", fake_get)
    target = tmp_path / "fallback.zip"
    update_download.download_verified_asset(
        url, hashlib.sha256(payload).hexdigest(), target, expected_size=total,
    )
    assert target.read_bytes() == payload
    assert calls["range"] == 1
    assert calls["normal"] == 1


def test_invalid_hash_does_not_replace_destination(monkeypatch, tmp_path: Path):
    payload = b"valid payload"
    url = "https://github.com/MENDI213/FiercyLauncher/releases/download/v9.9.9/test.zip"

    def fake_get(request_url, headers=None, **_kwargs):
        if (headers or {}).get("Range"):
            return FakeResponse(b"v", 206,
                                {"Content-Range": f"bytes 0-0/{len(payload)}", "Content-Length": "1"},
                                request_url)
        return FakeResponse(payload, 200, {"Content-Length": str(len(payload))}, request_url)

    monkeypatch.setattr(update_download.requests, "get", fake_get)
    target = tmp_path / "bad.zip"
    try:
        update_download.download_verified_asset(url, "0" * 64, target, expected_size=len(payload))
    except ValueError as exc:
        assert "SHA-256" in str(exc)
    else:
        raise AssertionError("Invalid SHA-256 should fail")
    assert not target.exists()


def test_update_redirect_to_untrusted_host_is_rejected(monkeypatch):
    calls = []

    class RedirectResponse(FakeResponse):
        def __init__(self):
            super().__init__(b"", 302, {"Location": "https://example.invalid/payload.zip"},
                             "https://github.com/MENDI213/FiercyLauncher/releases/download/a.zip")

    def fake_get(url, **kwargs):
        calls.append(url)
        return RedirectResponse()

    monkeypatch.setattr(update_download.requests, "get", fake_get)
    try:
        update_download._safe_get(
            "https://github.com/MENDI213/FiercyLauncher/releases/download/a.zip", timeout=(1, 1)
        )
    except ValueError as exc:
        assert "GitHub" in str(exc)
    else:
        raise AssertionError("Untrusted redirect must be rejected")
    assert len(calls) == 1


def test_speed_tracker_reports_recent_speed_not_lifetime_average(monkeypatch):
    clock = {"now": 0.0}
    monkeypatch.setattr(update_download.time, "monotonic", lambda: clock["now"])
    tracker = update_download._SpeedTracker(started=0.0, window_seconds=3.0)

    # Simulate a long DNS/CDN startup delay before the first chunk arrives.
    clock["now"] = 120.0
    first = tracker.update(12 * 1024 * 1024)
    assert first == 0.0

    # The next 12 MiB arrive in one second: current speed should be 12 MiB/s,
    # not the misleading 0.2 MiB/s lifetime average.
    clock["now"] = 121.0
    current = tracker.update(24 * 1024 * 1024)
    assert current == 12 * 1024 * 1024


def test_speed_tracker_resets_after_retry_restarts_progress(monkeypatch):
    clock = {"now": 0.0}
    monkeypatch.setattr(update_download.time, "monotonic", lambda: clock["now"])
    tracker = update_download._SpeedTracker(started=0.0, window_seconds=3.0)
    clock["now"] = 1.0
    tracker.update(2 * 1024 * 1024)
    clock["now"] = 1.5
    speed = tracker.update(512 * 1024)
    assert speed == 0.0
    clock["now"] = 2.0
    speed = tracker.update(1024 * 1024)
    assert speed == 1024 * 1024


def test_parallel_download_reports_actual_worker_count(monkeypatch, tmp_path: Path):
    payload = b"parallel-range-test" * 2_700_000
    total = len(payload)
    url = "https://github.com/MENDI213/FiercyLauncher/releases/download/v9.9.9/test.zip"
    labels = []
    requested_ranges = []

    def fake_get(request_url, headers=None, **_kwargs):
        range_header = (headers or {}).get("Range", "")
        if range_header:
            requested_ranges.append(range_header)
            match = re.fullmatch(r"bytes=(\d+)-(\d+)", range_header)
            assert match
            start, end = map(int, match.groups())
            body = payload[start:end + 1]
            return FakeResponse(
                body, 206,
                {"Content-Range": f"bytes {start}-{end}/{total}",
                 "Content-Length": str(len(body))},
                request_url,
            )
        return FakeResponse(payload, 200, {"Content-Length": str(total)}, request_url)

    monkeypatch.setattr(update_download.requests, "get", fake_get)
    monkeypatch.setattr(update_download.requests, "Session", FakeSession)
    target = tmp_path / "parallel.zip"
    update_download.download_verified_asset(
        url,
        hashlib.sha256(payload).hexdigest(),
        target,
        expected_size=total,
        max_workers=24,
        progress=lambda _done, _total, _speed, label: labels.append(label),
    )
    assert target.read_bytes() == payload
    assert any("8 потоков" in label for label in labels)
    # One single-byte probe plus every block is fetched through the dynamic queue.
    expected_blocks = (total + 2 * 1024 * 1024 - 1) // (2 * 1024 * 1024)
    assert len(requested_ranges) == expected_blocks + 1
