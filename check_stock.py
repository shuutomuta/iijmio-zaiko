#!/usr/bin/env python3
"""IIJmio 端末一覧ページの在庫監視スクリプト。

https://www.iijmio.jp/device/ を Playwright で描画し、対象商品の
カード(詳細ページへのリンクを含む区画)のテキストを取り出して、
「一時在庫切れ」「完売しました」等の表記の有無を判定する。
表記が消えたら「在庫復活の可能性」としてメール通知する。

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

LIST_URL = "https://www.iijmio.jp/device/"

# 監視対象。key は一覧ページ内の詳細リンク (detail.html?key=...) の識別子
TARGETS = [
    {
        "name": "POCO X8 Pro (8GB/256GB)",
        "key": "POCO_X8_Pro_8GB_256GB",
        "detail_url": "https://www.iijmio.jp/device/detail.html?key=POCO_X8_Pro_8GB_256GB",
    },
    {
        "name": "POCO F7 Pro (12GB/256GB)",
        "key": "POCO_F7_Pro_12GB_256GB",
        "detail_url": "https://www.iijmio.jp/device/detail.html?key=POCO_F7_Pro_12GB_256GB",
    },
]

# 在庫切れを示す表記。カードのテキストにこれらが1つも無ければ「購入可能」と判定する
OUT_MARKERS = [
    "一時在庫切れ",
    "完売しました",
    "在庫切れ",
    "次回入荷未定",
    "入荷未定",
    "販売を終了",
    "販売終了",
]

STATE_FILE = Path("state.json")
STATE_VERSION = 2  # 監視方式を変えたら上げる(旧stateを破棄して基準を取り直す)
PAGE_TIMEOUT_MS = 60_000
RETRY = 2

STATUS_LABEL = {
    "IN": "購入可能(在庫切れ表記なし)",
    "OUT": "在庫切れ",
    "NOT_FOUND": "一覧ページに見つかりません",
}

# 一覧ページから key に対応するカードのテキストを取り出す JavaScript。
# 詳細リンク(a[href*=key])を起点に親要素を6階層までさかのぼり、
# 在庫表記を含みうる適度な大きさ(<=1200文字)の区画テキストを返す。
JS_EXTRACT_CARD = """
(key) => {
  const anchors = Array.from(document.querySelectorAll('a[href*="' + key + '"]'));
  if (anchors.length === 0) return null;
  let best = null;
  for (const a of anchors) {
    let el = a;
    let candidate = (a.innerText || "").trim();
    for (let i = 0; i < 6 && el.parentElement; i++) {
      el = el.parentElement;
      const t = (el.innerText || "").trim();
      if (t.length > 1200) break;
      if (t.length >= 10) candidate = t;
    }
    if (!best || candidate.length > best.length) best = candidate;
  }
  return best;
}
"""


# ---------------------------------------------------------------- 判定ロジック


def judge_status(card_text: str | None) -> dict:
    """カードのテキストから状態を判定する。"""
    if card_text is None:
        return {"status": "NOT_FOUND", "markers": [], "text": ""}
    found = [m for m in OUT_MARKERS if m in card_text]
    status = "OUT" if found else "IN"
    return {"status": status, "markers": found, "text": card_text[:600]}


def is_restock(prev: dict, cur: dict) -> bool:
    """在庫復活(在庫切れ表記が消えて購入可能になった)か。"""
    return prev.get("status") == "OUT" and cur.get("status") == "IN"


def has_changed(prev: dict, cur: dict) -> bool:
    """通知すべき変化か。表記の増減や消滅・検出不能への遷移も含む。"""
    return (
        prev.get("status") != cur.get("status")
        or sorted(prev.get("markers", [])) != sorted(cur.get("markers", []))
    )


# ---------------------------------------------------------------- 取得


def fetch_cards() -> dict[str, str | None]:
    """一覧ページを1回描画し、対象ごとのカードテキストを返す。"""
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
                page.goto(LIST_URL, timeout=PAGE_TIMEOUT_MS, wait_until="load")
                page.wait_for_timeout(8_000)  # JS描画と商品一覧の読み込みを待つ
                body_len = len(page.inner_text("body"))
                if body_len < 500:
                    raise RuntimeError(f"ページ本文が短すぎます ({body_len} 文字)")
                cards: dict[str, str | None] = {}
                for t in TARGETS:
                    cards[t["key"]] = page.evaluate(JS_EXTRACT_CARD, t["key"])
                browser.close()
                return cards
        except Exception as e:  # noqa: BLE001
            last_err = e
            print(f"[warn] 取得失敗 ({attempt}回目): {e}", file=sys.stderr)
            time.sleep(10)
    raise RuntimeError(f"一覧ページの取得に失敗しました: {last_err}")


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


def format_entry(target: dict, result: dict) -> str:
    parts = [f"■ {target['name']}"]
    parts.append(f"状態: {STATUS_LABEL[result['status']]}")
    if result["markers"]:
        parts.append(f"検出した表記: {', '.join(result['markers'])}")
    parts.append(f"詳細ページ: {target['detail_url']}")
    parts.append(f"一覧ページ: {LIST_URL}")
    if result["text"]:
        parts.append("カードの抜粋:")
        for line in result["text"].splitlines():
            line = line.strip()
            if line:
                parts.append(f"  {line}")
    return "\n".join(parts)


# ---------------------------------------------------------------- メイン


def load_state() -> dict:
    if not STATE_FILE.exists():
        return {}
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    if data.get("version") != STATE_VERSION:
        return {}  # 旧形式は破棄して基準を取り直す
    return data


def main() -> int:
    prev_state = load_state()
    prev_items: dict = prev_state.get("items", {})
    first_run = not prev_items

    try:
        cards = fetch_cards()
    except Exception as e:  # noqa: BLE001
        print(f"[error] {e}", file=sys.stderr)
        return 1  # 前回状態は維持。ジョブ失敗によりGitHubから失敗通知が届く

    new_items: dict = {}
    changed_entries: list[str] = []
    restock_names: list[str] = []

    for t in TARGETS:
        cur = judge_status(cards.get(t["key"]))
        new_items[t["key"]] = cur
        print(f"[info] {t['name']}: {cur['status']} markers={cur['markers']}")

        prev = prev_items.get(t["key"])
        if prev is not None and has_changed(prev, cur):
            changed_entries.append(format_entry(t, cur))
            if is_restock(prev, cur):
                restock_names.append(t["name"])

    STATE_FILE.write_text(
        json.dumps(
            {"version": STATE_VERSION, "items": new_items},
            ensure_ascii=False, indent=2, sort_keys=True,
        ),
        encoding="utf-8",
    )

    if first_run:
        body = (
            "在庫監視(一覧ページ方式)の初回実行が完了しました。"
            "現在の状態を基準として保存します。\n\n"
        )
        body += "\n\n".join(
            format_entry(t, new_items[t["key"]]) for t in TARGETS
        )
        send_mail("[IIJmio在庫監視] セットアップ完了 (一覧ページ方式)", body)
    elif changed_entries:
        if restock_names:
            subject = f"[IIJmio在庫監視] 在庫復活の可能性: {', '.join(restock_names)}"
        else:
            subject = "[IIJmio在庫監視] 在庫表記に変化がありました"
        send_mail(subject, "\n\n".join(changed_entries))
    else:
        print("[info] 変化なし")

    return 0


if __name__ == "__main__":
    sys.exit(main())
