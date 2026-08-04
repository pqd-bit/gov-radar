#!/usr/bin/env python3
"""
gov-radar news collector
--------------------------
F&B 건강기능식품/웰니스 트렌드 + 국제무역 기회(수출입/바이어/소싱) 관련 뉴스를
RSS로 수집해 docs/data/news.json 으로 저장한다.

기존 정부지원사업 파이프라인(collector.py/notify.py/collect.yml)과는 완전히
독립된 별도 스크립트이며, 위 파일들을 참조하거나 수정하지 않는다.

데이터 소스 (전부 무료, API 키 불필요)
  1) Google News RSS - 검색어별 개별 쿼리 (한국어/영어)
  2) 업계 전문매체 RSS
     - just-food.com
     - fooddive.com
     - nutritionaloutlook.com   (NutraIngredients-Asia 대체)
     - foodbev.com              (FoodNavigator 대체)

  주: 실제 접속 검증 결과 FoodNavigator/NutraIngredients-Asia(William Reed/
  Informa 플랫폼)는 공개 RSS를 폐지하고 뉴스 sitemap만 제공한다
  (/Info/RSS-Feeds, /rss 등 전부 404). Food Business News의 RSS 엔드포인트는
  <item> 없이 빈 채널만 응답한다(0건). USDA FAS는 Akamai WAF가 자동화된
  요청을 전부 403으로 차단해 서버 환경(GitHub Actions 포함)에서 수집이
  불가능하다. 위 대체 소스(Nutritional Outlook, FoodBev Media)로 커버하고,
  Google News 쿼리에 무역/관세 키워드를 포함시켜 GAIN 리포트 관련 보도를
  간접적으로 커버한다.

  KOTRA 뉴스레터, 푸드위크 뉴스레터는 이미 다른 경로로 수신 중이므로
  중복 소스로 추가하지 않는다.

실행:
  python scripts/news_collector.py
출력:
  docs/data/news.json
"""
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

import feedparser
import requests

ROOT = Path(__file__).resolve().parent.parent
OUT_PATH = ROOT / "docs" / "data" / "news.json"

REQUEST_TIMEOUT = 20
USER_AGENT = "Mozilla/5.0 (compatible; gov-radar-news-bot/1.0)"

# Google News RSS 검색어 - (검색어, hl, gl, ceid)
GOOGLE_NEWS_QUERIES = [
    ("건강기능식품 트렌드", "ko", "KR", "KR:ko"),
    ("기능성 원료 수출", "ko", "KR", "KR:ko"),
    ("식품 바이어 수입", "ko", "KR", "KR:ko"),
    ("functional food trend", "en", "US", "US:en"),
    ("nutraceutical market", "en", "US", "US:en"),
    ("plant-based food trend", "en", "US", "US:en"),
    ("food buyer sourcing import", "en", "US", "US:en"),
    ("food export tariff", "en", "US", "US:en"),
]

# 업계 전문매체 RSS - 실제 접속해 <item> 존재까지 확인한 URL만 등록 (모듈 docstring 참고)
INDUSTRY_FEEDS = [
    ("just-food", "https://www.just-food.com/feed"),
    ("Food Dive", "https://www.fooddive.com/feeds/news/"),
    ("Nutritional Outlook", "https://www.nutritionaloutlook.com/rss.xml"),
    ("FoodBev Media", "https://www.foodbev.com/blog-feed.xml"),
]

# 관련도 판정에 쓰이는 키워드 그룹 (그룹 내 OR). 반드시 is_relevant() 내부에서만
# 참조한다 - 다른 곳에서 이 리스트를 직접 검사하는 코드를 추가하지 말 것.
WELLNESS_KEYWORDS = [
    "건강기능식품", "functional food", "nutraceutical", "wellness",
    "probiotic", "프로바이오틱스", "prebiotic", "postbiotic",
    "plant-based", "clean label", "기능성 원료", "단백질", "protein",
    "슈퍼푸드", "superfood", "gut health", "장 건강", "immune health",
    "면역력", "healthy aging", "헬시플레저", "dietary supplement",
]

TRADE_OPPORTUNITY_KEYWORDS = [
    "수출", "수입", "바이어", "buyer", "sourcing", "trade opportunity",
    "import demand", "export demand", "tariff", "관세", "mou",
    "무역관", "trade mission", "distributor wanted", "import ban",
    "export ban",
]


def is_relevant(title, summary):
    """
    관련도 판정 단일 authority.

    RSS 소스가 몇 개든 이 함수 하나만 거쳐서 관련도를 계산해야 하며, 이 함수를
    호출하지 않고 별도의 키워드 매칭 로직을 다른 곳에 작성해서는 안 된다
    (판정 로직이 두 곳 이상에 흩어지면서 소스별로 다르게 필터링되던
    이전 버그와 동일한 유형의 재발을 막기 위함).

    Returns:
        (is_match: bool, category: "wellness_trend"|"trade_opportunity"|None,
         score: int) - score는 매칭된 키워드 개수로, news_notify.py가 상위
        10건을 뽑을 때 재사용한다(재계산 없이 이 함수의 결과만 사용).
    """
    text = f"{title} {summary or ''}".lower()

    def _count_hits(keywords):
        hits = 0
        for kw in keywords:
            pattern = r"\b" + re.escape(kw.lower()) + r"\b"
            if re.search(pattern, text, flags=re.UNICODE):
                hits += 1
        return hits

    wellness_hits = _count_hits(WELLNESS_KEYWORDS)
    if wellness_hits > 0:
        return True, "wellness_trend", wellness_hits

    trade_hits = _count_hits(TRADE_OPPORTUNITY_KEYWORDS)
    if trade_hits > 0:
        return True, "trade_opportunity", trade_hits

    return False, None, 0


FETCH_ERRORS = []  # 소스별 fetch 예외 기록 - 빈 결과와 구분하기 위함


_CDATA_WRAPPER_RE = re.compile(r"^<!\[CDATA\[(.*)\]\]>$", re.S)


def _unwrap_cdata(raw):
    """일부 소스(nutritionaloutlook.com 등)는 title/description을 CDATA로
    감싸면서 그 자체를 다시 HTML 엔티티로 이스케이프해 내보낸다
    (예: <title>&lt;![CDATA[실제 제목]]&gt;</title>). XML 파서는 엔티티를
    정상적으로 복호화하므로 feedparser가 넘겨주는 문자열에는
    "<![CDATA[실제 제목]]>" 이 리터럴 텍스트로 남는다 - 이를 벗겨낸다."""
    if not raw:
        return raw
    m = _CDATA_WRAPPER_RE.match(raw.strip())
    return m.group(1).strip() if m else raw


def _strip_html(raw):
    if not raw:
        return ""
    text = _unwrap_cdata(raw)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _parse_published(entry):
    for key in ("published_parsed", "updated_parsed"):
        t = entry.get(key)
        if t:
            try:
                return datetime(*t[:6], tzinfo=timezone.utc).strftime("%Y-%m-%d")
            except Exception:
                pass
    return None


def _fetch_feed(url):
    resp = requests.get(url, timeout=REQUEST_TIMEOUT, headers={"User-Agent": USER_AGENT})
    resp.raise_for_status()
    return feedparser.parse(resp.content)


def fetch_google_news():
    items = []
    for query, hl, gl, ceid in GOOGLE_NEWS_QUERIES:
        url = f"https://news.google.com/rss/search?q={quote(query)}&hl={hl}&gl={gl}&ceid={ceid}"
        try:
            feed = _fetch_feed(url)
        except Exception as e:
            print(f"[ERROR] Google News RSS fetch 실패 ({query}): {e}", file=sys.stderr)
            FETCH_ERRORS.append(f"GoogleNews:{query}")
            continue

        for entry in feed.entries:
            raw_title = _unwrap_cdata((entry.get("title") or "").strip())
            source_name = "Google News"
            src = entry.get("source")
            if isinstance(src, dict) and src.get("title"):
                source_name = src["title"].strip()

            title = raw_title
            suffix = f" - {source_name}"
            if source_name != "Google News" and title.endswith(suffix):
                title = title[: -len(suffix)].strip()

            items.append({
                "title": title,
                "url": (entry.get("link") or "").strip(),
                "source": source_name,
                "published_date": _parse_published(entry),
                # Google News의 description은 제목을 감싼 HTML anchor뿐이라
                # 실질적인 요약이 아니므로 비워둔다.
                "summary": "",
            })
    return items


def fetch_industry_feeds():
    items = []
    for name, url in INDUSTRY_FEEDS:
        try:
            feed = _fetch_feed(url)
        except Exception as e:
            print(f"[ERROR] {name} RSS fetch 실패: {e}", file=sys.stderr)
            FETCH_ERRORS.append(name)
            continue

        if not feed.entries:
            print(f"[WARN] {name} RSS 응답에 항목 없음", file=sys.stderr)

        for entry in feed.entries:
            summary = _strip_html(entry.get("summary") or "")
            if len(summary) > 240:
                summary = summary[:240].rsplit(" ", 1)[0] + "..."

            items.append({
                "title": _unwrap_cdata((entry.get("title") or "").strip()),
                "url": (entry.get("link") or "").strip(),
                "source": name,
                "published_date": _parse_published(entry),
                "summary": summary,
            })
    return items


def dedupe_by_url(items):
    seen = set()
    out = []
    for it in items:
        url = it.get("url")
        if not url or url in seen:
            continue
        seen.add(url)
        out.append(it)
    return out


def main():
    raw_items = fetch_google_news() + fetch_industry_feeds()
    raw_items = [it for it in raw_items if it["title"] and it["url"]]

    if not raw_items:
        # 모든 소스가 fetch 실패했거나 정상 응답이 전부 빈 피드였던 경우:
        # 정상적인 "오늘은 관련 뉴스 0건"과 구분해 기존 news.json을
        # 빈 데이터로 덮어쓰지 않는다.
        print(
            f"[ERROR] 모든 소스에서 항목을 가져오지 못함 "
            f"(fetch 실패: {', '.join(FETCH_ERRORS) or '없음'}) - "
            f"기존 {OUT_PATH} 유지, 이번 회차는 갱신 skip",
            file=sys.stderr,
        )
        return

    relevant = []
    for raw in raw_items:
        ok, category, score = is_relevant(raw["title"], raw["summary"])
        if not ok:
            continue
        item = dict(raw)
        item["category"] = category
        item["keyword_score"] = score
        relevant.append(item)

    relevant = dedupe_by_url(relevant)
    relevant.sort(key=lambda x: x.get("published_date") or "0000-00-00", reverse=True)

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "count": len(relevant),
        "items": relevant,
    }

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    wellness_n = sum(1 for it in relevant if it["category"] == "wellness_trend")
    trade_n = sum(1 for it in relevant if it["category"] == "trade_opportunity")
    print(
        f"[OK] {len(relevant)}건 저장 (원본 {len(raw_items)}건 중 관련도 필터 통과, "
        f"wellness_trend {wellness_n} / trade_opportunity {trade_n}) -> {OUT_PATH}"
    )
    if FETCH_ERRORS:
        print(f"[WARN] 일부 소스 fetch 실패(다른 소스로 대체 진행됨): {', '.join(FETCH_ERRORS)}", file=sys.stderr)


if __name__ == "__main__":
    main()
