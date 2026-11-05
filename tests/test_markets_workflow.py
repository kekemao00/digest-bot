"""工作流里和市场账本有关的部分：取 state 分支失败时不记账、保存时账本只能追加、登记指纹的开关。

保存那一步的脚本直接从 daily.yml 里取出来，在临时目录的假 state 分支上跑（需要 bash、git、jq，GitHub 的机器上都有）。
钥匙和记录都是编造的。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml

from digest.cli import _truthy
from digest.markets import panel
from digest.markets.ledger import Ledger, keys_from_env
from digest.markets.settings import load_settings

ROOT = Path(__file__).resolve().parent.parent
KEYS = keys_from_env({"MARKETS_LEDGER_KEY": "fake-workflow-test-key-not-a-secret-0123456789"})
FEB = datetime(2030, 2, 10, tzinfo=timezone.utc)
MAR = datetime(2030, 3, 10, tzinfo=timezone.utc)
GIT = ("git", "-c", "user.name=test", "-c", "user.email=test@example.invalid")


def workflow(name: str) -> dict:
    return yaml.safe_load((ROOT / ".github/workflows" / name).read_text(encoding="utf-8"))


def step(job: dict, name: str) -> dict:
    (found,) = [s for s in job["steps"] if s.get("name") == name]
    return found


# ---- 取 state 分支失败时不记账 ----


def test_failed_state_checkout_reaches_the_panel():
    digest = workflow("daily.yml")["jobs"]["digest"]
    checkout = step(digest, "读取去重状态")
    assert checkout["continue-on-error"] is True and checkout["with"]["ref"] == "state"
    # continue-on-error 让 conclusion 总是 success，要看 outcome
    env = step(digest, "生成并推送简报")["env"]
    assert env[panel.STATE_OK_ENV] == f"${{{{ steps.{checkout['id']}.outcome == 'success' }}}}"


def test_attest_switch_matches_the_program():
    attest = workflow("daily.yml")["jobs"]["attest"]
    match = re.search(r"contains\(fromJSON\('(\[[^']*\])'\), vars\.MARKETS_ENABLED\)", attest["if"])
    assert match, attest["if"]
    values = json.loads(match.group(1))
    assert all(_truthy(v) for v in values)
    assert {v for v in ("1", "true", "yes", "on", "y", "no", "0", "false") if _truthy(v)} == set(values)
    # 登记的是提交到 state 分支的链头，不是这次运行上传的
    checkout = step(attest, "取出 state 分支")
    assert checkout["with"]["ref"] == "state" and checkout["with"]["path"] == ".state"
    assert not any("download-artifact" in s.get("uses", "") for s in attest["steps"])


def test_ci_preview_has_room_for_the_market_budgets():
    # 市场数据源连不上时，板块等满预算、回测试运行等满上限，再加上抓新闻和装依赖
    preview = workflow("ci.yml")["jobs"]["preview"]
    budget = load_settings().budget.seconds
    assert preview["timeout-minutes"] * 60 >= budget + panel.DRY_REPLAY_SECONDS + 300


# ---- 保存时账本只能追加 ----

needs_tools = pytest.mark.skipif(
    not all(shutil.which(tool) for tool in ("bash", "git", "jq", "cmp")), reason="需要 bash、git、jq、cmp"
)


def merge_script() -> str:
    save = workflow("daily.yml")["jobs"]["save"]
    download = next(s for s in save["steps"] if "download-artifact" in s.get("uses", ""))
    merge = step(save, "合并这次的结果")
    assert download["with"]["path"] == merge["env"]["ARTIFACT"]  # 结果不直接盖在 state 分支上
    names = [s.get("name") for s in save["steps"]]
    assert names.index("合并这次的结果") == names.index("提交去重状态和归档") - 1
    return merge["run"]


def git(state: Path, *args: str) -> str:
    return subprocess.run([*GIT, *args], cwd=state, check=True, capture_output=True, text=True).stdout


def state_branch(work: Path, extra: dict[str, str] | None = None) -> Path:
    """假的 state 分支：去重状态和一本有一条 2 月记录的账本。"""
    state = work / ".state"
    (state / "markets").mkdir(parents=True)
    (state / "state.json").write_text('{"made_up": 1}\n', encoding="utf-8")
    Ledger.open(state / "markets", KEYS).append([{"type": "witness", "n": 1}], FEB)
    for name, text in (extra or {}).items():
        (state / name).write_text(text, encoding="utf-8")
    git(state, "init", "-q")
    git(state, "add", "-A")
    git(state, "commit", "-qm", "编造的提交")
    return state


def this_run(state: Path, artifact: Path) -> Path:
    """这次运行上传的结果：在分支的内容上改的（不含 .git），去重状态换成新的。"""
    shutil.copytree(state, artifact, ignore=shutil.ignore_patterns(".git"))
    (artifact / "state.json").write_text('{"made_up": 2}\n', encoding="utf-8")
    return artifact


def merge(work: Path, artifact: Path) -> subprocess.CompletedProcess:
    # GitHub 上 run 的默认 shell 是 bash -e
    env = {**os.environ, "ARTIFACT": str(artifact)}
    proc = subprocess.run(["bash", "-e", "-c", merge_script()], cwd=work, env=env, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    assert json.loads((work / ".state" / "state.json").read_text(encoding="utf-8")) == {"made_up": 2}
    return proc


def files(directory: Path) -> dict[str, bytes]:
    return {p.name: p.read_bytes() for p in sorted(directory.iterdir()) if p.is_file()}


def fresh_ledger(markets: Path) -> None:
    """没取到 state 分支时会发生的：在空目录里新建一本账（新的盐和钥匙编号）。"""
    shutil.rmtree(markets)
    Ledger.open(markets, KEYS).append([{"type": "witness", "n": 9}], FEB)


def truncated(markets: Path) -> None:
    (month,) = markets.glob("*.jsonl.enc")
    month.write_bytes(month.read_bytes()[:-3])


def head_back(markets: Path) -> None:
    Ledger.open(markets, KEYS).append([{"type": "witness", "n": 2}], FEB)
    head = json.loads((markets / "head.json").read_text(encoding="utf-8"))
    (markets / "head.json").write_text(json.dumps({**head, "seq": 0}), encoding="utf-8")


def meta_gone(markets: Path) -> None:
    (markets / "ledger.meta.json").unlink()


@needs_tools
@pytest.mark.parametrize("change", [fresh_ledger, truncated, head_back, meta_gone])
def test_ledger_that_is_not_an_append_keeps_the_branch(tmp_path, change):
    state = state_branch(tmp_path)
    before = files(state / "markets")
    artifact = this_run(state, tmp_path / "artifact")
    change(artifact / "markets")
    proc = merge(tmp_path, artifact)
    assert "::warning::" in proc.stdout
    # 分支上的账本原样保留，还能用原来的钥匙打开；去重状态照常用这次的
    assert files(state / "markets") == before
    assert git(state, "status", "--porcelain", "markets/") == ""
    assert Ledger.open(state / "markets", KEYS).seq == 1
    assert "made_up" not in proc.stdout + proc.stderr


@needs_tools
def test_appended_ledger_is_saved_and_deletions_reach_the_branch(tmp_path):
    state = state_branch(tmp_path, {"markets/stray.txt": "编造的多余文件"})
    artifact = this_run(state, tmp_path / "artifact")
    Ledger.open(artifact / "markets", KEYS).append([{"type": "witness", "n": 2}], MAR)
    (artifact / "markets" / "stray.txt").unlink()  # 账本自检删掉的
    proc = merge(tmp_path, artifact)
    assert "::warning::" not in proc.stdout
    assert Ledger.open(state / "markets", KEYS).seq == 2
    assert not (state / "markets" / "stray.txt").exists()


@needs_tools
def test_run_without_ledger_leaves_the_branch_ledger_alone(tmp_path):
    # 没配钥匙、或者没取到 state 分支所以没记账：这次的结果里没有 markets/，分支上的不动
    state = state_branch(tmp_path)
    before = files(state / "markets")
    artifact = this_run(state, tmp_path / "artifact")
    shutil.rmtree(artifact / "markets")
    merge(tmp_path, artifact)
    assert files(state / "markets") == before


@needs_tools
def test_first_run_without_state_branch(tmp_path):
    artifact = tmp_path / "artifact"
    (artifact / "markets").mkdir(parents=True)
    (artifact / "state.json").write_text('{"made_up": 2}\n', encoding="utf-8")
    Ledger.open(artifact / "markets", KEYS).append([{"type": "witness"}], FEB)
    proc = merge(tmp_path, artifact)
    assert "::warning::" not in proc.stdout
    assert Ledger.open(tmp_path / ".state" / "markets", KEYS).seq == 1
