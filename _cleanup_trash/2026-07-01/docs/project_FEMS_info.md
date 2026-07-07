📦 프로젝트 개요
BEMS (Binggrae Energy Management System) — 빙그레 5개 공장(남양주1·남양주2·김해·광주·논산)의 일일 에너지/생산 데이터를 엑셀로 업로드 → MySQL에 저장 → Streamlit 웹에서 대시보드/비교분석/AI 예측·보고서를 제공하는 로컬 웹 앱입니다.

🏗️ 아키텍처
진입점: app/main.py — 사이드바 네비게이션, 라이트 모드 단일 테마, IP 기반 권한 판정, 페이지 라우팅
DB 계층: app/database/db_connection.py, schema.sql — root/viewer 계정 분리, 자동 init
서비스 계층: app/services/ — 11개 서비스 모듈
UI 페이지: app/pages/ — 10개 페이지 (대시보드, 비교분석 3종, AI 2종, 절감 플레이스홀더 2종, 생산실적 본 구현, 업로드, 변경이력)

🗄️ 데이터 모델 (5개 테이블)
테이블 역할
energy_daily 일별 에너지/생산 실적 (UK: factory+date)
energy_daily_audit 변경 이력 (UPLOAD/MANUAL/WEB_ROW_EDIT)
upload_batch 업로드 배치 이력
ai_reports AI 월간 보고서 캐시 (UK: factory+year+month)
prediction_log 일별 예측 로그 + MAPE (UK: factory+pred_date+target)
핵심 규칙: 월 원단위는 일 원단위 평균이 아니라 Σ사용량 / Σ(mix_prod_kg/1000)로 재계산. 남양주(=남양주1+남양주2)과 전사는 DB에 없는 파생 코드.

🤖 v5 예측 모델 시스템 (app/services/v5_common.py)
버전: **운영 중 v5.2** (분위수 회귀 + CQR 후처리 — 정상범주 밴드 출력). v5.1(점추정)은 레지스트리에 보존되어 즉시 롤백 가능.
앙상블: LGBM + XGBoost + CatBoost × M1~M4 피처셋(생산/온도/습도/THI) = 12개 모델 (v5.2는 P05/P50/P95 분위수마다 12개씩, 총 36개), SLSQP로 분위수별 Pinball loss 최소화 가중치 산출
타겟: 전력/연료/용수 (3개)
피처 명세 v1.1/1.2-wip-shortlist (점추정) / 1.1-quantile/1.2-wip-shortlist-quantile (분위수): lag1/r7mean/intensity_lag1/HDD/CDD/THI 등 누수 방지된 화이트리스트만 사용 (PLAN.md)
CQR 후처리: 검증셋 잔차로 q_hat을 산출해 P05/P95 밴드를 평행 확장 → 운영 PICP가 목표(0.90)에 수렴하도록 보정
재학습 워커: v5_retrain_worker.py는 v5.1 점추정 전용 (subprocess + 락 파일 + STATUS_PATH JSON). **v5.2 모델은 modeling_v5.2.py 스크립트로 별도 학습** (워커는 quantile_wip 프로파일 감지 시 자동 skip)
날씨 자동 동기화: weather_sync_service.py — 기상청 ASOS API → DB_weather.xlsx 갱신 (서울/김해/이천/부여)
집계 공장 처리: 남양주/전사 선택 시 구성 공장 예측값을 재사용/추가 예측 후 합산. v5.2 밴드도 공장별 P05/P95를 가법 합산하여 집계 밴드 생성
이상 판정 (정성적): **실측이 정상범주(P05~P95) 밖이면 이상** — `band_status IN ('over','under')`. 'over'(과사용 ↑) / 'under'(저사용 ↓) 구분. v5.1 행(밴드 NULL)은 MAPE>=20% 폴백
입력 변수 이상 탐지: `variable_anomaly_service.py` — z-score 기반으로 "이날 평소와 달랐던 입력 변수"를 자동 식별 (이상감지 진단 LLM 컨텍스트에 자동 주입)

🧠 AI 보고서 (ai_db_service.py)
LangChain create_sql_agent (gpt-5.4) + 시스템 프롬프트 ai_report_prompt.md → AI가 직접 DB를 조회/계산해 월간 임원용 보고서 생성, ai_reports 테이블에 UPSERT.

🎨 UI 구조
사이드바 메뉴: 대시보드(홈) / 생산실적 분석 / 에너지 모니터링(전력 사용량·연료·용수 사용량·원단위) / 에너지 절감관리(개발예정) / AI 에너지 분석(예측+보고서) / [관리자 전용] 데이터 업로드·변경 이력

대시보드는 AI 이상감지 배너(과사용 ↑ / 저사용 ↓ 구분) → 7일 추이 → 월간 YoY 차트 → 주요 인사이트(밴드 외 항목 강조) → KPI 카드 5종 → MTD/YTD 토글 → 추이라인 + 편차바 + 알람 + 도넛 + 상세 테이블의 6단 구조. 이상감지 상세 테이블의 컬럼은 "정상범위 [P05~P95] / 실측값 / 상태(↑/↓) / 위치(±)". 대시보드의 7일 추이 / 월간 YoY / 상세 비교 테이블에는 현재 화면 필터를 그대로 반영한 CSV 다운로드 버튼이 붙어 있어 보고서 작성 시 raw 데이터를 그대로 추출 가능.

AI 에너지 분석 — 에너지 사용 예측: viewer는 `📈 예측 실행`·`📋 예측 이력` 두 탭만 노출. host PC(관리자)는 추가로 `⚙️ 모델 관리` 탭에서 v5 모델 메타데이터 / 재학습 트리거 / 기상청 API 수동 동기화에 접근. 일반 실무자 화면에서는 MLOps·모델 경로·active_key 같은 시스템 운영 메타정보가 노출되지 않음.

⚙️ 운영
실행: SETUP.bat → WEB 실행.bat (포트 8501-8510 자동 탐색)
권한: 클라이언트 IP 기반 자동 판정. 호스트 PC(loopback / 서버 자기 LAN IP) → 관리자(root), 외부 PC → viewer. ADMIN_PASSWORD 사이드바 입력 방식은 사용하지 않음.
환경변수: DB 계정 2종(admin/viewer), OPENAI_API_KEY, KMA_API_KEY, V5 재학습 파라미터(`V5_WEIGHT_UPDATE_WINDOW_BDAYS`, `V5_FULL_RETRAIN_MIN_INTERVAL_HOURS`, `V5_FULL_RETRAIN_N_ESTIMATORS`)
