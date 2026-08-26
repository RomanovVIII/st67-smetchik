#!/usr/bin/env python3
"""Install and verify official XML schemas for ST67 Smetchik.

User-document inspection never invokes this module.  Schemas are downloaded
only by the explicit ``fetch --all`` command from the repository registry.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import resource
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
import shutil
import stat
import subprocess
import tempfile
from typing import Callable, Iterable
from urllib.parse import urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
import zipfile


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REGISTRY_PATH = PLUGIN_ROOT / "schemas" / "registry.json"
DATA_DIR_ENV = "SMETCHIK_DATA_DIR"
DEFAULT_DATA_DIR = Path("~/.local/share/smetchik/schemas")
HASH_LENGTH = 64
MAX_DOWNLOAD_BYTES = 64 * 1024 * 1024
MAX_ARCHIVE_ENTRIES = 256
MAX_EXPANDED_FILE_BYTES = 32 * 1024 * 1024
MAX_EXPANDED_TOTAL_BYTES = 128 * 1024 * 1024
MAX_COMPRESSION_RATIO = 200.0
RAR_TIMEOUT_SECONDS = 60
RAR_CPU_SECONDS = 45
MAX_RAR_LISTING_BYTES = 4 * 1024 * 1024


class SchemaManagerError(ValueError):
    """A controlled installation or verification failure."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class SchemaEntryResult:
    id: str
    status: str
    code: str | None = None
    message: str | None = None


@dataclass(frozen=True)
class SchemaOperationResult:
    full: bool
    entries: list[SchemaEntryResult]


Downloader = Callable[[str], bytes]


def default_data_dir() -> Path:
    """Return the local schema store, honouring its documented override."""
    configured = os.environ.get(DATA_DIR_ENV)
    value = Path(configured) if configured else DEFAULT_DATA_DIR
    return value.expanduser().resolve(strict=False)


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _require_sha256(value: object, field: str) -> str:
    if not isinstance(value, str) or len(value) != HASH_LENGTH:
        raise SchemaManagerError("registry_invalid", f"Registry field {field} must be a SHA-256 hex digest.")
    try:
        int(value, 16)
    except ValueError as error:
        raise SchemaManagerError("registry_invalid", f"Registry field {field} must be a SHA-256 hex digest.") from error
    return value.lower()


def _safe_relative_path(value: object, *, field: str) -> PurePosixPath:
    if not isinstance(value, str) or not value:
        raise SchemaManagerError("registry_invalid", f"Registry field {field} must be a relative path.")
    if "\\" in value:
        raise SchemaManagerError("registry_invalid", f"Registry field {field} must use a safe relative POSIX path.")
    path = PurePosixPath(value)
    if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        raise SchemaManagerError("registry_invalid", f"Registry field {field} must be a safe relative path.")
    return path


def _entry_id(value: object) -> str:
    if not isinstance(value, str) or not value or any(character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-" for character in value):
        raise SchemaManagerError("registry_invalid", "Registry entry id must contain only letters, digits, dot, underscore, or hyphen.")
    return value


def _normal_url(value: str) -> str:
    parts = urlsplit(value)
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path, parts.query, ""))


def validate_source_url(url: str, allowed_hosts: Iterable[str]) -> str:
    """Validate a source URL against the registry's exact HTTPS host allowlist."""
    parts = urlsplit(url)
    hosts = {host.lower() for host in allowed_hosts}
    if parts.scheme.lower() != "https" or not parts.hostname or parts.username or parts.password:
        raise ValueError("Schema source must use an HTTPS URL without user credentials.")
    try:
        port = parts.port
    except ValueError as error:
        raise ValueError("Schema source URL has an invalid port.") from error
    if port not in (None, 443) or parts.hostname.lower() not in hosts:
        raise ValueError("Schema source host is not allowlisted by the registry.")
    if not parts.path or parts.fragment:
        raise ValueError("Schema source URL is incomplete or contains a fragment.")
    return _normal_url(url)


def load_registry(registry_path: Path = DEFAULT_REGISTRY_PATH) -> tuple[list[dict[str, object]], set[str]]:
    """Read and validate the schema download registry before any network access."""
    try:
        payload = json.loads(registry_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise SchemaManagerError("registry_unavailable", "Schema registry is unavailable or invalid JSON.") from error
    if not isinstance(payload, dict) or payload.get("network_schema_resolution") is not False:
        raise SchemaManagerError("registry_invalid", "Schema registry must disable network schema resolution.")
    raw_entries = payload.get("adapters")
    if not isinstance(raw_entries, list) or not raw_entries:
        raise SchemaManagerError("registry_invalid", "Schema registry contains no adapters.")

    hosts: set[str] = set()
    entries: list[dict[str, object]] = []
    for raw_entry in raw_entries:
        if not isinstance(raw_entry, dict):
            raise SchemaManagerError("registry_invalid", "Each schema registry adapter must be an object.")
        source_url = raw_entry.get("source_url")
        if not isinstance(source_url, str):
            raise SchemaManagerError("registry_invalid", "Registry adapter source_url is required.")
        parts = urlsplit(source_url)
        if parts.hostname:
            hosts.add(parts.hostname.lower())
        entry = dict(raw_entry)
        entry["id"] = _entry_id(raw_entry.get("id"))
        entry["path"] = str(_safe_relative_path(raw_entry.get("path"), field="path"))
        entry["archive_path"] = str(
            _safe_relative_path(
                raw_entry.get("archive_path", raw_entry.get("path")),
                field="archive_path",
            )
        )
        entry["source_sha256"] = _require_sha256(raw_entry.get("source_sha256"), "source_sha256")
        entry["sha256"] = _require_sha256(raw_entry.get("sha256"), "sha256")
        entries.append(entry)
    for entry in entries:
        source_url = str(entry["source_url"])
        try:
            entry["source_url"] = validate_source_url(source_url, hosts)
        except ValueError as error:
            raise SchemaManagerError("registry_invalid", str(error)) from error
    return entries, hosts


def _archive_format(entry: dict[str, object]) -> str:
    suffix = Path(urlsplit(str(entry["source_url"])).path).suffix.lower()
    if suffix not in {".zip", ".rar"}:
        raise SchemaManagerError("registry_invalid", "Schema source must be a ZIP or RAR archive.")
    return suffix.removeprefix(".")


def _archive_path(data_dir: Path, entry: dict[str, object]) -> Path:
    return _path_in_store(
        data_dir,
        f"archives/{entry['id']}.{_archive_format(entry)}",
    )


def _path_in_store(data_dir: Path, relative: str) -> Path:
    root = data_dir.resolve(strict=False)
    target = (root / _safe_relative_path(relative, field="path")).resolve(strict=False)
    try:
        target.relative_to(root)
    except ValueError as error:
        raise SchemaManagerError("store_path_unsafe", "Schema store path escapes SMETCHIK_DATA_DIR.") from error
    return target


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        return None


def _download(url: str, allowed_hosts: set[str]) -> bytes:
    request = Request(url, headers={"User-Agent": "st67-smetchik-schema-manager/0.2"})
    try:
        opener = build_opener(_NoRedirect())
        with opener.open(request, timeout=30) as response:  # noqa: S310 - URL was validated from the built-in registry.
            final_url = response.geturl()
            if _normal_url(final_url) != _normal_url(url):
                raise SchemaManagerError("source_redirect_disallowed", "Schema source redirected to an address not recorded in the registry.")
            validate_source_url(final_url, allowed_hosts)
            payload = response.read(MAX_DOWNLOAD_BYTES + 1)
            if len(payload) > MAX_DOWNLOAD_BYTES:
                raise SchemaManagerError(
                    "source_too_large",
                    "Official schema archive exceeds the download size limit.",
                )
            return payload
    except SchemaManagerError:
        raise
    except OSError as error:
        raise SchemaManagerError("source_download_failed", "Official schema archive could not be downloaded.") from error


def _safe_zip_extract(archive_path: Path, destination: Path) -> None:
    try:
        with zipfile.ZipFile(archive_path) as archive:
            members = archive.infolist()
            if len(members) > MAX_ARCHIVE_ENTRIES:
                raise SchemaManagerError(
                    "archive_resource_limit", "ZIP archive exceeds the entry-count limit."
                )
            expanded_total = 0
            for member in members:
                name = member.filename
                if not name or "\\" in name:
                    raise SchemaManagerError("archive_path_unsafe", "ZIP archive contains an unsafe path.")
                relative = PurePosixPath(name)
                if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
                    raise SchemaManagerError("archive_path_unsafe", "ZIP archive contains an unsafe path.")
                mode = member.external_attr >> 16
                if stat.S_IFMT(mode) == stat.S_IFLNK:
                    raise SchemaManagerError("archive_path_unsafe", "ZIP archive contains a symbolic link.")
                if member.is_dir():
                    continue
                if member.file_size > MAX_EXPANDED_FILE_BYTES:
                    raise SchemaManagerError(
                        "archive_resource_limit", "ZIP member exceeds the expanded-size limit."
                    )
                expanded_total += member.file_size
                if expanded_total > MAX_EXPANDED_TOTAL_BYTES:
                    raise SchemaManagerError(
                        "archive_resource_limit", "ZIP archive exceeds the total expanded-size limit."
                    )
                if member.file_size and (
                    member.compress_size == 0
                    or member.file_size / member.compress_size > MAX_COMPRESSION_RATIO
                ):
                    raise SchemaManagerError(
                        "archive_resource_limit", "ZIP member exceeds the compression-ratio limit."
                    )
            for member in members:
                if member.is_dir():
                    continue
                relative = PurePosixPath(member.filename)
                target = destination.joinpath(*relative.parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(member, "r") as source, target.open("xb") as output:
                    shutil.copyfileobj(source, output)
    except SchemaManagerError:
        raise
    except (OSError, zipfile.BadZipFile) as error:
        raise SchemaManagerError("archive_invalid", "Official ZIP schema archive is damaged or unreadable.") from error


def _safe_rar_extract(archive_path: Path, destination: Path) -> None:
    unar = shutil.which("unar")
    if unar is None:
        raise SchemaManagerError(
            "unar_not_available",
            "RAR schema archive requires the local 'unar' command; this schema remains not installed.",
        )
    lsar = shutil.which("lsar")
    if lsar is None:
        raise SchemaManagerError(
            "lsar_not_available",
            "Safe RAR preflight requires the 'lsar' command shipped with unar; this schema remains not installed.",
        )

    def listing_limits() -> None:
        resource.setrlimit(
            resource.RLIMIT_FSIZE,
            (MAX_RAR_LISTING_BYTES, MAX_RAR_LISTING_BYTES),
        )
        resource.setrlimit(resource.RLIMIT_CPU, (RAR_CPU_SECONDS, RAR_CPU_SECONDS))

    try:
        with tempfile.TemporaryFile() as listing:
            listed = subprocess.run(
                [lsar, "-json", str(archive_path)],
                check=False,
                stdout=listing,
                stderr=subprocess.DEVNULL,
                timeout=RAR_TIMEOUT_SECONDS,
                preexec_fn=listing_limits,
            )
            if listed.returncode != 0:
                raise SchemaManagerError(
                    "archive_invalid", "Official RAR schema archive could not be listed by lsar."
                )
            listing.seek(0)
            raw_listing = listing.read(MAX_RAR_LISTING_BYTES + 1)
    except subprocess.TimeoutExpired as error:
        raise SchemaManagerError(
            "archive_timeout", "RAR listing exceeded the time limit."
        ) from error
    if len(raw_listing) > MAX_RAR_LISTING_BYTES:
        raise SchemaManagerError(
            "archive_resource_limit", "RAR listing exceeds the size limit."
        )
    try:
        listing_payload = json.loads(raw_listing)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise SchemaManagerError(
            "archive_invalid", "Official RAR schema archive returned an invalid listing."
        ) from error
    contents = listing_payload.get("lsarContents") if isinstance(listing_payload, dict) else None
    if not isinstance(contents, list):
        raise SchemaManagerError(
            "archive_invalid", "Official RAR schema archive listing has no contents."
        )
    if len(contents) > MAX_ARCHIVE_ENTRIES:
        raise SchemaManagerError(
            "archive_resource_limit", "RAR archive exceeds the entry-count limit."
        )
    listed_total = 0
    for item in contents:
        if not isinstance(item, dict):
            raise SchemaManagerError("archive_invalid", "RAR listing contains an invalid entry.")
        name = item.get("XADFileName")
        if not isinstance(name, str) or not name or "\\" in name:
            raise SchemaManagerError("archive_path_unsafe", "RAR archive contains an unsafe path.")
        relative = PurePosixPath(name)
        if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
            raise SchemaManagerError("archive_path_unsafe", "RAR archive contains an unsafe path.")
        size = item.get("XADFileSize", 0)
        compressed = item.get("XADCompressedSize")
        if not isinstance(size, int) or size < 0:
            raise SchemaManagerError("archive_invalid", "RAR listing contains an invalid size.")
        if size > MAX_EXPANDED_FILE_BYTES:
            raise SchemaManagerError("archive_resource_limit", "RAR member exceeds the expanded-size limit.")
        listed_total += size
        if listed_total > MAX_EXPANDED_TOTAL_BYTES:
            raise SchemaManagerError("archive_resource_limit", "RAR archive exceeds the total expanded-size limit.")
        if isinstance(compressed, int) and size and (
            compressed <= 0 or size / compressed > MAX_COMPRESSION_RATIO
        ):
            raise SchemaManagerError("archive_resource_limit", "RAR member exceeds the compression-ratio limit.")
    def apply_limits() -> None:
        resource.setrlimit(
            resource.RLIMIT_FSIZE,
            (MAX_EXPANDED_FILE_BYTES, MAX_EXPANDED_FILE_BYTES),
        )
        resource.setrlimit(resource.RLIMIT_CPU, (RAR_CPU_SECONDS, RAR_CPU_SECONDS))

    try:
        completed = subprocess.run(
            [unar, "-quiet", "-D", "-o", str(destination), str(archive_path)],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=RAR_TIMEOUT_SECONDS,
            preexec_fn=apply_limits,
        )
    except subprocess.TimeoutExpired as error:
        raise SchemaManagerError(
            "archive_timeout", "RAR extraction exceeded the time limit."
        ) from error
    if completed.returncode != 0:
        raise SchemaManagerError("archive_invalid", "Official RAR schema archive could not be extracted by unar.")
    extracted_count = 0
    expanded_total = 0
    for extracted in destination.rglob("*"):
        try:
            extracted.resolve(strict=False).relative_to(destination.resolve())
        except ValueError as error:
            raise SchemaManagerError("archive_path_unsafe", "RAR archive extracted a path outside its staging directory.") from error
        if extracted.is_symlink():
            raise SchemaManagerError("archive_path_unsafe", "RAR archive extracted a symbolic link.")
        if extracted.is_file():
            extracted_count += 1
            try:
                size = extracted.stat().st_size
            except OSError as error:
                raise SchemaManagerError(
                    "archive_invalid", "RAR output could not be inspected safely."
                ) from error
            expanded_total += size
            if (
                extracted_count > MAX_ARCHIVE_ENTRIES
                or size > MAX_EXPANDED_FILE_BYTES
                or expanded_total > MAX_EXPANDED_TOTAL_BYTES
            ):
                raise SchemaManagerError(
                    "archive_resource_limit", "RAR output exceeds extraction limits."
                )


def _atomic_replace(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as output, source.open("rb") as input_stream:
            shutil.copyfileobj(input_stream, output)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, target)
    except OSError as error:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise SchemaManagerError("install_failed", "Verified schema could not be installed atomically.") from error


def _snapshot(path: Path, *, max_bytes: int) -> bytes | None:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return None
    except OSError as error:
        raise SchemaManagerError(
            "installed_target_unsafe", "Existing schema target cannot be inspected safely."
        ) from error
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > max_bytes:
        raise SchemaManagerError(
            "installed_target_unsafe", "Existing schema target is not a bounded regular file."
        )
    descriptor = -1
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_dev != metadata.st_dev
            or opened.st_ino != metadata.st_ino
            or opened.st_size > max_bytes
        ):
            raise SchemaManagerError(
                "installed_target_unsafe", "Existing schema target changed during safe inspection."
            )
        with os.fdopen(descriptor, "rb", closefd=True) as stream:
            descriptor = -1
            payload = stream.read(max_bytes + 1)
        if len(payload) > max_bytes:
            raise SchemaManagerError(
                "installed_target_unsafe", "Existing schema target exceeds the size limit."
            )
        return payload
    except OSError as error:
        raise SchemaManagerError(
            "installed_target_unsafe", "Existing schema target cannot be read safely."
        ) from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _restore_snapshot(path: Path, payload: bytes | None) -> None:
    if payload is None:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".rollback", dir=path.parent
    )
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _fetch_entry(
    entry: dict[str, object],
    *,
    data_dir: Path,
    allowed_hosts: set[str],
    downloader: Downloader | None,
) -> SchemaEntryResult:
    identifier = str(entry["id"])
    source_url = str(entry["source_url"])
    try:
        payload = downloader(source_url) if downloader is not None else _download(source_url, allowed_hosts)
        if len(payload) > MAX_DOWNLOAD_BYTES:
            raise SchemaManagerError(
                "source_too_large", "Official schema archive exceeds the download size limit."
            )
        if _sha256(payload) != entry["source_sha256"]:
            raise SchemaManagerError("source_sha256_mismatch", "Downloaded archive SHA-256 does not match the registry.")
        archive_format = _archive_format(entry)
        with tempfile.TemporaryDirectory(prefix=".smetchik-schema-", dir=data_dir.parent) as temporary_dir:
            staging = Path(temporary_dir)
            archive = staging / f"{identifier}.{archive_format}"
            archive.write_bytes(payload)
            extracted = staging / "extracted"
            extracted.mkdir()
            if archive_format == "zip":
                _safe_zip_extract(archive, extracted)
            else:
                _safe_rar_extract(archive, extracted)
            archive_path = entry.get("archive_path", entry["path"])
            expected_xsd = extracted.joinpath(
                *_safe_relative_path(archive_path, field="archive_path").parts
            )
            if not expected_xsd.is_file():
                raise SchemaManagerError("xsd_missing_from_archive", "Official archive does not contain the expected XSD path.")
            if _sha256(expected_xsd.read_bytes()) != entry["sha256"]:
                raise SchemaManagerError("xsd_sha256_mismatch", "Extracted XSD SHA-256 does not match the registry.")
            archive_target = _archive_path(data_dir, entry)
            xsd_target = _path_in_store(data_dir, str(entry["path"]))
            archive_before = _snapshot(archive_target, max_bytes=MAX_DOWNLOAD_BYTES)
            xsd_before = _snapshot(xsd_target, max_bytes=MAX_EXPANDED_FILE_BYTES)
            try:
                _atomic_replace(archive, archive_target)
                _atomic_replace(expected_xsd, xsd_target)
            except SchemaManagerError:
                try:
                    _restore_snapshot(archive_target, archive_before)
                    _restore_snapshot(xsd_target, xsd_before)
                except OSError as rollback_error:
                    raise SchemaManagerError(
                        "install_rollback_failed",
                        "Schema install failed and the previous entry could not be restored.",
                    ) from rollback_error
                raise
        return SchemaEntryResult(identifier, "installed")
    except SchemaManagerError as error:
        status = "partial" if error.code in {"unar_not_available", "lsar_not_available"} else "failed"
        return SchemaEntryResult(identifier, status, error.code, str(error))
    except OSError as error:
        return SchemaEntryResult(identifier, "failed", "install_failed", "Schema install failed unexpectedly.")


def fetch_all(
    *,
    registry_path: Path = DEFAULT_REGISTRY_PATH,
    data_dir: Path | None = None,
    downloader: Downloader | None = None,
) -> SchemaOperationResult:
    """Fetch every supported schema from the built-in registry's URLs."""
    target_dir = (data_dir or default_data_dir()).expanduser().resolve(strict=False)
    entries, allowed_hosts = load_registry(registry_path)
    target_dir.mkdir(parents=True, exist_ok=True)
    results = [
        _fetch_entry(entry, data_dir=target_dir, allowed_hosts=allowed_hosts, downloader=downloader)
        for entry in entries
    ]
    return SchemaOperationResult(all(result.status == "installed" for result in results), results)


def _verify_entry(entry: dict[str, object], data_dir: Path) -> SchemaEntryResult:
    identifier = str(entry["id"])
    try:
        archive = _archive_path(data_dir, entry)
        xsd = _path_in_store(data_dir, str(entry["path"]))
    except SchemaManagerError as error:
        return SchemaEntryResult(identifier, "failed", error.code, str(error))
    try:
        archive_payload = _snapshot(archive, max_bytes=MAX_DOWNLOAD_BYTES)
        if archive_payload is None:
            return SchemaEntryResult(identifier, "missing", "archive_not_installed", "Verified source archive is not installed.")
        if _sha256(archive_payload) != entry["source_sha256"]:
            return SchemaEntryResult(identifier, "failed", "source_sha256_mismatch", "Installed archive SHA-256 does not match the registry.")
    except SchemaManagerError as error:
        return SchemaEntryResult(identifier, "failed", error.code, str(error))
    try:
        xsd_payload = _snapshot(xsd, max_bytes=MAX_EXPANDED_FILE_BYTES)
        if xsd_payload is None:
            return SchemaEntryResult(identifier, "missing", "schema_not_installed", "Verified XSD is not installed.")
        if _sha256(xsd_payload) != entry["sha256"]:
            return SchemaEntryResult(identifier, "failed", "xsd_sha256_mismatch", "Installed XSD SHA-256 does not match the registry.")
    except SchemaManagerError as error:
        return SchemaEntryResult(identifier, "failed", error.code, str(error))
    return SchemaEntryResult(identifier, "valid")


def verify_all(
    *,
    registry_path: Path = DEFAULT_REGISTRY_PATH,
    data_dir: Path | None = None,
) -> SchemaOperationResult:
    """Verify every expected locally installed archive and XSD without network access."""
    target_dir = (data_dir or default_data_dir()).expanduser().resolve(strict=False)
    entries, _allowed_hosts = load_registry(registry_path)
    results = [_verify_entry(entry, target_dir) for entry in entries]
    return SchemaOperationResult(all(result.status == "valid" for result in results), results)


def _json_result(result: SchemaOperationResult) -> str:
    return json.dumps(
        {"full": result.full, "entries": [asdict(entry) for entry in result.entries]},
        ensure_ascii=False,
        sort_keys=True,
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fetch and verify official ST67 Smetchik XML schemas.")
    commands = parser.add_subparsers(dest="command", required=True)
    fetch = commands.add_parser("fetch", help="Download verified schema archives from the built-in registry.")
    fetch.add_argument("--all", action="store_true", required=True, help="Fetch every registered schema.")
    commands.add_parser("verify", help="Verify locally installed schema archives and XSD files without network access.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        result = fetch_all() if args.command == "fetch" else verify_all()
    except SchemaManagerError as error:
        print(json.dumps({"full": False, "error": error.code, "message": str(error)}, ensure_ascii=False))
        return 2
    print(_json_result(result))
    return 0 if result.full else 2


if __name__ == "__main__":
    raise SystemExit(main())
