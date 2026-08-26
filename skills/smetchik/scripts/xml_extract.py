from __future__ import annotations

import hashlib
import json
import math
import os
import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable

import xmlschema
from lxml import etree

from safety_limits import XML_LIMITS, XmlLimits
from xml_adapters import extract_semantic_records, supports_semantic_adapter


FORBIDDEN_DECLARATIONS = (b"<!doctype", b"<!entity")
PLUGIN_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_REGISTRY_PATH = PLUGIN_ROOT / "schemas" / "registry.json"
SCHEMA_DATA_DIR_ENV = "SMETCHIK_DATA_DIR"
DEFAULT_SCHEMA_DATA_DIR = Path("~/.local/share/smetchik/schemas")
NUMERIC_LEXICAL = re.compile(r"^[+-]?(?:0|[1-9]\d*)(?:\.\d+)?$")
NON_NUMERIC_NAME_HINTS = ("id", "code", "version", "guid", "uuid", "cipher")


class XmlRejected(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _rejected(
    path: Path,
    code: str,
    message: str,
) -> tuple[dict[str, Any], str, list[dict[str, Any]], int]:
    return (
        {
            "format": "xml",
            "schema": None,
            "schema_validation": {"status": "not_performed", "error_count": 0},
            "structure": [],
            "semantic_interpretation_performed": False,
            "arithmetic_performed": False,
        },
        "rejected",
        [
            {
                "code": code,
                "message": message,
                "evidence": [
                    {
                        "source_path": str(path),
                        "locator": f"{path}:xml",
                    }
                ],
            }
        ],
        0,
    )


def _read_and_parse(path: Path, limits: XmlLimits) -> etree._Element:
    if path.stat().st_size > limits.max_file_bytes:
        raise XmlRejected("xml_limit_exceeded", "XML превышает допустимый размер.")
    raw = path.read_bytes()
    lowered = raw.lower()
    if any(declaration in lowered for declaration in FORBIDDEN_DECLARATIONS):
        raise XmlRejected("xml_dtd_forbidden", "DTD и объявления сущностей запрещены.")
    parser = etree.XMLParser(
        resolve_entities=False,
        no_network=True,
        load_dtd=False,
        dtd_validation=False,
        recover=False,
        huge_tree=False,
        remove_comments=False,
    )
    try:
        root = etree.fromstring(raw, parser=parser)
    except (etree.XMLSyntaxError, ValueError) as error:
        raise XmlRejected("xml_parse_error", "XML синтаксически не разобран.") from error
    node_count = 0
    total_text = 0
    for element in root.iter():
        node_count += 1
        if node_count > limits.max_nodes:
            raise XmlRejected("xml_limit_exceeded", "XML превышает лимит узлов.")
        depth = sum(1 for _ in element.iterancestors()) + 1
        if depth > limits.max_depth:
            raise XmlRejected("xml_limit_exceeded", "XML превышает лимит глубины.")
        for text in (element.text, element.tail):
            if text is None:
                continue
            if len(text) > limits.max_single_text_characters:
                raise XmlRejected("xml_limit_exceeded", "XML содержит чрезмерно длинное текстовое значение.")
            total_text += len(text)
            if total_text > limits.max_total_text_characters:
                raise XmlRejected("xml_limit_exceeded", "XML превышает общий лимит текста.")
    return root


def _qname(element: etree._Element) -> tuple[str, str]:
    qname = etree.QName(element)
    return qname.localname, qname.namespace or ""


def _matching_schemas(
    root: etree._Element,
    registry: Iterable[dict[str, Any]],
    suffix: str,
) -> list[dict[str, Any]]:
    local_name, namespace = _qname(root)
    matches: list[dict[str, Any]] = []
    for entry in registry:
        if not isinstance(entry, dict):
            continue
        extensions = entry.get("extensions")
        if isinstance(extensions, list) and suffix not in extensions:
            continue
        if entry.get("root") == local_name and (entry.get("namespace") or "") == namespace:
            matches.append(entry)
    return matches


def default_schema_data_dir() -> Path:
    """Return the external schema store; analysis never populates it."""
    configured = os.environ.get(SCHEMA_DATA_DIR_ENV)
    value = Path(configured) if configured else DEFAULT_SCHEMA_DATA_DIR
    return value.expanduser().resolve(strict=False)


def load_default_registry(
    *,
    data_dir: Path | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    payload = json.loads(DEFAULT_REGISTRY_PATH.read_text(encoding="utf-8"))
    if payload.get("network_schema_resolution") is not False:
        raise ValueError("registry must disable network schema resolution")
    adapters: list[dict[str, Any]] = []
    for source in payload.get("adapters", []):
        if not isinstance(source, dict):
            continue
        entry = dict(source)
        entry["root"] = source.get("root_element")
        entry["namespace"] = source.get("namespace") or ""
        entry["xsd_path"] = str((data_dir or default_schema_data_dir()) / str(source.get("path", "")))
        entry["xsd_version"] = source.get("xsd_dialect")
        adapters.append(entry)
    regional = [entry for entry in payload.get("regional_adapters", []) if isinstance(entry, dict)]
    return adapters, regional


def _xml_evidence(path: Path, xpath: str, line: int | None) -> dict[str, Any]:
    locator = f"{path}:xpath:{xpath}"
    if line is not None:
        locator += f";line:{line}"
    return {
        "source_path": str(path),
        "xpath": xpath,
        "line": line,
        "locator": locator,
    }


def _structure(root: etree._Element, path: Path) -> list[dict[str, Any]]:
    tree = root.getroottree()
    nodes: list[dict[str, Any]] = []
    for element in root.iter():
        local_name, namespace = _qname(element)
        xpath = tree.getpath(element)
        nodes.append(
            {
                "local_name": local_name,
                "namespace": namespace,
                "attribute_names": sorted(etree.QName(name).localname for name in element.attrib),
                "child_count": len(element),
                "evidence": _xml_evidence(path, xpath, element.sourceline),
            }
        )
    return nodes


def _attribute_xpath(element: etree._Element, raw_name: str) -> str:
    element_xpath = element.getroottree().getpath(element)
    qname = etree.QName(raw_name)
    if not qname.namespace:
        return f"{element_xpath}/@{qname.localname}"
    prefix = next(
        (candidate for candidate, namespace in element.nsmap.items() if candidate and namespace == qname.namespace),
        None,
    )
    if prefix:
        return f"{element_xpath}/@{prefix}:{qname.localname}"
    return (
        f"{element_xpath}/@*[local-name()='{qname.localname}' "
        f"and namespace-uri()='{qname.namespace}']"
    )


def _typed_value(local_name: str, raw_value: str) -> tuple[str, Any] | None:
    lexical = raw_value.strip()
    lowered_name = local_name.lower()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", lexical):
        try:
            return "date", date.fromisoformat(lexical).isoformat()
        except ValueError:
            return None
    if "T" in lexical:
        try:
            parsed = datetime.fromisoformat(lexical.replace("Z", "+00:00"))
            return "datetime", parsed.isoformat()
        except ValueError:
            pass
    if any(hint in lowered_name for hint in NON_NUMERIC_NAME_HINTS):
        return None
    if not NUMERIC_LEXICAL.fullmatch(lexical):
        return None
    significant_digits = len(lexical.lstrip("+-").replace(".", "").lstrip("0"))
    if significant_digits > 15:
        return None
    try:
        decimal_value = Decimal(lexical)
    except InvalidOperation:
        return None
    if "." not in lexical:
        return "integer", int(decimal_value)
    numeric_value = float(decimal_value)
    if not math.isfinite(numeric_value) or Decimal(str(numeric_value)) != decimal_value.normalize():
        return None
    return "decimal", numeric_value


def _semantic_values(root: etree._Element, path: Path) -> list[dict[str, Any]]:
    tree = root.getroottree()
    values: list[dict[str, Any]] = []
    for element in root.iter():
        element_local_name, _namespace = _qname(element)
        for raw_name, raw_value in element.attrib.items():
            attribute_name = etree.QName(raw_name).localname
            xpath = _attribute_xpath(element, raw_name)
            record: dict[str, Any] = {
                "value_kind": "attribute",
                "local_name": attribute_name,
                "owner_local_name": element_local_name,
                "xpath": xpath,
                "line": element.sourceline,
                "raw_value": raw_value,
                "evidence": _xml_evidence(path, xpath, element.sourceline),
            }
            typed = _typed_value(attribute_name, raw_value)
            if typed is not None:
                record["value_type"], record["typed_value"] = typed
            values.append(record)
        if len(element) != 0 or element.text is None:
            continue
        raw_value = element.text
        if not raw_value.strip():
            continue
        xpath = tree.getpath(element)
        record = {
            "value_kind": "element_text",
            "local_name": element_local_name,
            "xpath": xpath,
            "line": element.sourceline,
            "raw_value": raw_value,
            "evidence": _xml_evidence(path, xpath, element.sourceline),
        }
        typed = _typed_value(element_local_name, raw_value)
        if typed is not None:
            record["value_type"], record["typed_value"] = typed
        values.append(record)
    return values


def _validate_local_schema(
    root: etree._Element,
    entry: dict[str, Any],
    path: Path,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    root_evidence = [_xml_evidence(path, root.getroottree().getpath(root), root.sourceline)]
    raw_path = entry.get("xsd_path")
    if not isinstance(raw_path, str) or "://" in raw_path:
        return (
            {"status": "not_performed", "xsd_version": entry.get("xsd_version"), "error_count": 0},
            [
                {
                    "code": "local_schema_unavailable",
                    "message": "Registry не содержит допустимый локальный путь XSD.",
                    "evidence": root_evidence,
                }
            ],
        )
    xsd_path = Path(raw_path)
    if not xsd_path.is_file():
        return (
            {"status": "schema_not_installed", "xsd_version": entry.get("xsd_version"), "error_count": 0},
            [
                {
                    "code": "schema_not_installed",
                    "message": "Требуемая XSD не установлена локально; выполнена только структурная инвентаризация без смысловой и стоимостной проверки.",
                    "evidence": root_evidence,
                }
            ],
        )
    expected_hash = entry.get("sha256")
    if isinstance(expected_hash, str):
        actual_hash = hashlib.sha256(xsd_path.read_bytes()).hexdigest()
        if actual_hash != expected_hash:
            return (
                {"status": "not_performed", "xsd_version": entry.get("xsd_version"), "error_count": 0},
                [
                    {
                        "code": "local_schema_integrity_mismatch",
                        "message": "SHA-256 локального XSD не совпадает с registry.",
                        "evidence": root_evidence,
                    }
                ],
            )
    schema_type = xmlschema.XMLSchema11 if str(entry.get("xsd_version")) == "1.1" else xmlschema.XMLSchema
    try:
        schema = schema_type(
            xsd_path,
            allow="local",
            defuse="always",
            timeout=5,
            use_fallback=False,
        )
        errors = list(schema.iter_errors(root))
    except Exception:
        return (
            {"status": "not_performed", "xsd_version": entry.get("xsd_version"), "error_count": 0},
            [
                {
                    "code": "local_schema_unavailable",
                    "message": "Локальный XSD не удалось безопасно загрузить или применить.",
                    "evidence": root_evidence,
                }
            ],
        )
    if errors:
        return (
            {
                "status": "invalid",
                "xsd_version": entry.get("xsd_version"),
                "error_count": len(errors),
            },
            [
                {
                    "code": "xml_schema_invalid",
                    "message": "XML не прошёл валидацию по выбранному локальному XSD.",
                    "evidence": root_evidence,
                }
            ],
        )
    return (
        {"status": "valid", "xsd_version": entry.get("xsd_version"), "error_count": 0},
        [],
    )


def extract_xml(
    path: Path,
    *,
    registry: Iterable[dict[str, Any]] | None = None,
    regional_adapters: Iterable[dict[str, Any]] = (),
    limits: XmlLimits = XML_LIMITS,
) -> tuple[dict[str, Any], str, list[dict[str, Any]], int]:
    try:
        root = _read_and_parse(path, limits)
    except XmlRejected as error:
        return _rejected(path, error.code, error.message)
    structure = _structure(root, path)
    registry_limits: list[dict[str, Any]] = []
    if registry is None:
        try:
            effective_registry, default_regional = load_default_registry()
            effective_regional = default_regional
        except (OSError, ValueError, json.JSONDecodeError):
            effective_registry = []
            effective_regional = []
            registry_limits.append(
                {
                    "code": "local_schema_registry_unavailable",
                    "message": "Локальный registry схем отсутствует или повреждён.",
                    "evidence": [structure[0]["evidence"]],
                }
            )
    else:
        effective_registry = list(registry)
        effective_regional = list(regional_adapters)
    base_details: dict[str, Any] = {
        "format": "xml",
        "structure": structure,
        "semantic_interpretation_performed": False,
        "arithmetic_performed": False,
    }
    for regional in effective_regional:
        extensions = regional.get("extensions")
        if (
            path.suffix.lower() == ".mge"
            and isinstance(extensions, list)
            and path.suffix.lower() in extensions
        ):
            base_details.update(
                {
                    "schema": {"id": regional.get("id"), "status": regional.get("status")},
                    "schema_validation": {
                        "status": "adapter_only",
                        "error_count": 0,
                        "candidate_ids": [regional.get("id")],
                    },
                }
            )
            return (
                base_details,
                "partial",
                registry_limits
                + [
                    {
                        "code": "adapter_only_partial_extraction",
                        "message": "Для формата доступен только adapter metadata; выполнена структурная инвентаризация без полной XSD-валидации.",
                        "evidence": [structure[0]["evidence"]],
                    }
                ],
                0,
            )

    candidates = _matching_schemas(root, effective_registry, path.suffix.lower())
    if not candidates:
        base_details.update(
            {
                "schema": None,
                "schema_validation": {"status": "unsupported_schema", "error_count": 0},
            }
        )
        return (
            base_details,
            "partial",
            registry_limits
            + [
                {
                    "code": "unsupported_schema",
                    "message": "Схема неизвестна: выполнена только безопасная структурная инвентаризация.",
                    "evidence": [structure[0]["evidence"]],
                }
            ],
            0,
        )
    version_mismatches: list[dict[str, Any]] = []
    eligible: list[dict[str, Any]] = []
    for entry in candidates:
        version_attribute = entry.get("version_attribute")
        expected_version = entry.get("version")
        if isinstance(version_attribute, str) and expected_version is not None:
            if root.get(version_attribute) != expected_version:
                version_mismatches.append(entry)
                continue
        eligible.append(entry)
    if not eligible:
        first = version_mismatches[0]
        base_details["schema"] = {
            "id": first.get("id"),
            "root": first.get("root"),
            "namespace": first.get("namespace") or "",
            "version": first.get("version"),
        }
        base_details["schema_validation"] = {
            "status": "not_performed",
            "xsd_version": first.get("xsd_version"),
            "error_count": 0,
            "candidate_ids": [entry.get("id") for entry in version_mismatches],
        }
        return (
            base_details,
            "partial",
            [
                {
                    "code": "unsupported_schema_version",
                    "message": "Версия XML не совпадает с версией локального adapter registry.",
                    "evidence": [structure[0]["evidence"]],
                }
            ],
            0,
        )
    validations: list[tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]] = []
    for entry in eligible:
        validation, limits_for_entry = _validate_local_schema(root, entry, path)
        validations.append((entry, validation, limits_for_entry))
    valid = [record for record in validations if record[1]["status"] == "valid"]
    candidate_ids = [entry.get("id") for entry in eligible]
    if len(valid) == 1:
        entry, validation, limitations = valid[0]
        base_details["schema"] = {
            "id": entry.get("id"),
            "root": entry.get("root"),
            "namespace": entry.get("namespace") or "",
            "version": entry.get("version"),
        }
        validation["candidate_ids"] = candidate_ids
        base_details["schema_validation"] = validation
        if not supports_semantic_adapter(entry):
            return (
                base_details,
                "partial",
                registry_limits
                + limitations
                + [
                    {
                        "code": "semantic_adapter_unavailable",
                        "message": "XSD валидация успешна, но доверенный предметный adapter для этой схемы не зарегистрирован; выполнена только структурная инвентаризация.",
                        "evidence": [structure[0]["evidence"]],
                    }
                ],
                0,
            )
        values = _semantic_values(root, path)
        records = extract_semantic_records(
            root,
            path=path,
            entry=entry,
            semantic_values=values,
        )
        base_details["semantic_values"] = values
        base_details["semantic_records"] = records
        base_details["semantic_interpretation_performed"] = bool(records)
        if not records:
            return (
                base_details,
                "partial",
                registry_limits
                + limitations
                + [
                    {
                        "code": "semantic_records_not_found",
                        "message": "XML валиден по поддерживаемой XSD, но не содержит значений по зарегистрированным предметным путям adapter-а.",
                        "evidence": [structure[0]["evidence"]],
                    }
                ],
                0,
            )
        return base_details, "reliable", registry_limits + limitations, len(records)
    if len(valid) > 1:
        base_details["schema"] = None
        base_details["schema_validation"] = {
            "status": "ambiguous",
            "error_count": 0,
            "candidate_ids": [entry.get("id") for entry, _validation, _limits in valid],
        }
        return (
            base_details,
            "partial",
            registry_limits
            + [
                {
                    "code": "ambiguous_local_schema",
                    "message": "XML прошёл несколько локальных схем; смысловая интерпретация не выполнялась.",
                    "evidence": [structure[0]["evidence"]],
                }
            ],
            0,
        )
    all_limitations = [limit for _entry, _validation, limits_for_entry in validations for limit in limits_for_entry]
    invalid_count = sum(
        int(validation.get("error_count", 0))
        for _entry, validation, _limits in validations
        if validation.get("status") == "invalid"
    )
    missing_count = sum(
        1
        for _entry, validation, _limits in validations
        if validation.get("status") == "schema_not_installed"
    )
    validation_status = (
        "schema_not_installed"
        if missing_count
        else "invalid"
        if invalid_count
        else "not_performed"
    )
    reported_error_count = 0 if missing_count else invalid_count
    base_details["schema"] = None
    base_details["schema_validation"] = {
        "status": validation_status,
        "error_count": reported_error_count,
        "candidate_ids": candidate_ids,
    }
    if validation_status == "invalid":
        all_limitations = [
            {
                "code": "xml_schema_invalid",
                "message": "XML не прошёл ни один локальный XSD-кандидат.",
                "evidence": [structure[0]["evidence"]],
            }
        ]
    elif validation_status == "schema_not_installed":
        missing_limitations = [
            limit
            for _entry, validation, limits_for_entry in validations
            if validation.get("status") == "schema_not_installed"
            for limit in limits_for_entry
        ]
        all_limitations = missing_limitations[:1]
    return base_details, "partial", registry_limits + all_limitations, 0
