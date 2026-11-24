"""Release-file filtering with a narrowly scoped exception for certifi's public CA bundle.

Private keys and arbitrary PEM files must never enter a public release archive.
The certifi/cacert.pem file is different: it is a public list of trusted CA
certificates required for HTTPS verification by requests.
"""
from __future__ import annotations

from pathlib import Path, PurePosixPath


def is_certifi_ca_bundle(relative_path: str | Path) -> bool:
    normalized = str(relative_path).replace("\\", "/")
    parts = PurePosixPath(normalized).parts
    return len(parts) >= 2 and parts[-2].casefold() == "certifi" and parts[-1].casefold() == "cacert.pem"


def validate_certifi_ca_bundle(path: str | Path) -> bool:
    """Accept only a plausible public X.509 CA bundle, never a PEM private key."""
    candidate = Path(path)
    try:
        if candidate.is_symlink() or not candidate.is_file():
            return False
        size = candidate.stat().st_size
        if not 2_048 <= size <= 5 * 1024 * 1024:
            return False
        content = candidate.read_bytes()
    except OSError:
        return False

    if b"-----BEGIN PRIVATE KEY-----" in content or b"-----BEGIN RSA PRIVATE KEY-----" in content:
        return False
    return content.count(b"-----BEGIN CERTIFICATE-----") >= 1 and b"-----END CERTIFICATE-----" in content


def should_exclude_pem(relative_path: str | Path, full_path: str | Path) -> bool:
    """Exclude every PEM except a valid certifi CA bundle at the exact package path."""
    if not str(relative_path).lower().endswith(".pem"):
        return False
    if not is_certifi_ca_bundle(relative_path):
        return True
    return not validate_certifi_ca_bundle(full_path)
