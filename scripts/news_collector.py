#!/usr/bin/env python3
"""
gov-radar news collector
--------------------------
F&B 건강기능식품/웰니스 트렌드 + 국제무역 기회(수출입/바이어/소싱) 관련 뉴스를
RSS로 수집해 docs/data/news.json 으로 저장한다.

기존 정부지원사업 파이프라인(collector.py/notify.py/collect.yml)과는 완전히
독립된 별도 스크립트이며, 위 파일들을 참조하거나 수정하지 않는다.

데이터 소스 (전부 무료, API 키 불필요)
  1) Google News RSS - 검색어별 개별 쿼리 (국내 한글 키워드 / 해외 영문 키워드)
  2) 업계 전문매체 RSS
     국내(domestic):
       - 식품음료신문 thinkfood.co.kr
       - 식품저널 foodnews.co.kr
       - 식품외식경제 foodbank.co.kr
     해외(foreign):
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

  aT(한국농수산식품유통공사)는 사이트에 RSS 피드가 없어(보도자료 게시판만
  존재) Google News RSS에 site:at.or.kr 연산자를 건 쿼리로 대체 수집한다
  (직접 접속 검증 결과 정상적으로 관련 기사를 반환함).

  KOTRA 뉴스레터, 푸드위크 뉴스레터는 이미 다른 경로로 수신 중이므로
  중복 소스로 추가하지 않는다.

실행:
  python scripts/news_collector.py
출력:
  docs/data/news.json
"""
import difflib
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

# Google News RSS 검색어 - (검색어, hl, gl, ceid). 국내/해외를 별도 리스트로
# 관리하며, 각 항목의 origin은 어느 리스트에서 왔는지로 결정된다 (fetch
# 시점에 명시적으로 태깅 - main.py 등 다른 곳에서 origin을 추론하지 않는다).
DOMESTIC_GOOGLE_NEWS_QUERIES = [
    ("건강기능식품", "ko", "KR", "KR:ko"),
    ("건강기능식품 수출", "ko", "KR", "KR:ko"),
    ("기능성 원료", "ko", "KR", "KR:ko"),
    ("기능성 원료 수출", "ko", "KR", "KR:ko"),
    ("프로바이오틱스", "ko", "KR", "KR:ko"),
    ("식품 수출", "ko", "KR", "KR:ko"),
    ("농식품 수출", "ko", "KR", "KR:ko"),
    ("K-푸드 수출", "ko", "KR", "KR:ko"),
    ("해외 바이어", "ko", "KR", "KR:ko"),
    ("식품 트렌드", "ko", "KR", "KR:ko"),
    ("웰니스", "ko", "KR", "KR:ko"),
    # aT(한국농수산식품유통공사)는 자체 RSS가 없어 site: 연산자로 대체 수집
    ("site:at.or.kr 수출", "ko", "KR", "KR:ko"),
]

FOREIGN_GOOGLE_NEWS_QUERIES = [
    ("functional food trend", "en", "US", "US:en"),
    ("nutraceutical market", "en", "US", "US:en"),
    ("plant-based food trend", "en", "US", "US:en"),
    ("food buyer sourcing import", "en", "US", "US:en"),
    ("food export tariff", "en", "US", "US:en"),
]

# 업계 전문매체 RSS - 실제 접속해 <item> 존재까지 확인한 URL만 등록 (모듈 docstring 참고)
DOMESTIC_INDUSTRY_FEEDS = [
    ("식품음료신문", "https://www.thinkfood.co.kr/rss/allArticle.xml"),
    ("식품저널", "https://www.foodnews.co.kr/rss/allArticle.xml"),
    ("식품외식경제", "https://www.foodbank.co.kr/rss/allArticle.xml"),
]

FOREIGN_INDUSTRY_FEEDS = [
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

# origin(국내/해외) 버킷 내부에서 상위 노출시킬 기사에 주는 가산점 키워드.
# 반드시 priority_bonus() 내부에서만 참조한다 - is_relevant()의 관련도
# 판정과는 별개의 부가 스코어이며, 다른 곳에서 직접 매칭하지 않는다.
DOMESTIC_PRIORITY_KEYWORDS = ["칼럼", "사설", "기고", "리포트", "보고서", "연구"]
FOREIGN_PRIORITY_KEYWORDS = [
    "regulation", "compliance", "fda", "eu", "tariff", "관세", "규정",
    "trend", "트렌드",
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


def priority_bonus(origin, title, summary):
    """
    origin 버킷 내부 정렬용 가산점 단일 authority.

    국내 기사는 칼럼/사설/기고/리포트/보고서/연구가 제목·요약에 포함되면,
    해외 기사는 규제(regulation/compliance/FDA/EU/tariff/관세/규정)나
    트렌드(trend/트렌드) 관련 표현이 포함되면 가산점을 받는다. is_relevant()
    의 관련도 판정과는 무관한 별도 스코어이며, 이 함수 밖에서 같은 키워드를
    다시 매칭하는 코드를 추가하지 않는다.
    """
    text = f"{title} {summary or ''}".lower()
    keywords = DOMESTIC_PRIORITY_KEYWORDS if origin == "domestic" else FOREIGN_PRIORITY_KEYWORDS

    bonus = 0
    for kw in keywords:
        pattern = r"\b" + re.escape(kw.lower()) + r"\b"
        if re.search(pattern, text, flags=re.UNICODE):
            bonus += 1
    return bonus


def is_fresh(published_date, run_date, max_age_days=2):
    """
    신선도 판정 단일 authority.

    published_date("YYYY-MM-DD" 문자열)가 run_date(실행일, date 객체) 기준
    max_age_days일보다 오래됐으면 False. RSS 소스별 파싱 지연을 감안해
    1일이 아닌 2일을 기본 버퍼로 둔다. published_date가 없거나 파싱 불가능한
    경우, 신선하다고 잘못 통과시키는 쪽보다 안전하게 탈락(False) 처리한다.

    main()에서 최종 후보 리스트(relevant)를 만드는 단 한 곳에서만 호출한다
    - 이전에 관련도 판정 로직이 여러 곳에 중복 구현됐던 버그와 같은 실수를
    반복하지 않기 위함.
    """
    if not published_date:
        return False
    try:
        pub = datetime.strptime(published_date, "%Y-%m-%d").date()
    except ValueError:
        return False
    return (run_date - pub).days <= max_age_days


NEAR_DUP_SIMILARITY_THRESHOLD = 0.75

# 제목 맨 앞의 "[식음료브리핑]", "[단독]", "(속보)" 류 대괄호/괄호 접두어를
# 하나 이상 연속으로 제거한다. _normalize_title() 전용.
_LEADING_BRACKET_TAGS_RE = re.compile(r"^(?:\s*[\[(][^\]\)]{0,40}[\]\)])+\s*")
# "서울=", "부산=" 같은 매체 바이라인 접두어. _normalize_title() 전용.
_BYLINE_PREFIX_RE = re.compile(r"^[가-힣A-Za-z0-9·]{1,12}=\s*")
_QUOTE_BRACKET_CHARS_RE = re.compile(r"[\"'“”‘’「」『』()\[\]{}〈〉《》<>]")
_PUNCT_TO_SPACE_RE = re.compile(r"[,.\-–—:;!?~·|]")


def _normalize_title(title):
    """
    dedupe_near_duplicates() 전용 제목 정규화 헬퍼. 이 함수 밖에서 근접중복
    판정을 위한 별도 정규화 로직을 새로 만들지 말 것.
    """
    text = title or ""
    text = _LEADING_BRACKET_TAGS_RE.sub("", text)
    text = _BYLINE_PREFIX_RE.sub("", text)
    text = text.replace("…", " ")
    text = re.sub(r"\.{2,}", " ", text)
    text = text.lower()
    text = _QUOTE_BRACKET_CHARS_RE.sub("", text)
    text = _PUNCT_TO_SPACE_RE.sub(" ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def dedupe_near_duplicates(articles):
    """
    제목 유사도 기반 근접중복 제거 단일 authority.

    동일 보도자료가 매체명만 바뀐 채 여러 곳에 그대로 게재되면 URL은
    달라도 사실상 같은 기사가 된다 - URL 기준 중복방지(dedupe_by_url,
    news_sent.json)로는 못 잡는 케이스다. 제목을 정규화한 뒤
    difflib.SequenceMatcher로 유사도를 계산해 NEAR_DUP_SIMILARITY_THRESHOLD
    이상이면 같은 클러스터로 묶고, 클러스터당 1건만 남긴다.

    main()에서 is_fresh() 필터 통과 직후, URL 기준 중복 제거 이전에 단 한
    번만 호출한다. 근접중복 판정 로직을 다른 곳에 별도로 만들지 말 것
    (관련도 판정이 여러 곳에 흩어졌던 과거 실수를 반복하지 않기 위함).

    클러스터 내에서 남길 기사 선택 기준:
      (a) published_date가 가장 이른 것
      (b) 같으면 summary가 더 긴 것 (정보량이 많다고 가정)
      (c) 그래도 같으면 source 이름 사전순으로 첫 번째
    """
    normalized_titles = [_normalize_title(a.get("title")) for a in articles]

    clusters = []  # list[list[int]] - 원본 인덱스 리스트
    for idx, norm in enumerate(normalized_titles):
        target_cluster = None
        for cluster in clusters:
            if any(
                difflib.SequenceMatcher(None, norm, normalized_titles[member]).ratio()
                >= NEAR_DUP_SIMILARITY_THRESHOLD
                for member in cluster
            ):
                target_cluster = cluster
                break
        if target_cluster is not None:
            target_cluster.append(idx)
        else:
            clusters.append([idx])

    def _keep_key(idx):
        art = articles[idx]
        return (
            art.get("published_date") or "9999-99-99",
            -len(art.get("summary") or ""),
            art.get("source") or "",
        )

    return [articles[min(cluster, key=_keep_key)] for cluster in clusters]


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


def fetch_google_news(queries, origin):
    items = []
    for query, hl, gl, ceid in queries:
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
                "origin": origin,
            })
    return items


def fetch_industry_feeds(feeds, origin):
    items = []
    for name, url in feeds:
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
                "origin": origin,
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
    raw_items = (
        fetch_google_news(DOMESTIC_GOOGLE_NEWS_QUERIES, "domestic")
        + fetch_google_news(FOREIGN_GOOGLE_NEWS_QUERIES, "foreign")
        + fetch_industry_feeds(DOMESTIC_INDUSTRY_FEEDS, "domestic")
        + fetch_industry_feeds(FOREIGN_INDUSTRY_FEEDS, "foreign")
    )
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

    run_date = datetime.now(timezone.utc).date()

    relevant = []
    for raw in raw_items:
        ok, category, score = is_relevant(raw["title"], raw["summary"])
        if not ok:
            continue
        # 최종 후보 리스트를 만드는 단 한 곳 - 신선도 필터는 여기서만 호출한다.
        if not is_fresh(raw["published_date"], run_date):
            continue
        item = dict(raw)
        item["category"] = category
        item["keyword_score"] = score + priority_bonus(raw["origin"], raw["title"], raw["summary"])
        relevant.append(item)

    # 신선도 필터 통과 직후, URL 기준 중복 제거보다 먼저 근접중복(제목
    # 유사도 기반)을 제거한다 - 매체만 바뀐 동일 보도자료가 URL 중복방지를
    # 통과해 개별 기사로 남는 것을 여기서 잡는다.
    before_near_dup = len(relevant)
    relevant = dedupe_near_duplicates(relevant)
    near_dup_removed = before_near_dup - len(relevant)

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
    domestic_n = sum(1 for it in relevant if it["origin"] == "domestic")
    foreign_n = sum(1 for it in relevant if it["origin"] == "foreign")
    print(
        f"[OK] {len(relevant)}건 저장 (원본 {len(raw_items)}건 중 관련도+신선도 필터 통과, "
        f"근접중복 제거 {near_dup_removed}건, "
        f"wellness_trend {wellness_n} / trade_opportunity {trade_n}, "
        f"domestic {domestic_n} / foreign {foreign_n}) -> {OUT_PATH}"
    )
    if FETCH_ERRORS:
        print(f"[WARN] 일부 소스 fetch 실패(다른 소스로 대체 진행됨): {', '.join(FETCH_ERRORS)}", file=sys.stderr)


if __name__ == "__main__":
    main()
