#!/usr/bin/env python3
"""
F&B 건강기능식품/웰니스 트렌드 + 국제무역 기회 뉴스 중 아직 발송하지 않은
기사를 골라 "편집된" 느낌의 이메일로 보낸다.

news.json의 관련도 판정(category, keyword_score)과 신선도 필터는
news_collector.py에서 이미 끝난 상태로 신뢰하며, 이 스크립트에서 키워드나
발행일을 다시 매칭/필터링하지 않는다 - 국내(domestic)/해외(foreign) 5:5
비율 선정만 select_daily_articles()에서 수행한다.

주제 클러스터링/급상승 키워드/행사 캘린더 계산은 전부 trend_tracker.py에
위임한다(순수 알고리즘/규칙 기반, LLM 호출 없음) - 이 스크립트에서 같은
로직을 다시 구현하지 않는다.

기존 정부지원사업 notify.py, 피드백 학습(source_scores.json) 로직과는
완전히 독립된 별도 스크립트다.

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
from urllib.parse import quote

from trend_tracker import (
    cluster_by_theme,
    count_today_keywords,
    detect_trending_keywords,
    extract_event_mentions,
    load_keyword_history,
    prune_keyword_history,
    save_keyword_history,
)

ROOT = Path(__file__).resolve().parent.parent
DATA_PATH = ROOT / "docs" / "data" / "news.json"
SENT_PATH = ROOT / "docs" / "data" / "news_sent.json"

MAX_PER_DAY = 10

# GitHub Actions가 자동으로 "owner/repo" 형태로 주입해주는 환경변수 (별도 설정 불필요).
# dismiss.yml/notify.py와 동일한 GitHub Issues 트릭으로 피드백 링크를 만드는 데 쓴다.
GITHUB_REPO = os.environ.get("GITHUB_REPOSITORY", "")

SENT_RETENTION_DAYS = 14

CATEGORY_LABELS = {
    "wellness_trend": "웰니스 트렌드",
    "trade_opportunity": "무역 기회",
}

# 클러스터 카드의 소제목은 theme_label을 그대로 쓰지만, "기타"만 사람이 읽기
# 좋은 문구로 바꿔 표시한다(cluster_by_theme()가 반환하는 라벨 자체는
# 다른 소비자를 위해 "기타"로 유지 - 표시 문구 변환은 이 스크립트에서만).
MISC_CLUSTER_LABEL = "기타"
MISC_CLUSTER_HEADING = "그 외 소식"

ORIGIN_LABELS = {
    "domestic": "국내",
    "foreign": "해외",
}


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


def select_daily_articles(domestic, foreign, quota=10):
    """
    국내:해외 5:5 비율 선정 단일 authority.

    domestic/foreign은 호출 전에 이미 (신선도 통과 + keyword_score 내림차순)
    으로 정렬되어 있어야 한다. quota의 절반씩 우선 배정하고, 한쪽이 절반에
    못 미치면 부족한 만큼을 다른 쪽에서 채운다(총합은 최대 quota 유지). 양쪽
    다 부족하면 채우지 않고 있는 만큼만 반환한다 - 오래된 기사로 억지로
    채우는 로직은 두지 않는다(신선도 필터는 news_collector.py의 is_fresh()가
    이미 적용했으므로 여기 후보 리스트 자체에 오래된 기사가 없다).

    이 함수 밖에서 선정 결과 리스트를 추가로 자르거나 순서를 섞지 않는다.
    """
    half = quota // 2

    dom_n = min(half, len(domestic))
    for_n = min(half, len(foreign))

    leftover = quota - dom_n - for_n
    if leftover > 0:
        extra_dom = min(leftover, len(domestic) - dom_n)
        dom_n += extra_dom
        leftover -= extra_dom
    if leftover > 0:
        extra_for = min(leftover, len(foreign) - for_n)
        for_n += extra_for
        leftover -= extra_for

    return domestic[:dom_n] + foreign[:for_n]


def feedback_url(kind: str, article_id: str):
    """"도움됨 👍"/"별로 👎" 클릭 시 이동할 GitHub 이슈 생성 링크.

    dismiss.yml/notify.py의 dismiss_url()과 동일한 트릭: 이슈 제목을
    "feedback-good:{article_id}"/"feedback-bad:{article_id}" 형태로 만들어
    .github/workflows/news_feedback.yml 이 감지하도록 한다.
    """
    if not GITHUB_REPO or not article_id:
        return ""
    title = quote(f"feedback-{kind}:{article_id}")
    return f"https://github.com/{GITHUB_REPO}/issues/new?title={title}&labels=news-feedback"


def _card_html(it):
    title = html.escape(it["title"])
    source = html.escape(it.get("source") or "")
    published = html.escape(it.get("published_date") or "날짜 미상")
    category = html.escape(CATEGORY_LABELS.get(it.get("category"), it.get("category") or ""))
    origin = html.escape(ORIGIN_LABELS.get(it.get("origin"), it.get("origin") or ""))
    view_href = html.escape(it["url"], quote=True)
    good_href = html.escape(feedback_url("good", it.get("id") or ""), quote=True)
    bad_href = html.escape(feedback_url("bad", it.get("id") or ""), quote=True)

    return f"""
<div style="border:1px solid #e0e0e0;border-radius:8px;padding:16px;margin-bottom:12px;">
  <div style="font-size:15px;font-weight:bold;color:#111111;margin-bottom:6px;">{title}</div>
  <div style="font-size:13px;color:#767676;margin-bottom:12px;">{source} · <strong style="color:#111111;">{published}</strong> · {category} · {origin}</div>
  <div>
    <a href="{view_href}" style="display:inline-block;background-color:#2563eb;color:#ffffff;text-decoration:none;font-size:13px;padding:6px 12px;border-radius:6px;margin-right:8px;">기사 보기 →</a>
    <a href="{good_href}" style="display:inline-block;background-color:#f0fdf4;color:#15803d;text-decoration:none;font-size:13px;padding:6px 12px;border-radius:6px;margin-right:8px;">도움됨 👍</a>
    <a href="{bad_href}" style="display:inline-block;background-color:#fef2f2;color:#b91c1c;text-decoration:none;font-size:13px;padding:6px 12px;border-radius:6px;">별로 👎</a>
  </div>
</div>"""


def _briefing_html(trending_keywords):
    """상단 "오늘의 브리핑" - 급상승 키워드가 있을 때만 표시하고, 없으면
    빈 문자열을 반환해 섹션 자체를 생략한다."""
    if not trending_keywords:
        return ""
    keywords_text = ", ".join(trending_keywords)
    return (
        '<div style="background-color:#eff6ff;border-radius:8px;padding:12px 16px;'
        'margin-bottom:20px;font-size:13px;color:#1e3a8a;">'
        f"📈 이번 주 뜨는 키워드: <strong>{html.escape(keywords_text)}</strong>"
        "</div>"
    )


def _cluster_section_html(cluster):
    """주제별 클러스터 카드 하나. cluster_by_theme()가 반환한 "기타" 라벨만
    사람이 읽기 좋은 "그 외 소식" 문구로 바꿔 표시한다."""
    label = cluster["theme_label"]
    heading = MISC_CLUSTER_HEADING if label == MISC_CLUSTER_LABEL else label
    cards = "".join(_card_html(it) for it in cluster["articles"])
    return (
        f'<div style="font-size:14px;font-weight:bold;color:#111111;'
        f'margin:20px 0 10px;">{html.escape(heading)}</div>'
        f"{cards}"
    )


def _event_calendar_html(events):
    """하단 "이번 주 F&B 행사·전시 캘린더" - 이벤트가 없으면 섹션 자체를
    생략한다(빈 문자열 반환)."""
    if not events:
        return ""
    rows = "".join(
        '<li style="margin-bottom:6px;">'
        f'<strong style="color:#111111;">{html.escape(e["date_range"])}</strong> '
        f'{html.escape(e["name"])} '
        f'<span style="color:#767676;">· {html.escape(e["source"])}</span>'
        "</li>"
        for e in events
    )
    return (
        '<div style="margin-top:24px;padding-top:16px;border-top:1px solid #e0e0e0;">'
        '<div style="font-size:14px;font-weight:bold;color:#111111;margin-bottom:10px;">'
        "📅 이번 주 F&amp;B 행사·전시 캘린더</div>"
        f'<ul style="margin:0;padding-left:18px;font-size:13px;color:#111111;">{rows}</ul>'
        "</div>"
    )


def build_body(selected, trending_keywords=None, events=None):
    """
    이메일 본문 조립 단일 지점: [브리핑] -> [주제별 클러스터 카드] ->
    [행사 캘린더] 순서로 배치한다. 클러스터링은 trend_tracker.cluster_by_theme
    한 곳에서만 계산하고(여기서 selected를 다시 그룹핑하지 않는다),
    "기타" 클러스터는 cluster_by_theme()가 이미 맨 뒤로 정렬해 반환하므로
    이 함수는 받은 순서 그대로 렌더링만 한다.
    """
    trending_keywords = trending_keywords or []
    events = events or []

    header = (
        f'<p style="font-size:15px;color:#111111;margin:0 0 8px;">'
        f"오늘의 F&amp;B/무역 뉴스 ({len(selected)}건)"
        f"</p>"
    )
    briefing = _briefing_html(trending_keywords)
    clusters = cluster_by_theme(selected)
    cluster_sections = "".join(_cluster_section_html(c) for c in clusters)
    calendar = _event_calendar_html(events)

    return (
        '<div style="font-family:-apple-system,Helvetica,Arial,sans-serif;'
        'max-width:600px;margin:0 auto;">'
        f"{header}{briefing}{cluster_sections}{calendar}"
        "</div>"
    )


def main():
    items = load_news()
    sent_records = prune_sent(load_sent())
    sent_urls = {r["url"] for r in sent_records if r.get("url")}

    # 급상승 키워드/행사 캘린더는 오늘 수집된 전체 관련 기사(items) 기준으로
    # 계산한다 - 이메일에 실제로 실리는 10건(selected)보다 넓은 표본이라야
    # "이번 주 뜨는 키워드"/행사 캘린더 신호로 더 유의미하다. 클러스터링만
    # 실제로 카드로 보여줄 selected에 대해 build_body()에서 수행한다.
    history_before_today = load_keyword_history()
    trending_keywords = detect_trending_keywords(items, history_before_today)
    upcoming_events = extract_event_mentions(items)

    # keyword_history.json 갱신은 발송 대상 유무와 무관하게 매 실행마다
    # 반영한다(발송할 새 기사가 없어도 오늘 수집 자체는 이력에 남겨야
    # 다음날 이후의 "최근 7일 평균" 계산이 끊기지 않는다).
    today = date.today()
    history = dict(history_before_today)
    history[today.isoformat()] = count_today_keywords(items)
    history = prune_keyword_history(history, today)
    save_keyword_history(history)

    candidates = [it for it in items if it.get("url") and it["url"] not in sent_urls]
    domestic = [it for it in candidates if it.get("origin") == "domestic"]
    foreign = [it for it in candidates if it.get("origin") == "foreign"]
    domestic.sort(key=lambda x: x.get("keyword_score", 0), reverse=True)
    foreign.sort(key=lambda x: x.get("keyword_score", 0), reverse=True)

    selected = select_daily_articles(domestic, foreign, quota=MAX_PER_DAY)

    if not selected:
        print("[INFO] 발송 대상 없음 (신규 관련 뉴스 없음)")
        # 정리된 sent 기록은 저장 (14일 지난 항목 제거 반영)
        SENT_PATH.parent.mkdir(parents=True, exist_ok=True)
        SENT_PATH.write_text(
            json.dumps({"sent": sent_records}, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return

    body = build_body(selected, trending_keywords, upcoming_events)
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

    sent_records.extend({"url": it["url"], "sent_at": today.isoformat()} for it in selected)
    SENT_PATH.parent.mkdir(parents=True, exist_ok=True)
    SENT_PATH.write_text(
        json.dumps({"sent": sent_records}, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print(f"[OK] 발송 완료 -> {to_addr} ({len(selected)}건)")


if __name__ == "__main__":
    main()
