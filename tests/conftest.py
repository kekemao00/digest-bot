from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import pytest

from digest.config import load_config
from digest.sources.hn import HackerNews
from digest.state import State

FIXTURES = Path(__file__).parent / "fixtures"
# 固定“现在”，让快照稳定：北京时间 2026-10-03 08:02
NOW = datetime(2026, 10, 3, 8, 2, tzinfo=ZoneInfo("Asia/Shanghai"))


@pytest.fixture
def config():
    return load_config()


@pytest.fixture
def hn_payload():
    return json.loads((FIXTURES / "hn_search.json").read_text(encoding="utf-8"))


def source_options(config, source_id):
    return next(opts for opts in config.sources if opts["id"] == source_id)


def fixture_text(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def fetch_with(source, handler, state=None):
    """用假的 HTTP 响应运行一个信息源。"""
    if not callable(handler):
        body = handler
        handler = lambda request: httpx.Response(200, content=body if isinstance(body, bytes) else body.encode())
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        return source.fetch(client, NOW, state or State(None))


@pytest.fixture
def hn_source(config):
    return HackerNews(source_options(config, "hn"))


@pytest.fixture
def hn_items(hn_source, hn_payload):
    return fetch_with(hn_source, lambda request: httpx.Response(200, json=hn_payload))


def llm_reply(content: str | dict) -> httpx.Response:
    """OpenAI 兼容接口的响应。"""
    text = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
    return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": text}}]})


def llm_entries(request: httpx.Request) -> tuple[str, list[dict]]:
    """取出一次请求的系统提示词和送审的条目。"""
    messages = json.loads(request.content)["messages"]
    user = messages[1]["content"]
    return messages[0]["content"], json.loads(user[user.index("[") :])


def fake_llm(request: httpx.Request) -> httpx.Response:
    """确定性的假大模型：Ask HN 闲聊判为低分观点，招聘帖判为招聘，其余给中文标题和一句话。"""
    system, entries = llm_entries(request)
    if '"highlights"' in system:
        return llm_reply({"highlights": [{"n": e["n"], "text": f"要点：{e['title'][:16]}"} for e in entries[:3]]})
    result = []
    for e in entries:
        title = e["title"]
        kind, score = ("观点", 3) if title.startswith("Ask HN") else ("新闻", 7)
        focus = ["ai"] if "LLM" in title or "Claude" in title else []
        topic = "AI" if focus else "金融" if "Fed" in title else "科学" if "Voyager" in title else "软件"
        result.append({"id": e["id"], "title_zh": f"中文：{title[:12]}", "summary": f"一句话说明 {e['id']}",
                       "kind": kind, "score": score, "focus": focus, "topic": topic, "subject": ""})
    # 推理模型常见的输出形态：先有思考段，再用代码块包住 JSON
    return llm_reply("<think>先看看这些条目</think>\n```json\n" + json.dumps({"items": result}, ensure_ascii=False) + "\n```")
