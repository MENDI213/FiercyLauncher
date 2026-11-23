from __future__ import annotations

import stat
import zipfile
from pathlib import Path

import pytest

from secure_archive import validate_zip, safe_extract_zip


def make_zip(path: Path, files: dict[str, bytes]) -> Path:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in files.items():
            archive.writestr(name, data)
    return path


def test_valid_archive_extracts_to_staging(tmp_path: Path):
    source = make_zip(tmp_path / "valid.zip", {
        "Pack/level.dat": b"level",
        "Pack/region/r.0.0.mca": b"region",
    })
    target = tmp_path / "staging"
    with zipfile.ZipFile(source) as archive:
        validate_zip(archive)
        safe_extract_zip(archive, target, strip_prefix="Pack")
    assert (target / "level.dat").read_bytes() == b"level"
    assert (target / "region" / "r.0.0.mca").read_bytes() == b"region"


@pytest.mark.parametrize("member", ["../escape.txt", "C:/escape.txt", "/rooted.txt", "folder/../../escape"])
def test_traversal_paths_are_rejected(tmp_path: Path, member: str):
    path = make_zip(tmp_path / "bad.zip", {member: b"no"})
    with zipfile.ZipFile(path) as archive:
        with pytest.raises(ValueError):
            validate_zip(archive)


def test_duplicate_case_insensitive_names_are_rejected(tmp_path: Path):
    path = tmp_path / "duplicate.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("mods/Test.jar", b"a")
        archive.writestr("MODS/test.JAR", b"b")
    with zipfile.ZipFile(path) as archive:
        with pytest.raises(ValueError, match="повторяется"):
            validate_zip(archive)


def test_symlink_entries_are_rejected(tmp_path: Path):
    path = tmp_path / "link.zip"
    info = zipfile.ZipInfo("outside")
    info.create_system = 3
    info.external_attr = (stat.S_IFLNK | 0o777) << 16
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(info, b"../../outside")
    with zipfile.ZipFile(path) as archive:
        with pytest.raises(ValueError, match="Символические ссылки"):
            validate_zip(archive)


def test_windows_reserved_names_are_rejected(tmp_path: Path):
    path = make_zip(tmp_path / "reserved.zip", {"folder/CON.txt": b"no"})
    with zipfile.ZipFile(path) as archive:
        with pytest.raises(ValueError, match="Windows"):
            validate_zip(archive)
