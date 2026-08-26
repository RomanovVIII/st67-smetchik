from __future__ import annotations

import os
import re
import stat
import tempfile
import unicodedata
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Callable

from safety_limits import ZIP_LIMITS, ZipLimits


NestedInspector = Callable[
    [Path, str],
    tuple[dict[str, Any], list[dict[str, Any]], int, int],
]


class ZipRejected(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class ZipBudget:
    limits: ZipLimits
    entries: int = 0
    declared_bytes: int = 0
    actual_bytes: int = 0
    exhausted: bool = False
    failure_path: str | None = None
    failure_message: str | None = None

    def _reject(self, message: str, display_path: str) -> None:
        self.exhausted = True
        self.failure_path = display_path
        self.failure_message = message
        raise ZipRejected("zip_global_limit_exceeded", message)

    def reserve(self, entry_count: int, declared_bytes: int, display_path: str) -> None:
        if self.entries + entry_count > self.limits.max_entries:
            self._reject("Дерево ZIP превышает общий лимит числа записей.", display_path)
        if self.declared_bytes + declared_bytes > self.limits.max_declared_uncompressed_bytes:
            self._reject("Дерево ZIP превышает общий лимит объявленного распакованного размера.", display_path)
        self.entries += entry_count
        self.declared_bytes += declared_bytes

    def consume_actual(self, byte_count: int, display_path: str) -> None:
        if self.actual_bytes + byte_count > self.limits.max_actual_uncompressed_bytes:
            self._reject("Дерево ZIP превышает общий лимит фактически распакованных данных.", display_path)
        self.actual_bytes += byte_count

    def raise_if_exhausted(self) -> None:
        if self.exhausted:
            raise ZipRejected(
                "zip_global_limit_exceeded",
                self.failure_message or "Дерево ZIP превысило общий безопасный бюджет.",
            )

    def snapshot(self) -> dict[str, int]:
        return {
            "entries": self.entries,
            "declared_uncompressed_bytes": self.declared_bytes,
            "actual_uncompressed_bytes": self.actual_bytes,
        }


def _normalized_member_name(name: str) -> str:
    replaced = name.replace("\\", "/")
    if (
        not replaced
        or replaced.startswith("/")
        or re.match(r"^[A-Za-z]:/", replaced)
    ):
        raise ZipRejected("zip_unsafe_path", "ZIP содержит абсолютный или пустой путь.")
    parts = PurePosixPath(replaced).parts
    if any(part in {"", ".", ".."} for part in parts):
        raise ZipRejected("zip_unsafe_path", "ZIP содержит небезопасный компонент пути.")
    return "/".join(parts)


def _is_symlink(info: zipfile.ZipInfo) -> bool:
    mode = (info.external_attr >> 16) & 0xFFFF
    return info.create_system == 3 and stat.S_ISLNK(mode)


def _validate_entries(
    entries: list[zipfile.ZipInfo],
    limits: ZipLimits,
    budget: ZipBudget,
    display_path: str,
) -> list[tuple[zipfile.ZipInfo, str]]:
    if len(entries) > limits.max_entries:
        raise ZipRejected("zip_limit_exceeded", "ZIP превышает лимит числа записей.")
    seen: set[str] = set()
    total_size = 0
    validated: list[tuple[zipfile.ZipInfo, str]] = []
    for info in entries:
        name = _normalized_member_name(info.filename)
        collision_key = unicodedata.normalize("NFC", name.rstrip("/")).casefold()
        if collision_key in seen:
            raise ZipRejected(
                "zip_path_collision",
                "ZIP содержит конфликт путей после Unicode/case-нормализации.",
            )
        seen.add(collision_key)
        if _is_symlink(info):
            raise ZipRejected("zip_symlink_forbidden", "Символьные ссылки в ZIP запрещены.")
        if info.flag_bits & 0x1:
            raise ZipRejected("zip_encrypted_entry", "Зашифрованные записи ZIP не извлекаются.")
        if info.file_size > limits.max_single_uncompressed_bytes:
            raise ZipRejected("zip_limit_exceeded", "Запись ZIP превышает допустимый размер.")
        total_size += info.file_size
        if total_size > limits.max_declared_uncompressed_bytes:
            raise ZipRejected("zip_limit_exceeded", "ZIP превышает общий допустимый размер.")
        if info.file_size:
            ratio = info.file_size / max(info.compress_size, 1)
            if ratio > limits.max_compression_ratio:
                raise ZipRejected("zip_limit_exceeded", "ZIP превышает допустимую степень сжатия.")
        validated.append((info, name))
    budget.reserve(len(entries), total_size, display_path)
    return sorted(validated, key=lambda pair: pair[1])


def _rejected(
    path: Path,
    code: str,
    message: str,
    *,
    display_path: str | None = None,
    budget: ZipBudget | None = None,
) -> tuple[dict[str, Any], str, list[dict[str, Any]], int]:
    evidence_path = (
        budget.failure_path
        if budget is not None and code == "zip_global_limit_exceeded" and budget.failure_path
        else display_path or path.name
    )
    return (
        {
            "format": "zip",
            "entry_count": 0,
            "extracted_inventory": [],
            "global_budget": budget.snapshot() if budget is not None else None,
        },
        "rejected",
        [
            {
                "code": code,
                "message": message,
                "evidence": [{"source_path": evidence_path, "locator": evidence_path}],
            }
        ],
        0,
    )


def extract_zip(
    path: Path,
    *,
    inspect_nested: NestedInspector,
    limits: ZipLimits = ZIP_LIMITS,
    budget: ZipBudget | None = None,
    display_path: str | None = None,
) -> tuple[dict[str, Any], str, list[dict[str, Any]], int]:
    effective_budget = budget or ZipBudget(limits)
    effective_limits = effective_budget.limits
    archive_display = display_path or path.name
    try:
        with zipfile.ZipFile(path) as archive:
            entries = archive.infolist()
            validated = _validate_entries(
                entries,
                effective_limits,
                effective_budget,
                archive_display,
            )
            with tempfile.TemporaryDirectory(prefix="smetchik-zip-") as temporary:
                os.chmod(temporary, 0o700)
                root = Path(temporary).resolve()
                extracted: list[tuple[Path, str]] = []
                actual_total = 0
                for info, name in validated:
                    target = root.joinpath(*PurePosixPath(name).parts)
                    if not target.resolve(strict=False).is_relative_to(root):
                        raise ZipRejected("zip_unsafe_path", "Путь ZIP выходит из временного каталога.")
                    if info.is_dir():
                        target.mkdir(parents=True, exist_ok=True, mode=0o700)
                        os.chmod(target, 0o700)
                        continue
                    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                    for parent in target.parents:
                        if parent == root.parent:
                            break
                        if parent.is_relative_to(root):
                            os.chmod(parent, 0o700)
                    written = 0
                    with archive.open(info, "r") as source, target.open("xb") as destination:
                        while True:
                            chunk = source.read(1024 * 1024)
                            if not chunk:
                                break
                            written += len(chunk)
                            if (
                                written > info.file_size
                                or written > effective_limits.max_single_uncompressed_bytes
                            ):
                                raise ZipRejected("zip_limit_exceeded", "Фактический размер ZIP превышает лимит.")
                            effective_budget.consume_actual(len(chunk), archive_display)
                            actual_total += len(chunk)
                            destination.write(chunk)
                    if written != info.file_size:
                        raise ZipRejected("zip_size_mismatch", "Фактический размер записи ZIP не совпадает с объявленным.")
                    os.chmod(target, 0o600)
                    extracted.append((target, name))

                nested_inventory: list[dict[str, Any]] = []
                limitations: list[dict[str, Any]] = []
                records = 0
                for extracted_path, name in extracted:
                    item, item_limits, item_records, _unreadable = inspect_nested(extracted_path, name)
                    nested_inventory.append(item)
                    limitations.extend(item_limits)
                    records += item_records
                    effective_budget.raise_if_exhausted()
                details = {
                    "format": "zip",
                    "entry_count": len(entries),
                    "declared_uncompressed_bytes": sum(info.file_size for info in entries),
                    "actual_uncompressed_bytes": actual_total,
                    "global_budget": effective_budget.snapshot(),
                    "extracted_inventory": nested_inventory,
                }
                return details, "partial" if limitations else "reliable", limitations, records
    except ZipRejected as error:
        return _rejected(
            path,
            error.code,
            error.message,
            display_path=archive_display,
            budget=effective_budget,
        )
    except (OSError, RuntimeError, zipfile.BadZipFile, zipfile.LargeZipFile):
        return _rejected(
            path,
            "zip_invalid",
            "ZIP не удалось безопасно открыть или извлечь.",
            display_path=archive_display,
            budget=effective_budget,
        )
