"""市场板块各模块共用的数据类型：报价、一行内容、一次板块运行的结果。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from digest.render.dingtalk import Message

# 价格以美分为单位，相减时会出现 0.30000000000000004 这样的浮点误差，比较价差时留一点余量
_EPS = 1e-9


@dataclass(frozen=True)
class Quote:
    """一个二元合约的最优买卖价和最近成交价，都是 0 到 1 之间的概率；取不到的为 None。"""

    bid: float | None
    ask: float | None
    last: float | None

    @property
    def spread(self) -> float | None:
        """买卖价差 ask - bid；缺一边时为 None。"""
        if self.bid is None or self.ask is None:
            return None
        return round(self.ask - self.bid, 10)

    def mid(self, max_spread: float = 0.10) -> tuple[float, bool] | None:
        """可用的价格和它是不是成交价：(数值, 是否用了成交价)。

        价差不超过 max_spread 时用买卖价的中间值；价差太大、缺一边或买价高于卖价（报价错乱）时
        退回最近成交价，并标记为成交价；都没有时返回 None。
        """
        spread = self.spread
        if spread is not None and 0 <= spread <= max_spread + _EPS:
            return (self.bid + self.ask) / 2, False
        if self.last is not None:
            return self.last, True
        return None


@dataclass(frozen=True)
class Line:
    """板块里的一行内容，以及发送成功后要写进账本的记录。"""

    section: str  # "key" | "weekly" | "lopsided" | "mover" | "vix" | "score" | "note"
    text: str  # 最终的中文 Markdown 文本，外部文本已经过 md_text 转义
    sub: str | None = None  # 可选的灰色第二行（历史成绩、数据来源）
    expires: datetime | None = None  # 发送时已到这个时间就丢掉这一行（数据公布或合约收盘时间）
    rows: tuple[dict, ...] = ()  # 只有板块发送成功时才追加到账本的记录


@dataclass
class PanelResult:
    lines: list[Line]
    message: Message | None  # 没有可发的内容时为 None
    status: str  # 写进 Summary 的一行中文状态，绝不包含市场内容
    requests: int
    rows: list[dict]  # 账本记录：展示出的各行的记录，加上后台观察、官方数据核对和结算记录
