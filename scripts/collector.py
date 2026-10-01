#!/usr/bin/env python3
"""
gov-radar collector
--------------------
정부/공공기관 지원사업·공고 정보를 수집해 docs/data/programs.json 으로 저장한다.

데이터 소스
  1) K-Startup (창업진흥원) 공고정보 오픈API  - 청창사(청년창업사관학교) 포함, 공공데이터포털에서 인증키 발급
     https://www.data.go.kr/data/15125364/openapi.do
  2) 기업마당(bizinfo.go.kr) 지원사업정보 API - 중앙부처/지자체/유관기관 통합 공고
     https://www.bizinfo.go.kr/apiDetail.do?id=bizinfoApi
  3) 서울시 "내손안에 서울" 경제 카테고리 RSS (news.seoul.go.kr/economy/feed)
     서울시 고시공고 게시판은 공개 RSS가 없어(직접 접속 검증: 전부 404/SPA) 서울시
     공식 뉴스 채널로 대체, 소상공인/창업/지원 키워드가 포함된 기사만 선별한다.
  4) SBA(서울경제진흥원) - 자체 RSS 미제공(직접 접속 검증: /rss 등 전부 301 후 SPA),
     Google News "site:sba.seoul.kr" 쿼리로 대체 수집
  5) 서울창업허브(startup-plus.kr) - 자체 RSS 미제공(직접 접속 검증: /feed, /rss 전부
     302 후 빈 응답), Google News "site:startup-plus.kr" 쿼리로 대체 수집
     (3~5는 API 키 불필요, PEQUOD가 서울 소재 1인기업이라는 점을 반영해 추가)

필요 환경변수 (GitHub Actions Secrets 로 주입)
  KSTARTUP_API_KEY   : data.go.kr 에서 발급받은 K-Startup 서비스키 (Decoding 키)
  BIZINFO_API_KEY    : bizinfo.go.kr 에서 발급받은 crtfcKey

실행:
  python scripts/collector.py
출력:
  docs/data/programs.json
"""
import email.utils
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import quote
from xml.etree import ElementTree

import requests

ROOT = Path(__file__).resolve().parent.parent
OUT_PATH = ROOT / "docs" / "data" / "programs.json"
DISMISSED_PATH = ROOT / "docs" / "data" / "dismissed.json"

KSTARTUP_KEY = os.environ.get("KSTARTUP_API_KEY", "")
BIZINFO_KEY = os.environ.get("BIZINFO_API_KEY", "")

KSTARTUP_URL = "https://apis.data.go.kr/B552735/kisedKstartupService01/getAnnouncementInformation01"
BIZINFO_URL = "https://www.bizinfo.go.kr/uss/rss/bizinfoApi.do"

# 우선순위 프로그램 키워드 - Dean(1인 기업, 식품 수입유통, 글로벌 무역) 관심사 기준
# 필요에 따라 이 리스트만 수정하면 대시보드/이메일 필터에 바로 반영됨
PRIORITY_KEYWORDS = [
    # 청창사(청년창업사관학교)
    "청년창업사관학교", "청창사", "창업사관학교",
    # 1인기업 / 개인 창업자 대상 일반 프로그램
    "1인 창조기업", "1인창조기업", "1인기업", "예비창업", "초기창업", "재도전",
    # 식품 / 농식품
    "식품", "농식품", "먹거리", "외식",
    # 수출 / 해외진출 / 무역
    "수출바우처", "수출", "해외진출", "글로벌강소기업", "무역", "바이어", "해외마케팅",
    # "강남취창업허브센터 2026 정부지원사업" 자료 반영 - 공식 사업명 전체 리스트
    # (이미 위에 있는 청년창업사관학교/청창사/1인 창조기업/수출바우처는 중복 추가하지 않음)
    "모두의 창업", "예비창업패키지", "초기창업패키지", "창업도약패키지", "창업중심대학",
    "글로벌창업사관학교", "민관공동 TIPS", "팁스", "TIPS", "초격차 스타트업 프로젝트",
    "혁신창업사업화자금", "혁신소상공인", "강한소상공인", "로컬크리에이터", "글로벌소상공인",
    "K-소상공인", "소상공인 온라인판로", "희망리턴패키지", "재도전성공패키지", "재창업자금",
    "글로벌비즈니스센터", "K-스타트업센터", "관세대응 패키지",
]

# 제목에 포함되면 무조건 제외 (자격요건상 Dean이 지원 불가한 대상)
EXCLUDE_KEYWORDS = [
    "여성",  # 여성기업/여성창업 전용 프로그램
]

# 참여 가능 지역 - PEQUOD는 서울 소재 1인기업.
# region 문자열/제목에서 추출한 지역 태그가 아래 중 하나에만 해당하면
# 지역 제한 없이 지원 가능한 것으로 간주. 그 외 지역명이 하나라도 섞여 있으면
# 특정 지자체/특정 건물 입주기업 전용 공고로 보고 자동 배제한다.
ELIGIBLE_REGION_HINTS = ["전국", "서울", "수도권"]

# 제목 맨 앞 "[XX] ..." 형태의 대괄호 지역 태그
_BRACKET_REGION_RE = re.compile(r"^\s*\[([^\]]+)\]")

# K-Startup/기업마당 API의 region(supt_regin/area) 필드는 신뢰도가 낮아,
# 실제로는 특정 지자체/특정 건물 입주기업 전용 공고인데도 "전국"으로 잘못
# 태그되는 경우가 흔하다 (예: "[전북] ...", "강남구 개포동지역 ... 입주기업 모집").
# 이런 공고는 대부분 제목에 지자체명이 직접 노출되므로, region 필드와 별개로
# 제목에서도 지역명을 추출해 함께 판단한다.
#
# 광역자치단체(서울 제외 - 서울은 참여 가능 지역이므로 ELIGIBLE_REGION_HINTS 로 처리)
PROVINCE_NAMES = [
    "부산", "대구", "인천", "광주", "대전", "울산", "세종",
    "경기", "강원", "충북", "충남", "전북", "전남", "경북", "경남", "제주",
]

# 기초자치단체(시/군/구) 및 대표적인 동 단위 사례.
# "중구", "동구", "서구", "남구", "북구" 처럼 여러 도시에서 반복되고 일반 단어와도
# 겹치는(예: "서구" = 서구권/Western) 이름은 오탐 위험이 커서 제외했다 - 완전한 목록은
# 아니지만 실제 공고 제목에 자주 등장하는 이름 위주로 최대한 커버한다.
DISTRICT_NAMES = [
    # 서울 자치구
    "강남구", "강동구", "강북구", "강서구", "관악구", "광진구", "구로구", "금천구",
    "노원구", "도봉구", "동대문구", "동작구", "마포구", "서대문구", "서초구", "성동구",
    "성북구", "송파구", "양천구", "영등포구", "용산구", "은평구", "종로구", "중랑구",
    # 부산
    "부산진구", "해운대구", "사하구", "금정구", "연제구", "수영구", "사상구", "기장군", "영도구",
    # 대구
    "달서구", "달성군",
    # 인천
    "미추홀구", "연수구", "남동구", "부평구", "계양구", "강화군", "옹진군",
    # 광주
    "광산구",
    # 대전
    "유성구", "대덕구",
    # 울산
    "울주군",
    # 경기
    "수원시", "성남시", "의정부시", "안양시", "부천시", "광명시", "평택시", "동두천시",
    "안산시", "고양시", "과천시", "구리시", "남양주시", "오산시", "시흥시", "군포시",
    "의왕시", "하남시", "용인시", "파주시", "이천시", "안성시", "김포시", "화성시",
    "양주시", "포천시", "여주시", "연천군", "가평군", "양평군",
    # 강원
    "춘천시", "원주시", "강릉시", "동해시", "태백시", "속초시", "삼척시",
    "홍천군", "횡성군", "영월군", "평창군", "정선군", "철원군", "화천군", "양구군", "인제군", "고성군", "양양군",
    # 충북
    "청주시", "충주시", "제천시", "보은군", "옥천군", "영동군", "증평군", "진천군", "괴산군", "음성군", "단양군",
    # 충남
    "천안시", "공주시", "보령시", "아산시", "서산시", "논산시", "계룡시", "당진시",
    "금산군", "부여군", "서천군", "청양군", "홍성군", "예산군", "태안군",
    # 전북
    "전주시", "군산시", "익산시", "정읍시", "남원시", "김제시",
    "완주군", "진안군", "무주군", "장수군", "임실군", "순창군", "고창군", "부안군",
    # 전남
    "목포시", "여수시", "순천시", "나주시", "광양시",
    "담양군", "곡성군", "구례군", "고흥군", "보성군", "화순군", "장흥군", "강진군",
    "해남군", "영암군", "무안군", "함평군", "영광군", "장성군", "완도군", "진도군", "신안군",
    # 경북
    "포항시", "경주시", "김천시", "안동시", "구미시", "영주시", "영천시", "상주시", "문경시", "경산시",
    "군위군", "의성군", "청송군", "영양군", "영덕군", "청도군", "고령군", "성주군", "칠곡군", "예천군",
    "봉화군", "울진군", "울릉군",
    # 경남
    "창원시", "진주시", "통영시", "사천시", "김해시", "밀양시", "거제시", "양산시",
    "의령군", "함안군", "창녕군", "남해군", "하동군", "산청군", "함양군", "거창군", "합천군",
    # 제주
    "제주시", "서귀포시",
    # 특정 건물/센터 입주기업 대상 공고에서 자주 등장하는 동 단위 사례
    "개포동",
]


def parse_date(raw: str):
    """YYYYMMDD, YYYY-MM-DD 등 다양한 포맷을 YYYY-MM-DD 로 정규화"""
    if not raw:
        return None
    raw = raw.strip()
    digits = re.sub(r"[^0-9]", "", raw)
    if len(digits) == 8:
        try:
            return datetime.strptime(digits, "%Y%m%d").strftime("%Y-%m-%d")
        except ValueError:
            return None
    return None


def make_id(source: str, title: str, end_date: str):
    raw = f"{source}|{title}|{end_date or ''}"
    return hashlib.md5(raw.encode("utf-8")).hexdigest()[:10]


def extract_region_tags(title: str) -> list[str]:
    """제목에서 지역 힌트를 모두 추출한다.

    1) 제목 맨 앞 "[XX] ..." 대괄호 지역 태그를 우선 추출
    2) 대괄호 유무와 무관하게 제목 전체에서 알려진 지자체명(광역/기초)을 추가 추출

    region API 필드가 "전국"으로 잘못 태그된 지자체/특정 건물 전용 공고를
    걸러내기 위한 용도.
    """
    if not title:
        return []

    tags = []
    m = _BRACKET_REGION_RE.match(title)
    if m:
        tags.append(m.group(1).strip())

    for name in PROVINCE_NAMES + DISTRICT_NAMES:
        if name in title and name not in tags:
            tags.append(name)

    return tags


# PEQUOD는 서울 소재 1인기업이므로 서울 25개 자치구 단위로 한정된 공고
# (예: "강남구 개포동지역 ... 입주기업 모집")도 region 필드와 무관하게 지원
# 가능 지역으로 간주한다. 그 외 지자체(부산/경기/...) 는 여전히 배제 대상.
SEOUL_DISTRICTS = [
    "강남구", "강동구", "강북구", "강서구", "관악구", "광진구", "구로구", "금천구",
    "노원구", "도봉구", "동대문구", "동작구", "마포구", "서대문구", "서초구", "성동구",
    "성북구", "송파구", "양천구", "영등포구", "용산구", "은평구", "종로구", "중구", "중랑구",
]

# "중구"는 서울 외에도 인천/대전/울산/대구/부산에 동일한 구명이 있어 단독으로는
# 오탐 위험이 크다 (DISTRICT_NAMES에서도 같은 이유로 제외돼 extract_region_tags가
# 추출하지 않음). "서울"이 함께 언급된 경우에만 서울 자치구로 인정한다.
_AMBIGUOUS_SEOUL_DISTRICTS = {"중구"}


def _has_seoul_district(region: str, title: str) -> bool:
    """region 문자열 또는 extract_region_tags(title) 결과에 서울 자치구명이
    하나라도 포함되면 True."""
    haystacks = [region or ""] + extract_region_tags(title)
    for d in SEOUL_DISTRICTS:
        if d in _AMBIGUOUS_SEOUL_DISTRICTS:
            if "서울" in (title or "") and d in (title or ""):
                return True
            continue
        if any(d in h for h in haystacks):
            return True
    return False


def is_region_eligible(region: str, title: str):
    """region 필드와 제목에서 추출한 지역 태그를 함께 판단하는 단일 진입점.

    region 필드, 그리고 extract_region_tags(title) 결과 중 하나라도
    ELIGIBLE_REGION_HINTS(전국/서울/수도권) 이외의 지역명이면 False.
    단, 서울 자치구(SEOUL_DISTRICTS) 단위로 한정된 공고는 예외적으로 True.
    """
    if _has_seoul_district(region, title):
        return True
    if region and not any(hint in region for hint in ELIGIBLE_REGION_HINTS):
        return False
    for tag in extract_region_tags(title):
        if not any(hint in tag for hint in ELIGIBLE_REGION_HINTS):
            return False
    return True


# 사업 단계 태깅 - 하드 필터 아님, 대시보드/이메일에 참고용으로만 노출한다.
# 청창사/글로벌창업사관학교처럼 고유 프로그램명을 먼저 확인하고, 그 외에는
# 예비/초기/도약 같은 일반 단계 키워드로 판정한다.
def tag_stage(title: str):
    """제목에서 사업 단계를 추출한다. 매칭되는 키워드가 없으면 None."""
    if not title:
        return None
    if "청년창업사관학교" in title or "청창사" in title:
        return "청창사"
    if "글로벌창업사관학교" in title:
        return "글로벌"
    if "예비창업" in title:
        return "예비"
    if "초기창업" in title:
        return "초기"
    if "도약" in title:
        return "도약"
    return None


# 같은 기관(org)의 공고가 dismissed_orgs 에서 이 횟수 이상 누적되면
# 이후 수집분은 자동으로 is_priority=False 처리한다.
ORG_BLACKLIST_THRESHOLD = 3

DISMISSED_IDS = set()
DISMISSED_ORGS = {}


def is_org_blacklisted(org: str) -> bool:
    if not org:
        return False
    return DISMISSED_ORGS.get(org, 0) >= ORG_BLACKLIST_THRESHOLD


def compute_priority(title: str, region: str, org: str = ""):
    if any(k in title for k in EXCLUDE_KEYWORDS):
        return False
    if not is_region_eligible(region, title):
        return False
    if is_org_blacklisted(org):
        return False
    return any(k in title for k in PRIORITY_KEYWORDS)


def load_dismissed():
    """docs/data/dismissed.json 을 읽어 전역 DISMISSED_IDS/DISMISSED_ORGS 를 채운다."""
    global DISMISSED_IDS, DISMISSED_ORGS
    if not DISMISSED_PATH.exists():
        return
    try:
        data = json.loads(DISMISSED_PATH.read_text(encoding="utf-8"))
        DISMISSED_IDS = set(data.get("dismissed_ids", []))
        DISMISSED_ORGS = data.get("dismissed_orgs", {})
    except Exception:
        DISMISSED_IDS = set()
        DISMISSED_ORGS = {}


FETCH_ERRORS = []  # 소스별 fetch 예외(타임아웃 등) 기록 - 빈 결과와 구분하기 위함


def fetch_kstartup():
    """K-Startup 공고정보 오픈API 호출"""
    if not KSTARTUP_KEY:
        print("[WARN] KSTARTUP_API_KEY 미설정 - K-Startup 수집 skip", file=sys.stderr)
        return []

    items = []
    params = {
        "serviceKey": KSTARTUP_KEY,
        "page": 1,
        "perPage": 100,
        "returnType": "JSON",
    }
    try:
        resp = requests.get(KSTARTUP_URL, params=params, timeout=20)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        print(f"[ERROR] K-Startup fetch 실패: {e}", file=sys.stderr)
        FETCH_ERRORS.append("K-Startup")
        return []

    rows = data.get("data") or data.get("items") or []
    for row in rows:
        title = (row.get("biz_pbanc_nm") or row.get("intg_pbanc_biz_nm") or "").strip()
        start = parse_date(row.get("pbanc_rcpt_bgng_dt", ""))
        end = parse_date(row.get("pbanc_rcpt_end_dt", ""))
        org = (row.get("pbanc_ntrp_nm") or row.get("sprv_inst") or "창업진흥원").strip()
        url = row.get("detl_pg_url") or "https://www.k-startup.go.kr"
        region = row.get("supt_regin") or "전국"

        items.append({
            "id": make_id("K-Startup", title, end),
            "source": "K-Startup",
            "title": title,
            "org": org,
            "region": region,
            "start_date": start,
            "end_date": end,
            "url": url,
            "is_priority": compute_priority(title, region, org),
        })
    return items


def fetch_bizinfo():
    """기업마당 지원사업정보 API 호출 (XML 응답)"""
    if not BIZINFO_KEY:
        print("[WARN] BIZINFO_API_KEY 미설정 - 기업마당 수집 skip", file=sys.stderr)
        return []

    items = []
    params = {
        "crtfcKey": BIZINFO_KEY,
        "dataType": "xml",
        "searchCnt": 100,
    }
    try:
        resp = requests.get(BIZINFO_URL, params=params, timeout=20)
        resp.raise_for_status()
        root = ElementTree.fromstring(resp.content)
    except Exception as e:
        print(f"[ERROR] 기업마당 fetch 실패: {e}", file=sys.stderr)
        FETCH_ERRORS.append("기업마당")
        return []

    for item in root.iter("item"):
        def g(tag):
            el = item.find(tag)
            return el.text.strip() if el is not None and el.text else ""

        title = g("pblancNm")
        period = g("reqstBeginEndDe")  # 예: "20260101 ~ 20260131"
        start, end = None, None
        if "~" in period:
            parts = [p.strip() for p in period.split("~")]
            if len(parts) == 2:
                start, end = parse_date(parts[0]), parse_date(parts[1])

        region = g("area") or "전국"
        org = g("jrsdInsttNm") or g("excInsttNm") or "기업마당"

        items.append({
            "id": make_id("기업마당", title, end),
            "source": "기업마당",
            "title": title,
            "org": org,
            "region": region,
            "start_date": start,
            "end_date": end,
            "url": g("pblancUrl") or "https://www.bizinfo.go.kr",
            "is_priority": compute_priority(title, region, org),
        })
    return items


# --- 서울시 전용 포털 소스 (3개) -----------------------------------------
# PEQUOD가 서울 소재 1인기업이라는 점을 반영해 추가. 직접 접속해 RSS 유효성을
# 확인한 결과는 아래 각 함수 docstring 참고. 전부 API 키 불필요.

SEOUL_CITY_RSS_URL = "https://news.seoul.go.kr/economy/feed"

# Google News는 결과 제목을 "<원제목> - <발행처>" 형태로 내려주므로 <source>
# 태그의 발행처명을 이용해 접미사를 제거한다.
def _clean_google_news_title(raw_title: str, source_name: str) -> str:
    title = (raw_title or "").strip()
    suffix = f" - {source_name}" if source_name else ""
    if suffix and title.endswith(suffix):
        title = title[: -len(suffix)].strip()
    return title


def _parse_rss_date(raw: str):
    """RSS 표준 RFC 822 pubDate 포맷을 YYYY-MM-DD 로 정규화."""
    if not raw:
        return None
    try:
        return email.utils.parsedate_to_datetime(raw).strftime("%Y-%m-%d")
    except Exception:
        return None


# Google News의 site: 검색은 사이트명/메뉴 같은 정적 랜딩 페이지도 함께 반환하므로
# (예: "서울경제진흥원 - sba.seoul", "서울시 통합 창업 플랫폼 - startup-plus.kr")
# 실제 공고로 보이는 제목만 남기기 위한 키워드 필터.
ANNOUNCEMENT_HINT_WORDS = ["공고", "모집", "선정", "접수", "지원사업", "신청"]

# 서울시 "내손안에 서울" 경제 RSS는 패션위크/한강 행사 등 소상공인·창업 지원과
# 무관한 일반 경제 뉴스도 섞여 있어 아래 키워드가 포함된 기사만 남긴다.
SEOUL_CITY_RELEVANT_WORDS = ["소상공인", "창업", "지원"]


def fetch_google_news_fallback(query: str, source_tag: str, hint_words):
    """공식 RSS가 없는 서울시 산하 포털을 Google News의 site: 연산자로 대체 수집."""
    url = f"https://news.google.com/rss/search?q={quote(query)}&hl=ko&gl=KR&ceid=KR:ko"
    items = []
    try:
        resp = requests.get(url, timeout=20, headers={"User-Agent": "Mozilla/5.0"})
        resp.raise_for_status()
        root = ElementTree.fromstring(resp.content)
    except Exception as e:
        print(f"[ERROR] {source_tag} fetch 실패: {e}", file=sys.stderr)
        FETCH_ERRORS.append(source_tag)
        return []

    for item in root.iter("item"):
        def g(tag):
            el = item.find(tag)
            return el.text.strip() if el is not None and el.text else ""

        source_el = item.find("source")
        source_name = (source_el.text or "").strip() if source_el is not None else ""
        title = _clean_google_news_title(g("title"), source_name)
        if not title or not any(w in title for w in hint_words):
            continue

        region = "서울"
        end = _parse_rss_date(g("pubDate"))
        items.append({
            "id": make_id(source_tag, title, end),
            "source": source_tag,
            "title": title,
            "org": source_tag,
            "region": region,
            "start_date": None,
            "end_date": end,
            "url": g("link") or f"https://news.google.com/search?q={quote(query)}",
            "is_priority": compute_priority(title, region, source_tag),
        })
    return items


def fetch_sba():
    """서울경제진흥원(SBA) 공지사항.

    직접 접속 검증 결과 sba.seoul.kr/rss, /kr/bbs/rssFeed.do 등 전부 301
    리다이렉트 후 SPA 홈으로 떨어지고 RSS 자동탐색 링크도 없어 자체 RSS를
    제공하지 않는다. Google News "site:sba.seoul.kr" 쿼리로 대체 수집한다
    (직접 접속 검증 결과 "「2026년 SBA×굿윌스토어 굿파트너스」참여기업 3차
    모집 공고" 등 실제 공고가 반환됨).
    """
    return fetch_google_news_fallback(
        "site:sba.seoul.kr (공고 OR 모집 OR 지원사업)",
        "SBA(서울경제진흥원)",
        ANNOUNCEMENT_HINT_WORDS,
    )


def fetch_startup_hub():
    """서울창업허브(startup-plus.kr).

    직접 접속 검증 결과 /feed, /rss 전부 302 리다이렉트 후 빈 응답(size 0)이라
    RSS를 제공하지 않는다. Google News "site:startup-plus.kr" 쿼리로 대체
    수집한다 (직접 접속 검증 결과 "서울 AI 허브 멤버십 모집", "벤처확인
    도전기업 일대일 비대면 밋업 참여기업 모집" 등 실제 공고가 반환됨).
    """
    return fetch_google_news_fallback(
        "site:startup-plus.kr (공고 OR 모집 OR 지원사업)",
        "서울창업허브",
        ANNOUNCEMENT_HINT_WORDS,
    )


def fetch_seoul_city():
    """서울시 "내손안에 서울" 경제 카테고리 RSS.

    서울시 고시공고 게시판은 공개 RSS/자동탐색 링크가 없고(직접 접속 검증:
    홈페이지가 JS 네비게이션 기반 SPA) news.seoul.go.kr(서울시 공식 뉴스
    채널) 의 경제 카테고리 RSS(/economy/feed, 직접 접속 검증 결과 정상 응답)
    만 유효해 이를 사용한다. 소상공인/창업/지원 키워드가 포함된 기사만
    남긴다.
    """
    try:
        resp = requests.get(SEOUL_CITY_RSS_URL, timeout=20, headers={"User-Agent": "Mozilla/5.0"})
        resp.raise_for_status()
        root = ElementTree.fromstring(resp.content)
    except Exception as e:
        print(f"[ERROR] 서울시청 fetch 실패: {e}", file=sys.stderr)
        FETCH_ERRORS.append("서울시청")
        return []

    items_out = []
    for item in root.iter("item"):
        def g(tag):
            el = item.find(tag)
            return el.text.strip() if el is not None and el.text else ""

        title = g("title")
        if not title or not any(w in title for w in SEOUL_CITY_RELEVANT_WORDS):
            continue

        region = "서울"
        end = _parse_rss_date(g("pubDate"))
        items_out.append({
            "id": make_id("서울시청", title, end),
            "source": "서울시청",
            "title": title,
            "org": "서울시",
            "region": region,
            "start_date": None,
            "end_date": end,
            "url": g("link") or "https://news.seoul.go.kr",
            "is_priority": compute_priority(title, region, "서울시"),
        })
    return items_out


def dedupe(items):
    seen = set()
    out = []
    for it in items:
        key = (it["title"], it.get("end_date"))
        if key in seen:
            continue
        seen.add(key)
        out.append(it)
    return out


def main():
    load_dismissed()  # DISMISSED_IDS/DISMISSED_ORGS 채움 - fetch_* 의 compute_priority 가 사용

    collected = (
        fetch_kstartup() + fetch_bizinfo()
        + fetch_sba() + fetch_startup_hub() + fetch_seoul_city()
    )
    collected = [c for c in collected if c["title"]]
    collected = dedupe(collected)
    collected = [c for c in collected if c["id"] not in DISMISSED_IDS]

    # 사업 단계 태깅 (하드 필터 아님, 표시용) - 모든 소스에 동일하게 적용
    for c in collected:
        c["stage"] = tag_stage(c["title"])

    # 마감일 기준 정렬 (없는 항목은 뒤로)
    collected.sort(key=lambda x: x.get("end_date") or "9999-99-99")

    if not collected and FETCH_ERRORS:
        # 모든 fetch가 타임아웃/네트워크 오류로 실패한 경우: 정상적으로 "0건"인 것과 구분해
        # 기존에 수집돼 있던 programs.json을 빈 데이터로 덮어쓰지 않는다.
        print(
            f"[ERROR] 모든 소스 fetch 실패({', '.join(FETCH_ERRORS)}) - "
            f"기존 {OUT_PATH} 유지, 이번 회차는 갱신 skip",
            file=sys.stderr,
        )
        return

    payload = {
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "count": len(collected),
        "items": collected,
    }

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[OK] {len(collected)}건 저장 (숨김 {len(DISMISSED_IDS)}건 제외) -> {OUT_PATH}")


if __name__ == "__main__":
    main()
