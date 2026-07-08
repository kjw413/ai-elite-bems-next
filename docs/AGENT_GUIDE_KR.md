# AI 개발자 가이드 (코드 기준)

BEMS(빙그레 에너지 관리시스템) 저장소를 작업하는 AI 에이전트용 가이드입니다.
시스템 전반 사양은 [MASTER_SPEC_KR.md](MASTER_SPEC_KR.md)를 우선 참조하세요.

---

## 1. 가장 중요한 원칙

이 프로젝트의 source of truth는 문서가 아니라 **코드**입니다. 우선 참조 순서:

1. `app/main.py`
2. `app/database/schema.sql`
3. `app/services/*.py`
4. `app/pages/*.py`
5. 보조 문서(`docs/`)

> 과거 문서가 남아 있을 수 있으므로 낡은 DB 설명·메뉴 설명을 그대로 믿지 말 것.
> 기능이 "구현됐는지"는 `app/main.py` 라우팅 연결 여부로 확인한다.

---

## 2. 프로젝트 목표

- 엑셀/외부 수집 데이터를 MySQL에 적재하고 Streamlit 대시보드/비교분석 제공
- 업로드·변경 이력 추적(감사)
- v5 앙상블 분위수 예측(정상범주 밴드) + 이상감지·원인 진단
- AI 월간 실적 보고서 생성

---

## 3. 운영 환경 · 권한

| 항목 | 내용 |
| --- | --- |
| 실행 환경 | 로컬 PC, Streamlit, MySQL (클라우드 미사용) |
| 관리자 판정 | **클라이언트 IP 기반 자동 판정** (host PC → root, 외부 PC → viewer) |

> 관리자 모드는 **IP 기반**이다. 과거의 사이드바 비밀번호(ADMIN_PASSWORD) 로그인 방식은 사용하지 않는다.

---

## 4. 아키텍처 개요

```text
app/
  main.py            # 진입점: 라우팅 + 기동 시 자동 동기화(에너지·생산) 호출
  config/paths.py    # SAMPLED_DB_DIR 경로 해석
  domain/factories.py# 공장 마스터/코드 매핑/집계 헬퍼
  database/          # db_connection(root/viewer 분리, init_db), schema.sql(9 테이블)
  pages/             # 9개 라우트(사용량 통합/원단위/생산실적/AI/관리자 등)
  services/          # 24개 서비스
  prompts/           # LLM 시스템 프롬프트(ai_report / anomaly_diagnosis)
  utils/, predictive model/(energy usage/: v5 .pkl + registry)
```

- **데이터 수집 RPA는 별도 프로젝트 `../AI-Elite_MIS_RPA/`로 분리됨.** 웹은 그 RPA가
  `SAMPLED_DB_DIR`에 만든 엑셀을 기동 시 읽어 적재한다(`daily_energy_sync_service`,
  `production_dw_sync_service`). 웹 저장소에는 수집/재가공 코드가 없다.
- `production_dw_service`는 **조회 함수(`query_*`)만** 보유한다(재가공 `build_dataset`은 RPA로 이관).

---

## 5. 현재 구현 범위

구현됨: 대시보드, 에너지 모니터링 2화면(사용량 통합/원단위), **생산실적 분석**, 데이터 업로드, 업로드·변경 이력,
AI 실적 보고서, **에너지 사용 예측(v5.3, 권한별 탭, 이상감지)**.

메뉴에서 제외됨: 절감 계획 관리, 절감 실적 현황.

---

## 6. 개발 규칙

1. **진입점 기준** — 변경 전 `app/main.py`의 탭/라우팅 구조부터 확인.
2. **데이터 무결성** — `energy_daily` 고유키 `(factory, date)`, 동일 키는 업로드 시 덮어쓰기, 변경 숫자 컬럼은 `energy_daily_audit`에 기록.
3. **공장 코드** — DB 원본: 남양주1/남양주2/김해/광주/논산. 조회 파생: `남양주`(=1+2), `전사`(전체 합). `production_daily`는 F-코드(F10A/F10B/F20/F30/F40) 사용 → 매핑 주의.
4. **원단위** — `월 원단위 = Σ(사용량) / Σ(mix_prod_kg/1000)`. 일 원단위 평균을 월 원단위로 쓰지 말 것.
5. **사용자 식별** — `getpass.getuser()` 기반(`changed_by`).
6. **모델 아티팩트** — `app/predictive model/energy usage/`의 `.pkl`·`v5_model_registry.json`은 런타임 필수. 함부로 이동/삭제 금지.
7. **커밋 전 위생** — `pip install -r requirements-dev.txt && pre-commit install`. 훅이 시크릿·1MB↑ 바이너리·ruff 미사용 import/변수를 차단한다(우회 금지).

---

## 7. 핵심 파일

| 파일 | 역할 |
| --- | --- |
| `app/main.py` | 메인 UI 구조 + 기동 시 동기화 호출 |
| `app/database/schema.sql` | 실제 DB 스키마(9 테이블) |
| `app/database/db_connection.py` | DB 연결·초기화·IP 권한 판정 |
| `app/config/paths.py` | 외부 수집 엑셀 경로 해석 |
| `app/services/query_service.py` | 조회/집계 로직 |
| `app/services/upload_service.py` | 업로드 파이프라인 |
| `app/services/v5_common.py` | v5 모델 레지스트리/경로/공용 로직 |
| `app/services/usage_prediction_v5_service.py` | 예측 실행 |
| `app/services/ai_db_service.py` | LangChain SQL Agent 보고서 |
| `app/services/*_sync_service.py` | 기동 시 엑셀→DB 자동 적재 |

---

## 8. 실행

권장: `.env` 준비 → `SETUP.bat` → `WEB 실행.bat`.
수동: `python -c "import sys; sys.path.insert(0,'.'); from app.database.db_connection import init_db; init_db()"` 후 `streamlit run app/main.py`.

---

## 9. 문서 작업 시 주의

- 미구현 기능을 구현된 것처럼 쓰지 말 것(반대도 금지 — 생산실적·웹예측은 이미 구현됨).
- DB 문서는 `schema.sql`·`db_connection.py`와 교차 확인. ERD는 [DB_DIAGRAM_KR.md](DB_DIAGRAM_KR.md).
- 민감한 값(키/비밀번호)을 문서에 하드코딩하지 말 것.
- 개발/개선 이력은 `docs/history/`에 보존한다.
