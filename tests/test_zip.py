from __future__ import annotations

import hashlib
import io
import json
import stat
import tempfile
import zipfile
from pathlib import Path

import pytest

from smetchik_engine import inspect_input
from safety_limits import ZipLimits


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_zip(path: Path, entries: list[tuple[str | zipfile.ZipInfo, bytes]]) -> None:
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, payload in entries:
            archive.writestr(name, payload)


def zip_bytes(entries: list[tuple[str, bytes]]) -> bytes:
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_STORED) as archive:
        for name, payload in entries:
            archive.writestr(name, payload)
    return stream.getvalue()


def test_zip_is_privately_extracted_in_deterministic_order_then_cleaned(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = tmp_path / "package.zip"
    write_zip(source, [("b.xml", b"<b/>"), ("folder/a.xml", b"<a/>")])
    before = file_hash(source)
    temp_root = tmp_path / "temporary"
    temp_root.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(temp_root))

    result = inspect_input(source, mode="full")

    details = result["input_inventory"][0]["details"]
    assert details["entry_count"] == 2
    assert [item["path"] for item in details["extracted_inventory"]] == [
        "package.zip!/b.xml",
        "package.zip!/folder/a.xml",
    ]
    assert all(len(item["sha256"]) == 64 for item in details["extracted_inventory"])
    assert list(temp_root.iterdir()) == []
    assert file_hash(source) == before


@pytest.mark.parametrize(
    ("entry_name", "code"),
    [
        ("../outside.xml", "zip_unsafe_path"),
        ("/absolute.xml", "zip_unsafe_path"),
        ("C:/drive.xml", "zip_unsafe_path"),
    ],
)
def test_zip_rejects_traversal_and_absolute_paths(
    tmp_path: Path,
    entry_name: str,
    code: str,
) -> None:
    source = tmp_path / "unsafe.zip"
    write_zip(source, [(entry_name, b"<x/>")])

    result = inspect_input(source, mode="full")

    item = result["input_inventory"][0]
    assert item["extraction_status"] == "rejected"
    assert any(limit["code"] == code for limit in result["limitations"])
    assert not (tmp_path / "outside.xml").exists()


def test_zip_rejects_symlink_entries(tmp_path: Path) -> None:
    source = tmp_path / "symlink.zip"
    link = zipfile.ZipInfo("link.xml")
    link.create_system = 3
    link.external_attr = (stat.S_IFLNK | 0o777) << 16
    write_zip(source, [(link, b"target.xml")])

    result = inspect_input(source, mode="full")

    assert result["input_inventory"][0]["extraction_status"] == "rejected"
    assert any(limit["code"] == "zip_symlink_forbidden" for limit in result["limitations"])


@pytest.mark.parametrize(
    "names",
    [
        ("A.xml", "a.XML"),
        ("caf\N{LATIN SMALL LETTER E WITH ACUTE}.xml", "cafe\N{COMBINING ACUTE ACCENT}.xml"),
    ],
)
def test_zip_rejects_case_and_unicode_normalization_collisions(
    tmp_path: Path,
    names: tuple[str, str],
) -> None:
    source = tmp_path / "collision.zip"
    write_zip(source, [(names[0], b"<a/>"), (names[1], b"<b/>")])

    result = inspect_input(source, mode="full")

    assert result["input_inventory"][0]["extraction_status"] == "rejected"
    assert any(limit["code"] == "zip_path_collision" for limit in result["limitations"])


def test_zip_rejects_excessive_compression_ratio(tmp_path: Path) -> None:
    source = tmp_path / "bomb.zip"
    write_zip(source, [("zeros.bin", b"0" * (2 * 1024 * 1024))])

    result = inspect_input(source, mode="full")

    assert result["input_inventory"][0]["extraction_status"] == "rejected"
    assert any(limit["code"] == "zip_limit_exceeded" for limit in result["limitations"])


def test_zip_rejects_excessive_entry_count(tmp_path: Path) -> None:
    source = tmp_path / "many.zip"
    with zipfile.ZipFile(source, "w", compression=zipfile.ZIP_STORED) as archive:
        for index in range(2001):
            archive.writestr(f"e/{index}.txt", b"")

    result = inspect_input(source, mode="light")

    assert result["input_inventory"][0]["extraction_status"] == "rejected"
    assert any(limit["code"] == "zip_limit_exceeded" for limit in result["limitations"])


def test_nested_zip_uses_one_global_entry_budget(tmp_path: Path, monkeypatch) -> None:
    inner = zip_bytes([("a.xml", b"<a/>"), ("b.xml", b"<b/>"), ("c.xml", b"<c/>")])
    source = tmp_path / "outer.zip"
    write_zip(source, [("inner.zip", inner)])
    monkeypatch.setattr(
        "smetchik_engine.ZIP_LIMITS",
        ZipLimits(
            max_entries=3,
            max_declared_uncompressed_bytes=10_000,
            max_actual_uncompressed_bytes=10_000,
            max_single_uncompressed_bytes=10_000,
            max_compression_ratio=10_000,
        ),
    )

    result = inspect_input(source, mode="full")

    assert result["input_inventory"][0]["extraction_status"] == "rejected"
    limit = next(item for item in result["limitations"] if item["code"] == "zip_global_limit_exceeded")
    assert "outer.zip!/inner.zip" in limit["evidence"][0]["locator"]


def test_nested_zip_uses_one_global_declared_byte_budget(tmp_path: Path, monkeypatch) -> None:
    payload = b"abcdefghij"
    inner = zip_bytes([("a.bin", payload), ("b.bin", payload)])
    source = tmp_path / "outer.zip"
    write_zip(source, [("inner.zip", inner)])
    outer_declared = len(inner)
    monkeypatch.setattr(
        "smetchik_engine.ZIP_LIMITS",
        ZipLimits(
            max_entries=10,
            max_declared_uncompressed_bytes=outer_declared + len(payload),
            max_actual_uncompressed_bytes=100_000,
            max_single_uncompressed_bytes=100_000,
            max_compression_ratio=10_000,
        ),
    )

    result = inspect_input(source, mode="full")

    assert result["input_inventory"][0]["extraction_status"] == "rejected"
    assert any(limit["code"] == "zip_global_limit_exceeded" for limit in result["limitations"])


def test_nested_zip_uses_one_global_actual_byte_budget(tmp_path: Path, monkeypatch) -> None:
    payload = b"abcdefghij"
    inner = zip_bytes([("a.bin", payload)])
    source = tmp_path / "outer.zip"
    write_zip(source, [("inner.zip", inner)])
    monkeypatch.setattr(
        "smetchik_engine.ZIP_LIMITS",
        ZipLimits(
            max_entries=10,
            max_declared_uncompressed_bytes=100_000,
            max_actual_uncompressed_bytes=len(inner) + len(payload) - 1,
            max_single_uncompressed_bytes=100_000,
            max_compression_ratio=10_000,
        ),
    )

    result = inspect_input(source, mode="full")

    assert result["input_inventory"][0]["extraction_status"] == "rejected"
    assert any(limit["code"] == "zip_global_limit_exceeded" for limit in result["limitations"])


def test_nested_paths_and_evidence_include_every_archive_boundary(tmp_path: Path) -> None:
    inner = zip_bytes([("folder/file.xml", b"<Vendor/>")])
    source = tmp_path / "outer.zip"
    write_zip(source, [("inner.zip", inner)])

    result = inspect_input(source, mode="full")

    inner_item = result["input_inventory"][0]["details"]["extracted_inventory"][0]
    nested_item = inner_item["details"]["extracted_inventory"][0]
    assert inner_item["path"] == "outer.zip!/inner.zip"
    assert nested_item["path"] == "outer.zip!/inner.zip!/folder/file.xml"
    limitation = next(item for item in result["limitations"] if item["code"] == "unsupported_schema")
    assert limitation["evidence"][0]["source_path"] == nested_item["path"]
    assert limitation["evidence"][0]["locator"].startswith(nested_item["path"])
    serialized = json.dumps(result, ensure_ascii=False)
    assert "/tmp/smetchik-" not in serialized
    assert str(tmp_path) not in serialized
