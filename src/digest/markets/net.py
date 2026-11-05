"""市场板块的网络请求。所有请求都经过 Fetcher，受同一份预算（总时长和请求次数）约束。

安全约定：异常和日志里只有主机名、HTTP 状态码或异常类型，没有完整地址（路径和查询参数里有合约代码）。
不加任何请求头，用共享客户端默认的 User-Agent，不带任何个人信息。
"""

from __future__ import annotations

import json
import logging
import math
import random
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

import httpx

log = logging.getLogger("digest.markets")

# 服务端要求等太久就不重试了，免得一个慢接口吃掉整个预算
MAX_RETRY_AFTER = 5.0
# 建立连接（含 TLS 握手）最多等这么久：正常的接口一秒内就连上，连不上的地址不必等满整个请求的时限
CONNECT_TIMEOUT = 5.0


class BudgetExceeded(Exception):
    """预算（总时长或请求次数）用完了，后面的请求都不再发。"""


class FetchError(Exception):
    """一次请求最终失败。str() 是“主机名 状态码”或“主机名 异常类型”，不含路径和查询参数。

    kind：4xx / 5xx / status（其他非 2xx）/ connect（连不上）/ timeout / network（其他传输错误）/
    too_large（响应超过大小上限）/ bad_json（不是 JSON）/ bad_url；
    down（Kalshi 客户端的地址都连不上，这次没有发请求，见 kalshi.Kalshi）。
    """

    def __init__(self, host: str, status: int | None, kind: str, detail: str | None = None) -> None:
        self.host = host
        self.status = status
        self.kind = kind
        # 写进 Fetcher.hosts 和 Summary 的结果，例如 "HTTP 429"、"ConnectError"、"too_large"
        self.outcome = f"HTTP {status}" if status is not None else (detail or kind)
        super().__init__(f"{host} {status if status is not None else (detail or kind)}")


@dataclass
class Budget:
    """一次运行的网络预算：总秒数和总请求次数（重试也算一次）。"""

    seconds: float
    requests: int
    clock: Callable[[], float] = time.monotonic
    start: float = field(init=False)
    used: int = field(init=False)

    def __post_init__(self) -> None:
        self.start = self.clock()
        self.used = 0

    def check(self, reserve: float = 0.0) -> None:
        """请求次数用完，或已用时间加上 reserve 秒到了上限时抛 BudgetExceeded。"""
        if self.used >= self.requests:
            raise BudgetExceeded("请求次数用完了")
        if self.clock() - self.start + reserve >= self.seconds:
            raise BudgetExceeded("时间用完了")

    def remaining(self) -> float:
        return max(0.0, self.seconds - (self.clock() - self.start))


class _Failed(Exception):
    """一次尝试失败；带上能不能重试和服务端要求的等待时间。只在 Fetcher 内部使用。"""

    def __init__(self, error: FetchError, retryable: bool, retry_after: float | None = None) -> None:
        super().__init__(str(error))
        self.error = error
        self.retryable = retryable
        self.retry_after = retry_after


class Fetcher:
    """带预算的 GET：每次尝试前检查预算，响应边读边数字节，失败时按规则重试一次。"""

    def __init__(
        self,
        client: httpx.Client,
        budget: Budget,
        timeout: float = 10.0,
        max_bytes: int = 10_000_000,
        sleep: Callable[[float], None] = time.sleep,
        jitter: Callable[[], float] = random.random,
        min_interval: float = 0.0,
    ) -> None:
        self.client = client
        self.budget = budget
        self.timeout = timeout
        self.max_bytes = max_bytes
        self.sleep = sleep
        self.jitter = jitter
        self.min_interval = min_interval
        self.requests = 0  # 实际发出的请求次数
        self.hosts: dict[str, str] = {}  # 主机名 -> 最近一次的结果（"ok"、"HTTP 429"、"ConnectError" 等）
        self._last: float | None = None  # 上一次请求结束的时刻，用来拉开间隔

    def get_json(self, url: str, params: Mapping[str, Any] | None = None) -> Any:
        body, _ = self._get(url, params)
        try:
            return json.loads(body)
        except ValueError:  # 包括 JSONDecodeError 和 UnicodeDecodeError
            error = FetchError(_host(url), None, "bad_json")
        self.hosts[error.host] = error.outcome
        raise error

    def get_text(self, url: str, params: Mapping[str, Any] | None = None) -> str:
        body, encoding = self._get(url, params)
        return body.decode(encoding, errors="replace")

    def _get(self, url: str, params: Mapping[str, Any] | None) -> tuple[bytes, str]:
        host = _host(url)
        for attempt in range(2):
            self._wait_turn()
            try:
                result = self._once(url, params, host)
            except _Failed as failed:
                error = failed.error
                delay = self._retry_delay(failed) if attempt == 0 else None
            else:
                self.hosts[host] = "ok"
                return result
            # 在 except 块外面抛出，异常链里不会带上 httpx 的原始异常（它的文本里有完整地址）
            self.hosts[host] = error.outcome
            if delay is None:
                raise error
            log.info("市场数据：%s 返回 %s，%.1f 秒后重试一次", host, error.outcome, delay)
            self.sleep(delay)
        raise AssertionError("不会到这里")

    def _wait_turn(self) -> None:
        """检查预算；需要拉开间隔时先等一会儿（等待时间也计入预算）。"""
        wait = 0.0
        if self.min_interval > 0 and self._last is not None:
            wait = max(0.0, self.min_interval - (self.budget.clock() - self._last))
        self.budget.check(reserve=wait)
        if wait > 0:
            self.sleep(wait)

    def _retry_delay(self, failed: _Failed) -> float | None:
        """能重试时返回等待秒数；不该重试或预算不够再试一次时返回 None。"""
        if not failed.retryable:
            return None
        if failed.retry_after is not None:
            if failed.retry_after > MAX_RETRY_AFTER:
                return None
            delay = failed.retry_after
        else:
            delay = 2 + 3 * self.jitter()
        try:
            self.budget.check(reserve=delay)
        except BudgetExceeded:
            return None
        return delay

    def _once(self, url: str, params: Mapping[str, Any] | None, host: str) -> tuple[bytes, str]:
        timeout = min(self.timeout, self.budget.remaining())
        deadline = self.budget.clock() + timeout
        limits = httpx.Timeout(timeout, connect=min(timeout, CONNECT_TIMEOUT))
        self.requests += 1
        self.budget.used += 1
        try:
            with self.client.stream("GET", url, params=params, timeout=limits) as response:
                if not 200 <= response.status_code < 300:
                    raise _status_failure(host, response)
                return self._read(response, host, deadline), response.encoding or "utf-8"
        except _Failed:
            raise
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            raise _Failed(FetchError(host, None, "connect", type(exc).__name__), retryable=True) from None
        except httpx.TimeoutException as exc:
            retryable = isinstance(exc, httpx.ReadTimeout)
            raise _Failed(FetchError(host, None, "timeout", type(exc).__name__), retryable=retryable) from None
        except httpx.InvalidURL:
            raise _Failed(FetchError(host, None, "bad_url"), retryable=False) from None
        except httpx.HTTPError as exc:
            # 异常文本里有完整地址，只留类型
            raise _Failed(FetchError(host, None, "network", type(exc).__name__), retryable=False) from None
        finally:
            self._last = self.budget.clock()

    def _read(self, response: httpx.Response, host: str, deadline: float) -> bytes:
        """边读边数：超过大小上限或单次请求的时限就放弃，不等全部下载完。"""
        length = response.headers.get("content-length", "")
        if length.isdigit() and int(length) > self.max_bytes:
            raise _Failed(FetchError(host, None, "too_large"), retryable=False)
        chunks: list[bytes] = []
        size = 0
        for chunk in response.iter_bytes():
            size += len(chunk)
            if size > self.max_bytes:
                raise _Failed(FetchError(host, None, "too_large"), retryable=False)
            # 读超时只管两块数据之间的间隔，一点一点往外吐的慢接口要另外按总时长截断
            if self.budget.clock() > deadline:
                raise _Failed(FetchError(host, None, "timeout", "ReadTimeout"), retryable=True)
            chunks.append(chunk)
        return b"".join(chunks)


def _status_failure(host: str, response: httpx.Response) -> _Failed:
    status = response.status_code
    kind = "5xx" if status >= 500 else "4xx" if status >= 400 else "status"
    # 403 而且是 HTML 页面，多半是防火墙临时拦截，值得再试一次；JSON 的 403 是真的拒绝
    html = "html" in response.headers.get("content-type", "").lower()
    retryable = status == 429 or status >= 500 or (status == 403 and html)
    return _Failed(FetchError(host, status, kind), retryable, _retry_after(response.headers.get("retry-after")))


def _retry_after(value: str | None) -> float | None:
    """Retry-After 的秒数；写成日期或解析不了时返回 None（按默认间隔重试）。"""
    if not value:
        return None
    try:
        seconds = float(value.strip())
    except ValueError:
        return None
    return seconds if math.isfinite(seconds) and seconds >= 0 else None


def _host(url: str) -> str:
    try:
        return urlsplit(url).hostname or "?"
    except ValueError:
        return "?"
