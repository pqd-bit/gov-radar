#!/usr/bin/env python3
"""
마감 D-N 이내 '우선 관심' 공고만 모아 이메일로 발송한다.
GitHub Actions Secrets 필요:
  SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASS  (Gmail이면 앱 비밀번호 사용)
  NOTIFY_TO   : 받을 이메일 주소
  NOTIFY_DAYS : 알림 기준일 수 (기본 14 -> 마감 14일 전부터 매일 알림)
"""
import json
import os
import smtplib
import sys
from datetime import date, datetime
from email.mime.text import MIMEText
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_PATH = ROOT / "docs" / "data" / "programs.json"

NOTIFY_DAYS = int(os.environ.get("NOTIFY_DAYS", "14"))


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


def build_body(upcoming):
    lines = [f"오늘 기준 D-{NOTIFY_DAYS} 이내 마감 예정 우선 관심 공고 ({len(upcoming)}건)\n"]
    for it in upcoming:
        dleft = days_left(it["end_date"])
        lines.append(
            f"- [D-{dleft}] {it['title']}\n"
            f"  기관: {it['org']} | 마감: {it['end_date']} | {it['url']}"
        )
    return "\n".join(lines)


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

    msg = MIMEText(body, _charset="utf-8")
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
