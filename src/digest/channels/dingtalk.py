from __future__ import annotations

import base64
import hashlib
import hmac
import os
import time
from urllib.parse import quote_plus, urlsplit

import httpx

from digest.channels.base import ChannelError
from digest.render.dingtalk import Message

ALLOWED_HOST = "oapi.dingtalk.com"
# 只在确定消息没送到时重试：连接失败、限流或服务端错误。读超时可能已经送达，重试会重复推送
RETRY_ERRORS = (httpx.ConnectError, httpx.ConnectTimeout)
RETRY_STATUS = {429, 500, 502, 503, 504}
RETRY_DELAY = 5.0


def sign(secret: str, timestamp_ms: int) -> str:
    """钉钉自定义机器人“加签”：HmacSHA256(timestamp + "\\n" + secret)，Base64 后再做 URL 编码。"""
    payload = f"{timestamp_ms}\n{secret}".encode()
    digest = hmac.new(secret.encode(), payload, hashlib.sha256).digest()
    return quote_plus(base64.b64encode(digest).decode())


class DingTalk:
    name = "dingtalk"

    def __init__(self, webhook: str, secret: str, client: httpx.Client) -> None:
        parts = urlsplit(webhook)
        # 只允许发往钉钉官方域名，配置错误或被篡改时不会把消息发到别处
        if parts.scheme != "https" or parts.hostname != ALLOWED_HOST:
            raise ChannelError(f"DINGTALK_WEBHOOK 必须是 https://{ALLOWED_HOST}/ 开头的地址")
        if not secret:
            raise ChannelError("缺少 DINGTALK_SECRET：请在钉钉机器人安全设置里选择“加签”并把密钥存入 Secrets")
        self._webhook = webhook
        self._secret = secret
        self._client = client

    @classmethod
    def from_env(cls, client: httpx.Client) -> DingTalk:
        webhook = os.environ.get("DINGTALK_WEBHOOK", "").strip()
        if not webhook:
            raise ChannelError("缺少 DINGTALK_WEBHOOK")
        return cls(webhook, os.environ.get("DINGTALK_SECRET", "").strip(), client)

    def send(self, message: Message) -> None:
        body = {"msgtype": "markdown", "markdown": {"title": message.title, "text": message.text}}
        for attempt in range(2):
            try:
                resp = self._post(body)
            except RETRY_ERRORS as exc:
                if attempt == 0:
                    time.sleep(RETRY_DELAY)
                    continue
                raise ChannelError(f"钉钉请求失败：{type(exc).__name__}") from None
            except httpx.HTTPError as exc:
                # 异常文本可能包含请求 URL（含 access_token 和签名），只报类型
                raise ChannelError(f"钉钉请求失败：{type(exc).__name__}") from None
            if resp.status_code in RETRY_STATUS and attempt == 0:
                time.sleep(RETRY_DELAY)
                continue
            break
        if resp.status_code != 200:
            raise ChannelError(f"钉钉返回 HTTP {resp.status_code}")
        data = resp.json()
        if data.get("errcode") != 0:
            raise ChannelError(f"钉钉拒绝了消息：errcode={data.get('errcode')} {data.get('errmsg', '')}")

    def _post(self, body: dict) -> httpx.Response:
        # 签名带时间戳，每次请求重新计算
        timestamp = int(time.time() * 1000)
        joiner = "&" if "?" in self._webhook else "?"
        url = f"{self._webhook}{joiner}timestamp={timestamp}&sign={sign(self._secret, timestamp)}"
        return self._client.post(url, json=body)
