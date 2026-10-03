from __future__ import annotations

import httpx

USER_AGENT = "digest-bot/0.1 (+https://github.com/kekemao00/digest-bot)"


def make_client() -> httpx.Client:
    return httpx.Client(
        timeout=httpx.Timeout(20.0, connect=10.0),
        headers={"User-Agent": USER_AGENT},
        follow_redirects=True,
        transport=httpx.HTTPTransport(retries=2),
    )
