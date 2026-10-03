"""发布流程用：从 pyproject.toml 读出版本号，从 CHANGELOG.md 取出这一版的说明。

用法：python scripts/release_notes.py <说明的输出路径>
成功时把说明写到输出路径，并打印版本号；版本号格式不对或更新日志里没有这一版时退出码为 1。
"""

from __future__ import annotations

import re
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VERSION = re.compile(r"\d+\.\d+\.\d+")


def version(pyproject: str) -> str:
    value = tomllib.loads(pyproject)["project"]["version"]
    if not VERSION.fullmatch(value):
        raise ValueError(f"版本号应为 X.Y.Z 格式，现在是 {value!r}")
    return value


def notes(changelog: str, version: str) -> str:
    """取出 `## vX.Y.Z` 标题下面、下一个同级标题之前的内容。"""
    heading = re.search(rf"^## v{re.escape(version)}(?=\s|$).*$", changelog, re.M)
    if not heading:
        raise ValueError(f"CHANGELOG.md 里没有 v{version} 这一节")
    rest = changelog[heading.end() :]
    end = re.search(r"^## ", rest, re.M)
    body = rest[: end.start() if end else None].strip()
    if not body:
        raise ValueError(f"CHANGELOG.md 里 v{version} 这一节是空的")
    return body + "\n"


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print(__doc__, file=sys.stderr)
        return 2
    try:
        current = version((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        text = notes((ROOT / "CHANGELOG.md").read_text(encoding="utf-8"), current)
    except ValueError as error:
        print(f"发布中止：{error}", file=sys.stderr)
        return 1
    out = Path(argv[0])
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    print(current)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
