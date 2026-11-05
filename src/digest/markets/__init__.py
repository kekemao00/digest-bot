"""市场与预测板块：在新闻简报之后单独推送一条钉钉消息，内容来自预测市场的报价和官方数据。

安全约定（仓库是公开的）：
- 板块内容（价格、问题、合约代码）只发到钉钉，不进日志、Summary、--out、归档和 state.json。
- 账本只以密文保存在 .state/markets/ 下；没有密钥时什么都不写。
- 板块出任何错都不影响简报的推送和进程的退出码。
- 只有仓库变量 MARKETS_ENABLED 为真时才运行，CI 和 fork 默认不启用。

这里只放入口；各模块在入口函数里按需导入，避免没启用时加载用不到的代码。
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import httpx

    from digest.config import Config
    from digest.models import Item

log = logging.getLogger("digest.markets")

LOAD_FAILED = "市场板块：未启用（加载失败：{}）"
# 出了意外时各入口的状态行开头
_HEADS = {"send": "市场板块：未推送", "preview": "市场板块预览：未推送", "dry": "市场板块：出错"}


def enabled(env: Mapping[str, str] = os.environ) -> bool:
    """仓库变量 MARKETS_ENABLED 为真（1 / true / yes，不分大小写）时启用板块，和 cli._truthy 的规则一致。"""
    return (env.get("MARKETS_ENABLED") or "").strip().lower() in ("1", "true", "yes")


def run_panel(
    client: httpx.Client,
    config: Config,
    now_fn: Callable[[], datetime],
    state_dir: Path,
    news: Sequence[Item],
    channel: Any,
    mode: str = "send",
    env: Mapping[str, str] = os.environ,
) -> str:
    """简报之后跑一次板块。mode：send 推送并记账；preview 推送【预览】、不记账；dry 只组装、不推送不记账。

    news 是简报里实际显示的新闻（按显示顺序），没有推送简报时传空列表。从不抛异常，返回写进 Summary 的一行。
    """
    try:
        from digest.markets import panel
    except Exception as exc:
        return LOAD_FAILED.format(type(exc).__name__)
    try:
        return panel.run(client, config, now_fn, state_dir, news, channel, mode=mode, env=env)
    except Exception as exc:  # 板块出任何错都不能影响简报
        log.warning("市场板块出错：%s", type(exc).__name__)
        return f"{_HEADS.get(mode, _HEADS['send'])}：{type(exc).__name__}"


def run_preview(
    client: httpx.Client,
    config: Config,
    now_fn: Callable[[], datetime],
    state_dir: Path,
    channel: Any,
    env: Mapping[str, str] = os.environ,
) -> str:
    """只推送一条【预览】板块（不发简报、不记账、不改状态）；价格变化的后台观察也显示出来。"""
    return run_panel(client, config, now_fn, state_dir, [], channel, mode="preview", env=env)


def run_backtest(
    client: httpx.Client,
    config: Config,
    now_fn: Callable[[], datetime],
    state_dir: Path,
    channel: Any,
    env: Mapping[str, str] = os.environ,
) -> str:
    """Kalshi 历史回测。channel 为 None 时是试运行（只报计数，不推送、不保存）。从不抛异常。"""
    try:
        from digest.markets import panel
    except Exception as exc:
        return LOAD_FAILED.format(type(exc).__name__)
    try:
        return panel.run_replay(client, config, now_fn, state_dir, channel, env=env)
    except Exception as exc:
        log.warning("市场回测出错：%s", type(exc).__name__)
        return f"市场回测：出错：{type(exc).__name__}"
