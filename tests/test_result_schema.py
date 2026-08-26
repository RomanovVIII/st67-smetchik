from __future__ import annotations

import json
import importlib
import re
from copy import deepcopy
from pathlib import Path
from typing import Any

from pypdf import PdfWriter
import pytest

from smetchik_engine import inspect_input


ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = ROOT / "skills" / "smetchik" / "assets" / "smetchik.result.v1.schema.json"


def schema_errors(instance: Any, schema: dict[str, Any], root: dict[str, Any], path: str = "$") -> list[str]:
    if "$ref" in schema:
        target: Any = root
        for component in schema["$ref"].removeprefix("#/").split("/"):
            target = target[component]
        return schema_errors(instance, target, root, path)
    errors: list[str] = []
    expected_type = schema.get("type")
    type_names = [expected_type] if isinstance(expected_type, str) else expected_type or []
    type_map = {
        "object": dict,
        "array": list,
        "string": str,
        "integer": int,
        "number": (int, float),
        "boolean": bool,
        "null": type(None),
    }
    if type_names and not any(isinstance(instance, type_map[name]) for name in type_names):
        return [f"{path}: expected {type_names}, got {type(instance).__name__}"]
    if "const" in schema and instance != schema["const"]:
        errors.append(f"{path}: const mismatch")
    if "enum" in schema and instance not in schema["enum"]:
        errors.append(f"{path}: enum mismatch")
    if isinstance(instance, str):
        if len(instance) < schema.get("minLength", 0):
            errors.append(f"{path}: too short")
        if "pattern" in schema and re.fullmatch(schema["pattern"], instance) is None:
            errors.append(f"{path}: pattern mismatch")
    if isinstance(instance, int) and not isinstance(instance, bool):
        if instance < schema.get("minimum", instance):
            errors.append(f"{path}: below minimum")
    if isinstance(instance, dict):
        for required in schema.get("required", []):
            if required not in instance:
                errors.append(f"{path}: missing {required}")
        properties = schema.get("properties", {})
        if schema.get("additionalProperties") is False:
            for key in instance.keys() - properties.keys():
                errors.append(f"{path}: unexpected {key}")
        for key, value in instance.items():
            if key in properties:
                errors.extend(schema_errors(value, properties[key], root, f"{path}.{key}"))
    if isinstance(instance, list):
        if len(instance) < schema.get("minItems", 0):
            errors.append(f"{path}: too few items")
        if "items" in schema:
            for index, value in enumerate(instance):
                errors.extend(schema_errors(value, schema["items"], root, f"{path}[{index}]"))
    for conditional in schema.get("allOf", []):
        condition = conditional.get("if", {})
        condition_errors = schema_errors(instance, condition, root, path)
        branch = conditional.get("then") if not condition_errors else conditional.get("else")
        if branch:
            errors.extend(schema_errors(instance, branch, root, path))
    return errors


def test_mixed_limited_result_validates_published_json_schema(
    tmp_path: Path,
    monkeypatch,
) -> None:
    package = tmp_path / "package"
    package.mkdir()
    (package / "unknown.xml").write_text("<Vendor><Total>1</Total></Vendor>", encoding="utf-8")
    pdf = package / "image-only.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    with pdf.open("wb") as stream:
        writer.write(stream)
    monkeypatch.setenv("PATH", str(tmp_path / "no-binaries"))

    result = inspect_input(package, mode="full")
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))

    assert schema_errors(result, schema, schema) == []


def test_runtime_schema_validator_accepts_result_and_rejects_broken_contract(tmp_path: Path) -> None:
    try:
        contract = importlib.import_module("result_schema")
    except ModuleNotFoundError:
        contract = None
    assert contract is not None

    source = tmp_path / "unknown.bin"
    source.write_bytes(b"local")
    result = inspect_input(source, mode="light")
    contract.validate_result_schema(result)

    broken = dict(result)
    broken.pop("coverage")
    try:
        contract.validate_result_schema(broken)
    except contract.ResultSchemaError as error:
        assert "coverage" in str(error)
    else:
        raise AssertionError("broken result contract was accepted")


def test_schema_rejects_weak_check_question_and_finding_contract(tmp_path: Path) -> None:
    contract = importlib.import_module("result_schema")
    source = tmp_path / "estimate.bin"
    source.write_bytes(b"local")
    result = inspect_input(source, mode="full")

    broken_check = deepcopy(result)
    broken_check["checks"][0].pop("evidence")
    try:
        contract.validate_result_schema(broken_check)
    except contract.ResultSchemaError as error:
        assert "evidence" in str(error)
    else:
        raise AssertionError("check without evidence was accepted")

    broken_question = deepcopy(result)
    broken_question["questions"][0].pop("missing_fields")
    try:
        contract.validate_result_schema(broken_question)
    except contract.ResultSchemaError as error:
        assert "missing_fields" in str(error)
    else:
        raise AssertionError("question without missing_fields was accepted")


def test_schema_rejects_dangling_source_and_incomplete_calculation(tmp_path: Path) -> None:
    contract = importlib.import_module("result_schema")
    source = tmp_path / "estimate.bin"
    source.write_bytes(b"local")
    context = {
        "object": "Объект",
        "work_type": "construction",
        "funding_source": "budget",
        "region_or_price_zone": "67",
        "price_level_date": "2026-08-01",
        "calculation_method": "resource_index",
        "stage": "project_documentation",
        "document_set": ["LSR"],
        }
    trusted_vat = {
        "base_before_exemptions": "100",
        "exempt_amounts": [],
        "rate": "0.22",
        "declared_amount": "1",
        "source_fields_verified_complete": True,
        "evidence": {
            "source_path": "estimate.xlsx",
            "sheet": "ЛСР",
            "cell_range": "H20",
            "locator": "ЛСР!H20",
        },
    }
    result = inspect_input(
        source,
        mode="full",
        purpose="internal_review",
        context=context,
        _trusted_domain_context={"vat": trusted_vat},
    )
    assert result["findings"]

    dangling = deepcopy(result)
    dangling["findings"][0]["source_ids"] = ["MISSING"]
    try:
        contract.validate_result_schema(dangling)
    except contract.ResultSchemaError as error:
        assert "dangling" in str(error)
    else:
        raise AssertionError("dangling source ID was accepted")

    incomplete = deepcopy(result)
    incomplete["findings"][0]["calculation"].pop("difference")
    try:
        contract.validate_result_schema(incomplete)
    except contract.ResultSchemaError as error:
        assert "difference" in str(error)
    else:
        raise AssertionError("incomplete calculation was accepted")


@pytest.mark.parametrize(
    ("location", "non_finite"),
    [
        ("context", float("nan")),
        ("check_parameters", float("inf")),
        ("check_parameters", float("-inf")),
    ],
)
def test_schema_rejects_non_finite_numbers_in_nested_additional_properties(
    tmp_path: Path,
    location: str,
    non_finite: float,
) -> None:
    contract = importlib.import_module("result_schema")
    source = tmp_path / "estimate.bin"
    source.write_bytes(b"local")
    result = inspect_input(source, mode="light")

    if location == "context":
        result["context"]["opaque"] = {"nested": [{"amount": non_finite}]}
    else:
        result["checks"][0]["parameters"] = {
            "opaque": {"nested": [{"amount": non_finite}]}
        }

    with pytest.raises(contract.ResultSchemaError, match="non-finite"):
        contract.validate_result_schema(result)
