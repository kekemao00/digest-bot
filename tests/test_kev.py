from __future__ import annotations

import json
from datetime import datetime, timezone

from conftest import FIXTURES
from digest.kev import Catalog, annotate, cve_ids
from digest.models import Item


def item(title: str, summary: str | None = None) -> Item:
    return Item(source="hn", source_name="HN", section="tech", id=title, title=title, url="https://x.test",
                published_at=datetime.now(timezone.utc), summary=summary)


def catalog() -> Catalog:
    return Catalog.parse(json.loads((FIXTURES / "kev.json").read_text(encoding="utf-8")))


def test_cve_ids():
    text = "Fix for cve-2026-22222 and CVE-2026-11111; see CVE-2026-22222 again. CVE-26-1 is not one"
    assert cve_ids(text, None) == ["CVE-2026-22222", "CVE-2026-11111"]


def test_annotate_marks_exploited_vulnerabilities():
    exploited = item("Exploiting CVE-2026-22222 and CVE-2025-33333 in the wild")
    unknown = item("A new bug", "Tracked as CVE-2026-99999.")
    plain = item("Rust 2.0 roadmap")
    assert annotate([exploited, unknown, plain], catalog()) == 1
    # 提到几个已被利用的漏洞时，取最早列入目录的
    assert exploited.exploited.cve == "CVE-2025-33333"
    assert exploited.cves == ["CVE-2026-22222", "CVE-2025-33333"]
    # 不在目录里的也记下编号，以后可以核对它后来有没有被列入
    assert unknown.cves == ["CVE-2026-99999"] and unknown.exploited is None
    assert plain.cves == [] and plain.exploited is None


def test_annotate_without_catalog_only_records_ids():
    exploited = item("CVE-2026-11111 under attack")
    assert annotate([exploited], None) == 0
    assert exploited.cves == ["CVE-2026-11111"] and exploited.exploited is None
