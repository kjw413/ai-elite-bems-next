# BEMS 마스터 사양서 (코드 기준)

이 문서는 현재 코드(`app/main.py`, `app/services/`, `app/database/schema.sql`) 기준으로 정리한
**정본(canonical) 시스템 사양서**입니다. 스펙/개요 계열 문서(구 functional_specification,
poc_spec, ai_dev_package, project_FEMS_info)는 이 문서로 통합되었습니다.

- 데이터 모델 상세(ERD)는 [DB_DIAGRAM_KR.md](DB_DIAGRAM_KR.md) 참조
- 업로드 데이터 사전은 [Data_dictionery_v1.0.md](Data_dictionery_v1.0.md) 참조
- AI 작업 규칙은 [AGENT_GUIDE_KR.md](AGENT_GUIDE_KR.md) 참조
- 예측 모델 상세는 [energy_model_feature_architecture.md](energy_model_feature_architecture.md) 참조

---

## 1. 프로젝트 개요

**BEMS**(Binggrae Energy Management System)는 빙그레 5개 공장(남양주1·남양주2·김해·광주·논산)의
일별 에너지·생산 데이터를 통합 관리하는 **로컬 Streamlit 웹 앱**입니다.

핵심 흐름: **엑셀/외부 수집 → MySQL 적재 → 웹 대시보드·비교분석·AI 예측/보고서**.

주요 목적:
- 공장별·전사 기준 사용량/원단위 비교와 전년 대비 분석
- 업로드·변경 이력 추적(감사)
- AI 기반 이상감지·월간 실적 보고서
- v5 앙상블 분위수 예측(정상범주 밴드) 및 이상 원인 진단

---

## 2. 운영 환경 · 실행

| 항목 | 내용 |
| --- | --- |
| 실행 방식 | 로컬 PC 단독 실행 (별도 서버 없음) |
| 웹 프레임워크 | Streamlit |
| 데이터베이스 | 로컬 MySQL (utf8mb4) |
| 접속 주소 | 기본 `http://localhost:8501` |
| 포트 정책 | `WEB 실행.bat`가 `8501`~`8510` 중 가용 포트 탐색 |
| 사용자 모드 | Viewer 기본 / Admin(host PC)은 IP 기반 자동 판정 |

권장 실행 순서: ① `.env` 준비 → ② `SETUP.bat` → ③ `WEB 실행.bat`.
수동 실행: `python -c "import sys; sys.path.insert(0,'.'); from app.database.db_connection import init_db; init_db()"` 후 `streamlit run app/main.py`.

---

## 3. 기술 스택

| 영역 | 기술 |
| --- | --- |
| UI | Streamlit + Plotly |
| 애플리케이션 | Python (Pandas, Numpy, Scipy) |
| DB 연결 | mysql-connector-python, SQLAlchemy, PyMySQL |
| AI 보고서 | OpenAI, LangChain (create_sql_agent) |
| 예측 모델 | LightGBM · XGBoost · CatBoost + Joblib, scikit-learn |
| 자동화(메일) | jinja2, kaleido, truststore |

---

## 4. 애플리케이션 구조

```text
app/
  main.py                     # 진입점: 사이드바 라우팅, 라이트 테마, IP 권한, 기동 시 동기화 호출
  config/paths.py             # 외부 수집 엑셀(SAMPLED_DB_DIR) 경로 해석
  domain/factories.py         # 공장 도메인 마스터/코드 매핑/집계 헬퍼
  database/
    db_connection.py          # root/viewer 계정 분리, init_db (DDL/계정/권한)
    schema.sql                # 실제 DB 스키마 (9개 테이블)
  pages/                      # 9개 라우트(사용량 통합/원단위/생산실적/AI/관리자 등, §5)
  services/                   # 24개 서비스 모듈 (§4.1)
  prompts/                    # LLM 시스템 프롬프트 (ai_report / anomaly_diagnosis)
  utils/                      # excel_parser, df_format, page_state, file_io, tls 등
  predictive model/
    energy usage/             # 학습된 v5 모델(.pkl) + v5_model_registry.json (런타임)
    modeling_v5.1.py / modeling_v5.3.py, DB_holiday.xlsx, _archive/
```

> **데이터 수집 RPA는 별도 프로젝트로 분리됨** — `../AI-Elite_MIS_RPA/`. MIS 화면 수집·재가공은
> 그 프로젝트가 담당하고 결과 엑셀을 `SAMPLED_DB_DIR`에 저장하며, 웹은 기동 시 이를 읽어 적재한다(§7).

### 4.1 서비스 계층 (24개, 도메인별)

- **업로드·조회·감사**: `upload_service`, `validation_service`, `query_service`, `audit_service`, `event_annotation_service`, `target_service`
- **생산실적**: `production_dw_service`(production_daily 조회 전용), `production_dw_sync_service`(기동 시 적재), `production_correction_service`, `item_energy_impact_service`
- **에너지·날씨 동기화**: `daily_energy_sync_service`(기동 시 RawDB_에너지→energy_daily), `weather_sync_service`(기상청 ASOS API)
- **AI 보고서·이상진단**: `ai_report_service`, `ai_db_service`(LangChain SQL Agent), `anomaly_diagnosis_service`, `variable_anomaly_service`, `prediction_monitoring_service`
- **v5 예측**: `usage_prediction_v5_service`, `v5_common`, `v5_explainability`, `v5_quantile_training`, `v5_retrain_service`, `v5_retrain_worker`, `v5_retrain_lock`

---

## 5. 화면 구조

사이드바 메뉴(권한별 노출):

1. **대시보드(홈)** — 이상감지 배너(과사용↑/저사용↓) → 7일 추이 → 월간 YoY → 주요 인사이트 → KPI 카드 → MTD/YTD → 상세 비교 테이블. 화면 필터를 반영한 CSV 다운로드 지원.
2. **에너지 모니터링** — 사용량 통합(전력/연료/용수/폐수 선택) / 원단위. 전력 설비 분해와 폐수/용수 비율은 사용량 통합 화면 내부 섹션.
3. **생산실적 분석** — `production_daily` 기반 계획 vs 실적(월별/기간별/연간 모드, 품목/보관유형/제품유형별). **구현됨**.
4. **AI 에너지 분석**
   - 에너지 사용 예측 — viewer: `📈 예측 실행`·`📋 예측 이력` 2탭 / 관리자(host): + `⚙️ 모델 관리`(v5 메타·재학습·기상청 동기화·실측 역채움). **구현됨**.
   - 에너지 실적 보고서 — LangChain SQL Agent 기반 월간 보고서 생성/저장. **구현됨**.
5. **데이터 업로드** (관리자 전용) — 엑셀 업로드·검증·동기화.
6. **변경 이력 / 메모** (관리자 전용) — 감사 로그 + 이벤트 메모.

파생 공장 코드: `남양주` = 남양주1+남양주2, `전사` = 전체 합산 (DB 원본이 아니라 조회 시 계산).

---

## 6. 데이터 모델

MySQL 9개 테이블. 상세 ERD·관계는 [DB_DIAGRAM_KR.md](DB_DIAGRAM_KR.md) 참조.

| 테이블 | 역할 | 자연키 |
| --- | --- | --- |
| `energy_daily` | 일별 에너지·생산 실적 (중심 테이블) | factory + date |
| `energy_daily_audit` | 변경 이력 (UPLOAD/MANUAL/WEB_ROW_EDIT) | — |
| `upload_batch` | 업로드 배치 이력 | — |
| `ai_reports` | AI 월간 보고서 캐시 | factory + year + month |
| `prediction_log` | 일별 예측 로그 + 실측 + MAPE/밴드 | factory + pred_date + target |
| `anomaly_analysis` | LLM 이상 원인 진단 캐시 | factory + pred_date + target |
| `production_daily` | 일·품목별 생산 DW (F-코드 기준) | date + factory + item_code |
| `savings_target` | 연간 절감 목표 | factory + year + metric |
| `event_annotation` | 일별 이벤트 메모 | — |

- 저장 공장 코드: `남양주1`·`남양주2`·`김해`·`광주`·`논산`. 수치 컬럼은 `DOUBLE`.
- `production_daily`는 생산 DW의 F-코드(`F10A`/`F10B`/`F20`/`F30`/`F40`)를 쓰므로 `energy_daily`(한글 공장명)와 조인 시 코드 매핑 필요.

---

## 7. 데이터 수집·적재 파이프라인

수집(RPA)과 적재(웹)가 **엑셀 파일을 접점으로 분리**되어 있다. 웹은 서버 기동 시(`app/main.py`)
자동 동기화하며, 소스 파일 mtime이 바뀐 경우에만 실제 UPSERT한다.

| 데이터 | 수집(외부 RPA) 산출물 | 웹 기동 시 적재 서비스 | 대상 테이블 |
| --- | --- | --- | --- |
| 에너지 | `RawDB_에너지.xlsx` | `daily_energy_sync_service.auto_sync_once` | `energy_daily` |
| 생산실적 | `DB_생산실적.xlsx`(RPA가 재가공) | `production_dw_sync_service.auto_sync_production_once` | `production_daily` |

- 수집·재가공(`build_dataset` 등)은 별도 프로젝트 `../AI-Elite_MIS_RPA/`가 담당한다.
- 접점 폴더는 `.env`의 `SAMPLED_DB_DIR`(기본 `E:\Sampled DB`)이며 RPA와 웹이 동일해야 한다.
- 엑셀 직접 업로드(§8)는 위 자동 동기화와 별개의 관리자 수동 경로다.

---

## 8. 엑셀 업로드 사양 (energy_daily)

지원: `.xlsx`, `.xls`. 상세 컬럼/단위는 [Data_dictionery_v1.0.md](Data_dictionery_v1.0.md) 참조.

지원 시트명 → 저장 코드: `남양주1`→남양주1, `남양주2`→남양주2, `남양주`→남양주1, `F11`→남양주2, `김해`/`광주`/`논산`은 동일.

필수 컬럼: `date`, `freezing_power_kwh`, `air_compressor_kwh`, `total_power_kwh`, `fuel_nm3`, `water_ton`, `wastewater_ton`, `mix_prod_kg`, `power_per_ton_kwh`, `fuel_per_ton_nm3`, `water_per_ton_ton`.

검증: 빈 값→`0`, 숫자 아님→업로드 실패, 음수 허용, 동일 `(factory,date)`→업데이트(+변경 컬럼 감사 기록).

---

## 9. 집계 로직

- 일별 조회는 `energy_daily` 원본 사용.
- 월별 사용량 = 일별 합계.
- **월 원단위는 일 원단위 평균이 아니라 재계산**: `Σ(사용량) / Σ(mix_prod_kg / 1000)`.
- `남양주` 선택 시 남양주1+남양주2 합산, `전사` 선택 시 전체 합산.
- 폐수는 원단위 폐기 → 화면·메일에서 **폐수/용수 비**(폐수량/용수량, 소수점 2자리) 즉석 계산.

---

## 10. AI · 예측 시스템

### 10.1 v5 사용량 예측 (활성: v5.3)

- 대상: 전력·연료·용수. 5개 공장 일별.
- 앙상블: LightGBM·XGBoost·CatBoost × 피처셋, **분위수 회귀(P05·P50·P95) + CQR 후처리**로 정상범주 밴드 출력. SLSQP로 분위수별 Pinball loss 최소 가중치 산출.
- 레지스트리: `app/predictive model/energy usage/v5_model_registry.json`(활성 모델 경로/키). v5.1(점추정)·v5.2 보존으로 롤백 가능.
- 재학습: 관리자 버튼이 최신 `energy_daily` 기반 v5.3 전체 재학습(락 파일 + 상태 JSON, 검증 통과 시에만 자동 활성화).
- 날씨 피처: `weather_sync_service`가 기상청 ASOS API → `DB_weather.xlsx`(서울/김해/이천/부여).

### 10.2 이상 판정·진단

- 실측이 정상범주(P05~P95) 밖이면 이상(`band_status` = `over`/`under`). v5.1 행(밴드 NULL)은 MAPE≥20% 폴백.
- `variable_anomaly_service`가 z-score로 "평소와 달랐던 입력 변수"를 식별해 LLM 진단 컨텍스트에 주입 → `anomaly_diagnosis_service`가 원인 진단을 `anomaly_analysis`에 캐시.

### 10.3 AI 실적 보고서

- `ai_db_service`가 LangChain `create_sql_agent`로 DB를 직접 조회/계산해 월간 임원 보고서를 생성, `ai_reports`에 UPSERT. 시스템 프롬프트: `app/prompts/ai_report_prompt.md`.

---

## 11. 권한 구조

- 권한은 클라이언트 IP 기반 자동 판정(`db_connection.is_admin`): host PC(loopback/서버 LAN IP)→관리자(root), 외부 PC→viewer. **사이드바 비밀번호 로그인 방식은 사용하지 않는다.**
- 관리자 전용: `데이터 업로드`·`변경 이력` 메뉴, 예측의 `⚙️ 모델 관리` 탭·`🔄 실측값 역채움`, 예측 결과 DB 저장.
- DB 계정은 관리자/조회전용으로 분리, `init_db()`가 viewer 계정 생성 및 SELECT 권한 부여.

---

## 12. 현재 구현 상태

구현 완료: 엑셀 업로드·검증, MySQL 저장·초기화, 기동 시 자동 동기화(에너지·생산), 메인 대시보드(이상감지/7일추이/YoY/KPI/CSV), 에너지 모니터링 2화면, **생산실적 분석**, 업로드·변경 이력, AI 실적 보고서, **에너지 사용 예측(v5.3 앙상블, 권한별 탭, 이상감지)**.

메뉴에서 제외됨: 절감 계획 관리, 절감 실적 현황.

---

## 13. Source of Truth

이 문서의 기준은 문서가 아니라 코드입니다. 우선 참조 순서:

1. `app/main.py`
2. `app/database/schema.sql`
3. `app/services/*.py`, `app/pages/*.py`
4. 보조 문서(본 문서 및 `docs/`)
