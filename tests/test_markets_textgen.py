from __future__ import annotations

import json
import logging
from dataclasses import replace

import httpx
import pytest

from conftest import llm_reply
from digest.config import LLMSettings
from digest.llm import Endpoint
from digest.markets.textgen import BANNED, MarketLLM, has_banned

# 全是编出来的：假接口、假密钥、虚构的问题和新闻
KEY = "sk-fake-markets-key"
HOST = "llm.markets-test.invalid"
ENDPOINT = Endpoint(url=f"https://{HOST}/v1/chat/completions", key=KEY, model="fake-model")
ENV = {"LLM_BASE_URL": f"https://{HOST}/v1", "LLM_API_KEY": KEY, "LLM_MODEL": "fake-model"}

MARKETS = [
    ("0xaaa1:tok1", "Will the Zorblax Party win the 2031 Ruritania election?"),
    ("0xbbb2:tok2", "Will Example Bank cut its policy rate by March 2031?"),
]
NEWS = ["鲁里塔尼亚举行大选，佐布拉克斯党领先", "示例公司发布新款手机", "示例银行行长谈利率"]


def make(handler, settings: LLMSettings | None = None) -> tuple[MarketLLM, list[httpx.Request]]:
    requests: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return handler(request)

    client = httpx.Client(transport=httpx.MockTransport(record))
    return MarketLLM(client, ENDPOINT, settings or LLMSettings()), requests


def sent(request: httpx.Request) -> tuple[str, dict]:
    """一次请求的系统提示词和用户消息里的 JSON。"""
    messages = json.loads(request.content)["messages"]
    user = messages[1]["content"]
    return messages[0]["content"], json.loads(user[user.index("{") :])


def reply(*entries: dict) -> httpx.Response:
    return llm_reply({"markets": list(entries)})


def test_translates_and_matches_news():
    tool, requests = make(lambda r: reply(
        {"id": "1", "zh": "佐布拉克斯党会赢得 2031 年鲁里塔尼亚大选吗", "news": 1},
        {"id": "2", "zh": "示例银行 2031 年 3 月前会降息吗", "news": 0},
    ))
    result = tool.annotate(MARKETS, NEWS, BANNED)

    assert result == {
        "0xaaa1:tok1": ("佐布拉克斯党会赢得 2031 年鲁里塔尼亚大选吗", 1),
        "0xbbb2:tok2": ("示例银行 2031 年 3 月前会降息吗", None),
    }
    assert len(requests) == 1 and tool.calls == 1
    system, payload = sent(requests[0])
    assert '"markets"' in system and "只翻译" in system and "填 0" in system
    # 送给模型的是短编号，不是调用方的 id；新闻按显示顺序从 1 编号
    assert payload["markets"] == [{"id": "1", "question": MARKETS[0][1]}, {"id": "2", "question": MARKETS[1][1]}]
    assert payload["news"] == [{"n": n, "title": t} for n, t in enumerate(NEWS, 1)]
    assert "0xaaa1" not in requests[0].content.decode()


def test_banned_words_drop_translation_but_keep_news():
    tool, _ = make(lambda r: reply(
        {"id": "1", "zh": "现在买 入佐布拉克斯党胜选合约吗", "news": 1},
        {"id": "2", "zh": "示例银行会降息吗，推荐关注", "news": 3},
    ))
    result = tool.annotate(MARKETS, NEWS, BANNED)
    assert result == {"0xaaa1:tok1": (None, 1), "0xbbb2:tok2": (None, 3)}


def test_banned_words_come_from_the_rules():
    tool, _ = make(lambda r: reply({"id": "1", "zh": "佐布拉克斯党会赢吗", "news": 0}))
    assert tool.annotate(MARKETS[:1], [], ("佐布拉克斯",)) == {"0xaaa1:tok1": (None, None)}


def test_has_banned():
    assert has_banned("示例股票必涨吗", BANNED)
    assert has_banned("现在 加 仓 吗", BANNED)  # 拆开写也认得出
    assert has_banned("Zorblax HOLD", ("hold",))
    assert not has_banned("示例银行会降息吗", BANNED)
    assert not has_banned("任何文本", ("", " "))


@pytest.mark.parametrize("news", [0, -1, 4, 99, "2", 2.0, True, None, [1]])
def test_invalid_news_numbers_are_dropped(news):
    tool, _ = make(lambda r: reply({"id": "1", "zh": "佐布拉克斯党会赢吗", "news": news}))
    assert tool.annotate(MARKETS[:1], NEWS, BANNED) == {"0xaaa1:tok1": ("佐布拉克斯党会赢吗", None)}


def test_news_number_needs_news():
    tool, requests = make(lambda r: reply({"id": "1", "zh": "佐布拉克斯党会赢吗", "news": 1}))
    assert tool.annotate(MARKETS[:1], [], BANNED) == {"0xaaa1:tok1": ("佐布拉克斯党会赢吗", None)}
    assert sent(requests[0])[1]["news"] == []


def test_translation_is_cleaned_and_validated():
    long_zh = "佐布拉克斯党" * 10
    tool, _ = make(lambda r: reply(
        {"id": "1", "zh": "“佐布拉克斯党\nhttps://evil.invalid/x 会赢吗。”", "news": 2},
        {"id": "2", "zh": MARKETS[1][1], "news": 3},  # 原样照抄英文不算翻译
    ))
    result = tool.annotate(MARKETS, NEWS, BANNED)
    assert result == {"0xaaa1:tok1": ("佐布拉克斯党 会赢吗", 2), "0xbbb2:tok2": (None, 3)}

    tool, _ = make(lambda r: reply({"id": "1", "zh": long_zh, "news": 0}, {"id": "2", "zh": "Rate cut?", "news": 0}))
    result = tool.annotate(MARKETS, NEWS, BANNED)
    zh, _ = result["0xaaa1:tok1"]
    assert len(zh) == 40 and zh.endswith("…")
    assert result["0xbbb2:tok2"] == (None, None)  # 没有中文


def test_unknown_duplicate_and_malformed_entries_are_ignored():
    tool, _ = make(lambda r: reply(
        {"id": "9", "zh": "不存在的问题", "news": 1},
        "不是对象",
        {"id": 1, "zh": "佐布拉克斯党会赢吗", "news": 1},
        {"id": "1", "zh": "第二次出现的同一个问题", "news": 2},
        {"zh": "没有 id", "news": 1},
    ))
    markets = [*MARKETS, ("0xaaa1:tok1", "duplicate id is sent once")]
    result = tool.annotate(markets, NEWS, BANNED)
    assert result == {"0xaaa1:tok1": ("佐布拉克斯党会赢吗", 1)}


def test_no_markets_no_request():
    tool, requests = make(lambda r: reply())
    assert tool.annotate([], NEWS, BANNED) == {}
    assert requests == [] and tool.calls == 0


def test_bad_json_is_retried_once():
    answers = iter([llm_reply("对不起，我不能输出 JSON"), reply({"id": "1", "zh": "佐布拉克斯党会赢吗", "news": 0})])
    tool, requests = make(lambda r: next(answers))
    assert tool.annotate(MARKETS[:1], NEWS, BANNED) == {"0xaaa1:tok1": ("佐布拉克斯党会赢吗", None)}
    assert len(requests) == 2


def boom(request: httpx.Request) -> httpx.Response:
    raise RuntimeError("模拟意外")


def refused(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("refused", request=request)


@pytest.mark.parametrize("handler", [
    lambda r: httpx.Response(500),
    lambda r: httpx.Response(401, json={"error": "bad key"}),
    lambda r: llm_reply("not json"),
    lambda r: llm_reply({"markets": "不是列表"}),
    lambda r: llm_reply({"questions": []}),
    lambda r: httpx.Response(200, json={"unexpected": True}),
    refused,
    boom,
])
def test_llm_failure_returns_empty(handler, monkeypatch, caplog):
    monkeypatch.setattr("digest.llm.time.sleep", lambda s: None)
    caplog.set_level(logging.INFO)
    tool, _ = make(handler)
    assert tool.annotate(MARKETS, NEWS, BANNED) == {}
    # 本项目的日志里只有错误类型，不出现问题、新闻、接口地址和密钥
    # （httpx 自己的请求日志在正式运行时被 cli.setup_logging 调到 WARNING，这里不看）
    ours = "\n".join(r.getMessage() for r in caplog.records if r.name.startswith("digest"))
    for secret in ("Zorblax", "佐布拉克斯", "鲁里塔尼亚", HOST, KEY, "0xaaa1"):
        assert secret not in ours
    assert "市场板块" in ours


def test_time_budget_exhausted_returns_empty():
    tool, requests = make(lambda r: reply(), LLMSettings(time_budget=0.5))
    assert tool.annotate(MARKETS, NEWS, BANNED) == {}
    assert requests == []


def test_budget_starts_at_first_call_and_is_capped():
    tool, _ = make(lambda r: reply(), LLMSettings(time_budget=300, timeout=90))
    assert tool._chat is None  # 建好时还没开始计时
    assert (tool.settings.time_budget, tool.settings.timeout) == (60.0, 40.0)
    tool.annotate(MARKETS[:1], [], BANNED)
    assert tool._chat is not None and tool._chat.settings.time_budget == 60.0


def test_from_env(config, caplog):
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(500)))
    tool = MarketLLM.from_env(client, config, env=ENV)
    assert isinstance(tool, MarketLLM)
    assert tool.settings.time_budget == 60.0 and tool.settings.timeout == 40.0
    assert tool.settings.temperature == config.llm.temperature
    assert HOST not in repr(tool) and KEY not in repr(tool)

    assert MarketLLM.from_env(client, config, env={}) is None
    assert MarketLLM.from_env(client, config, env={**ENV, "LLM_MODEL": ""}) is None
    off = replace(config, llm=replace(config.llm, enabled=False))
    assert MarketLLM.from_env(client, off, env=ENV) is None

    caplog.set_level(logging.INFO)
    assert MarketLLM.from_env(client, config, env={**ENV, "LLM_BASE_URL": f"http://{HOST}/v1"}) is None
    assert "https" in caplog.text and HOST not in caplog.text and KEY not in caplog.text
