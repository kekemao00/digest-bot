from __future__ import annotations

import re
from datetime import datetime, time, timedelta, timezone
from typing import TYPE_CHECKING, Any

import httpx

from digest.kev import URL, Catalog, added_on, is_ransomware
from digest.models import Exploited, Item
from digest.sources.base import Source

if TYPE_CHECKING:
    from digest.state import State

_LINK = re.compile(r"https?://[^\s;,]+")
NVD = "https://nvd.nist.gov/vuln/detail/{cve}"


class KnownExploited(Source):
    """CISA 已知被利用漏洞目录里新收录的漏洞：每一条都有被实际利用的证据。

    目录只给收录日期，同一天收录的按是否被勒索软件使用排前后。抓到的目录同时用来给其他条目标注“已被利用”。
    """

    type = "kev"
    default_name = "CISA 被利用漏洞"

    def __init__(self, options: dict[str, Any]) -> None:
        super().__init__(options)
        self.catalog: Catalog | None = None

    def fetch(self, client: httpx.Client, now: datetime, state: State) -> list[Item]:
        self.now = now
        self.catalog = Catalog.fetch(client, self.options.get("url") or URL)
        since = (now - timedelta(hours=float(self.options.get("window_hours", 72)))).date()
        items = []
        for cve, vuln in self.catalog.entries.items():
            added = added_on(vuln)
            if added is None or added < since:
                continue
            title = str(vuln.get("vulnerabilityName") or "").strip()
            if not title:
                continue
            links = _LINK.findall(str(vuln.get("notes") or ""))
            nvd = NVD.format(cve=cve)
            # 优先链到厂商公告（有没有补丁、怎么缓解都在那里），NVD 作为补充链接
            vendor = next((link for link in links if "nvd.nist.gov" not in link), None)
            ransomware = is_ransomware(vuln)
            cwes = [str(c) for c in vuln.get("cwes") or [] if str(c).startswith("CWE-")]
            items.append(
                self.item(
                    id=cve,
                    title=title,
                    url=vendor or nvd,
                    links=[("NVD", nvd)] if vendor else [],
                    published_at=datetime.combine(added, time(), tzinfo=timezone.utc),
                    summary=str(vuln.get("shortDescription") or "").strip() or None,
                    extras=[cve, *cwes[:1]],
                    cves=[cve],
                    exploited=Exploited(cve=cve, added=added, ransomware=ransomware),
                )
            )
        return items

    def base_score(self, item: Item) -> float:
        # 越新越靠前；同一天收录的，勒索软件在用的排前面
        hours = (getattr(self, "now", None) or datetime.now(timezone.utc)) - item.published_at
        bonus = 12.0 if item.exploited and item.exploited.ransomware else 0.0
        return max(1.0, 100.0 - hours.total_seconds() / 3600 + bonus)
