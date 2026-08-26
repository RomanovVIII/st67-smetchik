from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import zipfile
from io import BytesIO
from pathlib import Path

import pytest


RUNTIME = Path(__file__).resolve().parents[1] / "runtime"
sys.path.insert(0, str(RUNTIME))

from schema_manager import (  # noqa: E402
    SchemaManagerError,
    _download,
    default_data_dir,
    fetch_all,
    validate_source_url,
    verify_all,
)


XSD = b"""<?xml version=\"1.0\"?>
<xs:schema xmlns:xs=\"http://www.w3.org/2001/XMLSchema\">
  <xs:element name=\"Estimate\" type=\"xs:string\"/>
</xs:schema>
"""


def sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def zip_payload(*members: tuple[str, bytes]) -> bytes:
    output = BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in members:
            archive.writestr(name, content)
    return output.getvalue()


def write_registry(tmp_path: Path, entries: list[dict[str, object]]) -> Path:
    path = tmp_path / "registry.json"
    path.write_text(
        json.dumps(
            {
                "network_schema_resolution": False,
                "adapters": entries,
                "regional_adapters": [],
            }
        ),
        encoding="utf-8",
    )
    return path


def entry(
    archive: bytes,
    *,
    identifier: str = "official.estimate.1",
    path: str = "official/estimate.xsd",
    source_url: str = "https://schemas.example.test/estimate.zip",
    xsd: bytes = XSD,
    archive_path: str | None = None,
) -> dict[str, object]:
    value: dict[str, object] = {
        "id": identifier,
        "path": path,
        "source_url": source_url,
        "source_sha256": sha256(archive),
        "sha256": sha256(xsd),
    }
    if archive_path is not None:
        value["archive_path"] = archive_path
    return value


def test_default_data_dir_honours_environment_override(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("SMETCHIK_DATA_DIR", str(tmp_path / "portable-store"))

    assert default_data_dir() == tmp_path / "portable-store"


def test_schema_manager_cli_help_is_runnable() -> None:
    completed = subprocess.run(
        [sys.executable, str(RUNTIME / "schema_manager.py"), "--help"],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0
    assert "fetch" in completed.stdout
    assert "verify" in completed.stdout


def test_schema_manager_cli_verify_is_offline_and_reports_missing_store(
    tmp_path: Path,
) -> None:
    environment = os.environ.copy()
    environment["SMETCHIK_DATA_DIR"] = str(tmp_path / "missing-store")
    completed = subprocess.run(
        [sys.executable, str(RUNTIME / "schema_manager.py"), "verify"],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )

    payload = json.loads(completed.stdout)
    assert completed.returncode == 2
    assert payload["full"] is False
    assert payload["entries"]
    assert {entry["status"] for entry in payload["entries"]} == {"missing"}


def test_fetch_all_installs_verified_zip_and_verify_checks_archive_and_xsd(tmp_path: Path) -> None:
    archive = zip_payload(("official/estimate.xsd", XSD))
    registry_path = write_registry(tmp_path, [entry(archive)])
    data_dir = tmp_path / "data"

    fetched = fetch_all(
        registry_path=registry_path,
        data_dir=data_dir,
        downloader=lambda url: archive,
    )
    verified = verify_all(registry_path=registry_path, data_dir=data_dir)

    assert fetched.full is True
    assert fetched.entries[0].status == "installed"
    assert (data_dir / "official" / "estimate.xsd").read_bytes() == XSD
    assert verified.full is True
    assert verified.entries[0].status == "valid"


def test_fetch_uses_archive_path_but_installs_to_public_store_path(
    tmp_path: Path,
) -> None:
    archive = zip_payload(("vendor/estimate.xsd", XSD))
    registry_path = write_registry(
        tmp_path,
        [entry(archive, archive_path="vendor/estimate.xsd")],
    )
    data_dir = tmp_path / "data"

    result = fetch_all(
        registry_path=registry_path,
        data_dir=data_dir,
        downloader=lambda _url: archive,
    )

    assert result.full is True
    assert (data_dir / "official" / "estimate.xsd").read_bytes() == XSD
    assert not (data_dir / "vendor" / "estimate.xsd").exists()


def test_fetch_all_rejects_source_hash_mismatch_without_creating_schema(tmp_path: Path) -> None:
    expected_archive = zip_payload(("official/estimate.xsd", XSD))
    registry_path = write_registry(tmp_path, [entry(expected_archive)])
    data_dir = tmp_path / "data"

    result = fetch_all(
        registry_path=registry_path,
        data_dir=data_dir,
        downloader=lambda url: b"wrong archive bytes",
    )

    assert result.full is False
    assert result.entries[0].status == "failed"
    assert result.entries[0].code == "source_sha256_mismatch"
    assert not (data_dir / "official" / "estimate.xsd").exists()


def test_injected_downloader_cannot_bypass_download_size_limit(
    tmp_path: Path, monkeypatch
) -> None:
    archive = b"123456789"
    registry_path = write_registry(tmp_path, [entry(archive)])
    monkeypatch.setattr("schema_manager.MAX_DOWNLOAD_BYTES", 8)

    result = fetch_all(
        registry_path=registry_path,
        data_dir=tmp_path / "data",
        downloader=lambda _url: archive,
    )

    assert result.full is False
    assert result.entries[0].code == "source_too_large"


@pytest.mark.parametrize(
    ("url", "allowed_hosts"),
    [
        ("http://schemas.example.test/estimate.zip", {"schemas.example.test"}),
        ("https://evil.example.test/estimate.zip", {"schemas.example.test"}),
        ("https://schemas.example.test:444/estimate.zip", {"schemas.example.test"}),
    ],
)
def test_validate_source_url_rejects_untrusted_or_non_https_source(
    url: str,
    allowed_hosts: set[str],
) -> None:
    with pytest.raises(ValueError):
        validate_source_url(url, allowed_hosts)


def test_fetch_all_rejects_corrupt_zip_without_partial_install(tmp_path: Path) -> None:
    corrupt = b"not a zip"
    registry_path = write_registry(tmp_path, [entry(corrupt)])
    data_dir = tmp_path / "data"

    result = fetch_all(
        registry_path=registry_path,
        data_dir=data_dir,
        downloader=lambda url: corrupt,
    )

    assert result.full is False
    assert result.entries[0].code == "archive_invalid"
    assert not (data_dir / "official" / "estimate.xsd").exists()


def test_fetch_all_rejects_zip_path_traversal_without_writing_outside_store(tmp_path: Path) -> None:
    archive = zip_payload(("../escaped.xsd", XSD), ("official/estimate.xsd", XSD))
    registry_path = write_registry(tmp_path, [entry(archive)])
    data_dir = tmp_path / "data"

    result = fetch_all(
        registry_path=registry_path,
        data_dir=data_dir,
        downloader=lambda url: archive,
    )

    assert result.full is False
    assert result.entries[0].code == "archive_path_unsafe"
    assert not (tmp_path / "escaped.xsd").exists()
    assert not (data_dir / "official" / "estimate.xsd").exists()


def test_fetch_all_keeps_zip_install_but_reports_rar_without_unar_as_partial(
    tmp_path: Path,
    monkeypatch,
) -> None:
    zip_archive = zip_payload(("official/estimate.xsd", XSD))
    rar_archive = b"synthetic-rar"
    registry_path = write_registry(
        tmp_path,
        [
            entry(zip_archive),
            entry(
                rar_archive,
                identifier="official.rar.1",
                path="official/rar-estimate.xsd",
                source_url="https://schemas.example.test/estimate.rar",
            ),
        ],
    )
    monkeypatch.setattr("schema_manager.shutil.which", lambda _name: None)
    data_dir = tmp_path / "data"

    result = fetch_all(
        registry_path=registry_path,
        data_dir=data_dir,
        downloader=lambda url: zip_archive if url.endswith(".zip") else rar_archive,
    )

    assert result.full is False
    assert [item.status for item in result.entries] == ["installed", "partial"]
    assert result.entries[1].code == "unar_not_available"
    assert (data_dir / "official" / "estimate.xsd").is_file()
    assert not (data_dir / "official" / "rar-estimate.xsd").exists()


def test_verify_all_detects_tampered_installed_xsd(tmp_path: Path) -> None:
    archive = zip_payload(("official/estimate.xsd", XSD))
    registry_path = write_registry(tmp_path, [entry(archive)])
    data_dir = tmp_path / "data"
    fetch_all(registry_path=registry_path, data_dir=data_dir, downloader=lambda url: archive)
    (data_dir / "official" / "estimate.xsd").write_bytes(b"tampered")

    result = verify_all(registry_path=registry_path, data_dir=data_dir)

    assert result.full is False
    assert result.entries[0].code == "xsd_sha256_mismatch"


def test_verify_rejects_installed_schema_symlink(tmp_path: Path) -> None:
    archive = zip_payload(("official/estimate.xsd", XSD))
    registry_path = write_registry(tmp_path, [entry(archive)])
    data_dir = tmp_path / "data"
    fetch_all(registry_path=registry_path, data_dir=data_dir, downloader=lambda _url: archive)
    external = tmp_path / "external.xsd"
    external.write_bytes(XSD)
    installed = data_dir / "official" / "estimate.xsd"
    installed.unlink()
    installed.symlink_to(external)

    result = verify_all(registry_path=registry_path, data_dir=data_dir)

    assert result.full is False
    assert result.entries[0].code == "store_path_unsafe"


def test_download_rejects_response_larger_than_limit(monkeypatch) -> None:
    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def geturl(self):
            return "https://schemas.example.test/estimate.zip"

        def read(self, size: int):
            return b"x" * size

    class Opener:
        def open(self, *_args, **_kwargs):
            return Response()

    monkeypatch.setattr("schema_manager.MAX_DOWNLOAD_BYTES", 8)
    monkeypatch.setattr("schema_manager.build_opener", lambda *_args: Opener())

    with pytest.raises(SchemaManagerError, match="size limit") as caught:
        _download(
            "https://schemas.example.test/estimate.zip",
            {"schemas.example.test"},
        )

    assert caught.value.code == "source_too_large"


@pytest.mark.parametrize(
    ("constant", "value", "expected_code"),
    [
        ("MAX_ARCHIVE_ENTRIES", 1, "archive_resource_limit"),
        ("MAX_EXPANDED_FILE_BYTES", 3, "archive_resource_limit"),
        ("MAX_EXPANDED_TOTAL_BYTES", 3, "archive_resource_limit"),
        ("MAX_COMPRESSION_RATIO", 0.5, "archive_resource_limit"),
    ],
)
def test_zip_resource_limits_fail_closed(
    tmp_path: Path,
    monkeypatch,
    constant: str,
    value: int | float,
    expected_code: str,
) -> None:
    archive = zip_payload(
        ("official/estimate.xsd", XSD),
        ("official/extra.txt", b"extra"),
    )
    registry_path = write_registry(tmp_path, [entry(archive)])
    data_dir = tmp_path / "data"
    monkeypatch.setattr(f"schema_manager.{constant}", value)

    result = fetch_all(
        registry_path=registry_path,
        data_dir=data_dir,
        downloader=lambda _url: archive,
    )

    assert result.full is False
    assert result.entries[0].code == expected_code
    assert not (data_dir / "official" / "estimate.xsd").exists()


def test_rar_extraction_timeout_is_reported_without_install(
    tmp_path: Path, monkeypatch
) -> None:
    rar_archive = b"synthetic-rar"
    registry_path = write_registry(
        tmp_path,
        [
            entry(
                rar_archive,
                source_url="https://schemas.example.test/estimate.rar",
            )
        ],
    )
    monkeypatch.setattr("schema_manager.shutil.which", lambda _name: "/usr/bin/unar")

    def timeout(*_args, **_kwargs):
        raise subprocess.TimeoutExpired("unar", 1)

    monkeypatch.setattr("schema_manager.subprocess.run", timeout)
    result = fetch_all(
        registry_path=registry_path,
        data_dir=tmp_path / "data",
        downloader=lambda _url: rar_archive,
    )

    assert result.full is False
    assert result.entries[0].code == "archive_timeout"


def test_rar_listing_rejects_unsafe_path_before_extraction(
    tmp_path: Path, monkeypatch
) -> None:
    rar_archive = b"synthetic-rar"
    registry_path = write_registry(
        tmp_path,
        [
            entry(
                rar_archive,
                source_url="https://schemas.example.test/estimate.rar",
            )
        ],
    )
    commands: list[str] = []
    monkeypatch.setattr(
        "schema_manager.shutil.which", lambda name: f"/usr/bin/{name}"
    )

    def fake_run(command, **kwargs):
        commands.append(Path(command[0]).name)
        if Path(command[0]).name == "lsar":
            kwargs["stdout"].write(
                json.dumps(
                    {
                        "lsarContents": [
                            {
                                "XADFileName": "../escaped.xsd",
                                "XADFileSize": 10,
                                "XADCompressedSize": 5,
                            }
                        ]
                    }
                ).encode("utf-8")
            )
            return type("Completed", (), {"returncode": 0})()
        raise AssertionError("unar must not run after unsafe listing")

    monkeypatch.setattr("schema_manager.subprocess.run", fake_run)
    result = fetch_all(
        registry_path=registry_path,
        data_dir=tmp_path / "data",
        downloader=lambda _url: rar_archive,
    )

    assert result.full is False
    assert result.entries[0].code == "archive_path_unsafe"
    assert commands == ["lsar"]


def test_rar_extraction_disables_automatic_enclosing_directory(
    tmp_path: Path, monkeypatch
) -> None:
    rar_archive = b"synthetic-rar"
    registry_path = write_registry(
        tmp_path,
        [
            entry(
                rar_archive,
                source_url="https://schemas.example.test/estimate.rar",
            )
        ],
    )
    monkeypatch.setattr(
        "schema_manager.shutil.which", lambda name: f"/usr/bin/{name}"
    )

    def fake_run(command, **kwargs):
        if Path(command[0]).name == "lsar":
            kwargs["stdout"].write(
                json.dumps(
                    {
                        "lsarContents": [
                            {
                                "XADFileName": "official/estimate.xsd",
                                "XADFileSize": len(XSD),
                                "XADCompressedSize": len(XSD),
                            }
                        ]
                    }
                ).encode("utf-8")
            )
        else:
            assert "-D" in command
            destination = Path(command[command.index("-o") + 1])
            target = destination / "official" / "estimate.xsd"
            target.parent.mkdir(parents=True)
            target.write_bytes(XSD)
        return type("Completed", (), {"returncode": 0})()

    monkeypatch.setattr("schema_manager.subprocess.run", fake_run)
    result = fetch_all(
        registry_path=registry_path,
        data_dir=tmp_path / "data",
        downloader=lambda _url: rar_archive,
    )

    assert result.full is True


def test_failed_second_publish_restores_previous_archive_and_schema(
    tmp_path: Path, monkeypatch
) -> None:
    old_xsd = b"old schema"
    archive = zip_payload(("official/estimate.xsd", XSD))
    registry_path = write_registry(tmp_path, [entry(archive)])
    data_dir = tmp_path / "data"
    old_archive_path = data_dir / "archives" / "official.estimate.1.zip"
    old_schema_path = data_dir / "official" / "estimate.xsd"
    old_archive_path.parent.mkdir(parents=True)
    old_schema_path.parent.mkdir(parents=True)
    old_archive_path.write_bytes(b"old archive")
    old_schema_path.write_bytes(old_xsd)

    import schema_manager

    original_replace = schema_manager._atomic_replace
    calls = 0

    def fail_second(source: Path, target: Path) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise SchemaManagerError("install_failed", "forced second publish failure")
        original_replace(source, target)

    monkeypatch.setattr(schema_manager, "_atomic_replace", fail_second)
    result = fetch_all(
        registry_path=registry_path,
        data_dir=data_dir,
        downloader=lambda _url: archive,
    )

    assert result.full is False
    assert old_archive_path.read_bytes() == b"old archive"
    assert old_schema_path.read_bytes() == old_xsd


def test_symlinked_archive_directory_cannot_redirect_install_outside_store(
    tmp_path: Path,
) -> None:
    archive = zip_payload(("official/estimate.xsd", XSD))
    registry_path = write_registry(tmp_path, [entry(archive)])
    data_dir = tmp_path / "data"
    outside = tmp_path / "outside"
    outside.mkdir()
    data_dir.mkdir()
    (data_dir / "archives").symlink_to(outside, target_is_directory=True)

    result = fetch_all(
        registry_path=registry_path,
        data_dir=data_dir,
        downloader=lambda _url: archive,
    )

    assert result.full is False
    assert result.entries[0].code == "store_path_unsafe"
    assert list(outside.iterdir()) == []


def test_existing_schema_symlink_is_rejected_without_reading_target(
    tmp_path: Path,
) -> None:
    archive = zip_payload(("official/estimate.xsd", XSD))
    registry_path = write_registry(tmp_path, [entry(archive)])
    data_dir = tmp_path / "data"
    external = tmp_path / "external.xsd"
    external.write_bytes(b"do not read or overwrite")
    linked = data_dir / "official" / "estimate.xsd"
    linked.parent.mkdir(parents=True)
    linked.symlink_to(external)

    result = fetch_all(
        registry_path=registry_path,
        data_dir=data_dir,
        downloader=lambda _url: archive,
    )

    assert result.full is False
    assert result.entries[0].code in {"store_path_unsafe", "installed_target_unsafe"}
    assert external.read_bytes() == b"do not read or overwrite"
