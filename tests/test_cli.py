from __future__ import annotations

import json
import logging
import time

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
    assert "钉钉消息预览" in summary.read_text(encoding="utf-8")
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
