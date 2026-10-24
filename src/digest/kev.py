"""CISA「已知被利用漏洞目录」（Known Exploited Vulnerabilities，KEV）。

漏洞有没有被实际利用，以这份目录为准，不靠大模型从文章里猜：条目里提到的 CVE 编号在目录里，
就标注“已被利用”。目录是美国 CISA 维护的公开 JSON，收录有可靠证据表明已被利用的漏洞。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from typing import Any

import httpx

from digest.models import Exploited, Item

URL = "https://www.cisa.gov/sites/default/files/feeds/known_exploited_vulnerabilities.json"
_CVE = re.compile(r"\bCVE-(\d{4})-(\d{4,7})\b", re.I)


def cve_ids(*texts: str | None) -> list[str]:
    """文本里出现的 CVE 编号，统一成大写，按出现顺序去重。"""
    found = (f"CVE-{m.group(1)}-{m.group(2)}" for text in texts if text for m in _CVE.finditer(text))
    return list(dict.fromkeys(found))


@dataclass
class Catalog:
    entries: dict[str, dict[str, Any]]  # CVE 编号 -> 目录里的原始记录

    @classmethod
    def parse(cls, data: Any) -> Catalog:
        vulns = data.get("vulnerabilities") if isinstance(data, dict) else None
        if not isinstance(vulns, list) or not vulns:
            raise ValueError("已知被利用漏洞目录为空或格式变了")
        entries = {}
        for vuln in vulns:
            if isinstance(vuln, dict) and (ids := cve_ids(str(vuln.get("cveID") or ""))):
                entries[ids[0]] = vuln
        return cls(entries)

    @classmethod
    def fetch(cls, client: httpx.Client, url: str = URL) -> Catalog:
        resp = client.get(url)
        resp.raise_for_status()
        return cls.parse(resp.json())

    def lookup(self, cve: str) -> Exploited | None:
        vuln = self.entries.get(cve.upper())
        if vuln is None or (added := added_on(vuln)) is None:
            return None
        return Exploited(cve=cve.upper(), added=added, ransomware=is_ransomware(vuln))


def added_on(vuln: dict[str, Any]) -> date | None:
    try:
        return date.fromisoformat(str(vuln.get("dateAdded") or ""))
    except ValueError:
        return None


def is_ransomware(vuln: dict[str, Any]) -> bool:
    return str(vuln.get("knownRansomwareCampaignUse") or "").strip().lower() == "known"


def annotate(items: list[Item], catalog: Catalog | None) -> int:
    """记下每条提到的 CVE 编号；有目录时，给提到已被利用漏洞的条目标注最早列入目录的那个。返回标注了几条。"""
    marked = 0
    for item in items:
        # 来源自己给出的编号（如目录本身的条目）在前
        item.cves = list(dict.fromkeys([*item.cves, *cve_ids(item.title, item.summary)]))
        if catalog is None or item.exploited:
            continue
        hits = [hit for cve in item.cves if (hit := catalog.lookup(cve))]
        if hits:
            item.exploited = min(hits, key=lambda h: h.added)
            marked += 1
    return marked
