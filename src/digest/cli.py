from __future__ import annotations

import argparse
import logging
import os
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from digest.channels.base import ChannelError
from digest.channels.dingtalk import DingTalk
from digest.config import load_config
from digest.http import make_client
from digest.models import Item
from digest.pipeline import select
from digest.render import archive, dingtalk
from digest.sources import build_sources

log = logging.getLogger("digest")


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in ("1", "true", "yes")


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="digest", description="生成并推送每日简报")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=_truthy(os.environ.get("DIGEST_DRY_RUN")),
        help="只生成预览，不发送（也可用环境变量 DIGEST_DRY_RUN=true）",
    )
    parser.add_argument("--out", type=Path, help="把钉钉消息和完整版写到这个目录")
    return parser.parse_args(argv)


def write_step_summary(message: dingtalk.Message) -> None:
    """在 GitHub Actions 运行页面上展示预览。"""
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as f:
        f.write(f"## 钉钉消息预览\n\n通知标题：{message.title}\n\n---\n\n{message.text}\n")


def setup_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    # httpx 在 INFO 级别会记录完整请求 URL，其中有钉钉的 access_token 和签名
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)


def main(argv: list[str] | None = None) -> int:
    setup_logging()
    args = parse_args(argv)
    config = load_config()
    now = datetime.now(ZoneInfo(config.timezone))
    sources = build_sources(config.sources)

    candidates: list[Item] = []
    failed = 0
    with make_client() as client:
        for source in sources:
            try:
                items = source.fetch(client, now)
            except Exception as exc:  # 单个来源失败不影响其他来源
                failed += 1
                log.warning("信息源 %s 抓取失败：%s", source.name, type(exc).__name__)
                continue
            log.info("信息源 %s：%d 条候选", source.name, len(items))
            candidates.extend(items)

        if sources and failed == len(sources):
            log.error("所有信息源都抓取失败，今天不推送")
            return 1

        selection = select(candidates, sources, config)
        log.info("入选 %d 条，落选 %d 条", len(selection.items), len(selection.rejected))
        message = dingtalk.render(selection, config, now)
        full = archive.render(selection, config, now)

        if args.out:
            args.out.mkdir(parents=True, exist_ok=True)
            (args.out / "dingtalk.md").write_text(f"<!-- {message.title} -->\n\n{message.text}\n", encoding="utf-8")
            (args.out / "archive.md").write_text(full, encoding="utf-8")

        if args.dry_run:
            print(f"通知标题：{message.title}\n\n{message.text}")
            write_step_summary(message)
            return 0
        if not selection.items:
            log.info("今天没有达到门槛的内容，不推送")
            return 0
        try:
            DingTalk.from_env(client).send(message)
        except ChannelError as exc:
            log.error("%s", exc)
            return 1
        log.info("已推送到钉钉")
    return 0
