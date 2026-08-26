from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import stat
import uuid
from pathlib import Path
from typing import Any

from canonical_model import build_canonical_model
from domain_checks import run_domain_checks
from excel_extract import extract_xls, extract_xlsx
from pdf_extract import extract_pdf
from result_schema import validate_result_schema
from safety_limits import INPUT_DIRECTORY_LIMITS, ZIP_LIMITS, InputDirectoryLimits
from xml_extract import extract_xml
from zip_extract import ZipBudget, extract_zip


SCHEMA_VERSION = "smetchik.result.v1"
PASSPORT_CONTEXT_FIELDS = (
    "object",
    "work_type",
    "funding_source",
    "region_or_price_zone",
    "price_level_date",
    "calculation_method",
    "stage",
    "document_set",
)
RESERVED_CONTEXT_FIELDS = {
    "xml_schema_registry",
    "semantic_records",
    "canonical_records",
    "full_row_coverage",
}
TRUSTED_DOMAIN_CONTEXT_FIELDS = {
    "vat",
    "contract_change",
    "normative_sources_verified",
}
FULL_ROW_CONTROL_DIMENSIONS = frozenset(
    {
        "arithmetic",
        "fields",
        "volume_source",
        "rate_norm",
        "indices_coefficients",
        "resources",
        "interdocument",
    }
)
MAX_PURPOSE_CHARACTERS = 256
MAX_CONTEXT_DEPTH = 32
MAX_CONTEXT_NODES = 20_000
MAX_CONTEXT_STRING_CHARACTERS = 200_000
MAX_PUBLIC_FINDINGS = 1_000
MAX_PUBLIC_LIMITATIONS = 1_000
MAX_PUBLIC_EVIDENCE_ITEMS = 200
MAX_PUBLIC_ROW_STATES = 500
MAX_PUBLIC_COVERAGE_GAPS = 500
MAX_PUBLIC_INVENTORY_ITEMS = 2_000
MAX_PUBLIC_HIERARCHY_ITEMS = 2_000
MAX_PUBLIC_AMOUNTS = 2_000


class InspectionInputError(ValueError):
    pass


class InputPreflightRejected(ValueError):
    def __init__(self, code: str, message: str, locator: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.locator = locator


def _validate_json_value(value: Any, *, depth: int = 0, nodes: list[int] | None = None) -> None:
    counter = nodes if nodes is not None else [0]
    counter[0] += 1
    if counter[0] > MAX_CONTEXT_NODES or depth > MAX_CONTEXT_DEPTH:
        raise InspectionInputError("context exceeds safe structural limits")
    if value is None or isinstance(value, (bool, int)):
        return
    if isinstance(value, float):
        if value != value or value in {float("inf"), float("-inf")}:
            raise InspectionInputError("context contains a non-finite number")
        return
    if isinstance(value, str):
        if len(value) > MAX_CONTEXT_STRING_CHARACTERS:
            raise InspectionInputError("context string exceeds safe limit")
        return
    if isinstance(value, list):
        for item in value:
            _validate_json_value(item, depth=depth + 1, nodes=counter)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise InspectionInputError("context keys must be strings")
            _validate_json_value(item, depth=depth + 1, nodes=counter)
        return
    raise InspectionInputError("context must contain only JSON-compatible values")


def _validated_public_inputs(
    input_path: Path,
    mode: str,
    purpose: str | None,
    context: dict[str, Any] | None,
) -> dict[str, Any]:
    if not isinstance(input_path, Path):
        raise InspectionInputError("input path must be a pathlib.Path")
    if mode not in {"light", "full"}:
        raise InspectionInputError("mode must be light or full")
    if purpose is not None:
        if not isinstance(purpose, str) or len(purpose) > MAX_PURPOSE_CHARACTERS:
            raise InspectionInputError("purpose is invalid")
        if any(ord(character) < 32 for character in purpose):
            raise InspectionInputError("purpose contains control characters")
    if context is None:
        return {}
    if not isinstance(context, dict):
        raise InspectionInputError("context must be a JSON object")
    reserved = RESERVED_CONTEXT_FIELDS & context.keys()
    if reserved or any(key.startswith("_trusted") for key in context):
        raise InspectionInputError("context contains a reserved field")
    _validate_json_value(context)
    try:
        encoded = json.dumps(context, ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise InspectionInputError("context is not valid JSON data") from error
    if len(encoded) > 1024 * 1024:
        raise InspectionInputError("context exceeds the 1 MiB limit")
    document_set = context.get("document_set")
    if document_set is not None and not (
        isinstance(document_set, str)
        or (
            isinstance(document_set, list)
            and all(isinstance(item, str) and item for item in document_set)
        )
    ):
        raise InspectionInputError("document_set must be a string or a list of strings")
    for field in PASSPORT_CONTEXT_FIELDS[:-1]:
        value = context.get(field)
        if value is not None and not isinstance(value, str):
            raise InspectionInputError(f"{field} must be a string")
    return dict(context)


def _validated_trusted_domain_context(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict) or not set(value) <= TRUSTED_DOMAIN_CONTEXT_FIELDS:
        raise InspectionInputError("trusted domain context contains unsupported fields")
    _validate_json_value(value)
    return dict(value)


def _validated_trusted_full_row_coverage(value: Any) -> dict[str, dict[str, Any]]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise InspectionInputError("trusted full-row coverage must be an object")
    _validate_json_value(value)
    normalized: dict[str, dict[str, Any]] = {}
    for row_id, attestation in value.items():
        if not isinstance(row_id, str) or not row_id or not isinstance(attestation, dict):
            raise InspectionInputError("trusted full-row coverage has an invalid row attestation")
        dimensions = attestation.get("completed_dimensions")
        evidence = attestation.get("evidence")
        if not (
            isinstance(dimensions, list)
            and all(isinstance(item, str) and item in FULL_ROW_CONTROL_DIMENSIONS for item in dimensions)
            and isinstance(evidence, list)
            and evidence
            and all(
                isinstance(item, dict)
                and isinstance(item.get("source_path"), str)
                and bool(item["source_path"].strip())
                and isinstance(item.get("locator"), str)
                and bool(item["locator"].strip())
                for item in evidence
            )
        ):
            raise InspectionInputError("trusted full-row coverage attestation is incomplete")
        normalized[row_id] = {
            "completed_dimensions": list(dict.fromkeys(dimensions)),
            "evidence": [dict(item) for item in evidence],
        }
    return normalized


def _schema_aligned_limitations(limitations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    for limitation in limitations:
        evidence_items = limitation.get("evidence")
        if not isinstance(evidence_items, list):
            continue
        for evidence in evidence_items:
            if isinstance(evidence, dict):
                for optional in ("sheet", "page", "cell_range", "xpath", "line", "bbox"):
                    if evidence.get(optional) is None:
                        evidence.pop(optional, None)
            if not isinstance(evidence, dict) or evidence.get("locator"):
                if isinstance(evidence, dict):
                    evidence.setdefault("source_path", "unknown")
                continue
            if evidence.get("xpath"):
                locator = f"xpath:{evidence['xpath']}"
                if evidence.get("line") is not None:
                    locator += f";line:{evidence['line']}"
            elif evidence.get("page") is not None:
                locator = f"page:{evidence['page']}"
            else:
                locator = "unresolved-evidence"
            evidence["locator"] = locator
            evidence.setdefault("source_path", "unknown")
    return limitations


def _attach_source_to_limitations(
    limitations: list[dict[str, Any]],
    source_path: str,
) -> list[dict[str, Any]]:
    for limitation in limitations:
        evidence_items = limitation.get("evidence")
        if not isinstance(evidence_items, list):
            limitation["evidence"] = []
            continue
        for evidence in evidence_items:
            if isinstance(evidence, dict):
                evidence.setdefault("source_path", source_path)
    return limitations


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _preflight_directory(
    root: Path,
    limits: InputDirectoryLimits,
) -> list[tuple[Path, str]]:
    files: list[tuple[Path, str]] = []
    total_bytes = 0
    stack: list[tuple[Path, str, int]] = [(root, "", 0)]
    while stack:
        directory, relative_prefix, depth = stack.pop()
        if depth > limits.max_depth:
            raise InputPreflightRejected(
                "input_directory_limit_exceeded",
                "Входной каталог превышает безопасный лимит глубины.",
                relative_prefix or root.name,
            )
        try:
            with os.scandir(directory) as iterator:
                entries = sorted(iterator, key=lambda item: item.name)
        except OSError as error:
            raise InputPreflightRejected(
                "input_directory_unreadable",
                "Входной каталог не удалось безопасно проинвентаризировать.",
                relative_prefix or root.name,
            ) from error
        directories: list[tuple[Path, str, int]] = []
        for entry in entries:
            display = f"{relative_prefix}/{entry.name}" if relative_prefix else entry.name
            try:
                if entry.is_symlink():
                    raise InputPreflightRejected(
                        "input_symlink_forbidden",
                        "Символьные ссылки во входном пакете не читаются.",
                        display,
                    )
                if entry.is_dir(follow_symlinks=False):
                    directories.append((Path(entry.path), display, depth + 1))
                    continue
                if not entry.is_file(follow_symlinks=False):
                    raise InputPreflightRejected(
                        "input_special_file_forbidden",
                        "Специальные файлы во входном каталоге запрещены.",
                        display,
                    )
                size = entry.stat(follow_symlinks=False).st_size
            except InputPreflightRejected:
                raise
            except OSError as error:
                raise InputPreflightRejected(
                    "input_directory_unreadable",
                    "Элемент входного каталога не удалось безопасно проверить.",
                    display,
                ) from error
            if len(files) + 1 > limits.max_files:
                raise InputPreflightRejected(
                    "input_directory_limit_exceeded",
                    "Входной каталог превышает безопасный лимит числа файлов.",
                    display,
                )
            total_bytes += size
            if total_bytes > limits.max_total_bytes:
                raise InputPreflightRejected(
                    "input_directory_limit_exceeded",
                    "Входной каталог превышает безопасный суммарный размер.",
                    display,
                )
            files.append((Path(entry.path), display))
        stack.extend(reversed(directories))
    return sorted(files, key=lambda item: item[1])


def _preflight_input(
    input_path: Path,
    limits: InputDirectoryLimits,
) -> list[tuple[Path, str]]:
    try:
        metadata = input_path.lstat()
    except OSError as error:
        raise InspectionInputError("input does not exist or cannot be inspected") from error
    if stat.S_ISLNK(metadata.st_mode):
        raise InputPreflightRejected(
            "input_symlink_forbidden",
            "Символьные ссылки во входном пакете не читаются.",
            input_path.name,
        )
    if stat.S_ISREG(metadata.st_mode):
        if metadata.st_size > limits.max_total_bytes:
            raise InputPreflightRejected(
                "input_file_limit_exceeded",
                "Входной файл превышает безопасный предел размера до извлечения.",
                input_path.name,
            )
        return [(input_path, input_path.name)]
    if stat.S_ISDIR(metadata.st_mode):
        return _preflight_directory(input_path, limits)
    raise InputPreflightRejected(
        "input_special_file_forbidden",
        "Входной объект не является обычным файлом или каталогом.",
        input_path.name,
    )


def _rebase_evidence_paths(value: Any, actual_path: Path, display_path: str) -> None:
    actual = str(actual_path)
    if isinstance(value, dict):
        for path_key in ("path", "source_path"):
            source_path = value.get(path_key)
            if isinstance(source_path, str) and (
                source_path in {actual, actual_path.name}
                or source_path.startswith(actual + ":")
            ):
                value[path_key] = display_path + source_path[len(actual) :] if source_path.startswith(actual + ":") else display_path
        locator = value.get("locator")
        if isinstance(locator, str):
            if locator.startswith(actual):
                value["locator"] = display_path + locator[len(actual) :]
            elif locator in {actual_path.name, str(actual_path)}:
                value["locator"] = display_path
        for nested in value.values():
            _rebase_evidence_paths(nested, actual_path, display_path)
    elif isinstance(value, list):
        for nested in value:
            _rebase_evidence_paths(nested, actual_path, display_path)


def _safe_count(value: Any) -> int:
    return len(value) if isinstance(value, list) else 0


def _copy_safe_scalars(source: dict[str, Any], keys: tuple[str, ...]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key in keys:
        value = source.get(key)
        if value is None or isinstance(value, (str, int, float, bool)):
            result[key] = value
    return result


def _compact_details(details: Any) -> dict[str, Any]:
    """Return structural extraction metadata without source document contents."""

    if not isinstance(details, dict):
        return {}
    fmt = str(details.get("format") or "").casefold()
    compact: dict[str, Any] = {"format": details.get("format")} if details.get("format") else {}
    if fmt in {"xlsx", "xlsm", "xls"}:
        sheets = details.get("sheets") if isinstance(details.get("sheets"), list) else []
        compact.update(
            _copy_safe_scalars(
                details,
                (
                    "read_only",
                    "file_size_bytes",
                    "archive_entry_count",
                    "declared_uncompressed_bytes",
                    "max_compression_ratio",
                    "zero_compressed_member",
                    "worksheet_count",
                    "serialized_cell_count",
                    "largest_declared_range_cells",
                    "declared_cell_count",
                    "calculation_mode",
                    "macro_capable",
                    "vba_payload_present",
                    "macros_executed",
                    "external_links_detected",
                    "semantic_row_count",
                    "formula_count",
                ),
            )
        )
        compact["sheet_count"] = len(sheets)
        compact["sheets"] = [
            _copy_safe_scalars(
                sheet,
                ("name", "state", "max_row", "max_column", "semantic_row_count"),
            )
            for sheet in sheets
            if isinstance(sheet, dict)
        ]
        compact["hidden_sheet_count"] = _safe_count(details.get("hidden_sheets"))
        compact["merged_range_count"] = _safe_count(details.get("merged_cells"))
        compact["defined_name_count"] = _safe_count(details.get("defined_names"))
        compact["missing_cached_formula_value_count"] = _safe_count(
            details.get("missing_cached_formula_values")
        )
        compact["formula_cache_mismatch_count"] = _safe_count(
            details.get("formula_cache_mismatches")
        )
        return compact
    if fmt == "pdf":
        pages = details.get("pages") if isinstance(details.get("pages"), list) else []
        compact.update(
            _copy_safe_scalars(
                details,
                ("encrypted", "page_count", "visual_verification_required"),
            )
        )
        compact["pages"] = []
        for page in pages:
            if not isinstance(page, dict):
                continue
            summary = _copy_safe_scalars(
                page,
                (
                    "page",
                    "rotation",
                    "text_layer_present",
                    "record_count",
                    "ocr_attempted",
                    "ocr_mean_confidence",
                    "visual_verification_required",
                    "extraction_status",
                ),
            )
            summary["word_count"] = _safe_count(page.get("words"))
            summary["table_count"] = _safe_count(page.get("tables"))
            summary["ocr_word_count"] = _safe_count(page.get("ocr_words"))
            summary["ocr_low_confidence_word_count"] = _safe_count(
                page.get("ocr_low_confidence_words")
            )
            compact["pages"].append(summary)
        return compact
    if fmt == "xml":
        schema = details.get("schema")
        if isinstance(schema, dict):
            compact["schema"] = _copy_safe_scalars(
                schema,
                ("id", "family", "status", "root", "namespace", "version"),
            )
        else:
            compact["schema"] = None
        validation = details.get("schema_validation")
        if isinstance(validation, dict):
            compact_validation = _copy_safe_scalars(
                validation,
                ("status", "xsd_version", "error_count"),
            )
            candidate_ids = validation.get("candidate_ids")
            if isinstance(candidate_ids, list):
                compact_validation["candidate_ids"] = [
                    candidate
                    for candidate in candidate_ids
                    if isinstance(candidate, str)
                ]
            compact["schema_validation"] = compact_validation
        compact["node_count"] = int(details.get("node_count") or _safe_count(details.get("structure")))
        compact["semantic_value_count"] = _safe_count(details.get("semantic_values"))
        compact["semantic_record_count"] = _safe_count(details.get("semantic_records"))
        compact["semantic_interpretation_performed"] = bool(
            details.get("semantic_interpretation_performed")
        )
        compact["arithmetic_performed"] = bool(details.get("arithmetic_performed"))
        return compact
    if fmt == "zip":
        compact.update(
            _copy_safe_scalars(
                details,
                (
                    "entry_count",
                    "declared_uncompressed_bytes",
                    "actual_uncompressed_bytes",
                ),
            )
        )
        budget = details.get("global_budget")
        if isinstance(budget, dict):
            compact["global_budget"] = _copy_safe_scalars(
                budget,
                ("entries", "declared_uncompressed_bytes", "actual_uncompressed_bytes"),
            )
        nested = details.get("extracted_inventory")
        compact["extracted_inventory"] = _compact_inventory(nested if isinstance(nested, list) else [])
        return compact
    return compact


def _compact_inventory(inventory: list[dict[str, Any]]) -> list[dict[str, Any]]:
    compact: list[dict[str, Any]] = []
    for item in inventory:
        if not isinstance(item, dict):
            continue
        compact.append(
            {
                "path": str(item.get("path") or "unknown"),
                "file_type": str(item.get("file_type") or "unknown"),
                "media_type": str(item.get("media_type") or "application/octet-stream"),
                "size_bytes": int(item.get("size_bytes") or 0),
                "sha256": str(item.get("sha256") or ""),
                "extraction_status": str(item.get("extraction_status") or "not_inspected"),
                "details": _compact_details(item.get("details")),
            }
        )
    return compact


def _record_public_truncation(
    entries: list[dict[str, Any]],
    omitted_counts: dict[str, int],
    *,
    path: str,
    category: str,
    total: int,
    kept: int,
) -> None:
    omitted = total - kept
    if omitted <= 0:
        return
    entries.append(
        {
            "path": path,
            "total": total,
            "kept": kept,
            "omitted": omitted,
        }
    )
    omitted_counts[category] = omitted_counts.get(category, 0) + omitted


def _bounded_public_result(result: dict[str, Any]) -> dict[str, Any]:
    """Bound chat/public JSON details without claiming omitted evidence was published."""

    if any(
        isinstance(item, dict) and item.get("code") == "public_result_truncated"
        for item in result.get("limitations", [])
    ):
        return result
    truncations: list[dict[str, Any]] = []
    omitted_counts: dict[str, int] = {}
    original_findings = [
        item for item in result.get("findings", []) if isinstance(item, dict)
    ]
    original_limitations = [
        item for item in result.get("limitations", []) if isinstance(item, dict)
    ]
    original_checked = int(result.get("coverage", {}).get("checked_records") or 0)
    original_arithmetic_checked = int(
        result.get("coverage", {}).get("arithmetic_checked_records") or 0
    )

    if len(original_findings) > MAX_PUBLIC_FINDINGS:
        severity_order = {"critical": 0, "material": 1, "recommendation": 2}
        ordered = sorted(
            enumerate(original_findings),
            key=lambda item: (
                severity_order.get(str(item[1].get("severity")), 99),
                str(item[1].get("id") or ""),
                item[0],
            ),
        )
        result["findings"] = [item for _index, item in ordered[:MAX_PUBLIC_FINDINGS]]
        _record_public_truncation(
            truncations,
            omitted_counts,
            path="$.findings",
            category="findings",
            total=len(original_findings),
            kept=MAX_PUBLIC_FINDINGS,
        )

    retained_findings = result.get("findings", [])
    for index, finding in enumerate(retained_findings):
        if not isinstance(finding, dict):
            continue
        evidence = finding.get("evidence")
        if isinstance(evidence, list) and len(evidence) > MAX_PUBLIC_EVIDENCE_ITEMS:
            total = len(evidence)
            finding["evidence"] = evidence[:MAX_PUBLIC_EVIDENCE_ITEMS]
            finding["evidence_summary"] = {
                "total": total,
                "kept": MAX_PUBLIC_EVIDENCE_ITEMS,
                "omitted": total - MAX_PUBLIC_EVIDENCE_ITEMS,
            }
            _record_public_truncation(
                truncations,
                omitted_counts,
                path=f"$.findings[{index}].evidence",
                category="finding_evidence",
                total=total,
                kept=MAX_PUBLIC_EVIDENCE_ITEMS,
            )

    for check_index, check in enumerate(result.get("checks", [])):
        if not isinstance(check, dict):
            continue
        evidence = check.get("evidence")
        if isinstance(evidence, list) and len(evidence) > MAX_PUBLIC_EVIDENCE_ITEMS:
            total = len(evidence)
            check["evidence"] = evidence[:MAX_PUBLIC_EVIDENCE_ITEMS]
            parameters = check.setdefault("parameters", {})
            if isinstance(parameters, dict):
                parameters["evidence_total"] = total
                parameters["evidence_omitted"] = total - MAX_PUBLIC_EVIDENCE_ITEMS
            _record_public_truncation(
                truncations,
                omitted_counts,
                path=f"$.checks[{check_index}].evidence",
                category="check_evidence",
                total=total,
                kept=MAX_PUBLIC_EVIDENCE_ITEMS,
            )
        parameters = check.get("parameters")
        if not isinstance(parameters, dict):
            continue
        for key, value in list(parameters.items()):
            if not isinstance(value, list) or len(value) <= MAX_PUBLIC_ROW_STATES:
                continue
            total = len(value)
            parameters[key] = value[:MAX_PUBLIC_ROW_STATES]
            parameters[f"{key}_total"] = total
            parameters[f"{key}_omitted"] = total - MAX_PUBLIC_ROW_STATES
            _record_public_truncation(
                truncations,
                omitted_counts,
                path=f"$.checks[{check_index}].parameters.{key}",
                category=(
                    "check_row_states" if key == "row_states" else "check_parameter_items"
                ),
                total=total,
                kept=MAX_PUBLIC_ROW_STATES,
            )

    coverage = result.get("coverage")
    if isinstance(coverage, dict):
        for key in ("unclassified_candidate_ranges", "gaps", "full_control_gaps"):
            value = coverage.get(key)
            if not isinstance(value, list) or len(value) <= MAX_PUBLIC_COVERAGE_GAPS:
                continue
            total = len(value)
            coverage[key] = value[:MAX_PUBLIC_COVERAGE_GAPS]
            coverage[f"{key}_total"] = total
            coverage[f"{key}_omitted"] = total - MAX_PUBLIC_COVERAGE_GAPS
            _record_public_truncation(
                truncations,
                omitted_counts,
                path=f"$.coverage.{key}",
                category=key,
                total=total,
                kept=MAX_PUBLIC_COVERAGE_GAPS,
            )

    top_level_caps = {
        "input_inventory": (MAX_PUBLIC_INVENTORY_ITEMS, "input_inventory"),
        "estimate_hierarchy": (MAX_PUBLIC_HIERARCHY_ITEMS, "estimate_hierarchy"),
        "amounts": (MAX_PUBLIC_AMOUNTS, "amounts"),
    }
    for key, (limit, category) in top_level_caps.items():
        value = result.get(key)
        if not isinstance(value, list) or len(value) <= limit:
            continue
        total = len(value)
        result[key] = value[:limit]
        _record_public_truncation(
            truncations,
            omitted_counts,
            path=f"$.{key}",
            category=category,
            total=total,
            kept=limit,
        )

    for limitation_index, limitation in enumerate(original_limitations):
        evidence = limitation.get("evidence")
        if not isinstance(evidence, list) or len(evidence) <= MAX_PUBLIC_EVIDENCE_ITEMS:
            continue
        total = len(evidence)
        limitation["evidence"] = evidence[:MAX_PUBLIC_EVIDENCE_ITEMS]
        limitation["evidence_total"] = total
        limitation["evidence_omitted"] = total - MAX_PUBLIC_EVIDENCE_ITEMS
        _record_public_truncation(
            truncations,
            omitted_counts,
            path=f"$.limitations[{limitation_index}].evidence",
            category="limitation_evidence",
            total=total,
            kept=MAX_PUBLIC_EVIDENCE_ITEMS,
        )

    if len(original_limitations) > MAX_PUBLIC_LIMITATIONS:
        # Reserve one public slot for the truncation marker itself.
        kept_limitations = MAX_PUBLIC_LIMITATIONS - 1
        _record_public_truncation(
            truncations,
            omitted_counts,
            path="$.limitations",
            category="limitations",
            total=len(original_limitations),
            kept=kept_limitations,
        )
        result["limitations"] = original_limitations[:kept_limitations]

    if not truncations:
        return result

    current_limitations = list(result.get("limitations", []))
    if len(current_limitations) >= MAX_PUBLIC_LIMITATIONS:
        kept_limitations = MAX_PUBLIC_LIMITATIONS - 1
        if "limitations" not in omitted_counts:
            _record_public_truncation(
                truncations,
                omitted_counts,
                path="$.limitations",
                category="limitations",
                total=len(original_limitations),
                kept=kept_limitations,
            )
        current_limitations = current_limitations[:kept_limitations]

    retained_severity_counts = {severity: 0 for severity in ("critical", "material", "recommendation")}
    original_severity_counts = {severity: 0 for severity in retained_severity_counts}
    for finding in original_findings:
        severity = str(finding.get("severity") or "")
        if severity in original_severity_counts:
            original_severity_counts[severity] += 1
    for finding in result.get("findings", []):
        if not isinstance(finding, dict):
            continue
        severity = str(finding.get("severity") or "")
        if severity in retained_severity_counts:
            retained_severity_counts[severity] += 1
    omitted_by_severity = {
        severity: original_severity_counts[severity] - retained_severity_counts[severity]
        for severity in ("critical", "material", "recommendation")
    }
    marker = {
        "code": "public_result_truncated",
        "message": "Публичный JSON содержит детерминированно ограниченный объём деталей.",
        "impact": "Не все построчные состояния и доказательства опубликованы; полное покрытие в этом результате не заявляется.",
        "required_input": "Разделить пакет на контролируемые части, повторить проверку и сверить omitted_counts; bounded JSON сохранять только по прямой команде.",
        "total_counts": {
            "findings": len(original_findings),
            "limitations": len(original_limitations),
            "checked_records": original_checked,
            "arithmetic_checked_records": original_arithmetic_checked,
        },
        "omitted_counts": dict(sorted(omitted_counts.items())),
        "omitted_by_severity": omitted_by_severity,
        "truncated_paths": sorted(truncations, key=lambda item: item["path"]),
        "evidence": [],
    }
    current_limitations.append(marker)
    result["limitations"] = current_limitations
    if isinstance(coverage, dict):
        coverage["checked_records_total_before_public_truncation"] = original_checked
        coverage[
            "arithmetic_checked_records_total_before_public_truncation"
        ] = original_arithmetic_checked
        coverage["row_level_checked"] = False
        coverage["checked_records"] = 0
        coverage["arithmetic_checked_records"] = 0
        if "Публичные детали сокращены" not in str(coverage.get("description") or ""):
            coverage["description"] = (
                str(coverage.get("description") or "").rstrip()
                + " Публичные детали сокращены; полное построчное доказательство не опубликовано."
            ).strip()
    if result.get("execution_status") == "completed":
        result["execution_status"] = "completed_with_limits"
    result["recommended_action"] = (
        "Разделить пакет на контролируемые части, повторить проверку и сверить omitted_counts; "
        "сохранять bounded JSON только по прямой команде."
    )
    if isinstance(coverage, dict):
        coverage["status"] = result.get("execution_status")
    return result


def _inventory_item(path: Path, display_path: str) -> dict[str, Any]:
    media_type, _ = mimetypes.guess_type(path.name)
    result = {
        "path": display_path,
        "file_type": path.suffix.lower().lstrip(".") or "unknown",
        "media_type": media_type or "application/octet-stream",
        "size_bytes": path.stat().st_size,
        "sha256": sha256_file(path),
        "extraction_status": "not_inspected",
        "details": {},
    }
    return result


def _extract_file(
    path: Path,
    display_path: str,
    depth: int = 0,
    archive_budget: ZipBudget | None = None,
    trusted_xml_registry: list[dict[str, Any]] | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]], int, int]:
    item = _inventory_item(path, display_path)
    suffix = path.suffix.lower()
    if suffix in {".xlsx", ".xlsm"}:
        details, status, limitations, records = extract_xlsx(path)
    elif suffix == ".xls":
        details, status, limitations, records = extract_xls(path)
    elif suffix == ".pdf":
        details, status, limitations, records = extract_pdf(path)
    elif suffix in {".xml", ".gge", ".mge"}:
        if trusted_xml_registry is None:
            details, status, limitations, records = extract_xml(path)
        else:
            details, status, limitations, records = extract_xml(
                path,
                registry=trusted_xml_registry,
            )
    elif suffix == ".zip":
        if depth >= 3:
            details, status, limitations, records = (
                {"format": "zip", "entry_count": 0, "extracted_inventory": []},
                "rejected",
                [
                    {
                        "code": "zip_nesting_limit",
                        "message": "Превышен лимит вложенности ZIP.",
                        "evidence": [{"locator": display_path}],
                    }
                ],
                0,
            )
        else:
            effective_budget = archive_budget or ZipBudget(ZIP_LIMITS)
            details, status, limitations, records = extract_zip(
                path,
                inspect_nested=lambda nested_path, nested_name: _extract_file(
                    nested_path,
                    f"{display_path}!/{nested_name}",
                    depth + 1,
                    effective_budget,
                    trusted_xml_registry,
                ),
                limits=ZIP_LIMITS,
                budget=effective_budget,
                display_path=display_path,
            )
    else:
        details, status, limitations, records = (
            {},
            "not_inspected",
            [
                {
                    "code": "unsupported_or_not_yet_extracted",
                    "message": "Файл только инвентаризирован.",
                    "evidence": [{"locator": display_path}],
                }
            ],
            0,
        )
    item["details"] = details
    item["extraction_status"] = status
    _rebase_evidence_paths(details, path, display_path)
    _rebase_evidence_paths(limitations, path, display_path)
    _attach_source_to_limitations(limitations, display_path)
    unreadable = 1 if status in {"failed", "not_inspected"} else 0
    return item, limitations, records, unreadable


def _missing_passport_fields(purpose: str | None, context: dict[str, Any]) -> list[str]:
    missing = []
    if purpose is None or not str(purpose).strip():
        missing.append("purpose")
    for field in PASSPORT_CONTEXT_FIELDS:
        value = context.get(field)
        if (
            value is None
            or value == []
            or value == {}
            or (isinstance(value, str) and not value.strip())
        ):
            missing.append(field)
    return missing


def _grouped_question(missing_fields: list[str]) -> dict[str, Any]:
    labels = {
        "purpose": "цель проверки",
        "object": "объект",
        "work_type": "вид работ",
        "funding_source": "источник финансирования",
        "region_or_price_zone": "регион или ценовую зону",
        "price_level_date": "дату уровня цен",
        "calculation_method": "метод расчёта",
        "stage": "стадию",
        "document_set": "состав документов",
        "check:VAT-01": "параметры облагаемой базы и НДС, если НДС входит в проверку",
        "check:ARITH-01": "неизвлечённые расчётные значения строк",
        "check:FIELD-01": "неизвлечённые обязательные поля строк",
        "check:COMPONENT-01": "неизвлечённые поля ресурсов и начислений",
        "check:KAC-01": "неизвлечённые реквизиты КАЦ",
        "check:ROUTE-PIR": "исходные данные маршрута ПИР",
        "check:ROUTE-SURVEY": "исходные данные маршрута изысканий",
        "check:ROUTE-OKN": "исходные данные маршрута ОКН",
        "check:ROUTE-DEMOLITION": "исходные данные маршрута сноса",
        "check:ROUTE-CONTRACT": "исходную договорную смету и параметры изменений",
    }
    human = [labels.get(field, field) for field in missing_fields]
    return {
        "id": "Q-PASSPORT-01",
        "prompt": "Одним ответом уточни: " + "; ".join(human) + ".",
        "missing_fields": missing_fields,
        "blocking": True,
        "grouped": True,
    }


def _record_evidence(record: dict[str, Any], fallback: str) -> dict[str, Any]:
    evidence = dict(record.get("evidence")) if isinstance(record.get("evidence"), dict) else {}
    evidence.setdefault("source_path", fallback)
    evidence.setdefault("locator", fallback)
    return evidence


def _blocked_domain_from_attribution(
    canonical_model: dict[str, Any],
    inventory: list[dict[str, Any]],
) -> dict[str, Any]:
    """Expose only extracted attribution while every substantive check is blocked."""

    estimates = canonical_model["estimates"]
    rows = [
        row
        for row in canonical_model["rows"]
        if row.get("reliability", "reliable") == "reliable"
    ]
    formulas = canonical_model["formulas"]
    row_evidence = [_record_evidence(row, "unresolved-row") for row in rows]
    estimate_evidence = [
        _record_evidence(estimate, "unresolved-estimate") for estimate in estimates
    ]
    blocked = {
        "status": "needs_input",
        "parameters": {"reason": "missing_passport"},
    }
    checks = [
        {
            "id": "INV-01",
            **blocked,
            "evidence": [
                {
                    "source_path": str(item.get("path") or "unknown"),
                    "locator": str(item.get("path") or "unknown"),
                }
                for item in inventory
            ],
        },
        {"id": "HIER-01", **blocked, "evidence": estimate_evidence},
        {"id": "ARITH-01", **blocked, "evidence": row_evidence},
        {
            "id": "FORMULA-01",
            **blocked,
            "evidence": [
                _record_evidence(formula, "unresolved-formula") for formula in formulas
            ],
        },
        {"id": "DUP-01", **blocked, "evidence": row_evidence},
        {"id": "FIELD-01", **blocked, "evidence": row_evidence},
        {"id": "COMPONENT-01", **blocked, "evidence": []},
        {"id": "KAC-01", **blocked, "evidence": []},
        {"id": "VAT-01", **blocked, "evidence": []},
        {"id": "ANALYTICS-01", **blocked, "evidence": []},
        {"id": "ROUTE-PIR", **blocked, "evidence": []},
        {"id": "ROUTE-SURVEY", **blocked, "evidence": []},
        {"id": "ROUTE-OKN", **blocked, "evidence": []},
        {"id": "ROUTE-DEMOLITION", **blocked, "evidence": []},
        {"id": "ROUTE-CONTRACT", **blocked, "evidence": []},
        {"id": "FULL-ROW-01", **blocked, "evidence": row_evidence},
    ]
    hierarchy = [
        {
            "estimate_id": estimate.get("estimate_id"),
            "estimate_type": estimate.get("estimate_type"),
            "parent_id": estimate.get("parent_id"),
            "declared_total": estimate.get("declared_total"),
            "evidence": _record_evidence(estimate, "unresolved-estimate"),
        }
        for estimate in estimates
    ]
    return {
        "checks": checks,
        "findings": [],
        "limitations": [],
        "normative_sources": [],
        "estimate_hierarchy": hierarchy,
        "amounts": [],
        "cost_analytics": {
            "categories": {},
            "total": "0",
            "counted_rows": 0,
            "unit_indicator": None,
        },
        "checkable_row_ids": [
            str(row.get("row_id") or index) for index, row in enumerate(rows, 1)
        ],
        "checked_row_ids": [],
    }


def _status_and_action(
    *,
    questions: list[dict[str, Any]],
    limitations: list[dict[str, Any]],
    findings: list[dict[str, Any]],
    inventory: list[dict[str, Any]],
    semantic_entities: int,
    estimate_rows: int,
) -> tuple[str, str, str]:
    unusable = bool(inventory) and all(
        item.get("extraction_status") in {"failed", "rejected", "not_inspected"}
        for item in inventory
    )
    if questions:
        return "needs_input", "needs_clarification", "Ответить на один сгруппированный вопрос и продолжить проверку."
    if not inventory or (unusable and semantic_entities == 0):
        return "failed", "verification_impossible", "Предоставить читаемый поддерживаемый пакет."
    material = any(finding.get("severity") in {"critical", "material"} for finding in findings)
    execution = "completed_with_limits" if limitations else "completed"
    if material:
        return execution, "material_nonconformities", "Устранить материальные несоответствия и повторить проверку."
    if estimate_rows == 0:
        return (
            "completed_with_limits",
            "verification_impossible",
            "Предоставить надёжно извлечённые сметные строки; одних формул или метаданных недостаточно.",
        )
    if semantic_entities == 0:
        return execution, "verification_impossible", "Предоставить данные, допускающие смысловую сметную проверку."
    if limitations:
        return execution, "verified_in_checked_scope", "Учесть перечисленные ограничения и при необходимости дополнить пакет."
    return execution, "verified_in_checked_scope", "Сохранить отчёт как подтверждение проверенного объёма."


def inspect_input(
    input_path: Path,
    *,
    mode: str,
    purpose: str | None = None,
    context: dict[str, Any] | None = None,
    _trusted_records: list[dict[str, Any]] | None = None,
    _trusted_xml_registry: list[dict[str, Any]] | None = None,
    _trusted_domain_context: dict[str, Any] | None = None,
    _trusted_full_row_coverage: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    inventory: list[dict[str, Any]] = []
    limitations: list[dict[str, Any]] = []
    input_files: list[tuple[Path, str]] = []
    extractor_records = 0
    unreadable_items = 0
    effective_context = _validated_public_inputs(input_path, mode, purpose, context)
    trusted_domain_context = _validated_trusted_domain_context(_trusted_domain_context)
    trusted_full_row_coverage = _validated_trusted_full_row_coverage(
        _trusted_full_row_coverage
    )
    if _trusted_records is not None and not (
        isinstance(_trusted_records, list)
        and all(isinstance(record, dict) for record in _trusted_records)
    ):
        raise InspectionInputError("trusted records must be a list of objects")
    if _trusted_xml_registry is not None and not (
        isinstance(_trusted_xml_registry, list)
        and all(isinstance(record, dict) for record in _trusted_xml_registry)
    ):
        raise InspectionInputError("trusted XML registry must be a list of objects")
    try:
        input_files = _preflight_input(input_path, INPUT_DIRECTORY_LIMITS)
    except InputPreflightRejected as error:
        limitations.append(
            {
                "code": error.code,
                "message": error.message,
                "evidence": [
                    {
                        "source_path": error.locator,
                        "locator": error.locator,
                    }
                ],
            }
        )
        unreadable_items = 1
    else:
        for path, display in input_files:
            item, item_limits, records, unreadable = _extract_file(
                path,
                display,
                trusted_xml_registry=_trusted_xml_registry,
            )
            inventory.append(item)
            limitations.extend(item_limits)
            extractor_records += records
            unreadable_items += unreadable
    canonical_context = dict(effective_context)
    if _trusted_records is not None:
        canonical_context["semantic_records"] = _trusted_records
    canonical_model = build_canonical_model(inventory, canonical_context)
    missing_passport_fields = _missing_passport_fields(purpose, effective_context)
    if missing_passport_fields:
        domain = _blocked_domain_from_attribution(canonical_model, inventory)
    else:
        domain_context = dict(effective_context)
        domain_context.update(trusted_domain_context)
        domain = run_domain_checks(
            canonical_model,
            inventory=inventory,
            mode=mode,
            purpose=purpose,
            context=domain_context,
            extraction_limitations=limitations,
            trusted_context_fields=set(trusted_domain_context),
        )
    limitations.extend(domain["limitations"])
    if canonical_model["unclassified_candidate_ranges"]:
        limitations.append(
            {
                "code": "unclassified_candidate_ranges",
                "message": "Часть потенциальных табличных данных не классифицирована как сметные строки.",
                "impact": "Построчное покрытие не может считаться сплошным.",
                "required_input": "Предоставить машиночитаемую таблицу или уточнить структуру диапазонов.",
                "evidence": canonical_model["unclassified_candidate_ranges"],
            }
        )

    missing_fields = list(missing_passport_fields)
    if not missing_passport_fields:
        for check in domain["checks"]:
            if check["status"] == "needs_input":
                field = f"check:{check['id']}"
                if field not in missing_fields:
                    missing_fields.append(field)
    questions = [_grouped_question(missing_fields)] if missing_fields else []
    if missing_fields and not missing_passport_fields:
        for check in domain["checks"]:
            if check["status"] == "passed":
                check["status"] = "needs_input"
                check.setdefault("parameters", {})["blocked_by_missing_passport"] = True

    checkable_ids = set(domain["checkable_row_ids"])
    arithmetic_checked_ids = set(domain["checked_row_ids"])
    arithmetic_check = next(
        (check for check in domain["checks"] if check.get("id") == "ARITH-01"),
        {},
    )
    arithmetic_states_by_id = {
        str(state.get("row_id")): str(state.get("status"))
        for state in arithmetic_check.get("parameters", {}).get("row_states", [])
        if isinstance(state, dict) and state.get("row_id") is not None
    }
    required_row_fields = ("name", "quantity", "unit", "unit_price", "declared_total")
    row_needs_input_ids = {
        str(row.get("row_id") or index)
        for index, row in enumerate(
            [
                row
                for row in canonical_model["rows"]
                if row.get("reliability", "reliable") == "reliable"
            ],
            1,
        )
        if any(row.get(field) in (None, "") for field in required_row_fields)
        and row.get("source_fields_verified_complete") is not True
    }
    attested_ids = {
        row_id
        for row_id, attestation in trusted_full_row_coverage.items()
        if FULL_ROW_CONTROL_DIMENSIONS
        <= set(attestation.get("completed_dimensions", []))
    }
    fully_checked_ids = (
        (checkable_ids - row_needs_input_ids) & arithmetic_checked_ids & attested_ids
        if mode == "full" and not missing_passport_fields
        else set()
    )
    row_by_id = {
        str(row.get("row_id") or index): row
        for index, row in enumerate(
            [
                row
                for row in canonical_model["rows"]
                if row.get("reliability", "reliable") == "reliable"
            ],
            1,
        )
    }
    full_control_gaps: list[dict[str, Any]] = []
    if not missing_passport_fields:
        full_states: list[dict[str, str]] = []
        full_evidence: list[dict[str, Any]] = []
        for row_id in sorted(checkable_ids):
            if row_id in fully_checked_ids:
                state = "passed"
                full_evidence.extend(trusted_full_row_coverage[row_id]["evidence"])
            elif row_id not in arithmetic_checked_ids or row_id in row_needs_input_ids:
                state = (
                    "limited"
                    if arithmetic_states_by_id.get(row_id) == "limited"
                    and row_id not in row_needs_input_ids
                    else "needs_input"
                )
                evidence = _record_evidence(row_by_id[row_id], row_id)
                full_evidence.append(evidence)
                missing_dimensions: list[str] = []
                if row_id not in arithmetic_checked_ids:
                    missing_dimensions.append("arithmetic")
                if row_id in row_needs_input_ids:
                    missing_dimensions.append("fields")
                full_control_gaps.append(
                    {
                        "row_id": row_id,
                        "missing_dimensions": missing_dimensions,
                        "evidence": evidence,
                    }
                )
            else:
                state = "limited"
                attestation = trusted_full_row_coverage.get(row_id, {})
                missing_dimensions = sorted(
                    FULL_ROW_CONTROL_DIMENSIONS
                    - set(attestation.get("completed_dimensions", []))
                )
                evidence = _record_evidence(row_by_id[row_id], row_id)
                full_evidence.append(evidence)
                full_control_gaps.append(
                    {
                        "row_id": row_id,
                        "missing_dimensions": missing_dimensions,
                        "evidence": evidence,
                    }
                )
            full_states.append({"row_id": row_id, "status": state})
        if mode == "light":
            full_status = "limited" if checkable_ids else "not_applicable"
        elif not checkable_ids:
            full_status = "not_applicable"
        elif fully_checked_ids == checkable_ids:
            full_status = "passed"
        elif any(state["status"] == "needs_input" for state in full_states):
            full_status = "needs_input"
        else:
            full_status = "limited"
        domain["checks"].append(
            {
                "id": "FULL-ROW-01",
                "status": full_status,
                "evidence": full_evidence,
                "parameters": {
                    "required_dimensions": sorted(FULL_ROW_CONTROL_DIMENSIONS),
                    "row_states": full_states,
                },
            }
        )
        if mode == "full" and full_control_gaps:
            limitations.append(
                {
                    "code": "full_row_controls_incomplete",
                    "message": "Для части строк выполнен арифметический контроль, но не подтверждены все измерения полного режима.",
                    "impact": "Построчное покрытие не может называться полным.",
                    "required_input": "Завершить контроль объёма и источника, нормы/расценки, индексов, коэффициентов, ресурсов и междокументных связей.",
                    "evidence": [gap["evidence"] for gap in full_control_gaps],
                }
            )
    extracted_rows = len(canonical_model["rows"])
    row_level_checked = (
        mode == "full"
        and bool(checkable_ids)
        and checkable_ids <= fully_checked_ids
        and not canonical_model["unclassified_candidate_ranges"]
    )
    semantic_entities = sum(
        len(canonical_model[name])
        for name in (
            "documents",
            "estimates",
            "rows",
            "resources",
            "accruals",
            "kacs",
            "totals",
            "formulas",
        )
    )
    if inventory and extracted_rows == 0 and not missing_passport_fields:
        limitations.append(
            {
                "code": "no_estimate_rows_available",
                "message": "В доступном пакете не классифицированы надёжные сметные строки.",
                "impact": "Формулы, метаданные или структура без строк не подтверждают проверку сметы.",
                "required_input": "Предоставить машиночитаемые сметные строки или уточнить их расположение.",
                "evidence": [
                    {
                        "source_path": str(item.get("path") or "unknown"),
                        "locator": str(item.get("path") or "unknown"),
                    }
                    for item in inventory
                ],
            }
        )
    execution_status, overall_status, recommended_action = _status_and_action(
        questions=questions,
        limitations=limitations,
        findings=domain["findings"],
        inventory=inventory,
        semantic_entities=semantic_entities,
        estimate_rows=extracted_rows,
    )
    if mode == "light":
        coverage_description = "Макропроверка всего доступного пакета. Позиции не проверялись построчно."
    elif row_level_checked and limitations:
        coverage_description = "Максимально сплошная проверка доступных данных с ограничениями."
    elif row_level_checked:
        coverage_description = "Сплошная проверка всех надёжно извлечённых сметных строк."
    else:
        coverage_description = "Максимально сплошная проверка доступных данных с ограничениями; построчное покрытие не подтверждено."
    result = {
        "schema_version": SCHEMA_VERSION,
        "run_id": str(uuid.uuid4()),
        "mode": mode,
        "purpose": purpose,
        "execution_status": execution_status,
        "context": effective_context,
        "input_inventory": _compact_inventory(inventory),
        "coverage": {
            "status": execution_status,
            "description": coverage_description,
            "row_level_checked": row_level_checked,
            "sampling_strategy": "none",
            "extracted_records": extracted_rows,
            "checkable_records": len(checkable_ids),
            "checked_records": len(fully_checked_ids) if mode == "full" else 0,
            "arithmetic_checked_records": (
                len(arithmetic_checked_ids) if mode == "full" else 0
            ),
            "reliably_extracted_records": len(checkable_ids),
            "extractor_records": extractor_records,
            "extracted_entities": semantic_entities,
            "unreadable_items": unreadable_items,
            "unclassified_candidate_ranges": canonical_model["unclassified_candidate_ranges"],
            "gaps": canonical_model["unclassified_candidate_ranges"],
            "full_control_gaps": full_control_gaps,
        },
        "estimate_hierarchy": domain["estimate_hierarchy"],
        "checks": domain["checks"],
        "amounts": domain["amounts"],
        "cost_analytics": domain["cost_analytics"],
        "findings": domain["findings"],
        "questions": questions,
        "limitations": _schema_aligned_limitations(limitations),
        "normative_sources": domain["normative_sources"],
        "artifacts": [],
        "overall_status": overall_status,
        "recommended_action": recommended_action,
    }
    for actual_path, display_path in input_files:
        _rebase_evidence_paths(result, actual_path, display_path)
    _rebase_evidence_paths(result, input_path, input_path.name)
    result = _bounded_public_result(result)
    validate_result_schema(result)
    return result
