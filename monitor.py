"""
iTIPNESS KIDSスイミング 振替枠監視スクリプト

フロー:
  1. キッズ保護者用ログイン (メールアドレス + パスワード)
  2. 会員選択画面で「選択する」をクリック
  3. 「振替カレンダーへ」をクリック
  4. 「振替枠確認（通常練習日）」タブを開く
  5. カレンダー上でリンクになっている日付 = 振替可能日
  6. そのうち日曜日のものがあれば ntfy.sh 経由で iPhone に通知
  7. 通知済みの日付は state.json に記録し、同じ枠で連続通知しない

必要な環境変数:
  TIPNESS_EMAIL    : キッズ保護者用ログインのメールアドレス
  TIPNESS_PASSWORD : パスワード
  NTFY_TOPIC       : ntfy.sh のトピック名（推測されにくいランダムな文字列に）
"""

import json
import os
import re
import sys
import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

LOGIN_URL = "https://i.tipness.co.jp/i/auth/login"
STATE_FILE = Path("state.json")
JST = ZoneInfo("Asia/Tokyo")
TARGET_WEEKDAY = 6  # 月=0 ... 日=6（日曜日を監視）
CHECK_NEXT_MONTH = True  # 「次月」も確認するか

EMAIL = os.environ["TIPNESS_EMAIL"]
PASSWORD = os.environ["TIPNESS_PASSWORD"]
NTFY_TOPIC = os.environ["NTFY_TOPIC"]


def notify(message: str, title: str = "Tipness振替枠") -> None:
    """ntfy.sh にプッシュ通知を送る"""
    requests.post(
        f"https://ntfy.sh/{NTFY_TOPIC}",
        data=message.encode("utf-8"),
        headers={
            "Title": title.encode("utf-8"),
            "Priority": "high",
            "Tags": "swimmer",
        },
        timeout=15,
    )


def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {"notified": []}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def login(page) -> None:
    """キッズ保護者用フォームでログインする"""
    page.goto(LOGIN_URL, wait_until="domcontentloaded")

    # ページ内の form のうち「キッズ」の文言を含み、
    # パスワード入力欄を持つものをキッズ保護者用フォームとみなす
    kids_form = None
    forms = page.locator("form")
    for i in range(forms.count()):
        f = forms.nth(i)
        try:
            text = f.inner_text(timeout=2000)
        except PWTimeout:
            continue
        if "キッズ" in text and f.locator("input[type=password]").count() > 0:
            kids_form = f
            break

    if kids_form is None:
        # 見つからない場合はパスワード欄を持つ最後のフォーム
        # （ページ構成上、キッズ用は3番目＝最後）
        candidates = [
            forms.nth(i)
            for i in range(forms.count())
            if forms.nth(i).locator("input[type=password]").count() > 0
        ]
        if not candidates:
            raise RuntimeError("ログインフォームが見つかりませんでした")
        kids_form = candidates[-1]

    # メールアドレス欄: email型優先、なければpassword以外のtext系入力
    email_input = kids_form.locator("input[type=email]")
    if email_input.count() == 0:
        email_input = kids_form.locator(
            "input:not([type=password]):not([type=hidden])"
            ":not([type=checkbox]):not([type=submit]):not([type=button])"
        )
    email_input.first.fill(EMAIL)
    kids_form.locator("input[type=password]").first.fill(PASSWORD)

    # 送信ボタン（「ログイン」ボタン or submit）
    submit = kids_form.locator(
        "button[type=submit], input[type=submit], button:has-text('ログイン')"
    )
    if submit.count() > 0:
        submit.first.click()
    else:
        kids_form.locator("input[type=password]").first.press("Enter")

    page.wait_for_load_state("domcontentloaded")


def goto_calendar(page) -> None:
    """会員選択 →「振替カレンダーへ」→「振替枠確認（通常練習日）」"""
    # 会員選択画面（会員が1人なら最初の「選択する」）
    select_btn = page.locator("a:has-text('選択する'), button:has-text('選択する')")
    select_btn.first.click()
    page.wait_for_load_state("domcontentloaded")

    # 振替カレンダーへ
    page.locator(
        "a:has-text('振替カレンダーへ'), button:has-text('振替カレンダーへ')"
    ).first.click()
    page.wait_for_load_state("domcontentloaded")

    # 「振替枠確認（通常練習日）」タブを念のためクリック
    tab = page.locator("text=振替枠確認").filter(has_text="通常練習日")
    if tab.count() > 0:
        try:
            tab.first.click(timeout=3000)
            page.wait_for_load_state("domcontentloaded")
        except PWTimeout:
            pass  # 既に選択済みならクリック不要


def find_calendar_table(page):
    """曜日ヘッダー（月火水...日）を含むカレンダーのtableを返す"""
    tables = page.locator("table")
    for i in range(tables.count()):
        t = tables.nth(i)
        try:
            text = t.inner_text(timeout=2000)
        except PWTimeout:
            continue
        if all(w in text for w in ["月", "火", "水", "木", "金", "土", "日"]):
            return t
    return None


def extract_open_dates(page) -> list[datetime.date]:
    """カレンダー上のリンク（=振替可能日）を date のリストで返す"""
    table = find_calendar_table(page)
    if table is None:
        raise RuntimeError("カレンダーのテーブルが見つかりませんでした")

    # 表示中の月を取得（例: 「7月」）
    m = re.search(r"(\d{1,2})月", table.inner_text())
    if not m:
        raise RuntimeError("カレンダーの月を特定できませんでした")
    month = int(m.group(1))

    # 年の推定: 表示月が現在月より大幅に前なら翌年とみなす
    now = datetime.datetime.now(JST)
    year = now.year
    if month < now.month - 1:
        year += 1

    dates = []
    links = table.locator("a")
    for i in range(links.count()):
        text = links.nth(i).inner_text().strip()
        if re.fullmatch(r"\d{1,2}", text):
            try:
                dates.append(datetime.date(year, month, int(text)))
            except ValueError:
                pass
    return dates


def go_next_month(page) -> bool:
    """「次月」リンクがあればクリックして True を返す"""
    nxt = page.locator("a:has-text('次月')")
    if nxt.count() > 0:
        nxt.first.click()
        page.wait_for_load_state("domcontentloaded")
        return True
    return False


def main() -> int:
    state = load_state()
    notified = set(state.get("notified", []))

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        try:
            login(page)
            goto_calendar(page)

            open_dates = extract_open_dates(page)
            if CHECK_NEXT_MONTH and go_next_month(page):
                open_dates += extract_open_dates(page)
        except Exception:
            # デバッグ用にスクリーンショットを保存（Actionsのartifactで確認可能）
            page.screenshot(path="error.png", full_page=True)
            browser.close()
            raise
        browser.close()

    today = datetime.datetime.now(JST).date()
    sundays = sorted(
        d for d in open_dates
        if d.weekday() == TARGET_WEEKDAY and d >= today
    )
    print(f"振替可能日: {sorted(open_dates)}")
    print(f"うち日曜日: {sundays}")

    new_slots = [d for d in sundays if d.isoformat() not in notified]
    if new_slots:
        lines = [d.strftime("%-m/%-d(日)") for d in new_slots]
        notify("日曜日に振替枠が出ました: " + "、".join(lines) + "\n今すぐ予約→ https://i.tipness.co.jp/i/user/")
        notified.update(d.isoformat() for d in new_slots)

    # 過去日と、リンクが消えた（=埋まった）日をstateから掃除
    # （埋まった日を消しておくと、再度空きが出たときにまた通知される）
    current = {d.isoformat() for d in sundays}
    notified = {s for s in notified if s in current}

    state["notified"] = sorted(notified)
    save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
