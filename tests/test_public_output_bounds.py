from __future__ import annotations

import importlib
import json
from copy import deepcopy
from pathlib import Path

import pytest

from smetchik_engine import inspect_input


FULL_DIMENSIONS = [
    "arithmetic",
    "fields",
    "volume_source",
    "rate_norm",
    "indices_coefficients",
    "resources",
    "interdocument",
]


def passport() -> dict[str, object]:
    return {
        "object": "Учебный объект",
        "work_type": "construction",
        "funding_source": "budget",
        "region_or_price_zone": "67",
        "price_level_date": "2026-08-01",
        "calculation_method": "resource_index",
        "stage": "project_documentation",
        "document_set": ["LSR"],
    }


def row(index: int, *, mismatch: bool = False) -> dict[str, object]:
    return {
        "entity": "estimate_row",
        "row_id": f"row-{index:04d}",
        "code": f"R-{index:04d}",
        "name": f"Работа {index:04d}",
        "unit": "м2",
        "quantity": "2",
        "unit_price": "100",
        "declared_total": "199" if mismatch else "200",
        "calculation_basis": {
            "formula": "quantity * unit_price",
            "operand_fields": ["quantity", "unit_price"],
            "source_fields_verified_complete": True,
            "evidence": {"locator": f"ЛСР!F{index + 1}"},
        },
        "evidence": {
            "source_path": "estimate.xlsx",
            "sheet": "ЛСР",
            "cell_range": f"A{index + 1}:F{index + 1}",
            "locator": f"ЛСР!A{index + 1}:F{index + 1}",
        },
    }


def inspect_rows(
    tmp_path: Path,
    rows: list[dict[str, object]],
    *,
    attest: bool,
) -> dict[str, object]:
    source = tmp_path / "estimate.bin"
    source.write_bytes(b"bounded-public-result")
    coverage = {
        str(item["row_id"]): {
            "completed_dimensions": FULL_DIMENSIONS,
            "evidence": [
                {
                    "source_path": "estimate.xlsx",
                    "locator": f"FULL:{item['row_id']}",
                }
            ],
        }
        for item in rows
    }
    return inspect_input(
        source,
        mode="full",
        purpose="internal_review",
        context=passport(),
        _trusted_records=rows,
        _trusted_full_row_coverage=coverage if attest else None,
    )


def test_public_projection_caps_nested_row_details_and_revokes_full_claim(
    tmp_path: Path,
    monkeypatch,
) -> None:
    engine = importlib.import_module("smetchik_engine")
    monkeypatch.setattr(engine, "MAX_PUBLIC_EVIDENCE_ITEMS", 50)
    monkeypatch.setattr(engine, "MAX_PUBLIC_ROW_STATES", 100)
    monkeypatch.setattr(engine, "MAX_PUBLIC_COVERAGE_GAPS", 100)
    rows = [row(index) for index in range(1, 181)]

    result = inspect_rows(tmp_path, rows, attest=True)

    assert result["coverage"]["row_level_checked"] is False
    assert result["coverage"]["checked_records"] == 0
    assert result["coverage"]["arithmetic_checked_records"] == 0
    assert result["coverage"]["checked_records_total_before_public_truncation"] == 180
    assert result["coverage"]["arithmetic_checked_records_total_before_public_truncation"] == 180
    for check in result["checks"]:
        assert len(check["evidence"]) <= 50
        row_states = check.get("parameters", {}).get("row_states", [])
        assert len(row_states) <= 100
    marker = next(
        limitation
        for limitation in result["limitations"]
        if limitation["code"] == "public_result_truncated"
    )
    assert marker["total_counts"]["checked_records"] == 180
    assert marker["omitted_counts"]["check_row_states"] >= 80
    assert "разделить пакет" in result["recommended_action"].casefold()
    assert "детальный артефакт" not in marker["required_input"].casefold()
    assert len(json.dumps(result, ensure_ascii=False, allow_nan=False).encode("utf-8")) <= 2_000_000


def test_finding_cap_prioritizes_severity_then_stable_id_and_reports_omissions(
    tmp_path: Path,
    monkeypatch,
) -> None:
    engine = importlib.import_module("smetchik_engine")
    monkeypatch.setattr(engine, "MAX_PUBLIC_FINDINGS", 100)
    base = inspect_rows(tmp_path, [row(1, mismatch=True)], attest=False)
    template = base["findings"][0]
    findings = []
    for severity, count in (("recommendation", 60), ("material", 60), ("critical", 5)):
        for index in range(count, 0, -1):
            finding = deepcopy(template)
            finding["id"] = f"{severity}:{index:03d}"
            finding["severity"] = severity
            findings.append(finding)
    base["findings"] = findings

    result = engine._bounded_public_result(base)

    assert len(result["findings"]) == 100
    ids = [finding["id"] for finding in result["findings"]]
    assert ids[:5] == [f"critical:{index:03d}" for index in range(1, 6)]
    assert ids[5:65] == [f"material:{index:03d}" for index in range(1, 61)]
    assert ids[65:] == [f"recommendation:{index:03d}" for index in range(1, 36)]
    marker = next(
        limitation
        for limitation in result["limitations"]
        if limitation["code"] == "public_result_truncated"
    )
    assert marker["total_counts"]["findings"] == 125
    assert marker["omitted_by_severity"] == {
        "critical": 0,
        "material": 0,
        "recommendation": 25,
    }


def test_cli_json_writer_has_a_hard_byte_limit(capsys, monkeypatch) -> None:
    cli = importlib.import_module("smetchik_cli")
    monkeypatch.setattr(cli, "MAX_CLI_JSON_BYTES", 64, raising=False)

    with pytest.raises(cli.CliError, match="size limit"):
        cli._write_result({"blob": "x" * 100}, None)
    assert capsys.readouterr().out == ""
