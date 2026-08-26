from __future__ import annotations

import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "skills" / "smetchik" / "assets"


@pytest.mark.parametrize(
    "schema_name",
    [
        "smetchik.result.v1.schema.json",
        "smetchik.corrections.v1.schema.json",
    ],
)
def test_published_schema_compiles_with_strict_ajv2020(schema_name: str) -> None:
    schema_path = ASSETS / schema_name
    script = f"""
const fs = require("node:fs");
const Ajv2020 = require("ajv/dist/2020");
const schema = JSON.parse(fs.readFileSync(process.argv[1], "utf8"));
new Ajv2020({{strict: true, validateFormats: false}}).compile(schema);
"""

    completed = subprocess.run(
        ["node", "-e", script, str(schema_path)],
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=10,
    )

    assert completed.returncode == 0, completed.stderr
