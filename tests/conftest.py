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
