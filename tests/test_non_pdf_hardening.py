from __future__ import annotations

from pathlib import Path

from smetchik_engine import inspect_input


MATRIX_LIGHT_IDS = {
    "PKG-01",
    "PKG-02",
    "PKG-03",
    "PKG-04",
    "QTY-01",
    "QTY-02",
    "RATE-01",
    "RATE-02",
    "RATE-03",
    "RES-01",
    "RES-02",
    "KAC-01",
    "KAC-02",
    "SUM-01",
    "SUM-02",
    "SUM-03",
    "SUM-04",
    "SUM-05",
    "SUM-06",
    "PIR-01",
    "SUR-01",
    "CAP-01",
    "OCH-01",
    "CONTRACT-01",
    "ANA-01",
    "ANA-02",
    "ANA-03",
    "ANA-04",
}


def passport(**updates: object) -> dict[str, object]:
    value: dict[str, object] = {
        "object": "Учебный объект",
        "work_type": "construction",
        "funding_source": "budget",
        "region_or_price_zone": "67",
        "price_level_date": "2026-08-01",
        "calculation_method": "resource_index",
        "stage": "project_documentation",
        "document_set": ["LSR"],
    }
    value.update(updates)
    return value


def row_evidence(cell: str = "A2:H2") -> dict[str, object]:
    return {
        "source_path": "estimate.xlsx",
        "sheet": "ЛСР",
        "cell_range": cell,
        "locator": f"ЛСР!{cell}",
    }


def inspect_rows(
    tmp_path: Path,
    rows: list[dict[str, object]],
    *,
    mode: str = "full",
    context: dict[str, object] | None = None,
) -> dict[str, object]:
    source = tmp_path / "estimate.bin"
    source.write_bytes(b"trusted-test-records")
    return inspect_input(
        source,
        mode=mode,
        purpose="internal_review",
        context=context or passport(),
        _trusted_records=rows,
    )


def test_arithmetic_without_explicit_calculation_basis_is_limited_not_a_finding(
    tmp_path: Path,
) -> None:
    result = inspect_rows(
        tmp_path,
        [
            {
                "entity": "estimate_row",
                "row_id": "already-indexed",
                "name": "Работа",
                "unit": "м2",
                "quantity": "2",
                "unit_price": "100",
                "index": "1.2",
                "declared_total": "200",
                "evidence": row_evidence(),
            }
        ],
    )

    arithmetic = next(check for check in result["checks"] if check["id"] == "ARITH-01")
    assert arithmetic["status"] == "limited"
    assert arithmetic["parameters"]["row_states"] == [
        {
            "row_id": "already-indexed",
            "status": "limited",
            "reason": "calculation_basis_not_verified",
        }
    ]
    assert result["coverage"]["arithmetic_checked_records"] == 0
    assert not any(finding["id"].startswith("ARITH-01") for finding in result["findings"])


def test_trusted_basis_uses_only_its_exact_operand_fields(tmp_path: Path) -> None:
    result = inspect_rows(
        tmp_path,
        [
            {
                "entity": "estimate_row",
                "row_id": "basis-without-index",
                "name": "Работа",
                "unit": "м2",
                "quantity": "2",
                "unit_price": "100",
                "index": "1.2",
                "declared_total": "200",
                "calculation_basis": {
                    "formula": "quantity * unit_price",
                    "operand_fields": ["quantity", "unit_price"],
                    "source_fields_verified_complete": True,
                    "evidence": {"locator": "ЛСР!F2"},
                },
                "evidence": row_evidence(),
            }
        ],
    )

    arithmetic = next(check for check in result["checks"] if check["id"] == "ARITH-01")
    assert arithmetic["status"] == "passed"
    assert result["coverage"]["arithmetic_checked_records"] == 1
    assert not any(finding["id"].startswith("ARITH-01") for finding in result["findings"])


def test_inconsistent_or_unapproved_calculation_basis_is_not_executed(tmp_path: Path) -> None:
    result = inspect_rows(
        tmp_path,
        [
            {
                "entity": "estimate_row",
                "row_id": "unapproved-basis",
                "name": "Работа",
                "unit": "м2",
                "quantity": "2",
                "unit_price": "100",
                "declared_total": "240",
                "calculation_basis": {
                    "formula": "quantity * unit_price * magic",
                    "operand_fields": ["quantity", "unit_price", "magic"],
                    "source_fields_verified_complete": True,
                    "evidence": {"locator": "ЛСР!F2"},
                },
                "evidence": row_evidence(),
            }
        ],
    )

    arithmetic = next(check for check in result["checks"] if check["id"] == "ARITH-01")
    assert arithmetic["status"] == "limited"
    assert result["coverage"]["arithmetic_checked_records"] == 0
    assert result["findings"] == []


def test_light_exposes_every_macro_control_and_cannot_finish_clean(tmp_path: Path) -> None:
    result = inspect_rows(
        tmp_path,
        [
            {
                "entity": "estimate_row",
                "row_id": "row-1",
                "name": "Работа",
                "unit": "м2",
                "quantity": "2",
                "unit_price": "100",
                "declared_total": "200",
                "evidence": row_evidence(),
            }
        ],
        mode="light",
    )

    checks = {check["id"]: check for check in result["checks"]}
    assert MATRIX_LIGHT_IDS <= checks.keys()
    assert checks["PKG-02"]["status"] == "limited"
    assert checks["RATE-01"]["status"] == "limited"
    assert checks["PIR-01"]["status"] == "not_applicable"
    assert any(
        limitation["code"] == "light_macro_controls_incomplete"
        for limitation in result["limitations"]
    )
    assert result["execution_status"] == "completed_with_limits"
    assert "Позиции не проверялись построчно" in result["coverage"]["description"]


def test_full_exposes_unimplemented_control_matrix_ids_as_explicit_limits(
    tmp_path: Path,
) -> None:
    result = inspect_rows(
        tmp_path,
        [
            {
                "entity": "estimate_row",
                "row_id": "row-1",
                "name": "Работа",
                "unit": "м2",
                "quantity": "2",
                "unit_price": "100",
                "declared_total": "200",
                "evidence": row_evidence(),
            }
        ],
        mode="full",
    )

    checks = {check["id"]: check for check in result["checks"]}
    assert MATRIX_LIGHT_IDS <= checks.keys()
    assert checks["QTY-01"]["status"] == "limited"
    assert checks["RATE-01"]["status"] == "limited"
    matrix_limit = next(
        limitation
        for limitation in result["limitations"]
        if limitation["code"] == "full_control_matrix_incomplete"
    )
    assert {"QTY-01", "RATE-01", "RES-01"} <= set(matrix_limit["control_ids"])
    assert result["execution_status"] == "completed_with_limits"
