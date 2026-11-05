from __future__ import annotations

import argparse
import json
import logging
import os
from collections.abc import Callable, Sequence
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from digest.channels.base import ChannelError
from digest.channels.dingtalk import DingTalk
from digest import history, kev
from digest.config import Config, load_config
from digest.http import make_client
from digest.llm import build_enricher
from digest.models import Item
from digest.pipeline import Selection, select
from digest.render import archive, dingtalk
from digest.sources import build_sources
from digest.sources.base import Source
from digest.sources.kev import KnownExploited
from digest.state import State

log = logging.getLogger("digest")

EDITIONS = ("morning", "evening")
# GitHub 自带的定时任务带不了参数，按 cron 的钟点判断是哪一版：当地时间这个钟点及以后的是晚间版
EVENING_FROM_HOUR = 15


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in ("1", "true", "yes")


def edition_for_cron(cron: str, timezone: str) -> str:
    """GitHub 定时触发属于哪一版。cron 用 UTC，第二段是小时；解析不了时按早间版处理。"""
    try:
        hour = int(cron.split()[1])
    except (IndexError, ValueError):
        return "morning"
    local = datetime.now(ZoneInfo("UTC")).replace(hour=hour, minute=0).astimezone(ZoneInfo(timezone))
    return "evening" if local.hour >= EVENING_FROM_HOUR else "morning"


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="digest", description="生成并推送每日简报")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=_truthy(os.environ.get("DIGEST_DRY_RUN")),
        help="只生成预览，不发送（也可用环境变量 DIGEST_DRY_RUN=true）",
    )
    parser.add_argument(
        "--scheduled",
        action="store_true",
        default=_truthy(os.environ.get("DIGEST_SCHEDULED")),
        help="按定时推送处理：当天已经推送过就跳过（也可用环境变量 DIGEST_SCHEDULED=true）",
    )
    parser.add_argument(
        "--edition",
        default=(os.environ.get("DIGEST_EDITION") or "").strip().lower() or None,
        help="推送哪一版：morning 早间版（默认），evening 晚间版（也可用环境变量 DIGEST_EDITION）",
    )
    parser.add_argument("--out", type=Path, help="把钉钉消息和完整版写到这个目录")
    parser.add_argument(
        "--archive-dir",
        type=Path,
        help="正式推送时把完整版写到这个目录的 YYYY/MM-DD.md（工作流随后提交到 state 分支）",
    )
    parser.add_argument(
        "--state",
        type=Path,
        default=Path(".state/state.json"),
        help="跨天去重状态文件（工作流从 state 分支取出），不存在时视为第一次运行",
    )
    parser.add_argument(
        "--markets-preview",
        action="store_true",
        default=_truthy(os.environ.get("DIGEST_MARKETS_PREVIEW")),
        help="不发简报，只推送一条【预览】市场板块，不记账、不改去重状态；同时用 --dry-run 时只组装不推送"
        "（需要 MARKETS_ENABLED；也可用环境变量 DIGEST_MARKETS_PREVIEW=true）",
    )
    parser.add_argument(
        "--markets-backtest",
        action="store_true",
        default=_truthy(os.environ.get("DIGEST_MARKETS_BACKTEST")),
        help="不发简报，跑一次 Kalshi 历史回测，推送【回测】并加密保存结果；同时用 --dry-run 时只试算、只报计数"
        "（需要 MARKETS_ENABLED；也可用环境变量 DIGEST_MARKETS_BACKTEST=true）",
    )
    args = parser.parse_args(argv)
    if args.edition is not None and args.edition not in EDITIONS:
        parser.error(f"--edition 只能是 {' 或 '.join(EDITIONS)}，现在是 {args.edition!r}")
    if args.markets_preview and args.markets_backtest:
        parser.error("--markets-preview 和 --markets-backtest 一次只能选一个")
    return args


def source_report(stats: dict[str, str], selection: Selection) -> str:
    chosen: dict[str, int] = {}
    for item in selection.items:
        chosen[item.source] = chosen.get(item.source, 0) + 1
    rows = [f"| {sid} | {status} | {chosen.get(sid, 0)} |" for sid, status in stats.items()]
    return "\n".join(["| 信息源 | 抓取结果 | 入选 |", "| --- | --- | --- |", *rows])


def _append_env_file(name: str, text: str) -> None:
    """写入 GitHub Actions 提供的文件（运行页面的 Summary、步骤输出）；本地运行时没有这些文件，什么也不做。"""
    if path := os.environ.get(name):
        with open(path, "a", encoding="utf-8") as f:
            f.write(text)


def write_step_summary(message: dingtalk.Message, report: str, full: str, preview: bool = True) -> None:
    """在 GitHub Actions 运行页面上展示这次的消息、各信息源情况和完整版。"""
    heading = "钉钉消息预览（未发送）" if preview else "钉钉消息"
    _append_env_file(
        "GITHUB_STEP_SUMMARY",
        f"## {heading}\n\n通知标题：{message.title}\n\n---\n\n{message.text}\n\n---\n\n"
        f"## 各信息源\n\n{report}\n\n<details><summary>完整版（含落选条目）</summary>\n\n{full}\n</details>\n",
    )


def archive_path(config: Config, now: datetime) -> str:
    """完整版在归档目录里的相对路径：早间版 YYYY/MM-DD.md，晚间版 YYYY/MM-DD-evening.md。"""
    return f"{now:%Y}/{now:%m-%d}{'-evening' if config.is_evening else ''}.md"


def archive_link(config: Config, now: datetime) -> str | None:
    """当天完整版的网页地址。没有在配置里指定时，用本仓库 state 分支上的 archive/ 目录。"""
    base = config.archive_base_url
    if not base and (repo := os.environ.get("GITHUB_REPOSITORY")):
        server = os.environ.get("GITHUB_SERVER_URL", "https://github.com")
        base = f"{server}/{repo}/blob/state/archive"
    return f"{base.rstrip('/')}/{archive_path(config, now)}" if base else None


def now_in(timezone: str) -> datetime:
    return datetime.now(ZoneInfo(timezone))


def skip_reason(config: Config, state: State, now: datetime) -> tuple[str, str] | None:
    """定时触发时，这一版现在不该推送的原因：（Summary 标题，日志说明）。"""
    today = now.date().isoformat()
    if config.is_evening and not config.evening.enabled:
        return "晚间版已停用，跳过", "config/sources.yaml 里停用了晚间版（evening.enabled: false）"
    if state.done(config.edition, now.date()):
        if config.is_evening:
            return "已推送过，跳过", f"今天（{today}）的晚间版已经处理过，定时任务不再重复推送"
        return "已推送过，跳过", f"今天（{today}）已经推送过，定时任务不再重复推送"
    # 晚间版的兜底触发可能被 GitHub 拖到深夜甚至第二天，这时不再推送，避免深夜打扰
    if config.is_evening and not 12 <= now.hour < config.evening.latest_hour:
        return "时间太晚，跳过", f"晚间版的触发到得太晚（{now:%H:%M}），今晚不再推送"
    return None


# ---- 市场与预测板块（只在仓库变量 MARKETS_ENABLED 打开时运行，见 README「市场与预测」） ----

MARKETS_LOAD_FAILED = "市场板块：未启用（加载失败：{}）"
# 没有新闻可推的早上不记“今天已推送”（last_sent），后面几路定时触发都会再跑一遍，板块不能每次都再推一条：
# 在 state 目录里记下每一版板块推送成功的日期，定时触发时同一版同一天只推一次。文件里只有日期，没有任何市场内容
MARKETS_SENT_FILE = "markets_sent.json"
MARKETS_SENT = "市场板块：已推送"  # run_panel 推送成功时状态行的开头
MARKETS_ALREADY = "市场板块：这一版今天已推送过，跳过"


def markets_enabled() -> bool:
    """CI 和 fork 默认没有这个变量：这时市场板块的代码一行都不运行，简报的行为和以前完全一样。"""
    return _truthy(os.environ.get("MARKETS_ENABLED"))


def report_markets(status: str, echo: bool = False) -> None:
    """板块在 Summary 里只占一行状态，不含任何市场内容（仓库是公开的，板块内容只发到钉钉）。

    echo 为真（试运行）时标准输出也只打印这一行，否则写进日志。
    """
    try:
        _append_env_file("GITHUB_STEP_SUMMARY", f"\n{status}\n")
    except OSError as exc:  # 简报已经推送，写不进 Summary 也不能让这次运行失败
        log.warning("市场板块的状态没写进 Summary：%s", type(exc).__name__)
    if echo:
        print(status)
    else:
        log.info("%s", status)


def call_markets(fallback: str, run: Callable[[Any], str]) -> str:
    """按需导入 digest.markets，用 run 调用它的一个入口，返回状态行。

    导入失败（缺依赖等）或入口意外抛出异常时，都只变成一行状态：fallback 加异常类型。
    """
    try:
        from digest import markets
    except Exception as exc:  # 包坏了也不能影响简报
        return MARKETS_LOAD_FAILED.format(type(exc).__name__)
    try:
        return run(markets)
    except Exception as exc:  # 入口本身不抛异常，这里再兜一层
        log.warning("市场板块出错：%s", type(exc).__name__)
        return f"{fallback}：{type(exc).__name__}"


def markets_sent_dates(state_dir: Path) -> dict[str, str]:
    """各版板块最近一次推送成功的日期：{"morning": "YYYY-MM-DD", ...}；没有或读不了时为空。"""
    try:
        data = json.loads((state_dir / MARKETS_SENT_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.items() if isinstance(k, str) and isinstance(v, str)}


def mark_markets_sent(state_dir: Path, edition: str, today: date) -> None:
    dates = markets_sent_dates(state_dir)
    dates[edition] = today.isoformat()
    try:
        state_dir.mkdir(parents=True, exist_ok=True)
        (state_dir / MARKETS_SENT_FILE).write_text(json.dumps(dates, sort_keys=True) + "\n", encoding="utf-8")
    except OSError as exc:  # 记不下来最多是多推一次，不能影响这次运行
        log.warning("市场板块的推送日期没记下来：%s", type(exc).__name__)


def markets_panel(
    client: httpx.Client,
    config: Config,
    now_fn: Callable[[], datetime],
    state_dir: Path,
    news: Sequence[Item],
    channel: DingTalk | None,
    dry: bool = False,
    once: bool = False,
) -> None:
    """简报之后跑市场板块：推送并记账（dry 时只组装，不推送不记账）。news 是简报里实际显示的条目，没推简报时为空。

    once 为真（定时触发）时，这一版今天已经推送过板块就跳过。推送成功后记下日期。
    只在 MARKETS_ENABLED 打开时运行；从不抛异常，不影响退出码，Summary 只多一行状态。
    """
    if not markets_enabled():
        return
    edition = "evening" if config.edition == "evening" else "morning"
    today = now_fn().date()
    if once and not dry and markets_sent_dates(state_dir).get(edition) == today.isoformat():
        report_markets(MARKETS_ALREADY)
        return
    mode = "dry" if dry else "send"
    status = call_markets(
        "市场板块：出错" if dry else "市场板块：未推送",
        lambda markets: markets.run_panel(client, config, now_fn, state_dir, list(news), channel, mode=mode),
    )
    report_markets(status, echo=dry)
    if not dry and status.startswith(MARKETS_SENT):
        mark_markets_sent(state_dir, edition, today)


def markets_only(args: argparse.Namespace, config: Config) -> int:
    """--markets-preview / --markets-backtest：不抓新闻、不发简报、不碰去重状态和归档，只跑市场板块。

    预览只推送【预览】，不记账；回测推送【回测】，有账本钥匙时加密保存结果。两者都服从 --dry-run。
    钉钉配置有误时和简报一样尽早失败；板块本身出错只写一行状态，退出码仍是 0。
    """
    if not markets_enabled():
        head = "市场回测" if args.markets_backtest else "市场板块预览"
        report_markets(f"{head}：未运行（仓库变量 MARKETS_ENABLED 没有打开）", echo=args.dry_run)
        return 0

    def now_fn() -> datetime:
        return now_in(config.timezone)

    state_dir = args.state.parent
    with make_client() as client:
        channel: DingTalk | None = None
        if not args.dry_run:
            try:
                channel = DingTalk.from_env(client)
            except ChannelError as exc:
                log.error("%s", exc)
                return 1
        if args.markets_backtest:
            # channel 为 None（试运行）时每类合约只算最近两次，只报计数，不推送不保存
            status = call_markets(
                "市场回测：出错",
                lambda markets: markets.run_backtest(client, config, now_fn, state_dir, channel),
            )
        elif args.dry_run:
            # 预览也服从 --dry-run：只组装不推送，状态行和简报试运行时的一样
            status = call_markets(
                "市场板块：出错",
                lambda markets: markets.run_panel(client, config, now_fn, state_dir, [], None, mode="dry"),
            )
        else:
            status = call_markets(
                "市场板块预览：未推送",
                lambda markets: markets.run_preview(client, config, now_fn, state_dir, channel),
            )
    report_markets(status, echo=args.dry_run)
    return 0


def setup_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    # httpx 在 INFO 级别会记录完整请求 URL，其中有钉钉的 access_token 和签名
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)


def main(argv: list[str] | None = None) -> int:
    setup_logging()
    args = parse_args(argv)
    config = load_config()
    edition = args.edition
    if edition is None:
        # GitHub 自带的定时任务带不了参数，按 cron 判断；本地运行默认早间版
        cron = os.environ.get("DIGEST_CRON", "").strip()
        edition = edition_for_cron(cron, config.timezone) if cron else "morning"
    if edition == "evening":
        config = config.for_evening()
    if args.markets_preview or args.markets_backtest:
        return markets_only(args, config)
    now = now_in(config.timezone)

    def now_fn() -> datetime:
        # 市场板块发送前要用真实的当前时间去掉刚到公布时间的行，不能用开始运行时的 now
        return now_in(config.timezone)

    # 市场板块只写 .state/markets/ 下的加密账本，和 state.json 在同一个目录
    state_dir = args.state.parent
    sources = build_sources(config.sources)
    state = State(args.state)

    # 晚间版达标太少不推送时，要把这次在博客列表页上新看到的文章还回去，留给第二天的早间版
    pages_before = {source_id: list(links) for source_id, links in state.pages.items()}
    candidates: list[Item] = []
    stats: dict[str, str] = {}
    failed = 0
    # 定时推送有几路触发（外部定时服务和 GitHub 自带的定时任务互为备用），同一天会到达多次，只推一次
    scheduled = args.scheduled or os.environ.get("GITHUB_EVENT_NAME") == "schedule"
    with make_client() as client:
        channel: DingTalk | None = None
        if not args.dry_run:
            if scheduled and (skip := skip_reason(config, state, now)):
                heading, reason = skip
                log.info("%s", reason)
                _append_env_file(
                    "GITHUB_STEP_SUMMARY",
                    f"## {heading}\n\n{reason}。这次触发没有发送消息，去重记录和归档也没有改动。\n",
                )
                # 工作流据此跳过上传和保存这两步
                _append_env_file("GITHUB_OUTPUT", "skipped=true\n")
                return 0
            # 钉钉配置有误时尽早失败，不白白抓取和调用大模型
            try:
                channel = DingTalk.from_env(client)
            except ChannelError as exc:
                log.error("%s", exc)
                return 1

        for source in sources:
            try:
                items = source.fetch(client, now, state)
            except Exception as exc:  # 单个来源失败不影响其他来源
                failed += 1
                stats[source.id] = f"失败：{type(exc).__name__}"
                log.warning("信息源 %s 抓取失败：%s %s", source.id, type(exc).__name__, _safe_detail(exc))
                continue
            stats[source.id] = f"{len(items)} 条候选"
            log.info("信息源 %s：%d 条候选", source.id, len(items))
            candidates.extend(items)

        if sources and failed == len(sources):
            log.error("所有信息源都抓取失败，今天不推送")
            return 1

        catalog = exploited_catalog(client, sources, candidates, config)
        if marked := kev.annotate(candidates, catalog):
            log.info("%d 条候选提到的漏洞已列入 CISA 已知被利用漏洞目录", marked)
        # 按天限量的领域（如安全）要算上今天另一版已经推过的
        sent_today = history.topics_sent(args.archive_dir, now.date(), config.edition) if args.archive_dir else None
        enricher = build_enricher(client, config)
        selection = select(
            candidates,
            sources,
            config,
            state,
            review=enricher.review if enricher else None,
            dedupe=enricher.duplicates if enricher else None,
            topics_sent=sent_today,
        )
        # 晚间版是补充，达标的太少就不打扰
        quiet = config.is_evening and len(selection.items) < config.evening.min_items
        if enricher and selection.items and not quiet:
            selection.highlights = enricher.highlights(selection.items)
        llm_status = enricher.status() if enricher else "大模型：未启用，使用原文标题和简介"
        for rejected in selection.rejected:
            if rejected.reason.startswith("大模型"):
                # 便于按日志调整评分门槛；标题本身是公开内容
                log.info("大模型淘汰：%s（%s）", rejected.item.title[:80], rejected.reason)
        log.info("入选 %d 条，落选 %d 条；%s", len(selection.items), len(selection.rejected), llm_status)
        full = archive.render(selection, config, now, status=llm_status)
        archive_url = None
        if not args.dry_run and args.archive_dir and not quiet:
            path = args.archive_dir / archive_path(config, now)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(full, encoding="utf-8")
            archive_url = archive_link(config, now)
        message = dingtalk.render(selection, config, now, archive_url)

        if args.out:
            args.out.mkdir(parents=True, exist_ok=True)
            (args.out / "dingtalk.md").write_text(f"<!-- {message.title} -->\n\n{message.text}\n", encoding="utf-8")
            (args.out / "archive.md").write_text(full, encoding="utf-8")

        report = f"{llm_status}\n\n{source_report(stats, selection)}"
        if quiet:
            note = f"晚间版只有 {len(selection.items)} 条达标，少于 {config.evening.min_items} 条，今晚不推送"
            log.info("%s", note)
            report = f"{note}\n\n{report}"
        write_step_summary(message, report, full, preview=channel is None or quiet or not selection.items)
        if args.dry_run or channel is None:
            print(f"通知标题：{message.title}\n\n{message.text}\n\n{report}")
            # 冒烟测试：真实抓取并组装板块，不推送、不记账；Summary 和标准输出只多一行状态
            markets_panel(client, config, now_fn, state_dir, message.items, None, dry=True)
            return 0
        if quiet:
            state.pages = pages_before
            state.last_evening = now.date().isoformat()
            state.save(now.date())
            # 简报今晚不推，今晚有美国数据时板块照样推送
            markets_panel(client, config, now_fn, state_dir, [], channel, once=scheduled)
            return 0
        if not selection.items:
            log.info("今天没有达到门槛的内容，不推送")
            state.save(now.date())
            # 这时不记“今天已推送”，后面的定时触发还会再来：板块同一版同一天只推一次
            markets_panel(client, config, now_fn, state_dir, [], channel, once=scheduled)
            return 0
        try:
            channel.send(message)
        except ChannelError as exc:
            log.error("%s", exc)
            return 1
        log.info("已推送到钉钉")
        state.mark_sent(selection.items, now.date(), config.edition)
        state.save(now.date())
        if args.archive_dir:
            try:
                history.update(args.archive_dir, selection, config, now.date())
            except Exception as exc:  # 目录页只是方便回查，不能因此丢掉去重状态
                log.warning("归档目录更新失败：%s", type(exc).__name__)
        # 简报已送达、去重状态已保存之后才跑板块；“今天第 n 条”对应消息里实际显示的条目
        # 定时触发时，同一天早些时候没有新闻、只推过板块的，这次不再重复推板块
        markets_panel(client, config, now_fn, state_dir, message.items, channel, once=scheduled)
    return 0


def exploited_catalog(
    client: httpx.Client, sources: list[Source], candidates: list[Item], config: Config
) -> kev.Catalog | None:
    """CISA 已知被利用漏洞目录：早间版的漏洞来源已经下载过就直接用；晚间版没有这个来源，
    有候选提到 CVE 编号时才单独下载。下载失败只是少了“已被利用”的标注，照常推送。"""
    if not config.mark_exploited:
        return None
    if catalog := next((s.catalog for s in sources if isinstance(s, KnownExploited) and s.catalog), None):
        return catalog
    if not any(kev.cve_ids(item.title, item.summary) for item in candidates):
        return None
    try:
        return kev.Catalog.fetch(client)
    except Exception as exc:  # 标注只是增强
        log.warning("已知被利用漏洞目录下载失败，这次不标注：%s %s", type(exc).__name__, _safe_detail(exc))
        return None


def _safe_detail(exc: Exception) -> str:
    """抓取失败时给出 HTTP 状态码，便于排查；不输出完整异常文本（可能含 URL 参数）。"""
    if isinstance(exc, httpx.HTTPStatusError):
        return f"HTTP {exc.response.status_code}"
    if isinstance(exc, ValueError):
        return str(exc)[:200]
    return ""
