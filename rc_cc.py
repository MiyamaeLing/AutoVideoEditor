"""Claude Code（サブスク）経由で Claude を呼ぶ。

「作成」タブの AI で「Claude で自動（サブスク・Claude Code）」を選ぶと、環境変数
ROUGHCUT_BACKEND=claude-code が付き、rc_llm._client() / _call() がここを使う。
API キーは使わず、PC に入っている Claude Code（`claude` コマンド）に、ログイン済みの
サブスク（Pro / Max）で答えてもらう。

  claude -p --output-format json --json-schema <schema> --system-prompt <短い役割> --tools ""
  （依頼文は標準入力で渡す。画像があるときは一時フォルダに保存し、Read ツールで見てもらう）
"""
from __future__ import annotations

import base64
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROLE = ("あなたはYouTube動画の編集を手伝うアシスタントです。依頼文の指示に従い、指定されたJSONの形式で答えてください。"
        "ファイルの作成・コマンドの実行はしないでください。")
TIMEOUT = int(os.environ.get("ROUGHCUT_CC_TIMEOUT", "1800"))   # 1回の問い合わせの上限（秒）
# Claude Code に API キーを渡すと、サブスクではなく API の料金になるので外す
_DROP_ENV = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL", "CLAUDE_CODE_USE_BEDROCK",
             "CLAUDE_CODE_USE_VERTEX")
INSTALL_HINT = ("Claude Code が見つかりません。PowerShell で次を実行してインストールし、"
                "一度 `claude` を起動してサブスクのアカウントでログインしてください:\n"
                "  irm https://claude.ai/install.ps1 | iex")
LOGIN_HINT = ("Claude Code にログインしていません。PowerShell（またはコマンドプロンプト）で `claude` を起動し、"
              "サブスク（Pro / Max）のアカウントでログインしてから、もう一度押してください。")


def find_claude() -> str | None:
    """claude コマンドの場所。見つからなければ None"""
    env = os.environ.get("ROUGHCUT_CLAUDE_BIN")
    if env and Path(env).exists():
        return env
    p = shutil.which("claude")
    if p:
        return p
    home = Path.home()
    cands = [home / ".local" / "bin" / "claude.exe", home / ".local" / "bin" / "claude",
             home / ".claude" / "local" / "claude"]
    if os.environ.get("APPDATA"):
        cands.append(Path(os.environ["APPDATA"]) / "npm" / "claude.cmd")
    for c in cands:
        if c.exists():
            return str(c)
    return None


def available() -> bool:
    return find_claude() is not None


class ClaudeCodeClient:
    """rc_llm._call から呼ばれる。anthropic.Anthropic() の代わり"""

    is_claude_code = True

    def __init__(self, exe: str | None = None):
        self.exe = exe or find_claude()
        if not self.exe:
            sys.exit("[エラー] " + INSTALL_HINT)
        self.model = os.environ.get("ROUGHCUT_CC_MODEL", "").strip()   # 空なら Claude Code の設定のまま
        self._unsupported: set[str] = set()   # 古い Claude Code が知らないオプション

    # ---------- 依頼文 ----------
    @staticmethod
    def _flatten(user, tmp: Path) -> tuple[str, int]:
        """messages の content（文字 or ブロックの並び）を、1つの文字にする。画像はファイルにして場所を書く"""
        if isinstance(user, str):
            return user, 0
        parts, n = [], 0
        for b in user:
            t = b.get("type") if isinstance(b, dict) else None
            if t == "text":
                parts.append(b.get("text", ""))
            elif t == "image":
                src = b.get("source") or {}
                if src.get("type") != "base64":
                    continue
                n += 1
                ext = {"image/png": ".png", "image/webp": ".webp", "image/gif": ".gif"}.get(src.get("media_type"), ".jpg")
                p = tmp / f"image_{n:02d}{ext}"
                p.write_bytes(base64.b64decode(src.get("data", "")))
                parts.append(f"[画像{n}: {p}]（Read ツールでこのファイルを開いて見てください）")
        return "\n".join(parts), n

    # ---------- 実行 ----------
    def _cmd(self, schema: dict, images: int, effort: str) -> list[str]:
        cmd = [self.exe, "-p", "--output-format", "json", "--system-prompt", ROLE]
        opt = {
            "--json-schema": [json.dumps(schema, ensure_ascii=False, separators=(",", ":"))],
            "--tools": ["Read" if images else ""],
            "--no-session-persistence": [],
            "--effort": [effort] if effort in ("low", "medium", "high", "xhigh", "max") else [],
        }
        if self.model:
            opt["--model"] = [self.model]
        for k, v in opt.items():
            if k in self._unsupported or (k == "--effort" and not v):
                continue
            cmd += [k, *v]
        if images:
            cmd += ["--allowedTools", "Read"]
        return cmd

    def _env(self) -> dict:
        e = {k: v for k, v in os.environ.items() if k not in _DROP_ENV}
        e.setdefault("PYTHONIOENCODING", "utf-8")
        return e

    def call(self, system: str, user, schema: dict, effort: str, usage: dict) -> dict:
        with tempfile.TemporaryDirectory(prefix="rc_cc_") as td:
            tmp = Path(td)
            body, images = self._flatten(user, tmp)
            schema_note = ""
            if "--json-schema" in self._unsupported:
                schema_note = ("\n\n## 出力するJSONの形式（JSON Schema）\n```json\n"
                               + json.dumps(schema, ensure_ascii=False) + "\n```\nJSONだけを出力してください。")
            prompt = f"{system}{schema_note}\n\n---\n\n{body}"
            for _ in range(4):   # 知らないオプションを外してやり直す（古い Claude Code 向け）
                cmd = self._cmd(schema, images, effort)
                try:
                    r = subprocess.run(cmd, input=prompt.encode("utf-8"), capture_output=True, cwd=td,
                                       env=self._env(), timeout=TIMEOUT)
                except FileNotFoundError:
                    sys.exit("[エラー] " + INSTALL_HINT)
                except subprocess.TimeoutExpired:
                    raise RuntimeError(f"Claude Code の返事が {TIMEOUT // 60} 分たっても来ませんでした。"
                                       "素材を分けるか、時間をおいてもう一度試してください。")
                out = r.stdout.decode("utf-8", "replace")
                err = r.stderr.decode("utf-8", "replace")
                bad = self._unknown_option(err + out)
                if bad and bad not in self._unsupported:
                    self._unsupported.add(bad)
                    if bad == "--json-schema":
                        prompt = (f"{system}\n\n## 出力するJSONの形式（JSON Schema）\n```json\n"
                                  + json.dumps(schema, ensure_ascii=False) + "\n```\nJSONだけを出力してください。"
                                  + f"\n\n---\n\n{body}")
                    continue
                return self._parse(out, err, r.returncode, usage)
        raise RuntimeError("Claude Code が古いため使えません。`claude update` で更新してください。")

    @staticmethod
    def _unknown_option(text: str) -> str | None:
        m = re.search(r"unknown option '?(--[a-zA-Z-]+)", text)
        return m.group(1) if m else None

    @staticmethod
    def _error_text(msg: str) -> str:
        low = msg.lower()
        if any(k in low for k in ("/login", "not logged in", "invalid api key", "authentication", "oauth")):
            return LOGIN_HINT
        if any(k in low for k in ("usage limit", "limit reached", "rate limit", "5-hour", "weekly limit")):
            return ("サブスクの使用量の上限に達しました。表示された時刻まで待つか、claude.ai の「使用状況」を確認してから、"
                    "もう一度押してください。")
        if "credit balance" in low:
            return "Claude Code が API キーで動いています。`claude` を起動して /login でサブスクのアカウントに切り替えてください。"
        return f"Claude Code でエラーが出ました: {msg.strip()[:400]}"

    def _parse(self, out: str, err: str, code: int, usage: dict) -> dict:
        data = None
        for line in reversed(out.strip().splitlines() or [""]):
            try:
                data = json.loads(line)
                break
            except json.JSONDecodeError:
                continue
        if not isinstance(data, dict):
            data = None
            try:
                data = json.loads(out)
            except json.JSONDecodeError:
                pass
        if not isinstance(data, dict):
            sys.exit("[エラー] " + self._error_text(err or out or f"終了コード {code}"))
        usage["sub"] = True
        u = data.get("usage") or {}
        usage["in"] = usage.get("in", 0) + int(u.get("input_tokens", 0) or 0) \
            + int(u.get("cache_creation_input_tokens", 0) or 0) + int(u.get("cache_read_input_tokens", 0) or 0)
        usage["out"] = usage.get("out", 0) + int(u.get("output_tokens", 0) or 0)
        if data.get("is_error") or data.get("subtype") not in (None, "success"):
            msg = str(data.get("result") or data.get("subtype") or err)
            if data.get("subtype") == "error_max_structured_output_retries":
                raise RuntimeError("Claudeの返答を指定の形にできませんでした。もう一度試してください。")
            sys.exit("[エラー] " + self._error_text(msg))
        so = data.get("structured_output")
        if isinstance(so, dict):
            return so
        return parse_json_text(str(data.get("result") or ""))


def parse_json_text(text: str) -> dict:
    """返答の文字から JSON を取り出す（```json で囲まれていても読む）"""
    t = text.strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", t, re.S)
    if m:
        t = m.group(1).strip()
    try:
        v = json.loads(t)
        if isinstance(v, dict):
            return v
    except json.JSONDecodeError:
        pass
    i, j = t.find("{"), t.rfind("}")
    if 0 <= i < j:
        try:
            v = json.loads(t[i:j + 1])
            if isinstance(v, dict):
                return v
        except json.JSONDecodeError:
            pass
    raise RuntimeError("Claudeの返答を読み取れませんでした（JSONの形が壊れています）。もう一度試してください。")


def check_text() -> str:
    """画面の「接続を確認」用。ログイン状態まで確かめる（短い問い合わせを1回する）"""
    exe = find_claude()
    if not exe:
        return "⚠ " + INSTALL_HINT
    try:
        c = ClaudeCodeClient(exe)
        res = c.call("「ok」と答えてください。", "接続の確認です。",
                     {"type": "object", "properties": {"ok": {"type": "string"}}, "required": ["ok"]}, "low", {})
        return f"✔ Claude Code で接続できました（{exe}）" if res else "⚠ 返事が空でした"
    except SystemExit as e:
        return "⚠ " + str(e).replace("[エラー] ", "")
    except Exception as e:  # noqa: BLE001
        return f"⚠ {e}"


if __name__ == "__main__":
    print(check_text())
