from __future__ import annotations

import json
import stat
import zipfile
from pathlib import Path
from types import SimpleNamespace

from PIL import Image
from pypdf import PdfWriter

import pdf_extract
import safety_limits
from canonical_model import build_canonical_model
from pdf_extract import _ocr_page
from smetchik_engine import inspect_input


PASSPORT = {
    "object": "Учебный объект",
    "work_type": "construction",
    "funding_source": "budget",
    "region_or_price_zone": "67",
    "price_level_date": "2026-08-01",
    "calculation_method": "resource_index",
    "stage": "project_documentation",
    "document_set": ["LSR"],
}

HEADER_BBOX = [10, 10, 390, 30]
ROW_BBOX = [10, 45, 390, 65]
CANDIDATE_ID = "page:1:ocr-table:1:row:1"


def _blank_pdf(path: Path) -> None:
    writer = PdfWriter()
    writer.add_blank_page(width=300, height=100)
    with path.open("wb") as stream:
        writer.write(stream)


def _tsv_word(
    *,
    line: int,
    word: int,
    left: int,
    width: int,
    text: str,
    confidence: float = 98.0,
) -> str:
    top = 10 if line == 1 else 45
    return (
        f"5\t1\t1\t1\t{line}\t{word}\t{left}\t{top}\t{width}\t20\t"
        f"{confidence}\t{text}"
    )


def _confident_table_tsv(*, row_total_confidence: float = 98.0) -> str:
    header = [
        (10, 120, "Name"),
        (140, 40, "Unit"),
        (190, 60, "Quantity"),
        (270, 45, "Price"),
        (335, 55, "Total"),
    ]
    row = [
        (10, 120, "Work", 98.0),
        (140, 40, "m2", 98.0),
        (190, 60, "2", 98.0),
        (270, 45, "100", 98.0),
        (335, 55, "200", row_total_confidence),
    ]
    lines = [
        "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext"
    ]
    lines.extend(
        _tsv_word(line=1, word=index, left=left, width=width, text=text)
        for index, (left, width, text) in enumerate(header, 1)
    )
    lines.extend(
        _tsv_word(
            line=2,
            word=index,
            left=left,
            width=width,
            text=text,
            confidence=confidence,
        )
        for index, (left, width, text, confidence) in enumerate(row, 1)
    )
    return "\n".join(lines)


def _run_real_ocr_with_fake_binaries(
    source: Path,
    monkeypatch,
    *,
    tsv: str,
) -> tuple[dict, list[dict]]:
    monkeypatch.setattr(pdf_extract.shutil, "which", lambda name: f"/usr/bin/{name}")

    def fake_run(command, **_kwargs):
        if command[0].endswith("pdftoppm"):
            temporary = Path(command[-1]).parent
            assert stat.S_IMODE(temporary.stat().st_mode) == 0o700
            Image.new("RGB", (400, 100), "white").save(
                Path(command[-1]).with_suffix(".png")
            )
            return SimpleNamespace(returncode=0, stdout="")
        return SimpleNamespace(returncode=0, stdout=tsv)

    monkeypatch.setattr(pdf_extract.subprocess, "run", fake_run)
    return _ocr_page(
        source,
        1,
        0,
        safety_limits.PDF_LIMITS,
        page_width_points=300,
        page_height_points=100,
        remaining_render_pixels=safety_limits.PDF_LIMITS.max_total_render_pixels,
    )


def _candidate(*, candidate_id: str = CANDIDATE_ID) -> dict:
    fields = {
        "name": "Work",
        "unit": "m2",
        "quantity": "2",
        "unit_price": "100",
        "declared_total": "200",
    }
    return {
        "record_type": "ocr_table_row_candidate",
        "candidate_id": candidate_id,
        "page": 1,
        "table_index": 1,
        "row_index": 1,
        "confidence": 98.0,
        "bbox": ROW_BBOX,
        "table_bbox": [10, 10, 390, 65],
        "rendered_image_size": [400, 100],
        "coordinate_space": "rendered_image_pixels",
        "rotation": 0,
        "proposed_fields": fields,
        "cells": [
            {
                "field": field,
                "text": value,
                "confidence": 98.0,
                "bbox": bbox,
            }
            for (field, value), bbox in zip(
                fields.items(),
                (
                    [10, 45, 130, 65],
                    [140, 45, 180, 65],
                    [190, 45, 250, 65],
                    [270, 45, 315, 65],
                    [335, 45, 390, 65],
                ),
            )
        ],
        "visual_verification_required": True,
        "evidence": {
            "source_path": "scan.pdf",
            "page": 1,
            "bbox": ROW_BBOX,
            "locator": f"scan.pdf:{candidate_id}",
        },
    }


def _fake_ocr_result(path: Path, candidate: dict) -> tuple[dict, list[dict]]:
    candidate = json.loads(json.dumps(candidate))
    candidate["evidence"]["source_path"] = str(path)
    candidate["evidence"]["locator"] = f"{path}:{candidate['candidate_id']}"
    return (
        {
            "ocr_attempted": True,
            "ocr_text": "Name Unit Quantity Price Total Work m2 2 100 200",
            "ocr_mean_confidence": 98.0,
            "ocr_words": [],
            "ocr_low_confidence_words": [],
            "ocr_table_candidates": [candidate],
            "records": [],
            "record_count": 0,
            "row_level_semantics": False,
            "render_dpi": 200,
            "render_pixel_count": 40_000,
            "visual_verification_required": True,
            "extraction_status": "partial",
        },
        [
            {
                "code": "pdf_visual_verification_required",
                "message": "visual check",
                "evidence": [candidate["evidence"]],
            }
        ],
    )


def _attestation(
    *,
    source_path: str = "scan.pdf",
    candidate_id: str = CANDIDATE_ID,
    bbox: list[int] = ROW_BBOX,
) -> dict:
    return {
        "record_type": "pdf_ocr_visual_attestation",
        "candidate_id": candidate_id,
        "confirmed": True,
        "evidence": {
            "source_path": source_path,
            "page": 1,
            "bbox": bbox,
            "locator": f"{source_path}:{candidate_id}:visual-attestation",
        },
    }


def test_confident_ocr_table_emits_bbox_candidate_but_never_a_row(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = tmp_path / "scan.pdf"
    _blank_pdf(source)

    result, limitations = _run_real_ocr_with_fake_binaries(
        source,
        monkeypatch,
        tsv=_confident_table_tsv(),
    )

    assert result["records"] == []
    assert result["record_count"] == 0
    assert result["row_level_semantics"] is False
    assert result["visual_verification_required"] is True
    assert result["extraction_status"] == "partial"
    assert len(result["ocr_table_candidates"]) == 1
    candidate = result["ocr_table_candidates"][0]
    assert candidate["candidate_id"] == CANDIDATE_ID
    assert candidate["bbox"] == ROW_BBOX
    assert candidate["table_bbox"] == [10, 10, 390, 65]
    assert candidate["rendered_image_size"] == [400, 100]
    assert candidate["proposed_fields"] == {
        "name": "Work",
        "unit": "m2",
        "quantity": "2",
        "unit_price": "100",
        "declared_total": "200",
    }
    assert all(len(cell["bbox"]) == 4 for cell in candidate["cells"])
    assert candidate["evidence"]["source_path"] == str(source)
    assert candidate["evidence"]["page"] == 1
    assert candidate["evidence"]["locator"].endswith(CANDIDATE_ID)
    assert any(limit["code"] == "pdf_visual_verification_required" for limit in limitations)


def test_ocr_table_candidate_requires_high_confidence_for_every_semantic_cell(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = tmp_path / "scan.pdf"
    _blank_pdf(source)

    result, _limitations = _run_real_ocr_with_fake_binaries(
        source,
        monkeypatch,
        tsv=_confident_table_tsv(row_total_confidence=89.0),
    )

    assert result["ocr_words"]
    assert result["ocr_table_candidates"] == []
    assert result["records"] == []


def test_ocr_candidate_is_not_canonical_without_private_visual_attestation(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = tmp_path / "scan.pdf"
    _blank_pdf(source)
    monkeypatch.setattr(
        pdf_extract,
        "_ocr_page",
        lambda path, *_args, **_kwargs: _fake_ocr_result(path, _candidate()),
    )

    result = inspect_input(
        source,
        mode="full",
        purpose="internal_review",
        context=PASSPORT,
    )
    serialized = json.dumps(result, ensure_ascii=False)

    assert result["coverage"]["checkable_records"] == 0
    assert result["coverage"]["row_level_checked"] is False
    assert result["input_inventory"][0]["details"]["pages"][0]["record_count"] == 0
    gap = next(
        evidence
        for limitation in result["limitations"]
        if limitation["code"] == "unclassified_candidate_ranges"
        for evidence in limitation["evidence"]
        if evidence.get("reason") == "pdf_ocr_table_visual_attestation_required"
    )
    assert gap["source_path"] == source.name
    assert gap["page"] == 1
    assert gap["bbox"] == ROW_BBOX
    assert gap["locator"].endswith(CANDIDATE_ID)
    assert str(tmp_path) not in serialized
    assert "smetchik-pdf-" not in serialized


def test_private_visual_attestation_promotes_exact_candidate_to_checkable_row(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = tmp_path / "scan.pdf"
    _blank_pdf(source)
    monkeypatch.setattr(
        pdf_extract,
        "_ocr_page",
        lambda path, *_args, **_kwargs: _fake_ocr_result(path, _candidate()),
    )

    result = inspect_input(
        source,
        mode="full",
        purpose="internal_review",
        context=PASSPORT,
        _trusted_records=[_attestation()],
    )

    assert result["coverage"]["checkable_records"] == 1
    assert result["coverage"]["arithmetic_checked_records"] == 0
    assert result["coverage"]["row_level_checked"] is False
    arithmetic = next(check for check in result["checks"] if check["id"] == "ARITH-01")
    assert arithmetic["status"] == "limited"
    assert arithmetic["parameters"]["row_states"] == [
        {
            "row_id": f"{source.name}:{CANDIDATE_ID}",
            "status": "limited",
            "reason": "calculation_basis_not_verified",
        }
    ]
    assert not any(finding["id"].startswith("ARITH-01") for finding in result["findings"])
    gap = result["coverage"]["full_control_gaps"][0]
    assert gap["row_id"] == f"{source.name}:{CANDIDATE_ID}"
    assert gap["evidence"]["source_path"] == source.name
    assert gap["evidence"]["page"] == 1
    assert gap["evidence"]["bbox"] == ROW_BBOX


def test_mismatched_bounds_or_duplicate_candidate_ids_are_rejected() -> None:
    inventory = [
        {
            "path": "scan.pdf",
            "file_type": "pdf",
            "details": {
                "format": "pdf",
                "pages": [
                    {
                        "page": 1,
                        "extraction_status": "partial",
                        "ocr_table_candidates": [_candidate(), _candidate()],
                    }
                ],
            },
        }
    ]

    duplicate_candidate_model = build_canonical_model(
        inventory,
        {"semantic_records": [_attestation()]},
    )
    assert duplicate_candidate_model["rows"] == []
    assert any(
        gap["reason"] == "duplicate_pdf_ocr_table_candidate_id"
        for gap in duplicate_candidate_model["unclassified_candidate_ranges"]
    )

    outside_render = _candidate()
    outside_render["bbox"] = [10, 45, 401, 65]
    outside_render["table_bbox"] = [10, 10, 401, 65]
    outside_render["cells"][-1]["bbox"] = [335, 45, 401, 65]
    outside_render["evidence"]["bbox"] = [10, 45, 401, 65]
    inventory[0]["details"]["pages"][0]["ocr_table_candidates"] = [outside_render]
    outside_render_model = build_canonical_model(
        inventory,
        {"semantic_records": [_attestation(bbox=[10, 45, 401, 65])]},
    )
    assert outside_render_model["rows"] == []
    assert any(
        gap["reason"] == "invalid_pdf_ocr_table_candidate"
        for gap in outside_render_model["unclassified_candidate_ranges"]
    )

    inventory[0]["details"]["pages"][0]["ocr_table_candidates"] = [_candidate()]
    mismatched_bounds_model = build_canonical_model(
        inventory,
        {"semantic_records": [_attestation(bbox=[11, 45, 390, 65])]},
    )
    assert mismatched_bounds_model["rows"] == []
    assert any(
        gap["reason"] == "pdf_ocr_visual_attestation_mismatch"
        for gap in mismatched_bounds_model["unclassified_candidate_ranges"]
    )

    duplicate_attestation_model = build_canonical_model(
        inventory,
        {"semantic_records": [_attestation(), _attestation()]},
    )
    assert duplicate_attestation_model["rows"] == []
    assert any(
        gap["reason"] == "duplicate_pdf_ocr_visual_attestation"
        for gap in duplicate_attestation_model["unclassified_candidate_ranges"]
    )


def test_nested_zip_ocr_candidate_privacy_uses_logical_path(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = tmp_path / "scan.pdf"
    _blank_pdf(source)
    archive = tmp_path / "package.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.write(source, arcname="docs/scan.pdf")
    monkeypatch.setattr(
        pdf_extract,
        "_ocr_page",
        lambda path, *_args, **_kwargs: _fake_ocr_result(path, _candidate()),
    )

    result = inspect_input(
        archive,
        mode="full",
        purpose="internal_review",
        context=PASSPORT,
    )
    serialized = json.dumps(result, ensure_ascii=False)

    assert str(tmp_path) not in serialized
    assert "smetchik-zip-" not in serialized
    assert "smetchik-pdf-" not in serialized
    assert f"package.zip!/docs/scan.pdf:{CANDIDATE_ID}" in serialized
