from __future__ import annotations

import json
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_public_release_identity_preserves_public_skill_and_runtime() -> None:
    manifest = json.loads(
        (ROOT / ".codex-plugin/plugin.json").read_text(encoding="utf-8")
    )
    skill = (ROOT / "skills/smetchik/SKILL.md").read_text(encoding="utf-8")
    agent = (ROOT / "skills/smetchik/agents/openai.yaml").read_text(
        encoding="utf-8"
    )
    bootstrap = (ROOT / "runtime/bootstrap.py").read_text(encoding="utf-8")

    assert manifest["name"] == "st67-smetchik"
    assert manifest["version"] == "0.2.0"
    assert manifest["author"]["name"] == "Studio 67"
    assert manifest["license"] == "MIT"
    assert manifest["homepage"] == "https://github.com/RomanovVIII/st67-smetchik"
    assert manifest["repository"] == "https://github.com/RomanovVIII/st67-smetchik"
    assert manifest["interface"]["displayName"] == "ST67 Сметчик"
    assert manifest["interface"]["developerName"] == "Studio 67"
    assert "name: smetchik" in skill
    assert "$smetchik" in agent
    assert ".local/share/smetchik" in bootstrap


def test_correction_route_is_documented_consistently() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    skill = (ROOT / "skills/smetchik/SKILL.md").read_text(encoding="utf-8")
    evidence = (
        ROOT / "skills/smetchik/references/evidence-and-report.md"
    ).read_text(encoding="utf-8")
    combined = "\n".join((readme, skill, evidence))

    assert "smetchik_cli.py" in readme and " correct source.xlsx" in readme
    assert "smetchik.corrections.v1" in readme
    assert "expected_old" in combined
    assert "confirmed" in combined
    assert "disputed" in combined
    assert "create-only" in combined
    assert "Формулы как новое значение" in combined
    assert "XLS/PDF/XML/GGE/MGE" in combined


def test_local_venvs_are_ignored_without_hiding_runtime_sources() -> None:
    def is_ignored(path: str) -> bool:
        completed = subprocess.run(
            ["git", "check-ignore", "--quiet", "--no-index", path],
            cwd=ROOT,
            check=False,
        )
        return completed.returncode == 0

    assert is_ignored("venv/bin/python")
    assert is_ignored("runtime/venv/bin/python")
    assert not is_ignored("runtime/bootstrap.py")
    assert not is_ignored("runtime/requirements.lock")


def test_runtime_docs_distinguish_sources_from_installed_venv() -> None:
    runtime_readme = (ROOT / "runtime/README.md").read_text(encoding="utf-8")
    install = (ROOT / "INSTALL.md").read_text(encoding="utf-8")

    assert "исходный каталог `runtime/`" in runtime_readme
    assert "установленное виртуальное окружение" in runtime_readme
    assert "Runtime не входит в Git" not in runtime_readme
    assert "исходный каталог `runtime/`" in install
    assert "$HOME/.local/share/smetchik/venv" in install


def test_public_release_has_only_required_root_documents() -> None:
    required_files = {
        "LICENSE",
        "README.md",
        "INSTALL.md",
        "SECURITY.md",
        "CHANGELOG.md",
        "NOTICE.md",
    }

    assert all((ROOT / path).is_file() for path in required_files)
    assert {path.name for path in ROOT.glob("*.md")} == required_files - {"LICENSE"}
    assert "MIT License" in (ROOT / "LICENSE").read_text(encoding="utf-8")
    assert "third-party xsd" in (ROOT / "NOTICE.md").read_text(encoding="utf-8").lower()


def test_public_install_documentation_uses_repo_marketplace_commands() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    install = (ROOT / "INSTALL.md").read_text(encoding="utf-8")
    combined = "\n".join((readme, install))

    assert (
        "codex plugin marketplace add RomanovVIII/st67-smetchik --ref v0.2.0"
        in combined
    )
    assert (
        "codex plugin marketplace add RomanovVIII/st67-smetchik\n" not in combined
    )
    assert "codex plugin add st67-smetchik@st67-smetchik" in combined
    assert "codex plugin remove st67-smetchik" in combined
    assert "новую задачу Codex" in combined
    assert "универсальном каталоге OpenAI" in combined
    assert "macOS" in combined and "Linux" in combined
    assert "runtime/bootstrap.py" in combined
    assert "runtime/schema_manager.py fetch --all" in combined
    assert "runtime/schema_manager.py verify" in combined
    assert "unar" in combined
    assert "Security" in (ROOT / "SECURITY.md").read_text(encoding="utf-8")


def test_public_schema_distribution_excludes_xsd_and_keeps_registry() -> None:
    registry = json.loads((ROOT / "schemas/registry.json").read_text(encoding="utf-8"))
    schema_docs = "\n".join(
        (ROOT / path).read_text(encoding="utf-8")
        for path in ("schemas/README.md", "schemas/NOTICE.md")
    )

    assert not list((ROOT / "schemas").rglob("*.xsd"))
    assert registry["network_schema_resolution"] is False
    assert registry["adapters"]
    assert "SHA-256" in schema_docs
    assert "не распространяются" in schema_docs


def test_repo_marketplace_exposes_the_repository_root_plugin() -> None:
    marketplace = json.loads(
        (ROOT / ".agents/plugins/marketplace.json").read_text(encoding="utf-8")
    )

    assert marketplace["name"] == "st67-smetchik"
    assert marketplace["interface"]["displayName"] == "ST67 Сметчик"
    assert marketplace["plugins"] == [
        {
            "name": "st67-smetchik",
            "source": {"source": "local", "path": "./"},
            "policy": {
                "installation": "AVAILABLE",
                "authentication": "ON_INSTALL",
            },
            "category": "Productivity",
        }
    ]


def test_public_acceptance_gates_match_schema_manager_and_repo_marketplace() -> None:
    acceptance = (
        ROOT / "skills/smetchik/references/acceptance-criteria.md"
    ).read_text(encoding="utf-8")

    assert "v0.2.0" in acceptance
    assert "schema_not_installed" in acceptance
    assert "fetch --all" in acceptance
    assert "st67-smetchik@st67-smetchik" in acceptance
    assert "@personal" not in acceptance


def test_auxiliary_format_docs_match_extension_based_routing() -> None:
    auxiliary = (
        ROOT / "skills/smetchik/references/formats-auxiliary.md"
    ).read_text(encoding="utf-8")

    assert "маршрутизирует по расширению" in auxiliary
    assert "общего magic-type автоопределения не выполняет" in auxiliary
    assert "сигнатуру контейнера" in auxiliary
    assert "unsupported_or_not_yet_extracted" in auxiliary
    assert "формат по содержимому, а не только по расширению" not in auxiliary
