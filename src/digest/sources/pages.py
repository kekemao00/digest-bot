from __future__ import annotations

import html
import logging
import re
from datetime import datetime
from typing import TYPE_CHECKING
from urllib.parse import urljoin

import httpx

from digest.models import Item
from digest.render.text import clean
from digest.sources.base import Source

if TYPE_CHECKING:
    from digest.state import State

log = logging.getLogger("digest")

_HREF = re.compile(r'href="([^"#?]+)"')
_META = re.compile(r'<meta\s+[^>]*?(?:property|name)="(og:title|og:description|description)"[^>]*?content="([^"]*)"', re.I)
_META_REVERSED = re.compile(r'<meta\s+[^>]*?content="([^"]*)"[^>]*?(?:property|name)="(og:title|og:description|description)"', re.I)
_TITLE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)
# 每次最多跟进的新文章数，避免页面改版时一次抓几十个链接
MAX_NEW = 5


def page_meta(text: str) -> dict[str, str]:
    meta = {k.lower(): v for k, v in _META.findall(text)}
    meta.update({k.lower(): v for v, k in _META_REVERSED.findall(text) if k.lower() not in meta})
    if "og:title" not in meta and (m := _TITLE.search(text)):
        meta["og:title"] = m.group(1)
    return {k: clean(html.unescape(v)) for k, v in meta.items()}


class NewsPage(Source):
    """没有 RSS 的博客：比较列表页上的文章链接，新出现的链接再打开读取标题和简介。

    第一次运行只记录现有文章，不推送，所以不会把旧文章当成新闻。
    """

    type = "page"

    def fetch(self, client: httpx.Client, now: datetime, state: State) -> list[Item]:
        url = self.options["url"]
        resp = client.get(url)
        resp.raise_for_status()
        pattern = re.compile(self.options["link_pattern"])
        links = list(dict.fromkeys(urljoin(url, h) for h in _HREF.findall(resp.text) if pattern.search(urljoin(url, h))))
        if not links:
            raise ValueError("列表页上没有找到文章链接，页面结构可能变了")
        new = state.new_links(self.id, links)
        if new is None:
            log.info("信息源 %s 第一次运行，记录现有 %d 篇文章，不推送", self.id, len(links))
            return []
        items = []
        for link in new[:MAX_NEW]:
            try:
                page = client.get(link)
                page.raise_for_status()
            except httpx.HTTPError as exc:
                log.warning("信息源 %s 打开新文章失败：%s", self.id, type(exc).__name__)
                continue
            meta = page_meta(page.text)
            title = meta.get("og:title")
            if not title:
                continue
            summary = meta.get("og:description") or meta.get("description")
            items.append(self.item(id=link, title=title, url=link, published_at=now, summary=summary or None))
        return items

    def base_score(self, item: Item) -> float:
        return 50.0
