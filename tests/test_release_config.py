from __future__ import annotations

import json
from pathlib import Path

from packaging.version import Version

from version import LAUNCHER_VERSION

ROOT = Path(__file__).resolve().parents[1]


def test_public_release_config_matches_client_version():
    config = json.loads((ROOT / "content" / "launcher.json").read_text(encoding="utf-8"))
    launcher = config["launcher"]
    assert Version(launcher["latest_version"]) == Version(LAUNCHER_VERSION)
    assert Version(launcher["minimum_version"]) <= Version(LAUNCHER_VERSION)


def test_public_news_matches_current_beta():
    news = json.loads((ROOT / "content" / "news.json").read_text(encoding="utf-8"))
    assert news["items"]
    assert LAUNCHER_VERSION in news["items"][0]["title"]
