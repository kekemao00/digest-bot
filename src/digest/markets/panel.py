"""市场板块的组装：取数据 -> 挑选 -> 排版 -> 发送 -> 记账（设计文档 10、13.8）。

- build：按顺序做关键数据、每周一看（周一早上）、定价一边倒、价格变化、波动率、上周对答案（周一早上），
  每一部分单独 try/except，一个数据源出错不影响其他部分。
- run：一次推送（send）、预览（preview）或试运行（dry）。发送成功后才把展示过的内容记进加密账本；
  官方数据核对（witness）和结算核对（settle）的记录不依赖发送，正式推送时总是记。
- run_replay：Kalshi 历史回测，结果加密保存，并推送【回测】消息。

返回给 Summary 的状态只有固定的中文说明、行数、请求次数、数据源的结果（“正常”、“HTTP 403”）和异常类型，
绝不出现问题文本、合约代码、价格和网址。
"""

from __future__ import annotations

import logging
import math
import os
import time
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

from digest.config import Config
from digest.markets import ledger, select
from digest.markets.kalshi import Kalshi, KEvent, period_of
from digest.markets.ledger import Keys, Ledger, LedgerError
from digest.markets.net import Budget, BudgetExceeded, FetchError, Fetcher
from digest.markets.polymarket import CLOB, DATA, GAMMA, Book, PMarket, Polymarket, wallet_stats
from digest.markets.render import render_panel
from digest.markets.schedule import is_monday
from digest.markets.select import MoverCheck
from digest.markets.settings import (
    DISABLED_STATUS,
    BudgetSettings,
    MarketsSettings,
    SeriesRule,
    load_settings,
    rules_hash,
)
from digest.markets.sources import cboe_last_close, cleveland_nowcast
from digest.markets.textgen import MarketLLM
from digest.markets.types import Line, PanelResult
from digest.models import Item
from digest.render.dingtalk import Message

log = logging.getLogger("digest.markets")

MODES = ("send", "preview", "dry")
# 正式推送时，官方数据核对和结算核对最多用掉预算（时长和请求次数）的这么多，剩下的留给板块本身
SCORE_SHARE = 0.3
SETTLE_LIMIT = 10
# 定价一边倒最多核实 max_items 的这么多倍个候选（每个一到两次请求）
LOPSIDED_TRIES = 3
# 克利夫兰联储的预测超过这么多天没更新就不放
NOWCAST_MAX_AGE_DAYS = 7
# 价格变化的核实：价格历史取最近 26 小时（够找到 24 小时前的整点），成交最多翻两页（没翻完 24 小时时
# 钱包的两项门槛算没通过），盘口看推动 5 个百分点
HISTORY_HOURS = 26
TRADES_PAGES = 2
DEPTH_POINTS = 0.05
BACKTEST_BLOB = "backtest"
# 回测试运行是 CI 的冒烟测试（预览任务整个限时 15 分钟），时长另设上限；到点就停，按部分完成报计数
DRY_REPLAY_SECONDS = 240.0
# 工作流在“读取去重状态”这一步失败时传 false：这时 .state 是空的，新建账本会在保存时盖掉 state 分支上的
# 账本钥匙记录（旧账本从此打不开），所以这次不记账。本地运行没有这个变量，照常记账
STATE_OK_ENV = "MARKETS_STATE_OK"
STATE_MISSING = "去重状态没取到，这次不记账"

# 出错时 Summary 里写的部分名称
SECTION_NAMES = {
    "key": "关键数据",
    "weekly": "每周一看",
    "polymarket": "Polymarket 列表",
    "lopsided": "定价一边倒",
    "mover": "价格变化",
    "llm": "翻译",
    "vix": "波动率",
    "score": "对答案",
    "nowcast": "克利夫兰联储预测",
    "replay": "回测成绩",
    "witness": "官方数据核对",
    "settle": "结算核对",
}
# 主机名 -> Summary 里的数据源名称；按网址后缀兜底（比如 Cboe 跳转后的地址）
VENUE_ORDER = ("Kalshi", "Polymarket", "Cboe", "克利夫兰联储", "劳工统计局", "纽约联储")
_VENUE_SUFFIXES = (
    ("kalshi.com", "Kalshi"),
    ("polymarket.com", "Polymarket"),
    ("cboe.com", "Cboe"),
    ("clevelandfed.org", "克利夫兰联储"),
    ("bls.gov", "劳工统计局"),
    ("newyorkfed.org", "纽约联储"),
)
_FAILED = object()  # 核实某一步没取到


@dataclass
class Context:
    """一次板块运行需要的全部输入。rows 是账本里已有的记录（加上这次的核对记录），backtest 是解密后的回测结果。"""

    fetcher: Fetcher
    settings: MarketsSettings
    now: datetime
    edition: str  # "morning" | "evening"
    mode: str  # "send" | "preview" | "dry"
    news: list[Item]  # 今天这条简报里显示的新闻，按显示顺序（第 i 个是第 i + 1 条）
    llm: MarketLLM | None
    rows: list[dict]
    backtest: dict | None


# ---- 组装 ----


def build(ctx: Context) -> PanelResult:
    """取数据并拼出各行，再排成消息（页脚的账本链头留空，发送前由 run 重新排版）。

    status 是一行运行情况：行数、各数据源的结果、出错的部分（只有异常类型）。
    """
    result, _errors = _build(ctx)
    return result


def guard(lines: list[Line], now: datetime) -> list[Line]:
    """发送前的最后一道检查：去掉已经到了公布时间或收盘时间的行。"""
    current = _aware(now)
    return [line for line in lines if line.expires is None or _aware(line.expires) > current]


def _build(ctx: Context, kalshi_dead: set[str] | None = None) -> tuple[PanelResult, list[tuple[str, str]]]:
    """build 的实现，另外返回出错的部分 [(部分, 异常类型)]，run 用它拼最终的状态行。"""
    builder = _Builder(ctx, kalshi_dead)
    lines = builder.lines()
    message = render_panel(
        lines, ctx.edition, ctx.now, None, cleveland=_cleveland(lines), preview=ctx.mode == "preview"
    )
    rows = [dict(row) for line in lines for row in line.rows] + builder.candidates
    details = _details(ctx.fetcher.hosts, ctx.settings, builder.errors)
    count = _sent_count(lines, message)
    status = f"市场板块：{count} 行；{details}"
    result = PanelResult(lines=lines, message=message, status=status, requests=ctx.fetcher.requests, rows=rows)
    return result, list(builder.errors)


class _Builder:
    """一次 build 的工作状态：出错记录、价格变化的候选记录、克利夫兰联储预测的缓存。"""

    def __init__(self, ctx: Context, kalshi_dead: set[str] | None = None) -> None:
        self.ctx = ctx
        self.settings = ctx.settings
        self.errors: list[tuple[str, str]] = []  # (部分, 异常类型)，去重
        self.candidates: list[dict] = []
        self._kalshi: Kalshi | None = None
        # 和前面结算核对共用的“连不上的 Kalshi 地址”：核对时已经连不上，这里就不再一个个等超时
        self._kalshi_dead = kalshi_dead if kalshi_dead is not None else set()
        self._nowcasts: Mapping[tuple[int, int], Any] | None = None

    # ---- 通用 ----

    def fail(self, section: str, exc: BaseException) -> None:
        name = type(exc).__name__
        if (section, name) not in self.errors:
            self.errors.append((section, name))
        detail = f"（{exc.outcome}）" if isinstance(exc, FetchError) else ""
        log.warning("市场板块：%s出错：%s%s", SECTION_NAMES.get(section, section), name, detail)

    def guarded(self, section: str, fn: Callable[[], Any], default: Any) -> Any:
        try:
            return fn()
        except Exception as exc:  # 一部分出错不影响其他部分
            self.fail(section, exc)
            return default

    def kalshi(self) -> Kalshi:
        if self._kalshi is None:
            self._kalshi = Kalshi(self.ctx.fetcher, self.settings.kalshi.hosts)
            self._kalshi.dead = self._kalshi_dead
        return self._kalshi

    def kalshi_down(self) -> bool:
        """Kalshi 的地址都连不上或超时了：后面的系列不用再试。"""
        return self._kalshi is not None and self._kalshi.down

    @property
    def morning(self) -> bool:
        return self.ctx.edition != "evening"

    # ---- 各部分 ----

    def lines(self) -> list[Line]:
        ctx, settings = self.ctx, self.settings
        owner = settings.audience != "group"  # 群里有别人时只放 Polymarket
        monday_morning = self.morning and is_monday(ctx.now)
        lines: list[Line] = []
        if owner:
            lines += self.guarded("key", self.key_lines, [])
            if monday_morning:
                lines += self.guarded("weekly", self.weekly_lines, [])
        # 晚间版没有今晚的数据、也不会显示价格变化时，消息反正不发，后面的就不用再请求了
        show_movers = ctx.mode == "preview" or settings.polymarket.movers.mode == "show"
        if self.morning or lines or show_movers:
            # 波动率只要两个请求，排在 Polymarket（后台观察的价格变化核实要十几个请求）前面，预算紧时先保住它；
            # 消息里各部分的顺序由 render_panel 按部分排，和这里的先后无关
            if owner:
                lines += self.guarded("vix", self.vix_lines, [])
            lines += self.guarded("polymarket", self.polymarket_lines, [])
        if monday_morning:
            lines += self.guarded("score", self.score_lines, [])
        return lines

    def key_lines(self) -> list[Line]:
        """关键数据：每类合约取在交易的事件，留下公布时间落在时间窗里的，按公布时间排。"""
        ctx, settings = self.ctx, self.settings
        found: list[tuple[datetime, int, Line]] = []
        for index, rule in enumerate(settings.kalshi.key_data):
            try:
                events = self.kalshi().events(rule.series, status="open", nested=True, max_pages=1)
            except BudgetExceeded as exc:
                self.fail("key", exc)
                break
            except Exception as exc:
                self.fail("key", exc)
                if self.kalshi_down():
                    break
                continue
            for event, release in select.key_events(rule, events, settings, ctx.now, ctx.edition):
                line = self.guarded("key", lambda r=rule, e=event, t=release: self.key_line(r, e, t), None)
                if line is not None:
                    found.append((release, index, line))
        found.sort(key=lambda item: (item[0], item[1]))
        return [line for _, _, line in found]

    def key_line(self, rule: SeriesRule, event: KEvent, release: datetime) -> Line | None:
        ctx = self.ctx
        if event.open_interest < self.settings.kalshi.min_open_interest:
            return None
        period = period_of(event.event_ticker)
        nowcast = self.nowcast(rule, period) if rule.series in select.NOWCAST_FIELDS else None
        lead = select.lead_of(ctx.now, release, ctx.edition)
        sub = select.replay_sub(self.record(rule.series, lead), isinstance(ctx.backtest, Mapping), lead)
        return select.key_line(rule, event, release, ctx.now, ctx.edition, self.settings, nowcast=nowcast, sub=sub)

    def nowcast(self, rule: SeriesRule, period: tuple[int, int] | None) -> float | None:
        """克利夫兰联储的预测，只在第一次需要时取一次；取不到时这次都不放。"""
        if self._nowcasts is None:
            try:
                self._nowcasts = cleveland_nowcast(self.ctx.fetcher, self.settings.nowcast.url)
            except Exception as exc:
                self.fail("nowcast", exc)
                self._nowcasts = {}
        return select.nowcast_value(rule, self._nowcasts, period, self.ctx.now, NOWCAST_MAX_AGE_DAYS)

    def record(self, series: str, lead: int) -> str | None:
        """回测里这类合约在同一提前量的成绩（一句话）；没有回测结果或提前太多天时返回 None。"""
        summary = self.ctx.backtest
        if not isinstance(summary, Mapping) or lead > select.MAX_LEAD_DAYS:
            return None
        try:
            from digest.markets import backtest

            label = backtest.lead_label(self.ctx.edition, min(lead, select.MAX_LEAD_DAYS))
            clause = backtest.record_clause(summary, series, label)
        except Exception as exc:
            self.fail("replay", exc)
            return None
        return clause.strip() if isinstance(clause, str) and clause.strip() else None

    def weekly_lines(self) -> list[Line]:
        ctx, settings = self.ctx, self.settings
        lines: list[Line] = []
        for rule in settings.kalshi.weekly:
            try:
                events = self.kalshi().events(rule.series, status="open", nested=True, max_pages=1)
            except BudgetExceeded as exc:
                self.fail("weekly", exc)
                break
            except Exception as exc:
                self.fail("weekly", exc)
                if self.kalshi_down():
                    break
                continue
            for event in select.weekly_events(rule, events, ctx.now):
                line = self.guarded("weekly", lambda r=rule, e=event: select.weekly_line(r, e, ctx.now, settings), None)
                if line is not None:
                    lines.append(line)
        return lines

    def polymarket_lines(self) -> list[Line]:
        """定价一边倒和价格变化共用一次事件列表，也共用一次大模型请求（翻译问题、匹配新闻）。"""
        poly = Polymarket(self.ctx.fetcher)
        markets = self.poly_markets(poly)
        if not markets:
            return []
        picks = self.guarded("lopsided", lambda: self.lopsided(poly, markets), [])
        checks = self.guarded("mover", lambda: self.movers(poly, markets), [])
        notes = self.annotate(picks, checks)
        lines = self.guarded("lopsided", lambda: self.lopsided_lines(picks, notes), [])
        lines += self.guarded("mover", lambda: self.mover_lines(checks, notes), [])
        return lines

    def poly_markets(self, poly: Polymarket) -> list[PMarket]:
        found: dict[str, PMarket] = {}
        settings = self.settings.polymarket
        for tag_id in settings.tags.values():
            try:
                markets = poly.events(tag_id, max_pages=settings.max_pages)
            except BudgetExceeded as exc:
                self.fail("polymarket", exc)
                break
            except Exception as exc:
                self.fail("polymarket", exc)
                continue
            for m in markets:
                found.setdefault(m.condition_id, m)
        return list(found.values())

    def lopsided(self, poly: Polymarket, markets: Sequence[PMarket]) -> list[tuple[PMarket, Book, float]]:
        """粗筛后的候选按流动性逐个核实：盘口价差和价格仍然合格、结算状态干净，够 max_items 个就停。"""
        rules = self.settings.polymarket.lopsided
        accepted: list[tuple[PMarket, Book, float]] = []
        for m in select.lopsided_picks(markets, self.settings, self.ctx.now)[: rules.max_items * LOPSIDED_TRIES]:
            if len(accepted) >= rules.max_items:
                break
            try:
                book = poly.book(m.token_ids[0])
                mid = select.verify_lopsided(book, rules)
                if mid is None:
                    continue
                resolution = poly.resolution(m.condition_id)
            except BudgetExceeded as exc:
                self.fail("lopsided", exc)
                break
            except FetchError:
                continue  # 已关闭的市场没有盘口（404），换下一个
            if resolution is not None and resolution.clean():
                accepted.append((m, book, mid))
        return accepted

    def movers(self, poly: Polymarket, markets: Sequence[PMarket]) -> list[MoverCheck]:
        checks: list[MoverCheck] = []
        for m in select.mover_candidates(markets, self.settings, self.ctx.now):
            check = MoverCheck(m)
            checks.append(check)
            try:
                self.verify(poly, check)
            except BudgetExceeded as exc:
                check.errors.append(f"budget:{type(exc).__name__}")
                self.fail("mover", exc)
                break
        return checks

    def verify(self, poly: Polymarket, check: MoverCheck) -> None:
        """核实一个候选：盘口（现在的价格、价差、深度）、价格历史、过去一天的成交、结算状态。"""
        m = check.market
        token = m.token_ids[0]
        now_ts = int(self.ctx.now.timestamp())
        rules = self.settings.polymarket.movers
        book = self.step(check, "book", lambda: poly.book(token))
        if book is _FAILED:
            return  # 没有盘口多半是已经关闭，后面的不用再查
        check.mid, check.spread = book.mid, book.spread
        check.depth_up, check.depth_down = book.depth(DEPTH_POINTS)
        history = self.step(check, "history", lambda: poly.history(token, now_ts - HISTORY_HOURS * 3600, 3600))
        if history is not _FAILED:
            check.p24, check.change, check.persist = select.price_change(
                history, check.mid, now_ts, rules.persist_points
            )
        found = self.step(
            check, "trades", lambda: poly.trades_since(m.condition_id, now_ts - select.DAY, TRADES_PAGES)
        )
        if found is not _FAILED:
            trades, check.trades_complete = found
            check.wallets, check.top_share = wallet_stats(trades, 0)
        resolution = self.step(check, "resolution", lambda: poly.resolution(m.condition_id))
        if resolution is not _FAILED:
            check.clean = resolution is not None and resolution.clean()

    @staticmethod
    def step(check: MoverCheck, name: str, fn: Callable[[], Any]) -> Any:
        try:
            return fn()
        except BudgetExceeded:
            raise
        except Exception as exc:
            check.errors.append(f"{name}:{type(exc).__name__}")
            return _FAILED

    def annotate(
        self, picks: Sequence[tuple[PMarket, Book, float]], checks: Sequence[MoverCheck]
    ) -> Mapping[str, tuple[str | None, int | None]]:
        """一次大模型请求：问题的中文译文，以及价格变化对应今天第几条新闻。出错时都用英文原文、不配新闻。"""
        llm = self.ctx.llm
        questions = [(m.condition_id, m.question) for m, _, _ in picks]
        questions += [(c.market.condition_id, c.market.question) for c in checks]
        if llm is None or not questions:
            return {}
        titles = [item.title_zh or item.title for item in self.ctx.news]
        try:
            notes = llm.annotate(questions, titles, self.settings.banned_words)
        except Exception as exc:
            self.fail("llm", exc)
            return {}
        if not isinstance(notes, Mapping):
            return {}
        for check in checks:
            zh, news = _note(notes, check.market.condition_id)
            check.zh = zh
            valid = isinstance(news, int) and not isinstance(news, bool) and 1 <= news <= len(titles)
            check.news = news if valid else None
        return notes

    def lopsided_lines(
        self, picks: Sequence[tuple[PMarket, Book, float]], notes: Mapping[str, tuple[str | None, int | None]]
    ) -> list[Line]:
        settings = self.settings
        return [select.lopsided_line(m, book, mid, _note(notes, m.condition_id)[0], settings) for m, book, mid in picks]

    def mover_lines(self, checks: Sequence[MoverCheck], notes: Mapping[str, Any]) -> list[Line]:
        """后台观察（shadow）时板块里不显示，只记候选；show 时最多显示一个全部门槛都通过的；预览时全部显示。"""
        settings, ctx = self.settings, self.ctx
        preview = ctx.mode == "preview"
        chosen = None
        if settings.polymarket.movers.mode == "show" and not preview:
            chosen = select.pick_mover(checks, settings)
        self.candidates = [select.candidate_row(c, settings, shown=c is chosen) for c in checks]
        if preview:
            return [line for c in checks if (line := select.mover_line(c, ctx.edition, settings, preview=True))]
        if chosen is not None and (line := select.mover_line(chosen, ctx.edition, settings)) is not None:
            return [line]
        return []

    def vix_lines(self) -> list[Line]:
        ctx, settings = self.ctx, self.settings
        vix = cboe_last_close(ctx.fetcher, settings.vix.vix_url)
        if not select.vix_lines(vix, None, ctx.now, settings):
            return []  # 没有或过时了，VIX9D 也不用取
        try:
            vix9d = cboe_last_close(ctx.fetcher, settings.vix.vix9d_url)
        except Exception as exc:
            self.fail("vix", exc)
            vix9d = None
        return select.vix_lines(vix, vix9d, ctx.now, settings)

    def score_lines(self) -> list[Line]:
        from digest.markets import score

        lines = score.weekly_lines(list(self.ctx.rows), self.ctx.now)
        return [line for line in lines or () if isinstance(line, Line)]


def _note(notes: Mapping[str, Any], key: str) -> tuple[str | None, int | None]:
    value = notes.get(key)
    if isinstance(value, tuple) and len(value) == 2:
        zh = value[0] if isinstance(value[0], str) and value[0] else None
        return zh, value[1]
    return None, None


def _cleveland(lines: Iterable[Line]) -> bool:
    """有关键数据行带了克利夫兰联储的预测时，页脚注明来源。"""
    return any(line.section == "key" and select.NOWCAST_CLAUSE in line.text for line in lines)


def _sent_count(lines: Sequence[Line], message: Message | None) -> int:
    return len(_sent(lines, message))


def _sent(lines: Sequence[Line], message: Message | None) -> list[Line]:
    """消息里实际出现的行（超长时 render_panel 会删掉一部分）。"""
    if message is None:
        return []
    return [line for line in lines if line.text in message.text]


# ---- 状态行 ----


def _host(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""


def _venue_names(settings: MarketsSettings) -> dict[str, str]:
    names: dict[str, str] = {}
    pairs = [(url, "Kalshi") for url in settings.kalshi.hosts]
    pairs += [(GAMMA, "Polymarket"), (CLOB, "Polymarket"), (DATA, "Polymarket")]
    pairs += [(settings.vix.vix_url, "Cboe"), (settings.vix.vix9d_url, "Cboe")]
    pairs += [(settings.nowcast.url, "克利夫兰联储"), (settings.witnesses.bls_v1, "劳工统计局")]
    pairs += [(settings.witnesses.nyfed, "纽约联储")]
    for url, name in pairs:
        if host := _host(url):
            names.setdefault(host, name)
    return names


def _venue_of(host: str, names: Mapping[str, str]) -> str:
    if host in names:
        return names[host]
    for suffix, name in _VENUE_SUFFIXES:
        if host == suffix or host.endswith("." + suffix):
            return name
    return host


def _venues(hosts: Mapping[str, str], settings: MarketsSettings) -> str:
    """各数据源最近一次请求的结果：“Kalshi 正常，克利夫兰联储 HTTP 403”。没有发过请求时写“没有联网”。"""
    names = _venue_names(settings)
    outcomes: dict[str, list[str]] = {}
    for host, outcome in hosts.items():
        outcomes.setdefault(_venue_of(host.lower(), names), []).append(str(outcome))
    if not outcomes:
        return "没有联网"
    order = {name: i for i, name in enumerate(VENUE_ORDER)}
    parts = []
    for venue in sorted(outcomes, key=lambda v: (order.get(v, len(order)), v)):
        results = outcomes[venue]
        bad = [r for r in results if r != "ok"]
        if not bad:
            word = "正常"
        elif len(bad) == len(results):
            word = bad[0]
        else:
            word = f"部分正常（{bad[0]}）"
        parts.append(f"{venue} {word}")
    return "，".join(parts)


def _details(hosts: Mapping[str, str], settings: MarketsSettings, errors: Sequence[tuple[str, str]]) -> str:
    parts = [_venues(hosts, settings)]
    if errors:
        parts.append("出错：" + "，".join(f"{SECTION_NAMES.get(s, s)} {name}" for s, name in errors))
    return "；".join(parts)


# ---- 一次推送、预览或试运行 ----


def run(
    client: httpx.Client,
    config: Config,
    now_fn: Callable[[], datetime],
    state_dir: Path | str,
    news: Sequence[Item] | None,
    channel: Any,
    mode: str = "send",
    env: Mapping[str, str] = os.environ,
) -> str:
    """跑一次板块，返回写进 Summary 的一行状态（不含任何市场内容）。调用方负责兜住异常。

    顺序：读规则 -> 打开账本（有钥匙时）-> 解密回测结果 -> 官方数据和结算核对（只在正式推送时）->
    build -> 发送前检查过期 -> 发送 -> 发送成功后记账（rules、shown、candidate、score 记录）->
    自检。发送失败或没有可发的内容时只记 witness、settle。dry 只组装，不发送、不记账。
    """
    quiet_http_logs()
    mode = mode if mode in MODES else "dry"
    settings = load_settings()
    if settings.error:
        return DISABLED_STATUS
    with _markets_http(client) as http:
        return _run(http, client, config, now_fn, Path(state_dir), news, channel, mode, env, settings)


def _run(
    http: httpx.Client,
    client: httpx.Client,
    config: Config,
    now_fn: Callable[[], datetime],
    state_dir: Path,
    news: Sequence[Item] | None,
    channel: Any,
    mode: str,
    env: Mapping[str, str],
    settings: MarketsSettings,
) -> str:
    """run 的实现。市场数据走 http（不在连接层偷偷重试的客户端），大模型照旧用共享的 client。"""
    edition = "evening" if config.edition == "evening" else "morning"
    markets_dir = state_dir / "markets"
    now = _aware(now_fn())

    keys, led, ledger_note = _open_ledger(markets_dir, env)
    rows = led.rows() if led is not None else []
    summary = _read_summary(markets_dir, keys) if led is not None else None

    errors: list[tuple[str, str]] = []
    checked: list[dict] = []  # witness 和 settle 记录
    score_fetcher: Fetcher | None = None
    kalshi_dead: set[str] = set()  # 结算核对和板块共用：连不上的 Kalshi 地址
    if mode == "send" and led is not None:
        score_fetcher = _make_fetcher(http, settings.budget, share=SCORE_SHARE)
        checked = _score_rows(score_fetcher, settings, rows, now, errors, kalshi_dead)
    fetcher = _make_fetcher(http, settings.budget, after=score_fetcher)
    llm = _llm(client, config, env) if mode != "dry" else None
    ctx = Context(fetcher, settings, now, edition, mode, list(news or []), llm, rows + checked, summary)
    result, build_errors = _build(ctx, kalshi_dead)
    hosts = {**(score_fetcher.hosts if score_fetcher else {}), **fetcher.hosts}
    details = _details(hosts, settings, errors + build_errors)
    warnings = f"规则文件有 {len(settings.warnings)} 处写错，已用默认值" if settings.warnings else ""

    send_time = _aware(now_fn())
    lines = guard(result.lines, send_time)
    head16 = led.head16() if led is not None else None
    # 账本还是空的（正式推送才这样写；预览、试运行不记账）：页脚写“账本此前为空”
    first_entry = mode == "send" and led is not None and head16 is None
    message = render_panel(
        lines, edition, send_time, head16, _cleveland(lines), preview=mode == "preview", first_entry=first_entry
    )
    if mode == "dry":
        return _join(f"市场板块：{_sent_count(lines, message)} 行（公开仓库，内容已隐藏）", details, warnings)

    sent, reason = _send(channel, message)
    sent_lines = _sent(lines, message) if sent else []
    head = "市场板块预览" if mode == "preview" else "市场板块"
    outcome = f"已推送 {len(sent_lines)} 行（内容不公开）" if sent else f"未推送：{reason}"
    parts = [f"{head}：{outcome}", details]
    if mode == "send":
        if led is None:
            parts.append(f"账本未记录（{ledger_note}）")
        else:
            batch = _ledger_batch(rows, sent_lines, result.rows, checked) if sent else list(checked)
            stamped = [_stamp(row, send_time, edition, mode) for row in batch]
            if failure := _append(led, stamped, send_time):
                parts.append(f"账本没写进去（{failure}）")
            tokens = _tokens(stamped + [row for line in result.lines for row in line.rows] + result.rows)
            if ledger.assert_no_plaintext(state_dir, tokens):
                parts.append("自检发现问题")
    parts.append(warnings)
    return _join(*parts)


def _join(*parts: str) -> str:
    return "；".join(p for p in parts if p)


def _send(channel: Any, message: Message | None) -> tuple[bool, str | None]:
    if message is None:
        return False, "没有可发的内容"
    if channel is None:
        return False, "没有推送渠道"
    try:
        channel.send(message)
    except Exception as exc:  # 板块发不出去不影响简报
        log.warning("市场板块：钉钉发送失败：%s", type(exc).__name__)
        return False, type(exc).__name__
    log.info("市场板块：已推送到钉钉")
    return True, None


def _ledger_batch(
    rows: Sequence[dict], sent: Sequence[Line], built: Sequence[dict], checked: Sequence[dict]
) -> list[dict]:
    """发送成功后要记的：规则指纹（变了才记）、发出去的各行、价格变化的全部候选、核对记录。"""
    batch = _rules_rows(rows)
    batch += [dict(row) for line in sent for row in line.rows]
    shown_movers = {row.get("key") for line in sent if line.section == "mover" for row in line.rows}
    for row in built:
        if row.get("type") == "candidate":
            batch.append({**row, "shown": row.get("key") in shown_movers})
    return batch + [dict(row) for row in checked]


def _rules_rows(rows: Sequence[dict]) -> list[dict]:
    try:
        digest = rules_hash()
    except OSError:
        return []
    last = next((r.get("sha256") for r in reversed(rows) if r.get("type") == "rules"), None)
    return [] if last == digest else [{"type": "rules", "sha256": digest}]


def _stamp(row: Mapping[str, Any], ts: datetime, edition: str, mode: str) -> dict:
    out = _plain(dict(row))
    out.setdefault("ts", select.iso(ts))
    out.setdefault("edition", edition)
    out.setdefault("mode", mode)
    out.pop("seq", None)
    out.pop("salt", None)
    return out


def _plain(value: Any) -> Any:
    """换成能写进账本的 JSON：时间换成 ISO 字符串，NaN 和无穷换成 None，元组换成列表。"""
    if isinstance(value, Mapping):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    if isinstance(value, datetime):
        return select.iso(value)
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _append(led: Ledger, rows: list[dict], now: datetime) -> str | None:
    """追加记录；失败时返回原因（固定的中文或异常类型）。"""
    if not rows:
        return None
    try:
        led.append(rows, now)
    except LedgerError as exc:
        log.warning("市场账本：记录没写进去：%s", exc.reason)
        return exc.reason
    except Exception as exc:
        log.warning("市场账本：记录没写进去：%s", type(exc).__name__)
        return type(exc).__name__
    return None


def _tokens(rows: Iterable[Mapping[str, Any]]) -> list[str]:
    """自检时在公开文件里查找的合约代码和 condition id（不查问题文本）。"""
    found: set[str] = set()

    def walk(value: Any, kalshi: bool) -> None:
        if isinstance(value, Mapping):
            for k, v in value.items():
                if isinstance(v, str) and (k in ("key", "ticker") or (k == "event" and kalshi)):
                    found.update(part for part in v.split(":") if part)
                else:
                    walk(v, kalshi)
        elif isinstance(value, (list, tuple)):
            for v in value:
                walk(v, kalshi)

    for row in rows:
        if isinstance(row, Mapping):
            walk(row, row.get("venue") == "kalshi")
    return sorted(found)


def _open_ledger(markets_dir: Path, env: Mapping[str, str]) -> tuple[Keys | None, Ledger | None, str | None]:
    """(钥匙, 账本, 不能记账的原因)。没有钥匙、账本打不开或工作流没取到 state 分支时这次不记账，也不碰任何文件。"""
    keys = ledger.keys_from_env(env)
    if keys is None:
        return None, None, ledger.key_status(env) or ledger.NO_KEY
    if (env.get(STATE_OK_ENV) or "").strip().lower() == "false":
        log.warning("市场账本：%s", STATE_MISSING)
        return keys, None, STATE_MISSING
    try:
        return keys, Ledger.open(markets_dir, keys), None
    except LedgerError as exc:
        log.warning("市场账本打不开，这次不记账：%s", exc.reason)
        return keys, None, exc.reason
    except Exception as exc:
        log.warning("市场账本打不开，这次不记账：%s", type(exc).__name__)
        return keys, None, type(exc).__name__


def _read_summary(markets_dir: Path, keys: Keys | None) -> dict | None:
    """解密回测结果；没有、钥匙不对或被改过时都当作还没跑过回测。"""
    try:
        data = ledger.read_blob(markets_dir, BACKTEST_BLOB, keys)
    except LedgerError as exc:
        log.warning("市场板块：回测结果读不了：%s", exc.reason)
        return None
    except Exception as exc:
        log.warning("市场板块：回测结果读不了：%s", type(exc).__name__)
        return None
    return data if isinstance(data, dict) else None


def _score_rows(
    fetcher: Fetcher,
    settings: MarketsSettings,
    rows: list[dict],
    now: datetime,
    errors: list[tuple[str, str]],
    kalshi_dead: set[str] | None = None,
) -> list[dict]:
    """官方首次公布值和平台结算的核对记录；出任何错都当作这次没有，下次再查。

    kalshi_dead 是和后面板块共用的“连不上的 Kalshi 地址”，这里连不上的，板块就不再等。
    """
    found: list[Any] = []
    try:
        from digest.markets import score
    except Exception as exc:
        errors.append(("witness", type(exc).__name__))
        return []
    try:
        found += list(score.collect_witnesses(fetcher, settings, rows, now) or [])
    except Exception as exc:
        errors.append(("witness", type(exc).__name__))
        log.warning("市场板块：官方数据核对出错：%s", type(exc).__name__)
    try:
        kalshi = Kalshi(fetcher, settings.kalshi.hosts)
        if kalshi_dead is not None:
            kalshi.dead = kalshi_dead
        found += list(score.collect_settlements(kalshi, Polymarket(fetcher), rows, now, limit=SETTLE_LIMIT) or [])
    except Exception as exc:
        errors.append(("settle", type(exc).__name__))
        log.warning("市场板块：结算核对出错：%s", type(exc).__name__)
    return [dict(row) for row in found if isinstance(row, Mapping)]


def _llm(client: httpx.Client, config: Config, env: Mapping[str, str]) -> MarketLLM | None:
    try:
        return MarketLLM.from_env(client, config, env)
    except Exception as exc:
        log.warning("市场板块：大模型不可用，问题用英文原文：%s", type(exc).__name__)
        return None


# ---- 历史回测 ----


def run_replay(
    client: httpx.Client,
    config: Config,
    now_fn: Callable[[], datetime],
    state_dir: Path | str,
    channel: Any,
    env: Mapping[str, str] = os.environ,
) -> str:
    """Kalshi 历史回测。channel 为 None 时是试运行：每类只算最近两次，只报计数，不推送、不保存。

    正式运行：推送【回测】消息；有钥匙时把结果加密保存并记一条 backtest 记录（推送失败也保存，回测跑一次很久）。
    一次都没算出来时不推送也不保存；没跑完整（部分完成或有请求出错）而且算出的比上次保存的少时，
    推送这次的，但不盖掉上次保存的（板块的灰色小字一直用上次完整的成绩）。
    """
    quiet_http_logs()
    settings = load_settings()
    if settings.error:
        return DISABLED_STATUS
    with _markets_http(client) as http:
        return _run_replay(http, config, now_fn, Path(state_dir), channel, env, settings)


def _run_replay(
    http: httpx.Client,
    config: Config,
    now_fn: Callable[[], datetime],
    state_dir: Path,
    channel: Any,
    env: Mapping[str, str],
    settings: MarketsSettings,
) -> str:
    """run_replay 的实现，市场数据走 http。"""
    dry = channel is None
    head = "市场回测（试运行）" if dry else "市场回测"
    markets_dir = state_dir / "markets"
    keys: Keys | None = None
    led: Ledger | None = None
    note: str | None = None
    if not dry:
        keys, led, note = _open_ledger(markets_dir, env)
    budget = settings.replay_budget
    if dry:
        budget = replace(budget, seconds=min(float(budget.seconds), DRY_REPLAY_SECONDS))
    fetcher = _make_fetcher(http, budget)

    from digest.markets import backtest
    from digest.markets.render import render_backtest

    summary = backtest.run(fetcher, settings, _aware(now_fn()), dry)
    summary = _plain(summary) if isinstance(summary, Mapping) else {}
    counts, events, scored = _replay_counts(summary, fetcher.requests)
    details = _venues(fetcher.hosts, settings)
    partial = "部分完成" if summary.get("partial") is True else ""
    if dry:
        return _join(f"{head}：{counts}", partial, details)
    if scored == 0:
        # 一次都没算出来（多半是连不上 Kalshi）：不推送空消息，也不能盖掉上次保存的好结果
        return _join(f"{head}：没有算出任何事件，没有推送，结果没有保存", counts, partial, details)

    sent, reason = _send(channel, render_backtest(summary, _aware(now_fn())))
    parts = [f"{head}：" + ("已推送（内容不公开）" if sent else f"未推送：{reason}"), counts, partial]
    if led is None or keys is None:
        parts.append(f"结果没有保存（{note}）")
    elif (kept := _keeps_previous(markets_dir, keys, summary, scored)) is not None:
        parts.append(f"结果没有保存（{kept}）")
    else:
        stored = _store_replay(markets_dir, led, keys, summary, events, config.edition, _aware(now_fn()))
        parts.append("结果已加密保存" if stored is None else f"结果没有保存（{stored}）")
        if ledger.assert_no_plaintext(state_dir):
            parts.append("自检发现问题")
    parts.append(details)
    return _join(*parts)


def _keeps_previous(markets_dir: Path, keys: Keys, summary: Mapping[str, Any], scored: int) -> str | None:
    """这次没跑完整（部分完成或有出错的请求）而且算出的比上次保存的少时，留着上次的结果，返回原因；否则返回 None。"""
    errors = _count(summary.get("errors")) or 0
    if summary.get("partial") is not True and errors == 0:
        return None
    previous = _read_summary(markets_dir, keys)
    if previous is None:
        return None
    _, _, before = _replay_counts(previous, 0)
    if scored >= before:
        return None
    log.warning("市场回测：这次没跑完整，算出的比上次少，留着上次的结果")
    return "没跑完整，比上次保存的少，留着上次的"


def _store_replay(
    markets_dir: Path, led: Ledger, keys: Keys, summary: dict, events: int, edition: str, now: datetime
) -> str | None:
    """先写加密的回测结果，再记一条带它指纹的 backtest 记录；失败时返回原因。"""
    try:
        sha = ledger.write_blob(markets_dir, BACKTEST_BLOB, summary, keys)
    except LedgerError as exc:
        log.warning("市场回测：结果没保存：%s", exc.reason)
        return exc.reason
    except Exception as exc:
        log.warning("市场回测：结果没保存：%s", type(exc).__name__)
        return type(exc).__name__
    row = {"type": "backtest", "sha256": sha, "events": events, "partial": summary.get("partial") is True}
    return _append(led, [_stamp(row, now, edition, "backtest")], now)


def _count(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return int(value) if value == int(value) and value >= 0 else None


def _replay_counts(summary: Mapping[str, Any], requests: int) -> tuple[str, int, int]:
    """“5 类合约 10 次事件，算出 10 次；请求 64 次”，以及事件总数（记进 backtest 记录）和算出的次数。

    回测结果里有 series_count / events / scored 时直接用；没有时按各类合约各时刻的最大 n 加起来算“算出”，
    再加上跳过的次数。请求次数用 Fetcher 实际发出的。
    """
    series = summary.get("series")
    entries = [e for e in series.values() if isinstance(e, Mapping)] if isinstance(series, Mapping) else []
    kinds = _count(summary.get("series_count"))
    if kinds is None:
        kinds = len(entries)
    scored = _count(summary.get("scored"))
    if scored is None:
        scored = sum(_max_n(entry) for entry in entries)
    skipped = _count(summary.get("skipped")) or 0
    events = _count(summary.get("events"))
    if events is None:
        events = scored + skipped
    return f"{kinds} 类合约 {events} 次事件，算出 {scored} 次；请求 {requests} 次", events, scored


def _max_n(entry: Mapping[str, Any]) -> int:
    labels = entry.get("labels") if isinstance(entry.get("labels"), Mapping) else entry
    counts = [_count(v.get("n")) for v in labels.values() if isinstance(v, Mapping)]
    return max((n for n in counts if n is not None), default=0)


# ---- 网络和日志 ----


def _clock() -> float:
    return time.monotonic()


def _sleep(seconds: float) -> None:
    time.sleep(seconds)


def _markets_client(client: httpx.Client) -> httpx.Client | None:
    """给市场数据新建一个连接层不重试的客户端（请求头、超时、跟随跳转都和共享客户端一样）。

    共享客户端（digest.http.make_client）的连接层会把连不上的请求自己再试两次，每次都等满连接超时，
    Fetcher 看不见也算不进预算，一次请求可能等三倍的时间。市场数据的重试只由 Fetcher 按预算决定。
    共享客户端不是真正联网的（测试里的假接口）时返回 None，直接用它。
    """
    if not isinstance(getattr(client, "_transport", None), httpx.HTTPTransport):
        return None
    return httpx.Client(
        timeout=client.timeout,
        headers=client.headers,
        follow_redirects=client.follow_redirects,
        transport=httpx.HTTPTransport(retries=0),
    )


@contextmanager
def _markets_http(client: httpx.Client) -> Iterator[httpx.Client]:
    """市场数据用的客户端，用完关掉（只关自己新建的）。"""
    own = _markets_client(client)
    try:
        yield own or client
    finally:
        if own is not None:
            own.close()


def _make_fetcher(
    client: httpx.Client, budget: BudgetSettings, share: float = 1.0, after: Fetcher | None = None
) -> Fetcher:
    """按规则文件的预算建 Fetcher。share 只给一部分预算；after 表示扣掉前一个 Fetcher 已经用掉的。"""
    seconds, requests = float(budget.seconds), int(budget.requests)
    if after is not None:
        seconds = max(0.0, seconds - (after.budget.clock() - after.budget.start))
        requests = max(0, requests - after.budget.used)
    if share < 1:
        seconds, requests = seconds * share, min(requests, max(1, int(requests * share)))
    return Fetcher(
        client,
        Budget(seconds, requests, clock=_clock),
        timeout=budget.timeout,
        max_bytes=budget.max_bytes,
        sleep=_sleep,
        min_interval=budget.min_interval,
    )


def quiet_http_logs() -> None:
    """httpx 在 INFO 级别会记下完整的请求地址（里面有合约代码和 condition id），市场板块运行时一律调到 WARNING。"""
    for name in ("httpx", "httpcore"):
        logger = logging.getLogger(name)
        if logger.level < logging.WARNING:
            logger.setLevel(logging.WARNING)


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)
