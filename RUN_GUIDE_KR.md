# BEMS 실행 가이드

이 문서는 현재 코드 기준으로 `SETUP.bat`, `WEB 실행.bat`, `app/main.py`를 따라 정리한 실행 가이드입니다.

---

## 1. 개요

- 애플리케이션은 `Streamlit` 기반 로컬 웹 앱입니다.
- 데이터 저장소는 `MySQL`입니다.
- 웹 UI 진입점은 `app/main.py`입니다.
- 관리자 전용 메뉴는 클라이언트 IP 기반 자동 판정으로 host PC 접속 시에만 표시됩니다(비밀번호 로그인 없음).

---

## 2. 사전 준비

- `Python 3.8` 이상
- 로컬 `MySQL` 서버 실행
- 프로젝트 루트에 `.env` 파일 준비

`.env`에는 최소한 아래 항목이 필요합니다.

- `DB_HOST`
- `DB_PORT`
- `DB_NAME`
- `DB_ADMIN_USER` (관리자/root 계정 사용자명. MySQL 본체 계정과 동일하게 둡니다)
- `DB_VIEWER_USER` (조회 전용 계정 사용자명, 자동 생성됨)
- `DB_VIEWER_PASSWORD` (조회 전용 계정 비밀번호)

> 권한 모델 변경 안내: 본 시스템은 더 이상 사이드바 비밀번호 입력 방식의 관리자 로그인을 사용하지 않습니다. 클라이언트 IP 기반으로 자동 판정합니다(호스트 PC → 관리자, 외부 PC → viewer). 따라서 `.env`에 `ADMIN_PASSWORD` 변수는 필요하지 않습니다.

AI 실적 보고서를 사용할 경우 아래 항목도 필요합니다.

- `OPENAI_API_KEY`

기상청 ASOS 자동 동기화를 사용할 경우:

- `KMA_API_KEY`

---

## 3. 빠른 시작

1. `.env.template`를 참고해 `.env`를 준비합니다.
2. `SETUP.bat`를 실행합니다.
3. 이후 실행은 `WEB 실행.bat`를 사용합니다.

---

## 3-1. (AI 예측) v5 모델 생성 (로컬에서 1회)

`AI 에너지 분석 > 에너지 사용 예측` 화면은 v5 예측 모델 파일이 필요합니다.

- 모델 파일(`v5.1.pkl`)은 용량이 커서 GitHub에 커밋/푸시하지 않습니다(100MB 제한).
- 최초 1회 로컬에서 모델을 생성하거나, 사내 공유 경로 등에서 받은 모델 파일을 아래 경로에 배치해야 합니다.

### 생성 방법

```bash
# (가상환경 활성화 후)
.venv\Scripts\activate

# v5 (재공품 shortlist 적용) 모델 생성 (시간이 걸릴 수 있습니다)
python "app/predictive model/modeling_v5.1.py"
```

생성이 완료되면 아래 위치에 파일이 생성됩니다.

- `app/predictive model/energy usage/v5.1.pkl`

참고:

- 모델 산출물(`energy usage/`)은 `.gitignore`로 제외되어 git에 올라가지 않습니다.
- `modeling_v2.py` ~ `modeling_v5.py` 등 이전 버전 학습 스크립트는 `app/predictive model/_archive/`에 보관되어 있습니다(현재는 사용하지 않음).

---

## 4. 실행 파일 설명

| 파일           | 역할                                                            |
| -------------- | --------------------------------------------------------------- |
| `SETUP.bat`    | Python 확인, `.venv` 생성, 패키지 설치, DB 초기화               |
| `WEB 실행.bat` | 패키지 점검, DB 초기화, 사용 가능한 포트 탐색 후 Streamlit 실행 |

`WEB 실행.bat`는 기본적으로 `8501` 포트를 먼저 시도하고, 이미 사용 중이면 `8502`부터 `8510`까지 순차적으로 확인합니다.

---

## 5. 수동 실행

```bash
# 1. 가상환경 활성화
.venv\Scripts\activate

# 2. 패키지 설치
pip install -r requirements.txt

# 2-1. 개발 훅 설치(커밋 전 시크릿/대용량 바이너리/미사용 코드 차단)
pip install -r requirements-dev.txt
pre-commit install

# 3. DB 초기화
python -c "import sys; sys.path.insert(0,'.'); from app.database.db_connection import init_db; init_db()"

# 4. 앱 실행
streamlit run app/main.py
```

수동 실행 시 기본 주소는 보통 `http://localhost:8501`입니다. 포트가 겹치면 Streamlit 옵션으로 다른 포트를 지정하세요.

커밋 전 전체 검사는 다음 명령으로 수동 실행할 수 있습니다.

```bash
pre-commit run --all-files
```

`gitleaks`가 로컬에 설치되어 있으면 시크릿 훅이 `gitleaks protect --staged`를 함께 실행합니다. 설치되어 있지 않아도 기본 패턴 스캐너가 동작합니다.

### 5-1. 외부 원천 Excel 경로

앱 서비스와 RPA 도구는 모두 [app/config/paths.py](app/config/paths.py)의 경로 해석을 공유합니다.

PC 또는 드라이브가 바뀌면 `.env`의 `SAMPLED_DB_DIR`만 먼저 수정하세요.

```env
SAMPLED_DB_DIR=E:\Sampled DB
```

파일별 위치가 기본 파일명과 다를 때만 아래 변수를 개별 override합니다.

```env
ENERGY_SOURCE_XLSX=E:\Sampled DB\RawDB_에너지.xlsx
WIP_SUMMARY_XLSX=E:\Sampled DB\DB_재공품.xlsx
WIP_ITEM_MASTER_XLSX=E:\Sampled DB\RawDB_재공품.xlsx
PRODUCTION_RAW_XLSX=E:\Sampled DB\RawDB_생산실적.xlsx
PRODUCTION_DW_XLSX=E:\Sampled DB\DB_생산실적.xlsx
PRODUCTION_RAW_DIR=E:\Sampled DB\Raw_생산실적
```

---

## 6. 현재 메뉴 구조

- `대시보드(홈)`
- `생산실적 분석`
- `에너지 모니터링`
  - `전력 사용량`
  - `연료·용수 사용량`
  - `원단위`
- `에너지 절감관리 (개발예정)`
- `AI 에너지 분석`
  - `에너지 사용 예측`
    - 일반 사용자(viewer): `📈 예측 실행`, `📋 예측 이력` 두 탭만 노출
    - 관리자(host PC): 위 두 탭에 더해 `⚙️ 모델 관리` 탭(모델 메타데이터·재학습·기상청 API 동기화)이 추가로 표시됩니다.
  - `에너지 실적 보고서`
- `데이터 업로드` (관리자 전용)
- `변경 이력` (관리자 전용)

현재 코드 기준으로 `절감 계획 관리`, `절감 실적 현황` 화면은 플레이스홀더이며, `생산실적 분석`과 `에너지 사용 예측` 화면은 정식 운영 화면입니다.

### 6-1. 관리자 / 일반 사용자 권한 차이

| 기능                               | 일반 사용자 (viewer) | 관리자 (host PC / root) |
| ---------------------------------- | :------------------: | :---------------------: |
| 대시보드 / 비교분석 / 보고서 조회  |          ✅          |           ✅            |
| 차트·표 CSV 다운로드               |          ✅          |           ✅            |
| 예측 실행 (DB 저장 없이 결과만)    |          ✅          |           ✅            |
| 예측 이력 조회                     |          ✅          |           ✅            |
| 예측 결과 DB 저장                  |          ❌          |           ✅            |
| 실측값 역채움 (`prediction_log`)   |   ❌ (버튼 미노출)   |           ✅            |
| 모델 메타데이터 / 재학습 트리거    |    ❌ (탭 미노출)    |           ✅            |
| 기상청 API 날씨 데이터 수동 동기화 |    ❌ (탭 미노출)    |           ✅            |
| 데이터 업로드 / 변경 이력          |          ❌          |           ✅            |

권한 판정은 클라이언트 IP 기반입니다. 호스트 PC(loopback `127.0.0.1`/`::1` 또는 서버 자기 LAN IP)에서 접속하면 자동으로 관리자, 외부 PC에서 접속하면 viewer로 분류됩니다.

---

## 7. 자주 발생하는 문제

| 문제                              | 해결 방법                                                                                                                                                                                                                               |
| --------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `python` 명령을 찾을 수 없음      | Python 설치 후 PATH 설정을 확인합니다.                                                                                                                                                                                                  |
| `WEB 실행.bat`에서 DB 초기화 실패 | MySQL 서비스 실행 여부와 `.env`의 DB 계정을 확인합니다.                                                                                                                                                                                 |
| 포트 `8501` 사용 중               | `WEB 실행.bat`가 자동으로 다음 포트를 찾습니다.                                                                                                                                                                                         |
| 패키지 오류                       | `SETUP.bat`를 다시 실행하거나 `.venv`에서 `pip install -r requirements.txt`를 재실행합니다.                                                                                                                                             |
| 관리자 메뉴가 보이지 않음         | 권한이 클라이언트 IP 기반으로 판정됩니다. 호스트 PC(서버 본체)에서 직접 접속하면 자동으로 관리자, 다른 PC에서 LAN으로 접속하면 viewer입니다. 관리자 메뉴가 필요하면 서버 본체에서 브라우저를 열어 `http://localhost:8501`로 접속하세요. |

---

## 8. 참고

- 업로드는 웹 UI의 `데이터 업로드` 탭에서 수행합니다.
- 업로드 허용 형식은 `.xlsx`, `.xls`입니다.
- 웹 UI에서 생성되는 AI 실적 보고서는 DB의 `ai_reports` 테이블에 저장됩니다.

---

## 9. 자동화 .bat 스크립트

`tools/mail/` 폴더에 일일 메일 자동화 스크립트가 제공됩니다(수동 .bat 트리거).
실행 로그는 `logs/automation/<name>_YYYYMMDD.log` 에 저장됩니다.

> MIS 데이터 수집·재가공 RPA는 별도 프로젝트 `../AI-Elite_MIS_RPA/`로 분리되었습니다. 수집 실행은 그 프로젝트의 `mis_rpa/*.bat`를 사용하고, 웹은 서버 기동 시 결과 엑셀(`SAMPLED_DB_DIR`)을 자동 적재합니다.

### 9-1. 일일 에너지 원단위 메일 자동 송부 — `run_daily_mail.bat`

`.env`의 `MAIL_RECIPIENTS`로 D-2 기준일의 공장별 원단위 실적 HTML 메일을 Gmail SMTP로 발송합니다.

```bat
:: 기본 (D-2, .env 설정)
tools\mail\run_daily_mail.bat

:: 특정 기준일
tools\mail\run_daily_mail.bat 2026-05-09

:: 수신자 임시 override
tools\mail\run_daily_mail.bat --to "me@company.com,boss@company.com"

:: 실제 발송 없이 HTML만 logs\automation 에 저장(테스트)
tools\mail\run_daily_mail.bat --dry-run
```

필수 `.env` 항목:

- `SMTP_HOST` (기본 `smtp.gmail.com`)
- `SMTP_PORT` (기본 `587`)
- `SMTP_USER` (보낸이 Gmail 주소)
- `SMTP_APP_PASSWORD` (Gmail 2단계 인증 → 앱 비밀번호 16자리)
- `MAIL_RECIPIENTS` (수신자 콤마 구분)
- `MAIL_CC` (참조자, 선택)
- `DAILY_REPORT_REFERENCE_OFFSET_DAYS` (기본 2 = D-2)

> 💡 Gmail 앱 비밀번호 발급: Google 계정 → 보안 → 2단계 인증 → 앱 비밀번호 (메일/Windows)

### 9-2. 자동화 패키지 의존성

`requirements.txt`에 추가됨:

- `jinja2`, `kaleido`, `pyyaml`
