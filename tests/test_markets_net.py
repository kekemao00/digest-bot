from __future__ import annotations

import logging
import traceback

import httpx
import pytest

from digest.markets.net import Budget, BudgetExceeded, FetchError, Fetcher

HOST = "api.example-market.test"
URL = f"https://{HOST}/v9/things"
SECRET = "fake-query-value-should-never-show"


class FakeClock:
    """假的单调时钟：sleep 不真睡，只把时间往前拨。"""

    def __init__(self) -> None:
        self.now = 1000.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def make(handler, *, seconds=60.0, requests=10, **options):
    """用假的 HTTP 响应和假时钟造一个 Fetcher。jitter 固定为 0.5，默认重试间隔就是 3.5 秒。"""
    clock = FakeClock()
    calls: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return handler(request)

    client = httpx.Client(transport=httpx.MockTransport(record), headers={"User-Agent": "test-agent"})
    fetcher = Fetcher(client, Budget(seconds, requests, clock=clock), sleep=clock.sleep, jitter=lambda: 0.5, **options)
    return fetcher, calls, clock


def sequence(*responses):
    """依次返回给定的响应；元素是异常类时抛出它（带上请求）。"""
    queue = list(responses)

    def handler(request: httpx.Request) -> httpx.Response:
        item = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(item, type) and issubclass(item, Exception):
            raise item(f"failed for {request.url}", request=request)
        return item

    return handler


def ok(data=None) -> httpx.Response:
    return httpx.Response(200, json=data if data is not None else {"value": 1})


def test_budget_check_requests_and_time():
    clock = FakeClock()
    budget = Budget(10.0, 2, clock=clock)
    budget.check()
    budget.used = 2
    with pytest.raises(BudgetExceeded):
        budget.check()
    budget.used = 0
    clock.now += 9
    budget.check()
    assert budget.remaining() == pytest.approx(1.0)
    with pytest.raises(BudgetExceeded):
        budget.check(reserve=1.0)
    clock.now += 5
    assert budget.remaining() == 0.0
    with pytest.raises(BudgetExceeded):
        budget.check()


def test_get_json_counts_requests_and_records_host():
    fetcher, calls, _ = make(lambda request: ok({"items": [1, 2]}))
    assert fetcher.get_json(URL, params={"limit": 5}) == {"items": [1, 2]}
    assert fetcher.requests == 1 and fetcher.budget.used == 1
    assert fetcher.hosts == {HOST: "ok"}
    assert calls[0].url.params["limit"] == "5"
    # 用共享客户端的 User-Agent，不加别的请求头（更不带邮箱之类的个人信息）
    assert calls[0].headers["user-agent"] == "test-agent"
    assert "from" not in calls[0].headers


def test_request_budget_stops_before_sending():
    fetcher, calls, _ = make(lambda request: ok(), requests=2)
    fetcher.get_json(URL)
    fetcher.get_json(URL)
    with pytest.raises(BudgetExceeded):
        fetcher.get_json(URL)
    assert len(calls) == 2 and fetcher.requests == 2


def test_time_budget_and_per_request_timeout():
    clock_box = {}

    def slow(request: httpx.Request) -> httpx.Response:
        clock_box["clock"].now += 25  # 很慢的接口：每次 25 秒
        return ok()

    fetcher, calls, clock = make(slow, seconds=50.0, timeout=30.0)
    clock_box["clock"] = clock
    fetcher.get_json(URL)
    fetcher.get_json(URL)
    with pytest.raises(BudgetExceeded):
        fetcher.get_json(URL)
    assert len(calls) == 2
    # 单次超时取 timeout 和剩余预算里小的那个：第二次只剩 25 秒
    assert calls[0].extensions["timeout"]["read"] == 30.0
    assert calls[1].extensions["timeout"]["read"] == pytest.approx(25.0)


def test_connect_timeout_is_capped():
    # 连接（含 TLS 握手）最多等 5 秒：连不上的地址不必等满整个请求的时限；剩余预算更少时取更少的
    fetcher, calls, clock = make(lambda request: ok(), seconds=13.0, timeout=10.0)
    fetcher.get_json(URL)
    assert calls[0].extensions["timeout"] == {"connect": 5.0, "read": 10.0, "write": 10.0, "pool": 10.0}
    clock.now += 10
    fetcher.get_json(URL)
    assert calls[1].extensions["timeout"]["connect"] == pytest.approx(3.0)
    assert calls[1].extensions["timeout"]["read"] == pytest.approx(3.0)


@pytest.mark.parametrize(
    "failure",
    [
        httpx.Response(429),
        httpx.Response(500),
        httpx.Response(503),
        httpx.Response(403, text="<html>blocked</html>", headers={"content-type": "text/html; charset=utf-8"}),
    ],
)
def test_retries_once_on_transient_responses(failure):
    fetcher, calls, clock = make(sequence(failure, ok()))
    assert fetcher.get_json(URL) == {"value": 1}
    assert len(calls) == 2 and fetcher.requests == 2
    assert clock.sleeps == [3.5]  # 2 + 3 * jitter
    assert fetcher.hosts[HOST] == "ok"


@pytest.mark.parametrize("error", [httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout])
def test_retries_once_on_connection_problems(error):
    fetcher, calls, clock = make(sequence(error, ok()))
    assert fetcher.get_json(URL) == {"value": 1}
    assert len(calls) == 2 and clock.sleeps == [3.5]


@pytest.mark.parametrize(
    "failure, status",
    [
        (httpx.Response(404), 404),
        (httpx.Response(400, json={"error": "bad"}), 400),
        (httpx.Response(403, json={"error": "forbidden"}), 403),  # JSON 的 403 是真的拒绝，不重试
    ],
)
def test_no_retry_on_client_errors(failure, status):
    fetcher, calls, clock = make(sequence(failure))
    with pytest.raises(FetchError) as info:
        fetcher.get_json(URL)
    assert info.value.status == status and info.value.kind == "4xx"
    assert len(calls) == 1 and clock.sleeps == []
    assert fetcher.hosts[HOST] == f"HTTP {status}"


def test_retries_only_once():
    fetcher, calls, clock = make(sequence(httpx.Response(503)))
    with pytest.raises(FetchError) as info:
        fetcher.get_json(URL)
    assert (info.value.status, info.value.kind) == (503, "5xx")
    assert len(calls) == 2 and clock.sleeps == [3.5]
    assert fetcher.hosts[HOST] == "HTTP 503"


def test_retry_after_header():
    fetcher, calls, clock = make(sequence(httpx.Response(429, headers={"Retry-After": "1"}), ok()))
    fetcher.get_json(URL)
    assert clock.sleeps == [1.0] and len(calls) == 2

    # 要求等太久就不重试了
    fetcher, calls, clock = make(sequence(httpx.Response(429, headers={"Retry-After": "30"}), ok()))
    with pytest.raises(FetchError):
        fetcher.get_json(URL)
    assert clock.sleeps == [] and len(calls) == 1

    # 日期格式的 Retry-After 按默认间隔
    when = "Wed, 21 Oct 2037 07:28:00 GMT"
    fetcher, calls, clock = make(sequence(httpx.Response(503, headers={"Retry-After": when}), ok()))
    fetcher.get_json(URL)
    assert clock.sleeps == [3.5]


def test_no_retry_when_request_budget_is_used_up():
    fetcher, calls, clock = make(sequence(httpx.Response(429), ok()), requests=1)
    with pytest.raises(FetchError) as info:
        fetcher.get_json(URL)
    assert info.value.status == 429
    assert len(calls) == 1 and clock.sleeps == []


def test_no_retry_when_time_budget_cannot_cover_the_wait():
    fetcher, calls, clock = make(sequence(httpx.Response(503), ok()), seconds=3.0)
    with pytest.raises(FetchError):
        fetcher.get_json(URL)
    assert len(calls) == 1 and clock.sleeps == []


def test_max_bytes_from_content_length():
    fetcher, calls, _ = make(lambda request: httpx.Response(200, content=b"x" * 2000), max_bytes=1000)
    with pytest.raises(FetchError) as info:
        fetcher.get_text(URL)
    assert info.value.kind == "too_large" and info.value.status is None
    assert len(calls) == 1  # 太大不重试
    assert fetcher.hosts[HOST] == "too_large"


def test_max_bytes_aborts_streamed_body_early():
    produced = []

    def chunks():
        for n in range(50):
            produced.append(n)
            yield b"y" * 400

    # 没有 Content-Length 的分块响应：读到超过上限就停，不等下载完
    fetcher, _, _ = make(lambda request: httpx.Response(200, content=chunks()), max_bytes=1000)
    with pytest.raises(FetchError) as info:
        fetcher.get_text(URL)
    assert info.value.kind == "too_large"
    assert len(produced) <= 3


def test_slow_drip_body_is_cut_at_request_timeout():
    box = {}

    def drip():
        for _ in range(20):
            box["clock"].now += 4  # 每块之间不超读超时，但加起来很久
            yield b"z" * 10

    fetcher, calls, clock = make(lambda request: httpx.Response(200, content=drip()), timeout=10.0, seconds=100.0)
    box["clock"] = clock
    with pytest.raises(FetchError) as info:
        fetcher.get_text(URL)
    assert info.value.kind == "timeout"
    assert len(calls) == 2  # 超时算可以重试的情况，重试一次后放弃


def test_fetch_error_has_host_but_never_the_url(caplog):
    caplog.set_level(logging.DEBUG)
    url = f"{URL}?api_key={SECRET}"
    fetcher, _, _ = make(sequence(httpx.Response(404)))
    with pytest.raises(FetchError) as info:
        fetcher.get_json(url, params={"series_ticker": "ZZTEST"})
    error = info.value
    assert str(error) == f"{HOST} 404"
    assert error.host == HOST
    assert SECRET not in repr(error) and "ZZTEST" not in str(error) and "/v9/" not in str(error)

    # httpx 的异常文本里有完整地址：异常链和日志里都不能出现
    fetcher, _, _ = make(sequence(httpx.ConnectError))
    with pytest.raises(FetchError) as info:
        fetcher.get_json(url, params={"series_ticker": "ZZTEST"})
    error = info.value
    assert str(error) == f"{HOST} ConnectError" and error.kind == "connect"
    assert error.__cause__ is None and error.__context__ is None
    text = "".join(traceback.format_exception_only(type(error), error))
    assert SECRET not in text and "ZZTEST" not in text
    # 只看本项目的日志；httpx 自己的请求日志在 cli.setup_logging 里调到了 WARNING
    ours = "\n".join(r.getMessage() for r in caplog.records if r.name.startswith("digest"))
    assert HOST in ours  # 重试时记了主机名
    assert SECRET not in ours and "ZZTEST" not in ours and "/v9/" not in ours


def test_get_json_bad_json():
    page = httpx.Response(200, text="<html>not json</html>", headers={"content-type": "text/html"})
    fetcher, calls, _ = make(lambda request: page)
    with pytest.raises(FetchError) as info:
        fetcher.get_json(URL)
    assert info.value.kind == "bad_json" and info.value.status is None
    assert str(info.value) == f"{HOST} bad_json"
    assert len(calls) == 1
    assert fetcher.hosts[HOST] == "bad_json"


def test_get_text_uses_declared_charset():
    body = "日期,数值\n".encode("gbk")
    fetcher, _, _ = make(lambda request: httpx.Response(200, content=body, headers={"content-type": "text/csv; charset=gbk"}))
    assert fetcher.get_text(URL) == "日期,数值\n"


def test_other_transport_errors_are_not_retried():
    fetcher, calls, clock = make(sequence(httpx.RemoteProtocolError))
    with pytest.raises(FetchError) as info:
        fetcher.get_text(URL)
    assert info.value.kind == "network" and str(info.value) == f"{HOST} RemoteProtocolError"
    assert len(calls) == 1 and clock.sleeps == []


def test_min_interval_spaces_requests():
    box = {}

    def handler(request: httpx.Request) -> httpx.Response:
        box["clock"].now += 0.25
        return ok()

    fetcher, calls, clock = make(handler, min_interval=1.0)
    box["clock"] = clock
    fetcher.get_json(URL)
    fetcher.get_json(URL)
    fetcher.get_json(URL)
    assert len(calls) == 3
    assert clock.sleeps == [pytest.approx(1.0), pytest.approx(1.0)]  # 从上一次请求结束算起


def test_hosts_tracks_each_host_separately():
    def handler(request: httpx.Request) -> httpx.Response:
        return ok() if request.url.host == HOST else httpx.Response(404)

    fetcher, _, _ = make(handler)
    fetcher.get_json(URL)
    with pytest.raises(FetchError):
        fetcher.get_json("https://other.example-market.test/x")
    assert fetcher.hosts == {HOST: "ok", "other.example-market.test": "HTTP 404"}
