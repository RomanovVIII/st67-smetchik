from __future__ import annotations

import hashlib
import importlib
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
PYTHON = Path(sys.executable)
CLI = ROOT / "skills" / "smetchik" / "scripts" / "smetchik_cli.py"


def run_cli(*args: object) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(PYTHON), str(CLI), *map(str, args)],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
    )


def test_inspect_requires_an_explicit_mode(tmp_path: Path) -> None:
    source = tmp_path / "estimate.xml"
    source.write_text("<root/>", encoding="utf-8")

    completed = run_cli("inspect", source)

    assert completed.returncode == 2
    assert "mode" in completed.stderr.lower()
    assert "traceback" not in (completed.stdout + completed.stderr).lower()


def test_inspect_writes_json_only_to_stdout_and_hashes_input(tmp_path: Path) -> None:
    source = tmp_path / "estimate.bin"
    payload = b"safe-local-input\n"
    source.write_bytes(payload)
    before = sorted(tmp_path.iterdir())

    completed = run_cli("inspect", source, "--mode", "light", "--purpose", "inventory")

    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout)
    assert result["schema_version"] == "smetchik.result.v1"
    assert result["mode"] == "light"
    assert result["purpose"] == "inventory"
    assert result["execution_status"] == "needs_input"
    assert result["input_inventory"][0]["sha256"] == hashlib.sha256(payload).hexdigest()
    assert result["coverage"]["row_level_checked"] is False
    assert result["coverage"]["sampling_strategy"] == "none"
    assert sorted(tmp_path.iterdir()) == before


def test_output_json_is_create_only_and_never_overwritten(tmp_path: Path) -> None:
    source = tmp_path / "estimate.xml"
    source.write_text("<root/>", encoding="utf-8")
    output = tmp_path / "result.json"
    output.write_text("sentinel", encoding="utf-8")

    completed = run_cli(
        "inspect",
        source,
        "--mode",
        "light",
        "--output-json",
        output,
    )

    assert completed.returncode != 0
    assert output.read_text(encoding="utf-8") == "sentinel"
    assert "exist" in (completed.stdout + completed.stderr).lower()
    assert "traceback" not in (completed.stdout + completed.stderr).lower()


def test_output_json_is_private_and_fsyncs_file_and_parent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cli = importlib.import_module("smetchik_cli")
    output = tmp_path / "result.json"
    fsynced_kinds: list[str] = []
    real_fsync = os.fsync

    def recording_fsync(descriptor: int) -> None:
        mode = os.fstat(descriptor).st_mode
        fsynced_kinds.append("directory" if stat.S_ISDIR(mode) else "file")
        real_fsync(descriptor)

    monkeypatch.setattr(os, "fsync", recording_fsync)
    cli._write_result({"schema_version": "test"}, output)

    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    assert fsynced_kinds == ["file", "directory"]
    assert sorted(path.name for path in tmp_path.iterdir()) == ["result.json"]


def test_output_json_rolls_back_if_directory_fsync_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cli = importlib.import_module("smetchik_cli")
    output = tmp_path / "result.json"
    real_fsync = os.fsync

    def failing_directory_fsync(descriptor: int) -> None:
        if stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise OSError("forced directory fsync failure")
        real_fsync(descriptor)

    monkeypatch.setattr(os, "fsync", failing_directory_fsync)

    with pytest.raises(cli.CliError, match="created|published|write"):
        cli._write_result({"schema_version": "test"}, output)

    assert not output.exists()
    assert list(tmp_path.iterdir()) == []


def test_output_json_removes_temporary_file_if_file_fsync_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cli = importlib.import_module("smetchik_cli")
    output = tmp_path / "result.json"
    real_fsync = os.fsync

    def failing_file_fsync(descriptor: int) -> None:
        if stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise OSError("forced file fsync failure")
        real_fsync(descriptor)

    monkeypatch.setattr(os, "fsync", failing_file_fsync)

    with pytest.raises(cli.CliError, match="created|published|write"):
        cli._write_result({"schema_version": "test"}, output)

    assert not output.exists()
    assert list(tmp_path.iterdir()) == []


def test_output_json_size_error_recommends_split_and_retry_without_artifact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cli = importlib.import_module("smetchik_cli")
    output = tmp_path / "result.json"
    monkeypatch.setattr(cli, "MAX_CLI_JSON_BYTES", 64)

    with pytest.raises(cli.CliError) as captured:
        cli._write_result({"blob": "x" * 100}, output)

    message = str(captured.value).casefold()
    assert "split" in message
    assert "retry" in message
    assert "artifact" not in message
    assert not output.exists()


def test_inventory_is_deterministic_for_a_directory(tmp_path: Path) -> None:
    package = tmp_path / "package"
    package.mkdir()
    (package / "b.xml").write_text("<b/>", encoding="utf-8")
    (package / "a.xml").write_text("<a/>", encoding="utf-8")

    first = run_cli("inspect", package, "--mode", "light")
    second = run_cli("inspect", package, "--mode", "light")

    assert first.returncode == second.returncode == 0
    first_result = json.loads(first.stdout)
    second_result = json.loads(second.stdout)
    first_inventory = [
        (item["path"], item["sha256"], item["size_bytes"])
        for item in first_result["input_inventory"]
    ]
    second_inventory = [
        (item["path"], item["sha256"], item["size_bytes"])
        for item in second_result["input_inventory"]
    ]
    assert first_inventory == second_inventory
    assert [item[0] for item in first_inventory] == ["a.xml", "b.xml"]


def test_missing_passport_returns_one_grouped_question_without_false_passes(tmp_path: Path) -> None:
    source = tmp_path / "estimate.xml"
    source.write_text("<root/>", encoding="utf-8")

    completed = run_cli("inspect", source, "--mode", "full")

    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout)
    assert result["execution_status"] == "needs_input"
    assert result["overall_status"] == "needs_clarification"
    assert len(result["questions"]) == 1
    assert result["questions"][0]["grouped"] is True
    assert "purpose" in result["questions"][0]["missing_fields"]
    assert all(check["status"] != "passed" for check in result["checks"])


def test_portable_passport_flags_override_context_json(tmp_path: Path) -> None:
    source = tmp_path / "estimate.xml"
    source.write_text("<root/>", encoding="utf-8")
    context = tmp_path / "context.json"
    context.write_text(
        json.dumps(
            {
                "object": "Старое значение",
                "work_type": "construction",
                "funding_source": "budget",
                "region_or_price_zone": "67",
                "price_level_date": "2026-08-01",
                "calculation_method": "resource_index",
                "stage": "project_documentation",
                "document_set": ["OLD"],
            }
        ),
        encoding="utf-8",
    )

    completed = run_cli(
        "inspect",
        source,
        "--mode",
        "light",
        "--purpose",
        "internal_review",
        "--context-json",
        context,
        "--object-name",
        "Новый объект",
        "--document-set",
        "LSR",
        "--document-set",
        "OSR",
    )

    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout)
    assert result["context"]["object"] == "Новый объект"
    assert result["context"]["document_set"] == ["LSR", "OSR"]
    assert result["questions"] == []


def test_context_json_rejects_symlink_before_reading_target(tmp_path: Path) -> None:
    source = tmp_path / "estimate.xml"
    source.write_text("<root/>", encoding="utf-8")
    target = tmp_path / "private-context.json"
    target.write_text('{"object":"SECRET_CONTEXT"}', encoding="utf-8")
    context = tmp_path / "context.json"
    context.symlink_to(target)

    completed = run_cli(
        "inspect",
        source,
        "--mode",
        "light",
        "--context-json",
        context,
    )

    combined = completed.stdout + completed.stderr
    assert completed.returncode == 2
    assert "symlink" in combined.casefold()
    assert "SECRET_CONTEXT" not in combined
    assert "traceback" not in combined.casefold()


def test_context_json_rejects_oversize_regular_file_before_parsing(tmp_path: Path) -> None:
    source = tmp_path / "estimate.xml"
    source.write_text("<root/>", encoding="utf-8")
    context = tmp_path / "context.json"
    with context.open("wb") as stream:
        stream.truncate(1024 * 1024 + 1)

    completed = run_cli(
        "inspect",
        source,
        "--mode",
        "light",
        "--context-json",
        context,
    )

    combined = completed.stdout + completed.stderr
    assert completed.returncode == 2
    assert "size" in combined.casefold() or "1 mib" in combined.casefold()
    assert "traceback" not in combined.casefold()
