from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
REFERENCES = ROOT / "skills" / "smetchik" / "references"


EXPECTED_GGE_ITEMS = {
    *(f"GGE-1.{item}" for item in range(1, 6)),
    *(f"GGE-2.{item}" for item in range(1, 10)),
    *(f"GGE-3.{item}" for item in range(1, 29)),
    *(f"GGE-4.{item}" for item in range(1, 6)),
    *(f"GGE-5.{item}" for item in range(1, 4)),
    *(f"GGE-6.{item}" for item in range(1, 6)),
}


def _table_rows(text: str, prefix: str) -> dict[str, list[str]]:
    rows: dict[str, list[str]] = {}
    for line in text.splitlines():
        if not line.startswith(f"| `{prefix}"):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        item_id = cells[0].strip("`")
        assert item_id not in rows, f"duplicate traceability ID: {item_id}"
        rows[item_id] = cells
    return rows


def test_gge_checklist_has_item_level_traceability_for_all_six_sections() -> None:
    text = (REFERENCES / "gge-checklist-2025-04-30.md").read_text(
        encoding="utf-8"
    )
    rows = _table_rows(text, "GGE-")

    assert set(rows) == EXPECTED_GGE_ITEMS
    assert all(len(cells) == 8 for cells in rows.values())
    for item_id, cells in rows.items():
        page, thesis, source_class, signal, basis, source, limitation = cells[1:]
        assert re.fullmatch(r"(?:[1-9]|1[0-9]|2[0-6])(?:[-–](?:[1-9]|1[0-9]|2[0-6]))?", page), item_id
        assert thesis and signal and basis and source and limitation, item_id
        assert source_class == "`methodical`", item_id
        assert "GGE-CL-2025" in source, item_id


def test_gge_percentages_are_diagnostic_only_and_never_sampling_thresholds() -> None:
    text = (REFERENCES / "gge-checklist-2025-04-30.md").read_text(
        encoding="utf-8"
    )
    rows = _table_rows(text, "GGE-")
    percent_rows = [cells for cells in rows.values() if "%" in " | ".join(cells)]

    assert percent_rows
    assert all("diagnostic_only" in " | ".join(cells) for cells in percent_rows)
    assert "не порог нарушения" in text
    assert "не процентная выборка" in text


def test_normative_registry_contains_live_routing_for_checklist_sources() -> None:
    text = (REFERENCES / "normative-registry.md").read_text(encoding="utf-8")

    assert "smetchik.normative.ru.2026-08-26.v2" in text
    assert "`verified_live`" in text
    assert "`currentness_not_verified`" in text
    for source_id in (
        "M-125",
        "M-783",
        "LAW-881",
        "LAW-384",
        "LAW-191",
        "LAW-116",
        "LAW-NK149",
        "DATA-KSR",
        "DATA-NCS",
    ):
        assert f"`{source_id}`" in text
    assert "https://minjust.consultant.ru/documents/39340" in text
    assert "https://minjust.consultant.ru/documents/36522" in text


def test_acceptance_criteria_contains_every_release_gate() -> None:
    text = (REFERENCES / "acceptance-criteria.md").read_text(encoding="utf-8")
    gate_ids = {
        "GATE-AUTOMATED",
        "GATE-VALIDATORS",
        "GATE-SCHEMAS",
        "GATE-XSD",
        "GATE-RUNTIME",
        "GATE-INSTALLED",
        "GATE-FRESH-EXPLICIT",
        "GATE-FRESH-IMPLICIT",
        "GATE-REAL-RUN",
        "GATE-MANUAL",
        "GATE-NETWORK",
        "GATE-READONLY",
        "GATE-NO-OVERWRITE",
    }

    assert all(text.count(f"`{gate_id}`") == 1 for gate_id in gate_ids)
    assert "полный `pytest`" in text
    assert "новой задаче" in text
    assert "реальном или обезличенном" in text
    assert "вне репозитория" in text
    assert "ручная приёмка" in text.lower()
    assert "--ref v0.2.0" in text
    assert "не подменяются синтетическими тестами" in text
