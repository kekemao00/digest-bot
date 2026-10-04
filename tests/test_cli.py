from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import pytest

import digest.cli as cli

TOKEN = "tok-should-never-be-logged"


class Network(list):
    """记录发到钉钉的消息；routes 可以给其他域名挂上假响应。"""

    routes: dict


@pytest.fixture
def network(monkeypatch, hn_payload, tmp_path):
    sent = Network()
    sent.routes = {}
    for hit in hn_payload["hits"]:
        hit["created_at_i"] = int(time.time()) - 3600

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "hn.algolia.com":
            return httpx.Response(200, json=hn_payload)
        if route := sent.routes.get(request.url.host):
            return route(request)
        if request.url.host == "oapi.dingtalk.com":
            sent.append(json.loads(request.content))
            return httpx.Response(200, json={"errcode": 0, "errmsg": "ok"})
        return httpx.Response(404)  # 其他信息源在这些测试里不可用

    monkeypatch.setattr(cli, "make_client", lambda: httpx.Client(transport=httpx.MockTransport(handler)))
    monkeypatch.setenv("DINGTALK_WEBHOOK", f"https://oapi.dingtalk.com/robot/send?access_token={TOKEN}")
    monkeypatch.setenv("DINGTALK_SECRET", "SEC")
    monkeypatch.delenv("DIGEST_DRY_RUN", raising=False)
    monkeypatch.chdir(tmp_path)  # 默认 state 路径是相对路径，放到临时目录里
    return sent


def test_sends_without_leaking_token(network, caplog, capsys):
    caplog.set_level(logging.DEBUG)
    assert cli.main([]) == 0
    assert len(network) == 1
    assert network[0]["msgtype"] == "markdown"
    logged = caplog.text + capsys.readouterr().out
    assert TOKEN not in logged


def test_dry_run_does_not_send(network, tmp_path, monkeypatch):
    monkeypatch.setenv("DIGEST_DRY_RUN", "true")
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    assert cli.main(["--out", str(tmp_path / "out")]) == 0
    assert network == []
    assert "钉钉消息预览（未发送）" in summary.read_text(encoding="utf-8")
    assert (tmp_path / "out" / "archive.md").exists()


def test_all_sources_failing_is_an_error(monkeypatch):
    monkeypatch.setattr(
        cli, "make_client", lambda: httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(503)))
    )
    assert cli.main(["--dry-run"]) == 1


LLM_KEY = "sk-llm-should-never-be-logged"


@pytest.fixture
def llm_env(monkeypatch):
    monkeypatch.setenv("LLM_BASE_URL", "https://llm.test/v1")
    monkeypatch.setenv("LLM_API_KEY", LLM_KEY)
    monkeypatch.setenv("LLM_MODEL", "test-model")


def test_llm_enriches_message(network, llm_env, caplog, capsys):
    from conftest import fake_llm

    network.routes["llm.test"] = fake_llm
    caplog.set_level(logging.DEBUG)
    assert cli.main([]) == 0
    text = network[0]["markdown"]["text"]
    assert "今日要点" in text and "中文：" in text
    assert "underrated book" not in text  # 假模型给 Ask HN 闲聊打了低分
    logged = caplog.text + capsys.readouterr().out
    assert "大模型：送审" in logged
    assert LLM_KEY not in logged and "llm.test" not in logged


def test_llm_failure_still_sends(network, llm_env, monkeypatch):
    calls = []
    network.routes["llm.test"] = lambda request: calls.append(request) or httpx.Response(500)
    monkeypatch.setattr("digest.llm.time.sleep", lambda s: None)
    assert cli.main([]) == 0
    text = network[0]["markdown"]["text"]
    assert "今日要点" not in text and "Show HN" in text
    assert len(calls) == 4  # 两批各重试一次后停用，不再请求


def test_archive_written_and_linked(network, tmp_path, monkeypatch):
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/digest-bot")
    assert cli.main(["--archive-dir", str(tmp_path / "archive")]) == 0
    files = list((tmp_path / "archive").glob("*/*.md"))
    assert len(files) == 1 and files[0].read_text(encoding="utf-8").startswith("# 每日简报")
    year, day = files[0].parent.name, files[0].stem
    link = f"https://github.com/owner/digest-bot/blob/state/archive/{year}/{day}.md"
    assert f"[完整版与落选条目]({link})" in network[0]["markdown"]["text"]
    # 推送成功后更新归档目录页和逐条清单
    assert f"]({year}/{day}.md)" in (tmp_path / "archive" / "README.md").read_text(encoding="utf-8")
    assert (tmp_path / "archive" / "items.jsonl").read_text(encoding="utf-8").count("\n") >= 1


def test_dry_run_writes_no_archive(network, tmp_path):
    assert cli.main(["--dry-run", "--archive-dir", str(tmp_path / "archive")]) == 0
    assert not (tmp_path / "archive").exists()


def test_scheduled_run_sends_once_per_day(network, monkeypatch):
    monkeypatch.setenv("GITHUB_EVENT_NAME", "schedule")
    assert cli.main([]) == 0
    assert cli.main([]) == 0  # 同一天第二次定时触发：跳过
    assert len(network) == 1
    monkeypatch.setenv("GITHUB_EVENT_NAME", "workflow_dispatch")
    assert cli.main([]) == 0  # 手动运行照常发送（去重后剩下的内容）
    assert len(network) == 2


def test_external_scheduler_sends_once_per_day(network, monkeypatch):
    # 外部定时服务通过 workflow_dispatch 触发并带上 scheduled=true，和 GitHub 自带的定时任务一样每天只推一次
    monkeypatch.setenv("GITHUB_EVENT_NAME", "workflow_dispatch")
    monkeypatch.setenv("DIGEST_SCHEDULED", "true")
    assert cli.main([]) == 0
    assert cli.main([]) == 0  # 外部服务重试：跳过
    assert len(network) == 1
    monkeypatch.setenv("GITHUB_EVENT_NAME", "schedule")
    monkeypatch.setenv("DIGEST_SCHEDULED", "")  # GitHub 自带的定时任务晚到，inputs 为空：同样跳过
    assert cli.main([]) == 0
    assert len(network) == 1
    monkeypatch.setenv("GITHUB_EVENT_NAME", "workflow_dispatch")
    monkeypatch.setenv("DIGEST_SCHEDULED", "false")  # 手动运行照常发送
    assert cli.main([]) == 0
    assert len(network) == 2


def test_skip_tells_workflow_not_to_save(network, tmp_path, monkeypatch):
    # 跳过时在 Summary 里写明，并告诉工作流不用上传和保存；正式推送时不输出 skipped，照常保存
    summary, output = tmp_path / "summary.md", tmp_path / "output.txt"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    monkeypatch.setenv("DIGEST_SCHEDULED", "true")
    assert cli.main([]) == 0
    assert len(network) == 1
    assert not output.exists()
    summary.unlink()
    assert cli.main([]) == 0
    assert len(network) == 1
    assert output.read_text(encoding="utf-8") == "skipped=true\n"
    assert summary.read_text(encoding="utf-8").startswith("## 已推送过，跳过")


SHANGHAI = ZoneInfo("Asia/Shanghai")
MORNING = datetime(2026, 10, 4, 7, 47, tzinfo=SHANGHAI)
EVENING = datetime(2026, 10, 4, 19, 47, tzinfo=SHANGHAI)
LINK = re.compile(r"\]\((https?://[^)]+)\)")


def at(monkeypatch, when):
    monkeypatch.setattr(cli, "now_in", lambda timezone: when)


def test_evening_edition_sends_once_apart_from_morning(network, monkeypatch, tmp_path):
    monkeypatch.setenv("DIGEST_SCHEDULED", "true")
    at(monkeypatch, MORNING)
    assert cli.main([]) == 0
    # GitHub 自带的晚间定时任务：按 cron 的钟点判断是晚间版
    at(monkeypatch, EVENING)
    monkeypatch.setenv("GITHUB_EVENT_NAME", "schedule")
    monkeypatch.setenv("DIGEST_CRON", "47 11 * * *")
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/digest-bot")
    assert cli.main(["--archive-dir", str(tmp_path / "archive")]) == 0
    assert len(network) == 2
    morning, evening = (m["markdown"] for m in network)
    assert evening["title"].startswith("晚间简报") and evening["text"].startswith("### 晚间简报 · 10月4日")
    # 早上推过的不再出现
    assert set(LINK.findall(evening["text"])).isdisjoint(LINK.findall(morning["text"]))
    assert (tmp_path / "archive" / "2026" / "10-04-evening.md").exists()
    assert "(https://github.com/owner/digest-bot/blob/state/archive/2026/10-04-evening.md)" in evening["text"]
    # 外部定时服务的晚间触发晚到：今晚已处理过，跳过；早间版的触发也不受晚间版影响，照样跳过
    monkeypatch.setenv("GITHUB_EVENT_NAME", "workflow_dispatch")
    monkeypatch.setenv("DIGEST_EDITION", "evening")
    assert cli.main([]) == 0
    monkeypatch.setenv("DIGEST_EDITION", "morning")
    assert cli.main([]) == 0
    assert len(network) == 2
    saved = json.loads(Path(".state/state.json").read_text(encoding="utf-8"))
    assert saved["last_sent"] == saved["last_evening"] == "2026-10-04"


def test_quiet_evening_is_not_sent(network, monkeypatch, hn_payload, tmp_path):
    from test_sources import anthropic_handler

    hn_payload["hits"] = hn_payload["hits"][:1]
    network.routes["www.anthropic.com"] = anthropic_handler({"/news/claude-frontier-academy": "Frontier Academy"})
    known = ["https://www.anthropic.com/news/barclays-scales-claude"]
    state_path = Path(".state/state.json")
    state_path.parent.mkdir()
    state_path.write_text(json.dumps({"version": 1, "pages": {"anthropic": known}}), encoding="utf-8")
    output = tmp_path / "output.txt"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    monkeypatch.setenv("DIGEST_EDITION", "evening")
    monkeypatch.setenv("DIGEST_SCHEDULED", "true")
    at(monkeypatch, EVENING)

    assert cli.main(["--archive-dir", str(tmp_path / "archive")]) == 0
    assert network == []
    assert not (tmp_path / "archive").exists()
    assert not output.exists()  # 要把“今晚已处理”存下来，不能跳过保存
    saved = json.loads(state_path.read_text(encoding="utf-8"))
    assert saved["last_evening"] == "2026-10-04" and saved["last_sent"] is None
    # 今晚没推送，博客上新看到的文章不记成已见过，留给第二天早上
    assert saved["pages"]["anthropic"] == known
    # 当晚的兜底触发不再重跑
    assert cli.main([]) == 0
    assert output.read_text(encoding="utf-8") == "skipped=true\n"


def test_late_evening_trigger_is_skipped(network, monkeypatch):
    monkeypatch.setenv("DIGEST_EDITION", "evening")
    monkeypatch.setenv("DIGEST_SCHEDULED", "true")
    for late in (datetime(2026, 10, 4, 23, 5, tzinfo=SHANGHAI), datetime(2026, 10, 5, 0, 37, tzinfo=SHANGHAI)):
        at(monkeypatch, late)
        assert cli.main([]) == 0
    assert network == []
    monkeypatch.setenv("DIGEST_SCHEDULED", "false")  # 手动运行不受时间限制
    assert cli.main([]) == 0
    assert len(network) == 1


def test_disabled_evening_is_skipped(network, monkeypatch):
    from dataclasses import replace

    config = cli.load_config()
    monkeypatch.setattr(cli, "load_config", lambda: replace(config, evening=replace(config.evening, enabled=False)))
    monkeypatch.setenv("DIGEST_EDITION", "evening")
    monkeypatch.setenv("DIGEST_SCHEDULED", "true")
    at(monkeypatch, EVENING)
    assert cli.main([]) == 0
    assert network == []


def test_unknown_edition_is_an_error(monkeypatch):
    monkeypatch.setenv("DIGEST_EDITION", "noon")
    with pytest.raises(SystemExit):
        cli.main(["--dry-run"])


def test_missing_webhook_fails_before_fetching(network, monkeypatch):
    fetched = []
    network.routes["hn.algolia.com"] = lambda request: fetched.append(request) or httpx.Response(500)
    monkeypatch.delenv("DINGTALK_WEBHOOK")
    assert cli.main([]) == 1
    assert fetched == [] and network == []
