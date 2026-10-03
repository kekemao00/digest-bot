from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "release_notes.py"

spec = importlib.util.spec_from_file_location("release_notes", SCRIPT)
release_notes = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release_notes)


def test_current_version_has_notes(tmp_path):
    # 改了版本号却没写更新日志时，发布流程会中止；这里提前在 CI 里拦下
    out = tmp_path / "notes.md"
    result = subprocess.run([sys.executable, str(SCRIPT), str(out)], capture_output=True, text=True, check=True)
    version = result.stdout.strip()
    assert f"## v{version} " in (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    assert out.read_text(encoding="utf-8").strip()


def test_notes_stop_at_next_version():
    changelog = "# 更新日志\n\n## v1.1.0 · 2026-11-01\n\n### 新增\n\n- 新功能\n\n## v1.0.0 · 2026-10-04\n\n- 第一版\n"
    assert release_notes.notes(changelog, "1.1.0") == "### 新增\n\n- 新功能\n"
    assert release_notes.notes(changelog, "1.0.0") == "- 第一版\n"


def test_missing_section_is_an_error():
    with pytest.raises(ValueError, match="没有 v1.0.1"):
        release_notes.notes("## v1.0.0\n\n- 第一版\n", "1.0.1")
    with pytest.raises(ValueError, match="没有 v1.0"):
        release_notes.notes("## v1.0.0\n\n- 第一版\n", "1.0")  # 不能匹配到 v1.0.0
    with pytest.raises(ValueError, match="是空的"):
        release_notes.notes("## v1.0.0\n\n## v0.9.0\n\n- 旧版\n", "1.0.0")


def test_version_format():
    assert release_notes.version('[project]\nversion = "1.0.0"\n') == "1.0.0"
    with pytest.raises(ValueError):
        release_notes.version('[project]\nversion = "1.0.0; rm -rf /"\n')
