# AI Elite BEMS Next 독립화 계획서

> 작성일: 2026-07-16
> 결정자: 사용자 (2026-07-16)
> 목표: **legacy 의존성 완전 배제** — `new/`만으로 독립 운용하면서 기존 legacy 웹의
> 기능을 모두 갖추고, React UI 기반의 세련된 디자인을 완성한다.
> 원칙: `legacy/`는 폐기 전까지 읽기 전용 참조본으로만 사용한다. 포팅한 로직은
> 반드시 동등성 테스트(같은 입력 → 같은 출력)로 검증한 뒤 의존을 끊는다.
> MySQL 스키마와 데이터는 그대로 유지한다(전환 대상은 코드·자산이지 데이터가 아님).

---

## 1. 현재 의존성 인벤토리

### 1.1 코드 의존 (backend/server.py의 `import_core` 지점)

| # | legacy 모듈 | 사용 기능 | 해소 방법 |
|---|---|---|---|
| 1 | `target_service` | 절감 목표 UPSERT | Phase 1 복사 |
| 2 | `event_annotation_service` | 이벤트 등록·수정·삭제 | Phase 1 복사 |
| 3 | `upload_service` (+`excel_parser`, `validation_service`, `audit_service`) | Excel 검증·UPSERT·감사 | Phase 1 복사 |
| 4 | `production_actual_service` (+`production_correction_service`) | 광주 WIP 환산·운영 생산량 | Phase 1 복사 |
| 5 | `usage_prediction_v5_service` (+`v5_common`, `v5_band_calibration` 등) | 예측 실행·누락 생성·역채움·모델 레지스트리 | Phase 1 복사 + 결함 수정 |
| 6 | `ai_db_service` + `ai_report_service` (+`app/prompts`) | LangChain 보고서 생성·저장 | Phase 1 복사 |

### 1.2 데이터·자산 의존 (legacy 폴더 안에 있는 비코드 자산)

- `legacy/.env` — DB·OpenAI·기상청 키 (→ `new/` 전용 `.env`로 이관)
- `legacy/app/predictive model/**` — v5.3 모델 `.pkl`, 레지스트리 JSON, 재학습 상태
- 휴일 캘린더·기상 관측소 데이터 스토어
- `E:\Sampled DB\*.xlsx` — RPA 산출 원본 (legacy 폴더 밖이므로 그대로 사용)

### 1.3 운영 흐름 의존 (가장 위험한 숨은 의존)

- **엑셀 → DB 자동 동기화가 Streamlit rerun에 묶여 있음**: `daily_energy_sync_service`,
  `production_dw_sync_service`는 legacy 앱이 rerun될 때만 mtime을 비교해 UPSERT한다.
  legacy 웹을 내리면 **신규 데이터가 DB에 들어오지 않는다.** 독립화의 필수 전제.
- 기상 동기화·모델 재학습도 legacy 화면에서만 트리거 가능.

### 1.4 미구현 기능 (legacy에는 있으나 new에 아직 없음)

What-if 시뮬레이터, 이상 원인 진단(LLM), 모델 재학습 UI, 기상 동기화 UI,
생산실적 기간·연간 모드, CSV 내보내기, 일일 메일 리포트(tools/mail).

---

## 2. 단계별 계획

> **전략 (2026-07-16 사용자 지시): 재구현이 아니라 "복사해서 사용".**
> 데이터 처리 서비스 등 필요한 legacy 파일을 `new/backend/app/`으로 복사해 그대로
> 사용한다. 복사본은 `new/` 소유이므로 결함을 복사본에서 직접 수정한다.
> `legacy/` 원본은 수정하지 않고 참조·비교 검증용으로만 유지한다.
> `app/config/paths.py`가 `PROJECT_ROOT = parents[2]` 기준이므로 트리를 그대로
> 복사하면 모델·휴일·.env 경로가 `new/backend/` 아래에서 자체 해석된다.

### Phase 0 — 예측 경로 긴급 복구 ✅ (2026-07-16 완료)

legacy `_fetch_energy_history`가 factory 컬럼 없이 `overlay_actual_production`을
호출하는 결함(legacy 자체 버그, Streamlit에서도 동일 발생)으로 예측 실행·누락
생성이 500으로 죽는 문제를 확인. 경산(F50)은 모델 미학습으로 예측 실행·집계에서
제외(사용자 결정). ※ 최초 런타임 패치로 대응했으나 Phase 1 복사본 직접 수정으로 대체.

### Phase 1 — 코어 복사 이식 ✅ (2026-07-16 완료)

- `legacy/app/{services,database,domain,utils,config,prompts}` → `new/backend/app/`
  (Streamlit UI인 pages/components/assets·main.py는 제외).
- `legacy/app/predictive model/` → 복사 (\_archive 923MB 제외, 활성 모델
  v5.3_20260625_130733.pkl 포함 약 3.8GB), `legacy/.env` → `new/backend/.env`,
  `legacy/requirements.txt` → `new/backend/requirements-core.txt`.
- `server.py`의 `import_core`가 legacy 폴더 대신 **로컬 복사본**을 로드하도록 전환.
- 발견 №1 결함을 복사본 `usage_prediction_v5_service.py`에서 정식 수정.
- 배치 스크립트가 legacy 폴더 없이도 설치·실행되도록 갱신.
- 완료 기준: legacy 폴더를 참조하지 않고 서버 기동·조회·예측 동작.

### Phase 2 — 복사본 기반 기능 완성·동등성 검증 ✅ (2026-07-16 완료)

- 업로드 **미리보기(2단계 확인)**: `POST /api/v1/upload/preview`(dry-run) +
  관리자 화면 1단계 검증·미리보기 → 2단계 DB 반영 흐름.
  실제 RawDB_에너지.xlsx로 검증(공장 6곳, 덮어쓰기 10,210건 정확 산출, DB 무변경).
- 이상 원인 진단: `POST /api/v1/predictions/diagnose`(캐시 우선, 재생성만 관리자) +
  예측 이력 표의 이탈 행 진단 버튼·마크다운 진단 패널. 캐시 건으로 검증(LLM 비용 0).
- 동등성 스팟체크: 브리지 SQL vs 복사본 legacy pandas의 생산량 오버레이를
  2,639개 (일자×공장) 키 전수 비교 → **불일치 0건**. 대시보드 MTD 전력 원단위·생산량도
  수동 계산과 일치(404.19 kWh/ton, 13,253.1 ton). 현재 DB에 F10 잔존 행 없음 확인.
- RUN_GUIDE·README를 독립 실행 구조 기준으로 갱신(배포 시 .env·모델 폴더 별도 복사 명시).

### Phase 3 — 자동화 독립 ✅ (2026-07-16 완료)

- FastAPI lifespan 백그라운드 스케줄러: 기동 즉시 + `BEMS_SYNC_INTERVAL_SECONDS`
  (기본 120초) 주기로 에너지·생산실적 엑셀 mtime 비교 동기화
  (복사본 `daily_energy_sync_service`·`production_dw_sync_service` 재사용,
  동시 실행 잠금, 0으로 비활성화 가능).
- 신규 API: `GET/POST /api/v1/sync/{status,run}` (수동 강제 동기화),
  `GET/POST /api/v1/weather/{status,sync}`, `GET /api/v1/model/training-status`,
  `POST /api/v1/model/retrain` (락 충돌 시 409). 전부 관리자 전용.
- 관리자 화면: "데이터·동기화" 탭에 자동 동기화 상태 카드+지금 동기화 버튼,
  "예측·모델 운영" 탭에 기상 동기화·재학습 카드(진행률·10초 폴링).
- 운영: API 로그 파일화(`logs/api_YYYYMMDD.log`), `AUTOSTART_REGISTER.bat`/
  `AUTOSTART_UNREGISTER.bat`(로그온 시 자동 시작).
- **검증: 서버 기동만으로 25초 내 energy_daily 07-14 → 07-15 자동 따라잡음(+6행,
  사람 개입 없음).** 재학습 워커는 복사본 자체 경로로 spawn되는 것 확인(실행은 미트리거).
- 완료 기준 충족: legacy 앱을 내려도 데이터 유입·예측·보고 지속.

### Phase 4 — UI 완성·세련화 & legacy 퇴역

- 잔여 기능: 생산실적 기간·연간 모드, CSV 내보내기, 재학습·기상 동기화 UI.
- 디자인 시스템 정비: 검증된 차트 팔레트·다크모드·인터랙션(툴팁·범례) 일관화,
  전 화면 반응형·인쇄 품질 점검.
- 일정 기간 legacy 병행(read-only) → 수치·권한·업로드 동등성 최종 확인 → 퇴역.

## 3. 단계 간 공통 규칙

1. legacy 파일은 **복사해서 사용**하고, 수정은 복사본에만 한다. 수정 내역은 본 문서
   "발견·수정 로그"에 기록해 legacy 원본과의 차이를 추적 가능하게 유지한다.
2. 각 Phase 완료 시 legacy 폴더 잔여 의존(.env·모델·코드)을 계획서와 작업정리 문서에 갱신한다.
3. 운영 전환(Phase 3 이후)까지 RUN_GUIDE_KR.md의 실행 절차를 항상 유효하게 유지한다.

### 발견·수정 로그 (복사본 ↔ legacy 차이)

| # | 파일 | 내용 |
|---|---|---|
| 1 | `app/services/usage_prediction_v5_service.py` `_fetch_energy_history` | legacy 결함 수정: SELECT에 `factory` 컬럼 포함 후 overlay, 반환 전 제거 (기존: factory 없이 overlay 호출 → ValueError) |

## 4. 진행 현황

| Phase | 상태 | 비고 |
|---|---|---|
| 0. 예측 경로 복구·경산 제외 | ✅ 완료 (2026-07-16) | 복사본 직접 수정으로 대체 |
| 1. 코어 복사 이식 | ✅ 완료 (2026-07-16) | legacy 코드 의존 제거 |
| 2. 기능 완성·동등성 검증 | ✅ 완료 (2026-07-16) | 미리보기·진단 연결, 오버레이 전수 비교 불일치 0 |
| 3. 자동화 독립 | ✅ 완료 (2026-07-16) | 스케줄러가 기동 25초 내 DB를 엑셀 수준으로 자동 동기화 검증. 기상·재학습 API/UI, 자동시작·로그 |
| 4. UI 완성·퇴역 | ⬜ 예정 | 디자인 시스템 정비, 연간 모드·CSV, legacy 병행 비교 후 퇴역 |
