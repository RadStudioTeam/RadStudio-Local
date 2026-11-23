#!/usr/bin/env python3
"""Import the IBSI 2D reference values from the official reference manual.

Source page:
    https://ibsi.readthedocs.io/en/latest/03_Image_features.html

The page is a copy-edited version of the final (v10) pre-print of the IBSI
reference manual, licensed under CC BY 4.0. It contains 169 feature tables.
This script extracts only the 2D subset:

* texture features: rows whose aggregation method starts with ``2D`` for the
  digital phantom and configurations A/B;
* non-texture features: configurations A/B, which the manual defines as 2D
  slice-wise configurations;
* morphological features are excluded because the manual defines them in 3D
  only and provides no 2D reference values.

Requires ``beautifulsoup4`` and ``lxml``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

from bs4 import BeautifulSoup

SOURCE_URL = "https://ibsi.readthedocs.io/en/latest/03_Image_features.html"
SOURCE_VERSION = "v10 (final pre-print, December 2019)"
SOURCE_CITATION = (
    "Zwanenburg A, Leger S, Vallières M, Löck S. Image biomarker "
    "standardisation initiative. arXiv:1612.07003v10."
)
SOURCE_LICENSE = "CC BY 4.0"
SOURCE_LICENSE_URL = "https://creativecommons.org/licenses/by/4.0/"
ARXIV_URL = "https://arxiv.org/abs/1612.07003v10"
DATASET_URL = "https://github.com/theibsi/data_sets"
RADIOLOGY_DOI = "10.1148/radiol.2020191145"

FAMILIES = {
    "Morphological features",
    "Local intensity features",
    "Intensity-based statistical features",
    "Intensity histogram features",
    "Intensity-volume histogram features",
    "Grey level co-occurrence based features",
    "Grey level run length based features",
    "Grey level size zone based features",
    "Grey level distance zone based features",
    "Neighbourhood grey tone difference based features",
    "Neighbouring grey level dependence based features",
}
TWO_D_DATASETS = {"dig. phantom", "config. A", "config. B"}
TWO_D_CONFIGS = {"config. A", "config. B"}
TEXTURE_FAMILIES = {
    "Grey level co-occurrence based features",
    "Grey level run length based features",
    "Grey level size zone based features",
    "Grey level distance zone based features",
    "Neighbourhood grey tone difference based features",
    "Neighbouring grey level dependence based features",
}
EXPECTED_REFERENCE_TABLES = 169
EXPECTED_MORPHOLOGY_TABLES = 25
EXPECTED_FEATURES = 144
EXPECTED_ROWS = 506


def clean_text(text: str | None) -> str:
    """Normalise whitespace, non-breaking spaces and permalink markers."""
    text = (text or "").replace("\u00a0", " ")
    text = re.sub(r"\s*¶$", "", text)
    text = re.sub(r"(\d)\s+(st|nd|rd|th)\b", r"\1\2", text)
    return re.sub(r"\s+", " ", text).strip()


def parse_number(text: str):
    r"""Convert a rendered value cell to a JSON number.

    The manual renders negative signs and scientific notation as LaTeX, e.g.
    ``\(-1.49 \\times 10^{3}\)`` or ``\(-\) 0.236``. Dashes mean "not
    applicable".
    """
    raw = text.strip()
    if not raw or raw in {"—", "-", "–", "✕", "✔"}:
        return None
    s = raw.replace("\u2212", "-")
    s = s.replace("\\(", "").replace("\\)", "")
    s = s.replace("\\times", "x").replace("\\cdot", "x")
    s = re.sub(r"x\s*10\^\{?\s*([-+]?\d+)\s*\}?", r"e\1", s)
    s = re.sub(r"\\mathrm\{[^}]*\}", "", s)
    s = s.replace("\\,", "").replace("\\!", "").replace("\\", "")
    s = re.sub(r"\s+", "", s)
    if s.startswith("e"):
        s = "1" + s
    try:
        value = float(s)
    except ValueError:
        return None
    if value.is_integer() and abs(value) < 1e15:
        return int(value)
    return value


def previous_code(table) -> str | None:
    for node in table.find_all_previous(["p"]):
        if "color:grey" in (node.get("style") or ""):
            return clean_text(node.get_text(" ", strip=True))
    return None


def previous_name(table) -> str | None:
    for node in table.find_all_previous(["h3"]):
        return clean_text(node.get_text(" ", strip=True))
    return None


def previous_family(table) -> str | None:
    for node in table.find_all_previous(["h2"]):
        text = clean_text(node.get_text(" ", strip=True))
        if text in FAMILIES:
            return text
    return None


def caption_number(table) -> int | None:
    caption = table.find("caption")
    if not caption:
        return None
    match = re.search(r"Table\s+(\d+)", caption.get_text(" ", strip=True))
    return int(match.group(1)) if match else None


def extract(source_path: Path):
    soup = BeautifulSoup(source_path.read_text(encoding="utf-8"), "lxml")
    features = []
    reference_tables = 0
    morphology_tables = 0
    unparsed = []
    missing = []

    for table_index, table in enumerate(soup.find_all("table"), start=1):
        headers = [th.get_text(" ", strip=True) for th in table.find_all("th")]
        if not headers or headers[0] != "data":
            continue
        reference_tables += 1
        code = previous_code(table)
        name = previous_name(table)
        family = previous_family(table)
        if family == "Morphological features":
            morphology_tables += 1
            continue
        if not code or not name or not family:
            missing.append(
                {
                    "tableIndex": table_index,
                    "code": code,
                    "name": name,
                    "family": family,
                }
            )
            continue

        rows = []
        for tr in table.find_all("tr"):
            cells = [td.get_text(" ", strip=True) for td in tr.find_all("td")]
            if len(cells) != len(headers):
                continue
            record = dict(zip(headers, cells))
            dataset = record["data"]
            aggregation = record.get("aggr. method")
            if aggregation is not None:
                if not aggregation.startswith("2D"):
                    continue
                if dataset not in TWO_D_DATASETS:
                    continue
            else:
                if dataset not in TWO_D_CONFIGS:
                    continue
                aggregation = f"{dataset} (2D)"

            value = parse_number(record["value"])
            tolerance = parse_number(record["tol."])
            if value is None:
                unparsed.append(
                    {
                        "code": code,
                        "dataset": dataset,
                        "aggregation": aggregation,
                        "rawValue": record["value"],
                    }
                )
            rows.append(
                {
                    "dataset": dataset,
                    "aggregation": aggregation,
                    "value": value,
                    "tolerance": tolerance,
                    "consensus": record["consensus"],
                    "rawValue": record["value"],
                    "rawTolerance": record["tol."],
                }
            )

        if rows:
            features.append(
                {
                    "code": code,
                    "name": name,
                    "family": family,
                    "tableIndex": table_index,
                    "tableNumber": caption_number(table),
                    "rows": rows,
                }
            )

    return {
        "features": features,
        "referenceTables": reference_tables,
        "morphologyTables": morphology_tables,
        "unparsed": unparsed,
        "missing": missing,
    }


def build_document(source_path: Path, extracted: dict, retrieved_at: str) -> dict:
    features = extracted["features"]
    family_features = Counter(feature["family"] for feature in features)
    family_rows = Counter(
        feature["family"] for feature in features for _ in feature["rows"]
    )
    dataset_rows = Counter(
        row["dataset"] for feature in features for row in feature["rows"]
    )
    aggregation_rows = Counter(
        row["aggregation"] for feature in features for row in feature["rows"]
    )
    tolerance_missing = sum(
        1
        for feature in features
        for row in feature["rows"]
        if row["tolerance"] is None
    )
    codes = [feature["code"] for feature in features]
    duplicate_codes = sorted(code for code, count in Counter(codes).items() if count > 1)
    texture_features = [f for f in features if f["family"] in TEXTURE_FAMILIES]
    non_texture_features = [f for f in features if f["family"] not in TEXTURE_FAMILIES]

    by_family = {
        family: {
            "features": family_features[family],
            "rows": family_rows[family],
        }
        for family in sorted(family_features)
    }

    checks = {
        "referenceTables": {
            "expected": EXPECTED_REFERENCE_TABLES,
            "actual": extracted["referenceTables"],
            "passed": extracted["referenceTables"] == EXPECTED_REFERENCE_TABLES,
        },
        "morphologyTables": {
            "expected": EXPECTED_MORPHOLOGY_TABLES,
            "actual": extracted["morphologyTables"],
            "passed": extracted["morphologyTables"] == EXPECTED_MORPHOLOGY_TABLES,
        },
        "featuresWith2DValues": {
            "expected": EXPECTED_FEATURES,
            "actual": len(features),
            "passed": len(features) == EXPECTED_FEATURES,
        },
        "rows": {
            "expected": EXPECTED_ROWS,
            "actual": sum(len(feature["rows"]) for feature in features),
            "passed": sum(len(feature["rows"]) for feature in features) == EXPECTED_ROWS,
        },
        "unparsedValues": {
            "expected": 0,
            "actual": len(extracted["unparsed"]),
            "passed": not extracted["unparsed"],
        },
        "missingMetadata": {
            "expected": 0,
            "actual": len(extracted["missing"]),
            "passed": not extracted["missing"],
        },
    }

    source_bytes = source_path.read_bytes()
    return {
        "schemaVersion": "1.0",
        "title": "IBSI 2D reference values",
        "description": (
            "2D subset of the reference values published in Chapter 3 of the "
            "IBSI reference manual. Values are copied from the rendered tables "
            "without numerical modification."
        ),
        "source": {
            "organization": "Image Biomarker Standardisation Initiative (IBSI)",
            "document": "The image biomarker standardisation initiative - Reference manual",
            "version": SOURCE_VERSION,
            "citation": SOURCE_CITATION,
            "radiologyDoi": RADIOLOGY_DOI,
            "urls": {
                "readTheDocs": SOURCE_URL,
                "arxiv": ARXIV_URL,
                "datasets": DATASET_URL,
            },
            "license": {
                "name": SOURCE_LICENSE,
                "url": SOURCE_LICENSE_URL,
            },
            "retrievedAt": retrieved_at,
            "sourceFile": {
                "name": source_path.name,
                "bytes": len(source_bytes),
                "sha256": hashlib.sha256(source_bytes).hexdigest(),
            },
            "extraction": {
                "script": "scripts/import_ibsi_2d_reference_values.py",
                "generatedAt": datetime.now(timezone.utc)
                .isoformat(timespec="seconds")
                .replace("+00:00", "Z"),
            },
        },
        "subset": {
            "name": "IBSI 2D reference values",
            "definition": (
                "Reference values that can be compared with a 2D slice-wise "
                "feature extraction pipeline."
            ),
            "inclusionRules": [
                "Feature tables in Chapter 3 of the IBSI reference manual.",
                (
                    "Texture features: rows whose aggregation method starts "
                    "with '2D', for the digital phantom and configurations A/B."
                ),
                (
                    "Non-texture features: configurations A/B, which the manual "
                    "defines as 2D slice-wise configurations."
                ),
            ],
            "exclusions": [
                (
                    "Morphological features: the manual defines 25 morphological "
                    "features in 3D only and provides no 2D reference values."
                ),
                "Configurations C/D/E and all 3D aggregations.",
                (
                    "Digital phantom rows for non-texture features: these tables "
                    "do not define a 2D aggregation."
                ),
            ],
            "counts": {
                "referenceTablesInManual": extracted["referenceTables"],
                "morphologyTablesExcluded": extracted["morphologyTables"],
                "featuresWith2DValues": len(features),
                "rows": sum(len(feature["rows"]) for feature in features),
                "byFamily": by_family,
                "byDataset": dict(sorted(dataset_rows.items())),
                "byAggregation": dict(sorted(aggregation_rows.items())),
            },
            "duplicateCodes": duplicate_codes,
            "scopeBreakdown": {
                "textureFeatures": {
                    "features": len(texture_features),
                    "rows": sum(len(f["rows"]) for f in texture_features),
                    "referenceDatasets": ["dig. phantom", "config. A", "config. B"],
                },
                "nonTextureFeatures": {
                    "features": len(non_texture_features),
                    "rows": sum(len(f["rows"]) for f in non_texture_features),
                    "referenceDatasets": ["config. A", "config. B"],
                    "note": (
                        "These features have no 2D digital-phantom reference "
                        "values; their 2D values come from the CT configurations "
                        "A/B. A digital-phantom-only run cannot compare them."
                    ),
                },
                "morphologyFeatures": {
                    "features": extracted["morphologyTables"],
                    "rows": 0,
                    "note": "The manual defines no 2D morphological reference values.",
                },
            },
            "toleranceRule": {
                "withTolerance": (
                    "Pass when abs(computed - reference) <= tolerance."
                ),
                "withoutTolerance": (
                    "The manual publishes no tolerance for this row. Report the "
                    "deviation with status 'no_tolerance' and exclude the row "
                    "from pass/fail consistency counts."
                ),
                "rowsWithTolerance": sum(
                    1
                    for feature in features
                    for row in feature["rows"]
                    if row["tolerance"] is not None
                ),
                "rowsWithoutTolerance": tolerance_missing,
                "note": (
                    "This rule is pre-registered before the comparison run and "
                    "does not invent a threshold for rows without a published "
                    "tolerance."
                ),
            },
            "status": {
                "referenceValues": "acquired",
                "comparison": "not_run",
                "platformFeatureMapping": "not_started",
            },
            "notes": [
                (
                    "Codes are not guaranteed to be unique across tables. "
                    "Use code + name + tableIndex when joining to a feature "
                    "mapping."
                ),
                (
                    "Inclusion in this file does not imply that the RadStudio "
                    "operator implements the feature; platform mapping is a "
                    "separate task."
                ),
            ],
        },
        "validation": {
            "passed": all(check["passed"] for check in checks.values()),
            "checks": checks,
            "unparsedValues": extracted["unparsed"],
            "missingMetadata": extracted["missing"],
            "byFamily": by_family,
        },
        "features": features,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source",
        type=Path,
        required=True,
        help="Downloaded 03_Image_features.html from the IBSI ReadTheDocs site.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("packages/compliance/ibsi-2d-reference-values.json"),
        help="Output JSON path.",
    )
    parser.add_argument(
        "--retrieved-at",
        default="2026-11-22T22:14:29+08:00",
        help="Timestamp when the source HTML was retrieved (ISO 8601).",
    )
    args = parser.parse_args()

    extracted = extract(args.source)
    document = build_document(args.source, extracted, args.retrieved_at)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(document, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    counts = document["subset"]["counts"]
    print(f"wrote {args.output}")
    print(
        f"features={counts['featuresWith2DValues']} rows={counts['rows']} "
        f"validation={'passed' if document['validation']['passed'] else 'FAILED'}"
    )
    for name, check in document["validation"]["checks"].items():
        if not check["passed"]:
            print(
                f"  {name}: expected {check['expected']}, actual {check['actual']}",
                file=sys.stderr,
            )
    return 0 if document["validation"]["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
