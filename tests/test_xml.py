from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from smetchik_engine import inspect_input
from xml_extract import extract_xml, load_default_registry


XSD_11 = """<?xml version="1.0" encoding="UTF-8"?>
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema" version="1.1">
  <xs:element name="Estimate">
    <xs:complexType>
      <xs:sequence>
        <xs:element name="total" type="xs:decimal"/>
      </xs:sequence>
      <xs:attribute name="version" type="xs:string" use="required" fixed="1.0"/>
      <xs:assert test="total ge 0"/>
    </xs:complexType>
  </xs:element>
</xs:schema>
"""


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def registry(xsd: Path) -> dict:
    return {
        "xml_schema_registry": [
            {
                "id": "synthetic-estimate-1",
                "root": "Estimate",
                "namespace": "",
                "version": "1.0",
                "version_attribute": "version",
                "xsd_path": str(xsd),
                "xsd_version": "1.1",
            }
        ]
    }


def test_known_local_xsd11_validates_without_loading_schema_location(
    tmp_path: Path,
    monkeypatch,
) -> None:
    xsd = tmp_path / "estimate.xsd"
    xsd.write_text(XSD_11, encoding="utf-8")
    source = tmp_path / "estimate.xml"
    source.write_text(
        """<Estimate xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
        xsi:noNamespaceSchemaLocation="https://invalid.example/remote.xsd"
        version="1.0"><total>10.50</total></Estimate>""",
        encoding="utf-8",
    )
    before = file_hash(source)

    def network_forbidden(*_args, **_kwargs):
        raise AssertionError("network access is forbidden")

    monkeypatch.setattr("socket.create_connection", network_forbidden)

    details, status, limitations, records = extract_xml(
        source,
        registry=registry(xsd)["xml_schema_registry"],
    )
    assert details["schema"]["id"] == "synthetic-estimate-1"
    assert details["schema_validation"]["status"] == "valid"
    assert details["schema_validation"]["xsd_version"] == "1.1"
    total = next(node for node in details["structure"] if node["local_name"] == "total")
    assert total["evidence"]["xpath"] == "/Estimate/total"
    assert isinstance(total["evidence"]["line"], int)
    assert total["evidence"]["source_path"] == str(source)
    assert total["evidence"]["locator"].endswith("xpath:/Estimate/total;line:3")
    assert details["semantic_interpretation_performed"] is False
    assert "semantic_values" not in details
    assert status == "partial"
    assert records == 0
    assert any(limit["code"] == "semantic_adapter_unavailable" for limit in limitations)
    assert file_hash(source) == before


def test_xsd11_assertion_failure_is_reported_as_invalid(tmp_path: Path) -> None:
    xsd = tmp_path / "estimate.xsd"
    xsd.write_text(XSD_11, encoding="utf-8")
    source = tmp_path / "invalid.xml"
    source.write_text("<Estimate version=\"1.0\"><total>-1</total></Estimate>", encoding="utf-8")

    details, status, limitations, records = extract_xml(
        source,
        registry=registry(xsd)["xml_schema_registry"],
    )
    assert details["schema_validation"]["status"] == "invalid"
    assert details["semantic_interpretation_performed"] is False
    assert "semantic_values" not in details
    assert details["schema_validation"]["error_count"] >= 1
    assert any(limit["code"] == "xml_schema_invalid" for limit in limitations)
    assert status == "partial"
    assert records == 0


def test_wrong_known_schema_version_is_not_semantically_interpreted(tmp_path: Path) -> None:
    xsd = tmp_path / "estimate.xsd"
    xsd.write_text(XSD_11, encoding="utf-8")
    source = tmp_path / "wrong-version.xml"
    source.write_text("<Estimate version=\"2.0\"><total>99</total></Estimate>", encoding="utf-8")

    details, status, limitations, records = extract_xml(
        source,
        registry=registry(xsd)["xml_schema_registry"],
    )
    assert details["schema_validation"]["status"] == "not_performed"
    assert details["semantic_interpretation_performed"] is False
    assert any(limit["code"] == "unsupported_schema_version" for limit in limitations)
    assert status == "partial"
    assert records == 0


def test_valid_unregistered_schema_does_not_guess_date_semantics(tmp_path: Path) -> None:
    xsd = tmp_path / "dated-estimate.xsd"
    xsd.write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema" version="1.1">
  <xs:element name="Estimate">
    <xs:complexType><xs:sequence>
      <xs:element name="issueDate" type="xs:date"/>
    </xs:sequence><xs:attribute name="version" fixed="1.0" use="required"/></xs:complexType>
  </xs:element>
</xs:schema>""",
        encoding="utf-8",
    )
    source = tmp_path / "dated.xml"
    source.write_text(
        '<Estimate version="1.0"><issueDate>2026-08-26</issueDate></Estimate>',
        encoding="utf-8",
    )

    details, status, limitations, records = extract_xml(
        source,
        registry=registry(xsd)["xml_schema_registry"],
    )

    assert details["schema_validation"]["status"] == "valid"
    assert details["semantic_interpretation_performed"] is False
    assert "semantic_values" not in details
    assert status == "partial"
    assert records == 0
    assert any(limit["code"] == "semantic_adapter_unavailable" for limit in limitations)


def test_ambiguous_valid_schemas_do_not_expose_values(tmp_path: Path) -> None:
    xsd = tmp_path / "estimate.xsd"
    xsd.write_text(XSD_11, encoding="utf-8")
    source = tmp_path / "ambiguous.xml"
    source.write_text('<Estimate version="1.0"><total>10.50</total></Estimate>', encoding="utf-8")
    entries = registry(xsd)["xml_schema_registry"]
    second = dict(entries[0], id="synthetic-estimate-2")

    details, status, limitations, records = extract_xml(
        source,
        registry=[entries[0], second],
    )
    assert details["schema_validation"]["status"] == "ambiguous"
    assert details["semantic_interpretation_performed"] is False
    assert "semantic_values" not in details
    assert "10.50" not in json.dumps(details, ensure_ascii=False)
    assert status == "partial"
    assert any(limit["code"] == "ambiguous_local_schema" for limit in limitations)
    assert records == 0


def test_unknown_schema_gets_structure_only_without_money_or_arithmetic(tmp_path: Path) -> None:
    source = tmp_path / "proprietary.xml"
    source.write_text(
        "<VendorEstimate><Total>999</Total><Rate>1</Rate></VendorEstimate>",
        encoding="utf-8",
    )

    result = inspect_input(source, mode="full")

    details = result["input_inventory"][0]["details"]
    assert details["schema_validation"]["status"] == "unsupported_schema"
    assert details["semantic_interpretation_performed"] is False
    assert details["arithmetic_performed"] is False
    assert result["amounts"] == []
    assert result["findings"] == []
    assert any(limit["code"] == "unsupported_schema" for limit in result["limitations"])
    assert "999" not in json.dumps(details, ensure_ascii=False)
    _details, _status, _limitations, records = extract_xml(source)
    assert records == 0


def test_known_schema_without_installed_xsd_is_offline_structure_only(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = tmp_path / "estimate.xml"
    source.write_text("<Estimate><total>10.50</total></Estimate>", encoding="utf-8")
    missing_xsd = tmp_path / "schemas" / "official" / "estimate.xsd"
    known_registry = [
        {
            "id": "official-estimate-1",
            "root": "Estimate",
            "namespace": "",
            "xsd_path": str(missing_xsd),
            "xsd_version": "1.1",
        }
    ]

    def network_forbidden(*_args, **_kwargs):
        raise AssertionError("analysis must not download schemas")

    monkeypatch.setattr("socket.create_connection", network_forbidden)
    details, status, limitations, records = extract_xml(source, registry=known_registry)

    assert status == "partial"
    assert records == 0
    assert details["schema_validation"]["status"] == "schema_not_installed"
    assert details["semantic_interpretation_performed"] is False
    assert details["arithmetic_performed"] is False
    assert "semantic_values" not in details
    assert any(item["code"] == "schema_not_installed" for item in limitations)
    assert sum(item["code"] == "schema_not_installed" for item in limitations) == 1


def test_dtd_and_external_entity_are_rejected_before_parsing(tmp_path: Path) -> None:
    source = tmp_path / "xxe.xml"
    source.write_text(
        "<!DOCTYPE x [<!ENTITY leak SYSTEM 'file:///etc/passwd'>]><x>&leak;</x>",
        encoding="utf-8",
    )

    result = inspect_input(source, mode="full")

    item = result["input_inventory"][0]
    assert item["extraction_status"] == "rejected"
    assert any(limit["code"] == "xml_dtd_forbidden" for limit in result["limitations"])
    assert "root:" not in json.dumps(result, ensure_ascii=False)


def test_excessive_xml_depth_is_rejected_by_central_limit(tmp_path: Path) -> None:
    source = tmp_path / "deep.xml"
    source.write_text("<n>" * 140 + "x" + "</n>" * 140, encoding="utf-8")

    result = inspect_input(source, mode="full")

    item = result["input_inventory"][0]
    assert item["extraction_status"] == "rejected"
    assert any(limit["code"] == "xml_limit_exceeded" for limit in result["limitations"])


def test_default_plugin_registry_reports_known_gge_schema_as_not_installed(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("SMETCHIK_DATA_DIR", str(tmp_path / "missing-schemas"))
    source = tmp_path / "unknown-construction.gge"
    source.write_text("<Construction/>", encoding="utf-8")

    result = inspect_input(source, mode="full")

    details = result["input_inventory"][0]["details"]
    assert details["schema_validation"]["status"] == "schema_not_installed"
    assert len(details["schema_validation"]["candidate_ids"]) == 8
    assert details["semantic_interpretation_performed"] is False
    assert any(limit["code"] == "schema_not_installed" for limit in result["limitations"])


def test_mge_registry_entry_is_adapter_only_partial_extraction(tmp_path: Path) -> None:
    source = tmp_path / "regional.mge"
    source.write_text("<RegionalEstimate><Total>100</Total></RegionalEstimate>", encoding="utf-8")

    result = inspect_input(source, mode="full")

    details = result["input_inventory"][0]["details"]
    assert details["schema_validation"]["status"] == "adapter_only"
    assert details["semantic_interpretation_performed"] is False
    assert details["arithmetic_performed"] is False
    assert any(
        limit["code"] == "adapter_only_partial_extraction"
        for limit in result["limitations"]
    )
    assert "100" not in json.dumps(details, ensure_ascii=False)
    _details, _status, _limitations, records = extract_xml(source)
    assert records == 0


GRAND_DOCUMENT_TYPES = [
    ("grandsmeta.local_estimate.2026_1_1", "{2B0470FD-477C-4359-9F34-EEBE36B7D340}", False),
    (
        "grandsmeta.object_or_summary_estimate.2026_1_1",
        "{2B0470FD-477C-4359-9F34-EEBE36B7D345}",
        False,
    ),
    ("grandsmeta.contract_estimate.2026_1_1", "{2B0470FD-477C-4359-9F34-EEBE36B7D353}", False),
    ("grandsmeta.market_analysis.2026_1_1", "{2B0470FD-477C-4359-9F34-EEBE36B7D354}", False),
    ("grandsmeta.quantity_takeoff.2026_1_1", "{2B0470FD-477C-4359-9F34-EEBE36B7D350}", True),
]


@pytest.mark.parametrize(("adapter_id", "document_type", "needs_parameters"), GRAND_DOCUMENT_TYPES)
def test_known_grandsmeta_schema_without_installation_is_structure_only(
    tmp_path: Path,
    monkeypatch,
    adapter_id: str,
    document_type: str,
    needs_parameters: bool,
) -> None:
    monkeypatch.setenv("SMETCHIK_DATA_DIR", str(tmp_path / "missing-schemas"))
    body = "<Parameters/>" if needs_parameters else ""
    source = tmp_path / f"{adapter_id}.xml"
    source.write_text(
        (
            '<Document Generator="test" ProgramVersion="2026.1.1" '
            f'DocumentType="{document_type}">{body}</Document>'
        ),
        encoding="utf-8",
    )

    details, status, limitations, records = extract_xml(source)

    assert status == "partial"
    assert records == 0
    assert details["schema"] is None
    assert details["schema_validation"]["status"] == "schema_not_installed"
    assert details["semantic_interpretation_performed"] is False
    assert any(limit["code"] == "schema_not_installed" for limit in limitations)


def test_default_registry_resolves_xsd_only_in_external_data_store(tmp_path: Path) -> None:
    registry_entries, _regional = load_default_registry(data_dir=tmp_path / "schemas")
    minstroy_entries = [
        entry for entry in registry_entries if entry.get("family") == "minstroy_gge"
    ]

    assert len(minstroy_entries) == 8
    for entry in minstroy_entries:
        assert Path(entry["xsd_path"]).is_relative_to(tmp_path / "schemas")


def test_missing_candidate_takes_precedence_over_invalid_candidate(
    tmp_path: Path,
) -> None:
    source = tmp_path / "estimate.xml"
    source.write_text("<Estimate><total>bad</total></Estimate>", encoding="utf-8")
    installed = tmp_path / "installed.xsd"
    installed.write_text(
        """<?xml version="1.0"?>
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="Estimate">
    <xs:complexType><xs:sequence>
      <xs:element name="total" type="xs:decimal"/>
    </xs:sequence></xs:complexType>
  </xs:element>
</xs:schema>
""",
        encoding="utf-8",
    )
    registry = [
        {
            "id": "installed-invalid",
            "root": "Estimate",
            "namespace": "",
            "xsd_path": str(installed),
            "xsd_version": "1.0",
        },
        {
            "id": "missing-possible-match",
            "root": "Estimate",
            "namespace": "",
            "xsd_path": str(tmp_path / "missing.xsd"),
            "xsd_version": "1.0",
        },
    ]

    details, status, limitations, records = extract_xml(source, registry=registry)

    assert status == "partial"
    assert records == 0
    assert details["schema_validation"]["status"] == "schema_not_installed"
    assert details["schema_validation"]["error_count"] == 0
    assert details["semantic_interpretation_performed"] is False
    assert details["arithmetic_performed"] is False
    assert any(item["code"] == "schema_not_installed" for item in limitations)
    assert sum(item["code"] == "schema_not_installed" for item in limitations) == 1
