from __future__ import annotations

import os
import math
import re
import shutil
import struct
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Iterable

import pdfplumber
from pypdf import PdfReader

from safety_limits import PDF_LIMITS, PdfLimits


MIN_OCR_CONFIDENCE = 60.0
MIN_OCR_TABLE_CONFIDENCE = 90.0
MAX_OCR_TABLE_CANDIDATES_PER_PAGE = 2_000
PROCESS_TIMEOUT_SECONDS = 45
HYBRID_MAX_TEXT_WORDS = 4
HYBRID_MAX_TEXT_CHARACTERS = 24
HYBRID_MIN_IMAGE_COVERAGE = 0.25

OCR_TABLE_REQUIRED_FIELDS = (
    "name",
    "unit",
    "quantity",
    "unit_price",
    "declared_total",
)
OCR_TABLE_HEADER_ALIASES = {
    "code": {"code", "код", "шифр", "обоснование"},
    "name": {"name", "item", "наименование", "работы", "затраты"},
    "unit": {"unit", "ед", "ед.", "единица"},
    "quantity": {"quantity", "qty", "количество", "объем", "объём"},
    "unit_price": {"price", "unit_price", "цена", "расценка"},
    "declared_total": {"total", "declared_total", "всего", "итого", "стоимость"},
}
OCR_NUMERIC_PATTERN = re.compile(
    r"^[+-]?(?:(?:\d{1,3}(?:[ \u00a0]\d{3})+)|\d+)(?:[.,]\d+)?$"
)


def _bbox(values: Iterable[float | int]) -> list[float]:
    return [round(float(value), 3) for value in values]


def _pdf_evidence(
    path: Path | str,
    page_number: int,
    locator_suffix: str = "",
    bbox: list[float] | None = None,
) -> dict[str, Any]:
    source_path = str(path)
    locator = f"{source_path}:page:{page_number}{locator_suffix}"
    evidence: dict[str, Any] = {
        "source_path": source_path,
        "page": page_number,
        "locator": locator,
    }
    if bbox is not None:
        evidence["bbox"] = bbox
    return evidence


def _normalized_text(text: str | None) -> str:
    if not text:
        return ""
    return "\n".join(line.strip() for line in text.splitlines() if line.strip())


def _page_box_dimensions(page: Any) -> tuple[float, float]:
    media_box = page.mediabox
    user_unit = float(page.get("/UserUnit", 1) or 1)
    return abs(float(media_box.width)) * user_unit, abs(float(media_box.height)) * user_unit


def _render_plan(
    *,
    page_width_points: float,
    page_height_points: float,
    rotation: int,
    remaining_render_pixels: int,
    limits: PdfLimits,
) -> dict[str, Any] | None:
    width = float(page_width_points)
    height = float(page_height_points)
    if rotation % 180:
        width, height = height, width
    if not all(math.isfinite(value) and value > 0 for value in (width, height)):
        return None
    requested_dpi = max(1, int(limits.default_ocr_dpi))
    minimum_dpi = max(1, int(limits.min_ocr_dpi))
    pixel_cap = min(int(limits.max_render_pixels_per_page), int(remaining_render_pixels))
    dimension_cap = int(limits.max_render_dimension_pixels)
    if pixel_cap <= 0 or dimension_cap <= 0:
        return None
    dimension_dpi = math.floor(dimension_cap * 72.0 / max(width, height))
    area_dpi = math.floor(math.sqrt(pixel_cap * 72.0 * 72.0 / (width * height)))
    dpi = min(requested_dpi, dimension_dpi, area_dpi)
    while dpi >= minimum_dpi:
        width_pixels = max(1, math.ceil(width * dpi / 72.0))
        height_pixels = max(1, math.ceil(height * dpi / 72.0))
        pixel_count = width_pixels * height_pixels
        if (
            width_pixels <= dimension_cap
            and height_pixels <= dimension_cap
            and pixel_count <= pixel_cap
        ):
            return {
                "dpi": dpi,
                "width_pixels": width_pixels,
                "height_pixels": height_pixels,
                "pixel_count": pixel_count,
                "resolution_reduced": dpi < requested_dpi,
            }
        dpi -= 1
    return None


def _png_dimensions(path: Path) -> tuple[int, int]:
    with path.open("rb") as stream:
        header = stream.read(24)
    if len(header) != 24 or header[:8] != b"\x89PNG\r\n\x1a\n" or header[12:16] != b"IHDR":
        raise RuntimeError("rendered output is not a PNG")
    return struct.unpack(">II", header[16:24])


def _page_image_coverage(page: pdfplumber.page.Page) -> float:
    page_area = max(float(page.width) * float(page.height), 1.0)
    largest = 0.0
    for image in page.images:
        try:
            x0 = float(image["x0"])
            x1 = float(image["x1"])
            top = float(image["top"])
            bottom = float(image["bottom"])
        except (KeyError, TypeError, ValueError):
            continue
        width = max(0.0, min(float(page.width), max(x0, x1)) - max(0.0, min(x0, x1)))
        height = max(
            0.0,
            min(float(page.height), max(top, bottom)) - max(0.0, min(top, bottom)),
        )
        largest = max(largest, width * height)
    return min(1.0, largest / page_area)


def _hybrid_page_requires_ocr(
    page: pdfplumber.page.Page,
    raw_text: str,
    raw_words: list[dict[str, Any]],
) -> bool:
    sparse_text = (
        len(raw_words) <= HYBRID_MAX_TEXT_WORDS
        or len(raw_text) <= HYBRID_MAX_TEXT_CHARACTERS
    )
    if not sparse_text:
        return False
    try:
        return _page_image_coverage(page) >= HYBRID_MIN_IMAGE_COVERAGE
    except Exception:
        return True


def _parse_tesseract_tsv(
    tsv: str,
    *,
    source_path: str,
    page_number: int,
    rotation: int,
    limits: PdfLimits = PDF_LIMITS,
    image_width: int | None = None,
    image_height: int | None = None,
) -> dict[str, Any]:
    reliable_words: list[dict[str, Any]] = []
    low_confidence_words: list[dict[str, Any]] = []
    confidences: list[float] = []
    accepted_lines: set[tuple[int, int, int]] = set()
    parsed_word_count = 0
    limit_exceeded = False
    for index, line in enumerate(tsv.splitlines()):
        if index == 0:
            continue
        columns = line.split("\t", 11)
        if len(columns) != 12:
            continue
        word = columns[11].strip()
        if not word:
            continue
        parsed_word_count += 1
        if parsed_word_count > limits.max_ocr_words_per_page:
            limit_exceeded = True
            break
        try:
            block_number = int(columns[2])
            paragraph_number = int(columns[3])
            line_number = int(columns[4])
            left = int(columns[6])
            top = int(columns[7])
            width = int(columns[8])
            height = int(columns[9])
            confidence = float(columns[10])
        except ValueError:
            continue
        word_bbox = [left, top, left + width, top + height]
        valid_bbox = left >= 0 and top >= 0 and width > 0 and height > 0
        if image_width is not None:
            valid_bbox = valid_bbox and left + width <= image_width
        if image_height is not None:
            valid_bbox = valid_bbox and top + height <= image_height
        evidence = _pdf_evidence(
            source_path,
            page_number,
            f":ocr-word:{parsed_word_count}:bbox:{left},{top},{left + width},{top + height}",
            [float(value) for value in word_bbox],
        )
        record = {
            "text": word,
            "confidence": confidence,
            "bbox": word_bbox,
            "coordinate_space": "rendered_image_pixels",
            "rotation": rotation,
            "line_id": [block_number, paragraph_number, line_number],
            "evidence": evidence,
        }
        if math.isfinite(confidence) and confidence >= MIN_OCR_CONFIDENCE and valid_bbox:
            reliable_words.append(record)
            confidences.append(confidence)
            accepted_lines.add((block_number, paragraph_number, line_number))
        else:
            record["reliability_reason"] = (
                "invalid_bbox"
                if not valid_bbox
                else "low_confidence"
            )
            low_confidence_words.append(record)
    return {
        "text": " ".join(word["text"] for word in reliable_words),
        "mean_confidence": sum(confidences) / len(confidences) if confidences else None,
        "reliable_words": reliable_words,
        "low_confidence_words": low_confidence_words,
        "reliable_record_count": len(accepted_lines),
        "limit_exceeded": limit_exceeded,
    }


def _valid_pixel_bbox(
    value: Any,
    *,
    image_width: int,
    image_height: int,
) -> bool:
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        return False
    try:
        left, top, right, bottom = (float(coordinate) for coordinate in value)
    except (TypeError, ValueError):
        return False
    return (
        all(math.isfinite(coordinate) for coordinate in (left, top, right, bottom))
        and 0 <= left < right <= image_width
        and 0 <= top < bottom <= image_height
    )


def _union_bbox(values: Iterable[list[float] | list[int]]) -> list[float]:
    boxes = [list(value) for value in values]
    return _bbox(
        (
            min(box[0] for box in boxes),
            min(box[1] for box in boxes),
            max(box[2] for box in boxes),
            max(box[3] for box in boxes),
        )
    )


def _ocr_header_field(value: Any) -> str | None:
    normalized = re.sub(r"\s+", " ", str(value or "").strip().casefold())
    normalized = normalized.strip(":;")
    for field, aliases in OCR_TABLE_HEADER_ALIASES.items():
        if normalized in aliases:
            return field
    return None


def _ocr_numeric_value(value: Any) -> bool:
    return bool(OCR_NUMERIC_PATTERN.fullmatch(str(value or "").strip()))


def _ocr_table_candidates(
    words: list[dict[str, Any]],
    *,
    source_path: Path | str,
    page_number: int,
    rotation: int,
    image_width: int,
    image_height: int,
    limits: PdfLimits,
) -> tuple[list[dict[str, Any]], bool]:
    """Build bounded, non-authoritative table row candidates from OCR geometry.

    A candidate is deliberately not an extracted record.  Promotion is handled by
    the canonical layer only after a separate visual attestation.
    """

    grouped: dict[tuple[int, int, int], list[dict[str, Any]]] = {}
    for word in words:
        line_id = word.get("line_id")
        confidence = word.get("confidence")
        if (
            not isinstance(line_id, list)
            or len(line_id) != 3
            or not all(isinstance(part, int) for part in line_id)
            or not isinstance(confidence, (int, float))
            or not math.isfinite(float(confidence))
            or not _valid_pixel_bbox(
                word.get("bbox"),
                image_width=image_width,
                image_height=image_height,
            )
        ):
            continue
        grouped.setdefault(tuple(line_id), []).append(word)

    lines: list[dict[str, Any]] = []
    for line_id, line_words in grouped.items():
        ordered = sorted(line_words, key=lambda word: (word["bbox"][0], word["bbox"][1]))
        lines.append(
            {
                "line_id": line_id,
                "words": ordered,
                "bbox": _union_bbox([word["bbox"] for word in ordered]),
            }
        )
    lines.sort(key=lambda line: (line["bbox"][1], line["bbox"][0], line["line_id"]))

    headers: list[tuple[int, dict[str, dict[str, Any]]]] = []
    for line_index, line in enumerate(lines):
        mapped: dict[str, dict[str, Any]] = {}
        ambiguous = False
        for word in line["words"]:
            field = _ocr_header_field(word.get("text"))
            if field is None:
                continue
            if field in mapped:
                ambiguous = True
                break
            mapped[field] = word
        if (
            not ambiguous
            and set(OCR_TABLE_REQUIRED_FIELDS) <= set(mapped)
            and all(
                float(mapped[field]["confidence"]) >= MIN_OCR_TABLE_CONFIDENCE
                for field in OCR_TABLE_REQUIRED_FIELDS
            )
        ):
            headers.append((line_index, mapped))

    maximum_candidates = max(
        0,
        min(
            MAX_OCR_TABLE_CANDIDATES_PER_PAGE,
            int(limits.max_ocr_words_per_page) // len(OCR_TABLE_REQUIRED_FIELDS),
            int(limits.max_total_table_cells) // len(OCR_TABLE_REQUIRED_FIELDS),
        ),
    )
    candidates: list[dict[str, Any]] = []
    truncated = False
    for table_index, (header_line_index, mapped_header) in enumerate(headers, start=1):
        next_header_index = (
            headers[table_index][0]
            if table_index < len(headers)
            else len(lines)
        )
        ordered_columns = sorted(
            mapped_header.items(),
            key=lambda item: (item[1]["bbox"][0] + item[1]["bbox"][2]) / 2,
        )
        centers = [
            (word["bbox"][0] + word["bbox"][2]) / 2
            for _field, word in ordered_columns
        ]
        if any(right <= left for left, right in zip(centers, centers[1:])):
            continue
        boundaries = [0.0]
        boundaries.extend(
            (left + right) / 2 for left, right in zip(centers, centers[1:])
        )
        boundaries.append(float(image_width))

        row_index = 0
        for line in lines[header_line_index + 1 : next_header_index]:
            if line["bbox"][1] < lines[header_line_index]["bbox"][3]:
                continue
            assigned: dict[str, list[dict[str, Any]]] = {
                field: [] for field, _word in ordered_columns
            }
            for word in line["words"]:
                center = (word["bbox"][0] + word["bbox"][2]) / 2
                for column_index, (field, _header_word) in enumerate(ordered_columns):
                    lower = boundaries[column_index]
                    upper = boundaries[column_index + 1]
                    if lower <= center < upper or (
                        column_index == len(ordered_columns) - 1 and center == upper
                    ):
                        assigned[field].append(word)
                        break
            if not all(assigned.get(field) for field in OCR_TABLE_REQUIRED_FIELDS):
                continue
            used_words = [
                word
                for field in OCR_TABLE_REQUIRED_FIELDS
                for word in assigned[field]
            ]
            if any(
                float(word["confidence"]) < MIN_OCR_TABLE_CONFIDENCE
                for word in used_words
            ):
                continue
            field_values = {
                field: " ".join(word["text"] for word in assigned[field]).strip()
                for field in OCR_TABLE_REQUIRED_FIELDS
            }
            if not all(
                _ocr_numeric_value(field_values[field])
                for field in ("quantity", "unit_price", "declared_total")
            ):
                continue
            if len(candidates) >= maximum_candidates:
                truncated = True
                break
            row_index += 1
            candidate_id = (
                f"page:{page_number}:ocr-table:{table_index}:row:{row_index}"
            )
            row_bbox = _union_bbox([word["bbox"] for word in used_words])
            header_bbox = lines[header_line_index]["bbox"]
            cells: list[dict[str, Any]] = []
            for field in OCR_TABLE_REQUIRED_FIELDS:
                cell_words = assigned[field]
                cell_bbox = _union_bbox([word["bbox"] for word in cell_words])
                cells.append(
                    {
                        "field": field,
                        "text": field_values[field],
                        "confidence": min(float(word["confidence"]) for word in cell_words),
                        "bbox": cell_bbox,
                        "evidence": _pdf_evidence(
                            source_path,
                            page_number,
                            (
                                f":ocr-table:{table_index}:row:{row_index}:"
                                f"cell:{field}"
                            ),
                            cell_bbox,
                        ),
                    }
                )
            candidates.append(
                {
                    "record_type": "ocr_table_row_candidate",
                    "candidate_id": candidate_id,
                    "page": page_number,
                    "table_index": table_index,
                    "row_index": row_index,
                    "confidence": min(float(word["confidence"]) for word in used_words),
                    "bbox": row_bbox,
                    "table_bbox": _union_bbox([header_bbox, row_bbox]),
                    "rendered_image_size": [image_width, image_height],
                    "coordinate_space": "rendered_image_pixels",
                    "rotation": rotation,
                    "proposed_fields": field_values,
                    "cells": cells,
                    "visual_verification_required": True,
                    "evidence": _pdf_evidence(
                        source_path,
                        page_number,
                        f":ocr-table:{table_index}:row:{row_index}",
                        row_bbox,
                    ),
                }
            )
        if truncated:
            break
    return candidates, truncated


def _empty_ocr_result(*, attempted: bool, status: str) -> dict[str, Any]:
    return {
        "ocr_attempted": attempted,
        "ocr_text": "",
        "ocr_mean_confidence": None,
        "ocr_words": [],
        "ocr_low_confidence_words": [],
        "ocr_table_candidates": [],
        "ocr_table_candidate_count": 0,
        "records": [],
        "record_count": 0,
        "row_level_semantics": False,
        "render_dpi": None,
        "render_pixel_count": 0,
        "visual_verification_required": True,
        "extraction_status": status,
    }


def _ocr_page(
    path: Path,
    page_number: int,
    rotation: int,
    limits: PdfLimits,
    *,
    page_width_points: float | None = None,
    page_height_points: float | None = None,
    remaining_render_pixels: int | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    page_evidence = _pdf_evidence(path, page_number)
    if page_width_points is None or page_height_points is None:
        try:
            reader = PdfReader(path, strict=False)
            page_width_points, page_height_points = _page_box_dimensions(
                reader.pages[page_number - 1]
            )
        except Exception:
            return (
                _empty_ocr_result(attempted=False, status="limited"),
                [
                    {
                        "code": "pdf_render_limit_exceeded",
                        "message": "Размер страницы не удалось безопасно определить до рендера.",
                        "evidence": [page_evidence],
                    }
                ],
            )
    render_plan = _render_plan(
        page_width_points=page_width_points,
        page_height_points=page_height_points,
        rotation=rotation,
        remaining_render_pixels=(
            limits.max_total_render_pixels
            if remaining_render_pixels is None
            else remaining_render_pixels
        ),
        limits=limits,
    )
    if render_plan is None:
        return (
            _empty_ocr_result(attempted=False, status="limited"),
            [
                {
                    "code": "pdf_render_limit_exceeded",
                    "message": "Размер страницы превышает безопасный предел локального рендера.",
                    "evidence": [page_evidence],
                }
            ],
        )
    renderer = shutil.which("pdftoppm")
    tesseract = shutil.which("tesseract")
    if renderer is None or tesseract is None:
        return (
            _empty_ocr_result(attempted=False, status="unreadable"),
            [
                {
                    "code": "ocr_unavailable",
                    "message": "Локальный OCR недоступен: отсутствует pdftoppm или tesseract.",
                    "evidence": [page_evidence],
                }
            ],
        )
    limitations: list[dict[str, Any]] = []
    if render_plan["resolution_reduced"]:
        limitations.append(
            {
                "code": "pdf_render_resolution_reduced",
                "message": "DPI локального рендера снижен для соблюдения пиксельных лимитов.",
                "render_dpi": render_plan["dpi"],
                "evidence": [page_evidence],
            }
        )
    try:
        with tempfile.TemporaryDirectory(prefix="smetchik-pdf-") as temporary:
            os.chmod(temporary, 0o700)
            render_base = Path(temporary) / "page"
            render = subprocess.run(
                [
                    renderer,
                    "-f",
                    str(page_number),
                    "-l",
                    str(page_number),
                    "-singlefile",
                    "-png",
                    "-r",
                    str(render_plan["dpi"]),
                    str(path),
                    str(render_base),
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=PROCESS_TIMEOUT_SECONDS,
            )
            image_path = render_base.with_suffix(".png")
            if render.returncode != 0 or not image_path.is_file():
                raise RuntimeError("local render failed")
            if image_path.stat().st_size > limits.max_rendered_image_bytes:
                raise RuntimeError("rendered image exceeds byte limit")
            rendered_width, rendered_height = _png_dimensions(image_path)
            rendered_pixels = rendered_width * rendered_height
            if (
                rendered_width > limits.max_render_dimension_pixels
                or rendered_height > limits.max_render_dimension_pixels
                or rendered_pixels > limits.max_render_pixels_per_page
                or rendered_pixels
                > (
                    limits.max_total_render_pixels
                    if remaining_render_pixels is None
                    else remaining_render_pixels
                )
            ):
                raise RuntimeError("rendered image exceeds pixel limit")
            recognized = subprocess.run(
                [tesseract, str(image_path), "stdout", "-l", "rus+eng", "--psm", "6", "tsv"],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=PROCESS_TIMEOUT_SECONDS,
            )
            if recognized.returncode != 0:
                raise RuntimeError("local OCR failed")
            parsed = _parse_tesseract_tsv(
                recognized.stdout,
                source_path=str(path),
                page_number=page_number,
                rotation=rotation,
                limits=limits,
                image_width=rendered_width,
                image_height=rendered_height,
            )
    except (OSError, RuntimeError, subprocess.SubprocessError):
        return (
            {
                **_empty_ocr_result(attempted=True, status="unreadable"),
                "render_dpi": render_plan["dpi"],
                "render_pixel_count": render_plan["pixel_count"],
            },
            limitations
            + [
                {
                    "code": "ocr_failed",
                    "message": "Локальное распознавание страницы не завершилось.",
                    "evidence": [page_evidence],
                }
            ],
        )

    result = {
        "ocr_attempted": True,
        "ocr_text": parsed["text"],
        "ocr_mean_confidence": parsed["mean_confidence"],
        "ocr_words": parsed["reliable_words"],
        "ocr_low_confidence_words": parsed["low_confidence_words"],
        "ocr_table_candidates": [],
        "ocr_table_candidate_count": 0,
        "records": [],
        "record_count": 0,
        "row_level_semantics": False,
        "render_dpi": render_plan["dpi"],
        "render_pixel_count": render_plan["pixel_count"],
        "visual_verification_required": True,
        "extraction_status": "partial" if parsed["reliable_words"] else "unreadable",
    }
    candidates, candidates_truncated = _ocr_table_candidates(
        parsed["reliable_words"],
        source_path=path,
        page_number=page_number,
        rotation=rotation,
        image_width=rendered_width,
        image_height=rendered_height,
        limits=limits,
    )
    result["ocr_table_candidates"] = candidates
    result["ocr_table_candidate_count"] = len(candidates)
    limitations.append(
        {
            "code": "pdf_visual_verification_required",
            "message": "Страница распознана OCR и требует визуальной сверки с рендером.",
            "evidence": [page_evidence],
        }
    )
    if parsed["low_confidence_words"]:
        limitations.append(
            {
                "code": "ocr_low_confidence_words",
                "message": (
                    "OCR содержит отдельно перечисленные слова с низкой уверенностью "
                    "или недостоверной геометрией."
                ),
                "count": len(parsed["low_confidence_words"]),
                "evidence": [word["evidence"] for word in parsed["low_confidence_words"][:100]],
            }
        )
    if parsed["limit_exceeded"]:
        result["extraction_status"] = "limited"
        limitations.append(
            {
                "code": "pdf_limit_exceeded",
                "message": "OCR остановлен по лимиту количества слов на странице.",
                "evidence": [page_evidence],
            }
        )
    if candidates_truncated:
        result["extraction_status"] = "limited"
        limitations.append(
            {
                "code": "pdf_ocr_table_candidate_limit_exceeded",
                "message": "Число OCR-кандидатов таблицы ограничено безопасным пределом.",
                "evidence": [page_evidence],
            }
        )
    if not parsed["reliable_words"]:
        limitations.append(
            {
                "code": "ocr_no_reliable_text",
                "message": "OCR не дал строк достаточной уверенности; данные страницы не подтверждены.",
                "evidence": [page_evidence],
            }
        )
    return result, limitations


def _word_result(
    word: dict[str, Any],
    path: Path,
    page_number: int,
    word_number: int,
    rotation: int,
) -> dict[str, Any]:
    word_bbox = _bbox((word["x0"], word["top"], word["x1"], word["bottom"]))
    return {
        "text": str(word.get("text", "")),
        "bbox": word_bbox,
        "coordinate_space": "pdf_points_top_origin",
        "rotation": rotation,
        "evidence": _pdf_evidence(
            path,
            page_number,
            f":word:{word_number}:bbox:{','.join(str(value) for value in word_bbox)}",
            word_bbox,
        ),
    }


def _line_records(
    words: list[dict[str, Any]],
    path: Path,
    page_number: int,
) -> list[dict[str, Any]]:
    lines: list[list[dict[str, Any]]] = []
    for word in sorted(words, key=lambda item: (item["bbox"][1], item["bbox"][0])):
        if not lines or abs(lines[-1][0]["bbox"][1] - word["bbox"][1]) > 3.0:
            lines.append([word])
        else:
            lines[-1].append(word)
    records: list[dict[str, Any]] = []
    for record_number, line_words in enumerate(lines, start=1):
        ordered = sorted(line_words, key=lambda item: item["bbox"][0])
        record_bbox = [
            min(item["bbox"][0] for item in ordered),
            min(item["bbox"][1] for item in ordered),
            max(item["bbox"][2] for item in ordered),
            max(item["bbox"][3] for item in ordered),
        ]
        records.append(
            {
                "record_type": "text_line",
                "record_index": record_number,
                "text": " ".join(item["text"] for item in ordered),
                "bbox": record_bbox,
                "evidence": _pdf_evidence(path, page_number, f":line:{record_number}", record_bbox),
            }
        )
    return records


def _table_results(
    page: pdfplumber.page.Page,
    path: Path,
    page_number: int,
    limits: PdfLimits,
    used_cells: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], int, bool]:
    page_evidence = _pdf_evidence(path, page_number)

    def failed(message: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]], int, bool]:
        return (
            [],
            [
                {
                    "code": "pdf_table_extraction_failed",
                    "message": message,
                    "evidence": [page_evidence],
                }
            ],
            used_cells,
            True,
        )

    try:
        detected = page.find_tables()
    except Exception:
        return failed("Таблицы страницы не удалось обнаружить с координатами.")
    if not isinstance(detected, list):
        return failed("Координатный парсер вернул неподдерживаемый результат таблиц.")
    if len(detected) > limits.max_tables_per_page:
        return (
            [],
            [
                {
                    "code": "pdf_limit_exceeded",
                    "message": "Число таблиц на странице превышает безопасный лимит.",
                    "evidence": [page_evidence],
                }
            ],
            used_cells,
            True,
        )
    tables: list[dict[str, Any]] = []
    for table_number, table in enumerate(detected, start=1):
        try:
            table_bbox = _bbox(table.bbox)
            source_rows = list(table.rows)
        except Exception:
            return failed("Геометрию строк таблицы не удалось извлечь.")
        prospective_cells = used_cells
        try:
            for source_row in source_rows:
                prospective_cells += len(source_row.cells)
                if prospective_cells > limits.max_total_table_cells:
                    return (
                        tables,
                        [
                            {
                                "code": "pdf_limit_exceeded",
                                "message": "Число ячеек таблиц превышает безопасный лимит.",
                                "evidence": [page_evidence],
                            }
                        ],
                        used_cells,
                        True,
                    )
            extracted_rows = table.extract()
        except Exception:
            return failed("Содержимое ячеек таблицы не удалось извлечь.")
        if not isinstance(extracted_rows, list) or len(extracted_rows) != len(source_rows):
            return failed("Извлечённые строки таблицы не совпали с её геометрией.")
        row_results: list[dict[str, Any]] = []
        for row_number, (row, extracted_cells) in enumerate(
            zip(source_rows, extracted_rows), start=1
        ):
            if not isinstance(extracted_cells, list) or len(extracted_cells) != len(row.cells):
                return failed("Извлечённые ячейки таблицы не совпали с её геометрией.")
            cells: list[dict[str, Any]] = []
            for column_number, (cell_bbox_raw, cell_text) in enumerate(
                zip(row.cells, extracted_cells), start=1
            ):
                used_cells += 1
                cell_bbox = _bbox(cell_bbox_raw) if cell_bbox_raw is not None else None
                cells.append(
                    {
                        "column_index": column_number,
                        "text": cell_text or "",
                        "bbox": cell_bbox,
                        "evidence": _pdf_evidence(
                            path,
                            page_number,
                            f":table:{table_number}:row:{row_number}:cell:{column_number}",
                            cell_bbox,
                        ),
                    }
                )
            row_bbox = _bbox(row.bbox)
            row_results.append(
                {
                    "row_index": row_number,
                    "bbox": row_bbox,
                    "cells": cells,
                    "evidence": _pdf_evidence(
                        path, page_number, f":table:{table_number}:row:{row_number}", row_bbox
                    ),
                }
            )
        tables.append(
            {
                "table_index": table_number,
                "bbox": table_bbox,
                "rows": row_results,
                "evidence": _pdf_evidence(path, page_number, f":table:{table_number}", table_bbox),
            }
        )
    return tables, [], used_cells, False


def _records_from_tables(tables: list[dict[str, Any]]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for table in tables:
        for row in table["rows"]:
            if not any(cell["text"].strip() for cell in row["cells"]):
                continue
            records.append(
                {
                    "record_type": "table_row",
                    "table_index": table["table_index"],
                    "row_index": row["row_index"],
                    "text": [cell["text"] for cell in row["cells"]],
                    "bbox": row["bbox"],
                    "evidence": row["evidence"],
                }
            )
    return records


def _terminal_pdf_result(
    path: Path,
    *,
    code: str,
    message: str,
    status: str,
    encrypted: bool = False,
    page_count: int = 0,
) -> tuple[dict[str, Any], str, list[dict[str, Any]], int]:
    return (
        {
            "format": "pdf",
            "encrypted": encrypted,
            "page_count": page_count,
            "pages": [],
            "visual_verification_required": False,
        },
        status,
        [
            {
                "code": code,
                "message": message,
                "evidence": [{"source_path": str(path), "locator": f"{path}:pdf"}],
            }
        ],
        0,
    )


def extract_pdf(
    path: Path,
    *,
    limits: PdfLimits = PDF_LIMITS,
) -> tuple[dict[str, Any], str, list[dict[str, Any]], int]:
    try:
        file_size = path.stat().st_size
    except OSError:
        return _terminal_pdf_result(
            path, code="unreadable_pdf", message="PDF недоступен для локального чтения.", status="failed"
        )
    if file_size > limits.max_file_bytes:
        return _terminal_pdf_result(
            path,
            code="pdf_limit_exceeded",
            message="PDF превышает безопасный лимит размера файла.",
            status="rejected",
        )
    try:
        reader = PdfReader(path, strict=False)
        if reader.is_encrypted:
            try:
                unlocked = reader.decrypt("")
            except Exception:
                unlocked = 0
            if not unlocked:
                return _terminal_pdf_result(
                    path,
                    code="encrypted_pdf",
                    message="PDF зашифрован и не может быть прочитан без входных данных.",
                    status="failed",
                    encrypted=True,
                )
        page_count = len(reader.pages)
        page_geometries: list[tuple[float, float] | None] = []
        for reader_page in reader.pages:
            try:
                page_geometries.append(_page_box_dimensions(reader_page))
            except Exception:
                page_geometries.append(None)
    except Exception:
        return _terminal_pdf_result(
            path, code="unreadable_pdf", message="PDF не прочитан локальным парсером.", status="failed"
        )
    if page_count > limits.max_pages:
        return _terminal_pdf_result(
            path,
            code="pdf_limit_exceeded",
            message="PDF превышает безопасный лимит количества страниц.",
            status="rejected",
            page_count=page_count,
        )

    pages: list[dict[str, Any]] = []
    limitations: list[dict[str, Any]] = []
    reliable_records = 0
    total_text_characters = 0
    total_words = 0
    total_table_cells = 0
    ocr_pages_started = 0
    render_pixels_reserved = 0

    def run_page_ocr(
        *,
        page_number: int,
        rotation: int,
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        nonlocal ocr_pages_started, render_pixels_reserved
        page_evidence = _pdf_evidence(path, page_number)
        if ocr_pages_started >= limits.max_ocr_pages:
            return (
                _empty_ocr_result(attempted=False, status="limited"),
                [
                    {
                        "code": "pdf_ocr_page_limit_exceeded",
                        "message": "Достигнут безопасный лимит числа OCR-страниц документа.",
                        "evidence": [page_evidence],
                    }
                ],
            )
        ocr_pages_started += 1
        geometry = page_geometries[page_number - 1]
        result, ocr_limits = _ocr_page(
            path,
            page_number,
            rotation,
            limits,
            page_width_points=geometry[0] if geometry else None,
            page_height_points=geometry[1] if geometry else None,
            remaining_render_pixels=max(
                0,
                limits.max_total_render_pixels - render_pixels_reserved,
            ),
        )
        render_pixels_reserved += max(0, int(result.get("render_pixel_count") or 0))
        return result, ocr_limits

    try:
        document = pdfplumber.open(path)
    except Exception:
        return _terminal_pdf_result(
            path,
            code="unreadable_pdf",
            message="PDF не прочитан координатным парсером.",
            status="failed",
            page_count=page_count,
        )
    with document:
        for page_number, page in enumerate(document.pages, start=1):
            rotation = int(page.rotation or 0) % 360
            page_evidence = _pdf_evidence(path, page_number)
            page_result: dict[str, Any] = {
                "page": page_number,
                "rotation": rotation,
                "text_layer_present": False,
                "text": "",
                "words": [],
                "tables": [],
                "records": [],
                "record_count": 0,
                "ocr_attempted": False,
                "ocr_text": "",
                "ocr_mean_confidence": None,
                "ocr_words": [],
                "ocr_low_confidence_words": [],
                "ocr_table_candidates": [],
                "ocr_table_candidate_count": 0,
                "row_level_semantics": False,
                "render_dpi": None,
                "render_pixel_count": 0,
                "visual_verification_required": False,
                "extraction_status": "unreadable",
                "evidence": page_evidence,
            }
            try:
                raw_text = _normalized_text(page.extract_text())
                raw_words = page.extract_words(use_text_flow=False, keep_blank_chars=False)
            except Exception:
                raw_text = ""
                raw_words = []
                limitations.append(
                    {
                        "code": "pdf_text_extraction_failed",
                        "message": "Текстовый слой страницы не удалось извлечь с координатами.",
                        "evidence": [page_evidence],
                    }
                )
            page_result["text_layer_present"] = bool(raw_words)
            exceeds_text = (
                len(raw_text) > limits.max_page_text_characters
                or total_text_characters + len(raw_text) > limits.max_total_text_characters
            )
            exceeds_words = (
                len(raw_words) > limits.max_words_per_page
                or total_words + len(raw_words) > limits.max_total_words
            )
            if exceeds_text or exceeds_words:
                page_result["extraction_status"] = "limited"
                page_result["visual_verification_required"] = True
                limitations.append(
                    {
                        "code": "pdf_limit_exceeded",
                        "message": "Извлечение страницы остановлено по лимиту текста или слов.",
                        "evidence": [page_evidence],
                    }
                )
                pages.append(page_result)
                continue
            total_text_characters += len(raw_text)
            total_words += len(raw_words)
            words = [
                _word_result(word, path, page_number, index, rotation)
                for index, word in enumerate(raw_words, start=1)
            ]
            page_result["text"] = raw_text
            page_result["words"] = words
            if words:
                tables, table_limits, total_table_cells, table_extraction_incomplete = _table_results(
                    page, path, page_number, limits, total_table_cells
                )
                limitations.extend(table_limits)
                page_result["tables"] = tables
                table_records = _records_from_tables(tables)
                page_result["records"] = table_records or _line_records(words, path, page_number)
                page_result["record_count"] = len(page_result["records"])
                page_result["extraction_status"] = (
                    "limited" if table_extraction_incomplete else "reliable"
                )
                if table_extraction_incomplete:
                    page_result["visual_verification_required"] = True
                if _hybrid_page_requires_ocr(page, raw_text, raw_words):
                    limitations.append(
                        {
                            "code": "pdf_hybrid_page_requires_ocr",
                            "message": (
                                "Страница содержит крупное изображение и недостаточный текстовый слой; "
                                "выполнен OCR с обязательной визуальной сверкой."
                            ),
                            "evidence": [page_evidence],
                        }
                    )
                    ocr_result, ocr_limits = run_page_ocr(
                        page_number=page_number,
                        rotation=rotation,
                    )
                    for key in (
                        "ocr_attempted",
                        "ocr_text",
                        "ocr_mean_confidence",
                        "ocr_words",
                        "ocr_low_confidence_words",
                        "ocr_table_candidates",
                        "ocr_table_candidate_count",
                        "render_dpi",
                        "render_pixel_count",
                    ):
                        page_result[key] = ocr_result.get(key)
                    page_result["row_level_semantics"] = False
                    page_result["visual_verification_required"] = True
                    if page_result["extraction_status"] != "limited":
                        page_result["extraction_status"] = (
                            "partial"
                            if ocr_result.get("extraction_status") == "partial"
                            else "limited"
                        )
                    limitations.extend(ocr_limits)
            else:
                ocr_result, ocr_limits = run_page_ocr(
                    page_number=page_number,
                    rotation=rotation,
                )
                page_result.update(ocr_result)
                page_result["words"] = ocr_result["ocr_words"]
                if ocr_result["ocr_text"]:
                    page_result["text"] = ocr_result["ocr_text"]
                limitations.extend(ocr_limits)
            reliable_records += int(page_result["record_count"])
            pages.append(page_result)

    details = {
        "format": "pdf",
        "encrypted": False,
        "page_count": page_count,
        "pages": pages,
        "visual_verification_required": any(page["visual_verification_required"] for page in pages),
    }
    status = "partial" if limitations else "reliable"
    return details, status, limitations, reliable_records
