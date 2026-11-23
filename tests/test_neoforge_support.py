import json
from pathlib import Path

import pytest

from neoforge_support import (
    ensure_launcher_profiles,
    filter_stable_neoforge_versions,
    find_installed_neoforge_version,
    neoforge_installer_url,
    neoforge_maven_coordinates,
)


def test_modern_legacy_minecraft_version_mapping():
    assert neoforge_maven_coordinates("1.21.1") == ("neoforge", "21.1.")
    assert neoforge_maven_coordinates("1.20.4") == ("neoforge", "20.4.")
    assert neoforge_maven_coordinates("26.3") == ("neoforge", "26.3.")
    assert neoforge_maven_coordinates("1.20.1") == ("forge", "1.20.1-")


def test_neoforge_stable_versions_for_minecraft_26_3():
    versions = [
        "26.3.0.0-beta", "26.3.0.75", "26.3.0.76", "26.3.0.115",
        "26.2.0.95", "26.3.0.116-beta",
    ]
    assert filter_stable_neoforge_versions("26.3", versions) == [
        "26.3.0.115", "26.3.0.76", "26.3.0.75",
    ]


def test_neoforge_stable_versions_for_1_21_1():
    versions = ["21.1.267", "21.1.228", "21.1.300-beta", "21.2.1"]
    assert filter_stable_neoforge_versions("1.21.1", versions) == ["21.1.267", "21.1.228"]


def test_1_20_1_uses_legacy_forge_maven_coordinate():
    versions = ["1.20.1-47.1.106", "1.20.1-47.1.105", "1.20.1-47.1.107-beta", "20.1.2"]
    assert filter_stable_neoforge_versions("1.20.1", versions) == [
        "1.20.1-47.1.106", "1.20.1-47.1.105",
    ]
    assert neoforge_installer_url("1.20.1", "1.20.1-47.1.106").endswith(
        "/forge/1.20.1-47.1.106/forge-1.20.1-47.1.106-installer.jar"
    )


def test_installer_url_uses_official_neoforge_maven():
    url = neoforge_installer_url("1.21.1", "21.1.267")
    assert url == (
        "https://maven.neoforged.net/releases/net/neoforged/neoforge/"
        "21.1.267/neoforge-21.1.267-installer.jar"
    )


def test_modern_minecraft_installer_url_uses_the_versioned_neoforge_artifact():
    assert neoforge_installer_url("26.3", "26.3.0.76").endswith(
        "/neoforge/26.3.0.76/neoforge-26.3.0.76-installer.jar"
    )


def test_minimal_launcher_profiles_is_created_and_preserved(tmp_path: Path):
    path, created = ensure_launcher_profiles(tmp_path)
    data = json.loads(path.read_text("utf-8"))
    assert created is True
    assert data["profiles"] == {}
    assert data["version"] == 3
    data["profiles"]["existing"] = {"name": "Existing", "type": "custom"}
    path.write_text(json.dumps(data), "utf-8")

    second_path, created_again = ensure_launcher_profiles(tmp_path)
    assert second_path == path
    assert created_again is False
    assert json.loads(path.read_text("utf-8"))["profiles"]["existing"]["name"] == "Existing"


def test_does_not_overwrite_corrupted_launcher_profiles(tmp_path: Path):
    path = tmp_path / "launcher_profiles.json"
    original = "{not valid json"
    path.write_text(original, "utf-8")
    with pytest.raises(RuntimeError, match="повреждён"):
        ensure_launcher_profiles(tmp_path)
    assert path.read_text("utf-8") == original


def test_installed_profile_is_validated_against_selected_minecraft(tmp_path: Path):
    version_id = "neoforge-21.1.267"
    version_dir = tmp_path / "versions" / version_id
    version_dir.mkdir(parents=True)
    (version_dir / f"{version_id}.json").write_text(json.dumps({
        "id": version_id,
        "inheritsFrom": "1.21.1",
        "mainClass": "cpw.mods.bootstraplauncher.BootstrapLauncher",
        "libraries": [{"name": "net.neoforged:neoforge:21.1.267"}],
    }), "utf-8")
    assert find_installed_neoforge_version(tmp_path, "1.21.1", "21.1.267") == version_id
    with pytest.raises(RuntimeError, match="Minecraft 1.20.1"):
        find_installed_neoforge_version(tmp_path, "1.20.1", "21.1.267")
