"""roughcut_drop.bat（ファイルをドラッグ＆ドロップして荒編集）で、受け取ったファイルを正しく読み取る。

- Explorer は、複数のファイルをドロップしたとき「マウスでつかんだファイル」を先頭にして渡すので、
  名前の順（C0001, C0002, … の自然な順）に並べ直す
- 「Q&A.mp4」のように & を含み、スペースを含まない名前は、cmd がコマンドの区切りと見なして
  途中で切ってしまう。そのため bat が元のコマンドライン（%cmdcmdline%）を環境変数で渡し、
  ここで読み取り直す
"""
from __future__ import annotations

import re
from pathlib import Path


def natural_key(p: Path | str):
    """C2.mp4 < C10.mp4 となる並べ方（大文字・小文字は区別しない）"""
    name = Path(p).name.lower()
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", name)]


def sort_inputs(paths: list[Path]) -> list[Path]:
    return sorted(paths, key=lambda p: (natural_key(p), str(p).lower()))


def parse_cmdline(cmdline: str, bat_name: str = "roughcut_drop.bat") -> list[str] | None:
    """cmd.exe /c ""C:\\...\\roughcut_drop.bat" C:\\撮影\\Q&A.mp4 "D:\\a b\\x.mp4"" から、bat の後ろのパスを取り出す。

    bat の名前が見つからないとき（コマンドプロンプトから直接実行したときなど）は None。
    """
    low = cmdline.lower()
    i = low.rfind(bat_name.lower())
    if i < 0:
        return None
    rest = cmdline[i + len(bat_name):]
    if rest.startswith('"'):        # bat のパスを閉じる引用符
        rest = rest[1:]
    rest = rest.rstrip()
    if rest.endswith('"') and rest.count('"') % 2 == 1:   # cmd /c "…" の外側を閉じる引用符
        rest = rest[:-1]
    out = []
    for m in re.finditer(r'"([^"]*)"|(\S+)', rest):
        tok = m.group(1) if m.group(1) is not None else m.group(2)
        if tok:
            out.append(tok)
    return out


def drop_inputs(argv_paths: list[str], cmdline: str | None, exists=lambda p: Path(p).is_file()) -> list[Path]:
    """bat から渡されたパス（argv_paths）と元のコマンドラインから、本当にドロップされたファイルを決める。"""
    paths = list(argv_paths)
    if cmdline:
        parsed = parse_cmdline(cmdline)
        # 取り出したものがすべて実在するファイルなら、そちらを信じる（& で切れた名前を元に戻せる）
        if parsed and all(exists(p) for p in parsed):
            paths = parsed
    return sort_inputs([Path(p) for p in paths])
