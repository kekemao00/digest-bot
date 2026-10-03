from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import pytest

from digest.config import load_config
from digest.sources.hn import HackerNews

FIXTURES = Path(__file__).parent / "fixtures"
# 固定“现在”，让快照稳定：北京时间 2026-10-03 08:02
NOW = datetime(2026, 10, 3, 8, 2, tzinfo=ZoneInfo("Asia/Shanghai"))


@pytest.fixture
def config():
    return load_config()


@pytest.fixture
def hn_payload():
    return json.loads((FIXTURES / "hn_search.json").read_text(encoding="utf-8"))


@pytest.fixture
def hn_source(config):
    return HackerNews(config.sources["hn"])


@pytest.fixture
def hn_items(hn_source, hn_payload):
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=hn_payload))
    with httpx.Client(transport=transport) as client:
        return hn_source.fetch(client, NOW)
