from __future__ import annotations

from pathlib import Path

import pytest

from canonical_model import build_canonical_model
from domain_checks import run_domain_checks
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


def passport(**updates: object) -> dict[str, object]:
    value: dict[str, object] = {
        "object": "Учебный объект",
        "work_type": "construction",
        "funding_source": "budget",
        "region_or_price_zone": "67",
        "price_level_date": "2026-08-01",
        "calculation_method": "resource_index",
        "stage": "project_documentation",
        "document_set": ["LSR", "OSR", "SSR"],
    }
    value.update(updates)
    return value


def semantic_source(tmp_path: Path, records: list[dict[str, object]]) -> tuple[Path, dict[str, object]]:
    source = tmp_path / "estimate.bin"
    source.write_bytes(b"local-domain-fixture")
    context = passport(semantic_records=records)
    return source, context


def inspect_semantic(
    source: Path,
    context: dict[str, object],
    *,
    mode: str,
    purpose: str,
    trusted_domain_context: dict[str, object] | None = None,
    full_row_ids: tuple[str, ...] = (),
) -> dict[str, object]:
    records = context.get("semantic_records", [])
    public_context = {key: value for key, value in context.items() if key != "semantic_records"}
    return inspect_input(
        source,
        mode=mode,
        purpose=purpose,
        context=public_context,
        _trusted_records=records if isinstance(records, list) else None,
        _trusted_domain_context=trusted_domain_context,
        _trusted_full_row_coverage={
            row_id: {
                "completed_dimensions": FULL_DIMENSIONS,
                "evidence": [evidence(f"FULL:{row_id}")],
            }
            for row_id in full_row_ids
        },
    )


def run_domain_records(
    records: list[dict[str, object]],
    *,
    context: dict[str, object] | None = None,
    trusted_context_fields: set[str] | None = None,
) -> dict[str, object]:
    model = build_canonical_model([], {"semantic_records": records})
    effective_context = passport()
    effective_context.update(context or {})
    return run_domain_checks(
        model,
        inventory=[{"path": "estimate.xlsx", "file_type": "xlsx"}],
        mode="full",
        purpose="internal_review",
        context=effective_context,
        extraction_limitations=[],
        trusted_context_fields=trusted_context_fields,
    )


def evidence(cell: str) -> dict[str, object]:
    return {
        "source_path": "estimate.xlsx",
        "sheet": "ЛСР-01",
        "cell_range": cell,
        "locator": f"ЛСР-01!{cell}",
    }


def calculation_basis(*operands: str, cell: str) -> dict[str, object]:
    return {
        "formula": " * ".join(operands),
        "operand_fields": list(operands),
        "source_fields_verified_complete": True,
        "evidence": {"locator": f"ЛСР-01!{cell}"},
    }


def test_full_mode_checks_every_reliable_row_with_exact_decimal_arithmetic(tmp_path: Path) -> None:
    source, context = semantic_source(
        tmp_path,
        [
            {
                "entity": "estimate_row",
                "row_id": "r-1",
                "estimate_id": "lsr-1",
                "code": "ГЭСН-01",
                "name": "Работа",
                "quantity": "2",
                "unit": "м2",
                "unit_price": "100",
                "index": "1.10",
                "coefficient": "1",
                "declared_total": "219.99",
                "category": "construction_work",
                "calculation_basis": calculation_basis(
                    "quantity",
                    "unit_price",
                    "index",
                    "coefficient",
                    cell="H2",
                ),
                "evidence": evidence("A2:H2"),
            },
            {
                "entity": "estimate_row",
                "row_id": "r-2",
                "estimate_id": "lsr-1",
                "code": "ГЭСН-02",
                "name": "Работа 2",
                "quantity": "1",
                "unit": "шт",
                "unit_price": "80",
                "declared_total": "80",
                "category": "materials",
                "calculation_basis": calculation_basis(
                    "quantity", "unit_price", cell="H3"
                ),
                "evidence": evidence("A3:H3"),
            },
        ],
    )

    result = inspect_semantic(
        source,
        context,
        mode="full",
        purpose="internal_review",
        full_row_ids=("r-1", "r-2"),
    )

    assert result["coverage"]["extracted_records"] == 2
    assert result["coverage"]["checkable_records"] == 2
    assert result["coverage"]["checked_records"] == 2
    assert result["coverage"]["row_level_checked"] is True
    arithmetic = next(f for f in result["findings"] if f["id"].startswith("ARITH-01"))
    assert arithmetic["calculation"] == {
        "observed": "219.99",
        "expected": "220.00",
        "formula": "quantity * unit_price * index * coefficient",
        "difference": "-0.01",
        "unit": None,
    }
    assert arithmetic["source_ids"] == ["INT-01"]
    assert arithmetic["limitation"] is False
    assert arithmetic["question"] is False


def test_light_mode_checks_whole_package_only_at_macro_level(tmp_path: Path) -> None:
    source, context = semantic_source(
        tmp_path,
        [
            {
                "entity": "estimate_row",
                "row_id": "r-1",
                "estimate_id": "lsr-1",
                "quantity": "2",
                "unit": "м2",
                "unit_price": "100",
                "declared_total": "1",
                "evidence": evidence("A2:F2"),
            }
        ],
    )

    result = inspect_semantic(source, context, mode="light", purpose="internal_review")

    assert result["coverage"]["sampling_strategy"] == "none"
    assert result["coverage"]["row_level_checked"] is False
    assert result["coverage"]["checkable_records"] == 1
    assert result["coverage"]["checked_records"] == 0
    assert not any(finding["id"].startswith("ARITH-01") for finding in result["findings"])
    assert next(check for check in result["checks"] if check["id"] == "ARITH-01")["status"] == "limited"


def test_full_mode_does_not_claim_row_coverage_when_reliable_row_needs_input(tmp_path: Path) -> None:
    source, context = semantic_source(
        tmp_path,
        [
            {
                "entity": "estimate_row",
                "row_id": "r-missing-price",
                "estimate_id": "lsr-1",
                "name": "Неполная строка",
                "quantity": "2",
                "unit": "м2",
                "unit_price": None,
                "declared_total": "20",
                "evidence": evidence("A2:F2"),
            }
        ],
    )

    result = inspect_semantic(source, context, mode="full", purpose="internal_review")

    assert result["coverage"]["checkable_records"] == 1
    assert result["coverage"]["checked_records"] == 0
    assert result["coverage"]["row_level_checked"] is False
    row_state = next(check for check in result["checks"] if check["id"] == "ARITH-01")["parameters"]["row_states"][0]
    assert row_state == {"row_id": "r-missing-price", "status": "needs_input"}


def test_private_full_marker_cannot_override_a_reliable_row_needing_input(tmp_path: Path) -> None:
    source, context = semantic_source(
        tmp_path,
        [
            {
                "entity": "estimate_row",
                "row_id": "r-missing-unit",
                "name": "Работа",
                "quantity": "2",
                "unit": None,
                "unit_price": "10",
                "declared_total": "20",
                "calculation_basis": calculation_basis(
                    "quantity", "unit_price", cell="F2"
                ),
                "evidence": evidence("A2:F2"),
            }
        ],
    )

    result = inspect_semantic(
        source,
        context,
        mode="full",
        purpose="internal_review",
        full_row_ids=("r-missing-unit",),
    )

    assert result["coverage"]["arithmetic_checked_records"] == 1
    assert result["coverage"]["checked_records"] == 0
    assert result["coverage"]["row_level_checked"] is False
    full_state = next(
        check for check in result["checks"] if check["id"] == "FULL-ROW-01"
    )["parameters"]["row_states"][0]
    assert full_state == {"row_id": "r-missing-unit", "status": "needs_input"}


def test_hierarchy_totals_are_reconciled_lsr_osr_ssr(tmp_path: Path) -> None:
    source, context = semantic_source(
        tmp_path,
        [
            {"entity": "estimate", "estimate_id": "ssr", "estimate_type": "SSR", "declared_total": "205", "evidence": evidence("H1")},
            {"entity": "estimate", "estimate_id": "osr", "estimate_type": "OSR", "parent_id": "ssr", "declared_total": "200", "evidence": evidence("H2")},
            {"entity": "estimate", "estimate_id": "lsr-1", "estimate_type": "LSR", "parent_id": "osr", "declared_total": "100", "evidence": evidence("H3")},
            {"entity": "estimate", "estimate_id": "lsr-2", "estimate_type": "LSR", "parent_id": "osr", "declared_total": "110", "evidence": evidence("H4")},
        ],
    )

    result = inspect_semantic(source, context, mode="full", purpose="internal_review")

    hierarchy = [f for f in result["findings"] if f["id"].startswith("HIER-01")]
    assert len(hierarchy) == 2
    osr = next(f for f in hierarchy if f["evidence"][0]["cell_range"] == "H2")
    assert osr["calculation"]["observed"] == "200"
    assert osr["calculation"]["expected"] == "210"
    assert osr["calculation"]["difference"] == "-10"
    assert {item["cell_range"] for item in osr["evidence"]} == {"H2", "H3", "H4"}

    check = next(check for check in result["checks"] if check["id"] == "HIER-01")
    relation_states = check["parameters"]["relation_states"]
    aggregate_states = check["parameters"]["aggregate_states"]
    assert len(relation_states) == 3
    assert all(state["status"] == "passed" for state in relation_states)
    assert all(len(state["evidence"]) == 2 for state in relation_states)
    assert {state["parent_id"] for state in aggregate_states} == {"osr", "ssr"}
    assert all(state["status"] == "finding" for state in aggregate_states)
    assert next(check for check in result["checks"] if check["id"] == "PKG-03")["status"] == "finding"


def test_hierarchy_cycle_marks_every_cycle_relation_and_cites_all_nodes(tmp_path: Path) -> None:
    source, context = semantic_source(
        tmp_path,
        [
            {
                "entity": "estimate",
                "estimate_id": "lsr",
                "estimate_type": "LSR",
                "parent_id": "osr",
                "declared_total": "100",
                "evidence": evidence("H1"),
            },
            {
                "entity": "estimate",
                "estimate_id": "osr",
                "estimate_type": "OSR",
                "parent_id": "lsr",
                "declared_total": "100",
                "evidence": evidence("H2"),
            },
        ],
    )

    result = inspect_semantic(source, context, mode="full", purpose="internal_review")

    check = next(check for check in result["checks"] if check["id"] == "HIER-01")
    cycle_states = [
        state
        for state in check["parameters"]["relation_states"]
        if state["reason"] == "hierarchy_cycle"
    ]
    assert check["status"] == "finding"
    assert {state["child_id"] for state in cycle_states} == {"lsr", "osr"}
    assert all(state["status"] == "finding" for state in cycle_states)
    cycle = next(finding for finding in result["findings"] if ":cycle:" in finding["id"])
    assert {item["cell_range"] for item in cycle["evidence"]} == {"H1", "H2"}


def test_hierarchy_missing_total_is_needs_input_not_silent_pass(tmp_path: Path) -> None:
    source, context = semantic_source(
        tmp_path,
        [
            {
                "entity": "estimate",
                "estimate_id": "osr",
                "estimate_type": "OSR",
                "declared_total": "100",
                "evidence": evidence("H1"),
            },
            {
                "entity": "estimate",
                "estimate_id": "lsr",
                "estimate_type": "LSR",
                "parent_id": "osr",
                "declared_total": None,
                "evidence": evidence("H2"),
            },
        ],
    )

    result = inspect_semantic(source, context, mode="full", purpose="internal_review")

    hierarchy = next(check for check in result["checks"] if check["id"] == "HIER-01")
    aggregate = hierarchy["parameters"]["aggregate_states"][0]
    assert hierarchy["status"] == "needs_input"
    assert aggregate["status"] == "needs_input"
    assert {item["cell_range"] for item in aggregate["evidence"]} == {"H1", "H2"}
    assert next(check for check in result["checks"] if check["id"] == "PKG-03")["status"] == "needs_input"
    assert any(
        limitation["code"] == "hierarchy_totals_not_available"
        for limitation in result["limitations"]
    )


@pytest.mark.parametrize("trust_gap", ["partial", "imprecise_evidence"])
def test_hierarchy_cannot_pass_unreliable_or_imprecisely_located_relations(
    tmp_path: Path,
    trust_gap: str,
) -> None:
    precise_parent = evidence("H1")
    precise_child = evidence("H2")
    file_only = {"source_path": "estimate.xlsx", "locator": "estimate.xlsx"}
    reliability = "partial" if trust_gap == "partial" else "reliable"
    parent_evidence = precise_parent if trust_gap == "partial" else file_only
    child_evidence = precise_child if trust_gap == "partial" else file_only
    source, context = semantic_source(
        tmp_path,
        [
            {
                "entity": "estimate",
                "estimate_id": "osr",
                "estimate_type": "OSR",
                "declared_total": "100",
                "reliability": reliability,
                "evidence": parent_evidence,
            },
            {
                "entity": "estimate",
                "estimate_id": "lsr",
                "estimate_type": "LSR",
                "parent_id": "osr",
                "declared_total": "100",
                "reliability": reliability,
                "evidence": child_evidence,
            },
        ],
    )

    result = inspect_semantic(source, context, mode="full", purpose="internal_review")

    hierarchy = next(check for check in result["checks"] if check["id"] == "HIER-01")
    assert hierarchy["status"] == "limited"
    assert hierarchy["parameters"]["relation_states"][0]["status"] == "limited"
    assert hierarchy["parameters"]["aggregate_states"][0]["status"] == "limited"
    assert next(check for check in result["checks"] if check["id"] == "PKG-03")["status"] == "limited"
    assert not any(finding["id"].startswith("HIER-01") for finding in result["findings"])


def test_ambiguous_parent_relation_cites_every_available_candidate(tmp_path: Path) -> None:
    source, context = semantic_source(
        tmp_path,
        [
            {
                "entity": "estimate",
                "estimate_id": "osr",
                "estimate_type": "OSR",
                "declared_total": "50",
                "evidence": evidence("H1"),
            },
            {
                "entity": "estimate",
                "estimate_id": "osr",
                "estimate_type": "OSR",
                "declared_total": "50",
                "evidence": evidence("H2"),
            },
            {
                "entity": "estimate",
                "estimate_id": "lsr",
                "estimate_type": "LSR",
                "parent_id": "osr",
                "declared_total": "100",
                "evidence": evidence("H3"),
            },
        ],
    )

    result = inspect_semantic(source, context, mode="full", purpose="internal_review")

    relation = next(check for check in result["checks"] if check["id"] == "HIER-01")[
        "parameters"
    ]["relation_states"][0]
    assert relation["status"] == "needs_input"
    assert relation["reason"] == "parent_id_not_unique"
    assert {item["cell_range"] for item in relation["evidence"]} == {"H1", "H2", "H3"}


def test_direct_lsr_to_ssr_is_not_self_attested_or_false_passed(tmp_path: Path) -> None:
    source, context = semantic_source(
        tmp_path,
        [
            {
                "entity": "estimate",
                "estimate_id": "ssr",
                "estimate_type": "SSR",
                "declared_total": "100",
                "evidence": evidence("H1"),
            },
            {
                "entity": "estimate",
                "estimate_id": "lsr",
                "estimate_type": "LSR",
                "parent_id": "ssr",
                "declared_total": "100",
                "evidence": evidence("H2"),
            },
        ],
    )
    context["hierarchy_relation_attestation"] = {
        "direct_lsr_to_ssr_allowed": True,
        "source_fields_verified_complete": True,
        "evidence": evidence("H30"),
    }

    result = inspect_semantic(source, context, mode="full", purpose="internal_review")

    hierarchy = next(check for check in result["checks"] if check["id"] == "HIER-01")
    relation = hierarchy["parameters"]["relation_states"][0]
    assert hierarchy["status"] == "needs_input"
    assert relation["status"] == "needs_input"
    assert relation["reason"] == "direct_lsr_to_ssr_route_not_attested"
    assert not any(finding["id"].startswith("HIER-01") for finding in result["findings"])


@pytest.mark.parametrize(
    ("allowed", "expected_status"),
    [(True, "passed"), (False, "finding")],
)
def test_direct_lsr_to_ssr_uses_only_source_bound_private_attestation(
    tmp_path: Path,
    allowed: bool,
    expected_status: str,
) -> None:
    source, context = semantic_source(
        tmp_path,
        [
            {
                "entity": "estimate",
                "estimate_id": "ssr",
                "estimate_type": "SSR",
                "declared_total": "100",
                "evidence": evidence("H1"),
            },
            {
                "entity": "estimate",
                "estimate_id": "lsr",
                "estimate_type": "LSR",
                "parent_id": "ssr",
                "declared_total": "100",
                "hierarchy_relation_attestation": {
                    "direct_lsr_to_ssr_allowed": allowed,
                    "source_fields_verified_complete": True,
                    "evidence": evidence("H30"),
                },
                "evidence": evidence("H2"),
            },
        ],
    )

    result = inspect_semantic(source, context, mode="full", purpose="internal_review")

    hierarchy = next(check for check in result["checks"] if check["id"] == "HIER-01")
    relation = hierarchy["parameters"]["relation_states"][0]
    assert hierarchy["status"] == expected_status
    assert relation["status"] == expected_status
    assert relation["reason"] == (
        "trusted_direct_lsr_to_ssr_allowed"
        if allowed
        else "trusted_direct_lsr_to_ssr_forbidden"
    )
    if allowed:
        assert not any(finding["id"].startswith("HIER-01") for finding in result["findings"])
    else:
        finding = next(finding for finding in result["findings"] if ":direction:" in finding["id"])
        assert finding["severity"] == "material"
        assert {item["cell_range"] for item in finding["evidence"]} == {"H1", "H2", "H30"}


def test_vat_uses_only_taxable_base_and_excludes_exempt_amounts(tmp_path: Path) -> None:
    source, context = semantic_source(tmp_path, [])
    context["vat"] = {
        "base_before_exemptions": "1000",
        "exempt_amounts": ["100"],
        "rate": "0.22",
        "declared_amount": "220",
        "source_fields_verified_complete": True,
        "evidence": evidence("H20"),
    }

    result = inspect_semantic(
        source,
        context,
        mode="full",
        purpose="internal_review",
        trusted_domain_context={"vat": context["vat"]},
    )

    finding = next(f for f in result["findings"] if f["id"].startswith("VAT-01"))
    assert finding["calculation"] == {
        "observed": "220",
        "expected": "198.00",
        "formula": "(base_before_exemptions - sum(exempt_amounts)) * rate",
        "difference": "22.00",
        "unit": None,
    }


def test_kac_missing_fields_are_limited_while_other_confirmed_issues_are_reported(
    tmp_path: Path,
) -> None:
    duplicated = {
        "entity": "estimate_row",
        "estimate_id": "lsr-1",
        "code": "MAT-1",
        "name": "Материал",
        "quantity": "1",
        "unit": "шт",
        "unit_price": "10",
        "declared_total": "10",
        "evidence": evidence("A2:F2"),
    }
    source, context = semantic_source(
        tmp_path,
        [
            {**duplicated, "row_id": "r-1"},
            {**duplicated, "row_id": "r-2", "evidence": evidence("A3:F3")},
            {
                "entity": "estimate_row",
                "row_id": "r-3",
                "estimate_id": "lsr-1",
                "name": "Нет цены",
                "quantity": "1",
                "unit": "шт",
                "declared_total": "1",
                "source_fields_verified_complete": True,
                "evidence": evidence("A4:F4"),
            },
            {
                "entity": "kac",
                "kac_id": "k-1",
                "name": "Материал по КАЦ",
                "price": "100",
                "source": "",
                "date": None,
                "vat_included": None,
                "delivery_included": None,
                "source_fields_verified_complete": True,
                "evidence": evidence("A10:F10"),
            },
            {
                "entity": "formula",
                "formula_id": "f-1",
                "formula": "=B2*C2",
                "computed_value": "20",
                "cached_value": "19",
                "evidence": evidence("D2"),
            },
        ],
    )

    result = inspect_semantic(source, context, mode="full", purpose="internal_review")
    identifiers = {finding["id"].split(":", 1)[0] for finding in result["findings"]}

    assert {"DUP-01", "FIELD-01", "FORMULA-01"} <= identifiers
    assert "KAC-01" not in identifiers
    kac = next(check for check in result["checks"] if check["id"] == "KAC-01")
    assert kac["status"] == "needs_input"
    assert {
        "unit",
        "name_characteristics",
        "region_or_supply_terms",
        "currency",
        "comparability",
        "source",
        "date",
        "vat_included",
        "delivery_included",
    } <= set(kac["parameters"]["record_states"][0]["missing_fields"])


def test_kac_passes_only_with_complete_comparable_price_and_evidence(tmp_path: Path) -> None:
    source, context = semantic_source(
        tmp_path,
        [
            {
                "entity": "kac",
                "kac_id": "k-complete",
                "name": "Насос",
                "characteristics": "10 м3/ч, 4 бар",
                "price": "120000",
                "unit": "шт",
                "region": "Смоленская область",
                "currency": "RUB",
                "comparability": "Одинаковые характеристики и условия поставки",
                "source": "Коммерческое предложение № 1",
                "date": "2026-08-01",
                "vat_included": True,
                "delivery_included": False,
                "evidence": evidence("A10:M10"),
            }
        ],
    )

    result = inspect_semantic(source, context, mode="full", purpose="internal_review")

    kac = next(check for check in result["checks"] if check["id"] == "KAC-01")
    assert kac["status"] == "passed"
    assert kac["parameters"]["record_states"] == [
        {
            "kac_id": "k-complete",
            "status": "passed",
            "missing_fields": [],
            "evidence": [evidence("A10:M10")],
        }
    ]
    assert kac["evidence"] == [evidence("A10:M10")]


def test_kac_file_level_locator_is_not_precise_evidence(tmp_path: Path) -> None:
    source, context = semantic_source(
        tmp_path,
        [
            {
                "entity": "kac",
                "kac_id": "k-file-only",
                "name": "Насос",
                "characteristics": "10 м3/ч, 4 бар",
                "price": "120000",
                "unit": "шт",
                "supply_terms": "Доставка до объекта",
                "currency": "RUB",
                "comparability": "Одинаковые характеристики",
                "source": "Коммерческое предложение № 1",
                "date": "2026-08-01",
                "vat_included": True,
                "delivery_included": True,
                "evidence": {
                    "source_path": "estimate.xlsx",
                    "locator": "estimate.xlsx",
                },
            }
        ],
    )

    result = inspect_semantic(source, context, mode="full", purpose="internal_review")

    kac = next(check for check in result["checks"] if check["id"] == "KAC-01")
    assert kac["status"] == "needs_input"
    assert kac["parameters"]["record_states"][0]["missing_fields"] == ["evidence"]


@pytest.mark.parametrize(
    ("reliability", "comparability", "expected_status"),
    [
        ("partial", "Одинаковые характеристики", "limited"),
        ("reliable", False, "needs_input"),
        ("reliable", {}, "needs_input"),
        ("reliable", 0, "needs_input"),
    ],
)
def test_kac_cannot_pass_partial_extraction_or_false_comparability(
    tmp_path: Path,
    reliability: str,
    comparability: object,
    expected_status: str,
) -> None:
    source, context = semantic_source(
        tmp_path,
        [
            {
                "entity": "kac",
                "kac_id": "k-trust-gap",
                "name": "Насос",
                "characteristics": "10 м3/ч, 4 бар",
                "price": "120000",
                "unit": "шт",
                "region": "Смоленская область",
                "currency": "RUB",
                "comparability": comparability,
                "source": "Коммерческое предложение № 1",
                "date": "2026-08-01",
                "vat_included": False,
                "delivery_included": False,
                "reliability": reliability,
                "evidence": evidence("A10:M10"),
            }
        ],
    )

    result = inspect_semantic(source, context, mode="full", purpose="internal_review")

    kac = next(check for check in result["checks"] if check["id"] == "KAC-01")
    assert kac["status"] == expected_status
    assert kac["parameters"]["record_states"][0]["status"] == expected_status
    assert not any(finding["id"].startswith("KAC-01") for finding in result["findings"])


def test_semantic_duplicates_and_component_completeness_are_checked(tmp_path: Path) -> None:
    source, context = semantic_source(
        tmp_path,
        [
            {"entity": "estimate_row", "row_id": "1", "estimate_id": "l", "name": "Монтаж насоса", "code": "A", "quantity": "1", "unit": "шт", "unit_price": "100", "declared_total": "100", "evidence": evidence("A2:F2")},
            {"entity": "estimate_row", "row_id": "2", "estimate_id": "l", "name": "  монтаж, насоса ", "code": "B", "quantity": "1", "unit": "шт", "unit_price": "101", "declared_total": "101", "evidence": evidence("A3:F3")},
            {"entity": "resource", "resource_id": "res-1", "name": "Труба", "quantity": "2", "unit": "м", "unit_price": None, "declared_total": "20", "source_fields_verified_complete": True, "evidence": evidence("A5:F5")},
            {"entity": "accrual", "accrual_id": "acc-1", "name": "НР", "basis": "100", "rate": "0.1", "declared_total": "10", "evidence": evidence("A6:F6")},
            {"entity": "index", "index_id": "idx-1", "value": "not-a-number", "evidence": evidence("A7")},
            {"entity": "coefficient", "coefficient_id": "coef-1", "value": "1.15", "evidence": evidence("A8")},
        ],
    )

    result = inspect_semantic(source, context, mode="full", purpose="internal_review")
    identifiers = {finding["id"].split(":", 1)[0] for finding in result["findings"]}

    assert {"DUP-02", "COMPONENT-01"} <= identifiers
    component = next(check for check in result["checks"] if check["id"] == "COMPONENT-01")
    assert component["status"] == "finding"


def test_route_checks_are_bounded_and_contract_change_is_reconciled(tmp_path: Path) -> None:
    source, context = semantic_source(tmp_path, [])
    context.update(
        {
            "work_type": "PIR",
            "special_routes": {"pir": {"inputs_complete": True}},
            "contract_change": {
                "project_estimate_total": "1000",
                "original_contract_estimate_total": "900",
                "changes_total": "50",
                "revised_contract_estimate_total": "960",
                "source_fields_verified_complete": True,
                "evidence": evidence("H30"),
            },
        }
    )

    result = inspect_semantic(
        source,
        context,
        mode="full",
        purpose="contract_estimate_changes",
        trusted_domain_context={"contract_change": context["contract_change"]},
    )
    statuses = {check["id"]: check["status"] for check in result["checks"]}

    assert statuses["ROUTE-PIR"] == "limited"
    assert statuses["ROUTE-SURVEY"] == "not_applicable"
    assert statuses["ROUTE-OKN"] == "not_applicable"
    assert statuses["ROUTE-DEMOLITION"] == "not_applicable"
    assert statuses["ROUTE-CONTRACT"] == "finding"
    contract = next(f for f in result["findings"] if f["id"].startswith("CONTRACT-01"))
    assert contract["calculation"]["expected"] == "950"
    assert contract["calculation"]["observed"] == "960"


def test_cost_analytics_groups_reliable_categories_but_rejects_public_unit_indicator(
    tmp_path: Path,
) -> None:
    source, context = semantic_source(
        tmp_path,
        [
            {"entity": "estimate_row", "row_id": "1", "estimate_id": "l", "name": "A", "quantity": "1", "unit": "шт", "unit_price": "60", "declared_total": "60", "category": "materials", "evidence": evidence("A2:F2")},
            {"entity": "estimate_row", "row_id": "2", "estimate_id": "l", "name": "B", "quantity": "1", "unit": "шт", "unit_price": "40", "declared_total": "40", "category": "labor", "evidence": evidence("A3:F3")},
        ],
    )
    context["unit_indicator"] = {"name": "м2", "quantity": "20"}

    result = inspect_semantic(source, context, mode="full", purpose="internal_review")

    assert result["cost_analytics"]["categories"] == {"labor": "40", "materials": "60"}
    assert result["cost_analytics"]["total"] == "100"
    assert result["cost_analytics"]["unit_indicator"] is None
    analytics = next(check for check in result["checks"] if check["id"] == "ANALYTICS-01")
    assert analytics["status"] == "limited"
    assert {item["cell_range"] for item in analytics["evidence"]} == {"A2:F2", "A3:F3"}
    assert any(
        limitation["code"] == "unit_indicator_not_trusted"
        for limitation in result["limitations"]
    )


def test_cost_analytics_excludes_unreliable_and_uncategorized_rows(tmp_path: Path) -> None:
    source, context = semantic_source(
        tmp_path,
        [
            {
                "entity": "estimate_row",
                "row_id": "reliable",
                "name": "Материал",
                "quantity": "1",
                "unit": "шт",
                "unit_price": "60",
                "declared_total": "60",
                "category": "materials",
                "evidence": evidence("A2:F2"),
            },
            {
                "entity": "estimate_row",
                "row_id": "partial",
                "name": "Работа",
                "quantity": "1",
                "unit": "шт",
                "unit_price": "40",
                "declared_total": "40",
                "category": "labor",
                "reliability": "partial",
                "evidence": evidence("A3:F3"),
            },
            {
                "entity": "estimate_row",
                "row_id": "uncategorized",
                "name": "Прочее",
                "quantity": "1",
                "unit": "шт",
                "unit_price": "5",
                "declared_total": "5",
                "evidence": evidence("A4:F4"),
            },
        ],
    )

    result = inspect_semantic(source, context, mode="full", purpose="internal_review")

    assert result["cost_analytics"]["categories"] == {"materials": "60"}
    assert result["cost_analytics"]["total"] == "60"
    assert result["cost_analytics"]["counted_rows"] == 1
    assert result["cost_analytics"]["excluded_rows"] == 2
    analytics = next(check for check in result["checks"] if check["id"] == "ANALYTICS-01")
    assert analytics["status"] == "limited"
    assert analytics["evidence"] == [evidence("A2:F2")]
    assert {
        "analytics_unreliable_rows_excluded",
        "analytics_uncategorized_rows_excluded",
    } <= {limitation["code"] for limitation in result["limitations"]}


def test_cost_analytics_does_not_pass_without_precise_row_evidence(tmp_path: Path) -> None:
    source, context = semantic_source(
        tmp_path,
        [
            {
                "entity": "estimate_row",
                "row_id": "file-level-only",
                "name": "Материал",
                "quantity": "1",
                "unit": "шт",
                "unit_price": "60",
                "declared_total": "60",
                "category": "materials",
                "evidence": {
                    "source_path": "estimate.xlsx",
                    "locator": "estimate.xlsx",
                },
            }
        ],
    )

    result = inspect_semantic(source, context, mode="full", purpose="internal_review")

    assert result["cost_analytics"]["categories"] == {}
    analytics = next(check for check in result["checks"] if check["id"] == "ANALYTICS-01")
    assert analytics["status"] == "limited"
    assert analytics["evidence"] == []
    assert any(
        limitation["code"] == "analytics_rows_without_precise_evidence"
        for limitation in result["limitations"]
    )


def test_private_evidence_bound_unit_indicator_is_computed() -> None:
    rows = [
        {
            "entity": "estimate_row",
            "row_id": "row",
            "name": "Работа",
            "quantity": "1",
            "unit": "шт",
            "unit_price": "100",
            "declared_total": "100",
            "category": "construction_work",
            "evidence": evidence("A2:F2"),
        }
    ]
    indicator = {
        "name": "м2",
        "quantity": "20",
        "source_fields_verified_complete": True,
        "evidence": evidence("H20"),
    }

    result = run_domain_records(
        rows,
        context={"unit_indicator": indicator},
        trusted_context_fields={"unit_indicator"},
    )

    assert result["cost_analytics"]["unit_indicator"] == {
        "name": "м2",
        "quantity": "20",
        "value": "5",
        "evidence": evidence("H20"),
    }
    analytics = next(check for check in result["checks"] if check["id"] == "ANALYTICS-01")
    assert analytics["status"] == "passed"
    assert {item["cell_range"] for item in analytics["evidence"]} == {"A2:F2", "H20"}


def test_unit_indicator_is_not_computed_without_categorized_cost_base() -> None:
    indicator = {
        "name": "м2",
        "quantity": "20",
        "source_fields_verified_complete": True,
        "evidence": evidence("H20"),
    }

    result = run_domain_records(
        [
            {
                "entity": "estimate_row",
                "row_id": "uncategorized",
                "name": "Работа",
                "quantity": "1",
                "unit": "шт",
                "unit_price": "100",
                "declared_total": "100",
                "evidence": evidence("A2:F2"),
            }
        ],
        context={"unit_indicator": indicator},
        trusted_context_fields={"unit_indicator"},
    )

    assert result["cost_analytics"]["unit_indicator"] is None
    assert next(check for check in result["checks"] if check["id"] == "ANALYTICS-01")[
        "status"
    ] == "limited"
    assert any(
        limitation["code"] == "unit_indicator_categorized_base_not_available"
        for limitation in result["limitations"]
    )


def test_negative_trusted_unit_indicator_denominator_is_rejected() -> None:
    indicator = {
        "name": "м2",
        "quantity": "-5",
        "source_fields_verified_complete": True,
        "evidence": evidence("H20"),
    }

    result = run_domain_records(
        [
            {
                "entity": "estimate_row",
                "row_id": "categorized",
                "name": "Работа",
                "quantity": "1",
                "unit": "шт",
                "unit_price": "100",
                "declared_total": "100",
                "category": "construction_work",
                "evidence": evidence("A2:F2"),
            }
        ],
        context={"unit_indicator": indicator},
        trusted_context_fields={"unit_indicator"},
    )

    assert result["cost_analytics"]["unit_indicator"] is None
    assert next(check for check in result["checks"] if check["id"] == "ANALYTICS-01")[
        "status"
    ] == "needs_input"
    assert any(
        limitation["code"] == "unit_indicator_value_not_usable"
        for limitation in result["limitations"]
    )


def test_full_formula_check_emits_state_and_evidence_for_every_formula(tmp_path: Path) -> None:
    source, context = semantic_source(
        tmp_path,
        [
            {
                "entity": "formula",
                "formula_id": "f-pass",
                "formula": "=1+1",
                "computed_value": "2",
                "cached_value": "2",
                "evidence": evidence("D2"),
            },
            {
                "entity": "formula",
                "formula_id": "f-mismatch",
                "formula": "=10+10",
                "computed_value": "20",
                "cached_value": "19",
                "evidence": evidence("D3"),
            },
            {
                "entity": "formula",
                "formula_id": "f-limited",
                "formula": "=UNSUPPORTED(A1)",
                "computed_value": None,
                "cached_value": "5",
                "evidence": evidence("D4"),
            },
        ],
    )

    result = inspect_semantic(source, context, mode="full", purpose="internal_review")

    formula = next(check for check in result["checks"] if check["id"] == "FORMULA-01")
    states = {state["formula_id"]: state for state in formula["parameters"]["row_states"]}
    assert formula["status"] == "finding"
    assert {identifier: state["status"] for identifier, state in states.items()} == {
        "f-pass": "passed",
        "f-mismatch": "finding",
        "f-limited": "limited",
    }
    assert all(len(state["evidence"]) == 1 for state in states.values())
    assert {item["cell_range"] for item in formula["evidence"]} == {"D2", "D3", "D4"}


def test_full_formula_aggregate_is_derived_from_nonfinding_states(tmp_path: Path) -> None:
    source, context = semantic_source(
        tmp_path,
        [
            {
                "entity": "formula",
                "formula_id": "f-pass",
                "formula": "=1+1",
                "computed_value": "2",
                "cached_value": "2",
                "evidence": evidence("D2"),
            },
            {
                "entity": "formula",
                "formula_id": "f-limited",
                "formula": "=UNSUPPORTED(A1)",
                "computed_value": None,
                "cached_value": "5",
                "evidence": evidence("D3"),
            },
        ],
    )

    result = inspect_semantic(source, context, mode="full", purpose="internal_review")

    formula = next(check for check in result["checks"] if check["id"] == "FORMULA-01")
    assert formula["status"] == "limited"
    assert [state["status"] for state in formula["parameters"]["row_states"]] == [
        "passed",
        "limited",
    ]


def test_formula_without_precise_evidence_cannot_pass_or_create_finding(tmp_path: Path) -> None:
    source, context = semantic_source(
        tmp_path,
        [
            {
                "entity": "formula",
                "formula_id": "file-level-only",
                "formula": "=1+1",
                "computed_value": "2",
                "cached_value": "3",
                "evidence": {
                    "source_path": "estimate.xlsx",
                    "locator": "estimate.xlsx",
                },
            }
        ],
    )

    result = inspect_semantic(source, context, mode="full", purpose="internal_review")

    formula = next(check for check in result["checks"] if check["id"] == "FORMULA-01")
    assert formula["status"] == "limited"
    assert formula["parameters"]["row_states"] == [
        {
            "formula_id": "file-level-only",
            "status": "limited",
            "reason": "formula_evidence_not_precise",
            "evidence": [
                {"source_path": "estimate.xlsx", "locator": "estimate.xlsx"}
            ],
        }
    ]
    assert not any(finding["id"].startswith("FORMULA-01") for finding in result["findings"])


@pytest.mark.parametrize(
    ("work_type", "route_key", "control_id"),
    [
        ("PIR", "pir", "ROUTE-PIR"),
        ("surveys", "surveys", "ROUTE-SURVEY"),
        ("OKN", "okn", "ROUTE-OKN"),
        ("demolition", "demolition", "ROUTE-DEMOLITION"),
    ],
)
def test_special_routes_remain_limited_even_with_verified_current_source(
    tmp_path: Path,
    work_type: str,
    route_key: str,
    control_id: str,
) -> None:
    source, context = semantic_source(tmp_path, [])
    context["work_type"] = work_type
    context["normative_sources_verified"] = [
        {
            "id": "OFF-01",
            "class": "method",
            "title": "Подтверждённый официальный источник",
            "normativity": "normative",
            "official_url": "https://example.test/official",
                "checked_at": "2026-08-26",
                "applicability": route_key,
                "edition": "редакция 2026-08-26",
                "pinpoint": "п. 1",
                "current_verified": True,
        }
    ]
    context["special_routes"] = {
        route_key: {"inputs_complete": True, "source_id": "OFF-01"}
    }

    passed = inspect_semantic(
        source,
        context,
        mode="full",
        purpose="internal_review",
        trusted_domain_context={
            "normative_sources_verified": context["normative_sources_verified"]
        },
    )

    assert next(check for check in passed["checks"] if check["id"] == control_id)["status"] == "limited"
    assert any(limit["code"] == "manual_route_review_required" for limit in passed["limitations"])

    context["special_routes"][route_key]["inputs_complete"] = False
    incomplete = inspect_semantic(
        source,
        context,
        mode="full",
        purpose="internal_review",
        trusted_domain_context={
            "normative_sources_verified": context["normative_sources_verified"]
        },
    )
    assert next(check for check in incomplete["checks"] if check["id"] == control_id)["status"] == "needs_input"
    assert not any(item["id"].startswith(control_id) for item in incomplete["findings"])


def test_normative_source_is_current_only_when_explicitly_live_verified(tmp_path: Path) -> None:
    source, context = semantic_source(tmp_path, [])
    context["work_type"] = "PIR"
    context["normative_sources_verified"] = [
        {
            "id": "OFF-01",
            "class": "method",
            "title": "Источник без live-флага",
            "normativity": "normative",
            "official_url": "https://example.test/official",
            "checked_at": "2026-08-26",
            "applicability": "pir",
            "edition": "редакция 2026-08-26",
            "pinpoint": "п. 1",
        }
    ]
    context["special_routes"] = {
        "pir": {"inputs_complete": True, "source_id": "OFF-01"}
    }

    result = inspect_semantic(
        source,
        context,
        mode="full",
        purpose="internal_review",
        trusted_domain_context={
            "normative_sources_verified": context["normative_sources_verified"]
        },
    )

    assert next(check for check in result["checks"] if check["id"] == "ROUTE-PIR")["status"] == "limited"
    assert any(limit["code"] == "currentness_not_verified" for limit in result["limitations"])
    assert all(source["id"] != "OFF-01" for source in result["normative_sources"])


def test_public_context_cannot_self_declare_a_verified_normative_source(tmp_path: Path) -> None:
    source, context = semantic_source(tmp_path, [])
    context["work_type"] = "PIR"
    context["normative_sources_verified"] = [
        {
            "id": "FAKE-01",
            "class": "method",
            "title": "Самодекларированный источник",
            "normativity": "normative",
            "official_url": "https://example.test/fake",
            "checked_at": "2026-08-26",
            "applicability": "pir",
            "edition": "редакция 2026-08-26",
            "pinpoint": "п. 1",
            "current_verified": True,
        }
    ]
    context["special_routes"] = {
        "pir": {"inputs_complete": True, "source_id": "FAKE-01"}
    }

    result = inspect_semantic(source, context, mode="full", purpose="internal_review")

    assert result["normative_sources"] == []
    assert next(check for check in result["checks"] if check["id"] == "ROUTE-PIR")["status"] == "limited"
    assert {"public_normative_sources_not_trusted", "currentness_not_verified"} <= {
        limitation["code"] for limitation in result["limitations"]
    }


def test_incomplete_extraction_is_limitation_not_material_finding(tmp_path: Path) -> None:
    source, context = semantic_source(
        tmp_path,
        [
            {"entity": "estimate_row", "row_id": "row", "name": "Работа", "quantity": "1", "unit": "шт", "unit_price": None, "declared_total": "10", "evidence": evidence("A2:F2")},
            {"entity": "resource", "resource_id": "resource", "name": "Материал", "quantity": "1", "unit": "шт", "unit_price": None, "declared_total": "10", "evidence": evidence("A3:F3")},
            {"entity": "kac", "kac_id": "kac", "name": "Цена", "price": "10", "source": None, "date": None, "vat_included": None, "delivery_included": None, "evidence": evidence("A4:F4")},
        ],
    )

    result = inspect_semantic(source, context, mode="full", purpose="internal_review")

    assert not any(finding["id"].startswith(("FIELD-01", "COMPONENT-01", "KAC-01")) for finding in result["findings"])
    assert {"row_fields_not_reliably_extracted", "component_fields_not_reliably_extracted", "kac_fields_not_reliably_extracted"} <= {
        limit["code"] for limit in result["limitations"]
    }
    assert result["overall_status"] != "material_nonconformities"


def test_unverified_context_values_do_not_mint_vat_or_contract_findings(tmp_path: Path) -> None:
    source, context = semantic_source(tmp_path, [])
    context["vat"] = {
        "base_before_exemptions": "1000",
        "exempt_amounts": ["100"],
        "rate": "0.22",
        "declared_amount": "220",
        "evidence": evidence("H20"),
    }
    context["contract_change"] = {
        "project_estimate_total": "1000",
        "original_contract_estimate_total": "900",
        "changes_total": "50",
        "revised_contract_estimate_total": "960",
        "evidence": evidence("H30"),
    }

    result = inspect_semantic(
        source,
        context,
        mode="full",
        purpose="contract_estimate_changes",
    )

    assert not any(
        finding["id"].startswith(("VAT-01", "CONTRACT-01"))
        for finding in result["findings"]
    )
    assert next(check for check in result["checks"] if check["id"] == "VAT-01")["status"] == "needs_input"
    assert next(check for check in result["checks"] if check["id"] == "ROUTE-CONTRACT")["status"] == "needs_input"
    assert {"vat_source_values_not_verified", "contract_source_values_not_verified"} <= {
        limitation["code"] for limitation in result["limitations"]
    }


def test_incompletely_extracted_rows_are_not_reported_as_duplicates(tmp_path: Path) -> None:
    source, context = semantic_source(
        tmp_path,
        [
            {
                "entity": "estimate_row",
                "row_id": row_id,
                "name": "Неполная строка",
                "quantity": "1",
                "unit": "шт",
                "unit_price": None,
                "declared_total": "10",
                "evidence": evidence(cell),
            }
            for row_id, cell in (("r-1", "A2:F2"), ("r-2", "A3:F3"))
        ],
    )

    result = inspect_semantic(source, context, mode="full", purpose="internal_review")

    assert not any(finding["id"].startswith(("DUP-01", "DUP-02")) for finding in result["findings"])
    assert result["overall_status"] != "material_nonconformities"


def test_canonical_model_reads_excel_pdf_and_known_xml_semantics_without_guessing_unknown_xml() -> None:
    inventory = [
        {
            "path": "estimate.xlsx",
            "file_type": "xlsx",
            "details": {
                "format": "xlsx",
                "sheets": [
                    {
                        "name": "ЛСР",
                        "semantic_tables": [
                            {
                                "rows": [
                                    {
                                        "row": 2,
                                        "field_values": {
                                            "name": "Работа",
                                            "quantity": "2",
                                            "unit": "м2",
                                            "unit_price": "10",
                                            "amount": "20",
                                        },
                                        "cell_locators": {
                                            "name": "ЛСР!A2",
                                            "quantity": "ЛСР!B2",
                                            "unit": "ЛСР!C2",
                                            "unit_price": "ЛСР!D2",
                                            "amount": "ЛСР!E2",
                                        },
                                        "evidence": {"locator": "ЛСР!2:2"},
                                    }
                                ]
                            }
                        ],
                        "cells": [],
                    }
                ],
            },
        },
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
                                "bbox": [0, 0, 100, 40],
                                "rows": [
                                    {
                                        "row_index": 1,
                                        "bbox": [0, 0, 100, 20],
                                        "cells": [
                                            {"text": "name", "bbox": [0, 0, 20, 20]},
                                            {"text": "quantity", "bbox": [20, 0, 40, 20]},
                                            {"text": "unit", "bbox": [40, 0, 60, 20]},
                                            {"text": "unit price", "bbox": [60, 0, 80, 20]},
                                            {"text": "total", "bbox": [80, 0, 100, 20]},
                                        ],
                                    },
                                    {
                                        "row_index": 2,
                                        "cells": [
                                            {"text": "Материал", "bbox": [0, 20, 20, 40]},
                                            {"text": "1", "bbox": [20, 20, 40, 40]},
                                            {"text": "шт", "bbox": [40, 20, 60, 40]},
                                            {"text": "5", "bbox": [60, 20, 80, 40]},
                                            {"text": "5", "bbox": [80, 20, 100, 40]},
                                        ],
                                        "bbox": [0, 20, 100, 40],
                                        "evidence": {"locator": "page:1:table:1:row:2"},
                                    },
                                ],
                            }
                        ],
                    }
                ],
            },
        },
        {
            "path": "known.gge",
            "file_type": "gge",
            "details": {
                "format": "xml",
                "schema": {"id": "minstroy.local_estimate_rim.3_01"},
                "schema_validation": {"status": "valid"},
                "semantic_records": [
                    {
                        "record_type": "estimate_row",
                        "row_id": "xml-row-1",
                        "estimate_id": "xml-estimate-1",
                        "name": "Работа XML",
                        "quantity": "3",
                        "unit": "шт",
                        "unit_price": "7",
                        "declared_total": "21",
                        "evidence": {
                            "xpath": "/Construction/Rows/Row",
                            "line": 10,
                            "locator": "xpath:/Construction/Rows/Row;line:10",
                        },
                    }
                ],
            },
        },
        {
            "path": "unknown.xml",
            "file_type": "xml",
            "details": {
                "format": "xml",
                "schema": None,
                "schema_validation": {"status": "unsupported_schema"},
                "structure": [{"evidence": {"xpath": "/Vendor", "line": 1}}],
                "semantic_values": [
                    {"local_name": "Total", "raw_value": "999", "xpath": "/Vendor/Total"}
                ],
            },
        },
    ]

    model = build_canonical_model(inventory, {})

    assert len(model["rows"]) == 3
    assert {row["evidence"]["source_path"] for row in model["rows"]} == {
        "estimate.xlsx",
        "estimate.pdf",
        "known.gge",
    }
    assert any(document["document_type"] == "LSR" for document in model["documents"])
    excel_row = next(row for row in model["rows"] if row["evidence"]["source_path"] == "estimate.xlsx")
    assert excel_row["evidence"]["sheet"] == "ЛСР"
    assert excel_row["evidence"]["cell_range"] == "A2:E2"
    assert all(row["evidence"]["source_path"] != "unknown.xml" for row in model["rows"])
    assert any(gap["source_path"] == "unknown.xml" for gap in model["unclassified_candidate_ranges"])
