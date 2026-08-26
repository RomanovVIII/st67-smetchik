#!/usr/bin/env python3
"""Create or verify the isolated Smetchik runtime.

The script never starts a service, opens a port, or reads user documents.
Package installation happens only without --verify-only.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import venv


SYSTEM_TOOLS = ("pdftotext", "pdftoppm", "tesseract")
ALLOWED_BOOTSTRAP_PACKAGES = frozenset({"pip", "setuptools", "wheel"})
LOCK_ENTRY = re.compile(
    r"^([A-Za-z0-9_.-]+)==([^\s;]+)(?:\s*;\s*([A-Za-z0-9_ .!='\"()<>-]+))?$"
)
LOCK_HASH = re.compile(r"^--hash=sha256:([0-9a-fA-F]{64})$")
LOCK_MARKER_TERM = re.compile(
    r"^(implementation_name|platform_python_implementation|sys_platform)\s*"
    r"(==|!=)\s*(['\"])([^'\"]+)\3$"
)
RUNTIME_OWNER_MARKER = ".smetchik-runtime-owner"
RUNTIME_OWNER_CONTENT = b"smetchik-runtime-v1\n"


def default_runtime_dir() -> Path:
    return Path(
        os.path.abspath(
            os.fspath(Path.home() / ".local" / "share" / "smetchik")
        )
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create or verify the isolated Smetchik Python runtime."
    )
    parser.add_argument(
        "--runtime-dir",
        type=Path,
        default=default_runtime_dir(),
        help="Runtime root; defaults to ~/.local/share/smetchik.",
    )
    parser.add_argument(
        "--verify-only",
        action="store_true",
        help="Do not create or install anything; only verify the existing runtime.",
    )
    return parser.parse_args()


def safe_runtime_dir(value: Path) -> Path:
    try:
        path = Path(os.path.abspath(os.fspath(value.expanduser())))
    except (OSError, RuntimeError) as error:
        raise SystemExit("Refusing an unreadable runtime root.") from error
    for component in (*reversed(path.parents), path):
        try:
            metadata = component.lstat()
        except FileNotFoundError:
            break
        except OSError as error:
            raise SystemExit("Refusing an unreadable runtime path component.") from error
        if stat.S_ISLNK(metadata.st_mode):
            raise SystemExit("Refusing a runtime path with a symlink component.")
        if component != path and not stat.S_ISDIR(metadata.st_mode):
            raise SystemExit("Refusing a runtime path with a non-directory component.")
    home = Path(os.path.abspath(os.fspath(Path.home())))
    forbidden = {Path("/"), home}
    if path in forbidden:
        raise SystemExit("Refusing an unsafe runtime root.")
    return path


def _valid_owner_marker(path: Path) -> bool:
    try:
        metadata = path.lstat()
    except OSError:
        return False
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_size != len(RUNTIME_OWNER_CONTENT):
        return False
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return False
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_dev != metadata.st_dev
            or opened.st_ino != metadata.st_ino
        ):
            return False
        with os.fdopen(descriptor, "rb", closefd=True) as stream:
            descriptor = -1
            return stream.read(len(RUNTIME_OWNER_CONTENT) + 1) == RUNTIME_OWNER_CONTENT
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def ensure_runtime_directory_owned(runtime_dir: Path) -> None:
    try:
        metadata = runtime_dir.lstat()
    except FileNotFoundError:
        return
    except OSError as error:
        raise SystemExit("Refusing an unreadable custom runtime directory.") from error
    if not stat.S_ISDIR(metadata.st_mode):
        raise SystemExit("Refusing a custom runtime path that is not a directory.")
    try:
        has_entries = next(runtime_dir.iterdir(), None) is not None
    except OSError as error:
        raise SystemExit("Refusing an unreadable custom runtime directory.") from error
    if not has_entries:
        return
    if not _valid_owner_marker(runtime_dir / RUNTIME_OWNER_MARKER):
        raise SystemExit("Refusing a nonempty unmarked custom runtime directory.")


def claim_runtime_directory(runtime_dir: Path) -> None:
    marker = runtime_dir / RUNTIME_OWNER_MARKER
    if _valid_owner_marker(marker):
        return
    descriptor = -1
    created: os.stat_result | None = None
    try:
        if next(runtime_dir.iterdir(), None) is not None:
            raise SystemExit("Refusing to claim a nonempty custom runtime directory.")
        descriptor = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        created = os.fstat(descriptor)
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb", closefd=True) as stream:
            descriptor = -1
            stream.write(RUNTIME_OWNER_CONTENT)
            stream.flush()
            os.fsync(stream.fileno())
    except FileExistsError as error:
        raise SystemExit("Refusing an untrusted custom runtime ownership marker.") from error
    except OSError as error:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                pass
        if created is not None:
            try:
                current = marker.lstat()
                if (
                    stat.S_ISREG(current.st_mode)
                    and current.st_dev == created.st_dev
                    and current.st_ino == created.st_ino
                ):
                    marker.unlink()
            except OSError:
                pass
        raise SystemExit("Could not claim the custom runtime directory safely.") from error


def ensure_venv_directory_safe(venv_dir: Path) -> None:
    try:
        metadata = venv_dir.lstat()
    except FileNotFoundError:
        return
    except OSError as error:
        raise SystemExit("Refusing an unreadable venv directory.") from error
    if stat.S_ISLNK(metadata.st_mode):
        raise SystemExit("Refusing a symlinked venv directory.")
    if not stat.S_ISDIR(metadata.st_mode):
        raise SystemExit("Refusing a venv path that is not a directory.")


def runtime_python(venv_dir: Path) -> Path:
    if os.name == "nt":
        return venv_dir / "Scripts" / "python.exe"
    return venv_dir / "bin" / "python"


def load_locked_versions(lockfile: Path) -> dict[str, str]:
    """Read a strict, fully pinned requirements lock file."""
    expected: dict[str, str] = {}
    current: tuple[str, str, int, bool] | None = None
    current_hashes = 0

    def finish_entry() -> None:
        nonlocal current, current_hashes
        if current is None:
            return
        name, version, line_number, applies = current
        if current_hashes == 0:
            raise SystemExit(
                f"Lock file entry {line_number} has no SHA-256 hash."
            )
        if applies:
            normalized = re.sub(r"[-_.]+", "-", name).lower()
            if normalized in expected:
                raise SystemExit(f"Duplicate package in lock file: {name}")
            expected[normalized] = version
        current = None
        current_hashes = 0

    for line_number, raw_line in enumerate(
        lockfile.read_text(encoding="utf-8").splitlines(), 1
    ):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        continued = line.endswith("\\")
        token = line[:-1].rstrip() if continued else line
        hash_match = LOCK_HASH.fullmatch(token)
        if hash_match is not None:
            if current is None:
                raise SystemExit(
                    f"Lock file hash {line_number} does not follow a package pin."
                )
            current_hashes += 1
            if not continued:
                finish_entry()
            continue
        finish_entry()
        match = LOCK_ENTRY.fullmatch(token)
        if match is None:
            raise SystemExit(
                f"Lock file entry {line_number} is not an exact name==version pin."
            )
        name, version, marker = match.groups()
        current = (name, version, line_number, _marker_applies(marker))
        if not continued:
            finish_entry()
    finish_entry()
    if not expected:
        raise SystemExit("Lock file contains no package pins.")
    return expected


def _marker_applies(marker: str | None) -> bool:
    if marker is None:
        return True
    values = {
        "implementation_name": sys.implementation.name,
        "platform_python_implementation": platform.python_implementation(),
        "sys_platform": sys.platform,
    }
    applies = True
    for raw_term in marker.split(" and "):
        match = LOCK_MARKER_TERM.fullmatch(raw_term.strip())
        if match is None:
            raise SystemExit("Lock file contains an unsupported environment marker.")
        variable, operator, _quote, expected_value = match.groups()
        equal = values[variable] == expected_value
        applies = applies and (equal if operator == "==" else not equal)
    return applies


def pip_install_command(python: Path, lockfile: Path) -> list[str]:
    return [
        str(python),
        "-m",
        "pip",
        "install",
        "--disable-pip-version-check",
        "--require-hashes",
        "--only-binary=:all:",
        "--requirement",
        str(lockfile),
    ]


def inspect_packages(python: Path, expected: dict[str, str]) -> dict[str, object]:
    code = (
        "import importlib.metadata,json,re;"
        f"expected={expected!r};"
        "normalize=lambda name:re.sub(r'[-_.]+','-',name).lower();"
        "actual={normalize(dist.metadata['Name']):dist.version "
        "for dist in importlib.metadata.distributions() if dist.metadata.get('Name')};"
        "missing=sorted(name for name in expected if name not in actual);"
        "print(json.dumps({'actual':actual,'missing':missing},sort_keys=True))"
    )
    completed = subprocess.run(
        [str(python), "-c", code],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        return {
            "ok": False,
            "error": "package_probe_failed",
            "detail": completed.stderr.strip()[-500:],
        }
    payload = json.loads(completed.stdout)
    extra = sorted(
        set(payload["actual"]) - set(expected) - ALLOWED_BOOTSTRAP_PACKAGES
    )
    mismatches = {
        name: {
            "expected": expected_version,
            "actual": payload["actual"].get(name),
        }
        for name, expected_version in expected.items()
        if payload["actual"].get(name) != expected_version
    }
    return {
        "ok": not payload["missing"] and not mismatches and not extra,
        "missing": payload["missing"],
        "mismatches": mismatches,
        "extra": extra,
    }


def inspect_system_tools() -> dict[str, object]:
    found = {name: shutil.which(name) for name in SYSTEM_TOOLS}
    result: dict[str, object] = {
        "ok": all(found.values()),
        "found": found,
        "tesseract_languages": [],
    }
    if found["tesseract"]:
        completed = subprocess.run(
            [str(found["tesseract"]), "--list-langs"],
            check=False,
            capture_output=True,
            text=True,
        )
        languages = sorted(
            line.strip()
            for line in completed.stdout.splitlines()
            if line.strip() and not line.lower().startswith("list of available")
        )
        result["tesseract_languages"] = languages
        result["ocr_language_ok"] = {"rus", "eng"}.issubset(languages)
        result["ok"] = bool(result["ok"] and result["ocr_language_ok"])
    else:
        result["ocr_language_ok"] = False
    return result


def main() -> int:
    args = parse_args()
    if sys.version_info < (3, 11):
        raise SystemExit("Python 3.11 or newer is required.")

    runtime_dir = safe_runtime_dir(args.runtime_dir)
    ensure_runtime_directory_owned(runtime_dir)
    venv_dir = runtime_dir / "venv"
    ensure_venv_directory_safe(venv_dir)
    python = runtime_python(venv_dir)
    lockfile = Path(__file__).with_name("requirements.lock")
    locked_versions = load_locked_versions(lockfile)

    if not args.verify_only:
        if not python.is_file():
            runtime_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            safe_runtime_dir(runtime_dir)
            claim_runtime_directory(runtime_dir)
            ensure_venv_directory_safe(venv_dir)
            venv.EnvBuilder(with_pip=True).create(venv_dir)
        safe_runtime_dir(runtime_dir)
        ensure_venv_directory_safe(venv_dir)
        os.chmod(runtime_dir, 0o700)
        os.chmod(venv_dir, 0o700)
        subprocess.run(pip_install_command(python, lockfile), check=True)

    if not python.is_file():
        print(
            json.dumps(
                {
                    "ok": False,
                    "runtime_dir": str(runtime_dir),
                    "error": "runtime_not_found",
                },
                ensure_ascii=False,
            )
        )
        return 2

    packages = inspect_packages(python, locked_versions)
    tools = inspect_system_tools()
    result = {
        "ok": bool(packages.get("ok") and tools.get("ok")),
        "runtime_dir": str(runtime_dir),
        "python": str(python),
        "packages": packages,
        "system_tools": tools,
        "services_started": False,
        "ports_opened": False,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
