from __future__ import annotations

import json
import logging
import time

import httpx
import pytest

import digest.cli as cli

TOKEN = "tok-should-never-be-logged"


@pytest.fixture
def network(monkeypatch, hn_payload, tmp_path):
    sent = []
    for hit in hn_payload["hits"]:
        hit["created_at_i"] = int(time.time()) - 3600

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "hn.algolia.com":
            return httpx.Response(200, json=hn_payload)
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
