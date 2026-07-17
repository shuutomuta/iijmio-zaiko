#!/usr/bin/env python3
"""IIJmio 端末ページの在庫監視スクリプト。

Playwright でページを描画し、在庫関連キーワードの出現状況を前回実行時
(state.json) と比較する。変化があればメールで通知する。

必要な環境変数:
  GMAIL_ADDRESS       送信元 Gmail アドレス
  GMAIL_APP_PASSWORD  Gmail のアプリパスワード (16桁)
  NOTIFY_TO           通知先アドレス (省略時は GMAIL_ADDRESS 宛)
"""

from __future__ import annotations

import json
import os
import smtplib
import sys
import time
from email.mime.text import MIMEText
from email.utils import formatdate
from pathlib import Path

TARGETS = [
    {
        "name": "Xiaomi POCO F7 Pro",
        "url": "https://www.iijmio.jp/device/xiaomi/pocof7pro.html",
    },
    {
        "name": "Xiaomi POCO X8 Pro",
        "url": "https://www.iijmio.jp/device/xiaomi/pocox8pro.html",
    },
]

# 在庫切れを示すキーワード
OUT_MARKERS = [
    "一時在庫切れ",
    "次回入荷未定",
    "在庫切れ",
    "入荷未定",
    "販売を終了",
    "販売終了",
]

# 在庫あり(購入可能)を示すキーワード
IN_MARKERS = [
    "このセットでお申し込み",
    "お申し込みはこちら",
    "カートに入れる",
    "端末のみ購入",
    "お申し込み",
]

STATE_FILE = Path("state.json")
PAGE_TIMEOUT_MS = 60_000
RETRY = 2


# ---------------------------------------------------------------- 解析ロジック


def summarize(text: str) -> dict:
    """ページ本文テキストから在庫関連の要約を作る。"""
    out_counts = {m: text.count(m) for m in OUT_MARKERS if text.count(m) > 0}
    in_counts = {m: text.count(m) for m in IN_MARKERS if text.count(m) > 0}

    # 在庫キーワードを含む行を文脈として抜き出す(通知メール用)
    lines = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or len(line) > 200:
            continue
        if any(m in line for m in OUT_MARKERS + IN_MARKERS):
            if line not in lines:
                lines.append(line)
    return {"out": out_counts, "in": in_counts, "lines": lines[:30]}


def status_key(summary: dict) -> str:
    """比較用の安定した文字列表現。"""
    return json.dumps({"out": summary["out"], "in": summary["in"]},
                      ensure_ascii=False, sort_keys=True)


def judge_change(prev: dict, cur: dict) -> tuple[bool, bool]:
    """(変化があったか, 在庫復活の可能性が高いか) を返す。"""
    changed = status_key(prev) != status_key(cur)
    if not changed:
        return False, False
    prev_out = sum(prev["out"].values())
    cur_out = sum(cur["out"].values())
    prev_in = sum(prev["in"].values())
    cur_in = sum(cur["in"].values())
    restock = cur_out < prev_out or cur_in > prev_in
    return True, restock


# ---------------------------------------------------------------- 取得


def fetch_text(url: str) -> str:
    from playwright.sync_api import sync_playwright

    last_err: Exception | None = None
    for attempt in range(1, RETRY + 2):
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch()
                page = browser.new_page(
                    user_agent=(
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/126.0.0.0 Safari/537.36"
                    )
                )
                page.goto(url, timeout=PAGE_TIMEOUT_MS, wait_until="load")
                # JS 描画の完了を待つ
                page.wait_for_timeout(5_000)
                text = page.inner_text("body")
                browser.close()
                if text and len(text) > 500:
                    return text
                raise RuntimeError(f"取得テキストが短すぎます ({len(text)} 文字)")
        except Exception as e:  # noqa: BLE001
            last_err = e
            print(f"[warn] 取得失敗 ({attempt}回目): {url}: {e}", file=sys.stderr)
            time.sleep(10)
    raise RuntimeError(f"ページ取得に失敗しました: {url}: {last_err}")


# ---------------------------------------------------------------- 通知


def send_mail(subject: str, body: str) -> None:
    addr = os.environ["GMAIL_ADDRESS"]
    password = os.environ["GMAIL_APP_PASSWORD"]
    to = os.environ.get("NOTIFY_TO") or addr

    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = addr
    msg["To"] = to
    msg["Date"] = formatdate(localtime=True)

    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as smtp:
        smtp.login(addr, password)
        smtp.send_message(msg)
    print(f"[info] メール送信: {subject} -> {to}")


def format_summary(name: str, url: str, summary: dict) -> str:
    parts = [f"■ {name}", url]
    parts.append(f"在庫切れ表記: {summary['out'] or 'なし'}")
    parts.append(f"購入可能表記: {summary['in'] or 'なし'}")
    if summary["lines"]:
        parts.append("該当箇所の抜粋:")
        parts.extend(f"  {line}" for line in summary["lines"])
    return "\n".join(parts)


# ---------------------------------------------------------------- メイン


def main() -> int:
    prev_state: dict = {}
    if STATE_FILE.exists():
        prev_state = json.loads(STATE_FILE.read_text(encoding="utf-8"))

    first_run = not prev_state
    new_state: dict = {}
    changed_reports: list[str] = []
    restock_names: list[str] = []
    errors: list[str] = []

    for t in TARGETS:
        name, url = t["name"], t["url"]
        try:
            text = fetch_text(url)
        except Exception as e:  # noqa: BLE001
            errors.append(f"{name}: {e}")
            # 取得失敗時は前回状態を維持して誤検知を防ぐ
            if name in prev_state:
                new_state[name] = prev_state[name]
            continue

        cur = summarize(text)
        new_state[name] = cur
        print(f"[info] {name}: out={cur['out']} in={cur['in']}")

        if name in prev_state:
            changed, restock = judge_change(prev_state[name], cur)
            if changed:
                changed_reports.append(format_summary(name, url, cur))
                if restock:
                    restock_names.append(name)

    STATE_FILE.write_text(
        json.dumps(new_state, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )

    if first_run:
        body = "在庫監視の初回実行が完了しました。現在の状態を基準として保存します。\n\n"
        body += "\n\n".join(
            format_summary(t["name"], t["url"], new_state[t["name"]])
            for t in TARGETS if t["name"] in new_state
        )
        if errors:
            body += "\n\n取得エラー:\n" + "\n".join(errors)
        send_mail("[IIJmio在庫監視] セットアップ完了 (初回実行)", body)
    elif changed_reports:
        if restock_names:
            subject = f"[IIJmio在庫監視] 在庫復活の可能性: {', '.join(restock_names)}"
        else:
            subject = "[IIJmio在庫監視] 在庫表記に変化がありました"
        body = "\n\n".join(changed_reports)
        if errors:
            body += "\n\n取得エラー:\n" + "\n".join(errors)
        send_mail(subject, body)
    else:
        print("[info] 変化なし")

    if errors:
        print("[error] " + " / ".join(errors), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
