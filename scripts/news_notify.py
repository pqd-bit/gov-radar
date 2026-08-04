#!/usr/bin/env python3
"""
F&B 건강기능식품/웰니스 트렌드 + 국제무역 기회 뉴스 중 아직 발송하지 않은
기사를 골라 이메일로 보낸다.

news.json의 관련도 판정(category, keyword_score)은 news_collector.py의
is_relevant() 단일 함수 결과를 그대로 신뢰하며, 이 스크립트에서 키워드를
다시 매칭하지 않는다 - 상위 N건 선정은 news_collector.py가 이미 계산해 둔
keyword_score로만 정렬한다.

기존 정부지원사업 notify.py와는 완전히 독립된 별도 스크립트다.

GitHub Actions Secrets 필요 (collect.yml/notify.py와 동일한 값 재사용):
  SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASS
  NOTIFY_TO
"""
import html
import json
import os
import smtplib
from datetime import date, timedelta
from email.mime.text import MIMEText
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_PATH = ROOT / "docs" / "data" / "news.json"
SENT_PATH = ROOT / "docs" / "data" / "news_sent.json"

MAX_PER_DAY = 10
SENT_RETENTION_DAYS = 14

CATEGORY_LABELS = {
    "wellness_trend": "웰니스 트렌드",
    "trade_opportunity": "무역 기회",
}
CATEGORY_ORDER = ["wellness_trend", "trade_opportunity"]


def load_news():
    if not DATA_PATH.exists():
        return []
    return json.loads(DATA_PATH.read_text(encoding="utf-8")).get("items", [])


def load_sent():
    if not SENT_PATH.exists():
        return []
    try:
        return json.loads(SENT_PATH.read_text(encoding="utf-8")).get("sent", [])
    except Exception:
        return []


def prune_sent(sent_records):
    """14일 지난 발송 기록은 자동 정리한다."""
    cutoff = (date.today() - timedelta(days=SENT_RETENTION_DAYS)).isoformat()
    return [r for r in sent_records if r.get("sent_at", "0000-00-00") >= cutoff]


def _card_html(it):
    title = html.escape(it["title"])
    source = html.escape(it.get("source") or "")
    published = html.escape(it.get("published_date") or "날짜 미상")
    view_href = html.escape(it["url"], quote=True)

    return f"""
<div style="border:1px solid #e0e0e0;border-radius:8px;padding:16px;margin-bottom:12px;">
  <div style="font-size:15px;font-weight:bold;color:#111111;margin-bottom:6px;">{title}</div>
  <div style="font-size:13px;color:#767676;margin-bottom:12px;">{source} · {published}</div>
  <div>
    <a href="{view_href}" style="display:inline-block;background-color:#2563eb;color:#ffffff;text-decoration:none;font-size:13px;padding:6px 12px;border-radius:6px;">기사 보기 →</a>
  </div>
</div>"""


def _section_html(label, items):
    if not items:
        return ""
    cards = "".join(_card_html(it) for it in items)
    return (
        f'<div style="font-size:14px;font-weight:bold;color:#111111;'
        f'margin:20px 0 10px;">{html.escape(label)}</div>'
        f"{cards}"
    )


def build_body(selected):
    header = (
        f'<p style="font-size:15px;color:#111111;margin:0 0 8px;">'
        f"오늘의 F&amp;B/무역 뉴스 ({len(selected)}건)"
        f"</p>"
    )
    sections = "".join(
        _section_html(CATEGORY_LABELS.get(cat, cat), [it for it in selected if it.get("category") == cat])
        for cat in CATEGORY_ORDER
    )
    return (
        '<div style="font-family:-apple-system,Helvetica,Arial,sans-serif;'
        'max-width:600px;margin:0 auto;">'
        f"{header}{sections}"
        "</div>"
    )


def main():
    items = load_news()
    sent_records = prune_sent(load_sent())
    sent_urls = {r["url"] for r in sent_records if r.get("url")}

    candidates = [it for it in items if it.get("url") and it["url"] not in sent_urls]
    candidates.sort(key=lambda x: x.get("keyword_score", 0), reverse=True)
    selected = candidates[:MAX_PER_DAY]

    if not selected:
        print("[INFO] 발송 대상 없음 (신규 관련 뉴스 없음)")
        # 정리된 sent 기록은 저장 (14일 지난 항목 제거 반영)
        SENT_PATH.parent.mkdir(parents=True, exist_ok=True)
        SENT_PATH.write_text(
            json.dumps({"sent": sent_records}, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return

    body = build_body(selected)
    subject = f"[Gov Radar] 오늘의 F&B/무역 뉴스 {len(selected)}건"

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

    today = date.today().isoformat()
    sent_records.extend({"url": it["url"], "sent_at": today} for it in selected)
    SENT_PATH.parent.mkdir(parents=True, exist_ok=True)
    SENT_PATH.write_text(
        json.dumps({"sent": sent_records}, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print(f"[OK] 발송 완료 -> {to_addr} ({len(selected)}건)")


if __name__ == "__main__":
    main()
