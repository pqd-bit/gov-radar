#!/usr/bin/env python3
"""
마감 D-N 이내 '우선 관심' 공고만 모아 이메일로 발송한다.
각 항목에는 "숨기기" 링크가 포함되며, 클릭 시 GitHub 이슈가 생성되고
.github/workflows/dismiss.yml 이 이를 감지해 dismissed.json 에 등록한다
(이후 수집부터 해당 공고는 영구 제외됨).

GitHub Actions Secrets 필요:
  SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASS  (Gmail이면 앱 비밀번호 사용)
  NOTIFY_TO   : 받을 이메일 주소
  NOTIFY_DAYS : 알림 기준일 수 (기본 14 -> 마감 14일 전부터 매일 알림)
"""
import html
import json
import os
import smtplib
import sys
from datetime import date, datetime
from email.mime.text import MIMEText
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parent.parent
DATA_PATH = ROOT / "docs" / "data" / "programs.json"

NOTIFY_DAYS = int(os.environ.get("NOTIFY_DAYS", "14"))
# GitHub Actions가 자동으로 "owner/repo" 형태로 주입해주는 환경변수 (별도 설정 불필요)
GITHUB_REPO = os.environ.get("GITHUB_REPOSITORY", "")


def load_items():
    if not DATA_PATH.exists():
        return []
    return json.loads(DATA_PATH.read_text(encoding="utf-8")).get("items", [])


def days_left(end_date_str):
    if not end_date_str:
        return None
    try:
        d = datetime.strptime(end_date_str, "%Y-%m-%d").date()
    except ValueError:
        return None
    return (d - date.today()).days


def dismiss_url(item_id: str):
    if not GITHUB_REPO:
        return ""
    title = quote(f"dismiss:{item_id}")
    return f"https://github.com/{GITHUB_REPO}/issues/new?title={title}&labels=dismiss"


def _card_html(it):
    dleft = days_left(it["end_date"])
    title = html.escape(it["title"])
    org = html.escape(it["org"])
    end_date = html.escape(it["end_date"])
    view_href = html.escape(it["url"], quote=True)
    dismiss_href = html.escape(dismiss_url(it["id"]), quote=True)

    return f"""
<div style="border:1px solid #e0e0e0;border-radius:8px;padding:16px;margin-bottom:12px;">
  <div style="font-size:15px;font-weight:bold;color:#111111;margin-bottom:6px;">[D-{dleft}] {title}</div>
  <div style="font-size:13px;color:#767676;margin-bottom:12px;">{org} · 마감 {end_date}</div>
  <div>
    <a href="{view_href}" style="display:inline-block;background-color:#2563eb;color:#ffffff;text-decoration:none;font-size:13px;padding:6px 12px;border-radius:6px;margin-right:8px;">공고 보기 →</a>
    <a href="{dismiss_href}" style="display:inline-block;background-color:#f3f4f6;color:#b91c1c;text-decoration:none;font-size:13px;padding:6px 12px;border-radius:6px;">그만보기 ✕</a>
  </div>
</div>"""


def build_body(upcoming):
    header = (
        f'<p style="font-size:15px;color:#111111;margin:0 0 16px;">'
        f"오늘 기준 D-{NOTIFY_DAYS} 이내 마감 예정 우선 관심 공고 ({len(upcoming)}건)"
        f"</p>"
    )
    cards = "".join(_card_html(it) for it in upcoming)
    return (
        '<div style="font-family:-apple-system,Helvetica,Arial,sans-serif;'
        'max-width:600px;margin:0 auto;">'
        f"{header}{cards}"
        "</div>"
    )


def main():
    items = load_items()
    upcoming = [
        it for it in items
        if it.get("is_priority")
        and it.get("end_date")
        and 0 <= (days_left(it["end_date"]) or -1) <= NOTIFY_DAYS
    ]
    upcoming.sort(key=lambda x: days_left(x["end_date"]))

    if not upcoming:
        print("[INFO] 알림 대상 없음 (우선 관심 공고 중 마감임박 건 없음)")
        return

    body = build_body(upcoming)
    subject = f"[Gov Radar] 우선 관심 마감임박 공고 {len(upcoming)}건 (D-{NOTIFY_DAYS} 이내)"

    host = os.environ.get("SMTP_HOST")
    port = int(os.environ.get("SMTP_PORT", "587"))
    user = os.environ.get("SMTP_USER")
    pw = os.environ.get("SMTP_PASS")
    to_addr = os.environ.get("NOTIFY_TO")

    if not all([host, user, pw, to_addr]):
        print("[WARN] SMTP 환경변수 미설정 - 콘솔 출력만 수행")
        print(body)
        return

    msg = MIMEText(body, "html", _charset="utf-8")
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = to_addr

    with smtplib.SMTP(host, port) as server:
        server.starttls()
        server.login(user, pw)
        server.sendmail(user, [to_addr], msg.as_string())

    print(f"[OK] 알림 메일 발송 완료 -> {to_addr}")


if __name__ == "__main__":
    main()
