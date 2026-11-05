"""市场板块接进命令行之后的端到端测试：简报照旧推送，板块排在后面，出任何错都不影响退出码，
公开的地方（Summary、标准输出、日志、state.json、归档）只出现一行状态，没有任何市场内容和密钥。

全是编造的数据：市场接口用 test_markets_panel 里按主机分发的假接口，新闻用 HN 的样例，钉钉是假的。
"""

from __future__ import annotations

import json
import logging
import re
import sys
from dataclasses import replace
from pathlib import Path

import httpx
import pytest

import digest
import digest.cli as cli
import digest.markets as markets
from digest.markets import backtest, panel
from digest.markets.ledger import Ledger, keys_from_env
from digest.markets.settings import BudgetSettings
from digest.render import dingtalk
from digest.render.text import md_text
from test_markets_panel import (
    CBOE_HOST,
    CLEVELAND_HOST,
    CPI_EVENING,
    KALSHI_HOST,
    LINE_CPI,
    LINE_HI,
    LINE_VIX,
    MONDAY_EVENING,
    POLY_HOSTS,
    SUMMARY,
    ZH,
    FakeLLM,
    Venues,
    panel_settings,
)
from test_markets_select import (
    COND_HI,
    COND_LO,
    COND_MOVE,
    CPI,
    FED,
    INX,
    JOBS,
    NOW,
    NOW_TS,
    Q_HI,
    Q_LO,
    Q_MOVE,
    Q_SAY,
    REC,
    TOKEN_HI,
    TOKEN_LO,
    TOKEN_MOVE,
)

TOKEN = "tok-cli-markets-should-never-be-logged"
LEDGER_KEY = "fake-cli-ledger-key-not-a-real-secret-0123456789"
OLD_KEY = "fake-cli-old-ledger-key-not-a-real-secret-987654"
MARKET_HOSTS = {KALSHI_HOST, CBOE_HOST, CLEVELAND_HOST, *POLY_HOSTS}
# 公开的地方绝不能出现的：问题、译文、合约代码、condition id、token id、价格、市场接口的主机和密钥
PRIVATE = (
    Q_HI, Q_LO, Q_MOVE, Q_SAY, *ZH.values(), CPI, JOBS, FED, INX, REC, "KXCPI", "KXFED",
    COND_HI, COND_LO, COND_MOVE, TOKEN_HI, TOKEN_LO, TOKEN_MOVE, LINE_CPI, LINE_HI, LINE_VIX,
    "15.87", "17.20", "example-kalshi", "cboe.example", "nowcast.example", LEDGER_KEY, OLD_KEY, TOKEN,
)
STATUS = re.compile(r"^市场(板块|板块预览|回测)[（：]", re.M)


class Net:
    """记录发到钉钉的消息和市场接口收到的请求；clock 是板块预算用的假时钟。"""

    def __init__(self, hn_payload: dict) -> None:
        self.hn = hn_payload
        self.venues = Venues()
        self.sent: list[dict] = []
        self.hosts: list[str] = []
        self.dingtalk = {"errcode": 0, "errmsg": "ok"}
        self.llm = FakeLLM()
        self.clock = [0.0]
        self.slow: dict[str, float] = {}  # 主机 -> 每次请求让假时钟走多少秒

    def handler(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host
        self.hosts.append(host)
        self.clock[0] += self.slow.get(host, 0.0)
        if host == "hn.algolia.com":
            return httpx.Response(200, json=self.hn)
        if host == "oapi.dingtalk.com":
            self.sent.append(json.loads(request.content))
            return httpx.Response(200, json=self.dingtalk)
        if host in MARKET_HOSTS:
            return self.venues.handler(request)
        return httpx.Response(404)  # 其他新闻来源和真实的市场接口在这些测试里都不可用

    @property
    def market_calls(self) -> list[str]:
        return [host for host in self.hosts if host in MARKET_HOSTS]

    def titles(self) -> list[str]:
        return [m["markdown"]["title"] for m in self.sent]


@pytest.fixture
def net(monkeypatch, hn_payload, tmp_path):
    """板块启用、带账本钥匙的正式环境（北京时间 2030-03-04 周一 07:30）。"""
    for hit in hn_payload["hits"]:
        hit["created_at_i"] = NOW_TS - 3600
    fake = Net(hn_payload)
    monkeypatch.setattr(cli, "make_client", lambda: httpx.Client(transport=httpx.MockTransport(fake.handler)))
    monkeypatch.setattr(cli, "now_in", lambda timezone: NOW)
    monkeypatch.setattr(panel, "load_settings", lambda: panel_settings())
    monkeypatch.setattr(panel, "_sleep", lambda seconds: None)
    monkeypatch.setattr(panel, "_clock", lambda: fake.clock[0])
    monkeypatch.setattr(panel, "_llm", lambda client, config, env: fake.llm)
    monkeypatch.setenv("DINGTALK_WEBHOOK", f"https://oapi.dingtalk.com/robot/send?access_token={TOKEN}")
    monkeypatch.setenv("DINGTALK_SECRET", "SEC-fake")
    monkeypatch.setenv("MARKETS_ENABLED", "true")
    monkeypatch.setenv("MARKETS_LEDGER_KEY", LEDGER_KEY)
    monkeypatch.chdir(tmp_path)  # 默认的 .state/state.json 是相对路径
    return fake


@pytest.fixture
def summary_file(monkeypatch, tmp_path) -> Path:
    path = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(path))
    return path


def market_lines(text: str) -> list[str]:
    return [line for line in text.splitlines() if STATUS.match(line)]


def ledger_rows() -> list[dict]:
    return Ledger.open(Path(".state/markets"), keys_from_env({"MARKETS_LEDGER_KEY": LEDGER_KEY})).rows()


def saved_state() -> dict:
    return json.loads(Path(".state/state.json").read_text(encoding="utf-8"))


def assert_private(text: str) -> None:
    for secret in PRIVATE:
        assert secret not in text, secret


def public_files(root: Path) -> str:
    """工作流会公开的文件：state.json、归档、Summary（.state/markets/ 下只有密文，单独检查）。"""
    parts = []
    for path in sorted(root.rglob("*")):
        if path.is_file() and "markets" not in path.relative_to(root).parts:
            parts.append(path.read_text(encoding="utf-8", errors="replace"))
    return "\n".join(parts)


# ---- 正式推送 ----


def test_panel_is_sent_after_the_news(net, summary_file, tmp_path):
    assert cli.main(["--archive-dir", ".state/archive"]) == 0
    # 先推简报，再推板块
    assert len(net.sent) == 2
    news, board = (m["markdown"] for m in net.sent)
    assert news["title"].startswith("每日简报") and board["title"] == "市场与预测"
    assert LINE_CPI in board["text"] and LINE_HI in board["text"]
    assert net.hosts.index(KALSHI_HOST) > net.hosts.index("hn.algolia.com")
    # 板块拿到的是简报里实际显示的新闻，按显示顺序（“今天第 n 条”）
    (call,) = net.llm.calls
    numbers = re.findall(r"^\*\*(\d+)\. ", news["text"], re.M)
    assert call[1] and [int(n) for n in numbers] == list(range(1, len(call[1]) + 1))
    positions = [news["text"].index(f"**{n}. [{md_text(title)}]") for n, title in enumerate(call[1], 1)]
    assert positions == sorted(positions)
    # 去重状态和归档照常保存；板块的账本只写在 .state/markets/ 下
    assert saved_state()["last_sent"] == NOW.date().isoformat()
    assert list(Path(".state/archive").glob("*/*.md"))
    types = [row["type"] for row in ledger_rows()]
    assert "shown" in types and "rules" in types
    # Summary 里关于板块只有一行，不含任何内容
    summary = summary_file.read_text(encoding="utf-8")
    (status,) = market_lines(summary)
    assert status.startswith("市场板块：已推送 9 行（内容不公开）；Kalshi 正常")
    assert_private(summary)


def test_nothing_public_leaks(net, summary_file, tmp_path, caplog, capsys, monkeypatch):
    caplog.set_level(logging.DEBUG)
    monkeypatch.setenv("MARKETS_LEDGER_KEY_OLD", OLD_KEY)  # 轮换用的旧钥匙也不能出现
    assert cli.main(["--archive-dir", ".state/archive", "--out", "out"]) == 0
    assert cli.main(["--archive-dir", ".state/archive"]) == 0  # 第二次：读已有的账本
    assert len(net.sent) == 4
    out, err = capsys.readouterr()
    for text in (out, err, caplog.text, summary_file.read_text(encoding="utf-8"), public_files(tmp_path)):
        assert_private(text)
    # .state/markets/ 下只有密文和不含内容的元数据
    for path in Path(".state/markets").rglob("*"):
        if path.is_file():
            data = path.read_bytes()
            for secret in (CPI, COND_HI, TOKEN_HI, Q_HI, LEDGER_KEY, OLD_KEY):
                assert secret.encode() not in data, (path.name, secret)


def test_news_send_failure_means_no_panel(net, summary_file):
    net.dingtalk = {"errcode": 310000, "errmsg": "编造的失败"}
    assert cli.main([]) == 1  # 和以前一样
    assert len(net.sent) == 1 and net.market_calls == []
    assert not Path(".state/markets").exists()
    assert market_lines(summary_file.read_text(encoding="utf-8")) == []


def test_panel_error_keeps_exit_code_and_state(net, summary_file, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("编造的内部错误 " + CPI)

    monkeypatch.setattr(panel, "run", boom)
    assert cli.main([]) == 0
    assert len(net.sent) == 1 and saved_state()["last_sent"] == NOW.date().isoformat()
    (status,) = market_lines(summary_file.read_text(encoding="utf-8"))
    assert status == "市场板块：未推送：RuntimeError"


def test_entry_point_raising_is_caught_by_the_cli(net, summary_file, monkeypatch):
    def boom(*args, **kwargs):
        raise ValueError("编造的内部错误 " + CPI)

    monkeypatch.setattr(markets, "run_panel", boom)
    assert cli.main([]) == 0
    assert saved_state()["last_sent"] == NOW.date().isoformat()
    summary = summary_file.read_text(encoding="utf-8")
    assert market_lines(summary) == ["市场板块：未推送：ValueError"]
    assert_private(summary)


def test_import_failure_is_one_status_line(net, summary_file, monkeypatch):
    monkeypatch.delattr(digest, "markets")
    monkeypatch.setitem(sys.modules, "digest.markets", None)
    assert cli.main([]) == 0
    assert len(net.sent) == 1 and saved_state()["last_sent"]
    assert market_lines(summary_file.read_text(encoding="utf-8")) == ["市场板块：未启用（加载失败：ModuleNotFoundError）"]


def test_slow_provider_runs_out_of_budget(net, summary_file):
    # 每个 Kalshi 请求让假时钟走 100 秒：150 秒的预算两个请求就用完，板块不会拖住整个运行
    net.slow[KALSHI_HOST] = 100.0
    net.slow[CBOE_HOST] = 100.0
    assert cli.main([]) == 0
    assert saved_state()["last_sent"] == NOW.date().isoformat()
    assert net.titles()[0].startswith("每日简报")
    assert net.market_calls.count(KALSHI_HOST) <= 3
    (status,) = market_lines(summary_file.read_text(encoding="utf-8"))
    assert "BudgetExceeded" in status
    assert_private(status)


def test_rows_recorded_after_a_successful_send_only(net, monkeypatch):
    # 板块发不出去（钉钉第二次拒绝）：简报照常、退出码 0，展示过的内容不记账
    replies = iter([{"errcode": 0, "errmsg": "ok"}, {"errcode": 310000, "errmsg": "编造的失败"}])

    def handler(request):
        if request.url.host == "oapi.dingtalk.com":
            net.dingtalk = next(replies)
        return Net.handler(net, request)

    monkeypatch.setattr(cli, "make_client", lambda: httpx.Client(transport=httpx.MockTransport(handler)))
    assert cli.main([]) == 0
    assert saved_state()["last_sent"] == NOW.date().isoformat()
    assert not Path(".state/markets").exists() or all(row["type"] in ("witness", "settle") for row in ledger_rows())


def test_without_ledger_key_sends_but_records_nothing(net, summary_file, monkeypatch):
    monkeypatch.delenv("MARKETS_LEDGER_KEY")
    assert cli.main([]) == 0
    assert len(net.sent) == 2 and not Path(".state/markets").exists()
    (status,) = market_lines(summary_file.read_text(encoding="utf-8"))
    assert "账本未记录（没有账本钥匙）" in status


# ---- 简报不推的晚上和没有内容的日子 ----


def test_quiet_evening_still_sends_tonight_line(net, summary_file, monkeypatch, hn_payload):
    hn_payload["hits"] = hn_payload["hits"][:1]
    monkeypatch.setattr(cli, "now_in", lambda timezone: CPI_EVENING)
    monkeypatch.setenv("DIGEST_EDITION", "evening")
    assert cli.main([]) == 0
    (board,) = (m["markdown"] for m in net.sent)  # 简报今晚不推，只有板块
    assert board["title"] == "今晚美国数据" and "CPI 环比" in board["text"]
    assert saved_state()["last_evening"] == CPI_EVENING.date().isoformat()
    (status,) = market_lines(summary_file.read_text(encoding="utf-8"))
    assert status.startswith("市场板块：已推送")
    assert "今晚第" not in board["text"]  # 没推简报，不能指向“今晚第 n 条”


def test_quiet_evening_without_data_sends_nothing(net, summary_file, monkeypatch, hn_payload):
    hn_payload["hits"] = hn_payload["hits"][:1]
    monkeypatch.setattr(cli, "now_in", lambda timezone: MONDAY_EVENING)
    monkeypatch.setenv("DIGEST_EDITION", "evening")
    assert cli.main([]) == 0
    assert net.sent == []
    assert saved_state()["last_evening"] == MONDAY_EVENING.date().isoformat()
    (status,) = market_lines(summary_file.read_text(encoding="utf-8"))
    assert status.startswith("市场板块：未推送：没有可发的内容")


def test_no_news_items_still_sends_panel(net, hn_payload):
    hn_payload["hits"] = []
    assert cli.main([]) == 0
    assert net.titles() == ["市场与预测"]
    assert saved_state()["last_sent"] is None


def test_scheduled_runs_without_news_send_the_panel_once(net, summary_file, hn_payload, monkeypatch):
    # 没有新闻可推的早上不记 last_sent，后面几路定时触发还会再跑一遍：板块同一版同一天只推一次
    hn_payload["hits"] = []
    monkeypatch.setenv("DIGEST_SCHEDULED", "true")
    for _ in range(3):
        assert cli.main([]) == 0
    assert net.titles() == ["市场与预测"]
    lines = market_lines(summary_file.read_text(encoding="utf-8"))
    assert lines[0].startswith(cli.MARKETS_SENT) and lines[1:] == [cli.MARKETS_ALREADY] * 2
    # 记下的只有日期，放在 .state/ 下（不在 .state/markets/ 里）
    marker = Path(".state") / cli.MARKETS_SENT_FILE
    assert json.loads(marker.read_text(encoding="utf-8")) == {"morning": NOW.date().isoformat()}
    assert_private(marker.read_text(encoding="utf-8"))
    # 手动运行照常推送
    monkeypatch.setenv("DIGEST_SCHEDULED", "false")
    assert cli.main([]) == 0
    assert net.titles() == ["市场与预测", "市场与预测"]


def test_scheduled_skip_runs_no_panel(net, monkeypatch):
    monkeypatch.setenv("DIGEST_SCHEDULED", "true")
    assert cli.main([]) == 0
    calls = len(net.market_calls)
    assert cli.main([]) == 0  # 今天已推送过：简报和板块都跳过
    assert len(net.market_calls) == calls and len(net.sent) == 2


# ---- 试运行：只打印一行状态 ----


def test_dry_run_prints_only_the_status_line(net, summary_file, capsys, caplog):
    caplog.set_level(logging.DEBUG)
    assert cli.main(["--dry-run"]) == 0
    assert net.sent == [] and not Path(".state").exists()
    out = capsys.readouterr().out
    (status,) = market_lines(out)
    assert out.rstrip().endswith(status)
    assert status == "市场板块：9 行（公开仓库，内容已隐藏）；Kalshi 正常，Polymarket 正常，Cboe 正常，克利夫兰联储 正常"
    summary = summary_file.read_text(encoding="utf-8")
    assert market_lines(summary) == [status]
    assert "钉钉消息预览（未发送）" in summary  # 简报的预览照旧
    for text in (out, summary, caplog.text):
        assert_private(text)
    assert net.llm.calls == []  # 试运行不调用大模型


def test_dry_run_with_the_real_rules_file(net, summary_file, monkeypatch, capsys):
    # 用仓库里真正的 config/markets.yaml（真实的 Kalshi、Cboe 地址在这里返回 404，Polymarket 仍是假接口）：
    # 读得进来，只有一行状态
    from digest.markets.settings import load_settings

    monkeypatch.setattr(panel, "load_settings", load_settings)
    assert cli.main(["--dry-run"]) == 0
    (status,) = market_lines(capsys.readouterr().out)
    assert re.match(r"市场板块：\d+ 行（公开仓库，内容已隐藏）；Kalshi HTTP 404，Polymarket 正常，Cboe HTTP 404", status)
    assert "规则文件" not in status  # 规则文件没有写错的地方
    assert market_lines(summary_file.read_text(encoding="utf-8")) == [status]


# ---- 预览和回测：不碰简报 ----


def test_preview_sends_only_the_panel_and_writes_nothing(net, summary_file, monkeypatch, tmp_path):
    monkeypatch.setenv("DIGEST_MARKETS_PREVIEW", "true")  # 工作流用环境变量传
    assert cli.main(["--archive-dir", ".state/archive"]) == 0
    assert net.titles() == ["【预览】市场与预测"]
    assert "hn.algolia.com" not in net.hosts  # 不抓新闻
    assert "【后台观察】" in net.sent[0]["markdown"]["text"]
    assert not Path(".state").exists()  # 不记账、不改去重状态、不写归档
    summary = summary_file.read_text(encoding="utf-8")
    assert market_lines(summary) == [
        "市场板块预览：已推送 10 行（内容不公开）；Kalshi 正常，Polymarket 正常，Cboe 正常，克利夫兰联储 正常"
    ]
    assert_private(summary)


def test_preview_obeys_dry_run(net, summary_file, capsys):
    assert cli.main(["--markets-preview", "--dry-run"]) == 0
    assert net.sent == [] and "hn.algolia.com" not in net.hosts and not Path(".state").exists()
    out = capsys.readouterr().out
    assert market_lines(out) == [out.strip()]
    assert out.startswith("市场板块：9 行（公开仓库，内容已隐藏）")


def test_backtest_sends_and_stores_encrypted(net, summary_file, monkeypatch):
    calls = []
    monkeypatch.setattr(backtest, "run", lambda fetcher, s, now, dry: calls.append(dry) or dict(SUMMARY, dry=dry))
    assert cli.main(["--markets-backtest"]) == 0
    assert calls == [False]
    assert net.titles() == ["【回测】Kalshi 过去的成绩"] and "hn.algolia.com" not in net.hosts
    assert Path(".state/markets/backtest.json.enc").exists()
    assert [row["type"] for row in ledger_rows()] == ["backtest"]
    assert not Path(".state/state.json").exists()  # 去重状态不动
    (status,) = market_lines(summary_file.read_text(encoding="utf-8"))
    assert status.startswith("市场回测：已推送（内容不公开）；5 类合约 10 次事件") and "结果已加密保存" in status


def test_backtest_dry_run_only_counts(net, summary_file, monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(backtest, "run", lambda fetcher, s, now, dry: calls.append(dry) or dict(SUMMARY, dry=dry))
    monkeypatch.setenv("DIGEST_DRY_RUN", "true")
    monkeypatch.setenv("DIGEST_MARKETS_BACKTEST", "true")
    assert cli.main([]) == 0
    assert calls == [True] and net.sent == [] and not Path(".state").exists()
    out = capsys.readouterr().out
    assert out.strip() == "市场回测（试运行）：5 类合约 10 次事件，算出 9 次；请求 0 次；没有联网"
    assert market_lines(summary_file.read_text(encoding="utf-8")) == [out.strip()]


def test_dry_replay_has_a_short_time_cap(net, monkeypatch):
    budgets = []
    monkeypatch.setattr(backtest, "run", lambda fetcher, s, now, dry: budgets.append(fetcher.budget.seconds) or {})
    assert cli.main(["--dry-run", "--markets-backtest"]) == 0
    assert cli.main(["--markets-backtest"]) == 0
    assert budgets == [panel.DRY_REPLAY_SECONDS, panel_settings().replay_budget.seconds]


def test_preview_and_backtest_together_is_an_error(net):
    with pytest.raises(SystemExit):
        cli.main(["--markets-preview", "--markets-backtest"])


def test_preview_with_broken_webhook_fails_early(net, monkeypatch):
    monkeypatch.delenv("DINGTALK_WEBHOOK")
    assert cli.main(["--markets-preview"]) == 1
    assert net.hosts == []


# ---- 没有启用：和以前完全一样 ----


def test_disabled_changes_nothing(net, summary_file, monkeypatch, capsys):
    monkeypatch.delenv("MARKETS_ENABLED")
    called = []
    monkeypatch.setattr(markets, "run_panel", lambda *a, **k: called.append(a) or "不该出现")
    assert cli.main([]) == 0
    assert cli.main(["--dry-run"]) == 0
    assert called == [] and net.market_calls == []
    assert net.titles() == [net.titles()[0]] and net.titles()[0].startswith("每日简报")
    assert "市场" not in summary_file.read_text(encoding="utf-8")
    assert "市场" not in capsys.readouterr().out
    assert not Path(".state/markets").exists()


def test_disabled_preview_and_backtest_do_nothing(net, summary_file, monkeypatch):
    monkeypatch.delenv("MARKETS_ENABLED")
    assert cli.main(["--markets-preview"]) == 0
    assert cli.main(["--markets-backtest"]) == 0
    assert net.hosts == [] and net.sent == [] and not Path(".state").exists()
    assert market_lines(summary_file.read_text(encoding="utf-8")) == [
        "市场板块预览：未运行（仓库变量 MARKETS_ENABLED 没有打开）",
        "市场回测：未运行（仓库变量 MARKETS_ENABLED 没有打开）",
    ]


# ---- 钉钉消息记下实际显示的条目 ----


def test_message_items_are_the_rendered_items(config, hn_source, hn_items, monkeypatch):
    from digest.pipeline import select as select_items

    selection = select_items(hn_items, [hn_source], config)
    full = dingtalk.render(selection, config, NOW)
    assert full.items == selection.items and full.items
    # 超长时末尾的条目被删掉，items 也跟着少，编号和“今天第 n 条”仍然对得上
    monkeypatch.setattr(dingtalk, "MAX_BYTES", len(full.text.encode()) - 1)
    trimmed = dingtalk.render(selection, config, NOW)
    assert trimmed.items == selection.items[: len(trimmed.items)] and len(trimmed.items) < len(selection.items)
    numbers = re.findall(r"^\*\*(\d+)\. ", trimmed.text, re.M)
    assert [int(n) for n in numbers] == list(range(1, len(trimmed.items) + 1))


def test_budget_split_never_exceeds_the_rules(net, summary_file, monkeypatch):
    # 正式推送时核对和板块共用规则文件里的预算：请求总数不超过上限
    monkeypatch.setattr(panel, "load_settings", lambda: replace(panel_settings(), budget=replace(BudgetSettings(), requests=5)))
    assert cli.main([]) == 0
    assert len(net.market_calls) <= 5
    assert saved_state()["last_sent"] == NOW.date().isoformat()
