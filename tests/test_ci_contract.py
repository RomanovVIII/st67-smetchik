from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_public_ci_covers_supported_systems_and_python_versions() -> None:
    workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    test_job = workflow.split("\n  package-smoke:", maxsplit=1)[0]

    assert "ubuntu-latest" in workflow
    assert "macos-latest" in workflow
    for version in ("3.11", "3.12", "3.13"):
        assert version in workflow
    assert "npm ci" in workflow
    assert "runtime/requirements-ci.lock" in workflow
    assert "--require-hashes" in workflow
    assert "--only-binary=:all:" in workflow
    assert "pytest -p no:cacheprovider -q" in workflow
    assert "validate_plugin.py" in workflow
    assert "quick_validate.py" in workflow
    assert "Install Linux document tools" in test_job
    assert "Install macOS document tools" in test_job
    assert "poppler-utils tesseract-ocr tesseract-ocr-rus" in test_job
    assert "brew install poppler tesseract-lang" in test_job
    assert "github/codeql-action/init@5ba2889ada762081db2c4f32a729827dce632c7b" in workflow
    assert "github/codeql-action/analyze@5ba2889ada762081db2c4f32a729827dce632c7b" in workflow


def test_public_ci_runs_runtime_package_smoke_without_publishing() -> None:
    workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")

    assert "runtime/bootstrap.py" in workflow
    assert "--verify-only" in workflow
    assert "permissions:" in workflow
    assert "contents: read" in workflow
    assert "pull-requests: write" not in workflow
    assert "id-token: write" not in workflow
    assert "uses: actions/checkout@v" not in workflow
    assert "uses: actions/setup-python@v" not in workflow
    assert "uses: actions/setup-node@v" not in workflow
    assert "uses: gitleaks/gitleaks-action@v" not in workflow
    assert "uses: github/codeql-action/" in workflow
    assert "uses: github/codeql-action/init@v" not in workflow
    assert "uses: github/codeql-action/analyze@v" not in workflow
