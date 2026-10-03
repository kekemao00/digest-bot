from __future__ import annotations

import json
import logging
from dataclasses import replace
from datetime import datetime, timezone

import httpx
import pytest

from conftest import fake_llm, llm_entries, llm_reply
from digest.llm import ChatClient, Endpoint, Enricher, LLMError, build_enricher, chat_url, parse_json
from digest.models import Item

KEY = "sk-should-never-be-logged"
HOST = "private-llm.internal.example"
ENDPOINT = Endpoint(url=f"https://{HOST}/v1/chat/completions", key=KEY, model="test-model")


def make_item(n: int, title: str, summary: str | None = None) -> Item:
    return Item(source="hn", source_name="Hacker News", section="tech", id=str(n), title=title,
                url=f"https://example.com/{n}", published_at=datetime.now(timezone.utc), summary=summary)


def enricher(config, handler, **settings) -> tuple[Enricher, list[httpx.Request]]:
    requests: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return handler(request)

    config = replace(config, llm=replace(config.llm, **settings))
    client = httpx.Client(transport=httpx.MockTransport(record))
    chat = ChatClient(client, ENDPOINT, config.llm, sleep=lambda s: None)
    return Enricher(chat, config), requests


def test_chat_url():
    assert chat_url("https://api.example.com/v1") == "https://api.example.com/v1/chat/completions"
    assert chat_url("https://api.example.com/v1/") == "https://api.example.com/v1/chat/completions"
    assert chat_url("https://api.example.com/v1/chat/completions") == "https://api.example.com/v1/chat/completions"
    # 本机调试（如 Ollama）可以用 http
    assert chat_url("http://localhost:11434/v1") == "http://localhost:11434/v1/chat/completions"
    for bad in ("http://api.example.com/v1", "ftp://api.example.com", "api.example.com/v1",
                "https://user:pass@api.example.com/v1"):
        with pytest.raises(LLMError):
            chat_url(bad)


def test_disabled_without_complete_env(config, caplog):
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(500)))
    assert build_enricher(client, config, env={"LLM_BASE_URL": "https://x.test/v1", "LLM_API_KEY": "k"}) is None
    assert build_enricher(client, config, env={}) is None
    env = {"LLM_BASE_URL": f"http://{HOST}/v1", "LLM_API_KEY": KEY, "LLM_MODEL": "m"}
    caplog.set_level(logging.INFO)
    assert build_enricher(client, config, env=env) is None
    assert "https" in caplog.text and HOST not in caplog.text


def test_parse_json_tolerates_wrappers():
    assert parse_json('<think>{"x": 0}</think>\n```json\n{"items": [1]}\n```') == {"items": [1]}
    assert parse_json('好的：{"items": []} 以上') == {"items": []}
    with pytest.raises(ValueError):
        parse_json("没有 JSON")


def test_review_enriches_and_rejects(config):
    items = [
        make_item(1, "Show HN: An open-source LLM inference server", "Runs a 70B model on a laptop."),
        make_item(2, "Ask HN: What's your favorite underrated book?"),
        make_item(3, "Rust 2.0 roadmap (draft)"),
    ]
    tool, requests = enricher(config, fake_llm)
    verdicts = tool.review(items)

    assert verdicts == {"hn:2": "大模型评分 3/10（观点）"}
    first = items[0]
    assert first.title_zh == "中文：Show HN: An"
    assert first.one_liner == "一句话说明 1"
    assert (first.kind, first.quality, first.llm_focus, first.topic) == ("新闻", 7, ["ai"], "AI")
    assert items[2].llm_focus == [] and items[2].topic == "软件"

    request = requests[0]
    assert request.headers["Authorization"] == f"Bearer {KEY}"
    body = json.loads(request.content)
    assert body["model"] == "test-model" and body["temperature"] == 0.2
    assert "response_format" not in body  # 默认不发，部分接口不支持
    _, entries = llm_entries(request)
    assert entries[0]["summary"] == "Runs a 70B model on a laptop."


def test_reject_kinds(config):
    item = make_item(1, "How Acme scaled with our platform")
    tool, _ = enricher(config, lambda r: llm_reply({"items": [{"id": "1", "kind": "营销", "score": 6}]}))
    assert tool.review([item]) == {"hn:1": "大模型判断为营销"}


def test_model_output_is_sanitized(config):
    item = make_item(1, "Some title")
    long_summary = "很长的一句话" * 30
    reply = {"items": [
        {"id": "1", "title_zh": "点击\nhttps://evil.example/x 领取 www.evil.example 奖励", "summary": long_summary,
         "kind": "胡编的类型", "score": 12, "focus": ["ai", "crypto"], "topic": "<b>AI</b>", "subject": "  "},
        {"id": "99", "title_zh": "不存在的条目"},
        "不是对象",
    ]}
    tool, _ = enricher(config, lambda r: llm_reply(reply))
    assert tool.review([item]) == {}
    assert item.title_zh == "点击 领取 奖励"
    assert "\n" not in item.title_zh and "http" not in item.title_zh
    assert len(item.one_liner) == 80 and item.one_liner.endswith("…")
    assert item.kind is None and item.topic is None
    assert item.quality == 10
    assert item.llm_focus == ["ai"]
    assert item.subject is None


def test_same_title_is_not_a_translation(config):
    item = make_item(1, "anthropics/claude-code")
    tool, _ = enricher(config, lambda r: llm_reply({"items": [{"id": "1", "title_zh": "anthropics/claude-code"}]}))
    tool.review([item])
    assert item.title_zh is None


def test_invalid_json_is_retried_once(config):
    answers = iter([llm_reply("抱歉，我无法输出 JSON"), llm_reply({"items": [{"id": "1", "title_zh": "标题", "score": 6}]})])
    item = make_item(1, "Title")
    tool, requests = enricher(config, lambda r: next(answers))
    tool.review([item])
    assert len(requests) == 2 and item.title_zh == "标题"


def test_garbage_output_degrades_to_original(config):
    item = make_item(1, "Title")
    tool, requests = enricher(config, lambda r: llm_reply("not json"))
    assert tool.review([item]) == {}
    assert item.title_zh is None and item.quality is None
    assert len(requests) == 2 and tool.failed_batches == 1


def test_bad_request_retries_without_optional_params(config):
    def handler(request):
        body = json.loads(request.content)
        if "temperature" in body or "max_tokens" in body:
            return httpx.Response(400, json={"error": "unsupported parameter: temperature"})
        return llm_reply({"items": [{"id": "1", "title_zh": "标题"}]})

    item = make_item(1, "Title")
    tool, requests = enricher(config, handler, json_mode=True)
    tool.review([item])
    assert item.title_zh == "标题"
    assert "response_format" in json.loads(requests[0].content)
    assert set(json.loads(requests[1].content)) == {"model", "messages"}


def test_repeated_failures_stop_calling(config):
    items = [make_item(n, f"Story {n}") for n in range(30)]
    tool, requests = enricher(config, lambda r: httpx.Response(503), batch_size=12)
    assert tool.review(items) == {}
    # 每批重试一次；连续两批失败后，第三批不再请求
    assert len(requests) == 4
    assert tool.chat.disabled and tool.failed_batches == 3
    assert all(i.title_zh is None for i in items)


def test_time_budget(config):
    clock = iter([0.0, 1000.0])
    client = httpx.Client(transport=httpx.MockTransport(fake_llm))
    chat = ChatClient(client, ENDPOINT, config.llm, clock=lambda: next(clock))
    with pytest.raises(LLMError, match="总时长"):
        chat.complete("system", "user")
    assert chat.calls == 0


def test_secrets_never_logged(config, caplog):
    from digest.cli import setup_logging

    setup_logging()  # 和正式运行一样：httpx 的请求日志（含完整地址）不输出
    caplog.set_level(logging.DEBUG)

    def handler(request):
        if b"Story 1" in request.content:
            raise httpx.ConnectError(f"cannot connect to {request.url}")
        return httpx.Response(401, json={"error": f"bad key {KEY}"})

    tool, _ = enricher(config, handler, batch_size=1)
    tool.review([make_item(1, "Story 1"), make_item(2, "Story 2")])
    assert tool.failed_batches == 2
    assert KEY not in caplog.text
    assert HOST not in caplog.text
    assert "ConnectError" in caplog.text and "HTTP 401" in caplog.text
    assert KEY not in repr(ENDPOINT)


def test_highlights(config):
    items = [make_item(n, f"Story {n}") for n in range(1, 6)]
    reply = {"highlights": [
        {"n": 4, "text": "第一条要点 https://evil.example"},
        {"n": 4, "text": "重复的编号"},
        {"n": 0, "text": "越界"},
        {"n": 99, "text": "越界"},
        {"n": True, "text": "布尔值"},
        {"n": 2, "text": ""},
        {"n": 1, "text": "第二条要点"},
        {"n": 5, "text": "第三条要点"},
        {"n": 3, "text": "超出条数"},
    ]}
    tool, _ = enricher(config, lambda r: llm_reply(reply))
    result = tool.highlights(items)
    assert [(h.key, h.text) for h in result] == [("hn:4", "第一条要点"), ("hn:1", "第二条要点"), ("hn:5", "第三条要点")]


def test_no_highlights_for_short_digest(config):
    tool, requests = enricher(config, fake_llm)
    assert tool.highlights([make_item(n, f"Story {n}") for n in range(3)]) == []
    assert requests == []


def test_unexpected_output_never_raises(config):
    item = make_item(1, "Title")
    # NaN 评分、id 是对象等奇怪输出：不抛异常，不淘汰
    tool, _ = enricher(config, lambda r: llm_reply('{"items": [{"id": "1", "score": NaN}, {"id": {"x": 1}}]}'))
    assert tool.review([item]) == {}
    assert item.quality is None


def test_spacing_and_trailing_period(config):
    item = make_item(1, "Some title")
    reply = {"items": [{"id": "1", "title_zh": "Redis作者推出ds4：本地运行LLM", "summary": "新闻使Qwen下单占比变化48个百分点。"}]}
    tool, _ = enricher(config, lambda r: llm_reply(reply))
    tool.review([item])
    assert item.title_zh == "Redis 作者推出 ds4：本地运行 LLM"
    assert item.one_liner == "新闻使 Qwen 下单占比变化 48 个百分点"
