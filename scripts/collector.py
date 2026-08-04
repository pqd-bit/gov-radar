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

필요 환경변수 (GitHub Actions Secrets 로 주입)
  KSTARTUP_API_KEY   : data.go.kr 에서 발급받은 K-Startup 서비스키 (Decoding 키)
  BIZINFO_API_KEY    : bizinfo.go.kr 에서 발급받은 crtfcKey

실행:
  python scripts/collector.py
출력:
  docs/data/programs.json
"""
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path
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
]

# 제목에 포함되면 무조건 제외 (자격요건상 Dean이 지원 불가한 대상)
EXCLUDE_KEYWORDS = [
    "여성",  # 여성기업/여성창업 전용 프로그램
]

# 참여 가능 지역 - PEQUOD는 서울 소재 1인기업.
# region 문자열에 아래 중 하나라도 포함되면 지역 제한 없이 지원 가능한 것으로 간주.
# 특정 지자체(부산/전남/전북/강원/충남/춘천 등) 소속 기업 전용 공고는 자동 배제.
ELIGIBLE_REGION_HINTS = ["전국", "서울"]


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


def is_region_eligible(region: str):
    if not region:
        return True
    return any(hint in region for hint in ELIGIBLE_REGION_HINTS)


def compute_priority(title: str, region: str):
    if any(k in title for k in EXCLUDE_KEYWORDS):
        return False
    if not is_region_eligible(region):
        return False
    return any(k in title for k in PRIORITY_KEYWORDS)


def load_dismissed():
    if not DISMISSED_PATH.exists():
        return set()
    try:
        data = json.loads(DISMISSED_PATH.read_text(encoding="utf-8"))
        return set(data.get("dismissed_ids", []))
    except Exception:
        return set()


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
            "is_priority": compute_priority(title, region),
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

        items.append({
            "id": make_id("기업마당", title, end),
            "source": "기업마당",
            "title": title,
            "org": g("jrsdInsttNm") or g("excInsttNm") or "기업마당",
            "region": region,
            "start_date": start,
            "end_date": end,
            "url": g("pblancUrl") or "https://www.bizinfo.go.kr",
            "is_priority": compute_priority(title, region),
        })
    return items


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
    dismissed = load_dismissed()

    collected = fetch_kstartup() + fetch_bizinfo()
    collected = [c for c in collected if c["title"]]
    collected = dedupe(collected)
    collected = [c for c in collected if c["id"] not in dismissed]

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
    print(f"[OK] {len(collected)}건 저장 (숨김 {len(dismissed)}건 제외) -> {OUT_PATH}")


if __name__ == "__main__":
    main()
