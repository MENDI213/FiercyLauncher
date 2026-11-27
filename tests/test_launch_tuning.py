import json
from pathlib import Path

import pytest

from launch_tuning import (
    mark_version_verified,
    merge_jvm_arguments,
    prefer_gui_java,
    recommended_jvm_arguments,
    should_verify_before_launch,
)


def _write_version(root: Path, version: str, parent: str = ""):
    version_dir = root / "versions" / version
    version_dir.mkdir(parents=True, exist_ok=True)
    metadata = {"id": version}
    if parent:
        metadata["inheritsFrom"] = parent
    (version_dir / f"{version}.json").write_text(json.dumps(metadata), encoding="utf-8")


def _write_client_jar(root: Path, version: str):
    jar_dir = root / "versions" / version
    jar_dir.mkdir(parents=True, exist_ok=True)
    (jar_dir / f"{version}.jar").write_bytes(b"fake-client-jar")


def test_recommended_jvm_arguments_are_conservative_and_java_aware():
    assert recommended_jvm_arguments("1.21.1", "neoforge", None) == []
    args = recommended_jvm_arguments("1.21.1", "neoforge", 21)
    assert "-XX:+UseG1GC" in args
    assert "-XX:+ParallelRefProcEnabled" in args
    assert "-Xmx" not in " ".join(args)


def test_custom_jvm_options_override_matching_defaults():
    merged = merge_jvm_arguments(
        ["-XX:+UseG1GC", "-XX:MaxGCPauseMillis=100"],
        "-XX:-UseG1GC -XX:MaxGCPauseMillis=200 -Dexample=true",
    )
    assert merged == [
        "-XX:-UseG1GC",
        "-XX:MaxGCPauseMillis=200",
        "-Dexample=true",
    ]


def test_heap_size_remains_controlled_by_ram_slider():
    with pytest.raises(ValueError, match="ползунком памяти"):
        merge_jvm_arguments(["-XX:+UseG1GC"], "-Xmx4G")


def test_version_verification_cache_avoids_repeated_online_repair(tmp_path):
    _write_version(tmp_path, "neoforge-21.1.228", parent="1.21.1")
    _write_version(tmp_path, "1.21.1")
    _write_client_jar(tmp_path, "1.21.1")

    # Legacy profiles without an explicit verification marker launch from local files;
    # the dedicated Verify button still performs a full repair scan.
    assert not should_verify_before_launch(tmp_path, "neoforge-21.1.228", now=1000)
    mark_version_verified(tmp_path, ["neoforge-21.1.228"], now=1000)
    assert not should_verify_before_launch(tmp_path, "neoforge-21.1.228", now=1100)
    assert should_verify_before_launch(
        tmp_path, "neoforge-21.1.228", max_age_seconds=60, now=1200
    )


def test_missing_client_jar_forces_verification_even_with_cache(tmp_path):
    _write_version(tmp_path, "1.21.1")
    mark_version_verified(tmp_path, ["1.21.1"], now=1000)
    assert should_verify_before_launch(tmp_path, "1.21.1", now=1050)


def test_javaw_is_preferred_when_a_sibling_exists(tmp_path, monkeypatch):
    java = tmp_path / "java.exe"
    javaw = tmp_path / "javaw.exe"
    java.write_bytes(b"java")
    javaw.write_bytes(b"javaw")
    assert prefer_gui_java(str(java), windows=True) == str(javaw.resolve())
