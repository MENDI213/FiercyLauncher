from __future__ import annotations

import os
from pathlib import Path

import pytest

from tls_bootstrap import configure_tls_bundle, resolve_ca_bundle
from release_packaging import is_certifi_ca_bundle, should_exclude_pem


def make_bundle(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes((b"-----BEGIN CERTIFICATE-----\n" + b"A" * 3000 +
                      b"\n-----END CERTIFICATE-----\n") * 2)
    return path


def test_resolve_finds_pyinstaller_internal_bundle_when_certifi_path_is_stale(tmp_path: Path):
    bundle = make_bundle(tmp_path / "app" / "_internal" / "certifi" / "cacert.pem")
    resolved = resolve_ca_bundle(
        certifi_path=tmp_path / "old-drive" / "_internal" / "certifi" / "cacert.pem",
        meipass=tmp_path / "app" / "_internal",
        executable=tmp_path / "app" / "FiercyLauncher.exe",
    )
    assert resolved == bundle.resolve()


def test_configure_repairs_invalid_environment_ca_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    bundle = make_bundle(tmp_path / "_internal" / "certifi" / "cacert.pem")
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", str(tmp_path / "missing.pem"))
    monkeypatch.setenv("CURL_CA_BUNDLE", str(tmp_path / "missing-curl.pem"))
    monkeypatch.setenv("SSL_CERT_FILE", str(tmp_path / "missing-ssl.pem"))
    result = configure_tls_bundle(
        certifi_path=tmp_path / "not-there" / "cacert.pem",
        meipass=tmp_path / "_internal",
        executable=tmp_path / "FiercyLauncher.exe",
    )
    assert result == bundle.resolve()
    assert Path(os.environ["REQUESTS_CA_BUNDLE"]) == bundle.resolve()
    assert Path(os.environ["CURL_CA_BUNDLE"]) == bundle.resolve()
    assert Path(os.environ["SSL_CERT_FILE"]) == bundle.resolve()


def test_valid_user_ca_path_is_preserved(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    bundled = make_bundle(tmp_path / "app" / "certifi" / "cacert.pem")
    custom = make_bundle(tmp_path / "custom-ca.pem")
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", str(custom))
    monkeypatch.delenv("CURL_CA_BUNDLE", raising=False)
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    configure_tls_bundle(certifi_path=bundled)
    assert Path(os.environ["REQUESTS_CA_BUNDLE"]) == custom
    assert Path(os.environ["CURL_CA_BUNDLE"]) == bundled


def test_resolve_fails_closed_if_no_valid_bundle(tmp_path: Path):
    with pytest.raises(FileNotFoundError, match="TLS|HTTPS-сертификатов"):
        resolve_ca_bundle(
            certifi_path=tmp_path / "missing.pem",
            meipass=tmp_path / "empty",
            executable=tmp_path / "app.exe",
        )


def test_release_filter_keeps_only_valid_certifi_ca_bundle(tmp_path: Path):
    bundle = make_bundle(tmp_path / "dist" / "_internal" / "certifi" / "cacert.pem")
    secret = tmp_path / "dist" / "private" / "server-key.pem"
    secret.parent.mkdir(parents=True)
    secret.write_text("-----BEGIN PRIVATE KEY-----\\nsecret\\n-----END PRIVATE KEY-----", encoding="ascii")
    wrong_place = tmp_path / "dist" / "assets" / "cacert.pem"
    wrong_place.parent.mkdir()
    wrong_place.write_bytes(bundle.read_bytes())

    assert is_certifi_ca_bundle("_internal/certifi/cacert.pem")
    assert not should_exclude_pem("_internal/certifi/cacert.pem", bundle)
    assert should_exclude_pem("private/server-key.pem", secret)
    assert should_exclude_pem("assets/cacert.pem", wrong_place)


def test_requests_session_uses_validated_ca_bundle(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import requests

    bundle = make_bundle(tmp_path / "_internal" / "certifi" / "cacert.pem")
    monkeypatch.delenv("REQUESTS_CA_BUNDLE", raising=False)
    monkeypatch.delenv("CURL_CA_BUNDLE", raising=False)
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    configure_tls_bundle(
        certifi_path=tmp_path / "missing" / "cacert.pem",
        meipass=tmp_path / "_internal",
        executable=tmp_path / "FiercyLauncher.exe",
    )
    session = requests.Session()
    settings = session.merge_environment_settings("https://example.invalid", {}, None, None, None)
    assert Path(settings["verify"]) == bundle.resolve()
