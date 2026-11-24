from __future__ import annotations

import zipfile
from pathlib import Path

from package_release import add_tree


def make_public_ca_bundle(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes((b"-----BEGIN CERTIFICATE-----\n" + b"A" * 3000 +
                      b"\n-----END CERTIFICATE-----\n") * 2)
    return path


def test_portable_archive_keeps_certifi_ca_but_excludes_private_pems(tmp_path: Path):
    root = tmp_path / "dist" / "FiercyLauncher"
    make_public_ca_bundle(root / "_internal" / "certifi" / "cacert.pem")
    private_key = root / "_internal" / "secrets" / "private-key.pem"
    private_key.parent.mkdir(parents=True)
    private_key.write_text("-----BEGIN PRIVATE KEY-----\nsecret\n-----END PRIVATE KEY-----", encoding="ascii")
    app = root / "FiercyLauncher.exe"
    app.write_bytes(b"dummy exe")

    archive_path = tmp_path / "portable.zip"
    with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as archive:
        add_tree(archive, root, "FiercyLauncher", include_hidden=True)

    with zipfile.ZipFile(archive_path) as archive:
        names = set(archive.namelist())
    assert "FiercyLauncher/_internal/certifi/cacert.pem" in names
    assert "FiercyLauncher/_internal/secrets/private-key.pem" not in names
    assert "FiercyLauncher/FiercyLauncher.exe" in names
