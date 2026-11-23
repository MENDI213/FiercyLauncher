from __future__ import annotations

import sys
import types

try:
    import PyQt5.QtCore  # noqa: F401
except ModuleNotFoundError:
    pyqt5 = types.ModuleType("PyQt5")
    qtcore = types.ModuleType("PyQt5.QtCore")

    class FakeQThread:
        pass

    qtcore.QThread = FakeQThread
    qtcore.pyqtSignal = lambda *_args, **_kwargs: None
    pyqt5.QtCore = qtcore
    sys.modules.setdefault("PyQt5", pyqt5)
    sys.modules.setdefault("PyQt5.QtCore", qtcore)

import github_service


class FakeResponse:
    def __init__(self, payload, status_code=200, headers=None, text=""):
        self._payload = payload
        self.status_code = status_code
        self.headers = headers or {}
        self.text = text
        self.url = "https://api.github.com/repos/MENDI213/FiercyLauncher/releases?per_page=20"

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def close(self):
        pass


def test_release_list_supports_prereleases(monkeypatch):
    calls = []
    release = {
        "tag_name": "v1.0.0.5-beta.1",
        "draft": False,
        "body": "beta",
        "html_url": "https://github.com/MENDI213/FiercyLauncher/releases/tag/v1.0.0.5-beta.1",
        "assets": [{
            "name": "FiercyLauncher-source.zip",
            "size": 1000,
            "digest": "sha256:" + "a" * 64,
            "browser_download_url": "https://github.com/MENDI213/FiercyLauncher/releases/download/v1.0.0.5-beta.1/FiercyLauncher-source.zip",
        }],
    }

    def fake_get(url, **kwargs):
        calls.append((url, kwargs))
        return FakeResponse([release])

    monkeypatch.setattr(github_service.requests, "get", fake_get)
    result = github_service.check_latest_release(
        "https://api.github.com/repos/MENDI213/FiercyLauncher/releases?per_page=20"
    )
    assert result["latest_version"] == "1.0.0.5-beta.1"
    assert result["update_available"] is True
    assert calls[0][1]["allow_redirects"] is False


def test_github_redirect_to_untrusted_host_is_rejected(monkeypatch):
    calls = []

    def fake_get(url, **kwargs):
        calls.append(url)
        return FakeResponse({}, 302, {"Location": "https://example.invalid/secret"})

    monkeypatch.setattr(github_service.requests, "get", fake_get)
    try:
        github_service._safe_github_get(
            "https://raw.githubusercontent.com/MENDI213/FiercyLauncher/main/content/news.json",
            allowed_hosts={"raw.githubusercontent.com"},
        )
    except ValueError as exc:
        assert "небезопасный" in str(exc)
    else:
        raise AssertionError("Untrusted GitHub redirect must be rejected")
    assert len(calls) == 1
