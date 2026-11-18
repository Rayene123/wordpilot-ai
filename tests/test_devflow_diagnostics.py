from __future__ import annotations

import importlib.util
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "devflow_diagnostics.py"
SPEC = importlib.util.spec_from_file_location("devflow_diagnostics", SCRIPT)
assert SPEC and SPEC.loader
diagnostics = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(diagnostics)


def test_missing_codex_is_reported_without_requiring_it(monkeypatch, tmp_path):
    monkeypatch.setattr(diagnostics.importlib.util, "find_spec", lambda _: object())
    result = diagnostics.run_diagnostics(root=tmp_path, environ={}, which=lambda _: None)

    assert result["document_app_status"] == "passed"
    assert result["devflow_status"] == "failed"
    assert result["codex_cli"]["status"] == "failed"
    assert "Codex CLI" in result["codex_cli"]["detail"]
    assert result["codex_authentication"]["status"] == "skipped"


def test_codex_discovery_uses_path_fallback():
    resolved, error = diagnostics._resolve_codex({}, lambda name: "/tools/codex" if name == "codex" else None)

    assert resolved == "/tools/codex"
    assert error is None


def test_explicit_codex_path_is_used(monkeypatch, tmp_path):
    executable = tmp_path / "codex.exe"
    executable.write_text("placeholder", encoding="utf-8")
    resolved, error = diagnostics._resolve_codex(
        {"CODEX_CLI_PATH": str(executable)}, lambda _: None,
    )

    assert resolved == str(executable.resolve())
    assert error is None


def test_diagnostic_root_is_derived_from_cloned_repository():
    assert diagnostics.REPOSITORY_ROOT == SCRIPT.resolve().parents[1]
