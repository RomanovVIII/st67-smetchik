from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import stat
from types import ModuleType, SimpleNamespace

import pytest


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
BOOTSTRAP = PLUGIN_ROOT / "runtime" / "bootstrap.py"


def load_bootstrap() -> ModuleType:
    spec = importlib.util.spec_from_file_location("smetchik_runtime_bootstrap", BOOTSTRAP)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_lock_parser_requires_exact_pins_and_normalizes_names(tmp_path: Path) -> None:
    bootstrap = load_bootstrap()
    lockfile = tmp_path / "requirements.lock"
    lockfile.write_text(
        "# complete lock\n"
        "PDFMiner.SIX==20260107 \\\n"
        "    --hash=sha256:" + "a" * 64 + "\n"
        "typing_extensions==4.16.0 \\\n"
        "    --hash=sha256:" + "b" * 64 + "\n",
        encoding="utf-8",
    )

    assert bootstrap.load_locked_versions(lockfile) == {
        "pdfminer-six": "20260107",
        "typing-extensions": "4.16.0",
    }

    lockfile.write_text("openpyxl>=3.1\n", encoding="utf-8")
    with pytest.raises(SystemExit, match="not an exact"):
        bootstrap.load_locked_versions(lockfile)


def test_lock_parser_rejects_exact_pin_without_hash(tmp_path: Path) -> None:
    bootstrap = load_bootstrap()
    lockfile = tmp_path / "requirements.lock"
    lockfile.write_text("openpyxl==3.1.5\n", encoding="utf-8")

    with pytest.raises(SystemExit, match="SHA-256 hash"):
        bootstrap.load_locked_versions(lockfile)


def test_pip_install_command_requires_hashes_and_binary_wheels() -> None:
    bootstrap = load_bootstrap()

    command = bootstrap.pip_install_command(
        Path("/isolated/python"), Path("/plugin/runtime/requirements.lock")
    )

    assert "--require-hashes" in command
    assert "--only-binary=:all:" in command


def test_public_runtime_lock_is_hashed_and_parseable() -> None:
    bootstrap = load_bootstrap()

    locked = bootstrap.load_locked_versions(
        PLUGIN_ROOT / "runtime" / "requirements.lock"
    )

    assert "openpyxl" in locked
    assert "xmlschema" in locked
    assert len(locked) >= 20
    assert "colorama" not in locked


def test_lock_parser_applies_supported_environment_markers(tmp_path: Path) -> None:
    bootstrap = load_bootstrap()
    lockfile = tmp_path / "requirements.lock"
    lockfile.write_text(
        "colorama==0.4.6 ; sys_platform == 'win32' \\\n"
        "    --hash=sha256:" + "c" * 64 + "\n"
        "cffi==2.1.1 ; platform_python_implementation != 'PyPy' \\\n"
        "    --hash=sha256:" + "d" * 64 + "\n",
        encoding="utf-8",
    )

    assert bootstrap.load_locked_versions(lockfile) == {"cffi": "2.1.1"}


def test_package_probe_checks_transitive_lock_entries(monkeypatch: pytest.MonkeyPatch) -> None:
    bootstrap = load_bootstrap()

    def fake_run(*args: object, **kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {
                    "actual": {"openpyxl": "3.1.5"},
                    "missing": ["et-xmlfile"],
                }
            ),
            stderr="",
        )

    monkeypatch.setattr(bootstrap.subprocess, "run", fake_run)
    result = bootstrap.inspect_packages(
        Path("/isolated/python"),
        {"openpyxl": "3.1.5", "et-xmlfile": "2.0.0"},
    )

    assert result["ok"] is False
    assert result["missing"] == ["et-xmlfile"]


def test_package_probe_rejects_only_unexpected_extra_packages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bootstrap = load_bootstrap()

    def fake_run(*args: object, **kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {
                    "actual": {
                        "openpyxl": "3.1.5",
                        "pip": "25.2",
                        "setuptools": "80.9.0",
                        "wheel": "0.45.1",
                        "unexpected-helper": "1.0.0",
                    },
                    "missing": [],
                }
            ),
            stderr="",
        )

    monkeypatch.setattr(bootstrap.subprocess, "run", fake_run)
    result = bootstrap.inspect_packages(
        Path("/isolated/python"),
        {"openpyxl": "3.1.5"},
    )

    assert result["ok"] is False
    assert result["extra"] == ["unexpected-helper"]


@pytest.mark.parametrize("marker_content", [None, "foreign-runtime\n"])
def test_custom_runtime_rejects_nonempty_unowned_directory_without_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    marker_content: str | None,
) -> None:
    bootstrap = load_bootstrap()
    runtime_dir = tmp_path / "foreign-runtime"
    runtime_dir.mkdir(mode=0o755)
    sentinel = runtime_dir / "sentinel.txt"
    sentinel.write_text("keep", encoding="utf-8")
    if marker_content is not None:
        (runtime_dir / ".smetchik-runtime-owner").write_text(
            marker_content,
            encoding="utf-8",
        )
    before_mode = runtime_dir.stat().st_mode
    before_files = {
        child.name: child.read_bytes()
        for child in runtime_dir.iterdir()
    }

    monkeypatch.setattr(
        bootstrap,
        "parse_args",
        lambda: SimpleNamespace(runtime_dir=runtime_dir, verify_only=False),
    )

    class MutationAttempt(RuntimeError):
        pass

    class FakeEnvBuilder:
        def __init__(self, **kwargs: object) -> None:
            pass

        def create(self, target: Path) -> None:
            raise MutationAttempt(f"unexpected create: {target}")

    monkeypatch.setattr(bootstrap.venv, "EnvBuilder", FakeEnvBuilder)
    monkeypatch.setattr(
        bootstrap.os,
        "chmod",
        lambda *_args: (_ for _ in ()).throw(MutationAttempt("unexpected chmod")),
    )

    with pytest.raises(SystemExit, match="owned|unmarked|runtime"):
        bootstrap.main()

    assert runtime_dir.stat().st_mode == before_mode
    assert {child.name: child.read_bytes() for child in runtime_dir.iterdir()} == before_files


@pytest.mark.parametrize("state", ["new", "empty", "marked"])
def test_custom_runtime_accepts_new_empty_or_explicitly_owned_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    state: str,
) -> None:
    bootstrap = load_bootstrap()
    runtime_dir = tmp_path / state
    if state != "new":
        runtime_dir.mkdir()
    if state == "marked":
        (runtime_dir / ".smetchik-runtime-owner").write_text(
            "smetchik-runtime-v1\n",
            encoding="utf-8",
        )
        (runtime_dir / "owned-data").write_text("safe", encoding="utf-8")

    monkeypatch.setattr(
        bootstrap,
        "parse_args",
        lambda: SimpleNamespace(runtime_dir=runtime_dir, verify_only=False),
    )

    class OwnershipAccepted(RuntimeError):
        pass

    class FakeEnvBuilder:
        def __init__(self, **kwargs: object) -> None:
            pass

        def create(self, target: Path) -> None:
            raise OwnershipAccepted(str(target))

    monkeypatch.setattr(bootstrap.venv, "EnvBuilder", FakeEnvBuilder)

    with pytest.raises(OwnershipAccepted):
        bootstrap.main()


@pytest.mark.parametrize("layout", ["directory", "symlink"])
def test_default_runtime_rejects_nonempty_unowned_root_without_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    layout: str,
) -> None:
    bootstrap = load_bootstrap()
    home = tmp_path / "home"
    share = home / ".local" / "share"
    share.mkdir(parents=True)
    raw_runtime = share / "smetchik"
    if layout == "symlink":
        owned_path = tmp_path / "foreign-runtime"
        owned_path.mkdir(mode=0o755)
        raw_runtime.symlink_to(owned_path, target_is_directory=True)
    else:
        raw_runtime.mkdir(mode=0o755)
        owned_path = raw_runtime
    sentinel = owned_path / "sentinel.txt"
    sentinel.write_text("keep", encoding="utf-8")
    before_mode = stat.S_IMODE(owned_path.stat().st_mode)
    before_files = {
        child.name: child.read_bytes()
        for child in owned_path.iterdir()
    }

    monkeypatch.setattr(
        bootstrap.Path,
        "home",
        classmethod(lambda _cls: home),
    )
    monkeypatch.setattr(
        bootstrap,
        "parse_args",
        lambda: SimpleNamespace(runtime_dir=raw_runtime, verify_only=False),
    )

    class MutationAttempt(RuntimeError):
        pass

    class FakeEnvBuilder:
        def __init__(self, **kwargs: object) -> None:
            pass

        def create(self, target: Path) -> None:
            raise MutationAttempt(f"unexpected create: {target}")

    monkeypatch.setattr(bootstrap.venv, "EnvBuilder", FakeEnvBuilder)
    monkeypatch.setattr(
        bootstrap.os,
        "chmod",
        lambda *_args: (_ for _ in ()).throw(MutationAttempt("unexpected chmod")),
    )

    with pytest.raises(SystemExit, match="owned|unmarked|symlink|runtime"):
        bootstrap.main()

    assert stat.S_IMODE(owned_path.stat().st_mode) == before_mode
    assert {child.name: child.read_bytes() for child in owned_path.iterdir()} == before_files


def test_custom_runtime_rejects_a_symlink_path_component_before_creation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bootstrap = load_bootstrap()
    actual_parent = tmp_path / "actual-parent"
    actual_parent.mkdir()
    linked_parent = tmp_path / "linked-parent"
    linked_parent.symlink_to(actual_parent, target_is_directory=True)
    raw_runtime = linked_parent / "runtime"

    monkeypatch.setattr(
        bootstrap,
        "parse_args",
        lambda: SimpleNamespace(runtime_dir=raw_runtime, verify_only=False),
    )

    class MutationAttempt(RuntimeError):
        pass

    class FakeEnvBuilder:
        def __init__(self, **kwargs: object) -> None:
            pass

        def create(self, target: Path) -> None:
            raise MutationAttempt(f"unexpected create: {target}")

    monkeypatch.setattr(bootstrap.venv, "EnvBuilder", FakeEnvBuilder)

    with pytest.raises(SystemExit, match="symlink"):
        bootstrap.main()

    assert not (actual_parent / "runtime").exists()


@pytest.mark.parametrize("with_python", [False, True])
def test_runtime_rejects_a_symlinked_venv_before_mutation_or_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    with_python: bool,
) -> None:
    bootstrap = load_bootstrap()
    runtime_dir = tmp_path / "owned-runtime"
    runtime_dir.mkdir()
    (runtime_dir / ".smetchik-runtime-owner").write_bytes(
        b"smetchik-runtime-v1\n"
    )
    unrelated = tmp_path / "unrelated-environment"
    unrelated.mkdir(mode=0o755)
    sentinel = unrelated / "sentinel.txt"
    sentinel.write_text("keep", encoding="utf-8")
    if with_python:
        python = unrelated / "bin" / "python"
        python.parent.mkdir()
        python.write_text("not a smetchik runtime", encoding="utf-8")
    (runtime_dir / "venv").symlink_to(unrelated, target_is_directory=True)
    before_mode = stat.S_IMODE(unrelated.stat().st_mode)
    before_files = {
        child.relative_to(unrelated).as_posix(): child.read_bytes()
        for child in unrelated.rglob("*")
        if child.is_file()
    }

    monkeypatch.setattr(
        bootstrap,
        "parse_args",
        lambda: SimpleNamespace(runtime_dir=runtime_dir, verify_only=False),
    )

    class MutationAttempt(RuntimeError):
        pass

    class FakeEnvBuilder:
        def __init__(self, **kwargs: object) -> None:
            pass

        def create(self, target: Path) -> None:
            raise MutationAttempt(f"unexpected create: {target}")

    monkeypatch.setattr(bootstrap.venv, "EnvBuilder", FakeEnvBuilder)
    monkeypatch.setattr(
        bootstrap.os,
        "chmod",
        lambda *_args: (_ for _ in ()).throw(MutationAttempt("unexpected chmod")),
    )
    monkeypatch.setattr(
        bootstrap.subprocess,
        "run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            MutationAttempt("unexpected subprocess")
        ),
    )

    with pytest.raises(SystemExit, match="venv|symlink"):
        bootstrap.main()

    assert sentinel.read_text(encoding="utf-8") == "keep"
    assert stat.S_IMODE(unrelated.stat().st_mode) == before_mode
    assert {
        child.relative_to(unrelated).as_posix(): child.read_bytes()
        for child in unrelated.rglob("*")
        if child.is_file()
    } == before_files


def test_runtime_owner_marker_is_mode_0600_even_with_restrictive_umask(
    tmp_path: Path,
) -> None:
    bootstrap = load_bootstrap()
    runtime_dir = tmp_path / "empty-runtime"
    runtime_dir.mkdir()

    previous_umask = os.umask(0o777)
    try:
        bootstrap.claim_runtime_directory(runtime_dir)
    finally:
        os.umask(previous_umask)

    marker = runtime_dir / ".smetchik-runtime-owner"
    assert stat.S_IMODE(marker.stat().st_mode) == 0o600
    assert marker.read_bytes() == b"smetchik-runtime-v1\n"


def test_runtime_owner_marker_creation_failure_removes_partial_marker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bootstrap = load_bootstrap()
    runtime_dir = tmp_path / "empty-runtime"
    runtime_dir.mkdir()
    monkeypatch.setattr(
        bootstrap.os,
        "fchmod",
        lambda *_args: (_ for _ in ()).throw(OSError("forced fchmod failure")),
    )

    with pytest.raises(SystemExit, match="claim"):
        bootstrap.claim_runtime_directory(runtime_dir)

    assert list(runtime_dir.iterdir()) == []
