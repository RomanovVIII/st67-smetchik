from __future__ import annotations

import importlib
from copy import deepcopy
from pathlib import Path

import pytest

from smetchik_engine import inspect_input


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


def result_with_internal_finding(tmp_path: Path) -> dict[str, object]:
    source = tmp_path / "estimate.bin"
    source.write_bytes(b"trusted-result-fixture")
    row = {
        "entity": "estimate_row",
        "row_id": "r-1",
        "name": "Работа",
        "unit": "м2",
        "quantity": "2",
        "unit_price": "100",
        "declared_total": "199",
        "calculation_basis": {
            "formula": "quantity * unit_price",
            "operand_fields": ["quantity", "unit_price"],
            "source_fields_verified_complete": True,
            "evidence": {"locator": "ЛСР!F2"},
        },
        "evidence": {
            "source_path": "estimate.xlsx",
            "sheet": "ЛСР",
            "cell_range": "A2:F2",
            "locator": "ЛСР!A2:F2",
        },
    }
    return inspect_input(
        source,
        mode="full",
        purpose="internal_review",
        context=passport(),
        _trusted_records=[row],
    )


def test_finding_has_stable_source_citation_projected_from_published_source(
    tmp_path: Path,
) -> None:
    result = result_with_internal_finding(tmp_path)

    finding = result["findings"][0]
    assert finding["source_ids"] == ["INT-01"]
    assert finding["source_citations"] == [
        {
            "source_id": "INT-01",
            "edition": "smetchik-0.1.0",
            "pinpoint": "skills/smetchik/references/control-matrix.md#правило-результата",
            "official_url": None,
        }
    ]


def test_runtime_schema_rejects_non_finite_numbers(tmp_path: Path) -> None:
    contract = importlib.import_module("result_schema")
    result = result_with_internal_finding(tmp_path)
    result["findings"][0]["evidence"][0]["bbox"] = [float("nan"), 0, 1, 1]

    with pytest.raises(contract.ResultSchemaError, match="finite|number"):
        contract.validate_result_schema(result)


def test_runtime_schema_rejects_malformed_official_url(tmp_path: Path) -> None:
    contract = importlib.import_module("result_schema")
    result = result_with_internal_finding(tmp_path)
    result["normative_sources"][0]["official_url"] = "not-a-uri"
    result["findings"][0]["source_citations"][0]["official_url"] = "not-a-uri"

    with pytest.raises(contract.ResultSchemaError, match="official_url|uri"):
        contract.validate_result_schema(result)


def test_runtime_schema_rejects_missing_or_mismatched_source_citation(
    tmp_path: Path,
) -> None:
    contract = importlib.import_module("result_schema")
    result = result_with_internal_finding(tmp_path)

    missing = deepcopy(result)
    missing["findings"][0].pop("source_citations")
    with pytest.raises(contract.ResultSchemaError, match="source_citations"):
        contract.validate_result_schema(missing)

    mismatched = deepcopy(result)
    mismatched["findings"][0]["source_citations"][0]["pinpoint"] = "invented-point"
    with pytest.raises(contract.ResultSchemaError, match="citation|pinpoint|mismatch"):
        contract.validate_result_schema(mismatched)


def test_external_verified_source_requires_edition_pinpoint_and_http_url(
    tmp_path: Path,
) -> None:
    contract = importlib.import_module("result_schema")
    result = result_with_internal_finding(tmp_path)
    result["normative_sources"].append(
        {
            "id": "OFF-01",
            "class": "method",
            "title": "Методика",
            "normativity": "normative",
            "official_url": "https://example.test/official",
            "checked_at": "2026-08-26",
            "applicability": "test",
            "edition": "редакция 2026-08-26",
            "pinpoint": "п. 1",
            "locator": None,
        }
    )
    contract.validate_result_schema(result)

    for field in ("edition", "pinpoint"):
        broken = deepcopy(result)
        broken["normative_sources"][1][field] = ""
        with pytest.raises(contract.ResultSchemaError, match=field):
            contract.validate_result_schema(broken)

    broken_url = deepcopy(result)
    broken_url["normative_sources"][1]["official_url"] = "file:///tmp/source"
    with pytest.raises(contract.ResultSchemaError, match="official_url"):
        contract.validate_result_schema(broken_url)


def test_cli_result_writer_refuses_non_finite_json(capsys) -> None:
    cli = importlib.import_module("smetchik_cli")

    with pytest.raises(cli.CliError, match="finite|JSON"):
        cli._write_result({"value": float("inf")}, None)
    assert capsys.readouterr().out == ""

