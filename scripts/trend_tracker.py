#!/usr/bin/env python3
"""
gov-radar 뉴스 다이제스트 "편집" 보조 모듈
--------------------------------------------
news_notify.py가 기사를 그냥 나열하지 않고 어피티/더스쿱처럼 주제별로
묶고, 급상승 키워드를 짚어주고, 행사 캘린더를 뽑아 보여줄 수 있도록
순수 알고리즘/규칙 기반으로 계산하는 함수들을 모아둔다.

LLM API 호출은 절대 하지 않는다 - 전부 키워드 매칭, 정규식, Union-Find,
카운팅 같은 결정적 규칙으로만 구현한다(비용 발생 방지).

기존 정부지원사업 파이프라인(collector.py/notify.py/collect.yml/dismiss.yml)
및 피드백 학습 로직(source_scores.json 관련 코드)과는 완전히 독립적이며,
이 파일은 news.json/keyword_history.json만 다룬다.

제공 함수 (news_notify.py에서 호출):
  cluster_by_theme(articles)              -> 주제별 클러스터링
  count_today_keywords(articles)          -> 오늘 키워드 빈도 카운트
  load_keyword_history() / save_keyword_history(history) / prune_keyword_history(history, today)
  detect_trending_keywords(today_articles, history) -> 급상승 키워드
  extract_event_mentions(articles)        -> 행사/전시 캘린더
"""
import json
import re
from collections import Counter
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
KEYWORD_HISTORY_PATH = ROOT / "docs" / "data" / "keyword_history.json"

KEYWORD_HISTORY_RETENTION_DAYS = 14


# ---------------------------------------------------------------------------
# 1. 주제 클러스터링
# ---------------------------------------------------------------------------

# 클러스터링용 키워드가 이 개수 미만이면(대부분 matched_keywords가 1개뿐인
# 경우) 제목/요약에서 보조 토�큰을 추가로 뽑아 co-occurrence 신호를 보강한다.
MIN_KEYWORDS_FOR_CLUSTERING = 2

# 클러스터를 묶는 최소 co-occurrence(겹치는 키워드 개수) 기준
CLUSTER_MIN_SHARED_KEYWORDS = 2

# 이 비율(기사 전체 중 등장 비율)을 넘는 키워드는 "허브 키워드"로 보고
# 클러스터를 묶는 근거(co-occurrence)에서 제외한다. "수출"처럼 트레이드
# 관련 기사 절반 가까이에 등장하는 단어를 그대로 허용하면, 서로 무관한
# 기사들이 "수출"+우연히 겹치는 흔한 단어 하나만으로 사슬처럼 전부 한
# 클러스터로 합쳐지는(전이적 과병합) 문제가 실제 news.json 데이터에서
# 확인됐다(74건 중 30건이 "수출·푸드 이슈" 하나로 뭉침). 라벨에는 여전히
# 등장할 수 있다 - _cluster_label()은 이 제외 없이 전체 키워드로 집계한다.
HUB_KEYWORD_DOC_FREQ_RATIO = 0.2
HUB_KEYWORD_DOC_FREQ_MIN_COUNT = 3

_HANGUL_TOKEN_RE = re.compile(r"[가-힣]{2,}")
# 영단어 fallback 토큰은 최소 3자 - "in", "as", "has" 같은 2자 이하 기능어가
# 실제 뉴스 제목에서 하나로 뭉쳐 "has·in 이슈" 같은 무의미한 라벨을 만드는
# 것을 실측(news.json)으로 확인해 최소 길이를 올렸다.
_ENGLISH_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9]{2,}")

# 명사가 아닌 조사/부사/접속어 등, 클러스터링에 노이즈만 되는 흔한 토큰.
# 완벽한 형태소 분석이 아닌 규칙 기반 근사치이므로 실제 뉴스 제목에 자주
# 나오는 것 위주로 최소한만 걸러낸다. _extract_fallback_tokens() 전용.
_HANGUL_STOPWORDS = {
    "오늘", "내일", "어제", "기사", "한편", "관련", "위해", "이번", "지난",
    "통해", "대한", "보다", "것으로", "라고", "이라고", "에서", "에게",
    "으로", "부터", "까지", "대해", "따르면", "밝혔다", "전했다", "있다",
    "했다", "한다", "된다", "됐다", "위한", "따라", "가장", "최근", "다시",
    # 실제 news.json 클러스터링 결과 검증 중 "확대"+"대응" 두 단어만 겹쳐
    # 마늘 비축 기사와 무관한 프로바이오틱스 공장 증설 기사가 잘못 묶이는
    # 사례를 확인함 - 보도자료 제목에 흔한 범용 동사성 명사라 주제
    # 식별력이 없어 stopword에 추가.
    "확대", "대응", "추진", "강화",
}
_ENGLISH_STOPWORDS = {
    "THE", "THIS", "THAT", "THESE", "THOSE", "WITH", "FROM", "FOR", "AND",
    "NEW", "NEWS", "REPORT", "HAS", "HAVE", "HAD", "WAS", "WERE", "ARE",
    "ITS", "INTO", "ONTO", "OVER", "OFF", "OUT", "NOT", "BUT", "YOU", "ALL",
    "CAN", "WILL", "WHO", "HOW", "WHY", "WHEN", "WHAT", "WHERE", "AFTER",
    "AMID", "AMONG", "ABOUT", "THAN", "THEN", "THEIR", "THEY", "SAYS",
    "SAID", "ALSO", "MORE", "MOST", "SOME", "SUCH", "EACH", "BEEN", "ONLY",
}


def _extract_fallback_tokens(title, summary):
    """matched_keywords가 부족할 때 제목/요약에서 보조로 뽑는 2글자 이상
    한글 연속 토큰 + 영단어(3자 이상). cluster_by_theme() 전용 - 실제
    명사 여부를 보장하지 않는 규칙 기반 근사치다."""
    text = f"{title or ''} {summary or ''}"
    tokens = set()

    for m in _HANGUL_TOKEN_RE.finditer(text):
        tok = m.group(0)
        if tok not in _HANGUL_STOPWORDS:
            tokens.add(tok)

    for m in _ENGLISH_WORD_RE.finditer(text):
        tok = m.group(0)
        if tok.upper() not in _ENGLISH_STOPWORDS:
            tokens.add(tok)

    return tokens


def _cluster_keywords(article):
    """클러스터링에 쓸 기사 1건의 키워드 집합. matched_keywords를 우선
    쓰고, 부족하면 제목/요약에서 뽑은 보조 토큰으로 보강한다."""
    keywords = set(article.get("matched_keywords") or [])
    if len(keywords) < MIN_KEYWORDS_FOR_CLUSTERING:
        keywords |= _extract_fallback_tokens(article.get("title"), article.get("summary"))
    return keywords


def _cluster_label(member_idxs, keyword_sets):
    """클러스터 내에서 가장 자주 등장한 키워드 1~2개로 "{키워드1}·{키워드2}
    이슈" 라벨을 만든다."""
    counter = Counter()
    for idx in member_idxs:
        counter.update(keyword_sets[idx])
    top = [kw for kw, _ in counter.most_common(2)]
    if not top:
        return "기타"
    return "·".join(top) + " 이슈"


def _hub_keywords(keyword_sets):
    """너무 흔해서 "같은 주제"의 근거가 되지 못하는 키워드 집합을 계산한다.
    cluster_by_theme() 전용."""
    doc_freq = Counter()
    for kws in keyword_sets:
        doc_freq.update(kws)
    n = len(keyword_sets)
    cutoff = max(HUB_KEYWORD_DOC_FREQ_MIN_COUNT, int(n * HUB_KEYWORD_DOC_FREQ_RATIO))
    return {kw for kw, df in doc_freq.items() if df > cutoff}


def cluster_by_theme(articles):
    """
    주제 클러스터링 단일 authority.

    각 기사의 키워드 집합(_cluster_keywords)에서 허브 키워드(_hub_keywords)
    를 제외하고도 2개 이상 겹치는 기사끼리 Union-Find로 묶는다. 허브
    키워드만으로는 절대 묶이지 않는다 - "수출"처럼 절반 가까운 기사에
    등장하는 단어 하나로 서로 무관한 기사들이 사슬처럼 전부 합쳐지는
    과병합을 막기 위함(라벨에는 여전히 등장할 수 있다 - _cluster_label()
    은 이 제외 없이 전체 키워드로 집계한다).

    다른 기사와 하나도 묶이지 못한 단독 기사는 "기타" 클러스터로 모아
    맨 뒤에 붙인다(리스트 마지막 원소).

    Returns:
        list[{"theme_label": str, "articles": list[dict]}] - 클러스터는
        소속 기사 수 내림차순으로 정렬되고, "기타"가 있으면 항상 마지막.
    """
    n = len(articles)
    keyword_sets = [_cluster_keywords(a) for a in articles]
    hub_keywords = _hub_keywords(keyword_sets)

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
        if not keyword_sets[i]:
            continue
        for j in range(i + 1, n):
            if not keyword_sets[j]:
                continue
            shared = (keyword_sets[i] & keyword_sets[j]) - hub_keywords
            if len(shared) >= CLUSTER_MIN_SHARED_KEYWORDS:
                union(i, j)

    groups = {}
    for idx in range(n):
        groups.setdefault(find(idx), []).append(idx)

    clusters = []
    misc_idxs = []
    for member_idxs in groups.values():
        if len(member_idxs) < 2:
            misc_idxs.extend(member_idxs)
            continue
        clusters.append({
            "theme_label": _cluster_label(member_idxs, keyword_sets),
            "articles": [articles[i] for i in member_idxs],
        })

    clusters.sort(key=lambda c: -len(c["articles"]))

    if misc_idxs:
        clusters.append({
            "theme_label": "기타",
            "articles": [articles[i] for i in sorted(misc_idxs)],
        })

    return clusters


# ---------------------------------------------------------------------------
# 2. 급상승 키워드 감지
# ---------------------------------------------------------------------------

TRENDING_LOOKBACK_DAYS = 7
TRENDING_MULTIPLIER = 2.0
TRENDING_MIN_TODAY_MENTIONS = 2
TRENDING_MAX_RESULTS = 3


def count_today_keywords(articles):
    """오늘 기사들의 matched_keywords 빈도 카운트 단일 authority.
    keyword_history.json 누적과 detect_trending_keywords() 판정 모두
    이 함수의 결과만 사용한다(따로 다시 세지 않는다)."""
    counter = Counter()
    for a in articles:
        counter.update(a.get("matched_keywords") or [])
    return dict(counter)


def load_keyword_history():
    if not KEYWORD_HISTORY_PATH.exists():
        return {}
    try:
        return json.loads(KEYWORD_HISTORY_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def prune_keyword_history(history, today):
    """최근 KEYWORD_HISTORY_RETENTION_DAYS(14)일만 남기고 그 이전 날짜는
    삭제한다."""
    cutoff = (today - timedelta(days=KEYWORD_HISTORY_RETENTION_DAYS)).isoformat()
    return {d: counts for d, counts in history.items() if d >= cutoff}


def save_keyword_history(history):
    KEYWORD_HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    KEYWORD_HISTORY_PATH.write_text(
        json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def detect_trending_keywords(today_articles, history):
    """
    급상승 키워드 판정 단일 authority.

    오늘 키워드별 언급 횟수(count_today_keywords)를 세고, 최근 7일(오늘
    이전) 평균 대비 오늘 언급이 2배 이상이며 오늘 언급이 최소 2건 이상인
    키워드를 "급상승"으로 판정한다. history에 아직 오늘 날짜가 반영되기
    전 상태를 넘겨야 한다(오늘 자신은 "최근 7일 평균" 계산에 포함하지
    않는다).

    최근 7일간 해당 키워드의 이력이 전혀 없으면(평균 0) "몇 배 급증"인지
    계산할 수 없으므로 급상승으로 판정하지 않는다 - keyword_history.json이
    아직 충분히 쌓이지 않은 서비스 초반에는 이 함수가 항상 빈 리스트를
    반환할 수 있다(데이터 부족을 급상승으로 오판하지 않기 위한 의도적
    설계).

    Returns:
        list[str] - 오늘 언급 횟수 내림차순, 최대 3개.
    """
    today_counts = count_today_keywords(today_articles)
    today = date.today()
    recent_days = [(today - timedelta(days=i)).isoformat() for i in range(1, TRENDING_LOOKBACK_DAYS + 1)]

    trending = []
    for kw, today_n in today_counts.items():
        if today_n < TRENDING_MIN_TODAY_MENTIONS:
            continue
        past_values = [history.get(d, {}).get(kw, 0) for d in recent_days]
        avg_past = sum(past_values) / len(recent_days)
        if avg_past <= 0:
            continue
        if today_n >= avg_past * TRENDING_MULTIPLIER:
            trending.append((kw, today_n))

    trending.sort(key=lambda x: -x[1])
    return [kw for kw, _ in trending[:TRENDING_MAX_RESULTS]]


# ---------------------------------------------------------------------------
# 3. 행사/전시 캘린더 자동 추출
# ---------------------------------------------------------------------------

EVENT_LOOKAHEAD_DAYS = 30

# 행사/전시 신호 키워드. 대소문자 무관하게 매칭(.lower() 비교) - 한글은
# lower()가 항등 연산이라 영단어(expo)만 실질적으로 영향을 받는다.
_EVENT_KEYWORDS = ["전시", "박람회", "페스티벌", "팝업", "행사", "엑스포", "expo"]

# "8.20~8.22", "8.20-8.22" 형태
_DATE_RANGE_DOT_RE = re.compile(r"(\d{1,2})\.(\d{1,2})\s*[~\-–]\s*\d{1,2}\.\d{1,2}")
# "8월 20일~22일", "8월 20일~9월 2일" 형태
_DATE_RANGE_KOR_RE = re.compile(r"(\d{1,2})월\s*(\d{1,2})일?\s*[~\-–]\s*(?:\d{1,2}월\s*)?\d{1,2}일?")
# 범위 없이 "9월" 처럼 달만 언급된 형태 (위 두 패턴에 이미 매칭된 부분은
# _find_date_mention()에서 우선순위로 걸러진다)
_DATE_BARE_MONTH_RE = re.compile(r"(\d{1,2})월")


def _find_date_mention(text):
    """텍스트에서 날짜 패턴을 찾아 (원문 매치, 시작월, 시작일)을 반환한다.
    범위 패턴을 우선 시도하고, 없으면 "MM월" 단독 언급을 그 달 1일로
    간주해 반환한다(구체적인 일자가 없는 근사치). 못 찾으면 None.
    extract_event_mentions() 전용."""
    m = _DATE_RANGE_DOT_RE.search(text)
    if m:
        return m.group(0), int(m.group(1)), int(m.group(2))

    m = _DATE_RANGE_KOR_RE.search(text)
    if m:
        return m.group(0), int(m.group(1)), int(m.group(2))

    m = _DATE_BARE_MONTH_RE.search(text)
    if m:
        return m.group(0), int(m.group(1)), 1

    return None


def _resolve_start_date(month, day, today):
    """(월, 일)을 오늘 기준으로 가장 가까운 미래의 실제 날짜로 변환한다.
    올해 날짜가 이미 6개월 이상 지났으면 해를 넘긴 행사로 보고 내년으로
    보정한다. 2/29처럼 존재하지 않는 날짜 등 파싱 불가능하면 None."""
    if not (1 <= month <= 12 and 1 <= day <= 31):
        return None
    for year in (today.year, today.year + 1):
        try:
            candidate = date(year, month, day)
        except ValueError:
            continue
        if (candidate - today).days >= -1:
            return candidate
    return None


def _normalize_event_name(name):
    return re.sub(r"\s+", " ", (name or "")).strip().casefold()


def extract_event_mentions(articles):
    """
    행사/전시 캘린더 추출 단일 authority.

    제목/요약에 행사 키워드(_EVENT_KEYWORDS)와 날짜 패턴이 "함께" 나타나는
    기사만 이벤트 후보로 본다 - 둘 중 하나만 있으면 오탐 위험이 커서
    제외한다(예: 날짜만 있는 일반 기사, 행사 키워드만 있고 일정이 없는
    회고성 기사). 오늘 기준 0~30일 이내에 시작하는 것만 남기고, 같은
    행사명(정규화한 기사 제목 기준) 반복 언급은 제거한다.

    Returns:
        list[{"name": str, "date_range": str, "source": str}] - 시작일
        기준 오름차순 정렬.
    """
    today = date.today()
    seen_names = set()
    events = []

    for a in articles:
        text = f"{a.get('title') or ''} {a.get('summary') or ''}"
        lowered = text.lower()
        if not any(kw in lowered for kw in _EVENT_KEYWORDS):
            continue

        found = _find_date_mention(text)
        if not found:
            continue
        date_range_text, month, day = found

        start = _resolve_start_date(month, day, today)
        if start is None:
            continue
        days_until = (start - today).days
        if not (0 <= days_until <= EVENT_LOOKAHEAD_DAYS):
            continue

        name = (a.get("title") or "").strip()
        dedupe_key = _normalize_event_name(name)
        if not dedupe_key or dedupe_key in seen_names:
            continue
        seen_names.add(dedupe_key)

        events.append({
            "name": name,
            "date_range": date_range_text,
            "source": a.get("source") or "",
            "_start": start,
        })

    events.sort(key=lambda e: e["_start"])
    for e in events:
        del e["_start"]
    return events
