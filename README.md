# Gov Radar

정부·공공기관 지원사업 공고를 자동 수집해 대시보드 + 마감 D-day 이메일 알림을 제공하는 1인 기업용 시스템.

## 구조
```
gov-radar/
├── scripts/
│   ├── collector.py   # K-Startup / 기업마당 API 수집 → docs/data/programs.json
│   └── notify.py      # D-14 이내 마감 공고 이메일 발송
├── docs/
│   ├── index.html     # 대시보드 (GitHub Pages)
│   └── data/programs.json
└── .github/workflows/collect.yml   # 매일 07:00 KST 자동 실행
```

## 설정 순서 (최초 1회, 30분 내외)

1. **저장소 생성**: 이 폴더를 새 GitHub repo(e.g. `gov-radar`)에 push, Settings → Pages → Source를 `docs/` 폴더로 설정.
2. **K-Startup API 키 발급**
   - data.go.kr → "창업진흥원_K-Startup(사업소개,사업공고,콘텐츠 등)_조회서비스" 활용신청
   - 승인 후 발급되는 **Decoding 키**를 사용
3. **기업마당 API 키 발급**
   - bizinfo.go.kr → 활용정보 → 정책정보 개방 → API 신청 (즉시 발급)
4. **GitHub Secrets 등록** (repo Settings → Secrets and variables → Actions)
   - `KSTARTUP_API_KEY`, `BIZINFO_API_KEY`
   - `SMTP_HOST`(예: smtp.gmail.com), `SMTP_PORT`(587), `SMTP_USER`, `SMTP_PASS`(Gmail 앱 비밀번호), `NOTIFY_TO`(수신 이메일)
5. **수동 1회 실행**: Actions 탭 → Gov Radar Daily Collect → Run workflow
6. 이후 매일 자동 실행 → `docs/data/programs.json` 갱신 + 커밋, D-14 이내 공고 이메일 발송

## 로컬 테스트
```bash
export KSTARTUP_API_KEY=xxx BIZINFO_API_KEY=xxx
python scripts/collector.py
python -m http.server 8080 --directory docs   # http://localhost:8080
```

## 우선순위 키워드 조정
`scripts/collector.py` 상단 `PRIORITY_KEYWORDS` 리스트에 청창사, 수출바우처, 농식품 등 관심 키워드 추가/수정.
K-Startup 공고 지역필터(`supt_regin`)는 API 응답 신뢰도가 낮아 서울만 요청해도 타 지역이 섞여 나올 수 있음 — 대시보드 필터로 재확인 권장.

## 미포함 범위 (필요 시 확장)
- 청창사 자체는 별도 사이트(kosmes.or.kr) 공고 주기가 있어, K-Startup 통합공고에 늦게 반영되는 경우 존재 → 필요 시 kosmes 공고 페이지 크롤러 추가 권장
- 카카오톡/Slack 알림은 notify.py 구조를 그대로 웹훅 POST로 교체하면 확장 가능
