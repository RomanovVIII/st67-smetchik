from __future__ import annotations

import re
from collections import defaultdict
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable
from urllib.parse import urlparse


INTERNAL_SOURCE_ID = "INT-01"
INTERNAL_SOURCE = {
    "id": INTERNAL_SOURCE_ID,
    "class": "internal",
    "title": "Встроенные детерминированные контроли Сметчика",
    "normativity": "non_normative",
    "official_url": None,
    "checked_at": "plugin-version:0.1.0",
    "applicability": "Арифметика, целостность данных и согласованность представленного пакета.",
    "edition": "smetchik-0.1.0",
    "pinpoint": "skills/smetchik/references/control-matrix.md#правило-результата",
    "locator": "skills/smetchik/references/control-matrix.md#правило-результата",
}

CHECK_STATUSES = {"passed", "finding", "not_applicable", "needs_input", "limited"}
CALCULATION_FORMULAS: dict[tuple[str, ...], str] = {
    ("quantity", "unit_price"): "quantity * unit_price",
    ("quantity", "unit_price", "index"): "quantity * unit_price * index",
    ("quantity", "unit_price", "coefficient"): "quantity * unit_price * coefficient",
    (
        "quantity",
        "unit_price",
        "index",
        "coefficient",
    ): "quantity * unit_price * index * coefficient",
}
CONTROL_MATRIX_IDS = (
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
)


def _decimal(value: Any) -> Decimal | None:
    if isinstance(value, bool) or value is None or value == "":
        return None
    try:
        parsed = Decimal(str(value).replace(" ", "").replace(",", "."))
    except (InvalidOperation, ValueError):
        return None
    return parsed if parsed.is_finite() else None


def _decimal_text(value: Decimal | Any) -> str:
    if not isinstance(value, Decimal):
        parsed = _decimal(value)
        if parsed is None:
            return str(value)
        value = parsed
    return format(value, "f")


def _evidence(record: dict[str, Any], fallback: str = "context") -> dict[str, Any]:
    source = record.get("evidence") if isinstance(record.get("evidence"), dict) else {}
    result = dict(source)
    result.setdefault("source_path", fallback)
    result.setdefault("locator", fallback)
    for optional in ("sheet", "page", "cell_range", "xpath", "line", "bbox"):
        if result.get(optional) is None:
            result.pop(optional, None)
    return result


def _precise_evidence_present(record: dict[str, Any]) -> bool:
    evidence = record.get("evidence")
    if not (
        isinstance(evidence, dict)
        and isinstance(evidence.get("source_path"), str)
        and bool(evidence["source_path"].strip())
        and isinstance(evidence.get("locator"), str)
        and bool(evidence["locator"].strip())
    ):
        return False
    excel_location = (
        isinstance(evidence.get("sheet"), str)
        and bool(evidence["sheet"].strip())
        and isinstance(evidence.get("cell_range"), str)
        and bool(evidence["cell_range"].strip())
    )
    pdf_location = isinstance(evidence.get("page"), int) and evidence["page"] > 0
    xml_location = (
        isinstance(evidence.get("xpath"), str)
        and evidence["xpath"].startswith("/")
    ) or (isinstance(evidence.get("line"), int) and evidence["line"] > 0)
    return excel_location or pdf_location or xml_location


def _check(
    control_id: str,
    status: str,
    *,
    evidence: Iterable[dict[str, Any]] = (),
    parameters: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if status not in CHECK_STATUSES:
        raise ValueError(f"invalid check status: {status}")
    return {
        "id": control_id,
        "status": status,
        "evidence": list(evidence),
        "parameters": parameters or {},
    }


def _calculation(
    *,
    observed: Any,
    expected: Any,
    formula: str,
    difference: Any,
    unit: str | None,
) -> dict[str, Any]:
    return {
        "observed": _decimal_text(observed) if isinstance(observed, Decimal) else observed,
        "expected": _decimal_text(expected) if isinstance(expected, Decimal) else expected,
        "formula": formula,
        "difference": _decimal_text(difference) if isinstance(difference, Decimal) else difference,
        "unit": unit,
    }


def _finding(
    finding_id: str,
    *,
    title: str,
    statement: str,
    evidence: Iterable[dict[str, Any]],
    calculation: dict[str, Any],
    impact: str,
    action: str,
    severity: str = "material",
    confidence: str = "confirmed",
    source_ids: list[str] | None = None,
) -> dict[str, Any]:
    normalized_evidence = []
    for item in evidence:
        normalized = dict(item)
        normalized.setdefault("source_path", "context")
        normalized.setdefault("locator", normalized["source_path"])
        normalized_evidence.append(normalized)
    return {
        "id": finding_id,
        "title": title,
        "severity": severity,
        "confidence": confidence,
        "statement": statement,
        "limitation": False,
        "question": False,
        "evidence": normalized_evidence or [{"source_path": "context", "locator": "context"}],
        "calculation": calculation,
        "source_ids": source_ids or [INTERNAL_SOURCE_ID],
        "impact": impact,
        "action": action,
    }


def _verified_sources(
    context: dict[str, Any],
    *,
    trusted: bool,
) -> tuple[list[dict[str, Any]], set[str]]:
    sources: list[dict[str, Any]] = []
    ids: set[str] = set()
    if not trusted:
        return sources, ids
    raw = context.get("normative_sources_verified")
    if isinstance(raw, dict):
        raw = list(raw.values())
    if not isinstance(raw, list):
        return sources, ids
    required = {
        "id",
        "class",
        "title",
        "normativity",
        "official_url",
        "checked_at",
        "applicability",
        "edition",
        "pinpoint",
    }
    for source in raw:
        if not isinstance(source, dict) or not required <= source.keys():
            continue
        if source.get("current_verified") is not True:
            continue
        source_class = str(source.get("class") or "")
        official_url = source.get("official_url")
        if source_class != "internal":
            if not isinstance(official_url, str):
                continue
            parsed_url = urlparse(official_url)
            if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
                continue
        if not isinstance(source.get("edition"), str) or not source["edition"].strip():
            continue
        if not isinstance(source.get("pinpoint"), str) or not source["pinpoint"].strip():
            continue
        normalized = {key: source[key] for key in required}
        normalized["locator"] = source.get("locator")
        sources.append(normalized)
        ids.add(str(source["id"]))
    return sources, ids


def _arithmetic_checks(
    model: dict[str, Any],
    mode: str,
) -> tuple[dict[str, Any], list[dict[str, Any]], set[str]]:
    rows = [row for row in model["rows"] if row.get("reliability", "reliable") == "reliable"]
    if mode == "light":
        return (
            _check(
                "ARITH-01",
                "limited" if rows else "not_applicable",
                parameters={
                    "reason": "light_mode_has_no_row_by_row_recalculation",
                    "row_states": [],
                },
            ),
            [],
            set(),
        )
    findings: list[dict[str, Any]] = []
    checked: set[str] = set()
    row_states: list[dict[str, str]] = []
    for ordinal, row in enumerate(rows, start=1):
        row_id = str(row.get("row_id") or ordinal)
        quantity = _decimal(row.get("quantity"))
        unit_price = _decimal(row.get("unit_price"))
        declared = _decimal(row.get("declared_total"))
        if quantity is None or unit_price is None or declared is None:
            row_states.append({"row_id": row_id, "status": "needs_input"})
            continue
        basis = row.get("calculation_basis")
        operands: tuple[str, ...] = ()
        formula: str | None = None
        if isinstance(basis, dict):
            raw_operands = basis.get("operand_fields")
            evidence = basis.get("evidence")
            if isinstance(raw_operands, list) and all(
                isinstance(field, str) for field in raw_operands
            ):
                operands = tuple(raw_operands)
            expected_formula = CALCULATION_FORMULAS.get(operands)
            if (
                expected_formula is not None
                and basis.get("formula") == expected_formula
                and basis.get("source_fields_verified_complete") is True
                and isinstance(evidence, dict)
                and isinstance(evidence.get("locator"), str)
                and bool(evidence["locator"].strip())
            ):
                formula = expected_formula
        if formula is None:
            row_states.append(
                {
                    "row_id": row_id,
                    "status": "limited",
                    "reason": "calculation_basis_not_verified",
                }
            )
            continue
        operand_values = [_decimal(row.get(field)) for field in operands]
        if any(value is None for value in operand_values):
            row_states.append({"row_id": row_id, "status": "needs_input"})
            continue
        checked.add(row_id)
        expected = Decimal("1")
        for operand in operand_values:
            assert operand is not None
            expected *= operand
        if declared == expected:
            row_states.append({"row_id": row_id, "status": "passed"})
            continue
        difference = declared - expected
        row_states.append({"row_id": row_id, "status": "finding"})
        findings.append(
            _finding(
                f"ARITH-01:{row_id}",
                title="Несоответствие арифметики строки",
                statement="Заявленная стоимость строки не равна точному произведению доступных множителей.",
                evidence=[_evidence(row)],
                calculation=_calculation(
                    observed=declared,
                    expected=expected,
                    formula=formula,
                    difference=difference,
                    unit=row.get("currency_unit"),
                ),
                impact=f"Итог строки отличается на {_decimal_text(difference)}.",
                action="Проверить исходные значения и исправить формулу либо заявленный итог в копии документа.",
            )
        )
    if not rows:
        status = "not_applicable"
    elif findings:
        status = "finding"
    elif any(state["status"] == "needs_input" for state in row_states):
        status = "needs_input"
    elif any(state["status"] == "limited" for state in row_states):
        status = "limited"
    else:
        status = "passed"
    return _check("ARITH-01", status, parameters={"row_states": row_states}), findings, checked


def _state_status(states: Iterable[dict[str, Any]]) -> str:
    statuses = [str(state.get("status") or "limited") for state in states]
    if not statuses:
        return "not_applicable"
    for status in ("finding", "needs_input", "limited"):
        if status in statuses:
            return status
    return "passed" if all(status == "passed" for status in statuses) else "limited"


def _formula_checks(model: dict[str, Any], mode: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    formulas = model["formulas"]
    if not formulas:
        return _check("FORMULA-01", "not_applicable", parameters={"row_states": []}), []
    formula_evidence = [_evidence(record) for record in formulas]
    if mode == "light":
        return (
            _check(
                "FORMULA-01",
                "limited",
                evidence=formula_evidence,
                parameters={"reason": "light_mode", "row_states": []},
            ),
            [],
        )
    findings: list[dict[str, Any]] = []
    row_states: list[dict[str, Any]] = []
    for ordinal, record in enumerate(formulas, start=1):
        identifier = str(record.get("formula_id") or ordinal)
        evidence = [_evidence(record)]
        if record.get("reliability", "reliable") != "reliable":
            row_states.append(
                {
                    "formula_id": identifier,
                    "status": "limited",
                    "reason": "formula_extraction_not_reliable",
                    "evidence": evidence,
                }
            )
            continue
        if not _precise_evidence_present(record):
            row_states.append(
                {
                    "formula_id": identifier,
                    "status": "limited",
                    "reason": "formula_evidence_not_precise",
                    "evidence": evidence,
                }
            )
            continue
        computed = _decimal(record.get("computed_value"))
        cached = _decimal(record.get("cached_value"))
        if computed is None or cached is None:
            row_states.append(
                {
                    "formula_id": identifier,
                    "status": "needs_input" if computed is None and cached is None else "limited",
                    "reason": (
                        "formula_values_not_available"
                        if computed is None and cached is None
                        else "formula_independent_recalculation_unavailable"
                    ),
                    "evidence": evidence,
                }
            )
            continue
        if computed == cached:
            row_states.append(
                {
                    "formula_id": identifier,
                    "status": "passed",
                    "evidence": evidence,
                }
            )
            continue
        row_states.append(
            {
                "formula_id": identifier,
                "status": "finding",
                "reason": "cached_value_mismatch",
                "evidence": evidence,
            }
        )
        findings.append(
            _finding(
                f"FORMULA-01:{identifier}",
                title="Формула и сохранённый результат расходятся",
                statement="Безопасно пересчитанная формула не совпадает с сохранённым значением.",
                evidence=evidence,
                calculation=_calculation(
                    observed=cached,
                    expected=computed,
                    formula=str(record.get("formula") or "stored_formula"),
                    difference=cached - computed,
                    unit=record.get("unit"),
                ),
                impact="Переносимый итог может зависеть от неактуального кэша формулы.",
                action="Пересчитать книгу в доверенной среде и проверить формулу в указанной ячейке.",
            )
        )
    status = _state_status(row_states)
    return (
        _check(
            "FORMULA-01",
            status,
            evidence=formula_evidence,
            parameters={
                "row_states": row_states,
                "evaluated": sum(state["status"] in {"passed", "finding"} for state in row_states),
                "unevaluated": sum(state["status"] in {"limited", "needs_input"} for state in row_states),
            },
        ),
        findings,
    )


ESTIMATE_TYPE_ALIASES = {
    "lsr": "LSR",
    "лср": "LSR",
    "local_estimate": "LSR",
    "osr": "OSR",
    "оср": "OSR",
    "object_estimate": "OSR",
    "ssr": "SSR",
    "сср": "SSR",
    "summary_estimate": "SSR",
    "osr_or_ssr": "OSR_OR_SSR",
    "object_or_summary_estimate": "OSR_OR_SSR",
}
EXPECTED_HIERARCHY_EDGES = {("LSR", "OSR"), ("OSR", "SSR")}


def _estimate_type(record: dict[str, Any]) -> str | None:
    value = str(record.get("estimate_type") or "").strip().casefold()
    return ESTIMATE_TYPE_ALIASES.get(value)


def _hierarchy_cycles(parent_of: dict[str, str]) -> list[list[str]]:
    visit_state: dict[str, int] = {}
    stack: list[str] = []
    stack_positions: dict[str, int] = {}
    cycles: list[list[str]] = []

    def visit(node: str) -> None:
        visit_state[node] = 1
        stack_positions[node] = len(stack)
        stack.append(node)
        parent = parent_of.get(node)
        if parent in parent_of:
            if visit_state.get(parent, 0) == 0:
                visit(parent)
            elif visit_state.get(parent) == 1:
                cycles.append(stack[stack_positions[parent] :].copy())
        stack.pop()
        stack_positions.pop(node, None)
        visit_state[node] = 2

    for node in sorted(parent_of):
        if visit_state.get(node, 0) == 0:
            visit(node)
    return cycles


def _hierarchy_checks(
    model: dict[str, Any],
) -> tuple[
    dict[str, Any],
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
]:
    estimates = model["estimates"]
    hierarchy = [
        {
            "estimate_id": estimate.get("estimate_id"),
            "estimate_type": estimate.get("estimate_type"),
            "parent_id": estimate.get("parent_id"),
            "declared_total": estimate.get("declared_total"),
            "evidence": _evidence(estimate),
        }
        for estimate in estimates
    ]
    records_by_id: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for estimate in estimates:
        estimate_id = str(estimate.get("estimate_id") or "")
        if estimate_id:
            records_by_id[estimate_id].append(estimate)

    children: dict[str, list[dict[str, Any]]] = defaultdict(list)
    relation_states: list[dict[str, Any]] = []
    findings: list[dict[str, Any]] = []
    limitations: list[dict[str, Any]] = []
    parent_of: dict[str, str] = {}
    relation_by_edge: dict[tuple[str, str], dict[str, Any]] = {}

    for ordinal, child in enumerate(estimates, start=1):
        raw_parent_id = child.get("parent_id")
        if raw_parent_id in (None, ""):
            continue
        child_id = str(child.get("estimate_id") or f"relation-{ordinal}")
        parent_id = str(raw_parent_id)
        child_evidence = _evidence(child)
        parents = records_by_id.get(parent_id, [])
        if len(parents) != 1:
            available_evidence = [*[_evidence(parent) for parent in parents], child_evidence]
            state = {
                "relation_id": f"{child_id}->{parent_id}",
                "child_id": child_id,
                "parent_id": parent_id,
                "child_type": _estimate_type(child),
                "parent_type": None,
                "status": "needs_input",
                "reason": "parent_not_found" if not parents else "parent_id_not_unique",
                "evidence": available_evidence,
            }
            relation_states.append(state)
            limitations.append(
                {
                    "code": "hierarchy_parent_not_resolved",
                    "message": "Родительская смета для указанной связи не определена однозначно.",
                    "impact": "Направление связи и перенос итога не проверены.",
                    "required_input": "Предоставить однозначный идентификатор и документ родительской сметы.",
                    "evidence": available_evidence,
                }
            )
            continue

        parent = parents[0]
        evidence = [_evidence(parent), child_evidence]
        child_type = _estimate_type(child)
        parent_type = _estimate_type(parent)
        state = {
            "relation_id": f"{child_id}->{parent_id}",
            "child_id": child_id,
            "parent_id": parent_id,
            "child_type": child_type,
            "parent_type": parent_type,
            "status": "passed",
            "reason": "expected_hierarchy_direction",
            "evidence": evidence,
        }
        relation_data_trusted = (
            child.get("reliability", "reliable") == "reliable"
            and parent.get("reliability", "reliable") == "reliable"
            and _precise_evidence_present(child)
            and _precise_evidence_present(parent)
        )
        if child.get("reliability", "reliable") != "reliable" or parent.get(
            "reliability", "reliable"
        ) != "reliable":
            state["status"] = "limited"
            state["reason"] = "hierarchy_records_not_reliable"
            limitations.append(
                {
                    "code": "hierarchy_records_not_reliable",
                    "message": "Связь сметных уровней извлечена с неполной надёжностью.",
                    "impact": "Направление связи не считается доказательно проверенным.",
                    "required_input": "Подтвердить типы и связь по читаемым исходным документам.",
                    "evidence": evidence,
                }
            )
        elif not _precise_evidence_present(child) or not _precise_evidence_present(parent):
            state["status"] = "limited"
            state["reason"] = "hierarchy_evidence_not_precise"
            limitations.append(
                {
                    "code": "hierarchy_evidence_not_precise",
                    "message": "Связь сметных уровней не имеет точной страницы, ячейки или XPath для обоих документов.",
                    "impact": "Направление связи не считается доказательно проверенным.",
                    "required_input": "Уточнить точные позиции родительской и дочерней смет.",
                    "evidence": evidence,
                }
            )
        elif (child_type, parent_type) == ("LSR", "SSR"):
            attestation = child.get("hierarchy_relation_attestation")
            allowed = (
                attestation.get("direct_lsr_to_ssr_allowed")
                if isinstance(attestation, dict) and _source_values_verified(attestation)
                else None
            )
            if isinstance(allowed, bool):
                state["evidence"] = [*evidence, _evidence(attestation)]
                if allowed:
                    state["reason"] = "trusted_direct_lsr_to_ssr_allowed"
                else:
                    state["status"] = "finding"
                    state["reason"] = "trusted_direct_lsr_to_ssr_forbidden"
                    findings.append(
                        _finding(
                            f"HIER-01:direction:{child_id}",
                            title="Подтверждённый маршрут и прямая связь ЛСР–ССР расходятся",
                            statement="Доказательный профиль требует промежуточный уровень ОСР, но ЛСР связан непосредственно с ССР.",
                            evidence=state["evidence"],
                            calculation=_calculation(
                                observed="LSR -> SSR",
                                expected="LSR -> OSR -> SSR",
                                formula="trusted_hierarchy_route",
                                difference="intermediate_OSR_missing",
                                unit=None,
                            ),
                            impact="Структура переноса итогов не соответствует подтверждённому маршруту пакета.",
                            action="Добавить требуемый уровень ОСР либо подтвердить применимое исключение.",
                        )
                    )
            else:
                state["status"] = "needs_input"
                state["reason"] = "direct_lsr_to_ssr_route_not_attested"
                limitations.append(
                    {
                        "code": "direct_lsr_to_ssr_route_not_attested",
                        "message": "Прямая связь ЛСР–ССР не классифицируется без подтверждённого маршрута пакета.",
                        "impact": "Связь не считается ни допустимой, ни нарушающей требования.",
                        "required_input": "Подтвердить применимость промежуточного уровня ОСР или допустимое исключение по источнику.",
                        "evidence": evidence,
                    }
                )
        elif (child_type, parent_type) in EXPECTED_HIERARCHY_EDGES:
            pass
        elif child_type is None or parent_type is None or "OSR_OR_SSR" in {child_type, parent_type}:
            state["status"] = "needs_input"
            state["reason"] = "hierarchy_types_not_resolved"
            limitations.append(
                {
                    "code": "hierarchy_types_not_resolved",
                    "message": "Тип одного из уровней сметной иерархии не определён однозначно.",
                    "impact": "Направление связи ЛСР–ОСР–ССР не подтверждено.",
                    "required_input": "Уточнить типы родительской и дочерней смет.",
                    "evidence": evidence,
                }
            )
        else:
            state["status"] = "finding"
            state["reason"] = "invalid_hierarchy_direction"
            findings.append(
                _finding(
                    f"HIER-01:direction:{child_id}",
                    title="Недопустимое направление связи сметных уровней",
                    statement="Подтверждённые типы смет связаны в направлении вне последовательности ЛСР–ОСР–ССР.",
                    evidence=evidence,
                    calculation=_calculation(
                        observed=f"{child_type} -> {parent_type}",
                        expected="LSR -> OSR or OSR -> SSR",
                        formula="allowed_hierarchy_edges",
                        difference="invalid_direction",
                        unit=None,
                    ),
                    impact="Иерархия может переносить итоги между несопоставимыми уровнями.",
                    action="Исправить тип или родительскую связь сметы в копии документа.",
                )
            )
        relation_states.append(state)
        children[parent_id].append(child)
        if (
            relation_data_trusted
            and child.get("estimate_id") not in (None, "")
            and len(records_by_id[child_id]) == 1
        ):
            parent_of[child_id] = parent_id
            relation_by_edge[(child_id, parent_id)] = state

    cycles = _hierarchy_cycles(parent_of)
    for cycle_number, cycle_nodes in enumerate(cycles, start=1):
        cycle_edges = {(node, parent_of[node]) for node in cycle_nodes}
        cycle_evidence = [_evidence(records_by_id[node][0]) for node in cycle_nodes]
        for edge in cycle_edges:
            state = relation_by_edge.get(edge)
            if state is not None:
                state["status"] = "finding"
                state["reason"] = "hierarchy_cycle"
        cycle_path = " -> ".join([*cycle_nodes, cycle_nodes[0]])
        findings.append(
            _finding(
                f"HIER-01:cycle:{cycle_number}",
                title="Цикл в сметной иерархии",
                statement="Цепочка родительских связей возвращается к исходной смете.",
                evidence=cycle_evidence,
                calculation=_calculation(
                    observed=cycle_path,
                    expected="acyclic hierarchy",
                    formula="parent_links_form_acyclic_graph",
                    difference="cycle_detected",
                    unit=None,
                ),
                impact="Итоги нельзя однозначно агрегировать по уровням ЛСР–ОСР–ССР.",
                action="Разорвать циклическую родительскую связь и повторить проверку переносов.",
            )
        )

    aggregate_states: list[dict[str, Any]] = []
    for parent_id, child_records in children.items():
        parents = records_by_id.get(parent_id, [])
        if len(parents) != 1:
            continue
        parent = parents[0]
        evidence = [_evidence(parent), *[_evidence(child) for child in child_records]]
        declared = _decimal(parent.get("declared_total"))
        child_totals = [_decimal(child.get("declared_total")) for child in child_records]
        state: dict[str, Any] = {
            "aggregate_id": parent_id,
            "parent_id": parent_id,
            "child_ids": [str(child.get("estimate_id") or "") for child in child_records],
            "status": "passed",
            "reason": "totals_reconciled",
            "evidence": evidence,
        }
        aggregate_records = [parent, *child_records]
        if any(
            record.get("reliability", "reliable") != "reliable"
            for record in aggregate_records
        ):
            state["status"] = "limited"
            state["reason"] = "hierarchy_records_not_reliable"
        elif any(not _precise_evidence_present(record) for record in aggregate_records):
            state["status"] = "limited"
            state["reason"] = "hierarchy_evidence_not_precise"
        elif declared is None or any(total is None for total in child_totals):
            state["status"] = "needs_input"
            state["reason"] = "hierarchy_totals_not_available"
            limitations.append(
                {
                    "code": "hierarchy_totals_not_available",
                    "message": "Итог родительской или дочерней сметы не извлечён надёжно.",
                    "impact": "Перенос итогов по этой группе не пересчитан.",
                    "required_input": "Предоставить читаемые итоги родительской и всех дочерних смет.",
                    "evidence": evidence,
                }
            )
        else:
            expected = sum((total for total in child_totals if total is not None), Decimal(0))
            if declared != expected:
                state["status"] = "finding"
                state["reason"] = "total_mismatch"
                findings.append(
                    _finding(
                        f"HIER-01:total:{parent_id}",
                        title="Итог родительской сметы не равен сумме дочерних",
                        statement="Перенос итогов в иерархии ЛСР–ОСР–ССР арифметически не согласован.",
                        evidence=evidence,
                        calculation=_calculation(
                            observed=declared,
                            expected=expected,
                            formula="sum(child_estimate.declared_total)",
                            difference=declared - expected,
                            unit=parent.get("currency_unit"),
                        ),
                        impact=f"Итог уровня отличается на {_decimal_text(declared - expected)}.",
                        action="Сверить состав дочерних смет и перенос итога в родительскую смету.",
                    )
                )
        aggregate_states.append(state)

    all_states = [*relation_states, *aggregate_states]
    status = _state_status(all_states)
    return (
        _check(
            "HIER-01",
            status,
            evidence=[_evidence(item) for item in estimates],
            parameters={
                "relation_states": relation_states,
                "aggregate_states": aggregate_states,
            },
        ),
        findings,
        hierarchy,
        limitations,
    )


def _duplicate_and_field_checks(
    model: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    rows = [
        row
        for row in model["rows"]
        if row.get("reliability", "reliable") == "reliable"
    ]
    findings: list[dict[str, Any]] = []
    limitations: list[dict[str, Any]] = []
    required = ("name", "quantity", "unit", "unit_price", "declared_total")
    duplicate_candidates = [
        row
        for row in rows
        if all(row.get(field) not in (None, "") for field in required)
    ]
    duplicate_groups: dict[tuple[str, ...], list[dict[str, Any]]] = defaultdict(list)
    semantic_groups: dict[tuple[str, ...], list[dict[str, Any]]] = defaultdict(list)
    for row in duplicate_candidates:
        key = tuple(
            re.sub(r"\s+", " ", str(row.get(field) or "").strip().casefold())
            for field in ("estimate_id", "code", "name", "quantity", "unit", "unit_price", "declared_total")
        )
        duplicate_groups[key].append(row)
        semantic_name = re.sub(
            r"[^0-9a-zа-я]+",
            " ",
            str(row.get("name") or "").strip().casefold().replace("ё", "е"),
        ).strip()
        semantic_key = (
            str(row.get("estimate_id") or ""),
            semantic_name,
            str(row.get("quantity") or "").replace(",", "."),
            str(row.get("unit") or "").strip().casefold(),
        )
        semantic_groups[semantic_key].append(row)
    for ordinal, duplicates in enumerate(duplicate_groups.values(), start=1):
        if len(duplicates) < 2:
            continue
        findings.append(
            _finding(
                f"DUP-01:{ordinal}",
                title="Обнаружены совпадающие строки",
                statement="Строки совпадают по смете, наименованию, объёму, единице, цене и итогу.",
                evidence=[_evidence(row) for row in duplicates],
                calculation=_calculation(
                    observed=str(len(duplicates)),
                    expected="1",
                    formula="count(exact_normalized_row_signature)",
                    difference=str(len(duplicates) - 1),
                    unit="строка",
                ),
                impact="Возможное двойное включение стоимости.",
                action="Проверить основания повторных строк и удалить только подтверждённый дубль.",
                confidence="probable",
            )
        )
    for ordinal, duplicates in enumerate(semantic_groups.values(), start=1):
        if len(duplicates) < 2:
            continue
        exact_signatures = {
            tuple(
                re.sub(r"\s+", " ", str(row.get(field) or "").strip().casefold())
                for field in ("code", "name", "quantity", "unit", "unit_price", "declared_total")
            )
            for row in duplicates
        }
        if len(exact_signatures) == 1:
            continue
        findings.append(
            _finding(
                f"DUP-02:{ordinal}",
                title="Возможный смысловой дубль строк",
                statement="Строки имеют одинаковое нормализованное наименование, объём и единицу, но различаются другими реквизитами.",
                evidence=[_evidence(row) for row in duplicates],
                calculation=_calculation(
                    observed=str(len(duplicates)),
                    expected="1",
                    formula="count(normalized_name + quantity + unit within estimate)",
                    difference=str(len(duplicates) - 1),
                    unit="строка",
                ),
                impact="Возможно повторное включение одной работы или ресурса под разными реквизитами.",
                action="Сопоставить основания и подтвердить, являются ли строки самостоятельными позициями.",
                severity="recommendation",
                confidence="probable",
            )
        )
    uncertain_missing = 0
    for ordinal, row in enumerate(rows, start=1):
        missing = [field for field in required if row.get(field) in (None, "")]
        if not missing:
            continue
        if row.get("source_fields_verified_complete") is not True:
            uncertain_missing += 1
            limitations.append(
                {
                    "code": "row_fields_not_reliably_extracted",
                    "message": "Для строки не извлечены все расчётные поля; отсутствие в исходнике не подтверждено.",
                    "impact": "Строка не может считаться построчно проверенной, но дефект сметы не установлен.",
                    "required_input": "Предоставить машиночитаемую строку или визуально подтвердить исходные поля.",
                    "evidence": [_evidence(row)],
                }
            )
            continue
        identifier = str(row.get("row_id") or ordinal)
        findings.append(
            _finding(
                f"FIELD-01:{identifier}",
                title="В строке отсутствуют обязательные расчётные поля",
                statement="Для сплошной проверки строки недостаточно извлечённых значений.",
                evidence=[_evidence(row)],
                calculation=_calculation(
                    observed=", ".join(missing),
                    expected="name, quantity, unit, unit_price, declared_total",
                    formula="required_fields(row)",
                    difference="not_applicable",
                    unit=None,
                ),
                impact="Арифметика и обоснованность строки не могут быть подтверждены полностью.",
                action="Предоставить читаемую строку или заполнить перечисленные поля в исходном документе.",
                confidence="confirmed",
            )
        )
    duplicate_status = (
        "finding"
        if any(f["id"].startswith(("DUP-01", "DUP-02")) for f in findings)
        else "needs_input"
        if len(duplicate_candidates) != len(rows)
        else "passed"
        if rows
        else "not_applicable"
    )
    fields_status = (
        "needs_input"
        if uncertain_missing
        else "finding"
        if any(f["id"].startswith("FIELD-01") for f in findings)
        else "passed"
        if rows
        else "not_applicable"
    )
    return [
        _check(
            "DUP-01",
            duplicate_status,
            evidence=[_evidence(row) for row in rows],
            parameters={
                "evaluated_rows": len(duplicate_candidates),
                "unevaluated_rows": len(rows) - len(duplicate_candidates),
            },
        ),
        _check(
            "FIELD-01",
            fields_status,
            evidence=[_evidence(row) for row in rows],
            parameters={"uncertain_missing_rows": uncertain_missing},
        ),
    ], findings, limitations


def _component_checks(
    model: dict[str, Any],
    mode: str,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    specs = {
        "resources": ("resource_id", "name", "quantity", "unit", "unit_price", "declared_total"),
        "accruals": ("accrual_id", "name", "basis", "rate", "declared_total"),
        "indices": ("index_id", "value"),
        "coefficients": ("coefficient_id", "value"),
        "totals": ("total_type", "declared_total"),
    }
    records = [(collection, record) for collection in specs for record in model[collection]]
    if not records:
        return _check("COMPONENT-01", "not_applicable"), [], []
    if mode == "light":
        return _check("COMPONENT-01", "limited", parameters={"reason": "light_mode"}), [], []
    findings: list[dict[str, Any]] = []
    limitations: list[dict[str, Any]] = []
    states: list[dict[str, str]] = []
    for ordinal, (collection, record) in enumerate(records, start=1):
        missing = [field for field in specs[collection] if record.get(field) in (None, "")]
        if collection in {"indices", "coefficients"} and not missing and _decimal(record.get("value")) is None:
            missing.append("numeric_value")
        identifier = str(
            record.get("resource_id")
            or record.get("accrual_id")
            or record.get("index_id")
            or record.get("coefficient_id")
            or record.get("total_id")
            or ordinal
        )
        if not missing:
            states.append({"record_id": identifier, "status": "passed"})
            continue
        confirmed_invalid_value = "numeric_value" in missing
        if record.get("source_fields_verified_complete") is not True and not confirmed_invalid_value:
            states.append({"record_id": identifier, "status": "needs_input"})
            limitations.append(
                {
                    "code": "component_fields_not_reliably_extracted",
                    "message": f"Для компонента {collection} не извлечены все поля; исходный пропуск не подтверждён.",
                    "impact": "Компонент не проверен полностью, но несоответствие исходного документа не установлено.",
                    "required_input": "Подтвердить исходные поля по машиночитаемому документу или визуальной сверке.",
                    "evidence": [_evidence(record)],
                }
            )
            continue
        states.append({"record_id": identifier, "status": "finding"})
        findings.append(
            _finding(
                f"COMPONENT-01:{collection}:{identifier}",
                title="Неполные данные сметного компонента",
                statement=f"Компонент {collection} не содержит обязательных полей для воспроизводимой проверки.",
                evidence=[_evidence(record)],
                calculation=_calculation(
                    observed=", ".join(missing),
                    expected=", ".join(specs[collection]),
                    formula=f"required_fields({collection})",
                    difference="not_applicable",
                    unit=None,
                ),
                impact="Компонент не может быть сплошно проверен или независимо пересчитан.",
                action="Уточнить перечисленные поля в исходном машиночитаемом документе.",
            )
        )
    status = "needs_input" if limitations else "finding" if findings else "passed"
    return _check(
        "COMPONENT-01",
        status,
        evidence=[_evidence(record) for _collection, record in records],
        parameters={"record_states": states},
    ), findings, limitations


def _kac_checks(
    model: dict[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    findings: list[dict[str, Any]] = []
    limitations: list[dict[str, Any]] = []
    record_states: list[dict[str, Any]] = []

    def present(value: Any) -> bool:
        if isinstance(value, (dict, list, tuple, set)):
            return bool(value)
        return value is not None and (not isinstance(value, str) or bool(value.strip()))

    for ordinal, kac in enumerate(model["kacs"], start=1):
        identifier = str(kac.get("kac_id") or ordinal)
        missing: list[str] = []
        raw_price = kac.get("price") if present(kac.get("price")) else kac.get("unit_price")
        price = _decimal(raw_price)
        if price is None or price <= 0:
            missing.append("price")
        combined_name = kac.get("name_characteristics")
        if not (
            present(combined_name)
            or present(kac.get("description"))
            or (present(kac.get("name")) and present(kac.get("characteristics")))
        ):
            missing.append("name_characteristics")
        if not present(kac.get("unit")):
            missing.append("unit")
        if not any(
            present(kac.get(field))
            for field in (
                "region",
                "region_or_price_zone",
                "supply_terms",
                "delivery_terms",
                "terms_of_supply",
            )
        ):
            missing.append("region_or_supply_terms")
        if not any(present(kac.get(field)) for field in ("currency", "currency_code")):
            missing.append("currency")
        comparability_values = [
            kac.get(field)
            for field in ("comparability", "comparability_basis", "comparison_basis")
        ]
        if not any(
            value is True
            or (isinstance(value, str) and bool(value.strip()))
            for value in comparability_values
        ):
            missing.append("comparability")
        for field in ("source", "date", "vat_included", "delivery_included"):
            if not present(kac.get(field)):
                missing.append(field)
        if not _precise_evidence_present(kac):
            missing.append("evidence")
        reliable = kac.get("reliability", "reliable") == "reliable"
        state = {
            "kac_id": identifier,
            "status": "limited" if not reliable else "needs_input" if missing else "passed",
            "missing_fields": missing,
            "evidence": [_evidence(kac)],
        }
        if not reliable:
            state["reason"] = "kac_extraction_not_reliable"
        record_states.append(state)
        if not reliable:
            limitations.append(
                {
                    "code": "kac_record_not_reliable",
                    "message": "Данные КАЦ извлечены с неполной надёжностью и не могут получить passed.",
                    "impact": "Цена и сопоставимость не считаются доказательно проверенными.",
                    "required_input": "Подтвердить запись КАЦ по читаемому источнику.",
                    "evidence": [_evidence(kac)],
                }
            )
        elif missing:
            limitations.append(
                {
                    "code": "kac_fields_not_reliably_extracted",
                    "message": "КАЦ не содержит полного доказательного набора цены, характеристик и условий сопоставимости.",
                    "impact": "Цена не считается воспроизводимо проверенной, но дефект исходной сметы не установлен.",
                    "required_input": "Уточнить: " + ", ".join(missing) + ".",
                    "missing_fields": missing,
                    "evidence": [_evidence(kac)],
                }
            )
    status = _state_status(record_states)
    return (
        _check(
            "KAC-01",
            status,
            evidence=[_evidence(kac) for kac in model["kacs"]],
            parameters={"record_states": record_states},
        ),
        findings,
        limitations,
    )


def _source_values_verified(record: dict[str, Any]) -> bool:
    return (
        record.get("source_fields_verified_complete") is True
        and _precise_evidence_present(record)
    )


def _vat_check(
    context: dict[str, Any],
    *,
    trusted: bool,
) -> tuple[
    dict[str, Any],
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
]:
    vat = context.get("vat")
    if not isinstance(vat, dict):
        return _check("VAT-01", "not_applicable"), [], [], []
    if not trusted or not _source_values_verified(vat):
        limitation = {
            "code": "vat_source_values_not_verified",
            "message": "Значения для расчёта НДС переданы как контекст, но их полнота и привязка к источнику не подтверждены.",
            "impact": "Автоматический расчёт не создаёт подтверждённое замечание по исходной смете.",
            "required_input": "Подтвердить значения по исходному документу и передать точную доказательную ссылку.",
            "evidence": [_evidence(vat)],
        }
        return _check("VAT-01", "needs_input", evidence=[_evidence(vat)]), [], [], [limitation]
    base = _decimal(vat.get("base_before_exemptions"))
    rate = _decimal(vat.get("rate"))
    declared = _decimal(vat.get("declared_amount"))
    raw_exempt = vat.get("exempt_amounts", [])
    exemptions = [_decimal(value) for value in raw_exempt] if isinstance(raw_exempt, list) else []
    if base is None or rate is None or declared is None or any(value is None for value in exemptions):
        return _check("VAT-01", "needs_input"), [], [], []
    taxable = base - sum((value for value in exemptions if value is not None), Decimal(0))
    expected = taxable * rate
    amount = {
        "kind": "vat",
        "taxable_base": _decimal_text(taxable),
        "rate": _decimal_text(rate),
        "declared": _decimal_text(declared),
        "expected": _decimal_text(expected),
    }
    if declared == expected:
        return _check("VAT-01", "passed", evidence=[_evidence(vat)]), [], [amount], []
    finding = _finding(
        "VAT-01:aggregate",
        title="НДС рассчитан не по подтверждённой облагаемой базе",
        statement="Заявленный НДС не совпадает с расчётом после исключения освобождённых элементов.",
        evidence=[_evidence(vat)],
        calculation=_calculation(
            observed=declared,
            expected=expected,
            formula="(base_before_exemptions - sum(exempt_amounts)) * rate",
            difference=declared - expected,
            unit=vat.get("currency_unit"),
        ),
        impact=f"Сумма НДС отличается на {_decimal_text(declared - expected)}.",
        action="Сверить налоговый статус элементов и пересчитать НДС по совокупной облагаемой базе.",
    )
    return _check("VAT-01", "finding", evidence=[_evidence(vat)]), [finding], [amount], []


def _cost_analytics(
    model: dict[str, Any],
    context: dict[str, Any],
    *,
    unit_indicator_trusted: bool,
) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
    categories: dict[str, Decimal] = defaultdict(Decimal)
    total = Decimal(0)
    counted = 0
    counted_evidence: list[dict[str, Any]] = []
    unreliable: list[dict[str, Any]] = []
    imprecise_evidence: list[dict[str, Any]] = []
    uncategorized: list[dict[str, Any]] = []
    invalid_amounts: list[dict[str, Any]] = []
    for row in model["rows"]:
        if row.get("reliability", "reliable") != "reliable":
            unreliable.append(row)
            continue
        if not _precise_evidence_present(row):
            imprecise_evidence.append(row)
            continue
        declared = _decimal(row.get("declared_total"))
        if declared is None:
            invalid_amounts.append(row)
            continue
        category = str(row.get("category") or "").strip()
        if not category or category.casefold() in {"unclassified", "uncategorized"}:
            uncategorized.append(row)
            continue
        categories[category] += declared
        total += declared
        counted += 1
        counted_evidence.append(_evidence(row))
    result: dict[str, Any] = {
        "categories": {name: _decimal_text(value) for name, value in sorted(categories.items())},
        "total": _decimal_text(total),
        "counted_rows": counted,
        "excluded_rows": (
            len(unreliable)
            + len(imprecise_evidence)
            + len(uncategorized)
            + len(invalid_amounts)
        ),
        "unit_indicator": None,
    }
    limitations: list[dict[str, Any]] = []
    state_statuses: list[str] = ["passed"] if counted else []
    if unreliable:
        state_statuses.append("limited")
        limitations.append(
            {
                "code": "analytics_unreliable_rows_excluded",
                "message": "Строки с неполной надёжностью извлечения исключены из структуры стоимости.",
                "impact": "Категориальные итоги отражают только надёжно извлечённые строки.",
                "required_input": "Подтвердить исключённые строки по читаемому источнику.",
                "evidence": [_evidence(row) for row in unreliable],
            }
        )
    if imprecise_evidence:
        state_statuses.append("limited")
        limitations.append(
            {
                "code": "analytics_rows_without_precise_evidence",
                "message": "Строки без точной страницы, ячейки или XPath исключены из структуры стоимости.",
                "impact": "Категориальные итоги содержат только доказательно локализованные строки.",
                "required_input": "Уточнить точную позицию исключённых строк в исходных документах.",
                "evidence": [_evidence(row) for row in imprecise_evidence],
            }
        )
    if uncategorized:
        state_statuses.append("limited")
        limitations.append(
            {
                "code": "analytics_uncategorized_rows_excluded",
                "message": "Надёжные строки без подтверждённой категории исключены из структуры стоимости.",
                "impact": "Категориальные итоги не покрывают всю надёжно извлечённую стоимость.",
                "required_input": "Указать доказательно подтверждённые категории исключённых строк.",
                "evidence": [_evidence(row) for row in uncategorized],
            }
        )
    if invalid_amounts:
        state_statuses.append("needs_input")
        limitations.append(
            {
                "code": "analytics_amounts_not_available",
                "message": "Часть надёжных строк не имеет числового итога для аналитики.",
                "impact": "Структура стоимости не включает эти строки.",
                "required_input": "Предоставить надёжно извлечённые суммы исключённых строк.",
                "evidence": [_evidence(row) for row in invalid_amounts],
            }
        )
    indicator = context.get("unit_indicator")
    if isinstance(indicator, dict):
        if not unit_indicator_trusted:
            state_statuses.append("limited")
            limitations.append(
                {
                    "code": "unit_indicator_not_trusted",
                    "message": "Публично переданный знаменатель удельного показателя не является доказательным источником.",
                    "impact": "Удельный показатель не рассчитан; структура стоимости остаётся доступной в проверенном объёме.",
                    "required_input": "Передать знаменатель через доверенный источник с точной доказательной ссылкой.",
                    "evidence": [_evidence(indicator)],
                }
            )
        elif not _source_values_verified(indicator):
            state_statuses.append("needs_input")
            limitations.append(
                {
                    "code": "unit_indicator_source_not_verified",
                    "message": "Источник и полнота знаменателя удельного показателя не подтверждены.",
                    "impact": "Удельный показатель не рассчитан.",
                    "required_input": "Подтвердить значение, единицу и доказательную ссылку знаменателя.",
                    "evidence": [_evidence(indicator)],
                }
            )
        elif counted == 0:
            state_statuses.append("limited")
            limitations.append(
                {
                    "code": "unit_indicator_categorized_base_not_available",
                    "message": "Нет доказательно категоризированной стоимости для расчёта удельного показателя.",
                    "impact": "Нулевое значение не публикуется как расчётный удельный показатель.",
                    "required_input": "Сначала подтвердить категории и суммы строк, входящих в числитель.",
                    "evidence": [_evidence(indicator)],
                }
            )
        else:
            quantity = _decimal(indicator.get("quantity"))
            if quantity is None or quantity <= 0 or not str(indicator.get("name") or "").strip():
                state_statuses.append("needs_input")
                limitations.append(
                    {
                        "code": "unit_indicator_value_not_usable",
                        "message": "Доказательный знаменатель не содержит ненулевое значение и наименование единицы.",
                        "impact": "Удельный показатель не рассчитан.",
                        "required_input": "Уточнить ненулевой знаменатель и его единицу измерения.",
                        "evidence": [_evidence(indicator)],
                    }
                )
            else:
                indicator_evidence = _evidence(indicator)
                counted_evidence.append(indicator_evidence)
                result["unit_indicator"] = {
                    "name": indicator.get("name"),
                    "quantity": _decimal_text(quantity),
                    "value": _decimal_text(total / quantity),
                    "evidence": indicator_evidence,
                }
    if not model["rows"]:
        status = "not_applicable"
    elif not state_statuses:
        status = "needs_input"
    else:
        status = _state_status({"status": value} for value in state_statuses)
    return (
        result,
        _check(
            "ANALYTICS-01",
            status,
            evidence=counted_evidence,
            parameters={
                "counted_rows": counted,
                "excluded_rows": result["excluded_rows"],
            },
        ),
        limitations,
    )


ROUTES = {
    "ROUTE-PIR": ("pir", {"pir", "пир", "design", "design_work"}),
    "ROUTE-SURVEY": ("surveys", {"survey", "surveys", "изыскания", "engineering_surveys"}),
    "ROUTE-OKN": ("okn", {"okn", "окн", "heritage", "heritage_conservation"}),
    "ROUTE-DEMOLITION": ("demolition", {"demolition", "снос"}),
}


def _route_checks(
    context: dict[str, Any],
    purpose: str | None,
    verified_source_ids: set[str],
    *,
    contract_trusted: bool,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    checks: list[dict[str, Any]] = []
    findings: list[dict[str, Any]] = []
    limitations: list[dict[str, Any]] = []
    work_type = str(context.get("work_type") or "").strip().casefold()
    configs = context.get("special_routes") if isinstance(context.get("special_routes"), dict) else {}
    for control_id, (route, aliases) in ROUTES.items():
        if work_type not in aliases:
            checks.append(_check(control_id, "not_applicable"))
            continue
        config = configs.get(route)
        if not isinstance(config, dict):
            checks.append(_check(control_id, "needs_input", parameters={"required": f"special_routes.{route}"}))
            limitations.append(
                {
                    "code": "route_inputs_not_available",
                    "message": f"Для специального маршрута {route} не переданы профильные исходные данные.",
                    "impact": "Профильный маршрут не выполнен.",
                    "required_input": f"Передать состав исходных данных special_routes.{route}.",
                    "evidence": [],
                }
            )
            continue
        source_id = str(config.get("source_id") or "")
        if not source_id or source_id not in verified_source_ids:
            checks.append(_check(control_id, "limited", parameters={"reason": "currentness_not_verified"}))
            limitations.append(
                {
                    "code": "currentness_not_verified",
                    "message": f"Актуальность нормативного основания маршрута {route} не подтверждена в текущем запуске.",
                    "impact": "Категоричный нормативный вывод по маршруту не сформирован.",
                    "required_input": "Передать подтверждённый официальный источник в context.normative_sources_verified.",
                    "evidence": [],
                }
            )
            continue
        if config.get("inputs_complete") is not True:
            checks.append(
                _check(
                    control_id,
                    "needs_input",
                    evidence=[_evidence(config)],
                    parameters={"source_id": source_id, "reason": "route_inputs_incomplete"},
                )
            )
            limitations.append(
                {
                    "code": "route_inputs_incomplete",
                    "message": f"Для маршрута {route} заявлен неполный состав исходных данных.",
                    "impact": "Профильная проверка не завершена; несоответствие сметы этим фактом не доказано.",
                    "required_input": "Дополнить исходные документы специального маршрута.",
                    "evidence": [_evidence(config)],
                }
            )
            continue
        checks.append(
            _check(
                control_id,
                "limited",
                evidence=[_evidence(config)],
                parameters={"source_id": source_id, "reason": "manual_route_review_required"},
            )
        )
        limitations.append(
            {
                "code": "manual_route_review_required",
                "message": f"Для маршрута {route} ещё не реализован детерминированный профильный контроль данных.",
                "impact": "Автоматический результат не подтверждает прохождение специального маршрута.",
                "required_input": "Выполнить профильную нормативную проверку вручную по подтверждённому источнику.",
                "evidence": [_evidence(config)],
            }
        )

    normalized_purpose = str(purpose or "").casefold()
    contract = context.get("contract_change")
    contract_applicable = isinstance(contract, dict) or (
        ("contract" in normalized_purpose or "договор" in normalized_purpose)
        and ("change" in normalized_purpose or "измен" in normalized_purpose)
    )
    if not contract_applicable:
        checks.append(_check("ROUTE-CONTRACT", "not_applicable"))
    elif not isinstance(contract, dict):
        checks.append(_check("ROUTE-CONTRACT", "needs_input", parameters={"required": "context.contract_change"}))
    elif not contract_trusted or not _source_values_verified(contract):
        checks.append(
            _check(
                "ROUTE-CONTRACT",
                "needs_input",
                evidence=[_evidence(contract)],
                parameters={"reason": "contract_source_values_not_verified"},
            )
        )
        limitations.append(
            {
                "code": "contract_source_values_not_verified",
                "message": "Параметры договорных изменений не получены из доверенно привязанного источника.",
                "impact": "Автоматическая сверка не создаёт подтверждённое замечание по договорной смете.",
                "required_input": "Подтвердить значения по исходным документам и передать точную доказательную ссылку.",
                "evidence": [_evidence(contract)],
            }
        )
    else:
        original = _decimal(contract.get("original_contract_estimate_total"))
        changes = _decimal(contract.get("changes_total"))
        revised = _decimal(contract.get("revised_contract_estimate_total"))
        project = _decimal(contract.get("project_estimate_total"))
        if original is None or changes is None or revised is None or project is None:
            checks.append(_check("ROUTE-CONTRACT", "needs_input", evidence=[_evidence(contract)]))
        else:
            expected = original + changes
            contract_findings: list[dict[str, Any]] = []
            if revised != expected:
                contract_findings.append(
                    _finding(
                        "CONTRACT-01:change_reconciliation",
                        title="Изменения договорной сметы не согласованы с исходным итогом",
                        statement="Пересмотренный итог не равен исходной договорной смете с учётом изменений.",
                        evidence=[_evidence(contract)],
                        calculation=_calculation(
                            observed=revised,
                            expected=expected,
                            formula="original_contract_estimate_total + changes_total",
                            difference=revised - expected,
                            unit=contract.get("currency_unit"),
                        ),
                        impact=f"Договорный итог отличается на {_decimal_text(revised - expected)}.",
                        action="Сверить журнал изменений и пересчитать договорный итог.",
                    )
                )
            if revised > project:
                contract_findings.append(
                    _finding(
                        "CONTRACT-01:project_ceiling",
                        title="Договорная смета превышает проектную смету",
                        statement="Пересмотренный договорный итог выше представленного проектного итога.",
                        evidence=[_evidence(contract)],
                        calculation=_calculation(
                            observed=revised,
                            expected=project,
                            formula="revised_contract_estimate_total <= project_estimate_total",
                            difference=revised - project,
                            unit=contract.get("currency_unit"),
                        ),
                        impact=f"Превышение проектной сметы составляет {_decimal_text(revised - project)}.",
                        action="Уточнить основания изменения проектной сметы и договорного итога.",
                    )
                )
            findings.extend(contract_findings)
            checks.append(
                _check(
                    "ROUTE-CONTRACT",
                    "finding" if contract_findings else "passed",
                    evidence=[_evidence(contract)],
                    parameters={"scope": "estimate_structure_only_no_procurement_review"},
                )
            )
    return checks, findings, limitations


def _matrix_status_from_check(
    checks_by_id: dict[str, dict[str, Any]],
    source_id: str,
    *,
    absence_is_limited: bool = False,
) -> tuple[str, list[dict[str, Any]], dict[str, Any]]:
    source = checks_by_id.get(source_id)
    if source is None:
        return "limited", [], {"reason": "control_not_implemented"}
    status = str(source.get("status") or "limited")
    if absence_is_limited and status == "not_applicable":
        status = "limited"
    parameters = {
        "source_control": source_id,
        **(
            {"reason": "applicability_or_evidence_not_established"}
            if status == "limited" and source.get("status") == "not_applicable"
            else {}
        ),
    }
    return status, list(source.get("evidence") or []), parameters


def _append_control_matrix_inventory(
    *,
    checks: list[dict[str, Any]],
    limitations: list[dict[str, Any]],
    inventory: list[dict[str, Any]],
    context: dict[str, Any],
    purpose: str | None,
    mode: str,
) -> None:
    checks_by_id = {str(check.get("id")): check for check in checks}
    mappings: dict[str, tuple[str, bool]] = {
        "PKG-01": ("INV-01", False),
        "PKG-03": ("HIER-01", True),
        "QTY-02": ("ARITH-01", False),
        "KAC-01": ("KAC-01", True),
        "SUM-06": ("VAT-01", True),
        "ANA-01": ("DUP-01", True),
        "ANA-03": ("ANALYTICS-01", True),
        "PIR-01": ("ROUTE-PIR", False),
        "SUR-01": ("ROUTE-SURVEY", False),
        "OCH-01": ("ROUTE-OKN", False),
        "CONTRACT-01": ("ROUTE-CONTRACT", False),
    }
    work_type = str(context.get("work_type") or "").strip().casefold()
    capital_or_demolition = work_type in {
        "capital_repair",
        "капремонт",
        "капитальный ремонт",
        "demolition",
        "снос",
    }
    matrix_checks: dict[str, dict[str, Any]] = {}
    for control_id in CONTROL_MATRIX_IDS:
        if control_id == "CAP-01":
            if not capital_or_demolition:
                matrix_checks[control_id] = _check(
                    control_id,
                    "not_applicable",
                    parameters={"applicability_basis": "passport.work_type"},
                )
            elif work_type in ROUTES["ROUTE-DEMOLITION"][1]:
                status, evidence, parameters = _matrix_status_from_check(
                    checks_by_id, "ROUTE-DEMOLITION"
                )
                matrix_checks[control_id] = _check(
                    control_id, status, evidence=evidence, parameters=parameters
                )
            else:
                matrix_checks[control_id] = _check(
                    control_id,
                    "limited",
                    parameters={"reason": "capital_repair_control_not_implemented"},
                )
            continue
        mapped = mappings.get(control_id)
        if mapped is None:
            matrix_checks[control_id] = _check(
                control_id,
                "limited",
                parameters={"reason": "control_not_implemented"},
            )
            continue
        source_id, absence_is_limited = mapped
        status, evidence, parameters = _matrix_status_from_check(
            checks_by_id,
            source_id,
            absence_is_limited=absence_is_limited,
        )
        matrix_checks[control_id] = _check(
            control_id,
            status,
            evidence=evidence,
            parameters=parameters,
        )

    for control_id in CONTROL_MATRIX_IDS:
        candidate = matrix_checks[control_id]
        existing = checks_by_id.get(control_id)
        if existing is None:
            checks.append(candidate)
            checks_by_id[control_id] = candidate
        elif candidate["status"] == "limited" and existing.get("status") == "not_applicable":
            existing["status"] = "limited"
            existing.setdefault("parameters", {}).update(candidate["parameters"])

    incomplete_ids = [
        control_id
        for control_id in CONTROL_MATRIX_IDS
        if checks_by_id[control_id]["status"] in {"limited", "needs_input"}
    ]
    if not incomplete_ids:
        return
    evidence = [
        {
            "source_path": str(item.get("path") or "unknown"),
            "locator": str(item.get("path") or "unknown"),
        }
        for item in inventory[:1]
    ]
    limitations.append(
        {
            "code": (
                "light_macro_controls_incomplete"
                if mode == "light"
                else "full_control_matrix_incomplete"
            ),
            "message": (
                "Обязательные макроконтроли лёгкого режима перечислены, но часть не выполнена доказательно."
                if mode == "light"
                else "Обязательные контроли полного режима перечислены, но часть не выполнена доказательно."
            ),
            "impact": "Результат не является чистым подтверждением полного набора сметных контролей.",
            "required_input": "Выполнить перечисленные контроли по исходным документам и подтверждённым основаниям.",
            "control_ids": incomplete_ids,
            "evidence": evidence,
        }
    )


def run_domain_checks(
    model: dict[str, Any],
    *,
    inventory: list[dict[str, Any]],
    mode: str,
    purpose: str | None,
    context: dict[str, Any],
    extraction_limitations: list[dict[str, Any]],
    trusted_context_fields: set[str] | None = None,
) -> dict[str, Any]:
    trusted_fields = trusted_context_fields or set()
    checks: list[dict[str, Any]] = []
    findings: list[dict[str, Any]] = []
    limitations: list[dict[str, Any]] = []
    amounts: list[dict[str, Any]] = []

    inventory_status = "limited" if extraction_limitations else "passed" if inventory else "needs_input"
    checks.append(
        _check(
            "INV-01",
            inventory_status,
            evidence=[
                {"source_path": item.get("path", "unknown"), "locator": item.get("path", "unknown")}
                for item in inventory
            ],
            parameters={"items": len(inventory)},
        )
    )
    hierarchy_check, hierarchy_findings, hierarchy, hierarchy_limitations = _hierarchy_checks(model)
    checks.append(hierarchy_check)
    findings.extend(hierarchy_findings)
    limitations.extend(hierarchy_limitations)

    arithmetic_check, arithmetic_findings, checked_row_ids = _arithmetic_checks(model, mode)
    checks.append(arithmetic_check)
    findings.extend(arithmetic_findings)
    formula_check, formula_findings = _formula_checks(model, mode)
    checks.append(formula_check)
    findings.extend(formula_findings)
    integrity_checks, integrity_findings, integrity_limitations = _duplicate_and_field_checks(model)
    checks.extend(integrity_checks)
    findings.extend(integrity_findings)
    limitations.extend(integrity_limitations)
    component_check, component_findings, component_limitations = _component_checks(model, mode)
    checks.append(component_check)
    findings.extend(component_findings)
    limitations.extend(component_limitations)
    kac_check, kac_findings, kac_limitations = _kac_checks(model)
    checks.append(kac_check)
    findings.extend(kac_findings)
    limitations.extend(kac_limitations)
    vat_check, vat_findings, vat_amounts, vat_limitations = _vat_check(
        context,
        trusted="vat" in trusted_fields,
    )
    checks.append(vat_check)
    findings.extend(vat_findings)
    amounts.extend(vat_amounts)
    limitations.extend(vat_limitations)

    analytics, analytics_check, analytics_limitations = _cost_analytics(
        model,
        context,
        unit_indicator_trusted="unit_indicator" in trusted_fields,
    )
    checks.append(analytics_check)
    limitations.extend(analytics_limitations)

    verified_sources, verified_ids = _verified_sources(
        context,
        trusted="normative_sources_verified" in trusted_fields,
    )
    if context.get("normative_sources_verified") and "normative_sources_verified" not in trusted_fields:
        limitations.append(
            {
                "code": "public_normative_sources_not_trusted",
                "message": "Заявленные в публичном контексте нормативные источники не считаются проверенными текущим запуском.",
                "impact": "Они не используются для категоричных нормативных выводов и не публикуются как подтверждённые.",
                "required_input": "Проверить актуальность официального источника доверенным маршрутом.",
                "evidence": [],
            }
        )
    route_checks, route_findings, route_limitations = _route_checks(
        context,
        purpose,
        verified_ids,
        contract_trusted="contract_change" in trusted_fields,
    )
    checks.extend(route_checks)
    findings.extend(route_findings)
    limitations.extend(route_limitations)

    _append_control_matrix_inventory(
        checks=checks,
        limitations=limitations,
        inventory=inventory,
        context=context,
        purpose=purpose,
        mode=mode,
    )

    used_source_ids = {source_id for finding in findings for source_id in finding["source_ids"]}
    normative_sources = list(verified_sources)
    if INTERNAL_SOURCE_ID in used_source_ids:
        normative_sources.insert(0, dict(INTERNAL_SOURCE))
    source_by_id = {str(source["id"]): source for source in normative_sources}
    retained_findings: list[dict[str, Any]] = []
    for finding in findings:
        ordered_source_ids = list(dict.fromkeys(map(str, finding.get("source_ids", []))))
        missing_source_ids = [
            source_id for source_id in ordered_source_ids if source_id not in source_by_id
        ]
        if missing_source_ids:
            limitations.append(
                {
                    "code": "finding_source_citation_unverified",
                    "message": "Замечание не опубликовано как finding: доказательное основание не прошло проверку.",
                    "impact": "Категоричный вывод без проверенной ссылки исключён из результата.",
                    "required_input": "Подтвердить редакцию, точный пункт и официальный URL основания.",
                    "finding_id": finding.get("id"),
                    "source_ids": missing_source_ids,
                    "evidence": list(finding.get("evidence") or []),
                }
            )
            control_id = str(finding.get("id") or "").split(":", 1)[0]
            for check in checks:
                if check.get("id") == control_id and check.get("status") == "finding":
                    check["status"] = "limited"
                    check.setdefault("parameters", {})["reason"] = (
                        "finding_source_citation_unverified"
                    )
            continue
        finding["source_ids"] = ordered_source_ids
        finding["source_citations"] = [
            {
                "source_id": source_id,
                "edition": source_by_id[source_id]["edition"],
                "pinpoint": source_by_id[source_id]["pinpoint"],
                "official_url": source_by_id[source_id]["official_url"],
            }
            for source_id in ordered_source_ids
        ]
        retained_findings.append(finding)
    findings = retained_findings

    checkable_rows = [row for row in model["rows"] if row.get("reliability", "reliable") == "reliable"]
    return {
        "checks": checks,
        "findings": findings,
        "limitations": limitations,
        "normative_sources": normative_sources,
        "estimate_hierarchy": hierarchy,
        "amounts": amounts,
        "cost_analytics": analytics,
        "checkable_row_ids": [str(row.get("row_id") or index) for index, row in enumerate(checkable_rows, 1)],
        "checked_row_ids": sorted(checked_row_ids),
    }
