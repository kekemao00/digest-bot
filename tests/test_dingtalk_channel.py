from __future__ import annotations

import base64
import hashlib
import hmac
import json
from urllib.parse import parse_qs, unquote_plus, urlsplit

import httpx
import pytest

from digest.channels.base import ChannelError
from digest.channels.dingtalk import DingTalk, sign
from digest.render.dingtalk import Message

WEBHOOK = "https://oapi.dingtalk.com/robot/send?access_token=tok123"


def test_sign_matches_dingtalk_algorithm():
    expected = base64.b64encode(hmac.new(b"SEC", b"1700000000000\nSEC", hashlib.sha256).digest()).decode()
    assert unquote_plus(sign("SEC", 1700000000000)) == expected


def test_rejects_non_dingtalk_host():
    with httpx.Client() as client:
        with pytest.raises(ChannelError):
            DingTalk("https://evil.test/robot/send?access_token=x", "SEC", client)
        with pytest.raises(ChannelError):
            DingTalk("http://oapi.dingtalk.com/robot/send?access_token=x", "SEC", client)
        with pytest.raises(ChannelError):
            DingTalk(WEBHOOK, "", client)


def test_send_signs_request_and_posts_markdown():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = request.url
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"errcode": 0, "errmsg": "ok"})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        DingTalk(WEBHOOK, "SEC", client).send(Message(title="t", text="body"))
    query = parse_qs(urlsplit(str(seen["url"])).query)
    assert query["access_token"] == ["tok123"]
    assert {"timestamp", "sign"} <= query.keys()
    assert seen["body"] == {"msgtype": "markdown", "markdown": {"title": "t", "text": "body"}}


def test_errors_never_include_token():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"cannot reach {request.url}")

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ChannelError) as info:
            DingTalk(WEBHOOK, "SEC", client).send(Message(title="t", text="x"))
    assert "tok123" not in str(info.value)
    assert info.value.__cause__ is None


def test_api_error_is_reported():
    transport = httpx.MockTransport(lambda r: httpx.Response(200, json={"errcode": 310000, "errmsg": "sign not match"}))
    with httpx.Client(transport=transport) as client:
        with pytest.raises(ChannelError, match="310000"):
            DingTalk(WEBHOOK, "SEC", client).send(Message(title="t", text="x"))
