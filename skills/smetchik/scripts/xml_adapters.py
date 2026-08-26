from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from lxml import etree


@dataclass(frozen=True)
class CanonicalFieldRule:
    field_name: str
    relative_paths: tuple[str, ...]
    resolution: str = "first"


@dataclass(frozen=True)
class SemanticRule:
    record_type: str
    path_pattern: str
    canonical_fields: tuple[CanonicalFieldRule, ...] = ()

    def matches(self, normalized_xpath: str) -> bool:
        return re.fullmatch(self.path_pattern, normalized_xpath) is not None


DOCUMENT_FIELDS = (
    CanonicalFieldRule("name", ("Name",)),
    CanonicalFieldRule("code", ("Num",)),
)

MINSTROY_COST_FIELDS = (
    CanonicalFieldRule("name", ("Name",)),
    CanonicalFieldRule("code", ("Code",)),
    CanonicalFieldRule("unit", ("Unit",)),
    CanonicalFieldRule("quantity", ("QuantityTotal", "Quantity")),
    CanonicalFieldRule("declared_total", ("Totals/Current/Direct",)),
)

MINSTROY_TOTAL_FIELDS = (
    CanonicalFieldRule("declared_total", ("Total", ".")),
)

GRAND_POSITION_FIELDS = (
    CanonicalFieldRule("name", ("@Caption",)),
    CanonicalFieldRule("code", ("@Code",)),
    CanonicalFieldRule("unit", ("@Units",)),
    CanonicalFieldRule("quantity", ("@Quantity", "Quantity/@Result")),
)

OS_INDEX_VALUE_PATHS = (
    "@Pz",
    "@Sroy",
    "@Mont",
    "@Obor",
    "@Other",
    "@Mine",
    "@Oz",
    "@Em",
    "@Zm",
    "@Mt",
    "@Tr",
    "@Nr",
    "@Sp",
    "@EdPr",
    "@Fot",
    "@Gmc",
)


def _minstroy_local_rules(*, include_current_unit_price: bool) -> tuple[SemanticRule, ...]:
    cost_fields = MINSTROY_COST_FIELDS
    if include_current_unit_price:
        cost_fields += (
            CanonicalFieldRule("unit_price", ("PerUnit/Current/Direct",)),
        )
    return (
        SemanticRule("document", r"/Construction", DOCUMENT_FIELDS),
        SemanticRule(
            "estimate",
            r"/Construction/Object/Estimate",
            (
                CanonicalFieldRule("name", ("Name",)),
                CanonicalFieldRule("code", ("Num",)),
            ),
        ),
        SemanticRule(
            "estimate_row",
            r"/Construction/Object/Estimate/Sections/Section/Items/Item/Cost",
            cost_fields,
        ),
        SemanticRule(
            "formula",
            r"/Construction/Object/Estimate/Sections/Section/Items/Item/(?:Cost/)?QuantityFormula",
        ),
        SemanticRule(
            "resource",
            r"/Construction/Object/Estimate/Sections/Section/Items/Item/(?:Cost/)?Resources/[^/]+(?:/Replaced)?",
        ),
        SemanticRule(
            "coefficient",
            r"/Construction/Object/Estimate/.+/Coefficients/Coefficient",
        ),
        SemanticRule("index", r"/Construction/Object/Estimate/.+/Index"),
        SemanticRule(
            "accrual",
            r"/Construction/Object/Estimate/.+/(?:Overhead|Profit)",
        ),
        SemanticRule(
            "total",
            r"/Construction/Object/Estimate/(?:"
            r"Sections/Section/SectionPrice/(?:Summary/(?:Total|Price)|Total)"
            r"|EstimatePrice/.+/(?:Total|Price|PriceBase|PriceCurrent|Final)"
            r")",
            MINSTROY_TOTAL_FIELDS,
        ),
    )


RULES: dict[str, tuple[SemanticRule, ...]] = {
    "minstroy.local_estimate_bim.3_01": _minstroy_local_rules(
        include_current_unit_price=True
    ),
    "minstroy.local_estimate_rim.3_01": _minstroy_local_rules(
        include_current_unit_price=False
    ),
    "minstroy.object_estimate.3_01": (
        SemanticRule("document", r"/Construction", DOCUMENT_FIELDS),
        SemanticRule(
            "estimate",
            r"/Construction/Object",
            (
                CanonicalFieldRule("name", ("Name",)),
                CanonicalFieldRule("code", ("Num",)),
                CanonicalFieldRule("declared_total", ("Details/Summary/Total",)),
            ),
        ),
        SemanticRule(
            "estimate_row",
            r"/Construction/Object/LocalEstimate",
            (
                CanonicalFieldRule("name", ("Name",)),
                CanonicalFieldRule("code", ("Num",)),
                CanonicalFieldRule("declared_total", ("Details/Summary/Total",)),
            ),
        ),
        SemanticRule(
            "total",
            r"/Construction/Object/(?:"
            r"Summary/[^/]+"
            r"|Details/.+/(?:Total|Price)"
            r"|LocalEstimate/Details/.+/(?:Total|Price)"
            r")",
            MINSTROY_TOTAL_FIELDS,
        ),
    ),
    "minstroy.summary_estimate.3_01": (
        SemanticRule("document", r"/Construction", DOCUMENT_FIELDS),
        SemanticRule("total", r"/Construction/Summary", MINSTROY_TOTAL_FIELDS),
        SemanticRule(
            "estimate_row",
            r"/Construction/Chapter(?:[1-9]|1[0-4])/(?:LocalEstimate|ObjectEstimate|Estimate|Cost|Item)",
            (
                CanonicalFieldRule("name", ("Name",)),
                CanonicalFieldRule("code", ("Num",)),
                CanonicalFieldRule("declared_total", ("Total", "Summary/Total")),
            ),
        ),
        SemanticRule(
            "total",
            r"/Construction/Chapter(?:[1-9]|1[0-4])/.+/(?:Total|Price|Summary|SubSummary)",
            MINSTROY_TOTAL_FIELDS,
        ),
    ),
    "minstroy.costs_summary.3_01": (
        SemanticRule(
            "document",
            r"/Construction",
            (CanonicalFieldRule("name", ("ConstructionSite",)),),
        ),
        SemanticRule(
            "estimate",
            r"/Construction/Costs/Estimate",
            (CanonicalFieldRule("declared_total", ("Total",)),),
        ),
        SemanticRule(
            "total",
            r"/Construction/Costs/(?:Building|Equipment|Other|EstimateTotal|EstimateTotalVAT)",
            MINSTROY_TOTAL_FIELDS,
        ),
    ),
    "minstroy.quantity_takeoff.3_01": (
        SemanticRule(
            "document",
            r"/Construction",
            (CanonicalFieldRule("name", ("ObjectName",)),),
        ),
        SemanticRule(
            "estimate_row",
            r"/Construction/Sections/Section/Works/Work",
            (
                CanonicalFieldRule("name", ("Name",)),
                CanonicalFieldRule("unit", ("Unit",)),
                CanonicalFieldRule("quantity", ("Quantity",)),
            ),
        ),
        SemanticRule(
            "formula",
            r"/Construction/Sections/Section/Works/Work/QuantityFormula",
        ),
    ),
    "minstroy.market_analysis.4_02": (
        SemanticRule(
            "document",
            r"/Construction",
            (CanonicalFieldRule("name", ("ObjectName",)),),
        ),
        SemanticRule(
            "KAC",
            r"/Construction/Sections/(?:Section|SectionFSBC)/Items/(?:Item|ItemFSBC)",
            (
                CanonicalFieldRule("name", ("Name",)),
                CanonicalFieldRule("unit", ("Unit",)),
            ),
        ),
        SemanticRule(
            "KAC",
            r"/Construction/Sections/(?:Section|SectionFSBC)/Items/(?:Item|ItemFSBC)/Offer",
            (
                CanonicalFieldRule("name", ("OfferName",)),
                CanonicalFieldRule("code", ("OfferCode",)),
                CanonicalFieldRule("unit", ("OfferUnit",)),
                CanonicalFieldRule("unit_price", ("OfferPrice/OfferUnitWithoutVAT",)),
                CanonicalFieldRule("declared_total", ("OfferPrice/EstimateWithoutVAT",)),
            ),
        ),
    ),
    "minstroy.explanatory_note_estimate.4_01": (
        SemanticRule("document", r"/Construction", DOCUMENT_FIELDS),
        SemanticRule(
            "estimate",
            r"/Construction/ObjectDescription/(?:NonIndustrialObject|IndustrialObject|LinearObject)",
        ),
        SemanticRule(
            "resource",
            r"/Construction/(?:ObjectDescription|ExplanatoryNoteEstimate)/.+/Resources/Resource",
        ),
        SemanticRule(
            "total",
            r"/Construction/ExplanatoryNoteEstimate/.+/(?:Total|Summary)",
        ),
    ),
    "grandsmeta.local_estimate.2026_1_1": (
        SemanticRule("document", r"/Document"),
        SemanticRule(
            "estimate_row",
            r"/Document/Chapters/Chapter/Position",
            GRAND_POSITION_FIELDS
            + (
                CanonicalFieldRule("unit_price", ("PriceCurr/@PZ",)),
                CanonicalFieldRule("declared_total", ("Itog/ItogRes/@Total",)),
            ),
        ),
        SemanticRule("formula", r"/Document/Chapters/Chapter/Position/Quantity"),
        SemanticRule(
            "resource",
            r"/Document/Chapters/Chapter/Position/Resources/[^/]+(?:/Replaced)?",
        ),
        SemanticRule(
            "coefficient",
            r"/Document/(?:Koefficients/K|Chapters/Chapter/Position/.+/Koefficients/K)",
        ),
        SemanticRule(
            "index",
            r"/Document/(?:Indexes/Index|Chapters/Chapter/Position/.+/Index)",
        ),
        SemanticRule(
            "accrual",
            r"/Document/(?:AddZatrats/[^/]+|Chapters/Chapter/Position/.+/AddZatrs/AddZatr)",
        ),
        SemanticRule(
            "KAC",
            r"/Document/Chapters/Chapter/Position/.+/(?:ConjuncturalAnalysis|KA)/.+",
        ),
        SemanticRule(
            "total",
            r"/Document/(?:Itog(?:/.+)?|Chapters/Chapter/(?:Itog|Summary|Total)(?:/.+)?|Chapters/Chapter/Position/Itog/[^/]+)",
            (
                CanonicalFieldRule("name", ("@Caption",)),
                CanonicalFieldRule("declared_total", ("@Total",)),
            ),
        ),
    ),
    "grandsmeta.object_or_summary_estimate.2026_1_1": (
        SemanticRule("document", r"/Document"),
        SemanticRule(
            "index",
            r"/Document/Indexes/Index",
            (
                CanonicalFieldRule("name", ("@Caption",)),
                CanonicalFieldRule("code", ("@Code",)),
                CanonicalFieldRule("index", OS_INDEX_VALUE_PATHS, "unique"),
                CanonicalFieldRule("value", OS_INDEX_VALUE_PATHS, "unique"),
            ),
        ),
        SemanticRule(
            "estimate_row",
            r"/Document/Chapters/Chapter/Position",
            (
                CanonicalFieldRule("name", ("@Caption",)),
                CanonicalFieldRule("code", ("@Obosn",)),
                CanonicalFieldRule("unit", ("@Units",)),
                CanonicalFieldRule("quantity", ("@UnitsQty",)),
                CanonicalFieldRule("unit_price", ("@EdPr",)),
                CanonicalFieldRule("declared_total", ("Total/@Total",)),
                CanonicalFieldRule("coefficient", ("@K",)),
            ),
        ),
        SemanticRule(
            "total",
            r"/Document/Chapters/Chapter/(?:Position/(?:Total|Mode_BaseIdx|Mode_BaseNoIdx|Mode_ResCurr)|Total|Summary)(?:/WithIndex)?",
            (
                CanonicalFieldRule("name", ("@Caption",)),
                CanonicalFieldRule("declared_total", ("@Total",)),
            ),
        ),
        SemanticRule("accrual", r"/Document/AddCharges/Item"),
    ),
    "grandsmeta.contract_estimate.2026_1_1": (
        SemanticRule("document", r"/Document"),
        SemanticRule(
            "estimate_row",
            r"/Document/Chapters/Chapter/Position",
            GRAND_POSITION_FIELDS
            + (
                CanonicalFieldRule("unit_price", ("Itog/UnitCost/@Total",)),
                CanonicalFieldRule("declared_total", ("Itog/Total/@Total",)),
            ),
        ),
        SemanticRule("formula", r"/Document/Chapters/Chapter/Position/Quantity"),
        SemanticRule("accrual", r"/Document/AdditionalExpenses/Item"),
        SemanticRule(
            "total",
            r"/Document/(?:Itog|Chapters/Chapter/Itog|Chapters/Chapter/Position/Itog/(?:PosTotal|Expense|SubTotal|ExpenseWithK|WithK|UnitCost|Total))",
            (
                CanonicalFieldRule("name", ("@Caption",)),
                CanonicalFieldRule("declared_total", ("@Total",)),
            ),
        ),
    ),
    "grandsmeta.market_analysis.2026_1_1": (
        SemanticRule("document", r"/Document"),
        SemanticRule(
            "estimate_row",
            r"/Document/Chapters/Chapter/Position",
            (
                CanonicalFieldRule("name", ("@Caption",)),
                CanonicalFieldRule("unit", ("@Units",)),
            ),
        ),
        SemanticRule(
            "KAC",
            r"/Document/Chapters/Chapter/Position/(?:Item|Items/Item)",
            (
                CanonicalFieldRule("name", ("@Caption",)),
                CanonicalFieldRule("code", ("@Obosn",)),
                CanonicalFieldRule("unit", ("@Units",)),
                CanonicalFieldRule("unit_price", ("@OptPrice",)),
                CanonicalFieldRule("coefficient", ("@IzmRatio",)),
            ),
        ),
        SemanticRule(
            "accrual",
            r"/Document/Chapters/Chapter/Position/(?:Item|Items/Item)/AddZatrs/AddZatr",
        ),
    ),
    "grandsmeta.quantity_takeoff.2026_1_1": (
        SemanticRule("document", r"/Document"),
        SemanticRule(
            "estimate_row",
            r"/Document/Chapters/Chapter/Position",
            GRAND_POSITION_FIELDS,
        ),
        SemanticRule("formula", r"/Document/Chapters/Chapter/Position/Quantity"),
    ),
}


EXPECTED_FAMILIES = {
    adapter_id: "minstroy_gge" if adapter_id.startswith("minstroy.") else "grandsmeta_openxml"
    for adapter_id in RULES
}


def supports_semantic_adapter(entry: dict[str, Any]) -> bool:
    adapter_id = entry.get("id")
    if not isinstance(adapter_id, str) or adapter_id not in RULES:
        return False
    return (
        entry.get("status") == "supported"
        and entry.get("family") == EXPECTED_FAMILIES[adapter_id]
    )


def _normalized_xpath(xpath: str) -> str:
    return re.sub(r"\[\d+\]", "", xpath)


def _direct_fields(anchor_xpath: str, values: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    fields: list[dict[str, Any]] = []
    for value in values:
        value_xpath = value.get("xpath")
        if not isinstance(value_xpath, str):
            continue
        if value_xpath == anchor_xpath or value_xpath.startswith(f"{anchor_xpath}/@"):
            fields.append(value)
            continue
        if "/@" not in value_xpath and value_xpath.rsplit("/", 1)[0] == anchor_xpath:
            fields.append(value)
    return fields


def _absolute_field_xpath(anchor_xpath: str, relative_path: str) -> str:
    if relative_path == ".":
        return anchor_xpath
    return f"{anchor_xpath}/{relative_path}"


def _canonical_value(field_name: str, source: dict[str, Any]) -> Any:
    if field_name in {"name", "code", "unit"}:
        return source.get("raw_value")
    return source.get("typed_value", source.get("raw_value"))


def _mapped_fields(
    anchor_xpath: str,
    mappings: tuple[CanonicalFieldRule, ...],
    values_by_xpath: dict[str, dict[str, Any]],
) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, dict[str, Any]]]:
    canonical: dict[str, Any] = {}
    sources: list[dict[str, Any]] = []
    canonical_sources: dict[str, dict[str, Any]] = {}
    for mapping in mappings:
        candidates = [
            values_by_xpath[xpath]
            for relative_path in mapping.relative_paths
            if (xpath := _absolute_field_xpath(anchor_xpath, relative_path)) in values_by_xpath
            and str(values_by_xpath[xpath].get("raw_value") or "").strip()
        ]
        if not candidates:
            continue
        if mapping.resolution == "unique":
            if len(candidates) != 1:
                continue
            selected = candidates[0]
        elif mapping.resolution == "first":
            selected = candidates[0]
        else:
            raise ValueError(f"unsupported canonical field resolution: {mapping.resolution}")
        canonical[mapping.field_name] = _canonical_value(mapping.field_name, selected)
        sources.append(selected)
        canonical_sources[mapping.field_name] = {
            "xpath": selected.get("xpath"),
            "line": selected.get("line"),
            "raw_value": selected.get("raw_value"),
            "typed_value": selected.get("typed_value"),
            "evidence": selected.get("evidence"),
        }
        if canonical_sources[mapping.field_name]["typed_value"] is None:
            canonical_sources[mapping.field_name].pop("typed_value")
    return canonical, sources, canonical_sources


IDENTIFIER_FIELDS = {
    "document": "document_id",
    "estimate": "estimate_id",
    "estimate_row": "row_id",
    "resource": "resource_id",
    "accrual": "accrual_id",
    "index": "index_id",
    "coefficient": "coefficient_id",
    "KAC": "kac_id",
    "total": "total_id",
    "formula": "formula_id",
}


def extract_semantic_records(
    root: etree._Element,
    *,
    path: Path,
    entry: dict[str, Any],
    semantic_values: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    adapter_id = str(entry["id"])
    rules = RULES[adapter_id]
    tree = root.getroottree()
    values_by_xpath = {
        str(value["xpath"]): value
        for value in semantic_values
        if isinstance(value, dict) and isinstance(value.get("xpath"), str)
    }
    records: list[dict[str, Any]] = []
    for element in root.iter():
        xpath = tree.getpath(element)
        normalized = _normalized_xpath(xpath)
        fields = _direct_fields(xpath, semantic_values)
        for rule in rules:
            if not rule.matches(normalized):
                continue
            canonical, mapped_sources, canonical_sources = _mapped_fields(
                xpath,
                rule.canonical_fields,
                values_by_xpath,
            )
            fields_by_xpath = {
                str(field.get("xpath")): field
                for field in (*fields, *mapped_sources)
                if isinstance(field, dict) and field.get("xpath")
            }
            if not fields_by_xpath:
                continue
            locator = f"{path}:xpath:{xpath}"
            if element.sourceline is not None:
                locator += f";line:{element.sourceline}"
            record = {
                "record_type": rule.record_type,
                "schema_id": adapter_id,
                "xpath": xpath,
                "normalized_schema_path": normalized,
                "line": element.sourceline,
                "fields": list(fields_by_xpath.values()),
                **canonical,
                "evidence": {
                    "source_path": str(path),
                    "xpath": xpath,
                    "line": element.sourceline,
                    "locator": locator,
                },
            }
            if canonical_sources:
                record["canonical_field_sources"] = canonical_sources
            identifier_field = IDENTIFIER_FIELDS[rule.record_type]
            record[identifier_field] = f"{adapter_id}:{xpath}"
            records.append(record)
    return records
