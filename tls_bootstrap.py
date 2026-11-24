"""Locate and configure a trusted CA bundle for HTTPS without weakening TLS.

PyInstaller onedir applications are stored under ``_internal``.  The CA bundle
must be shipped with the application; if the normal certifi path is unavailable,
we check the known PyInstaller locations and fail closed if none is valid.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path


_MIN_BUNDLE_SIZE = 2_048
_MAX_BUNDLE_SIZE = 5 * 1024 * 1024


def _is_valid_bundle(path: Path) -> bool:
    try:
        if path.is_symlink() or not path.is_file():
            return False
        size = path.stat().st_size
        if not _MIN_BUNDLE_SIZE <= size <= _MAX_BUNDLE_SIZE:
            return False
        data = path.read_bytes()
    except OSError:
        return False
    if b"-----BEGIN PRIVATE KEY-----" in data or b"-----BEGIN RSA PRIVATE KEY-----" in data:
        return False
    return b"-----BEGIN CERTIFICATE-----" in data and b"-----END CERTIFICATE-----" in data


def resolve_ca_bundle(*, certifi_path: str | Path | None = None,
                      meipass: str | Path | None = None,
                      executable: str | Path | None = None) -> Path:
    candidates: list[Path] = []
    if certifi_path is not None:
        candidates.append(Path(certifi_path))
    else:
        try:
            import certifi
            candidates.append(Path(certifi.where()))
        except Exception:
            pass

    bundle_roots: list[Path] = []
    if meipass is not None:
        bundle_roots.append(Path(meipass))
    elif getattr(sys, "_MEIPASS", None):
        bundle_roots.append(Path(sys._MEIPASS))
    if executable is not None:
        executable_parent = Path(executable).resolve().parent
    else:
        executable_parent = Path(sys.executable).resolve().parent
    bundle_roots.extend((executable_parent / "_internal", executable_parent))

    for root in bundle_roots:
        candidates.extend((root / "certifi" / "cacert.pem", root / "_internal" / "certifi" / "cacert.pem"))

    checked: set[str] = set()
    for candidate in candidates:
        try:
            key = str(candidate.resolve())
        except OSError:
            key = str(candidate)
        if key in checked:
            continue
        checked.add(key)
        if _is_valid_bundle(candidate):
            return candidate.resolve()

    locations = "\n".join(f" - {item}" for item in candidates) or " - (пути не определены)"
    raise FileNotFoundError(
        "В комплекте FiercyLauncher не найден корректный набор HTTPS-сертификатов "
        "certifi/cacert.pem. Переустанови лаунчер из полного официального архива. "
        "Проверка TLS не отключалась. Проверенные пути:\n" + locations
    )


def configure_tls_bundle(*, certifi_path: str | Path | None = None,
                          meipass: str | Path | None = None,
                          executable: str | Path | None = None) -> Path:
    """Point standard Python HTTPS clients at a valid CA bundle.

    A valid user-supplied CA path is respected. Missing or stale environment
    paths are repaired to the bundled public certifi bundle.
    """
    bundle = resolve_ca_bundle(certifi_path=certifi_path, meipass=meipass, executable=executable)
    for variable in ("REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE", "SSL_CERT_FILE"):
        current = os.environ.get(variable, "").strip().strip('"')
        if current:
            existing = Path(current).expanduser()
            if _is_valid_bundle(existing):
                continue
        os.environ[variable] = str(bundle)
    return bundle
