from __future__ import annotations

from typing import Protocol

from digest.render.dingtalk import Message


class ChannelError(RuntimeError):
    """发送失败。错误信息里不能出现 webhook 地址或密钥。"""


class Channel(Protocol):
    name: str

    def send(self, message: Message) -> None: ...
