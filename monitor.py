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
import base64
import json
import os
import re
import sys
from pathlib import Path
from urllib.parse import urljoin
from zoneinfo import ZoneInfo
from email.utils import parsedate_to_datetime

import time

import requests
from bs4 import BeautifulSoup

# 一時的な通信エラー（リトライ対象）
NETWORK_ERRORS = (
    requests.exceptions.Timeout,
    requests.exceptions.ConnectionError,
)

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
OBSERVED_MONTHS = set()
STATUS_FILE = Path("status.json")


class BlockedError(RuntimeError):
    pass


class TemporaryError(RuntimeError):
    def __init__(self, message, retry_after=0):
        super().__init__(message)
        self.retry_after = retry_after


class LinkNotFound(RuntimeError):
    pass


def retry_after_seconds(value):
    if not value:
        return 0
    try:
        return max(0, int(value))
    except ValueError:
        try:
            return max(0, int((parsedate_to_datetime(value) - datetime.datetime.now(datetime.timezone.utc)).total_seconds()))
        except (TypeError, ValueError, OverflowError):
            return 0


def fetch(session: requests.Session, method: str, url: str, **kw) -> requests.Response:
    global LAST_HTML
    # Retry read requests only; never repeatedly submit login/forms after refusal.
    attempts = 3 if method.upper() == "GET" else 1
    for attempt in range(attempts):
        try:
            r = session.request(method, url, timeout=(15, 45), **kw)
            LAST_HTML = r.text
            if r.status_code == 403 or "セキュリティ機能によりページの表示がブロック" in r.text:
                raise BlockedError("Tipness側のアクセス拒否（403）。空き枠は未確認です。")
            if r.status_code == 429:
                raise TemporaryError("Tipness側の利用頻度制限（429）。空き枠は未確認です。", retry_after_seconds(r.headers.get("Retry-After")))
            if r.status_code in (500, 502, 503, 504):
                raise TemporaryError(f"Tipness側の一時障害（HTTP {r.status_code}）。", retry_after_seconds(r.headers.get("Retry-After")))
            r.raise_for_status()
            return r
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError, TemporaryError) as error:
            # Rate limiting and long server-requested pauses wait until a later run.
            if (isinstance(error, TemporaryError) and ("429" in str(error) or error.retry_after > 30)) or attempt == attempts - 1:
                raise
            delay = max(10 * (attempt + 1), getattr(error, "retry_after", 0))
            print(f"一時的な通信不調。{delay}秒待って再確認します（{attempt + 1}/{attempts}）。")
            time.sleep(delay)


def notify(message: str, title: str = "Tipness振替枠") -> None:
    response = requests.post(
        f"https://ntfy.sh/{NTFY_TOPIC}",
        data=message.encode("utf-8"),
        headers={
            "Title": "=?UTF-8?B?" + base64.b64encode(title.encode("utf-8")).decode("ascii") + "?=",
            "Priority": "high",
            "Tags": "swimmer",
        },
        timeout=15,
    )
    response.raise_for_status()


def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {"notified": []}


def save_state(state: dict) -> None:
    temporary = STATE_FILE.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    temporary.replace(STATE_FILE)


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
    raise LinkNotFound(f"『{text}』のリンク/ボタンが見つかりません")


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
    explicit_year = re.search(r"(20\d{2})\s*年", table.get_text())
    if explicit_year:
        year = int(explicit_year.group(1))
    elif month == now.month:
        year = now.year
    elif month == now.month % 12 + 1:
        year = now.year + (1 if now.month == 12 else 0)
    else:
        raise RuntimeError("想定外のカレンダー月です。空き枠として扱いません。")

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
    OBSERVED_MONTHS.add((year, month))
    return dates


def crawl() -> list[datetime.date]:
    """ログイン〜カレンダー解析までを1回実行して日曜の空き日リストを返す"""
    s = make_session()
    r = login(s)

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
            if "振替可能な日程" in r2.text:
                sundays += parse_sunday_slots(soup2)
            else:
                raise RuntimeError("翌月カレンダーを確認できませんでした。")
        except LinkNotFound:
            pass  # 次月リンクが無い月は無視
    return sundays


def write_status(state, status, message, now, **extra):
    health = state.setdefault("health", {})
    report = {
        "checked_at": now.isoformat(), "status": status, "message": message,
        "last_success": health.get("last_success"),
        "next_check_after": health.get("next_check_after"), **extra,
    }
    temp = STATUS_FILE.with_suffix(".tmp")
    temp.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(STATUS_FILE)
    print(json.dumps(report, ensure_ascii=False))
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as handle:
            handle.write("\n### Tipness監視結果\n\n")
            handle.write(f"状態: **{status}** — {message}\n\n")
            handle.write(f"最終確認成功: {report['last_success'] or '未記録'}\n\n")
            if report["next_check_after"]:
                handle.write(f"再確認可能時刻: {report['next_check_after']}\n")
    return report


def main() -> int:
    now = datetime.datetime.now(JST)
    state = load_state()
    health = state.setdefault("health", {})
    # This scheduled pause does not count as an availability check.
    if (now.hour == 23 and now.minute >= 45) or (now.hour == 0 and now.minute < 15):
        write_status(state, "scheduled_pause", "WEB振替のメンテナンス時間（23:45〜0:15）。今回は未確認です。", now)
        return 0
    next_check = health.get("next_check_after")
    if next_check and now < datetime.datetime.fromisoformat(next_check):
        write_status(state, "deferred", "前回の拒否・一時障害を受け、間隔を空けています。今回は未確認です。", now)
        return 0

    OBSERVED_MONTHS.clear()
    try:
        sundays = crawl()
        today = now.date()
        sundays = sorted({d for d in sundays if d >= today})
        print(f"日曜の振替可能日: {[d.isoformat() for d in sundays]}")
        notified = set(state.get("notified", []))
        new_slots = [d for d in sundays if d.isoformat() not in notified]
        if new_slots:
            lines = [f"{d.month}/{d.day}(日)" for d in new_slots]
            notify("日曜日に振替枠が出ました: " + "、".join(lines) + f"\n今すぐ予約→ {MEMBER_URL}")
            notified.update(d.isoformat() for d in new_slots)
        current = {d.isoformat() for d in sundays}
        # Preserve records for months that were not observed (e.g. no next-month link).
        state["notified"] = sorted(
            n for n in notified
            if datetime.date.fromisoformat(n) >= today
            and (n in current or (datetime.date.fromisoformat(n).year, datetime.date.fromisoformat(n).month) not in OBSERVED_MONTHS)
        )
        state["health"] = {"last_success": now.isoformat(), "consecutive_failures": 0}
        save_state(state)
        write_status(state, "ok", "カレンダーの確認と必要な通知処理が完了しました。", now, slot_count=len(sundays))
        return 0
    except Exception as error:
        # Do not persist exception URLs: notification URLs can contain the secret topic.
        if isinstance(error, BlockedError):
            category, message = "blocked", "Tipnessがアクセスを拒否しました（403）。空き枠は未確認です。"
        elif isinstance(error, TemporaryError):
            category, message = "temporary", str(error)
        elif isinstance(error, NETWORK_ERRORS):
            category, message = "network", "通信失敗。空き枠または通知送信の完了を確認できませんでした。"
        elif isinstance(error, requests.exceptions.HTTPError):
            code = error.response.status_code if error.response is not None else "unknown"
            category, message = "http_error", f"HTTP {code}。処理を完了できませんでした。"
        elif isinstance(error, RuntimeError):
            category, message = "page_error", str(error)
        else:
            category, message = "error", f"処理失敗（{type(error).__name__}）。確認が必要です。"
        failures = health.get("consecutive_failures", 0) + 1
        health.update(consecutive_failures=failures, last_failure=now.isoformat(), category=category)
        # 403 is never retried immediately. Give the service a progressively longer rest.
        if category == "blocked":
            pause = min(6 * 3600, 3600 * 2 ** min(failures - 1, 3))
            health["next_check_after"] = (now + datetime.timedelta(seconds=pause)).isoformat()
        elif category == "temporary":
            pause = max(600, getattr(error, "retry_after", 0))
            health["next_check_after"] = (now + datetime.timedelta(seconds=pause)).isoformat()
        else:
            health.pop("next_check_after", None)
        # Notification failure must never advance the notified list.
        save_state(state)
        write_status(state, category, message, now)
        if os.environ.get("GITHUB_ACTIONS") == "true":
            print(f"::error::{message}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
