from __future__ import annotations

import hashlib
import json
import stat
import zipfile
from pathlib import Path
from types import SimpleNamespace

from PIL import Image
from pypdf import PdfWriter
from pypdf import PdfReader
from pypdf.generic import ArrayObject, DecodedStreamObject, DictionaryObject, NameObject

import pdf_extract
from canonical_model import build_canonical_model
from pdf_extract import _ocr_page, _parse_tesseract_tsv, _render_plan, extract_pdf
import safety_limits
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


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def add_text_page(writer: PdfWriter, text: str, *, rotate: int = 0) -> None:
    page = writer.add_blank_page(width=300, height=300)
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    font_reference = writer._add_object(font)
    page[NameObject("/Resources")] = DictionaryObject(
        {NameObject("/Font"): DictionaryObject({NameObject("/F1"): font_reference})}
    )
    content = DecodedStreamObject()
    escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    content.set_data(f"BT /F1 12 Tf 40 250 Td ({escaped}) Tj ET".encode("ascii"))
    page[NameObject("/Contents")] = writer._add_object(content)
    if rotate:
        page.rotate(rotate)


def make_mixed_pdf(path: Path) -> None:
    writer = PdfWriter()
    add_text_page(writer, "Estimate text layer total 123.45")
    rotated = writer.add_blank_page(width=300, height=300)
    rotated.rotate(90)
    writer.add_blank_page(width=300, height=300)
    with path.open("wb") as stream:
        writer.write(stream)


def make_table_pdf(path: Path) -> None:
    writer = PdfWriter()
    page = writer.add_blank_page(width=300, height=300)
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    font_reference = writer._add_object(font)
    page[NameObject("/Resources")] = DictionaryObject(
        {NameObject("/Font"): DictionaryObject({NameObject("/F1"): font_reference})}
    )
    content = DecodedStreamObject()
    content.set_data(
        b"0 0 0 RG 20 260 m 280 260 l 20 220 m 280 220 l 20 180 m 280 180 l "
        b"20 180 m 20 260 l 150 180 m 150 260 l 280 180 m 280 260 l S "
        b"BT /F1 12 Tf 35 238 Td (Item) Tj 135 0 Td (Cost) Tj ET "
        b"BT /F1 12 Tf 35 198 Td (Work) Tj 135 0 Td (125.50) Tj ET"
    )
    page[NameObject("/Contents")] = writer._add_object(content)
    with path.open("wb") as stream:
        writer.write(stream)


def make_hybrid_pdf(path: Path, image_source: Path) -> None:
    Image.new("RGB", (600, 600), "white").save(image_source, "PDF", resolution=144)
    reader = PdfReader(image_source)
    writer = PdfWriter()
    writer.add_page(reader.pages[0])
    page = writer.pages[0]
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    font_reference = writer._add_object(font)
    resources = page[NameObject("/Resources")].get_object()
    resources[NameObject("/Font")] = DictionaryObject({NameObject("/F1"): font_reference})
    overlay = DecodedStreamObject()
    overlay.set_data(b"BT /F1 8 Tf 5 5 Td (stub) Tj ET")
    original_contents = page.raw_get("/Contents")
    page[NameObject("/Contents")] = ArrayObject(
        [original_contents, writer._add_object(overlay)]
    )
    with path.open("wb") as stream:
        writer.write(stream)


def make_semantic_table_pdf(path: Path) -> None:
    writer = PdfWriter()
    page = writer.add_blank_page(width=700, height=300)
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    font_reference = writer._add_object(font)
    page[NameObject("/Resources")] = DictionaryObject(
        {NameObject("/Font"): DictionaryObject({NameObject("/F1"): font_reference})}
    )
    columns = (20, 120, 270, 350, 440, 530, 680)
    grid = " ".join(f"{x} 180 m {x} 260 l" for x in columns)
    labels = (
        (30, 238, "Code"),
        (130, 238, "Name"),
        (280, 238, "Unit"),
        (360, 238, "Quantity"),
        (450, 238, "Unit Price"),
        (540, 238, "Total"),
        (30, 198, "FER-01"),
        (130, 198, "Work"),
        (280, 198, "m2"),
        (360, 198, "2"),
        (450, 198, "100"),
        (540, 198, "200"),
    )
    text = " ".join(
        f"BT /F1 10 Tf {x} {y} Td ({value}) Tj ET" for x, y, value in labels
    )
    content = DecodedStreamObject()
    content.set_data(
        (
            "0 0 0 RG 20 260 m 680 260 l 20 220 m 680 220 l "
            f"20 180 m 680 180 l {grid} S {text}"
        ).encode("ascii")
    )
    page[NameObject("/Contents")] = writer._add_object(content)
    with path.open("wb") as stream:
        writer.write(stream)


def test_pdf_inventories_text_rotation_and_unreadable_pages(tmp_path: Path) -> None:
    source = tmp_path / "estimate.pdf"
    make_mixed_pdf(source)
    before = file_hash(source)

    details, status, limitations, records = extract_pdf(source)
    pages = details["pages"]
    assert details["page_count"] == 3
    assert [page["rotation"] for page in pages] == [0, 90, 0]
    assert pages[0]["text_layer_present"] is True
    assert "Estimate text layer" in pages[0]["text"]
    assert pages[0]["evidence"]["page"] == 1
    assert pages[0]["evidence"]["source_path"] == str(source)
    assert pages[0]["evidence"]["locator"].endswith("page:1")
    assert pages[0]["words"]
    first_bbox = pages[0]["words"][0]["bbox"]
    assert len(first_bbox) == 4
    assert first_bbox[0] == 40.0
    assert first_bbox[2] > first_bbox[0]
    assert pages[0]["words"][0]["evidence"]["source_path"] == str(source)
    assert pages[0]["records"]
    assert pages[0]["record_count"] == len(pages[0]["records"])
    assert pages[1]["ocr_attempted"] is True
    assert pages[2]["ocr_attempted"] is True
    assert pages[1]["visual_verification_required"] is True
    assert details["visual_verification_required"] is True
    assert status == "partial"
    assert records == sum(page["record_count"] for page in pages)
    assert any(
        limit["code"] in {"ocr_no_reliable_text", "ocr_failed", "ocr_unavailable"}
        for limit in limitations
    )

    result = inspect_input(source, mode="full")
    item = result["input_inventory"][0]
    public_pages = item["details"]["pages"]
    assert item["details"]["page_count"] == 3
    assert [page["rotation"] for page in public_pages] == [0, 90, 0]
    assert public_pages[0]["word_count"] > 0
    assert "text" not in public_pages[0]
    assert "words" not in public_pages[0]
    assert "Estimate text layer" not in json.dumps(result, ensure_ascii=False)
    assert any(
        limit["code"] in {"ocr_no_reliable_text", "ocr_failed", "ocr_unavailable"}
        for limit in result["limitations"]
    )
    assert result["execution_status"] in {"completed_with_limits", "needs_input"}
    assert sum(page["record_count"] for page in public_pages) != item["details"]["page_count"]
    assert file_hash(source) == before


def test_missing_pdf_renderer_is_a_limitation_not_a_crash(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = tmp_path / "image-only.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    with source.open("wb") as stream:
        writer.write(stream)

    monkeypatch.setenv("PATH", str(tmp_path / "empty-bin"))

    result = inspect_input(source, mode="full")

    page = result["input_inventory"][0]["details"]["pages"][0]
    assert page["ocr_attempted"] is False
    assert page["extraction_status"] == "unreadable"
    assert page["visual_verification_required"] is True
    assert any(limit["code"] == "ocr_unavailable" for limit in result["limitations"])
    assert result["execution_status"] in {"completed_with_limits", "needs_input"}


def test_pdf_extracts_words_tables_rows_and_cells_with_bboxes(tmp_path: Path) -> None:
    source = tmp_path / "table.pdf"
    make_table_pdf(source)

    details, status, limitations, records = extract_pdf(source)

    page = details["pages"][0]
    assert {word["text"] for word in page["words"]} >= {"Item", "Cost", "Work", "125.50"}
    assert page["tables"]
    table = page["tables"][0]
    assert len(table["bbox"]) == 4
    assert table["evidence"]["source_path"] == str(source)
    assert table["evidence"]["page"] == 1
    assert table["rows"][0]["cells"][0]["text"] == "Item"
    assert len(table["rows"][0]["cells"][0]["bbox"]) == 4
    assert table["rows"][1]["cells"][1]["text"] == "125.50"
    assert ":table:1:row:2:cell:2" in table["rows"][1]["cells"][1]["evidence"]["locator"]
    assert page["record_count"] == 2
    assert records == 2
    assert status == "reliable"
    assert limitations == []


def test_tesseract_tsv_preserves_reliable_boxes_and_reports_low_confidence() -> None:
    tsv = "\n".join(
        [
            "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext",
            "5\t1\t1\t1\t1\t1\t10\t20\t30\t12\t95.5\tReliable",
            "5\t1\t1\t1\t1\t2\t45\t20\t20\t12\t42.0\tDoubtful",
        ]
    )

    parsed = _parse_tesseract_tsv(
        tsv,
        source_path="scan.pdf",
        page_number=1,
        rotation=90,
    )

    assert parsed["text"] == "Reliable"
    assert parsed["mean_confidence"] == 95.5
    assert parsed["reliable_words"][0]["bbox"] == [10, 20, 40, 32]
    assert parsed["reliable_words"][0]["rotation"] == 90
    assert parsed["reliable_words"][0]["evidence"]["source_path"] == "scan.pdf"
    assert parsed["low_confidence_words"][0]["text"] == "Doubtful"
    assert parsed["low_confidence_words"][0]["confidence"] == 42.0
    assert parsed["reliable_record_count"] == 1


def test_tesseract_high_confidence_word_with_invalid_bbox_is_not_reliable() -> None:
    tsv = "\n".join(
        [
            "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext",
            "5\t1\t1\t1\t1\t1\t90\t20\t30\t12\t99.0\tOutside",
        ]
    )

    parsed = _parse_tesseract_tsv(
        tsv,
        source_path="scan.pdf",
        page_number=1,
        rotation=0,
        image_width=100,
        image_height=100,
    )

    assert parsed["reliable_words"] == []
    assert parsed["low_confidence_words"][0]["reliability_reason"] == "invalid_bbox"
    assert parsed["reliable_record_count"] == 0


def test_pdf_file_and_page_limits_reject_before_bulk_extraction(tmp_path: Path) -> None:
    source = tmp_path / "many-pages.pdf"
    make_mixed_pdf(source)

    details, status, limitations, records = extract_pdf(
        source,
        limits=safety_limits.PdfLimits(max_file_bytes=1),
    )
    assert status == "rejected"
    assert details["pages"] == []
    assert records == 0
    assert limitations[0]["code"] == "pdf_limit_exceeded"

    details, status, limitations, records = extract_pdf(
        source,
        limits=safety_limits.PdfLimits(max_pages=2),
    )
    assert status == "rejected"
    assert details["page_count"] == 3
    assert details["pages"] == []
    assert records == 0
    assert limitations[0]["code"] == "pdf_limit_exceeded"


def test_pdf_text_limit_stops_with_explicit_partial_status(tmp_path: Path) -> None:
    source = tmp_path / "text.pdf"
    writer = PdfWriter()
    add_text_page(writer, "Estimate text layer total 123.45")
    with source.open("wb") as stream:
        writer.write(stream)

    details, status, limitations, records = extract_pdf(
        source,
        limits=safety_limits.PdfLimits(max_total_text_characters=5),
    )

    assert status == "partial"
    assert records == 0
    assert details["pages"][0]["extraction_status"] == "limited"
    assert details["pages"][0]["text"] == ""
    assert any(limit["code"] == "pdf_limit_exceeded" for limit in limitations)


def test_hybrid_page_with_sparse_text_and_large_image_requires_ocr(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = tmp_path / "hybrid.pdf"
    make_hybrid_pdf(source, tmp_path / "image-source.pdf")
    calls: list[tuple[int, int]] = []

    def fake_ocr_page(
        path: Path,
        page_number: int,
        rotation: int,
        limits: safety_limits.PdfLimits,
        **kwargs,
    ):
        calls.append((page_number, rotation))
        return (
            {
                "ocr_attempted": True,
                "ocr_text": "Item Quantity Price",
                "ocr_mean_confidence": 98.0,
                "ocr_words": [],
                "ocr_low_confidence_words": [],
                "records": [],
                "record_count": 0,
                "row_level_semantics": False,
                "visual_verification_required": True,
                "extraction_status": "partial",
                "render_pixel_count": 10_000,
            },
            [
                {
                    "code": "pdf_visual_verification_required",
                    "message": "visual check",
                    "evidence": [
                        {
                            "source_path": str(path),
                            "page": page_number,
                            "locator": f"{path}:page:{page_number}",
                        }
                    ],
                }
            ],
        )

    monkeypatch.setattr(pdf_extract, "_ocr_page", fake_ocr_page)

    details, status, limitations, _records = extract_pdf(source)

    page = details["pages"][0]
    assert calls == [(1, 0)]
    assert page["text_layer_present"] is True
    assert page["ocr_attempted"] is True
    assert page["visual_verification_required"] is True
    assert page["extraction_status"] == "partial"
    assert page["row_level_semantics"] is False
    assert status == "partial"
    assert any(limit["code"] == "pdf_hybrid_page_requires_ocr" for limit in limitations)


def test_ocr_words_remain_partial_and_do_not_become_row_records(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = tmp_path / "scan.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=300, height=300)
    with source.open("wb") as stream:
        writer.write(stream)
    tsv = "\n".join(
        [
            "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext",
            "5\t1\t1\t1\t1\t1\t10\t20\t30\t12\t96.0\tQuantity",
            "5\t1\t1\t1\t1\t2\t45\t20\t20\t12\t41.0\tDoubtful",
        ]
    )
    commands: list[list[str]] = []

    monkeypatch.setattr(pdf_extract.shutil, "which", lambda name: f"/usr/bin/{name}")

    def fake_run(command, **_kwargs):
        commands.append(command)
        if command[0].endswith("pdftoppm"):
            temporary = Path(command[-1]).parent
            assert stat.S_IMODE(temporary.stat().st_mode) == 0o700
            Image.new("RGB", (100, 100), "white").save(
                Path(command[-1]).with_suffix(".png")
            )
            return SimpleNamespace(returncode=0, stdout="")
        return SimpleNamespace(returncode=0, stdout=tsv)

    monkeypatch.setattr(pdf_extract.subprocess, "run", fake_run)

    result, limitations = _ocr_page(
        source,
        1,
        90,
        safety_limits.PDF_LIMITS,
        page_width_points=300,
        page_height_points=300,
        remaining_render_pixels=safety_limits.PDF_LIMITS.max_total_render_pixels,
    )

    assert result["ocr_attempted"] is True
    assert result["extraction_status"] == "partial"
    assert result["visual_verification_required"] is True
    assert result["row_level_semantics"] is False
    assert result["records"] == []
    assert result["record_count"] == 0
    assert result["ocr_words"][0]["rotation"] == 90
    assert result["ocr_low_confidence_words"][0]["text"] == "Doubtful"
    assert any(limit["code"] == "ocr_low_confidence_words" for limit in limitations)
    assert any(command[0].endswith("tesseract") and "rus+eng" in command for command in commands)


def test_table_extraction_error_marks_page_limited(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = tmp_path / "text.pdf"
    writer = PdfWriter()
    add_text_page(writer, "Estimate quantity price total")
    with source.open("wb") as stream:
        writer.write(stream)

    def fail_tables(_page):
        raise RuntimeError("table parser failed")

    monkeypatch.setattr(pdf_extract.pdfplumber.page.Page, "find_tables", fail_tables)

    details, status, limitations, _records = extract_pdf(source)

    page = details["pages"][0]
    assert status == "partial"
    assert page["extraction_status"] == "limited"
    assert page["visual_verification_required"] is True
    assert any(limit["code"] == "pdf_table_extraction_failed" for limit in limitations)


def test_huge_media_box_is_rejected_before_pdftoppm(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = tmp_path / "huge-media-box.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=1_000_000, height=1_000_000)
    with source.open("wb") as stream:
        writer.write(stream)

    monkeypatch.setattr(pdf_extract.shutil, "which", lambda name: f"/usr/bin/{name}")

    def must_not_run(*_args, **_kwargs):
        raise AssertionError("renderer must not start for an unsafe MediaBox")

    monkeypatch.setattr(pdf_extract.subprocess, "run", must_not_run)

    details, status, limitations, records = extract_pdf(source)

    page = details["pages"][0]
    assert status == "partial"
    assert records == 0
    assert page["ocr_attempted"] is False
    assert page["extraction_status"] == "limited"
    assert page["visual_verification_required"] is True
    assert any(limit["code"] == "pdf_render_limit_exceeded" for limit in limitations)


def test_render_plan_reduces_dpi_within_dimension_and_pixel_caps() -> None:
    limits = safety_limits.PdfLimits(
        min_ocr_dpi=20,
        max_render_dimension_pixels=1_000,
        max_render_pixels_per_page=1_000_000,
        max_total_render_pixels=1_000_000,
    )

    plan = _render_plan(
        page_width_points=500,
        page_height_points=500,
        rotation=90,
        remaining_render_pixels=limits.max_total_render_pixels,
        limits=limits,
    )

    assert plan is not None
    assert plan["resolution_reduced"] is True
    assert limits.min_ocr_dpi <= plan["dpi"] < limits.default_ocr_dpi
    assert plan["width_pixels"] <= limits.max_render_dimension_pixels
    assert plan["height_pixels"] <= limits.max_render_dimension_pixels
    assert plan["pixel_count"] <= limits.max_render_pixels_per_page


def test_only_geometric_table_with_explicit_headers_reaches_canonical_rows(
    tmp_path: Path,
) -> None:
    source = tmp_path / "semantic-table.pdf"
    make_semantic_table_pdf(source)

    result = inspect_input(
        source,
        mode="full",
        purpose="internal_review",
        context=PASSPORT,
    )

    assert result["coverage"]["checkable_records"] == 1
    assert result["coverage"]["arithmetic_checked_records"] == 0
    arithmetic = next(check for check in result["checks"] if check["id"] == "ARITH-01")
    assert arithmetic["status"] == "limited"
    assert arithmetic["parameters"]["row_states"][0]["status"] == "limited"
    assert arithmetic["parameters"]["row_states"][0]["reason"] == (
        "calculation_basis_not_verified"
    )
    assert arithmetic["parameters"]["row_states"][0]["row_id"].endswith(
        ":page:1:table:1:row:2"
    )
    assert not any(finding["id"].startswith("ARITH-01") for finding in result["findings"])
    evidence = result["coverage"]["full_control_gaps"][0]["evidence"]
    assert evidence["source_path"] == source.name
    assert evidence["page"] == 1
    assert ":table:1:row:2" in evidence["locator"]


def test_ocr_text_never_becomes_estimate_rows_and_public_result_has_no_temp_path(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = tmp_path / "scan.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=300, height=300)
    with source.open("wb") as stream:
        writer.write(stream)

    def fake_ocr_page(path: Path, page_number: int, _rotation: int, _limits, **_kwargs):
        evidence = {
            "source_path": str(path),
            "page": page_number,
            "locator": f"{path}:page:{page_number}:ocr-word:1",
            "bbox": [10, 10, 60, 20],
        }
        return (
            {
                "ocr_attempted": True,
                "ocr_text": "Code Quantity Unit Price Total FER-01 2 m2 100 200",
                "ocr_mean_confidence": 99.0,
                "ocr_words": [
                    {
                        "text": "Quantity",
                        "confidence": 99.0,
                        "bbox": [10, 10, 60, 20],
                        "coordinate_space": "rendered_image_pixels",
                        "rotation": 0,
                        "line_id": [1, 1, 1],
                        "evidence": evidence,
                    }
                ],
                "ocr_low_confidence_words": [],
                "records": [],
                "record_count": 0,
                "row_level_semantics": False,
                "render_dpi": 200,
                "render_pixel_count": 100_000,
                "visual_verification_required": True,
                "extraction_status": "partial",
            },
            [
                {
                    "code": "pdf_visual_verification_required",
                    "message": "visual check",
                    "evidence": [evidence],
                }
            ],
        )

    monkeypatch.setattr(pdf_extract, "_ocr_page", fake_ocr_page)

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
    assert str(tmp_path) not in serialized
    assert "smetchik-pdf-" not in serialized
    assert source.name in serialized
    assert ":page:1:ocr-word:1" in serialized


def test_nested_zip_pdf_rebases_evidence_without_zip_or_pdf_temp_paths(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = tmp_path / "scan.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=300, height=300)
    with source.open("wb") as stream:
        writer.write(stream)
    archive = tmp_path / "package.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.write(source, arcname="docs/scan.pdf")

    def no_ocr_tools(_name: str):
        return None

    monkeypatch.setattr(pdf_extract.shutil, "which", no_ocr_tools)

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
    assert "package.zip!/docs/scan.pdf" in serialized
    assert ":page:1" in serialized


def test_document_ocr_page_budget_stops_additional_renderers(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = tmp_path / "two-scans.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=300, height=300)
    writer.add_blank_page(width=300, height=300)
    with source.open("wb") as stream:
        writer.write(stream)
    calls: list[int] = []

    def fake_ocr_page(_path: Path, page_number: int, _rotation: int, _limits, **_kwargs):
        calls.append(page_number)
        return (
            {
                **pdf_extract._empty_ocr_result(attempted=True, status="unreadable"),
                "render_pixel_count": 100,
            },
            [],
        )

    monkeypatch.setattr(pdf_extract, "_ocr_page", fake_ocr_page)

    details, status, limitations, records = extract_pdf(
        source,
        limits=safety_limits.PdfLimits(max_ocr_pages=1),
    )

    assert calls == [1]
    assert status == "partial"
    assert records == 0
    assert details["pages"][1]["ocr_attempted"] is False
    assert details["pages"][1]["extraction_status"] == "limited"
    assert any(limit["code"] == "pdf_ocr_page_limit_exceeded" for limit in limitations)


def test_pdf_table_without_header_geometry_is_not_a_canonical_estimate_row() -> None:
    cells = [
        {"text": "Code", "bbox": None},
        {"text": "Quantity", "bbox": None},
        {"text": "Unit Price", "bbox": None},
        {"text": "Total", "bbox": None},
    ]
    inventory = [
        {
            "path": "estimate.pdf",
            "file_type": "pdf",
            "details": {
                "format": "pdf",
                "pages": [
                    {
                        "page": 1,
                        "extraction_status": "reliable",
                        "tables": [
                            {
                                "table_index": 1,
                                "bbox": [0, 0, 100, 100],
                                "rows": [
                                    {"row_index": 1, "bbox": [0, 0, 100, 20], "cells": cells},
                                    {
                                        "row_index": 2,
                                        "bbox": [0, 20, 100, 40],
                                        "cells": [
                                            {"text": "FER-01", "bbox": [0, 20, 25, 40]},
                                            {"text": "2", "bbox": [25, 20, 50, 40]},
                                            {"text": "100", "bbox": [50, 20, 75, 40]},
                                            {"text": "200", "bbox": [75, 20, 100, 40]},
                                        ],
                                        "evidence": {
                                            "source_path": "estimate.pdf",
                                            "page": 1,
                                            "locator": "estimate.pdf:page:1:table:1:row:2",
                                            "bbox": [0, 20, 100, 40],
                                        },
                                    },
                                ],
                            }
                        ],
                    }
                ],
            },
        }
    ]

    model = build_canonical_model(inventory, PASSPORT)

    assert model["rows"] == []
