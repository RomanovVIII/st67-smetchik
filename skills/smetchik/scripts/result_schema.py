from __future__ import annotations

import json
import math
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


RESULT_SCHEMA_PATH = Path(__file__).resolve().parent.parent / "assets" / "smetchik.result.v1.schema.json"


class ResultSchemaError(ValueError):
    pass


def _non_finite_errors(
    value: Any,
    *,
    path: str = "$",
    seen: set[int] | None = None,
) -> list[str]:
    if isinstance(value, float) and not math.isfinite(value):
        return [f"{path}: non-finite number"]
    if not isinstance(value, (dict, list)):
        return []
    visited = seen if seen is not None else set()
    identity = id(value)
    if identity in visited:
        return []
    visited.add(identity)
    errors: list[str] = []
    if isinstance(value, dict):
        for name, child in value.items():
            errors.extend(
                _non_finite_errors(child, path=f"{path}.{name}", seen=visited)
            )
    else:
        for index, child in enumerate(value):
            errors.extend(
                _non_finite_errors(child, path=f"{path}[{index}]", seen=visited)
            )
    return errors


def _matches_type(value: Any, type_name: str) -> bool:
    if type_name == "object":
        return isinstance(value, dict)
    if type_name == "array":
        return isinstance(value, list)
    if type_name == "string":
        return isinstance(value, str)
    if type_name == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if type_name == "number":
        return (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and (not isinstance(value, float) or math.isfinite(value))
        )
    if type_name == "boolean":
        return isinstance(value, bool)
    if type_name == "null":
        return value is None
    return False


def _resolve_reference(root: dict[str, Any], reference: str) -> dict[str, Any]:
    if not reference.startswith("#/"):
        raise ResultSchemaError("external JSON Schema references are forbidden")
    target: Any = root
    for component in reference[2:].split("/"):
        target = target[component.replace("~1", "/").replace("~0", "~")]
    if not isinstance(target, dict):
        raise ResultSchemaError("JSON Schema reference does not resolve to an object")
    return target


def _validation_errors(
    instance: Any,
    schema: dict[str, Any],
    root: dict[str, Any],
    path: str,
) -> list[str]:
    if "$ref" in schema:
        return _validation_errors(instance, _resolve_reference(root, schema["$ref"]), root, path)
    errors: list[str] = []
    type_declaration = schema.get("type")
    allowed_types = [type_declaration] if isinstance(type_declaration, str) else type_declaration or []
    if allowed_types and not any(_matches_type(instance, name) for name in allowed_types):
        if "number" in allowed_types and isinstance(instance, float) and not math.isfinite(instance):
            return [f"{path}: non-finite number"]
        return [f"{path}: invalid type"]
    if "const" in schema and instance != schema["const"]:
        errors.append(f"{path}: const mismatch")
    if "enum" in schema and instance not in schema["enum"]:
        errors.append(f"{path}: enum mismatch")
    if isinstance(instance, str):
        if len(instance) < int(schema.get("minLength", 0)):
            errors.append(f"{path}: below minLength")
        maximum_length = schema.get("maxLength")
        if maximum_length is not None and len(instance) > int(maximum_length):
            errors.append(f"{path}: above maxLength")
        pattern = schema.get("pattern")
        if isinstance(pattern, str) and re.search(pattern, instance) is None:
            errors.append(f"{path}: pattern mismatch")
        if schema.get("format") == "uri":
            parsed = urlparse(instance)
            if not parsed.scheme or (parsed.scheme in {"http", "https"} and not parsed.netloc):
                errors.append(f"{path}: invalid uri")
    if isinstance(instance, (int, float)) and not isinstance(instance, bool):
        minimum = schema.get("minimum")
        if minimum is not None and instance < minimum:
            errors.append(f"{path}: below minimum")
    if isinstance(instance, dict):
        properties = schema.get("properties", {})
        for required in schema.get("required", []):
            if required not in instance:
                errors.append(f"{path}: missing required property {required}")
        if schema.get("additionalProperties") is False:
            for name in instance.keys() - properties.keys():
                errors.append(f"{path}: unexpected property {name}")
        for name, value in instance.items():
            child_schema = properties.get(name)
            if isinstance(child_schema, dict):
                errors.extend(_validation_errors(value, child_schema, root, f"{path}.{name}"))
    if isinstance(instance, list):
        if len(instance) < int(schema.get("minItems", 0)):
            errors.append(f"{path}: below minItems")
        maximum_items = schema.get("maxItems")
        if maximum_items is not None and len(instance) > int(maximum_items):
            errors.append(f"{path}: above maxItems")
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for index, value in enumerate(instance):
                errors.extend(_validation_errors(value, item_schema, root, f"{path}[{index}]"))
    for branch in schema.get("allOf", []):
        condition = branch.get("if")
        selected = branch
        if isinstance(condition, dict):
            selected = branch.get("then", {}) if not _validation_errors(instance, condition, root, path) else branch.get("else", {})
        if isinstance(selected, dict):
            errors.extend(_validation_errors(instance, selected, root, path))
    if "oneOf" in schema:
        matches = sum(
            not _validation_errors(instance, candidate, root, path)
            for candidate in schema["oneOf"]
        )
        if matches != 1:
            errors.append(f"{path}: expected exactly one oneOf branch")
    return errors


def validate_result_schema(result: dict[str, Any]) -> None:
    finite_errors = _non_finite_errors(result)
    if finite_errors:
        raise ResultSchemaError("; ".join(finite_errors[:20]))
    try:
        schema = json.loads(RESULT_SCHEMA_PATH.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ResultSchemaError("result JSON Schema is unavailable") from error
    errors = _validation_errors(result, schema, schema, "$")
    coverage = result.get("coverage") if isinstance(result.get("coverage"), dict) else {}
    checked = coverage.get("checked_records")
    checkable = coverage.get("checkable_records")
    if isinstance(checked, int) and isinstance(checkable, int) and checked > checkable:
        errors.append("$.coverage: checked_records exceeds checkable_records")
    if result.get("mode") == "light" and checked not in (None, 0):
        errors.append("$.coverage: light mode cannot claim checked rows")
    if coverage.get("row_level_checked") is True:
        if result.get("mode") != "full" or not isinstance(checkable, int) or checkable <= 0 or checked != checkable:
            errors.append("$.coverage: invalid row_level_checked claim")
        if coverage.get("unclassified_candidate_ranges"):
            errors.append("$.coverage: unclassified ranges forbid row_level_checked")
    questions = result.get("questions") if isinstance(result.get("questions"), list) else []
    checks = result.get("checks") if isinstance(result.get("checks"), list) else []
    if result.get("execution_status") == "needs_input":
        if len(questions) != 1:
            errors.append("$.questions: needs_input requires one grouped question")
        if any(isinstance(check, dict) and check.get("status") == "passed" for check in checks):
            errors.append("$.checks: needs_input result cannot contain passed controls")
    source_objects = result.get("normative_sources") if isinstance(result.get("normative_sources"), list) else []
    source_by_id: dict[str, dict[str, Any]] = {}
    for source_index, source in enumerate(source_objects):
        if not isinstance(source, dict) or not source.get("id"):
            continue
        source_id = str(source["id"])
        if source_id in source_by_id:
            errors.append(f"$.normative_sources[{source_index}].id: duplicate source ID")
            continue
        source_by_id[source_id] = source
        source_class = source.get("class")
        edition = source.get("edition")
        pinpoint = source.get("pinpoint")
        official_url = source.get("official_url")
        if not isinstance(edition, str) or not edition.strip():
            errors.append(f"$.normative_sources[{source_index}].edition: required")
        if not isinstance(pinpoint, str) or not pinpoint.strip():
            errors.append(f"$.normative_sources[{source_index}].pinpoint: required")
        if source_class != "internal":
            parsed = urlparse(official_url) if isinstance(official_url, str) else None
            if parsed is None or parsed.scheme not in {"http", "https"} or not parsed.netloc:
                errors.append(f"$.normative_sources[{source_index}].official_url: valid http(s) URL required")
    source_ids = set(source_by_id)
    for index, finding in enumerate(result.get("findings", [])):
        if not isinstance(finding, dict):
            continue
        ordered_source_ids = list(map(str, finding.get("source_ids", [])))
        if len(ordered_source_ids) != len(set(ordered_source_ids)):
            errors.append(f"$.findings[{index}].source_ids: duplicate references")
        dangling = set(ordered_source_ids) - source_ids
        if dangling:
            errors.append(f"$.findings[{index}].source_ids: dangling references {sorted(dangling)}")
        citations = finding.get("source_citations")
        citation_ids = (
            [str(item.get("source_id")) for item in citations if isinstance(item, dict)]
            if isinstance(citations, list)
            else []
        )
        if citation_ids != ordered_source_ids:
            errors.append(f"$.findings[{index}].source_citations: order/source mismatch")
        for citation_index, citation in enumerate(citations if isinstance(citations, list) else []):
            if not isinstance(citation, dict):
                continue
            source = source_by_id.get(str(citation.get("source_id")))
            if source is None:
                continue
            for field in ("edition", "pinpoint", "official_url"):
                if citation.get(field) != source.get(field):
                    errors.append(
                        f"$.findings[{index}].source_citations[{citation_index}].{field}: citation mismatch"
                    )
    if any(
        isinstance(finding, dict) and "INT-01" in finding.get("source_ids", [])
        for finding in result.get("findings", [])
    ):
        internal = next(
            (
                source
                for source in source_objects
                if isinstance(source, dict) and source.get("id") == "INT-01"
            ),
            None,
        )
        if not internal or internal.get("class") != "internal" or internal.get("normativity") != "non_normative":
            errors.append("$.normative_sources: INT-01 must be a non-normative internal source")
    if errors:
        raise ResultSchemaError("; ".join(errors[:20]))
