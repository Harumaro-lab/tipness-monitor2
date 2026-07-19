"""
iTIPNESS KIDSスイミング 振替枠監視スクリプト（requests版）

ブラウザ自動操作(Playwright)は使わず、通常のブラウザと同じHTTP通信を直接行う。
ログインは login_id / login_pass の単純なPOST（キッズ用・メール用で共通）。

フロー:
  1. ログインページをGET → login_id/login_pass をPOST
  2. 会員選択ページで「選択する」のリンク/フォームをたどる
  3. 「振替カレンダーへ」をたどる（必要なら「通常練習日」タブも）
  4. カレンダー表の「日」列でリンクになっている日付 = 日曜の振替可能枠
  5. 新しい枠があれば ntfy.sh 経由で iPhone に通知（state.json で重複通知防止）

必要な環境変数: TIPNESS_EMAIL / TIPNESS_PASSWORD / NTFY_TOPIC
"""

import datetime
import json
import os
import re
import sys
from pathlib import Path
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

BASE = "https://i.tipness.co.jp"
LOGIN_URL = f"{BASE}/i/auth/login"
MEMBER_URL = f"{BASE}/i/kf2/"  # 振替予約（会員選択）ページ
STATE_FILE = Path("state.json")
DEBUG_FILE = Path("error.html")
JST = ZoneInfo("Asia/Tokyo")
CHECK_NEXT_MONTH = True

EMAIL = os.environ["TIPNESS_EMAIL"]
PASSWORD = os.environ["TIPNESS_PASSWORD"]
NTFY_TOPIC = os.environ["NTFY_TOPIC"]

# 通常のPCブラウザと同等のヘッダ
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
}

LAST_HTML = ""  # デバッグ用: 最後に受信したHTML


def fetch(session: requests.Session, method: str, url: str, **kw) -> requests.Response:
    global LAST_HTML
    r = session.request(method, url, timeout=30, **kw)
    LAST_HTML = r.text
    return r


def notify(message: str, title: str = "Tipness振替枠") -> None:
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


def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update(HEADERS)
    return s


def login(s: requests.Session) -> requests.Response:
    fetch(s, "GET", LOGIN_URL)  # セッションCookie取得
    r = fetch(
        s,
        "POST",
        LOGIN_URL,
        data={"login_id": EMAIL, "login_pass": PASSWORD},
        headers={"Referer": LOGIN_URL, "Origin": BASE},
        allow_redirects=True,
    )
    if "パスワードが違います" in r.text:
        raise RuntimeError(
            "サイトが『ログインID、またはパスワードが違います』と返しました。"
        )
    if "ログイン方法をお選びください" in r.text:
        raise RuntimeError("ログイン後もログイン画面のままです（詳細はerror.html参照）")
    return r


def follow(s: requests.Session, current_url: str, soup: BeautifulSoup, text: str):
    """『text』と書かれたリンクまたはボタンをたどり、(response, soup, url) を返す"""
    # 1) リンク
    for a in soup.find_all("a"):
        if text in a.get_text(strip=True):
            href = a.get("href") or ""
            if href and not href.startswith(("javascript:", "#")):
                r = fetch(s, "GET", urljoin(current_url, href),
                          headers={"Referer": current_url})
                return r, BeautifulSoup(r.text, "html.parser"), r.url
    # 2) フォーム内のボタン
    for inp in soup.find_all(["input", "button"]):
        label = (inp.get("value") or "") + inp.get_text(strip=True)
        if text not in label:
            continue
        form = inp.find_parent("form")
        if form is None:
            continue
        data = {}
        for f in form.find_all(["input", "select", "textarea"]):
            name = f.get("name")
            if not name:
                continue
            ftype = (f.get("type") or "text").lower()
            if ftype in ("checkbox", "radio") and not f.has_attr("checked"):
                continue
            if ftype in ("submit", "button", "image"):
                if f is inp:
                    data[name] = f.get("value", "")
                continue
            data[name] = f.get("value", "")
        # onclick="this.form.action='calendar/';..." 形式で送信先を
        # 切り替えるボタンに対応
        action_attr = form.get("action") or ""
        onclick = inp.get("onclick") or ""
        m = re.search(r"action\s*=\s*['\"]([^'\"]+)['\"]", onclick)
        if m:
            action_attr = m.group(1)
        action = urljoin(current_url, action_attr or current_url)
        method = (form.get("method") or "get").lower()
        if method == "post":
            r = fetch(s, "POST", action, data=data,
                      headers={"Referer": current_url, "Origin": BASE})
        else:
            r = fetch(s, "GET", action, params=data,
                      headers={"Referer": current_url})
        return r, BeautifulSoup(r.text, "html.parser"), r.url
    raise RuntimeError(f"『{text}』のリンク/ボタンが見つかりません（error.html参照）")


WEEKDAY_HEADERS = ["月", "火", "水", "木", "金", "土", "日"]


def find_calendar_table(soup: BeautifulSoup):
    for table in soup.find_all("table"):
        text = table.get_text()
        if all(w in text for w in WEEKDAY_HEADERS):
            return table
    return None


def parse_sunday_slots(soup: BeautifulSoup) -> list[datetime.date]:
    """カレンダー表の「日」列にあるリンク日付を date のリストで返す"""
    table = find_calendar_table(soup)
    if table is None:
        raise RuntimeError("カレンダー表が見つかりません（error.html参照）")

    # 表示中の月
    m = re.search(r"(\d{1,2})\s*月", table.get_text())
    if not m:
        raise RuntimeError("カレンダーの月を特定できません")
    month = int(m.group(1))
    now = datetime.datetime.now(JST)
    year = now.year + (1 if month < now.month - 1 else 0)

    rows = table.find_all("tr")
    # 曜日ヘッダー行から「日」列の位置を特定
    sunday_col = None
    for row in rows:
        cells = row.find_all(["td", "th"])
        texts = [c.get_text(strip=True) for c in cells]
        if "日" in texts and "土" in texts and "月" in texts:
            sunday_col = texts.index("日")
            header_row = row
            break
    if sunday_col is None:
        raise RuntimeError("曜日ヘッダー行が見つかりません")

    dates = []
    for row in rows:
        if row is header_row:
            continue
        cells = row.find_all(["td", "th"])
        if len(cells) <= sunday_col:
            continue
        cell = cells[sunday_col]
        link = cell.find("a")
        if link:
            day_text = link.get_text(strip=True)
            if re.fullmatch(r"\d{1,2}", day_text):
                try:
                    dates.append(datetime.date(year, month, int(day_text)))
                except ValueError:
                    pass
    return dates


def main() -> int:
    state = load_state()
    notified = set(state.get("notified", []))

    s = make_session()
    try:
        r = login(s)
        soup = BeautifulSoup(r.text, "html.parser")
        url = r.url

        # ログイン後はキッズ会員トップに着地するため、振替予約ページへ明示的に移動
        r = fetch(s, "GET", MEMBER_URL)
        soup, url = BeautifulSoup(r.text, "html.parser"), r.url

        r, soup, url = follow(s, url, soup, "選択する")
        r, soup, url = follow(s, url, soup, "振替カレンダーへ")

        # カレンダーは「受講予定」タブが初期表示。必ず「振替枠確認（通常練習日）」
        # タブへ切り替える（受講予定のリンクを空き枠と誤検知しないため）
        r, soup, url = follow(s, url, soup, "通常練習日")
        if "振替可能な日程" not in r.text:
            raise RuntimeError(
                "『振替枠確認（通常練習日）』タブに切り替わっていません（error.html参照）"
            )

        sundays = parse_sunday_slots(soup)

        if CHECK_NEXT_MONTH:
            try:
                r2, soup2, url2 = follow(s, url, soup, "次月")
                # 次月ページも振替枠確認ビューであることを確認できた場合のみ採用
                if "振替可能な日程" in r2.text:
                    sundays += parse_sunday_slots(soup2)
            except RuntimeError:
                pass  # 次月リンクが無い月は無視
    except Exception:
        DEBUG_FILE.write_text(LAST_HTML, encoding="utf-8")
        raise

    today = datetime.datetime.now(JST).date()
    sundays = sorted({d for d in sundays if d >= today})
    print(f"日曜の振替可能日: {[d.isoformat() for d in sundays]}")

    new_slots = [d for d in sundays if d.isoformat() not in notified]
    if new_slots:
        lines = [f"{d.month}/{d.day}(日)" for d in new_slots]
        notify(
            "日曜日に振替枠が出ました: " + "、".join(lines)
            + f"\n今すぐ予約→ {MEMBER_URL}"
        )
        notified.update(d.isoformat() for d in new_slots)

    # 埋まった枠・過去の枠はstateから除去（再度空いたら改めて通知される）
    current = {d.isoformat() for d in sundays}
    state["notified"] = sorted(n for n in notified if n in current)
    save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
