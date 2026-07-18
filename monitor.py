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
MEMBER_SELECT_URL = "https://i.tipness.co.jp/i/user/"  # WEB振替の会員選択ページ
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
    page.wait_for_timeout(1500)

    # このサイトはPC用/スマホ用でログイン欄がHTML内に重複している可能性がある。
    # そこでDOM順ではなく「画面上の見た目の位置」を使う:
    # 緑の見出し「キッズ保護者様でログイン」の下にある"表示中の"入力欄・ボタンを
    # Playwrightのレイアウトセレクタ(:below)で特定し、人間と同じ操作で入力する。
    A = ":text('キッズ保護者様でログイン')"

    def first_visible(selector: str):
        loc = page.locator(selector)
        for i in range(loc.count()):
            el = loc.nth(i)
            try:
                if el.is_visible():
                    return el
            except Exception:
                continue
        return None

    email_in = (
        first_visible(f"input[type=text]:below({A})")
        or first_visible(f"input[type=email]:below({A})")
    )
    pw_in = first_visible(f"input[type=password]:below({A})")
    login_btn = first_visible(f":text-is('ログイン'):below({A})")

    missing = [
        name for name, el in [
            ("メール欄", email_in), ("パスワード欄", pw_in), ("ログインボタン", login_btn)
        ] if el is None
    ]
    if missing:
        raise RuntimeError(f"キッズ保護者欄の要素が見つかりません: {', '.join(missing)}")

    email_in.click()
    email_in.fill(EMAIL)
    pw_in.click()
    pw_in.fill(PASSWORD)
    login_btn.click()

    page.wait_for_load_state("domcontentloaded")
    page.wait_for_timeout(2000)

    # ログイン失敗の検知
    content = page.content()
    if "パスワードが違います" in content:
        raise RuntimeError(
            "サイト側が『ログインID、またはパスワードが違います』と返しました。"
            "Secretsの値と手動ログインで使った値が完全に一致しているか確認してください。"
        )
    if "auth/login" in page.url or (
        page.locator("input[type=password]").count() > 0 and "キッズ" in content
    ):
        raise RuntimeError(
            "ログインに失敗した可能性があります。"
            "TIPNESS_EMAIL / TIPNESS_PASSWORD のSecretsを確認してください。"
        )


def click_by_text(page, text: str) -> None:
    """a / button / input いずれのタグでもテキスト一致でクリックする"""
    loc = page.locator(
        f"a:has-text('{text}'), button:has-text('{text}'), "
        f"input[type=submit][value*='{text}'], input[type=button][value*='{text}']"
    )
    if loc.count() == 0:
        loc = page.get_by_text(text, exact=False)
    loc.first.click()
    page.wait_for_load_state("domcontentloaded")


def goto_calendar(page) -> None:
    """会員選択 →「振替カレンダーへ」→「振替枠確認（通常練習日）」"""
    # ログイン後の遷移先がWEB振替とは限らないため、会員選択ページへ明示的に移動
    if "選択する" not in page.content():
        page.goto(MEMBER_SELECT_URL, wait_until="domcontentloaded")
        page.wait_for_timeout(1000)

    # 会員選択画面（会員が1人なら最初の「選択する」）
    click_by_text(page, "選択する")

    # 振替カレンダーへ
    click_by_text(page, "振替カレンダーへ")

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
