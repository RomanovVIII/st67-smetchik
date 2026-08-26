from __future__ import annotations

import re
import math
from collections import defaultdict
from typing import Any, Iterable

from safety_limits import PDF_LIMITS


CANONICAL_COLLECTIONS = (
    "documents",
    "estimates",
    "rows",
    "resources",
    "accruals",
    "indices",
    "coefficients",
    "kacs",
    "totals",
    "formulas",
)

ENTITY_COLLECTION = {
    "document": "documents",
    "estimate": "estimates",
    "estimate_row": "rows",
    "row": "rows",
    "resource": "resources",
    "accrual": "accruals",
    "index": "indices",
    "coefficient": "coefficients",
    "kac": "kacs",
    "market_price": "kacs",
    "total": "totals",
    "formula": "formulas",
}

HEADER_ALIASES = {
    "code": {"code", "шифр", "обоснование", "номер расценки", "код"},
    "name": {"name", "наименование", "работы и затраты", "наименование работ"},
    "quantity": {"quantity", "количество", "объем", "объём", "объем работ", "объём работ"},
    "unit": {"unit", "единица", "ед. изм.", "ед изм", "единица измерения"},
    "unit_price": {"unit_price", "цена единицы", "цена за единицу", "цена", "стоимость единицы", "unit price"},
    "declared_total": {"total", "declared_total", "всего", "стоимость", "итого"},
    "index": {"index", "индекс"},
    "coefficient": {"coefficient", "коэффициент", "коэф."},
    "category": {"category", "категория", "вид затрат"},
}

KNOWN_XML_DOCUMENT_TYPES = {
    "local_estimate": "LSR",
    "object_estimate": "OSR",
    "summary_estimate": "SSR",
    "object_or_summary_estimate": "OSR_OR_SSR",
    "costs_summary": "COSTS_SUMMARY",
    "quantity_takeoff": "VOR",
    "market_analysis": "KAC",
    "contract_estimate": "CONTRACT_ESTIMATE",
    "explanatory_note": "EXPLANATORY_NOTE",
}

PDF_OCR_ATTESTATION_TYPE = "pdf_ocr_visual_attestation"
PDF_OCR_CANDIDATE_TYPE = "ocr_table_row_candidate"
PDF_OCR_CANDIDATE_ID = re.compile(
    r"^page:([1-9][0-9]*):ocr-table:([1-9][0-9]*):row:([1-9][0-9]*)$"
)
PDF_OCR_REQUIRED_FIELDS = (
    "name",
    "unit",
    "quantity",
    "unit_price",
    "declared_total",
)
PDF_OCR_NUMBER = re.compile(
    r"^[+-]?(?:(?:\d{1,3}(?:[ \u00a0]\d{3})+)|\d+)(?:[.,]\d+)?$"
)
PDF_OCR_MIN_TABLE_CONFIDENCE = 90.0


def _empty_model() -> dict[str, Any]:
    model: dict[str, Any] = {name: [] for name in CANONICAL_COLLECTIONS}
    model.update(
        {
            "unclassified_candidate_ranges": [],
            "limitations": [],
            "source_record_count": 0,
        }
    )
    return model


def _clean_header(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip().casefold())


def _header_field(value: Any) -> str | None:
    normalized = _clean_header(value)
    for field, aliases in HEADER_ALIASES.items():
        if normalized in aliases:
            return field
    return None


def _cell_range_from_locators(locators: Any) -> str | None:
    if not isinstance(locators, dict):
        return None
    coordinates: list[tuple[int, int, str]] = []
    for locator in locators.values():
        match = re.search(r"!([A-Z]{1,3})([1-9][0-9]*)$", str(locator or ""), re.IGNORECASE)
        if not match:
            continue
        column = 0
        for character in match.group(1).upper():
            column = column * 26 + ord(character) - 64
        coordinates.append((int(match.group(2)), column, match.group(1).upper() + match.group(2)))
    if not coordinates:
        return None
    coordinates.sort()
    first, last = coordinates[0][2], coordinates[-1][2]
    return first if first == last else f"{first}:{last}"


def _normalize_evidence(
    evidence: Any,
    *,
    source_path: str,
    locator: str,
) -> dict[str, Any]:
    normalized = dict(evidence) if isinstance(evidence, dict) else {}
    normalized.setdefault("source_path", source_path)
    normalized.setdefault("locator", locator)
    if "cell" in normalized and "cell_range" not in normalized:
        normalized["cell_range"] = normalized.pop("cell")
    for optional in ("sheet", "page", "cell_range", "xpath", "line", "bbox"):
        if normalized.get(optional) is None:
            normalized.pop(optional, None)
    return normalized


def _add_record(
    model: dict[str, Any],
    record: Any,
    *,
    source_path: str,
    fallback_locator: str,
    reliability: str = "reliable",
) -> bool:
    if not isinstance(record, dict):
        return False
    entity = str(record.get("entity") or record.get("record_type") or "").strip().casefold()
    collection = ENTITY_COLLECTION.get(entity)
    if collection is None:
        return False
    normalized = dict(record)
    normalized["entity"] = entity
    if collection == "rows":
        if "declared_total" not in normalized and "amount" in normalized:
            normalized["declared_total"] = normalized["amount"]
        if "unit_price" not in normalized and "price" in normalized:
            normalized["unit_price"] = normalized["price"]
    normalized.setdefault("reliability", reliability)
    normalized["evidence"] = _normalize_evidence(
        record.get("evidence"),
        source_path=source_path,
        locator=fallback_locator,
    )
    model[collection].append(normalized)
    model["source_record_count"] += 1

    if collection == "rows":
        row_id = str(normalized.get("row_id") or len(model["rows"]))
        if normalized.get("index") is not None:
            model["indices"].append(
                {
                    "entity": "index",
                    "index_id": f"{row_id}:index",
                    "row_id": row_id,
                    "value": normalized["index"],
                    "reliability": normalized["reliability"],
                    "evidence": normalized["evidence"],
                }
            )
        if normalized.get("coefficient") is not None:
            model["coefficients"].append(
                {
                    "entity": "coefficient",
                    "coefficient_id": f"{row_id}:coefficient",
                    "row_id": row_id,
                    "value": normalized["coefficient"],
                    "reliability": normalized["reliability"],
                    "evidence": normalized["evidence"],
                }
            )
    return True


def _records_from_table(
    model: dict[str, Any],
    table: Any,
    *,
    source_path: str,
    sheet: str | None = None,
    page: int | None = None,
) -> int:
    if not isinstance(table, dict):
        return 0
    rows = table.get("rows")
    if not isinstance(rows, list) or not rows:
        return 0
    headers = table.get("headers")
    mappings: dict[int, str] = {}
    data_rows = rows
    if isinstance(headers, list):
        mappings = {index: field for index, value in enumerate(headers) if (field := _header_field(value))}
    elif isinstance(rows[0], list):
        mappings = {index: field for index, value in enumerate(rows[0]) if (field := _header_field(value))}
        data_rows = rows[1:]
    accepted = 0
    for offset, raw_row in enumerate(data_rows, start=1):
        if isinstance(raw_row, dict):
            record = {field: raw_row.get(field) for field in HEADER_ALIASES if field in raw_row}
            record.update({key: value for key, value in raw_row.items() if key not in record})
        elif isinstance(raw_row, list) and mappings:
            record = {field: raw_row[index] for index, field in mappings.items() if index < len(raw_row)}
        else:
            continue
        if not ({"quantity", "unit_price", "declared_total"} & record.keys()):
            continue
        record.setdefault("entity", "estimate_row")
        record.setdefault("row_id", f"{source_path}:{sheet or page or 'table'}:{offset}")
        record.setdefault("estimate_id", table.get("estimate_id") or sheet or f"{source_path}:estimate")
        location = table.get("range") or f"row:{offset}"
        evidence = dict(record.get("evidence") or {})
        if sheet:
            evidence.setdefault("sheet", sheet)
        if page:
            evidence.setdefault("page", page)
        record["evidence"] = evidence
        if _add_record(
            model,
            record,
            source_path=source_path,
            fallback_locator=f"{sheet or f'page:{page}'}!{location}",
        ):
            accepted += 1
    return accepted


def _excel_sheet_records(model: dict[str, Any], source_path: str, sheet: dict[str, Any]) -> int:
    sheet_name = str(sheet.get("name") or "sheet")
    accepted = 0
    for record in sheet.get("semantic_records", []):
        accepted += int(
            _add_record(
                model,
                record,
                source_path=source_path,
                fallback_locator=sheet_name,
            )
        )
    for table in sheet.get("tables", []):
        accepted += _records_from_table(model, table, source_path=source_path, sheet=sheet_name)
    for table in sheet.get("semantic_tables", []):
        if not isinstance(table, dict):
            continue
        for row in table.get("rows", []):
            if not isinstance(row, dict) or not isinstance(row.get("field_values"), dict):
                continue
            record = {
                "entity": "estimate_row",
                "row_id": f"{source_path}:{sheet_name}:{row.get('row')}",
                "estimate_id": f"{source_path}:{sheet_name}",
                **row["field_values"],
                "evidence": {
                    **(row.get("evidence") or {}),
                    "source_path": source_path,
                    "sheet": sheet_name,
                    "cell_range": _cell_range_from_locators(row.get("cell_locators")),
                },
            }
            if isinstance(row.get("calculation_basis"), dict):
                record["calculation_basis"] = dict(row["calculation_basis"])
            accepted += int(
                _add_record(
                    model,
                    record,
                    source_path=source_path,
                    fallback_locator=f"{sheet_name}!{row.get('row')}:{row.get('row')}",
                )
            )
    cells = sheet.get("cells")
    if not isinstance(cells, list):
        return accepted
    by_row: dict[int, list[tuple[int, dict[str, Any]]]] = defaultdict(list)
    for cell in cells:
        if not isinstance(cell, dict):
            continue
        coordinate = str(cell.get("coordinate") or "")
        match = re.fullmatch(r"([A-Z]{1,3})([1-9][0-9]*)", coordinate)
        if not match:
            continue
        column = 0
        for character in match.group(1):
            column = column * 26 + ord(character) - 64
        by_row[int(match.group(2))].append((column, cell))
        if cell.get("formula"):
            _add_record(
                model,
                {
                    "entity": "formula",
                    "formula_id": f"{source_path}:{sheet_name}:{coordinate}",
                    "formula": cell.get("formula"),
                    "computed_value": cell.get("computed_safe_value"),
                    "cached_value": cell.get("cached_value"),
                    "evidence": {
                        "source_path": source_path,
                        "sheet": sheet_name,
                        "cell_range": coordinate,
                        "locator": f"{sheet_name}!{coordinate}",
                    },
                },
                source_path=source_path,
                fallback_locator=f"{sheet_name}!{coordinate}",
            )
    if accepted:
        return accepted
    header_row: int | None = None
    mappings: dict[int, str] = {}
    for row_number in sorted(by_row):
        candidate = {
            column: field
            for column, cell in by_row[row_number]
            if (field := _header_field(cell.get("value") if cell.get("formula") is None else None))
        }
        if len(candidate) >= 3 and ({"quantity", "unit_price", "declared_total"} & set(candidate.values())):
            header_row, mappings = row_number, candidate
            break
    if header_row is None:
        if by_row:
            model["unclassified_candidate_ranges"].append(
                {
                    "source_path": source_path,
                    "sheet": sheet_name,
                    "locator": f"{sheet_name}!used-range",
                    "reason": "spreadsheet_content_not_classified_as_estimate_rows",
                }
            )
        return 0
    for row_number in sorted(number for number in by_row if number > header_row):
        values = {
            field: cell.get("cached_value") if cell.get("formula") else cell.get("value")
            for column, cell in by_row[row_number]
            if (field := mappings.get(column))
        }
        if not any(value not in (None, "") for value in values.values()):
            continue
        columns = [column for column, _cell in by_row[row_number] if column in mappings]
        first = min(columns)
        last = max(columns)

        def column_name(number: int) -> str:
            result = ""
            while number:
                number, remainder = divmod(number - 1, 26)
                result = chr(65 + remainder) + result
            return result

        cell_range = f"{column_name(first)}{row_number}:{column_name(last)}{row_number}"
        record = {
            "entity": "estimate_row",
            "row_id": f"{source_path}:{sheet_name}:{row_number}",
            "estimate_id": f"{source_path}:{sheet_name}",
            **values,
            "evidence": {
                "source_path": source_path,
                "sheet": sheet_name,
                "cell_range": cell_range,
                "locator": f"{sheet_name}!{cell_range}",
            },
        }
        accepted += int(
            _add_record(
                model,
                record,
                source_path=source_path,
                fallback_locator=f"{sheet_name}!{cell_range}",
            )
        )
    return accepted


def _pdf_table_records(
    model: dict[str, Any],
    table: dict[str, Any],
    *,
    source_path: str,
    page_number: int,
) -> int:
    def has_bbox(value: Any) -> bool:
        if not isinstance(value, (list, tuple)) or len(value) != 4:
            return False
        try:
            left, top, right, bottom = (float(coordinate) for coordinate in value)
        except (TypeError, ValueError):
            return False
        return right > left and bottom > top

    if not has_bbox(table.get("bbox")):
        return 0
    rows = table.get("rows")
    if not isinstance(rows, list) or not rows:
        return 0
    first_cells = rows[0].get("cells") if isinstance(rows[0], dict) else None
    if not isinstance(first_cells, list) or not has_bbox(rows[0].get("bbox")):
        return 0
    mapping = {
        index: field
        for index, cell in enumerate(first_cells)
        if (
            isinstance(cell, dict)
            and has_bbox(cell.get("bbox"))
            and (field := _header_field(cell.get("text")))
        )
    }
    if len(mapping) < 3 or not ({"quantity", "unit_price", "declared_total"} & set(mapping.values())):
        return 0
    accepted = 0
    for raw_row in rows[1:]:
        if (
            not isinstance(raw_row, dict)
            or not isinstance(raw_row.get("cells"), list)
            or not has_bbox(raw_row.get("bbox"))
        ):
            continue
        if any(
            index >= len(raw_row["cells"])
            or not isinstance(raw_row["cells"][index], dict)
            or not has_bbox(raw_row["cells"][index].get("bbox"))
            for index in mapping
        ):
            continue
        values = {
            field: raw_row["cells"][index].get("text")
            for index, field in mapping.items()
            if index < len(raw_row["cells"]) and isinstance(raw_row["cells"][index], dict)
        }
        if not any(str(value or "").strip() for value in values.values()):
            continue
        row_index = raw_row.get("row_index")
        record_evidence = {
            **(raw_row.get("evidence") or {}),
            "source_path": source_path,
            "page": page_number,
            "bbox": raw_row.get("bbox"),
        }
        if record_evidence["bbox"] is None:
            record_evidence.pop("bbox")
        record = {
            "entity": "estimate_row",
            "row_id": f"{source_path}:page:{page_number}:table:{table.get('table_index')}:row:{row_index}",
            "estimate_id": f"{source_path}:page:{page_number}:table:{table.get('table_index')}",
            **values,
            "evidence": record_evidence,
        }
        accepted += int(
            _add_record(
                model,
                record,
                source_path=source_path,
                fallback_locator=f"page:{page_number}:table:{table.get('table_index')}:row:{row_index}",
                reliability="reliable",
            )
        )
    return accepted


def _pdf_pixel_bbox(value: Any, image_size: tuple[int, int] | None = None) -> list[float] | None:
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        return None
    try:
        bbox = [float(coordinate) for coordinate in value]
    except (TypeError, ValueError):
        return None
    left, top, right, bottom = bbox
    if not (
        all(math.isfinite(coordinate) for coordinate in bbox)
        and 0 <= left < right
        and 0 <= top < bottom
    ):
        return None
    if image_size is not None and (
        right > image_size[0] or bottom > image_size[1]
    ):
        return None
    return bbox


def _pdf_ocr_attestation_records(
    context_records: Any,
) -> dict[tuple[str, str], list[dict[str, Any]]]:
    attestations: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    if not isinstance(context_records, list):
        return attestations
    for record in context_records:
        if not isinstance(record, dict) or record.get("record_type") != PDF_OCR_ATTESTATION_TYPE:
            continue
        candidate_id = record.get("candidate_id")
        evidence = record.get("evidence")
        if (
            isinstance(candidate_id, str)
            and PDF_OCR_CANDIDATE_ID.fullmatch(candidate_id)
            and isinstance(evidence, dict)
            and isinstance(evidence.get("source_path"), str)
            and evidence["source_path"]
        ):
            attestations[(evidence["source_path"], candidate_id)].append(record)
    return attestations


def _validated_pdf_ocr_candidate(
    candidate: Any,
    *,
    source_path: str,
    page_number: int,
) -> dict[str, Any] | None:
    if not isinstance(candidate, dict) or candidate.get("record_type") != PDF_OCR_CANDIDATE_TYPE:
        return None
    candidate_id = candidate.get("candidate_id")
    match = PDF_OCR_CANDIDATE_ID.fullmatch(candidate_id) if isinstance(candidate_id, str) else None
    if match is None:
        return None
    parsed_page, table_index, row_index = (int(value) for value in match.groups())
    if (
        parsed_page != page_number
        or candidate.get("page") != page_number
        or candidate.get("table_index") != table_index
        or candidate.get("row_index") != row_index
        or candidate.get("coordinate_space") != "rendered_image_pixels"
        or candidate.get("visual_verification_required") is not True
        or candidate.get("rotation") not in {0, 90, 180, 270}
    ):
        return None
    image_size_raw = candidate.get("rendered_image_size")
    if not (
        isinstance(image_size_raw, list)
        and len(image_size_raw) == 2
        and all(isinstance(value, int) and not isinstance(value, bool) and value > 0 for value in image_size_raw)
        and all(value <= PDF_LIMITS.max_render_dimension_pixels for value in image_size_raw)
        and image_size_raw[0] * image_size_raw[1] <= PDF_LIMITS.max_render_pixels_per_page
    ):
        return None
    image_size = (image_size_raw[0], image_size_raw[1])
    row_bbox = _pdf_pixel_bbox(candidate.get("bbox"), image_size)
    table_bbox = _pdf_pixel_bbox(candidate.get("table_bbox"), image_size)
    if row_bbox is None or table_bbox is None:
        return None
    if not (
        table_bbox[0] <= row_bbox[0]
        and table_bbox[1] <= row_bbox[1]
        and table_bbox[2] >= row_bbox[2]
        and table_bbox[3] >= row_bbox[3]
    ):
        return None
    confidence = candidate.get("confidence")
    if not (
        isinstance(confidence, (int, float))
        and not isinstance(confidence, bool)
        and math.isfinite(float(confidence))
        and float(confidence) >= PDF_OCR_MIN_TABLE_CONFIDENCE
    ):
        return None
    proposed = candidate.get("proposed_fields")
    if not isinstance(proposed, dict) or set(proposed) != set(PDF_OCR_REQUIRED_FIELDS):
        return None
    if not all(
        isinstance(proposed[field], str)
        and 0 < len(proposed[field].strip()) <= 200_000
        for field in PDF_OCR_REQUIRED_FIELDS
    ):
        return None
    if not all(
        PDF_OCR_NUMBER.fullmatch(proposed[field].strip())
        for field in ("quantity", "unit_price", "declared_total")
    ):
        return None
    cells = candidate.get("cells")
    if not isinstance(cells, list) or len(cells) != len(PDF_OCR_REQUIRED_FIELDS):
        return None
    cells_by_field: dict[str, dict[str, Any]] = {}
    for cell in cells:
        if not isinstance(cell, dict) or cell.get("field") not in PDF_OCR_REQUIRED_FIELDS:
            return None
        field = cell["field"]
        if field in cells_by_field or cell.get("text") != proposed[field]:
            return None
        cell_bbox = _pdf_pixel_bbox(cell.get("bbox"), image_size)
        cell_confidence = cell.get("confidence")
        if (
            cell_bbox is None
            or not (
                row_bbox[0] <= cell_bbox[0]
                and row_bbox[1] <= cell_bbox[1]
                and row_bbox[2] >= cell_bbox[2]
                and row_bbox[3] >= cell_bbox[3]
            )
            or not isinstance(cell_confidence, (int, float))
            or isinstance(cell_confidence, bool)
            or not math.isfinite(float(cell_confidence))
            or float(cell_confidence) < PDF_OCR_MIN_TABLE_CONFIDENCE
        ):
            return None
        cells_by_field[field] = cell
    if set(cells_by_field) != set(PDF_OCR_REQUIRED_FIELDS):
        return None
    evidence = candidate.get("evidence")
    if not isinstance(evidence, dict):
        return None
    evidence_bbox = _pdf_pixel_bbox(evidence.get("bbox"), image_size)
    if (
        evidence.get("source_path") != source_path
        or evidence.get("page") != page_number
        or evidence_bbox != row_bbox
        or not isinstance(evidence.get("locator"), str)
        or not evidence["locator"].endswith(candidate_id)
    ):
        return None
    return {
        "candidate_id": candidate_id,
        "table_index": table_index,
        "row_index": row_index,
        "bbox": row_bbox,
        "proposed_fields": {field: proposed[field].strip() for field in PDF_OCR_REQUIRED_FIELDS},
        "evidence": {
            "source_path": source_path,
            "page": page_number,
            "bbox": row_bbox,
            "locator": f"{source_path}:{candidate_id}",
        },
    }


def _pdf_ocr_attestation_matches(
    attestation: dict[str, Any],
    candidate: dict[str, Any],
    *,
    source_path: str,
    page_number: int,
) -> bool:
    evidence = attestation.get("evidence")
    if not isinstance(evidence, dict):
        return False
    locator = evidence.get("locator")
    return (
        attestation.get("confirmed") is True
        and attestation.get("candidate_id") == candidate["candidate_id"]
        and evidence.get("source_path") == source_path
        and evidence.get("page") == page_number
        and _pdf_pixel_bbox(evidence.get("bbox")) == candidate["bbox"]
        and isinstance(locator, str)
        and locator.startswith(source_path + ":")
        and candidate["candidate_id"] in locator
    )


def _pdf_ocr_candidate_gap(
    *,
    source_path: str,
    page_number: int,
    candidate: Any,
    reason: str,
) -> dict[str, Any]:
    candidate_id = (
        candidate.get("candidate_id")
        if isinstance(candidate, dict) and isinstance(candidate.get("candidate_id"), str)
        else "invalid-candidate"
    )
    gap: dict[str, Any] = {
        "source_path": source_path,
        "page": page_number,
        "locator": f"{source_path}:{candidate_id}",
        "reason": reason,
    }
    bbox = _pdf_pixel_bbox(candidate.get("bbox")) if isinstance(candidate, dict) else None
    if bbox is not None:
        gap["bbox"] = bbox
    return gap


def _known_xml_document_type(schema_id: Any) -> str | None:
    normalized = str(schema_id or "").casefold()
    for marker, document_type in KNOWN_XML_DOCUMENT_TYPES.items():
        if marker in normalized:
            return document_type
    return None


XML_ENTITY_IDENTIFIER_FIELDS = {
    "document": "document_id",
    "estimate": "estimate_id",
    "estimate_row": "row_id",
    "resource": "resource_id",
    "accrual": "accrual_id",
    "index": "index_id",
    "coefficient": "coefficient_id",
    "kac": "kac_id",
    "total": "total_id",
    "formula": "formula_id",
}


def _xml_record_with_logical_id(
    record: Any,
    *,
    source_path: str,
) -> Any:
    if not isinstance(record, dict):
        return record
    entity = str(record.get("entity") or record.get("record_type") or "").strip().casefold()
    identifier_field = XML_ENTITY_IDENTIFIER_FIELDS.get(entity)
    schema_id = record.get("schema_id")
    xpath = record.get("xpath")
    if not (
        identifier_field
        and isinstance(schema_id, str)
        and schema_id
        and isinstance(xpath, str)
        and xpath.startswith("/")
    ):
        return record
    normalized = dict(record)
    normalized[identifier_field] = f"{source_path}:{schema_id}:{xpath}"
    return normalized


def _walk_inventory(items: Iterable[dict[str, Any]]) -> Iterable[dict[str, Any]]:
    for item in items:
        if not isinstance(item, dict):
            continue
        yield item
        nested = item.get("details", {}).get("extracted_inventory", [])
        if isinstance(nested, list):
            yield from _walk_inventory(nested)


def build_canonical_model(
    inventory: list[dict[str, Any]],
    context: dict[str, Any],
) -> dict[str, Any]:
    """Build a format-neutral, evidence-preserving estimate model.

    Unknown XML is intentionally never interpreted semantically. Context records are
    useful for adapters and tests, but remain evidence-bound and cannot make an
    unsupported source schema "known".
    """

    model = _empty_model()
    context_semantic_records = context.get("semantic_records", [])
    ocr_attestations = _pdf_ocr_attestation_records(context_semantic_records)
    for item in _walk_inventory(inventory):
        source_path = str(item.get("path") or "unknown")
        details = item.get("details") if isinstance(item.get("details"), dict) else {}
        fmt = str(details.get("format") or item.get("file_type") or "").casefold()
        semantic_allowed = True
        if fmt == "xml":
            validation = details.get("schema_validation", {})
            schema = details.get("schema") or {}
            semantic_allowed = validation.get("status") == "valid" and bool(schema.get("id"))
            document_type = _known_xml_document_type(schema.get("id")) if semantic_allowed else None
            if document_type:
                _add_record(
                    model,
                    {
                        "entity": "document",
                        "document_id": source_path,
                        "document_type": document_type,
                        "schema_id": schema.get("id"),
                        "evidence": {"source_path": source_path, "locator": f"{source_path}:root"},
                    },
                    source_path=source_path,
                    fallback_locator=f"{source_path}:root",
                )
                semantic_count = sum(
                    int(
                        _add_record(
                            model,
                            _xml_record_with_logical_id(
                                record,
                                source_path=source_path,
                            ),
                            source_path=source_path,
                            fallback_locator=source_path,
                        )
                    )
                    for record in details.get("semantic_records", [])
                )
                if semantic_count == 0:
                    model["unclassified_candidate_ranges"].append(
                        {
                            "source_path": source_path,
                            "locator": f"{source_path}:semantic-records",
                            "reason": "known_xml_adapter_records_not_mapped_to_canonical_entities",
                        }
                    )
            else:
                root_evidence = (details.get("structure") or [{}])[0].get("evidence", {})
                model["unclassified_candidate_ranges"].append(
                    {
                        **_normalize_evidence(root_evidence, source_path=source_path, locator=f"{source_path}:root"),
                        "reason": "xml_semantics_not_available_for_verified_known_schema",
                    }
                )
        if semantic_allowed and fmt != "xml":
            for record in details.get("semantic_records", []):
                _add_record(model, record, source_path=source_path, fallback_locator=source_path)
        if fmt in {"xlsx", "xlsm", "xls"}:
            for table in details.get("tables", []):
                _records_from_table(model, table, source_path=source_path)
            for sheet in details.get("sheets", []):
                if isinstance(sheet, dict):
                    _excel_sheet_records(model, source_path, sheet)
        elif fmt == "pdf":
            accepted = 0
            candidate_occurrences: dict[str, int] = defaultdict(int)
            for page in details.get("pages", []):
                if not isinstance(page, dict):
                    continue
                for candidate in page.get("ocr_table_candidates", []):
                    if isinstance(candidate, dict) and isinstance(candidate.get("candidate_id"), str):
                        candidate_occurrences[candidate["candidate_id"]] += 1
            ocr_candidate_seen = False
            for table in details.get("tables", []):
                if isinstance(table, dict):
                    accepted += _pdf_table_records(
                        model,
                        table,
                        source_path=source_path,
                        page_number=int(table.get("page") or 1),
                    )
            for page in details.get("pages", []):
                if not isinstance(page, dict):
                    continue
                page_number = int(page.get("page") or 1)
                for record in page.get("semantic_records", []):
                    accepted += int(
                        _add_record(
                            model,
                            record,
                            source_path=source_path,
                            fallback_locator=f"page:{page.get('page')}",
                            reliability="reliable" if page.get("extraction_status") == "reliable" else "partial",
                        )
                    )
                for table in page.get("tables", []):
                    accepted += _pdf_table_records(
                        model,
                        table,
                        source_path=source_path,
                        page_number=page_number,
                    )
                for raw_candidate in page.get("ocr_table_candidates", []):
                    ocr_candidate_seen = True
                    candidate_id = (
                        raw_candidate.get("candidate_id")
                        if isinstance(raw_candidate, dict)
                        else None
                    )
                    if (
                        isinstance(candidate_id, str)
                        and candidate_occurrences.get(candidate_id, 0) > 1
                    ):
                        model["unclassified_candidate_ranges"].append(
                            _pdf_ocr_candidate_gap(
                                source_path=source_path,
                                page_number=page_number,
                                candidate=raw_candidate,
                                reason="duplicate_pdf_ocr_table_candidate_id",
                            )
                        )
                        continue
                    candidate = _validated_pdf_ocr_candidate(
                        raw_candidate,
                        source_path=source_path,
                        page_number=page_number,
                    )
                    if candidate is None:
                        model["unclassified_candidate_ranges"].append(
                            _pdf_ocr_candidate_gap(
                                source_path=source_path,
                                page_number=page_number,
                                candidate=raw_candidate,
                                reason="invalid_pdf_ocr_table_candidate",
                            )
                        )
                        continue
                    key = (source_path, candidate["candidate_id"])
                    matching_attestations = ocr_attestations.get(key, [])
                    if not matching_attestations:
                        reason = "pdf_ocr_table_visual_attestation_required"
                    elif len(matching_attestations) > 1:
                        reason = "duplicate_pdf_ocr_visual_attestation"
                    elif not _pdf_ocr_attestation_matches(
                        matching_attestations[0],
                        candidate,
                        source_path=source_path,
                        page_number=page_number,
                    ):
                        reason = "pdf_ocr_visual_attestation_mismatch"
                    else:
                        record = {
                            "entity": "estimate_row",
                            "row_id": f"{source_path}:{candidate['candidate_id']}",
                            "estimate_id": (
                                f"{source_path}:page:{page_number}:"
                                f"ocr-table:{candidate['table_index']}"
                            ),
                            **candidate["proposed_fields"],
                            "visual_attestation_confirmed": True,
                            "evidence": candidate["evidence"],
                        }
                        accepted += int(
                            _add_record(
                                model,
                                record,
                                source_path=source_path,
                                fallback_locator=(
                                    f"page:{page_number}:ocr-table:"
                                    f"{candidate['table_index']}:row:{candidate['row_index']}"
                                ),
                                reliability="reliable",
                            )
                        )
                        continue
                    model["unclassified_candidate_ranges"].append(
                        _pdf_ocr_candidate_gap(
                            source_path=source_path,
                            page_number=page_number,
                            candidate=raw_candidate,
                            reason=reason,
                        )
                    )
            if details.get("pages") and accepted == 0 and not ocr_candidate_seen:
                model["unclassified_candidate_ranges"].append(
                    {
                        "source_path": source_path,
                        "locator": f"{source_path}:pages",
                        "reason": "pdf_text_or_ocr_not_classified_as_estimate_rows",
                    }
                )

    for record in context_semantic_records:
        if isinstance(record, dict) and record.get("record_type") == PDF_OCR_ATTESTATION_TYPE:
            continue
        _add_record(
            model,
            record,
            source_path=str(record.get("evidence", {}).get("source_path") or "context"),
            fallback_locator="context:semantic_records",
        )
    for record in context.get("canonical_records", []):
        _add_record(
            model,
            record,
            source_path=str(record.get("evidence", {}).get("source_path") or "context"),
            fallback_locator="context:canonical_records",
        )

    # Stable order makes reports and tests reproducible without changing evidence.
    for collection in CANONICAL_COLLECTIONS:
        model[collection].sort(
            key=lambda record: (
                str(record.get("evidence", {}).get("source_path", "")),
                str(record.get("evidence", {}).get("locator", "")),
                str(record.get("row_id") or record.get("estimate_id") or record.get("id") or ""),
            )
        )
    return model
