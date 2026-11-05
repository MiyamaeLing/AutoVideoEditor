"""最新版に更新する（GitHub の MiyamaeLing/AutoVideoEditor の main から）。

使い方の例:
  python update.py              # 新しい版があるか確かめて、あれば更新する（確認あり）
  python update.py --check      # 確かめるだけ
  python update.py --yes        # 確認なしで更新（画面の「ヘルプ → 最新版に更新」はこれを使います）
  python update.py --zip AutoVideoEditor-main.zip   # 手元にダウンロードした zip で更新する
  python update.py --rollback   # 直前の更新を取り消して、元に戻す

自分で直した templates のファイル・.venv・設定・順番待ちの一覧は消しません。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

import rc_win  # noqa: F401
import rc_ops_update as U


def log(s: str) -> None:
    print(s, flush=True)


def main():
    ap = argparse.ArgumentParser(description="最新版に更新する")
    ap.add_argument("--check", action="store_true", help="新しい版があるか確かめるだけ")
    ap.add_argument("--yes", action="store_true", help="確認せずに更新する")
    ap.add_argument("--zip", type=Path, help="手元の zip（GitHub の「Download ZIP」）で更新する")
    ap.add_argument("--force", action="store_true", help="同じ版でも、git で管理しているフォルダでも更新する")
    ap.add_argument("--no-deps", action="store_true", help="ライブラリの入れ直しをしない")
    ap.add_argument("--rollback", action="store_true", help="直前の更新を取り消す")
    ap.add_argument("--record-manifest", action="store_true", help=argparse.SUPPRESS)   # setup.ps1 から使う
    ap.add_argument("--app-dir", type=Path, default=U.APP_DIR, help=argparse.SUPPRESS)
    args = ap.parse_args()
    app = args.app_dir.resolve()

    if args.record_manifest:
        U.record_manifest(app)
        return
    if args.rollback:
        log("■ 直前の更新を取り消しています")
        try:
            U.rollback(app, log)
        except U.UpdateError as e:
            sys.exit(f"[エラー] {e}")
        log("■ 元に戻しました。ツールを開き直してください。")
        return

    loc = U.local_version(app)
    log(f"■ 今の版: {U.version_text(loc)}")
    rem = {"sha": "", "date": "", "message": ""}
    if not args.zip:
        try:
            rem = U.remote_version()
        except U.UpdateError as e:
            sys.exit(f"[エラー] {e}")
        newer = U.is_newer(loc, rem)
        log(f"  最新の版: {U.version_text(rem)} {rem['message']}")
        if args.check:
            log("  → 新しい版があります。`python update.py` で更新できます。" if newer else "  → 最新の版です。")
            print(f"UPDATE_AVAILABLE={int(newer)}")
            return
        if not newer and not args.force:
            log("■ すでに最新の版です。更新は必要ありません。")
            return
    if (app / ".git").exists() and not args.force:
        sys.exit("[エラー] このフォルダは git で管理されています。自動更新は使わずに `git pull` で更新してください。\n"
                 "  （どうしても上書きするときは --force を付けます）")
    if not args.yes and not args.check:
        ans = input("更新しますか？（自分で直したテンプレートや設定は残ります） [y/N] ").strip().lower()
        if ans not in ("y", "yes", "はい"):
            log("  やめました。")
            return

    if args.zip:
        log(f"■ {args.zip.name} から更新します")
        try:
            data = args.zip.read_bytes()
        except OSError as e:
            sys.exit(f"[エラー] zip を読めませんでした: {e}")
    else:
        log("■ 最新版をダウンロードしています")
        try:
            data = U._http_get(U.ZIP_URL, timeout=60, progress=lambda p: log(f"    ダウンロード {p}%"))
        except Exception as e:  # noqa: BLE001
            sys.exit(f"[エラー] ダウンロードできませんでした（ネットにつながっているか確認してください）: {e}")
    try:
        files, sha = U.read_zip(data)
    except U.UpdateError as e:
        sys.exit(f"[エラー] {e}")
    if rem.get("sha") and sha and sha != rem["sha"]:
        rem = {"sha": sha, "date": "", "message": ""}   # ダウンロードの間にさらに新しくなった
    log("■ ファイルを更新しています")
    try:
        plan = U.apply(files, app, sha=sha or rem.get("sha", ""), date=rem.get("date", ""), log=log)
    except OSError as e:
        sys.exit(f"[エラー] ファイルを書き換えられませんでした: {e}\n"
                 "  ほかのアプリでツールのファイルを開いていたら閉じて、もう一度実行してください。"
                 "  途中まで書き換えたときは `python update.py --rollback` で元に戻せます。")
    log(f"  更新 {len(plan['write'])}個 / 追加 {len(plan['add'])}個 / 変わらず {plan['same']}個")
    for rel in plan["write"][:30]:
        log(f"    更新: {rel}")
    for rel in plan["add"][:30]:
        log(f"    追加: {rel}")
    if plan["kept"]:
        log(f"  自分で直したテンプレートのファイルは、そのままにしました（{len(plan['kept'])}個）:")
        for rel in plan["kept"][:30]:
            log(f"    そのまま: {rel}")
        log(f"  新しい版の中身は {Path(plan['backup']).name} の「{U.NEW_COPY}」にあります（見比べたいとき用）")
    if plan["backup"]:
        log(f"  上書きする前のファイル: {plan['backup']}（`python update.py --rollback` で元に戻せます）")
    if plan["requirements_changed"]:
        if args.no_deps:
            log("[お知らせ] 必要なライブラリが変わりました。setup.bat をもう一度実行してください。")
        elif not U.install_requirements(app, log):
            log("[注意] ライブラリの追加に失敗しました。setup.bat をもう一度実行してください。")
        else:
            log("  ライブラリの追加が終わりました")
    if plan["setup_changed"]:
        log("[お知らせ] セットアップ（setup.bat）も新しくなりました。動かないところがあれば、setup.bat をもう一度実行してください。")
    log("■ 更新が終わりました。ツールを開き直すと新しい版になります。")
    print("UPDATED=1")


if __name__ == "__main__":
    main()
