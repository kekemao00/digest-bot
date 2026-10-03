from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# 追踪参数不影响内容，去掉后才能识别同一篇文章
_TRACKING = re.compile(r"^(utm_\w+|ref|ref_src|source|fbclid|gclid|mc_cid|mc_eid)$", re.I)
_ARXIV = re.compile(r"^/(?:abs|pdf|html)/([0-9]{4}\.[0-9]{4,5})(?:v\d+)?(?:\.pdf)?/?$")


def canonical_url(url: str) -> str:
    """把同一内容的不同写法归一，用于跨来源合并和跨天去重。"""
    parts = urlsplit(url.strip())
    host = (parts.hostname or "").lower().removeprefix("www.").removeprefix("m.")
    path = parts.path or "/"
    if host in ("arxiv.org", "export.arxiv.org") and (m := _ARXIV.match(path)):
        return f"arxiv.org/abs/{m.group(1)}"
    if host == "huggingface.co" and (m := re.match(r"^/papers/([0-9]{4}\.[0-9]{4,5})", path)):
        return f"arxiv.org/abs/{m.group(1)}"
    if host == "github.com":
        # 仓库主页和 README 锚点等都算同一个仓库
        segments = [s for s in path.split("/") if s][:2]
        if len(segments) == 2:
            return f"github.com/{segments[0].lower()}/{segments[1].lower()}"
    query = urlencode(sorted((k, v) for k, v in parse_qsl(parts.query) if not _TRACKING.match(k)))
    path = path.rstrip("/") or "/"
    return urlunsplit(("", host, path, query, "")).lstrip("/")
