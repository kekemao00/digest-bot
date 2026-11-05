"""市场板块的加密账本：只追加、带哈希链的逐行密文，以及整文件加密的小数据块（回测结果）。

文件都在 .state/markets/ 下，会随 state 分支公开：
- ledger.meta.json：密钥派生参数（scrypt 的盐）和钥匙编号，不含任何市场数据。
- head.json：最新的序号和链头哈希，登记到公开透明日志用。
- YYYY-MM.jsonl.enc：每行一条 AES-256-GCM 加密的记录，按记录时间（UTC）的月份分文件，只追加，从不改写旧行。
- <name>.json.enc：整文件加密的数据块（如回测结果 backtest.json.enc），每次整体重写。

每行的格式：DBL1.<钥匙编号>.<序号>.<b64url(nonce)>.<b64url(密文)>
- 明文是记录加上随机盐 salt 和序号 seq 的规范 JSON；附加认证数据是 "钥匙编号|序号|上一个链头"。
- 承诺值 c = sha256(盐 + 去掉 salt 后的规范 JSON)，链头 h = sha256(上一个链头 + c + sha256(这一行))。

安全约定：
- 密钥只从环境变量 MARKETS_LEDGER_KEY / MARKETS_LEDGER_KEY_OLD 读取，而且只在本模块读。
- 没有密钥就什么都不写；打开账本只读不写，校验不通过时不碰任何文件。
- 异常和日志里只有固定的中文原因、月份文件名和行号，没有密钥、明文和文件内容。
- cryptography 只在用到时才导入，缺这个库时账本停用。
"""

from __future__ import annotations

import base64
import binascii
import copy
import hashlib
import hmac
import json
import logging
import os
import re
import tempfile
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger("digest.markets")

KEY_ENV = "MARKETS_LEDGER_KEY"
OLD_KEY_ENV = "MARKETS_LEDGER_KEY_OLD"
MIN_KEY_CHARS = 32

VERSION = 1
META = "ledger.meta.json"
HEAD = "head.json"
LINE_MAGIC = "DBL1"
BLOB_MAGIC = "DBB1"
ZERO = "0" * 64  # 第一行之前的链头

# 密钥派生：scrypt(n=2**15, r=8, p=1)，每把钥匙一个 16 字节随机盐
SCRYPT_N = 2**15
SCRYPT_R = 8
SCRYPT_P = 1
KEY_BYTES = 32
SALT_BYTES = 16
NONCE_BYTES = 12
TAG_BYTES = 16
KID_INFO = b"digest-bot kid"

# 公开文件里查合约代码时，太短的代码容易和普通文字撞上，不查
MIN_TOKEN_CHARS = 6

# 原因（写进 LedgerError.reason，调用方可以直接放进 Summary）
NO_KEY = "没有账本钥匙"
SHORT_KEY = "账本钥匙太短"
NO_CRYPTO = "缺少加密库"
KDF_FAILED = "账本钥匙派生失败"
WRONG_KEY = "账本钥匙不对"
TAMPERED = "账本内容被改动或损坏"
BROKEN_CHAIN = "账本链对不上"
BAD_FORMAT = "账本文件格式不对"
BAD_META = "账本元数据有误"
BAD_HEAD = "账本头文件有误"
BAD_ROW = "账本记录无法保存"
IO_FAILED = "账本读写失败"
SELF_CHECK_STATUS = "市场板块：自检发现问题"

_KID = re.compile(r"[0-9a-f]{8}")
_HEX64 = re.compile(r"[0-9a-f]{64}")
_B64U = re.compile(r"[A-Za-z0-9_-]+")
_LINE = re.compile(r"DBL1\.([0-9a-f]{8})\.([1-9][0-9]{0,11})\.([A-Za-z0-9_-]+)\.([A-Za-z0-9_-]+)")
_BLOB = re.compile(r"DBB1\.([0-9a-f]{8})\.([A-Za-z0-9_-]+)\.([A-Za-z0-9_-]+)")
_MONTH_FILE = re.compile(r"([0-9]{4}-(?:0[1-9]|1[0-2]))\.jsonl\.enc")
_BLOB_NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,39}")
_UPDATED = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z")


class LedgerError(Exception):
    """账本打不开或写不进。reason 是固定的中文原因；str() 可能再带上月份文件名和行号，不含密钥和内容。"""

    def __init__(self, reason: str, where: str | None = None) -> None:
        self.reason = reason
        super().__init__(f"{reason}（{where}）" if where else reason)


# ---- 钥匙 ----


@dataclass(frozen=True)
class _Kdf:
    """一把钥匙的派生参数，存在 ledger.meta.json 里。"""

    salt: bytes
    n: int = SCRYPT_N
    r: int = SCRYPT_R
    p: int = SCRYPT_P

    def to_json(self) -> dict:
        return {"salt": _b64(self.salt), "kdf": "scrypt", "n": self.n, "r": self.r, "p": self.p}


@dataclass(frozen=True, repr=False)
class Keys:
    """账本口令：current 用来写新记录（也能读它自己写的），old 只用来读换钥匙之前的记录。"""

    current: str
    old: str | None = None
    # 派生结果的缓存：scrypt 每次要几十毫秒，同一次运行里同一个盐只算一次
    _cache: dict = field(default_factory=dict, init=False, compare=False)

    def __repr__(self) -> str:  # 防止被意外打印到日志
        return "Keys(***)"

    def _derive(self, which: str, kdf: _Kdf) -> bytes | None:
        passphrase = self.current if which == "current" else self.old
        if passphrase is None:
            return None
        cache_key = (which, kdf)
        if cache_key not in self._cache:
            self._cache[cache_key] = _scrypt(passphrase, kdf)
        return self._cache[cache_key]


def _read_keys(env: Mapping[str, str]) -> tuple[Keys | None, str | None, bool]:
    """(口令, 不可用的原因, 旧口令是否因为太短被忽略)。"""
    current = (env.get(KEY_ENV) or "").strip()
    if not current:
        return None, NO_KEY, False
    if len(current) < MIN_KEY_CHARS:
        return None, SHORT_KEY, False
    old = (env.get(OLD_KEY_ENV) or "").strip() or None
    short_old = old is not None and len(old) < MIN_KEY_CHARS
    if short_old or old == current:
        old = None
    return Keys(current, old), None, short_old


def keys_from_env(env: Mapping[str, str] = os.environ) -> Keys | None:
    """读取账本口令。MARKETS_LEDGER_KEY 去掉首尾空白后不足 32 个字符时返回 None（不记账）。

    MARKETS_LEDGER_KEY_OLD 可选，换钥匙后用来读旧记录；太短或和新口令相同时忽略。
    """
    keys, _status, short_old = _read_keys(env)
    if short_old:
        log.warning("市场账本：%s 太短（至少 %d 个字符），已忽略", OLD_KEY_ENV, MIN_KEY_CHARS)
    return keys


def key_status(env: Mapping[str, str] = os.environ) -> str | None:
    """钥匙可用时返回 None，否则返回原因：没有账本钥匙 / 账本钥匙太短。不返回钥匙本身。"""
    return _read_keys(env)[1]


def _scrypt(passphrase: str, kdf: _Kdf) -> bytes:
    # OpenSSL 默认只给 scrypt 32 MiB 内存，n=2**15、r=8 正好超出，按参数算出需要的上限再留 1 MiB
    maxmem = 128 * kdf.r * (kdf.n + kdf.p + 2) + 2**20
    try:
        return hashlib.scrypt(
            passphrase.encode("utf-8"), salt=kdf.salt, n=kdf.n, r=kdf.r, p=kdf.p, dklen=KEY_BYTES, maxmem=maxmem
        )
    except (ValueError, MemoryError, AttributeError):
        raise LedgerError(KDF_FAILED) from None


def _kid(key: bytes) -> str:
    return hmac.new(key, KID_INFO, hashlib.sha256).hexdigest()[:8]


def _aesgcm() -> Any:
    """按需导入 AES-GCM；没装 cryptography 时账本停用。"""
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException:
        # 除了 ImportError，装坏了的 cryptography 在导入时会抛 pyo3 的 PanicException（继承 BaseException），
        # 调用方只接 Exception，这里一并转成 LedgerError，免得它改变进程的退出码
        raise LedgerError(NO_CRYPTO) from None
    return AESGCM


@dataclass(frozen=True)
class _Meta:
    kdfs: dict[str, _Kdf]  # 钥匙编号 -> 派生参数
    current: str

    def to_json(self) -> dict:
        return {"version": VERSION, "keys": {kid: kdf.to_json() for kid, kdf in self.kdfs.items()},
                "current": self.current}


class _Keyring:
    """把口令和元数据里的钥匙编号对上：哪些编号读得了，新记录用哪个编号写。"""

    def __init__(self, keys: Keys, directory: Path, meta: _Meta | None) -> None:
        self.keys = keys
        self.dir = directory
        self.meta = meta
        self._opened: dict[str, bytes | None] = {}
        self._writer: tuple[str, bytes, _Kdf] | None = None

    def key_for(self, kid: str) -> bytes | None:
        """能解开这个编号的派生密钥；当前和旧口令都对不上时为 None。"""
        if kid not in self._opened:
            self._opened[kid] = self._match(kid)
        return self._opened[kid]

    def _match(self, kid: str) -> bytes | None:
        kdf = self.meta.kdfs.get(kid) if self.meta else None
        if kdf is None:
            return None
        for which in ("current", "old"):
            key = self.keys._derive(which, kdf)
            if key is not None and hmac.compare_digest(_kid(key), kid):
                return key
        return None

    def _stored(self) -> tuple[dict[str, _Kdf], _Meta | None]:
        """内存里和磁盘上的钥匙合在一起。同一次运行里账本和数据块各有一个 _Keyring，
        先写的那个可能已经往元数据里加了新钥匙，后写的要沿用它，不能覆盖掉。"""
        disk = _read_meta(self.dir)
        return {**(self.meta.kdfs if self.meta else {}), **(disk.kdfs if disk else {})}, disk

    def writer(self) -> tuple[str, bytes]:
        """写新记录用的 (钥匙编号, 密钥)。当前口令对不上已有编号时生成新盐和新编号（写入时才存进元数据）。"""
        if self._writer is None:
            stored, _disk = self._stored()
            for kid, kdf in stored.items():
                key = self.keys._derive("current", kdf)
                if key is not None and hmac.compare_digest(_kid(key), kid):
                    self._writer = (kid, key, kdf)
                    break
            else:
                while True:
                    kdf = _Kdf(os.urandom(SALT_BYTES))
                    key = self.keys._derive("current", kdf)
                    if _kid(key) not in stored:  # 编号只有 32 位，极少数情况下撞上已有编号就换个盐
                        break
                self._writer = (_kid(key), key, kdf)
                if stored:
                    log.info("市场账本：口令和已有的钥匙都对不上，启用新钥匙（旧记录要靠 %s 读）", OLD_KEY_ENV)
            self._opened[self._writer[0]] = self._writer[1]
        return self._writer[0], self._writer[1]

    def save_meta(self) -> None:
        """把写新记录用的钥匙记进元数据并设为当前钥匙；和磁盘上的一样时不写。"""
        kid, _key = self.writer()
        stored, disk = self._stored()
        stored.setdefault(kid, self._writer[2])
        meta = _Meta(stored, kid)
        if meta != disk:
            _atomic_write(self.dir / META, _json_file(meta.to_json()))
        self.meta = meta


# ---- 编码 ----


def canonical_json(obj: Any) -> str:
    """规范 JSON：键排序、无空白、保留中文；不接受 NaN 和无穷大。"""
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _json_file(obj: Any) -> bytes:
    return (json.dumps(obj, ensure_ascii=False, sort_keys=True, indent=1) + "\n").encode("utf-8")


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _unb64(text: Any) -> bytes | None:
    """标准 base64；只接受规范写法（重新编码后一字不差），否则 None。"""
    if not isinstance(text, str):
        return None
    try:
        data = base64.b64decode(text, validate=True)
    except (binascii.Error, ValueError):
        return None
    return data if _b64(data) == text else None


def _b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _unb64u(text: str) -> bytes | None:
    """URL 安全、不带填充的 base64；同一段字节只有一种写法，改动任何一个字符都会被发现。"""
    if not _B64U.fullmatch(text):
        return None
    try:
        data = base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except (binascii.Error, ValueError):
        return None
    return data if _b64u(data) == text else None


def _commit(plain: dict) -> bytes:
    """承诺值：sha256(盐 + 去掉 salt 后的规范 JSON)。"""
    salt = _unb64(plain.get("salt"))
    if salt is None or len(salt) != SALT_BYTES:
        raise LedgerError(TAMPERED)
    rest = {k: v for k, v in plain.items() if k != "salt"}
    return hashlib.sha256(salt + canonical_json(rest).encode("utf-8")).digest()


def _link(prev: str, commitment: bytes, line: str) -> str:
    return hashlib.sha256(bytes.fromhex(prev) + commitment + hashlib.sha256(line.encode("ascii")).digest()).hexdigest()


def _utc(now: datetime) -> datetime:
    return now.replace(tzinfo=timezone.utc) if now.tzinfo is None else now.astimezone(timezone.utc)


def _iso(now: datetime) -> str:
    return _utc(now).strftime("%Y-%m-%dT%H:%M:%SZ")


def _month_of(ts: Any, fallback: datetime) -> str:
    """记录时间 ts（ISO）所在的 UTC 月份；解析不了或年份不是四位数时用 fallback 的月份。"""
    if isinstance(ts, str):
        try:
            when = _utc(datetime.fromisoformat(ts))
        except (ValueError, OverflowError):
            when = None
        if when is not None and 1000 <= when.year <= 9999:
            return f"{when.year:04d}-{when.month:02d}"
    when = _utc(fallback)
    return f"{when.year:04d}-{when.month:02d}"


# ---- 文件读写 ----


def _atomic_write(path: Path, data: bytes) -> None:
    """先写同目录下的临时文件并落盘，再用 os.replace 换上，中途出错不会留下半个文件。"""
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    _fsync_dir(path.parent)


def _append_lines(path: Path, lines: list[str]) -> None:
    new = not path.exists()
    with open(path, "ab") as f:
        f.write("".join(line + "\n" for line in lines).encode("ascii"))
        f.flush()
        os.fsync(f.fileno())
    if new:
        _fsync_dir(path.parent)


def _fsync_dir(directory: Path) -> None:
    # 让新文件名和 os.replace 也落盘；有的系统（如 Windows）不支持对目录 fsync，忽略即可
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _meta_from(data: bytes) -> _Meta | None:
    """解析并严格校验元数据：只允许约定的键，编号是 8 位十六进制，盐是 16 字节的 base64。"""
    try:
        obj = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(obj, dict) or set(obj) != {"version", "keys", "current"} or not _is_int(obj["version"], VERSION):
        return None
    keys = obj["keys"]
    if not isinstance(keys, dict) or not keys or obj["current"] not in keys:
        return None
    kdfs: dict[str, _Kdf] = {}
    for kid, entry in keys.items():
        if not _KID.fullmatch(kid) or not isinstance(entry, dict) or set(entry) != {"salt", "kdf", "n", "r", "p"}:
            return None
        salt = _unb64(entry["salt"])
        n, r, p = entry["n"], entry["r"], entry["p"]
        if salt is None or len(salt) != SALT_BYTES or entry["kdf"] != "scrypt":
            return None
        # 参数限制在合理范围内，被改坏的元数据不会让 scrypt 吃光内存
        if not (_is_int(n) and 2**14 <= n <= 2**17 and n & (n - 1) == 0 and _is_int(r) and 1 <= r <= 16
                and _is_int(p) and 1 <= p <= 4):
            return None
        kdfs[kid] = _Kdf(salt, n, r, p)
    return _Meta(kdfs, obj["current"])


@dataclass(frozen=True)
class _Head:
    seq: int
    head: str


def _head_from(data: bytes) -> _Head | None:
    try:
        obj = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(obj, dict) or set(obj) != {"version", "seq", "head", "updated"}:
        return None
    seq, head, updated = obj["seq"], obj["head"], obj["updated"]
    if not (_is_int(obj["version"], VERSION) and _is_int(seq) and seq >= 0 and isinstance(head, str)
            and _HEX64.fullmatch(head) and isinstance(updated, str) and _UPDATED.fullmatch(updated)):
        return None
    return _Head(seq, head)


def _is_int(value: Any, expected: int | None = None) -> bool:
    ok = isinstance(value, int) and not isinstance(value, bool)
    return ok and (expected is None or value == expected)


def _read_meta(directory: Path) -> _Meta | None:
    path = directory / META
    if not path.exists():
        return None
    meta = _meta_from(path.read_bytes())
    if meta is None:
        raise LedgerError(BAD_META)
    return meta


def _read_head(directory: Path) -> _Head | None:
    path = directory / HEAD
    if not path.exists():
        return None
    head = _head_from(path.read_bytes())
    if head is None:
        raise LedgerError(BAD_HEAD)
    return head


def _month_files(directory: Path) -> list[tuple[str, Path]]:
    if not directory.is_dir():
        return []
    found = []
    for path in directory.iterdir():
        match = _MONTH_FILE.fullmatch(path.name)
        if match:
            found.append((match.group(1), path))
    return sorted(found)


def _file_lines(path: Path) -> list[str]:
    """一个月份文件的各行；不是 ASCII 或最后一行没写完都算格式不对。"""
    try:
        text = path.read_bytes().decode("ascii")
    except UnicodeDecodeError:
        raise LedgerError(BAD_FORMAT, path.name) from None
    if not text:
        return []
    if not text.endswith("\n"):
        raise LedgerError(BAD_FORMAT, f"{path.name} 最后一行不完整")
    return text[:-1].split("\n")


# ---- 账本 ----


class Ledger:
    """打开时解密并校验全部记录；之后可以读记录、追加新记录。用 Ledger.open() 创建。"""

    def __init__(self, directory: Path, ring: _Keyring) -> None:
        self.dir = directory
        self._ring = ring
        self._rows: list[dict] = []
        self.seq = 0
        self.head = ZERO
        self._month: str | None = None  # 最后一行所在文件的月份，新记录不会写进更早的文件
        self._broken = False

    @classmethod
    def open(cls, dir: Path | str, keys: Keys | None) -> Ledger:
        """读元数据，按月份顺序解密每一行，核对序号、附加认证数据、哈希链和 head.json。

        只读不写（目录不存在时也不创建）。任何一处对不上都抛 LedgerError，调用方这次就不记账、不碰文件。
        """
        if keys is None:
            raise LedgerError(NO_KEY)
        aesgcm = _aesgcm()
        directory = Path(dir)
        try:
            ledger = cls(directory, _Keyring(keys, directory, _read_meta(directory)))
            ledger._load(aesgcm)
        except OSError as exc:
            raise LedgerError(IO_FAILED, type(exc).__name__) from None
        return ledger

    def _load(self, aesgcm: Any) -> None:
        files = _month_files(self.dir)
        if files and self._ring.meta is None:
            raise LedgerError(BAD_META, "元数据文件缺失")
        heads = [ZERO]  # heads[i] 是第 i 行之后的链头
        ciphers: dict[str, Any] = {}
        for month, path in files:
            for number, line in enumerate(_file_lines(path), 1):
                where = f"{path.name} 第 {number} 行"
                plain = self._open_line(line, where, aesgcm, ciphers)
                self.head = _link(self.head, _commit(plain), line)
                self.seq += 1
                heads.append(self.head)
                plain.pop("salt")
                self._rows.append(plain)
            self._month = month
        head = _read_head(self.dir)
        if head is None:
            if self.seq:
                raise LedgerError(BROKEN_CHAIN, "head.json 缺失")
        elif head.seq > self.seq or heads[head.seq] != head.head:
            raise LedgerError(BROKEN_CHAIN, "head.json")
        elif head.seq < self.seq:
            # 上次追加时记录写进去了、head.json 还没来得及换（中途被打断）；记录本身的链是完整的
            log.warning("市场账本：head.json 落后于账本 %d 行，下次追加时一起更新", self.seq - head.seq)

    def _open_line(self, line: str, where: str, aesgcm: Any, ciphers: dict[str, Any]) -> dict:
        match = _LINE.fullmatch(line)
        if not match:
            raise LedgerError(BAD_FORMAT, where)
        kid, seq_text, nonce_text, ct_text = match.groups()
        seq = int(seq_text)
        if seq != self.seq + 1:
            raise LedgerError(BROKEN_CHAIN, where)
        nonce, ct = _unb64u(nonce_text), _unb64u(ct_text)
        if nonce is None or ct is None or len(nonce) != NONCE_BYTES or len(ct) < TAG_BYTES:
            raise LedgerError(BAD_FORMAT, where)
        key = self._ring.key_for(kid)
        if key is None:
            raise LedgerError(WRONG_KEY, where)
        if kid not in ciphers:
            ciphers[kid] = aesgcm(key)
        try:
            data = ciphers[kid].decrypt(nonce, ct, f"{kid}|{seq}|{self.head}".encode("ascii"))
            plain = json.loads(data.decode("utf-8"))
        except Exception:  # 认证失败（InvalidTag）或解出来不是 JSON：钥匙对、内容被改过
            raise LedgerError(TAMPERED, where) from None
        if not isinstance(plain, dict) or not _is_int(plain.get("seq"), seq):
            raise LedgerError(TAMPERED, where)
        return plain

    def rows(self) -> list[dict]:
        """全部记录（解密后，按写入顺序）；每条带 seq，不带 salt。返回副本，改了不影响账本。"""
        return copy.deepcopy(self._rows)

    def head16(self) -> str | None:
        """页脚上印的链头前 16 位；账本还是空的时为 None。"""
        return self.head[:16] if self.seq else None

    def append(self, rows: list[dict], now: datetime) -> str:
        """追加记录，返回新的链头（64 位十六进制）。rows 为空时什么都不写。

        每条记录没有 ts 时补上 now（UTC）。先全部加密好，再依次写元数据（有新钥匙时）、各月份文件
        （追加并 fsync）和 head.json（临时文件 + os.replace）。记录不能自带 salt 和 seq 字段，
        值必须能编码成 JSON（不能有 NaN、datetime 等），否则整批都不写。
        """
        if self._broken:
            raise LedgerError(IO_FAILED, "上次写入中断")
        if not rows:
            return self.head
        cipher = _aesgcm()
        try:
            kid, key = self._ring.writer()
        except OSError as exc:
            raise LedgerError(IO_FAILED, type(exc).__name__) from None
        aead = cipher(key)
        seq, head, month = self.seq, self.head, self._month
        lines: list[tuple[str, str]] = []  # (月份, 一行)
        added: list[dict] = []
        for row in rows:
            if not isinstance(row, dict) or "salt" in row or "seq" in row:
                raise LedgerError(BAD_ROW)
            seq += 1
            record = {**row, "seq": seq, "salt": _b64(os.urandom(SALT_BYTES))}
            record.setdefault("ts", _iso(now))
            try:
                text = canonical_json(record)
            except (TypeError, ValueError):
                raise LedgerError(BAD_ROW) from None
            # 承诺值按解析回来的对象算，和读账本时的算法完全一致
            plain = json.loads(text)
            nonce = os.urandom(NONCE_BYTES)
            ct = aead.encrypt(nonce, text.encode("utf-8"), f"{kid}|{seq}|{head}".encode("ascii"))
            line = f"{LINE_MAGIC}.{kid}.{seq}.{_b64u(nonce)}.{_b64u(ct)}"
            head = _link(head, _commit(plain), line)
            month = max(month or "", _month_of(record["ts"], now))
            lines.append((month, line))
            plain.pop("salt")
            added.append(plain)
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            self._ring.save_meta()
            for target in dict.fromkeys(m for m, _ in lines):
                _append_lines(self.dir / f"{target}.jsonl.enc", [line for m, line in lines if m == target])
            _atomic_write(self.dir / HEAD, _json_file({"version": VERSION, "seq": seq, "head": head,
                                                       "updated": _iso(now)}))
        except OSError as exc:
            self._broken = True
            raise LedgerError(IO_FAILED, type(exc).__name__) from None
        self.seq, self.head, self._month = seq, head, month
        self._rows.extend(added)
        return head


# ---- 加密数据块 ----


def _blob_path(directory: Path, name: str) -> Path:
    if not isinstance(name, str) or not _BLOB_NAME.fullmatch(name):
        raise LedgerError(BAD_FORMAT, "数据块名字不对")
    return directory / f"{name}.json.enc"


def write_blob(dir: Path | str, name: str, obj: Any, keys: Keys | None) -> str:
    """把 obj 加密成整个文件 <name>.json.enc（每次整体重写），返回明文规范 JSON 的 sha256。

    格式 DBB1.<钥匙编号>.<b64url(nonce)>.<b64url(密文)>，附加认证数据 "钥匙编号|blob|name"，
    所以把文件改名后就解不开了。
    """
    if keys is None:
        raise LedgerError(NO_KEY)
    directory = Path(dir)
    path = _blob_path(directory, name)
    cipher = _aesgcm()
    try:
        text = canonical_json(obj)
    except (TypeError, ValueError):
        raise LedgerError(BAD_ROW) from None
    try:
        ring = _Keyring(keys, directory, _read_meta(directory))
        kid, key = ring.writer()
        nonce = os.urandom(NONCE_BYTES)
        ct = cipher(key).encrypt(nonce, text.encode("utf-8"), f"{kid}|blob|{name}".encode("ascii"))
        directory.mkdir(parents=True, exist_ok=True)
        ring.save_meta()
        _atomic_write(path, f"{BLOB_MAGIC}.{kid}.{_b64u(nonce)}.{_b64u(ct)}\n".encode("ascii"))
    except OSError as exc:
        raise LedgerError(IO_FAILED, type(exc).__name__) from None
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def read_blob(dir: Path | str, name: str, keys: Keys | None) -> Any | None:
    """解密 <name>.json.enc；文件不存在时返回 None，钥匙不对或内容被改时抛 LedgerError。"""
    if keys is None:
        raise LedgerError(NO_KEY)
    directory = Path(dir)
    path = _blob_path(directory, name)
    cipher = _aesgcm()
    try:
        if not path.exists():
            return None
        raw = path.read_bytes()
        meta = _read_meta(directory)
    except OSError as exc:
        raise LedgerError(IO_FAILED, type(exc).__name__) from None
    try:
        text = raw.decode("ascii")
    except UnicodeDecodeError:
        raise LedgerError(BAD_FORMAT, path.name) from None
    match = _BLOB.fullmatch(text[:-1] if text.endswith("\n") else text)
    if not match:
        raise LedgerError(BAD_FORMAT, path.name)
    kid, nonce_text, ct_text = match.groups()
    nonce, ct = _unb64u(nonce_text), _unb64u(ct_text)
    if nonce is None or ct is None or len(nonce) != NONCE_BYTES or len(ct) < TAG_BYTES:
        raise LedgerError(BAD_FORMAT, path.name)
    if meta is None:
        raise LedgerError(BAD_META, "元数据文件缺失")
    key = _Keyring(keys, directory, meta).key_for(kid)
    if key is None:
        raise LedgerError(WRONG_KEY, path.name)
    try:
        data = cipher(key).decrypt(nonce, ct, f"{kid}|blob|{name}".encode("ascii"))
        return json.loads(data.decode("utf-8"))
    except Exception:
        raise LedgerError(TAMPERED, path.name) from None


# ---- 上传前自检 ----


def assert_no_plaintext(state_dir: Path | str, tokens: Iterable[str] = ()) -> list[str]:
    """上传 .state 之前的自检，返回发现的问题（中文，不含文件内容和合约代码）；从不抛异常。

    1. .state/markets/ 下每个文件都必须是合格的 ledger.meta.json / head.json、每行都是 DBL1 密文的
       *.jsonl.enc，或者 DBB1 密文的其他 *.enc。不合格的文件只可能是程序的错（只有本模块往这里写），
       直接删除，免得明文被提交到公开的 state 分支。
    2. state.json、items.jsonl 等 .state 下的文件和 archive/ 里不能出现 tokens（合约代码、condition id；
       不查问题原文，它可能和新闻标题撞上）。只记警告，不删文件。
    调用方在返回非空时往 Summary 写 SELF_CHECK_STATUS。
    """
    root = Path(state_dir)
    problems: list[str] = []
    try:
        problems += _clean_markets_dir(root / "markets")
    except Exception as exc:
        problems.append(f"市场账本目录自检出错：{type(exc).__name__}")
    try:
        problems += _scan_public_files(root, tokens)
    except Exception as exc:
        problems.append(f"公开文件自检出错：{type(exc).__name__}")
    for problem in problems:
        log.warning("市场板块自检：%s", problem)
    return problems


def _clean_markets_dir(markets: Path) -> list[str]:
    if not markets.exists() and not markets.is_symlink():
        return []
    bad: list[Path] = []
    if markets.is_symlink() or not markets.is_dir():
        bad.append(markets)
    else:
        for top, dirs, files in os.walk(markets, followlinks=False):
            base = Path(top)
            # 指向别处的目录链接不跟进去，当作不合格文件删掉（只删链接本身）
            linked = [d for d in dirs if (base / d).is_symlink()]
            dirs[:] = [d for d in dirs if d not in linked]
            for name in [*linked, *files]:
                path = base / name
                if not _conforming(path):
                    bad.append(path)
    failed = 0
    for path in bad:
        try:
            path.unlink()
        except OSError:
            failed += 1
    problems = []
    if len(bad) > failed:
        problems.append(f"市场账本目录里有 {len(bad) - failed} 个文件格式不对（可能是明文），已删除")
    if failed:
        problems.append(f"市场账本目录里有 {failed} 个格式不对的文件删不掉")
    return problems


def _conforming(path: Path) -> bool:
    if path.is_symlink() or not path.is_file():
        return False
    name = path.name
    if name not in (META, HEAD) and not name.endswith(".enc"):
        return False
    data = path.read_bytes()
    if name == META:
        return _meta_from(data) is not None
    if name == HEAD:
        return _head_from(data) is not None
    try:
        text = data.decode("ascii")
    except UnicodeDecodeError:
        return False
    if name.endswith(".jsonl.enc"):
        if not text:
            return True
        return text.endswith("\n") and all(_LINE.fullmatch(line) for line in text[:-1].split("\n"))
    return bool(_BLOB.fullmatch(text[:-1] if text.endswith("\n") else text))


def _scan_public_files(root: Path, tokens: Iterable[str]) -> list[str]:
    needles = sorted({t.strip().casefold() for t in tokens if isinstance(t, str) and len(t.strip()) >= MIN_TOKEN_CHARS})
    if not needles or not root.is_dir():
        return []
    # .state 下直接放的文件（state.json、items.jsonl 等）和整个 archive/；不看 markets/（密文）和 .git/
    paths = [p for p in root.iterdir() if p.is_file()]
    archive = root / "archive"
    if archive.is_dir():
        for top, _dirs, files in os.walk(archive):
            paths.extend(Path(top) / name for name in files)
    hits = 0
    for path in paths:
        text = path.read_text(encoding="utf-8", errors="replace").casefold()
        if any(needle in text for needle in needles):
            hits += 1
    return [f"有 {hits} 个公开文件里出现了市场合约代码"] if hits else []
