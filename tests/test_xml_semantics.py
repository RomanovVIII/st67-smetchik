from __future__ import annotations

import json
import zipfile
from pathlib import Path
from typing import Any

import pytest
import xmlschema
from lxml import etree

from smetchik_engine import inspect_input
from xml_extract import extract_xml, load_default_registry


_DEFAULT_SCHEMA_REGISTRY, _DEFAULT_REGIONAL_ADAPTERS = load_default_registry()
pytestmark = pytest.mark.skipif(
    not all(Path(entry["xsd_path"]).is_file() for entry in _DEFAULT_SCHEMA_REGISTRY),
    reason="requires official schemas installed by runtime/schema_manager.py fetch --all",
)


GRAND_FIXTURES = {
    "grandsmeta.local_estimate.2026_1_1": (
        '<Document Generator="test" ProgramVersion="2026.1.1" '
        'DocumentType="{2B0470FD-477C-4359-9F34-EEBE36B7D340}">'
        '<Chapters><Chapter Caption="Section" SysID="1">'
        '<Position Caption="Work" Code="FER-01" Units="m2" Quantity="2" '
        'SysID="2" PriceLevel="Curr"><Quantity Fx="1+1" Result="2"/>'
        '<PriceCurr PZ="100"/><Itog><ItogRes Total="200"/></Itog>'
        '</Position></Chapter></Chapters></Document>',
        {"document", "estimate_row", "formula", "total"},
    ),
    "grandsmeta.object_or_summary_estimate.2026_1_1": (
        '<Document Generator="test" ProgramVersion="2026.1.1" '
        'DocumentType="{2B0470FD-477C-4359-9F34-EEBE36B7D345}">'
        '<Indexes><Index Caption="Quarter index" Code="IDX-1" Sroy="1.1"/></Indexes>'
        '<Chapters><Chapter Caption="Chapter"><Position Caption="Estimate" '
        'Obosn="LS-01" Units="m2" UnitsQty="2" EdPr="100" K="1" '
        'Pz="10" SysID="1"><Total Total="200"/>'
        '</Position></Chapter></Chapters></Document>',
        {"document", "index", "estimate_row", "total"},
    ),
    "grandsmeta.contract_estimate.2026_1_1": (
        '<Document Generator="test" ProgramVersion="2026.1.1" '
        'DocumentType="{2B0470FD-477C-4359-9F34-EEBE36B7D353}">'
        '<Chapters><Chapter Caption="Chapter"><Position Caption="Work" Units="m2" SysID="1">'
        '<Quantity Fx="1+1" Result="2"/><Itog><Total Total="10"/></Itog>'
        '</Position></Chapter></Chapters><Itog Total="10"/></Document>',
        {"document", "estimate_row", "formula", "total"},
    ),
    "grandsmeta.market_analysis.2026_1_1": (
        '<Document Generator="test" ProgramVersion="2026.1.1" '
        'DocumentType="{2B0470FD-477C-4359-9F34-EEBE36B7D354}">'
        '<Chapters><Chapter Caption="Chapter"><Position Caption="Resource" Units="pcs" SysID="1">'
        '<Items><Item Caption="Material" Obosn="KSR-01" Units="pcs" '
        'OptPrice="100" IzmRatio="1" VAT="20" '
        'Supplier="1" SupplierKind="Supplier"/></Items>'
        '</Position></Chapter></Chapters></Document>',
        {"document", "estimate_row", "KAC"},
    ),
    "grandsmeta.quantity_takeoff.2026_1_1": (
        '<Document Generator="test" ProgramVersion="2026.1.1" '
        'DocumentType="{2B0470FD-477C-4359-9F34-EEBE36B7D350}"><Parameters/>'
        '<Chapters><Chapter Caption="Chapter"><Position Caption="Work" Units="m2" SysID="1">'
        '<Quantity Fx="1+1" Result="2"/></Position></Chapter></Chapters></Document>',
        {"document", "estimate_row", "formula"},
    ),
}


PASSPORT = {
    "object": "Учебный объект",
    "work_type": "construction",
    "funding_source": "budget",
    "region_or_price_zone": "67",
    "price_level_date": "2026-08-01",
    "calculation_method": "resource_index",
    "stage": "project_documentation",
    "document_set": ["LSR", "OSR", "SSR", "KAC"],
}


MINSTROY_EXPECTED_TYPES = {
    "minstroy.local_estimate_bim.3_01": {"document", "estimate", "estimate_row"},
    "minstroy.local_estimate_rim.3_01": {"document", "estimate", "estimate_row", "total"},
    "minstroy.object_estimate.3_01": {"document", "estimate", "estimate_row", "total"},
    "minstroy.summary_estimate.3_01": {"document", "total"},
    "minstroy.costs_summary.3_01": {"document", "estimate", "total"},
    "minstroy.quantity_takeoff.3_01": {"document", "estimate_row", "formula"},
    "minstroy.market_analysis.4_02": {"document", "KAC"},
    "minstroy.explanatory_note_estimate.4_01": {"document", "estimate"},
}


SAMPLE_VALUES = (
    "1",
    "2026",
    "67",
    "01",
    "1.2.3.4",
    "2026-01-01",
    "2026-01-01T00:00:00",
    "false",
    "x",
    "ФЕР",
    "КА",
    "материал",
    "https://example.invalid/document.pdf",
    "00000000-0000-0000-0000-000000000001",
    "{00000000-0000-0000-0000-000000000001}",
    "00000000000000000000000000000000",
    "1234567890",
    "12345678901",
    "AA==",
    "00",
)


def _sample_value(xsd_type: Any, local_name: str) -> str:
    enumeration = getattr(xsd_type, "enumeration", None)
    if enumeration:
        for value in enumeration:
            if value is not None:
                return str(value)
    lowered = local_name.lower()
    preferred: list[str] = []
    if "guid" in lowered or "uid" in lowered:
        preferred.extend(
            [
                "00000000-0000-0000-0000-000000000001",
                "{00000000-0000-0000-0000-000000000001}",
            ]
        )
    if "checksum" in lowered:
        preferred.append("00000000000000000000000000000000")
    if "region" in lowered:
        preferred.append("67")
    if "class" in lowered:
        preferred.append("1.2.3.4")
    if "prefix" in lowered:
        preferred.append("ФЕР")
    if "year" in lowered:
        preferred.append("2026")
    if "type" in lowered:
        preferred.extend(["КА", "материал"])
    for candidate in (*preferred, *SAMPLE_VALUES):
        try:
            if xsd_type.is_valid(candidate):
                return candidate
        except Exception:
            continue
    raise AssertionError(f"No safe sample for {local_name}: {xsd_type!r}")


def _emit_group(parent: etree._Element, group: Any, depth: int = 0) -> None:
    assert depth < 100
    model = getattr(group, "model", None)
    if model is None:
        return
    particles = list(group)
    if model == "choice":
        candidate = next((particle for particle in particles if particle.max_occurs != 0), None)
        if candidate is not None:
            _emit_particle(parent, candidate, force=True, depth=depth + 1)
        return
    for particle in particles:
        if particle.min_occurs:
            _emit_particle(parent, particle, force=False, depth=depth + 1)


def _emit_particle(
    parent: etree._Element,
    particle: Any,
    *,
    force: bool,
    depth: int,
) -> None:
    if getattr(particle, "model", None) is not None and not hasattr(particle, "type"):
        for _ in range(1 if force else particle.min_occurs):
            _emit_group(parent, particle, depth + 1)
        return
    for _ in range(1 if force else particle.min_occurs):
        element = etree.SubElement(parent, particle.local_name)
        xsd_type = particle.type
        if xsd_type.is_simple():
            element.text = _sample_value(xsd_type, particle.local_name)
            continue
        for attribute in xsd_type.attributes.values():
            if attribute.use == "required":
                element.set(
                    attribute.local_name,
                    _sample_value(attribute.type, attribute.local_name),
                )
        if xsd_type.has_simple_content():
            element.text = _sample_value(xsd_type.content, particle.local_name)
        else:
            _emit_group(element, xsd_type.content, depth + 1)


def _minimal_minstroy_document(entry: dict[str, Any]) -> etree._Element:
    schema = xmlschema.XMLSchema11(
        entry["xsd_path"],
        allow="local",
        defuse="always",
        timeout=5,
        use_fallback=False,
    )
    root_declaration = schema.elements[entry["root"]]
    root = etree.Element(root_declaration.local_name)
    _emit_group(root, root_declaration.type.content)
    if entry["id"] == "minstroy.market_analysis.4_02":
        sections = root.find("Sections")
        assert sections is not None
        sections_declaration = next(
            element
            for element in root_declaration.type.content.iter_elements()
            if element.local_name == "Sections"
        )
        section_declaration = next(
            element
            for element in sections_declaration.type.content.iter_elements()
            if element.local_name == "Section"
        )
        _emit_particle(sections, section_declaration, force=True, depth=0)
    assert schema.is_valid(root)
    return root


def _assert_semantic_contract(
    source: Path,
    details: dict[str, Any],
    adapter_id: str,
    expected_types: set[str],
    records: int,
) -> None:
    semantic_records = details["semantic_records"]
    assert details["semantic_interpretation_performed"] is True
    assert records == len(semantic_records) > 0
    assert expected_types <= {record["record_type"] for record in semantic_records}
    for record in semantic_records:
        assert record["schema_id"] == adapter_id
        assert record["fields"]
        assert record["evidence"]["source_path"] == str(source)
        assert record["evidence"]["xpath"].startswith("/")
        assert isinstance(record["evidence"]["line"], int)
        assert record["evidence"]["locator"]
        for field in record["fields"]:
            assert "raw_value" in field
            assert field["evidence"]["source_path"] == str(source)


@pytest.mark.parametrize("adapter_id", sorted(GRAND_FIXTURES))
def test_each_grandsmeta_adapter_emits_contextual_semantic_records(
    tmp_path: Path,
    adapter_id: str,
) -> None:
    xml, expected_types = GRAND_FIXTURES[adapter_id]
    source = tmp_path / f"{adapter_id}.xml"
    source.write_text(xml, encoding="utf-8")

    details, status, limitations, records = extract_xml(source)

    assert status == "reliable"
    assert limitations == []
    assert details["schema"]["id"] == adapter_id
    _assert_semantic_contract(source, details, adapter_id, expected_types, records)


@pytest.mark.parametrize("adapter_id", sorted(MINSTROY_EXPECTED_TYPES))
def test_each_minstroy_adapter_emits_contextual_semantic_records(
    tmp_path: Path,
    adapter_id: str,
) -> None:
    registry, _regional = load_default_registry()
    entry = next(item for item in registry if item["id"] == adapter_id)
    source = tmp_path / f"{adapter_id}.xml"
    source.write_bytes(
        etree.tostring(
            _minimal_minstroy_document(entry),
            xml_declaration=True,
            encoding="UTF-8",
        )
    )

    details, status, limitations, records = extract_xml(source)

    assert status == "reliable"
    assert limitations == []
    assert details["schema"]["id"] == adapter_id
    _assert_semantic_contract(
        source,
        details,
        adapter_id,
        MINSTROY_EXPECTED_TYPES[adapter_id],
        records,
    )


def _mapped_minstroy_bim(entry: dict[str, Any]) -> etree._Element:
    root = _minimal_minstroy_document(entry)
    cost = root.find("./Object/Estimate/Sections/Section/Items/Item/Cost")
    assert cost is not None
    replacements = {
        "Code": "FER-01",
        "Name": "Concrete work",
        "Quantity": "2",
        "QuantityTotal": "2",
        "Unit": "m2",
    }
    for child_name, value in replacements.items():
        child = cost.find(child_name)
        assert child is not None
        child.text = value
    for container_name, value in (("PerUnit", "100"), ("Totals", "200")):
        container = cost.find(container_name)
        assert container is not None
        current = etree.SubElement(container, "Current")
        etree.SubElement(current, "Direct").text = value

    schema = xmlschema.XMLSchema11(
        entry["xsd_path"],
        allow="local",
        defuse="always",
        timeout=5,
        use_fallback=False,
    )
    assert schema.is_valid(root)
    return root


def _mapped_record(details: dict[str, Any], record_type: str) -> dict[str, Any]:
    return next(
        record
        for record in details["semantic_records"]
        if record["record_type"] == record_type
    )


def test_grand_local_promotes_only_xsd_mapped_row_fields(tmp_path: Path) -> None:
    xml, _types = GRAND_FIXTURES["grandsmeta.local_estimate.2026_1_1"]
    source = tmp_path / "grand-local.xml"
    source.write_text(xml, encoding="utf-8")

    details, status, limitations, _records = extract_xml(source)

    assert status == "reliable"
    assert limitations == []
    row = _mapped_record(details, "estimate_row")
    assert {
        field: row[field]
        for field in ("name", "code", "unit", "quantity", "unit_price", "declared_total")
    } == {
        "name": "Work",
        "code": "FER-01",
        "unit": "m2",
        "quantity": 2,
        "unit_price": 100,
        "declared_total": 200,
    }
    mapped_paths = {field["xpath"] for field in row["fields"]}
    assert row["xpath"] + "/@Code" in mapped_paths
    assert row["xpath"] + "/PriceCurr/@PZ" in mapped_paths
    assert row["xpath"] + "/Itog/ItogRes/@Total" in mapped_paths


def test_grand_os_promotes_row_index_and_coefficient_from_exact_paths(tmp_path: Path) -> None:
    xml, _types = GRAND_FIXTURES["grandsmeta.object_or_summary_estimate.2026_1_1"]
    source = tmp_path / "grand-os.xml"
    source.write_text(xml, encoding="utf-8")

    details, status, limitations, _records = extract_xml(source)

    assert status == "reliable"
    assert limitations == []
    row = _mapped_record(details, "estimate_row")
    assert {
        field: row[field]
        for field in (
            "name",
            "code",
            "unit",
            "quantity",
            "unit_price",
            "declared_total",
            "coefficient",
        )
    } == {
        "name": "Estimate",
        "code": "LS-01",
        "unit": "m2",
        "quantity": 2,
        "unit_price": 100,
        "declared_total": 200,
        "coefficient": 1,
    }
    index = _mapped_record(details, "index")
    assert index["name"] == "Quarter index"
    assert index["code"] == "IDX-1"
    assert index["index"] == 1.1
    assert index["value"] == 1.1


def test_grand_kac_promotes_price_without_inventing_a_total(tmp_path: Path) -> None:
    xml, _types = GRAND_FIXTURES["grandsmeta.market_analysis.2026_1_1"]
    source = tmp_path / "grand-kac.xml"
    source.write_text(xml, encoding="utf-8")

    details, status, limitations, _records = extract_xml(source)

    assert status == "reliable"
    assert limitations == []
    kac = _mapped_record(details, "KAC")
    assert {
        field: kac[field]
        for field in ("name", "code", "unit", "unit_price", "coefficient")
    } == {
        "name": "Material",
        "code": "KSR-01",
        "unit": "pcs",
        "unit_price": 100,
        "coefficient": 1,
    }
    assert "declared_total" not in kac


def test_minstroy_bim_promotes_documented_cost_fields(tmp_path: Path) -> None:
    registry, _regional = load_default_registry()
    entry = next(
        item for item in registry if item["id"] == "minstroy.local_estimate_bim.3_01"
    )
    source = tmp_path / "minstroy-bim.xml"
    source.write_bytes(
        etree.tostring(
            _mapped_minstroy_bim(entry),
            xml_declaration=True,
            encoding="UTF-8",
        )
    )

    details, status, limitations, _records = extract_xml(source)

    assert status == "reliable"
    assert limitations == []
    row = _mapped_record(details, "estimate_row")
    assert {
        field: row[field]
        for field in ("name", "code", "unit", "quantity", "unit_price", "declared_total")
    } == {
        "name": "Concrete work",
        "code": "FER-01",
        "unit": "m2",
        "quantity": 2,
        "unit_price": 100,
        "declared_total": 200,
    }


@pytest.mark.parametrize(
    ("adapter_id", "expected_row_status"),
    [
        ("grandsmeta.local_estimate.2026_1_1", "limited"),
        ("grandsmeta.object_or_summary_estimate.2026_1_1", "limited"),
    ],
)
def test_inspect_input_checks_grand_mapped_rows_end_to_end(
    tmp_path: Path,
    adapter_id: str,
    expected_row_status: str,
) -> None:
    xml, _types = GRAND_FIXTURES[adapter_id]
    source = tmp_path / f"{adapter_id}.xml"
    source.write_text(xml, encoding="utf-8")

    result = inspect_input(
        source,
        mode="full",
        purpose="internal_review",
        context=dict(PASSPORT),
    )

    assert result["coverage"]["extracted_records"] == 1
    assert result["coverage"]["checkable_records"] == 1
    assert result["coverage"]["arithmetic_checked_records"] == 0
    arithmetic = next(check for check in result["checks"] if check["id"] == "ARITH-01")
    assert arithmetic["status"] == "limited"
    assert [state["status"] for state in arithmetic["parameters"]["row_states"]] == [
        expected_row_status
    ]
    assert [state["reason"] for state in arithmetic["parameters"]["row_states"]] == [
        "calculation_basis_not_verified"
    ]
    assert not any(finding["id"].startswith("ARITH-01") for finding in result["findings"])


def test_inspect_input_consumes_grand_kac_semantic_entity(tmp_path: Path) -> None:
    xml, _types = GRAND_FIXTURES["grandsmeta.market_analysis.2026_1_1"]
    source = tmp_path / "grand-kac.xml"
    source.write_text(xml, encoding="utf-8")

    result = inspect_input(
        source,
        mode="full",
        purpose="internal_review",
        context=dict(PASSPORT),
    )

    assert result["coverage"]["extracted_entities"] >= 3
    kac_check = next(check for check in result["checks"] if check["id"] == "KAC-01")
    assert kac_check["status"] == "needs_input"
    assert any(
        evidence.get("xpath", "").endswith("/Items/Item")
        for evidence in kac_check["evidence"]
    )


def test_inspect_input_checks_minstroy_mapped_row_end_to_end(tmp_path: Path) -> None:
    registry, _regional = load_default_registry()
    entry = next(
        item for item in registry if item["id"] == "minstroy.local_estimate_bim.3_01"
    )
    source = tmp_path / "minstroy-bim.xml"
    source.write_bytes(
        etree.tostring(
            _mapped_minstroy_bim(entry),
            xml_declaration=True,
            encoding="UTF-8",
        )
    )

    result = inspect_input(
        source,
        mode="full",
        purpose="internal_review",
        context=dict(PASSPORT),
    )

    assert result["coverage"]["extracted_records"] == 1
    assert result["coverage"]["checkable_records"] == 1
    assert result["coverage"]["arithmetic_checked_records"] == 0
    arithmetic = next(check for check in result["checks"] if check["id"] == "ARITH-01")
    assert arithmetic["status"] == "limited"
    assert [state["status"] for state in arithmetic["parameters"]["row_states"]] == [
        "limited"
    ]
    assert [state["reason"] for state in arithmetic["parameters"]["row_states"]] == [
        "calculation_basis_not_verified"
    ]
    assert not any(finding["id"].startswith("ARITH-01") for finding in result["findings"])


@pytest.mark.parametrize(
    "xml",
    [
        "<Vendor><Name>Fake</Name><Quantity>2</Quantity><Price>100</Price><Total>200</Total></Vendor>",
        "<Construction><Name>Fake</Name><Quantity>2</Quantity><Price>100</Price><Total>200</Total></Construction>",
    ],
)
def test_inspect_input_never_guesses_rows_for_unknown_or_invalid_xml(
    tmp_path: Path,
    xml: str,
) -> None:
    source = tmp_path / "untrusted.xml"
    source.write_text(xml, encoding="utf-8")

    result = inspect_input(
        source,
        mode="full",
        purpose="internal_review",
        context=dict(PASSPORT),
    )

    assert result["coverage"]["extracted_records"] == 0
    assert result["coverage"]["checkable_records"] == 0
    assert result["coverage"]["checked_records"] == 0


def _assert_xml_result_does_not_leak_private_paths(
    result: dict[str, Any],
    private_path: Path,
    logical_source: str,
) -> None:
    serialized = json.dumps(result, ensure_ascii=False)
    assert str(private_path) not in serialized
    assert "smetchik-zip-" not in serialized
    arithmetic = next(check for check in result["checks"] if check["id"] == "ARITH-01")
    row_id = arithmetic["parameters"]["row_states"][0]["row_id"]
    assert row_id.startswith(
        f"{logical_source}:grandsmeta.local_estimate.2026_1_1:"
    )
    assert ":/Document/Chapters/Chapter/Position" in row_id
    check_evidence = [
        evidence
        for check in result["checks"]
        for evidence in check.get("evidence", [])
    ]
    assert any(
        evidence.get("xpath") == "/Document/Chapters/Chapter/Position"
        and isinstance(evidence.get("line"), int)
        for evidence in check_evidence
    )


def test_inspect_input_xml_ids_do_not_leak_top_level_source_path(tmp_path: Path) -> None:
    xml, _types = GRAND_FIXTURES["grandsmeta.local_estimate.2026_1_1"]
    source = tmp_path / "private-estimate.xml"
    source.write_text(xml, encoding="utf-8")

    result = inspect_input(
        source,
        mode="full",
        purpose="internal_review",
        context=dict(PASSPORT),
    )

    _assert_xml_result_does_not_leak_private_paths(
        result,
        tmp_path,
        "private-estimate.xml",
    )


def test_inspect_input_xml_ids_do_not_leak_nested_zip_temp_path(tmp_path: Path) -> None:
    xml, _types = GRAND_FIXTURES["grandsmeta.local_estimate.2026_1_1"]
    source = tmp_path / "package.zip"
    with zipfile.ZipFile(source, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("nested/private-estimate.xml", xml)

    result = inspect_input(
        source,
        mode="full",
        purpose="internal_review",
        context=dict(PASSPORT),
    )

    _assert_xml_result_does_not_leak_private_paths(
        result,
        tmp_path,
        "package.zip!/nested/private-estimate.xml",
    )
    assert any(
        evidence.get("source_path") == "package.zip!/nested/private-estimate.xml"
        for check in result["checks"]
        for evidence in check.get("evidence", [])
    )


def test_identical_xml_files_keep_distinct_logical_row_ids_in_one_zip(tmp_path: Path) -> None:
    xml, _types = GRAND_FIXTURES["grandsmeta.local_estimate.2026_1_1"]
    source = tmp_path / "twins.zip"
    with zipfile.ZipFile(source, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("a.xml", xml)
        archive.writestr("b.xml", xml)

    result = inspect_input(
        source,
        mode="full",
        purpose="internal_review",
        context=dict(PASSPORT),
    )

    assert result["coverage"]["checkable_records"] == 2
    assert result["coverage"]["arithmetic_checked_records"] == 0
    arithmetic = next(check for check in result["checks"] if check["id"] == "ARITH-01")
    assert arithmetic["status"] == "limited"
    assert {state["status"] for state in arithmetic["parameters"]["row_states"]} == {
        "limited"
    }
    assert {state["reason"] for state in arithmetic["parameters"]["row_states"]} == {
        "calculation_basis_not_verified"
    }
    row_ids = {state["row_id"] for state in arithmetic["parameters"]["row_states"]}
    assert len(row_ids) == 2
    assert {
        row_id.split(":grandsmeta.local_estimate.2026_1_1:", 1)[0]
        for row_id in row_ids
    } == {"twins.zip!/a.xml", "twins.zip!/b.xml"}
    assert not any(finding["id"].startswith("ARITH-01") for finding in result["findings"])
