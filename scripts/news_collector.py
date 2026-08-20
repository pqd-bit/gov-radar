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
       - nutritioninsight.com     (2026-08-20 추가, 직접 RSS 확인)
       - foodnavigator.com        (2026-08-20 추가, 아래 참고)

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

  2026-08-20 소스 확대 조사 결과:
    - 데일리팜(dailypharm.com): 직접 RSS 전부 404 (rssIndex.html도 없음,
      thinkfood류와 다른 CMS). Google News site:dailypharm.com 쿼리로
      대체하고 "건강기능식품" 키워드를 함께 걸어 관련 기사만 잡히도록 함
      (직접 접속 검증 결과 실제 기사가 반환됨).
    - 식품산업통계정보 FIS(atfis.or.kr): 직접 RSS 없음. Google News
      site: 쿼리도 대부분 통계 포털의 정적 메뉴 페이지만 반환되고 실제
      기사가 거의 없어(직접 접속 검증) 추가하지 않음 - aT 뉴스는 기존
      site:at.or.kr 쿼리로 이미 커버됨.
    - 뉴트리원(nutrione.co.kr): 실제로는 건기식 쇼핑몰(자사몰)이지
      전문지/매체가 아님(직접 접속 확인: "뉴트리원 공식몰..."). 뉴스
      소스로 부적합해 제외.
    - Nutrition Insight(nutritioninsight.com): 홈페이지 <link rel="alternate">
      로 RSS 자동탐색 성공 - 실제 엔드포인트는 CDN인
      resource-cns.cnsmedia.com/rss/ninews.xml (직접 접속 검증, 50건).
    - Food Navigator USA: foodnavigator-usa.com은 현재 foodnavigator.com
      (기존 EU/글로벌판)으로 301 리다이렉트되어 별도 사이트로 존재하지
      않음(William Reed가 통합한 것으로 보임, 직접 접속 검증). 즉 "USA
      전용판"은 더 이상 없고, 통합된 foodnavigator.com의 RSS
      (/arc/outboundfeeds/rss/, 직접 접속 검증 20건)만 유효하다 - 이를
      "FoodNavigator"로 등록한다(기존 FoodBev Media 대체 소스와는 별개로
      유지 - 매체가 다름).
    - Ingredients Network(ingredientsnetwork.com): RSS 자동탐색/직접 경로
      전부 실패. Google News site:ingredientsnetwork.com 쿼리로 대체
      (직접 접속 검증 결과 FDA/성분 관련 실제 기사 반환됨).

실행:
  python scripts/news_collector.py
출력:
  docs/data/news.json
"""
import difflib
import hashlib
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
SOURCE_SCORES_PATH = ROOT / "docs" / "data" / "source_scores.json"

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
    # 데일리팜도 자체 RSS가 없어(모듈 docstring 참고) site: 연산자로 대체 수집
    ("site:dailypharm.com 건강기능식품", "ko", "KR", "KR:ko"),
]

FOREIGN_GOOGLE_NEWS_QUERIES = [
    ("functional food trend", "en", "US", "US:en"),
    ("nutraceutical market", "en", "US", "US:en"),
    ("plant-based food trend", "en", "US", "US:en"),
    ("food buyer sourcing import", "en", "US", "US:en"),
    ("food export tariff", "en", "US", "US:en"),
    # Ingredients Network도 자체 RSS가 없어(모듈 docstring 참고) site: 연산자로 대체 수집
    ("site:ingredientsnetwork.com", "en", "US", "US:en"),
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
    ("Nutrition Insight", "https://resource-cns.cnsmedia.com/rss/ninews.xml"),
    # foodnavigator-usa.com은 foodnavigator.com(기존 EU/글로벌판)으로 301
    # 리다이렉트되어 별도 사이트로 존재하지 않는다(모듈 docstring 참고) -
    # 통합된 도메인의 RSS를 등록한다.
    ("FoodNavigator", "https://www.foodnavigator.com/arc/outboundfeeds/rss/"),
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
         score: int, matched_keywords: list[str]) - score는 매칭된 키워드
        개수로, news_notify.py가 상위 10건을 뽑을 때 재사용한다(재계산 없이
        이 함수의 결과만 사용). matched_keywords는 apply_learned_weight()가
        source_scores.json의 키워드별 피드백 점수를 조회할 때만 사용한다.
    """
    text = f"{title} {summary or ''}".lower()

    def _count_hits(keywords):
        hits = []
        for kw in keywords:
            pattern = r"\b" + re.escape(kw.lower()) + r"\b"
            if re.search(pattern, text, flags=re.UNICODE):
                hits.append(kw)
        return hits

    wellness_hits = _count_hits(WELLNESS_KEYWORDS)
    if wellness_hits:
        return True, "wellness_trend", len(wellness_hits), wellness_hits

    trade_hits = _count_hits(TRADE_OPPORTUNITY_KEYWORDS)
    if trade_hits:
        return True, "trade_opportunity", len(trade_hits), trade_hits

    return False, None, 0, []


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


# apply_learned_weight()의 배율 clamp 범위 - Dean의 "도움됨/별로" 피드백
# 누적치가 아무리 극단적이어도 특정 소스/키워드가 완전히 0이 되거나
# 점수가 무한정 커지지 않도록 한다.
LEARNED_WEIGHT_MIN = 0.3
LEARNED_WEIGHT_MAX = 3.0
LEARNED_WEIGHT_STEP = 0.15


def load_source_scores():
    """docs/data/source_scores.json 을 읽는다. news_feedback.yml이 Dean의
    "도움됨 👍"/"별로 👎" 클릭을 이 파일에 소스/키워드별 good/bad 카운트로
    누적하면, 다음 수집 회차부터 apply_learned_weight()가 여기서 읽어
    반영한다."""
    if not SOURCE_SCORES_PATH.exists():
        return {"sources": {}, "keywords": {}}
    try:
        data = json.loads(SOURCE_SCORES_PATH.read_text(encoding="utf-8"))
        data.setdefault("sources", {})
        data.setdefault("keywords", {})
        return data
    except Exception:
        return {"sources": {}, "keywords": {}}


def apply_learned_weight(article, scores):
    """
    피드백 기반 가중치 반영 단일 지점.

    article["keyword_score"](base_score)에 source_scores.json의 해당
    source와 매칭된 키워드(article["matched_keywords"])들의 good/bad
    카운트 합을 반영해 score = base_score * (1 + good*0.15 - bad*0.15)
    형태로 조정한다. 배율은 [LEARNED_WEIGHT_MIN, LEARNED_WEIGHT_MAX]로
    clamp해 피드백이 누적돼도 극단적으로 치우치지 않게 한다.

    스코어링 파이프라인에서 이 함수 하나만, main()의 keyword_score 계산
    직후 한 지점에서만 호출한다 - 가중치 반영 로직을 다른 곳에 중복
    구현하지 않는다.
    """
    base_score = article.get("keyword_score", 0)

    good = 0
    bad = 0

    source_stats = scores.get("sources", {}).get(article.get("source"), {})
    good += source_stats.get("good", 0)
    bad += source_stats.get("bad", 0)

    keyword_stats = scores.get("keywords", {})
    for kw in article.get("matched_keywords", []):
        kw_stats = keyword_stats.get(kw, {})
        good += kw_stats.get("good", 0)
        bad += kw_stats.get("bad", 0)

    multiplier = 1 + good * LEARNED_WEIGHT_STEP - bad * LEARNED_WEIGHT_STEP
    multiplier = max(LEARNED_WEIGHT_MIN, min(LEARNED_WEIGHT_MAX, multiplier))
    return round(base_score * multiplier, 3)


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


ENTITY_DUP_DATE_WINDOW_DAYS = 3

# 따옴표(직선/곡선/한글 낫표)로 감싼 제품명/브랜드명. 최소 2자 이상만 인정해
# 노이즈를 줄인다. _extract_entities() 전용.
_QUOTED_ENTITY_RE = re.compile(r"['\"‘’“”「」『』]([^'\"‘’“”「」『』]{2,20})['\"‘’“”「」『』]")
# "OO그룹" 표기. _extract_entities() 전용.
_GROUP_SUFFIX_RE = re.compile(r"[가-힣A-Za-z0-9]{1,10}그룹")
# "㈜OOO" / "OOO㈜" / "OOO 주식회사" 표기. _extract_entities() 전용.
_CORP_MARK_RE = re.compile(r"㈜\s*[가-힣A-Za-z0-9]{1,10}|[가-힣A-Za-z0-9]{1,10}\s*㈜")
_JUSIKHOESA_RE = re.compile(r"[가-힣A-Za-z0-9]{1,10}\s*주식회사")
# "OO사" 표기 - 뒤에 조사/구두점/문장 끝이 와야 온전한 단어로 인정한다.
_SA_SUFFIX_RE = re.compile(r"[가-힣]{2,6}사(?=[\s,.·’”\"')]|$)")
# 영문 대문자로 시작하는 고유명사(1~4단어 연속 Title Case). _extract_entities() 전용.
_ENGLISH_PROPER_NOUN_RE = re.compile(r"\b[A-Z][A-Za-z0-9&.]*(?:\s+[A-Z][A-Za-z0-9&.]*){0,3}\b")
# 한국 뉴스 제목 관용구: 맨 앞 "회사명," 패턴 (대괄호 접두어는 먼저 제거).
_LEADING_ENTITY_COMMA_RE = re.compile(r"^([A-Za-z가-힣0-9&·]{1,12}),")

# "OO사" 패턴에서 걸러야 하는 일반 명사(회사 고유명사가 아닌 통칭/역할어).
# 이 목록에 없는 나머지 "OO사" 매치까지 전부 진짜 회사명이라는 보장은 없지만
# (완벽한 NER이 아닌 휴리스틱이므로), 뉴스 제목에 실제로 자주 나오는 통칭은
# 걸러 오탐(false positive) 위험을 낮춘다.
_GENERIC_SA_WORDS = {
    "기사", "행사", "회사", "이사", "조사", "검사", "감사", "역사", "강사", "교사",
    "관계사", "계열사", "협력사", "제조사", "유통사", "판매사", "수출사", "수입사",
    "운영사", "시행사", "대행사", "물류사", "광고사", "여행사", "항공사", "보험사",
    "증권사", "방송사", "신문사", "통신사", "건설사", "참가사", "참여사", "발주사",
    "하청사", "원청사", "계약사", "관련사", "소속사", "당사", "타사", "자사", "귀사",
    "본사", "지사", "출판사", "잡지사",
}

# 영문 고유명사 후보에서 걸러야 하는 일반어/약어. _extract_entities() 전용.
_ENGLISH_STOPWORDS = {
    "THE", "THIS", "THAT", "THESE", "THOSE", "WHAT", "HOW", "WHY", "WHEN",
    "WHERE", "WHO", "NEW", "GLOBAL", "WORLD", "KOREA", "KOREAN", "SOUTH",
    "NORTH", "MOU", "SCI", "FDA", "EU", "US", "UK", "CEO", "IPO", "ESG",
    "GDP", "AI", "IT", "API", "PR", "IR", "R", "D", "CES", "NEWS", "REPORT",
    "B2B", "B2C", "USDA", "WTO", "OECD",
}

# 이름 하나만으로는 특정 사건을 식별하는 신호가 되지 못하는 대형 공공기관/
# 무역진흥기관. aT(한국농수산식품유통공사), KOTRA(대한무역투자진흥공사)는
# 하루에도 서로 무관한 여러 사건에 대해 각각 보도자료를 내므로, 이름만
# 일치한다고 같은 사건으로 묶으면 완전히 다른 기사가 잘못 묶인다(실제
# news.json 검증에서 "aT" 하나로 인도 K-푸드페어 기사와 한우·한돈 싱가포르
# 기사가, "KOTRA" 하나로 덴마크 해조류 시장과 파라과이 라면 시장 동향
# 기사가 잘못 묶이는 것을 확인함). _extract_entities() 전용.
_LOW_SIGNAL_INSTITUTIONS = {"at", "kotra"}


def _normalize_for_feed_match(text):
    return re.sub(r"[^0-9a-z가-힣]", "", text.casefold())


# 업계 전문매체 RSS의 "appeared first on Just Food ." 같은 피드 자체
# boilerplate 문구가 영문 고유명사 정규식에 걸려 매체 시그니처가 개체명으로
# 오인식되는 것을 막는다 - 등록된 피드 이름과 일치하면 제외한다.
# _extract_entities() 전용.
_FEED_SOURCE_NAMES_NORMALIZED = {
    _normalize_for_feed_match(name)
    for name, _url in DOMESTIC_INDUSTRY_FEEDS + FOREIGN_INDUSTRY_FEEDS
}


def _extract_entities(title, summary):
    """
    dedupe_by_entity() 전용 개체명(회사/브랜드/제품명) 추출 헬퍼.

    완벽한 NER이 아닌 휴리스틱이다: 따옴표로 묶인 제품명, "OO그룹"/"㈜OOO"/
    "OOO 주식회사"/"OO사" 회사 표기, 영문 대문자 시작 고유명사, 제목 맨 앞
    "회사명," 관용구를 정규식으로 뽑는다. 매칭 결과는 대소문자/공백을
    정규화(casefold + strip)해 반환하므로 호출 측에서 다시 정규화하지 않는다.
    """
    text = f"{title or ''} {summary or ''}"
    entities = set()

    for m in _QUOTED_ENTITY_RE.finditer(text):
        entities.add(m.group(1))

    for m in _GROUP_SUFFIX_RE.finditer(text):
        entities.add(m.group(0))

    for m in _CORP_MARK_RE.finditer(text):
        entities.add(m.group(0).replace("㈜", "").strip())

    for m in _JUSIKHOESA_RE.finditer(text):
        entities.add(m.group(0))

    for m in _SA_SUFFIX_RE.finditer(text):
        word = m.group(0)
        if word not in _GENERIC_SA_WORDS:
            entities.add(word)

    for m in _ENGLISH_PROPER_NOUN_RE.finditer(text):
        word = m.group(0).strip()
        if len(word) >= 2 and word.upper() not in _ENGLISH_STOPWORDS:
            entities.add(word)

    lead_text = _LEADING_BRACKET_TAGS_RE.sub("", title or "")
    m = _LEADING_ENTITY_COMMA_RE.match(lead_text)
    if m:
        entities.add(m.group(1))

    normalized = {e.strip().casefold() for e in entities if e.strip()}
    normalized -= _LOW_SIGNAL_INSTITUTIONS
    normalized = {
        e for e in normalized
        if _normalize_for_feed_match(e) not in _FEED_SOURCE_NAMES_NORMALIZED
    }
    return normalized


def dedupe_by_entity(articles):
    """
    개체명(회사/브랜드/제품명) + 발행일 근접 기반 근접중복 제거 단일 authority.

    dedupe_near_duplicates()는 제목 문자열 자체의 유사도(0.75 이상)만 잡는다.
    매체마다 헤드라인을 독자적으로 재작성해 제목 유사도가 낮게 나오더라도,
    같은 회사/브랜드/제품을 다루는 기사가 ENTITY_DUP_DATE_WINDOW_DAYS(3일)
    이내에 몰려 있으면 같은 사건으로 간주해 여기서 추가로 잡는다.

    main()에서 dedupe_near_duplicates() 직후, dedupe_by_url() 이전에 단 한
    번만 호출한다. 개체명 추출/판정 로직을 다른 곳에 새로 만들지 말 것
    (관련도 판정이 여러 곳에 흩어졌던 과거 실수를 반복하지 않기 위함).

    같은 개체명이 매칭되는 기사가 2건 이상이고 서로 published_date가 3일
    이내면 하나의 클러스터로 묶어 summary가 가장 긴(정보량이 많다고 가정하는)
    1건만 남긴다. 개체명이 하나도 추출되지 않거나 published_date가 없는/
    파싱 불가능한 기사는 클러스터링 대상에서 제외하고 그대로 남긴다(과매칭
    방지를 위해 신호가 없으면 병합하지 않는 쪽을 택함).
    """
    entity_sets = [_extract_entities(a.get("title"), a.get("summary")) for a in articles]

    dates = []
    for a in articles:
        try:
            dates.append(datetime.strptime(a.get("published_date") or "", "%Y-%m-%d").date())
        except ValueError:
            dates.append(None)

    n = len(articles)
    parent = list(range(n))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(x, y):
        rx, ry = find(x), find(y)
        if rx != ry:
            parent[ry] = rx

    for i in range(n):
        if not entity_sets[i] or dates[i] is None:
            continue
        for j in range(i + 1, n):
            if not entity_sets[j] or dates[j] is None:
                continue
            if abs((dates[i] - dates[j]).days) > ENTITY_DUP_DATE_WINDOW_DAYS:
                continue
            if entity_sets[i] & entity_sets[j]:
                union(i, j)

    clusters = {}
    for idx in range(n):
        clusters.setdefault(find(idx), []).append(idx)

    kept = []
    for member_idxs in clusters.values():
        best_idx = max(member_idxs, key=lambda i: len(articles[i].get("summary") or ""))
        kept.append(articles[best_idx])
    return kept


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


def make_article_id(url: str) -> str:
    """기사 id 발급 단일 지점. url 기준 안정적인 해시라 재수집해도 동일
    기사는 같은 id를 유지하며, news_notify.py의 피드백 링크(feedback-good:/
    feedback-bad:{id})와 news_feedback.yml이 news.json에서 기사를 다시
    찾을 때 이 id로 조회한다."""
    return hashlib.md5((url or "").encode("utf-8")).hexdigest()[:10]


def main():
    raw_items = (
        fetch_google_news(DOMESTIC_GOOGLE_NEWS_QUERIES, "domestic")
        + fetch_google_news(FOREIGN_GOOGLE_NEWS_QUERIES, "foreign")
        + fetch_industry_feeds(DOMESTIC_INDUSTRY_FEEDS, "domestic")
        + fetch_industry_feeds(FOREIGN_INDUSTRY_FEEDS, "foreign")
    )
    raw_items = [it for it in raw_items if it["title"] and it["url"]]
    for it in raw_items:
        it["id"] = make_article_id(it["url"])

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
    source_scores = load_source_scores()

    relevant = []
    for raw in raw_items:
        ok, category, score, matched_keywords = is_relevant(raw["title"], raw["summary"])
        if not ok:
            continue
        # 최종 후보 리스트를 만드는 단 한 곳 - 신선도 필터는 여기서만 호출한다.
        if not is_fresh(raw["published_date"], run_date):
            continue
        item = dict(raw)
        item["category"] = category
        item["matched_keywords"] = matched_keywords
        item["keyword_score"] = score + priority_bonus(raw["origin"], raw["title"], raw["summary"])
        # 스코어링 파이프라인에서 피드백 가중치를 반영하는 단 한 지점.
        item["keyword_score"] = apply_learned_weight(item, source_scores)
        relevant.append(item)

    # 신선도 필터 통과 직후, URL 기준 중복 제거보다 먼저 근접중복(제목
    # 유사도 기반)을 제거한다 - 매체만 바뀐 동일 보도자료가 URL 중복방지를
    # 통과해 개별 기사로 남는 것을 여기서 잡는다.
    before_near_dup = len(relevant)
    relevant = dedupe_near_duplicates(relevant)
    near_dup_removed = before_near_dup - len(relevant)

    # 제목 유사도로는 못 잡는, 매체마다 헤드라인을 다르게 재작성한 근접중복을
    # 개체명(회사/브랜드/제품명) + 발행일 근접 기준으로 추가 제거한다.
    before_entity_dup = len(relevant)
    relevant = dedupe_by_entity(relevant)
    entity_dup_removed = before_entity_dup - len(relevant)

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
        f"제목 근접중복 제거 {near_dup_removed}건, 개체명 근접중복 제거 {entity_dup_removed}건, "
        f"wellness_trend {wellness_n} / trade_opportunity {trade_n}, "
        f"domestic {domestic_n} / foreign {foreign_n}) -> {OUT_PATH}"
    )
    if FETCH_ERRORS:
        print(f"[WARN] 일부 소스 fetch 실패(다른 소스로 대체 진행됨): {', '.join(FETCH_ERRORS)}", file=sys.stderr)


if __name__ == "__main__":
    main()
