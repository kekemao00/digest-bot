from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from digest.markets import ledger as L
from digest.markets.ledger import (
    Keys,
    Ledger,
    LedgerError,
    assert_no_plaintext,
    key_status,
    keys_from_env,
    read_blob,
    write_blob,
)

# 明显是假的口令，只用于测试；任何异常文本和日志里都不能出现它们
KEY = "fake-ledger-passphrase-for-tests-0001"
OLD = "fake-older-passphrase-for-tests-0002"
WRONG = "fake-wrong-passphrase-for-tests-0003"
SECRETS = (KEY, OLD, WRONG)
NOW = datetime(2031, 5, 14, 23, 30, tzinfo=timezone.utc)
MAY = "2031-05.jsonl.enc"
APRIL = "2031-04.jsonl.enc"


def make_rows(n: int, start: int = 0, ts: str = "2031-05-14T23:30:00Z") -> list[dict]:
    """编造的账本记录：假合约代码、假问题、假价格。"""
    return [
        {"type": "shown", "ts": ts, "edition": "morning", "mode": "send", "venue": "kalshi",
         "key": f"ZZFAKE-31MAY-T{start + i}", "question": f"测试问题第{start + i}个", "p": 40 + i,
         "quote": {"bid": 0.39, "ask": 0.41, "last": None, "mid": 0.4}}
        for i in range(n)
    ]


def snapshot(directory: Path) -> dict[str, bytes]:
    return {str(p.relative_to(directory)): p.read_bytes() for p in sorted(directory.rglob("*")) if p.is_file()}


def assert_clean(exc: BaseException) -> None:
    for secret in SECRETS:
        assert secret not in str(exc) and secret not in repr(exc)


def unb64url(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def canonical(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


@pytest.fixture
def keys() -> Keys:
    return Keys(KEY)


@pytest.fixture
def two_months(tmp_path, keys) -> Path:
    """两个月份文件：4 月 2 行、5 月 3 行。"""
    led = Ledger.open(tmp_path, keys)
    led.append(make_rows(2, ts="2031-04-30T23:00:00Z"), datetime(2031, 4, 30, 23, tzinfo=timezone.utc))
    led.append(make_rows(3, start=2), NOW)
    return tmp_path


# ---- 钥匙 ----


def test_keys_from_env():
    assert keys_from_env({}) is None and key_status({}) == L.NO_KEY
    assert keys_from_env({"MARKETS_LEDGER_KEY": "   "}) is None and key_status({"MARKETS_LEDGER_KEY": " "}) == L.NO_KEY
    short = {"MARKETS_LEDGER_KEY": "x" * 31 + "  "}
    assert keys_from_env(short) is None and key_status(short) == L.SHORT_KEY
    keys = keys_from_env({"MARKETS_LEDGER_KEY": f"  {KEY}\n", "MARKETS_LEDGER_KEY_OLD": f"{OLD}\n"})
    assert keys == Keys(KEY, OLD) and key_status({"MARKETS_LEDGER_KEY": KEY}) is None
    # 旧口令太短或和新口令一样时忽略
    assert keys_from_env({"MARKETS_LEDGER_KEY": KEY, "MARKETS_LEDGER_KEY_OLD": "short"}) == Keys(KEY)
    assert keys_from_env({"MARKETS_LEDGER_KEY": KEY, "MARKETS_LEDGER_KEY_OLD": KEY}) == Keys(KEY)
    assert repr(keys) == "Keys(***)" and str(keys) == "Keys(***)"


def test_keys_from_env_reads_os_environ(monkeypatch):
    assert keys_from_env() is None
    monkeypatch.setenv("MARKETS_LEDGER_KEY", KEY)
    assert keys_from_env() == Keys(KEY)


# ---- 读写和格式 ----


def test_round_trip(tmp_path, keys):
    directory = tmp_path / "markets"
    led = Ledger.open(directory, keys)
    assert not directory.exists()  # 打开只读不写
    assert led.rows() == [] and led.head16() is None and led.head == L.ZERO
    assert led.append([], NOW) == L.ZERO and not directory.exists()

    rows = make_rows(3)
    head = led.append(rows, NOW)
    assert len(head) == 64 and led.head16() == head[:16] and led.seq == 3
    assert sorted(p.name for p in directory.iterdir()) == ["2031-05.jsonl.enc", "head.json", "ledger.meta.json"]

    again = Ledger.open(directory, Keys(KEY))
    assert again.head == head and again.head16() == head[:16]
    assert again.rows() == [{**row, "seq": i} for i, row in enumerate(rows, 1)]
    # 返回的是副本
    again.rows()[0]["quote"]["bid"] = 0.99
    assert again.rows()[0]["quote"]["bid"] == 0.39

    # 磁盘上没有任何明文
    for name, data in snapshot(directory).items():
        for needle in ("ZZFAKE", "测试问题", "shown", "0.39"):
            assert needle.encode() not in data, name


def test_missing_ts_filled_from_now(tmp_path, keys):
    led = Ledger.open(tmp_path, keys)
    led.append([{"type": "witness", "value": 0.3}], NOW)
    assert Ledger.open(tmp_path, keys).rows() == [{"type": "witness", "value": 0.3, "ts": "2031-05-14T23:30:00Z",
                                                    "seq": 1}]


def test_format_matches_spec(tmp_path, keys):
    """按设计文档独立重算：scrypt 参数、钥匙编号、DBL1 行、AAD、加盐承诺和哈希链、head.json。"""
    head = Ledger.open(tmp_path, keys).append(make_rows(3), NOW)
    meta = json.loads((tmp_path / "ledger.meta.json").read_text())
    kid = meta["current"]
    assert meta["version"] == 1 and list(meta["keys"]) == [kid]
    entry = meta["keys"][kid]
    assert (entry["kdf"], entry["n"], entry["r"], entry["p"]) == ("scrypt", 32768, 8, 1)
    salt = base64.b64decode(entry["salt"])
    assert len(salt) == 16
    key = hashlib.scrypt(KEY.encode(), salt=salt, n=32768, r=8, p=1, dklen=32, maxmem=64 * 2**20)
    assert hmac.new(key, b"digest-bot kid", hashlib.sha256).hexdigest()[:8] == kid

    prev = "0" * 64
    text = (tmp_path / MAY).read_text()
    assert text.endswith("\n")
    for seq, line in enumerate(text.splitlines(), 1):
        magic, line_kid, line_seq, nonce, ct = line.split(".")
        assert (magic, line_kid, line_seq) == ("DBL1", kid, str(seq)) and len(unb64url(nonce)) == 12
        plain = AESGCM(key).decrypt(unb64url(nonce), unb64url(ct), f"{kid}|{seq}|{prev}".encode())
        obj = json.loads(plain)
        assert plain.decode() == canonical(obj) and obj["seq"] == seq
        row_salt = base64.b64decode(obj.pop("salt"))
        assert len(row_salt) == 16
        commitment = hashlib.sha256(row_salt + canonical(obj).encode()).digest()
        prev = hashlib.sha256(bytes.fromhex(prev) + commitment + hashlib.sha256(line.encode()).digest()).hexdigest()
    assert prev == head
    assert json.loads((tmp_path / "head.json").read_text()) == {
        "version": 1, "seq": 3, "head": head, "updated": "2031-05-14T23:30:00Z"}


def test_append_only_and_meta_stable(tmp_path, keys):
    led = Ledger.open(tmp_path, keys)
    led.append(make_rows(2), NOW)
    before = (tmp_path / MAY).read_bytes()
    meta = (tmp_path / "ledger.meta.json").read_bytes()
    reopened = Ledger.open(tmp_path, Keys(KEY))
    reopened.append(make_rows(2, start=2), NOW)
    assert (tmp_path / MAY).read_bytes().startswith(before)  # 旧行原样保留
    assert (tmp_path / "ledger.meta.json").read_bytes() == meta  # 同一把钥匙不重写元数据
    assert [r["seq"] for r in Ledger.open(tmp_path, keys).rows()] == [1, 2, 3, 4]


def test_salted_commitments_differ_for_same_row(tmp_path, keys):
    led = Ledger.open(tmp_path, keys)
    row = make_rows(1)[0]
    led.append([row], NOW)
    led.append([dict(row)], NOW)
    first, second = (tmp_path / MAY).read_text().splitlines()
    assert first.split(".")[3:] != second.split(".")[3:]


def test_head16_stable(tmp_path, keys):
    head = Ledger.open(tmp_path, keys).append(make_rows(2), NOW)
    assert {Ledger.open(tmp_path, keys).head16() for _ in range(3)} == {head[:16]}


def test_month_rollover(two_months, keys):
    assert sorted(p.name for p in two_months.glob("*.jsonl.enc")) == [APRIL, MAY]
    assert len((two_months / APRIL).read_text().splitlines()) == 2
    led = Ledger.open(two_months, keys)
    assert [r["seq"] for r in led.rows()] == [1, 2, 3, 4, 5]
    # 时间早于最后一个文件的记录不往回写，跟在最新的文件后面
    led.append(make_rows(1, start=9, ts="2031-04-02T00:00:00Z"), NOW)
    assert len((two_months / APRIL).read_text().splitlines()) == 2
    assert len((two_months / MAY).read_text().splitlines()) == 4
    assert Ledger.open(two_months, keys).rows()[-1]["key"] == "ZZFAKE-31MAY-T9"


def test_month_uses_utc_of_row_ts(tmp_path, keys):
    # 北京时间 5 月 1 日早上 7 点是 UTC 4 月 30 日
    Ledger.open(tmp_path, keys).append(make_rows(1, ts="2031-05-01T07:00:00+08:00"), NOW)
    assert [p.name for p in tmp_path.glob("*.jsonl.enc")] == [APRIL]


def test_bad_rows_write_nothing(tmp_path, keys):
    directory = tmp_path / "markets"
    led = Ledger.open(directory, keys)
    for bad in ([{"salt": "x"}], [{"seq": 1}], [{"value": float("nan")}], [{"when": NOW}], ["not a dict"],
                [make_rows(1)[0], {"value": float("inf")}]):
        with pytest.raises(LedgerError) as info:
            led.append(bad, NOW)
        assert info.value.reason == L.BAD_ROW
    assert not directory.exists()
    led.append(make_rows(1), NOW)
    assert [r["seq"] for r in Ledger.open(directory, keys).rows()] == [1]


# ---- 篡改 ----


def _edit_lines(path: Path, change) -> None:
    lines = path.read_text().splitlines()
    change(lines)
    path.write_text("".join(line + "\n" for line in lines))


def _flip(text: str, index: int) -> str:
    return text[:index] + ("A" if text[index] != "A" else "B") + text[index + 1:]


def flip_ciphertext(d: Path) -> None:
    def change(lines):
        parts = lines[1].split(".")
        parts[4] = _flip(parts[4], len(parts[4]) // 2)
        lines[1] = ".".join(parts)
    _edit_lines(d / MAY, change)


def flip_last_ciphertext_char(d: Path) -> None:
    def change(lines):
        parts = lines[0].split(".")
        parts[4] = parts[4][:-1] + ("B" if parts[4][-1] == "A" else "A")
        lines[0] = ".".join(parts)
    _edit_lines(d / MAY, change)


def flip_nonce(d: Path) -> None:
    def change(lines):
        parts = lines[0].split(".")
        parts[3] = _flip(parts[3], 3)
        lines[0] = ".".join(parts)
    _edit_lines(d / MAY, change)


def swap_lines(d: Path) -> None:
    def change(lines):
        lines[0], lines[1] = lines[1], lines[0]
    _edit_lines(d / MAY, change)


def delete_middle_line(d: Path) -> None:
    _edit_lines(d / MAY, lambda lines: lines.pop(1))


def delete_last_line(d: Path) -> None:
    _edit_lines(d / MAY, lambda lines: lines.pop())


def cut_last_line(d: Path) -> None:
    data = (d / MAY).read_bytes()
    (d / MAY).write_bytes(data[:-20])


def delete_month_file(d: Path) -> None:
    (d / APRIL).unlink()


def truncate_head(d: Path) -> None:
    data = (d / "head.json").read_bytes()
    (d / "head.json").write_bytes(data[: len(data) // 2])


def edit_head_hash(d: Path) -> None:
    head = json.loads((d / "head.json").read_text())
    head["head"] = ("0" if head["head"][0] != "0" else "1") + head["head"][1:]
    (d / "head.json").write_text(json.dumps(head))


def edit_head_seq(d: Path) -> None:
    head = json.loads((d / "head.json").read_text())
    head["seq"] -= 1
    (d / "head.json").write_text(json.dumps(head))


def raise_head_seq(d: Path) -> None:
    head = json.loads((d / "head.json").read_text())
    head["seq"] += 1
    (d / "head.json").write_text(json.dumps(head))


def delete_head(d: Path) -> None:
    (d / "head.json").unlink()


def replace_with_other_ledger_line(d: Path) -> None:
    # 用同一把钥匙（同一个盐和编号）另写一本账，把它序号 3 的一行换进来：
    # 序号和钥匙都对，但上一个链头不同，AAD 校验不过
    other = d.parent / "other"
    other.mkdir()
    shutil.copy(d / "ledger.meta.json", other / "ledger.meta.json")
    Ledger.open(other, Keys(KEY)).append(make_rows(3, start=7), NOW)
    foreign = (other / MAY).read_text().splitlines()[2]
    assert foreign.split(".")[2] == "3"

    def change(lines):
        lines[0] = foreign
    _edit_lines(d / MAY, change)


def edit_meta_salt(d: Path) -> None:
    meta = json.loads((d / "ledger.meta.json").read_text())
    entry = meta["keys"][meta["current"]]
    entry["salt"] = base64.b64encode(b"\x07" * 16).decode()
    (d / "ledger.meta.json").write_text(json.dumps(meta))


def delete_meta(d: Path) -> None:
    (d / "ledger.meta.json").unlink()


def plaintext_line(d: Path) -> None:
    _edit_lines(d / MAY, lambda lines: lines.append('{"key": "ZZFAKE"}'))


TAMPERS = {
    flip_ciphertext: {L.TAMPERED, L.BAD_FORMAT},
    flip_last_ciphertext_char: {L.TAMPERED, L.BAD_FORMAT},
    flip_nonce: {L.TAMPERED, L.BAD_FORMAT},
    swap_lines: {L.BROKEN_CHAIN},
    delete_middle_line: {L.BROKEN_CHAIN},
    delete_last_line: {L.BROKEN_CHAIN},
    cut_last_line: {L.BAD_FORMAT},
    delete_month_file: {L.BROKEN_CHAIN},
    truncate_head: {L.BAD_HEAD},
    edit_head_hash: {L.BROKEN_CHAIN},
    edit_head_seq: {L.BROKEN_CHAIN},
    raise_head_seq: {L.BROKEN_CHAIN},
    delete_head: {L.BROKEN_CHAIN},
    replace_with_other_ledger_line: {L.TAMPERED},
    edit_meta_salt: {L.WRONG_KEY},
    delete_meta: {L.BAD_META},
    plaintext_line: {L.BAD_FORMAT},
}


@pytest.mark.parametrize("tamper", list(TAMPERS), ids=lambda f: f.__name__)
def test_tampering_detected(tmp_path, keys, tamper):
    directory = tmp_path / "markets"
    led = Ledger.open(directory, keys)
    led.append(make_rows(2, ts="2031-04-30T23:00:00Z"), NOW)
    led.append(make_rows(3, start=2), NOW)
    tamper(directory)
    before = snapshot(directory)
    with pytest.raises(LedgerError) as info:
        Ledger.open(directory, Keys(KEY))
    assert info.value.reason in TAMPERS[tamper]
    assert_clean(info.value)
    assert snapshot(directory) == before  # 校验失败时不碰任何文件


def test_wrong_key(two_months):
    before = snapshot(two_months)
    with pytest.raises(LedgerError) as info:
        Ledger.open(two_months, Keys(WRONG))
    assert info.value.reason == L.WRONG_KEY
    assert_clean(info.value)
    assert snapshot(two_months) == before


def test_interrupted_append_recovers(tmp_path, keys, caplog):
    """记录写进去了但 head.json 还没换（中途被打断）：链本身完整，可以继续用。"""
    led = Ledger.open(tmp_path, keys)
    led.append(make_rows(2), NOW)
    old_head = (tmp_path / "head.json").read_bytes()
    full = led.append(make_rows(1, start=2), NOW)
    (tmp_path / "head.json").write_bytes(old_head)
    caplog.set_level(logging.WARNING)
    again = Ledger.open(tmp_path, keys)
    assert again.head == full and len(again.rows()) == 3 and "head.json 落后" in caplog.text
    newest = again.append(make_rows(1, start=3), NOW)
    assert json.loads((tmp_path / "head.json").read_text())["seq"] == 4
    assert Ledger.open(tmp_path, keys).head == newest


# ---- 换钥匙 ----


def test_rotation(tmp_path):
    led = Ledger.open(tmp_path, Keys(OLD))
    led.append(make_rows(2), NOW)
    old_kid = json.loads((tmp_path / "ledger.meta.json").read_text())["current"]
    blob_hash = write_blob(tmp_path, "backtest", {"n": 3}, Keys(OLD))

    # 新口令对不上任何已有编号，又没给旧口令：读不了旧记录
    with pytest.raises(LedgerError) as info:
        Ledger.open(tmp_path, Keys(KEY))
    assert info.value.reason == L.WRONG_KEY

    rotated = Keys(KEY, OLD)
    led = Ledger.open(tmp_path, rotated)
    assert len(led.rows()) == 2
    head = led.append(make_rows(2, start=2), NOW)
    meta = json.loads((tmp_path / "ledger.meta.json").read_text())
    new_kid = meta["current"]
    assert new_kid != old_kid and set(meta["keys"]) == {old_kid, new_kid}
    kids = [line.split(".")[1] for line in (tmp_path / MAY).read_text().splitlines()]
    assert kids == [old_kid, old_kid, new_kid, new_kid]  # 旧行不重新加密，新行用新钥匙

    reopened = Ledger.open(tmp_path, Keys(KEY, OLD))
    assert reopened.head == head and [r["seq"] for r in reopened.rows()] == [1, 2, 3, 4]
    assert read_blob(tmp_path, "backtest", rotated) == {"n": 3}
    # 旧记录还在，所以去掉旧口令就打不开；只拿旧口令也读不了新记录
    for partial in (Keys(KEY), Keys(OLD)):
        with pytest.raises(LedgerError) as info:
            Ledger.open(tmp_path, partial)
        assert info.value.reason == L.WRONG_KEY
        assert_clean(info.value)
    # 数据块重写后换成新钥匙
    assert write_blob(tmp_path, "backtest", {"n": 3}, rotated) == blob_hash
    assert read_blob(tmp_path, "backtest", Keys(KEY)) == {"n": 3}
    assert (tmp_path / "backtest.json.enc").read_text().split(".")[1] == new_kid


# ---- 没有钥匙、没有加密库 ----


def test_no_key_writes_nothing(tmp_path):
    directory = tmp_path / ".state" / "markets"
    for call in (lambda: Ledger.open(directory, None),
                 lambda: write_blob(directory, "backtest", {"n": 1}, None),
                 lambda: read_blob(directory, "backtest", None)):
        with pytest.raises(LedgerError) as info:
            call()
        assert info.value.reason == L.NO_KEY
    assert keys_from_env({}) is None
    assert not (tmp_path / ".state").exists()


def test_missing_cryptography(tmp_path, keys, monkeypatch):
    monkeypatch.setitem(sys.modules, "cryptography.hazmat.primitives.ciphers.aead", None)
    for call in (lambda: Ledger.open(tmp_path / "m", keys), lambda: write_blob(tmp_path / "m", "backtest", {}, keys)):
        with pytest.raises(LedgerError) as info:
            call()
        assert info.value.reason == L.NO_CRYPTO
    assert not (tmp_path / "m").exists()


def test_broken_cryptography_install(tmp_path, keys, monkeypatch):
    """装坏了的 cryptography 导入时抛的是继承 BaseException 的 PanicException，也要变成 LedgerError。"""

    class FakePanic(BaseException):
        pass

    class Broken:
        def __getattr__(self, name):
            raise FakePanic("Python API call failed")

    monkeypatch.setitem(sys.modules, "cryptography.hazmat.primitives.ciphers.aead", Broken())
    with pytest.raises(LedgerError) as info:
        Ledger.open(tmp_path, keys)
    assert info.value.reason == L.NO_CRYPTO


# ---- 加密数据块 ----


def test_blob_round_trip(tmp_path, keys):
    summary = {"series": {"ZZFAKE": {"n": 7, "mae_median": 0.05}}, "partial": False, "名称": "测试"}
    digest = write_blob(tmp_path, "backtest", summary, keys)
    assert digest == hashlib.sha256(canonical(summary).encode()).hexdigest()
    data = (tmp_path / "backtest.json.enc").read_bytes()
    assert data.startswith(b"DBB1.") and b"ZZFAKE" not in data and "测试".encode() not in data
    assert read_blob(tmp_path, "backtest", Keys(KEY)) == summary
    assert read_blob(tmp_path, "missing", keys) is None
    # 数据块和账本共用一把钥匙
    Ledger.open(tmp_path, keys).append(make_rows(1), NOW)
    assert len(json.loads((tmp_path / "ledger.meta.json").read_text())["keys"]) == 1
    # 整体重写
    write_blob(tmp_path, "backtest", {"n": 1}, keys)
    assert read_blob(tmp_path, "backtest", keys) == {"n": 1}


@pytest.mark.parametrize("rotate", [False, True])
def test_blob_then_append_share_one_new_key(tmp_path, rotate):
    """回测的顺序：先打开账本，再写数据块，最后追加记录。两边要用同一个新编号，谁也不覆盖谁。"""
    if rotate:
        Ledger.open(tmp_path, Keys(OLD)).append(make_rows(1), NOW)
    keys = Keys(KEY, OLD) if rotate else Keys(KEY)
    led = Ledger.open(tmp_path, keys)
    write_blob(tmp_path, "backtest", {"n": 2}, keys)
    led.append(make_rows(1, start=1), NOW)
    meta = json.loads((tmp_path / "ledger.meta.json").read_text())
    assert len(meta["keys"]) == (2 if rotate else 1)
    assert (tmp_path / "backtest.json.enc").read_text().split(".")[1] == meta["current"]
    assert (tmp_path / MAY).read_text().splitlines()[-1].split(".")[1] == meta["current"]
    assert read_blob(tmp_path, "backtest", keys) == {"n": 2}
    assert len(Ledger.open(tmp_path, keys).rows()) == (2 if rotate else 1)


def test_blob_bound_to_name_and_key(tmp_path, keys):
    write_blob(tmp_path, "backtest", {"n": 7}, keys)
    shutil.copy(tmp_path / "backtest.json.enc", tmp_path / "other.json.enc")
    with pytest.raises(LedgerError) as info:
        read_blob(tmp_path, "other", keys)
    assert info.value.reason == L.TAMPERED
    with pytest.raises(LedgerError) as info:
        read_blob(tmp_path, "backtest", Keys(WRONG))
    assert info.value.reason == L.WRONG_KEY
    assert_clean(info.value)
    text = (tmp_path / "backtest.json.enc").read_text()
    parts = text.strip().split(".")
    parts[3] = _flip(parts[3], 5)
    (tmp_path / "backtest.json.enc").write_text(".".join(parts) + "\n")
    with pytest.raises(LedgerError) as info:
        read_blob(tmp_path, "backtest", keys)
    assert info.value.reason in {L.TAMPERED, L.BAD_FORMAT}
    for bad in ("../escape", "Backtest", "a.b", ""):
        with pytest.raises(LedgerError):
            write_blob(tmp_path, bad, {}, keys)
    assert not (tmp_path.parent / "escape.json.enc").exists()


# ---- 上传前自检 ----


def test_assert_no_plaintext_removes_planted_files(tmp_path, keys):
    state = tmp_path / ".state"
    markets = state / "markets"
    led = Ledger.open(markets, keys)
    head = led.append(make_rows(3), NOW)
    write_blob(markets, "backtest", {"n": 1}, keys)
    (markets / "2031-06.jsonl.enc").write_bytes(b"")  # 空文件里没有明文，可以留着
    valid = snapshot(markets)
    assert assert_no_plaintext(state, ["ZZFAKE-31MAY-T0"]) == []

    planted = {
        "notes.json": '{"key": "ZZFAKE-31MAY-T0", "p": 42}',
        "2031-07.jsonl.enc": '{"key": "ZZFAKE-31MAY-T0"}\n',
        "extra.enc": "ZZFAKE-31MAY-T0 0.42",
        "fake.json.enc": "DBB1.not-a-real-blob",
        "sub/ledger.meta.json": '{"version": 1, "keys": {}, "current": "ZZFAKE"}',
        "sub/head.json": '{"version": 1, "seq": 1, "head": "ZZFAKE", "updated": "x"}',
        "sub/deeper/readme.txt": "测试问题",
    }
    for name, text in planted.items():
        (markets / name).parent.mkdir(parents=True, exist_ok=True)
        (markets / name).write_text(text, encoding="utf-8")
    # 一行密文后面混进一行明文的月份文件
    mixed = (markets / MAY).read_text() + '{"key": "ZZFAKE-31MAY-T0"}\n'
    (markets / "2031-08.jsonl.enc").write_text(mixed)

    problems = assert_no_plaintext(state, ["ZZFAKE-31MAY-T0"])
    assert len(problems) == 1 and "8 个文件" in problems[0] and "已删除" in problems[0]
    assert all("ZZFAKE" not in p and "测试" not in p for p in problems)
    assert snapshot(markets) == valid  # 合格的文件原样保留，不合格的都删了
    assert Ledger.open(markets, keys).head == head
    assert read_blob(markets, "backtest", keys) == {"n": 1}


def test_assert_no_plaintext_validates_meta_and_head(tmp_path, keys):
    state = tmp_path / ".state"
    markets = state / "markets"
    Ledger.open(markets, keys).append(make_rows(1), NOW)
    meta = json.loads((markets / "ledger.meta.json").read_text())
    meta["note"] = "ZZFAKE-31MAY-T0"  # 多出来的键
    (markets / "ledger.meta.json").write_text(json.dumps(meta))
    head = json.loads((markets / "head.json").read_text())
    head["head"] = "测试" + head["head"][2:]  # 不是十六进制
    (markets / "head.json").write_text(json.dumps(head, ensure_ascii=False))
    problems = assert_no_plaintext(state)
    assert problems and "2 个文件" in problems[0]
    assert not (markets / "ledger.meta.json").exists() and not (markets / "head.json").exists()
    assert (markets / MAY).exists()


def test_assert_no_plaintext_token_scan(tmp_path, caplog):
    state = tmp_path / ".state"
    (state / "archive" / "2031").mkdir(parents=True)
    (state / "state.json").write_text('{"sent": {"https://example.com/zzfake-31may-t0": "2031-05-14"}}')
    (state / "archive" / "2031" / "05-14.md").write_text("# 简报\n\nZZFAKE-31MAY-T0 出现在这里\n")
    (state / "archive" / "items.jsonl").write_text('{"title": "普通新闻"}\n')
    (state / "archive" / "2031" / "05-13.md").write_text("# 简报\n\n测试问题第0个\n")  # 问题原文不查
    caplog.set_level(logging.WARNING)
    problems = assert_no_plaintext(state, ["ZZFAKE-31MAY-T0", "ZZ", ""])
    assert problems == ["有 2 个公开文件里出现了市场合约代码"]
    assert "ZZFAKE" not in caplog.text.upper()
    assert (state / "state.json").exists() and (state / "archive" / "2031" / "05-14.md").exists()
    # 太短的代码不查，免得和普通文字撞上
    assert assert_no_plaintext(state, ["ZZ", "zzf"]) == []


def test_assert_no_plaintext_never_raises(tmp_path, monkeypatch):
    assert assert_no_plaintext(tmp_path / "missing", ["ZZFAKE-31MAY-T0"]) == []
    (tmp_path / "file").write_text("x")
    assert assert_no_plaintext(tmp_path / "file", ["ZZFAKE-31MAY-T0"]) == []
    # markets 本身是个文件：删掉
    state = tmp_path / ".state"
    state.mkdir()
    (state / "markets").write_text('{"key": "ZZFAKE-31MAY-T0"}')
    assert len(assert_no_plaintext(state)) == 1 and not (state / "markets").exists()
    # 内部出错也只返回问题，不抛异常
    (state / "markets").mkdir()
    (state / "markets" / "notes.txt").write_text("x")

    def boom(*args, **kwargs):
        raise PermissionError("denied")

    monkeypatch.setattr(L.os, "walk", boom)
    problems = assert_no_plaintext(state, ["ZZFAKE-31MAY-T0"])
    assert problems == ["市场账本目录自检出错：PermissionError"]


# ---- 密钥不外泄 ----


def test_secrets_never_in_errors_or_logs(tmp_path, keys, caplog):
    caplog.set_level(logging.DEBUG)
    errors: list[BaseException] = []
    led = Ledger.open(tmp_path, keys)
    led.append(make_rows(2), NOW)
    write_blob(tmp_path, "backtest", {"n": 1}, keys)
    for call in (lambda: Ledger.open(tmp_path, Keys(WRONG, OLD)),
                 lambda: read_blob(tmp_path, "backtest", Keys(WRONG)),
                 lambda: led.append([{"salt": KEY}], NOW),
                 lambda: Ledger.open(tmp_path / "missing", None)):
        with pytest.raises(LedgerError) as info:
            call()
        errors.append(info.value)
    flip_ciphertext(tmp_path)
    with pytest.raises(LedgerError) as info:
        Ledger.open(tmp_path, keys)
    errors.append(info.value)
    keys_from_env({"MARKETS_LEDGER_KEY": KEY, "MARKETS_LEDGER_KEY_OLD": "fake-too-short"})
    Ledger.open(tmp_path / "rotated", Keys(OLD)).append(make_rows(1), NOW)
    write_blob(tmp_path / "rotated", "backtest", {}, Keys(KEY, OLD))
    for exc in errors:
        assert_clean(exc)
        assert exc.__cause__ is None and exc.__suppress_context__ or exc.__context__ is None
    for secret in (*SECRETS, "fake-too-short"):
        assert secret not in caplog.text
    assert "MARKETS_LEDGER_KEY_OLD" in caplog.text  # 只提变量名
