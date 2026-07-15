# AI Elite BEMS Next

기존 `kjw413/AI-Elite-BEMS`의 Streamlit 화면을 React 19 + Next.js 15 UI로 전환 중인 사내망용 프로젝트입니다. 기존 MySQL, 예측모델, LangChain/OpenAI 보고서, 엑셀 업로드 로직은 버리지 않고 FastAPI 브리지로 연결합니다.

## 달라지는 실행 구조

```text
사내 사용자 브라우저
  ├─ :3000  React/Next.js 운영 UI
  └─ :8000  FastAPI 브리지 ── 로컬 MySQL
                           ├─ v5.3 예측모델
                           ├─ AI 보고서 서비스
                           └─ Excel 업로드/검증 서비스
```

- Streamlit의 전체 스크립트 재실행 방식 대신 필요한 API만 요청합니다.
- 브라우저 렌더링과 Python 계산을 분리해 화면 조작이 DB·모델 실행을 반복 트리거하지 않습니다.
- 모델·보고서·업로드 동작은 기존 서비스에 위임하고, 조회 집계와 API 표시 단위는 브리지에서 명시적으로 관리합니다.
- 브라우저가 API에 직접 연결하므로 기존의 클라이언트 IP 기반 관리자/조회자 구분을 유지합니다.

## 설치 및 실행 (Windows)

현재 저장소 구조:

```text
AI-Elite-BEMS/
  legacy/  # 기존 Streamlit 소스, .env, 모델 (Next 작업에서는 읽기 전용)
  new/     # React/Next.js + FastAPI 마이그레이션 버전
```

1. `legacy/`에 소스, `.env`, 모델 파일과 `requirements.txt`가 있는지 확인합니다.
2. legacy와 호환되는 Python 및 Node.js 22.13 이상을 설치합니다.
3. 이 폴더의 `SETUP_LOCAL.bat`를 한 번 실행합니다. 모든 Python 패키지는 `new/.venv`에 설치됩니다.
4. `CONFIGURE_FIREWALL.bat`를 관리자 권한으로 한 번 실행합니다.
5. 이후에는 `RUN_BEMS_NEXT.bat`로 실행합니다. 기본 동작은 현재 소스를 다시 빌드한 뒤 시작하는 것입니다.
6. 같은 사내망 사용자에게 `http://<서버PC이름>:3000`을 공유합니다.

기존 프로젝트가 기본 위치(`../legacy`)가 아니면 실행 전에 환경변수로 경로를 지정합니다.

```bat
set BEMS_CORE_ROOT=C:\work\AI-Elite-BEMS\legacy
RUN_BEMS_NEXT.bat
```

검증 완료한 기존 번들을 의도적으로 재사용할 때만 `BEMS_SKIP_BUILD=1`을 지정할 수
있습니다. API 주소를 별도로 지정하지 않으면 브라우저가 현재 접속한 서버 호스트의
`:8000/api/v1`을 자동 사용합니다. 별도 주소가 필요하면
`.env.local.example`을 `.env.local`로 복사해 `NEXT_PUBLIC_BEMS_API_BASE`를 설정합니다.

## 데이터 보호

- DB 비밀번호와 API 키는 서버 프로세스 환경에서만 읽습니다. 시작할 때 기존
  `legacy/.env`도 서버 환경으로 불러오며 브라우저에는 전달하지 않습니다.
- `DB_VIEWER_USER`·`DB_VIEWER_PASSWORD`는 필수입니다. FastAPI 직접 조회에는 DB에서
  `SELECT`만 부여한 계정을 사용해야 하며, 실제 grant는 운영 전 별도로 확인합니다.
  쓰기 작업은 관리자 권한 검사 후 기존 서비스에 위임합니다.
- 브라우저나 React 번들에 DB 계정, OpenAI 키, 기상청 키를 넣지 않습니다.
- 외부 클라우드 DB로 데이터를 복제하지 않습니다.
- 관리자 쓰기 기능은 서버 PC 또는 `BEMS_ADMIN_IPS`에 명시한 IP에만 허용됩니다.
- 허용할 프런트 Origin을 추가해야 하면 API 실행 환경의 `BEMS_ALLOWED_ORIGINS`에
  `http://호스트:3000` 형식으로 콤마 구분해 지정합니다.
- 설치·실행 스크립트와 FastAPI는 Python bytecode 생성을 끄고 `legacy/`에 파일이나
  패키지를 쓰지 않습니다.

## 현재 구현 상태

- 구현됨: 반응형 셸, 공장·기준일 필터, 역할·DB 연결 상태
- 구현됨: 대시보드, 사용량, 원단위, 생산실적, AI 예측의 핵심 조회 화면
- 구현됨: FastAPI 조회·관리 API와 API 실패 시 예시 데이터 fallback
- 검증됨: `npm run typecheck`, 프로덕션 빌드, HTTP 200 기동 smoke, 백엔드 단위 테스트 17건
- 미구현: AI 보고서 및 관리자 React 화면
- 미구현: 예측 실행·재학습·기상 동기화·What-if·이상 진단 UI
- 미검증: 실제 브라우저 차트·반응형·콘솔, 실제 사내 DB 수치·권한 동등성

세부 구조와 전환 범위는 [docs/ARCHITECTURE_KR.md](docs/ARCHITECTURE_KR.md)와 [docs/MIGRATION_SCOPE_KR.md](docs/MIGRATION_SCOPE_KR.md)를 참고하세요.

## 개발 명령

```bash
npm run dev
npm run typecheck
npm run build
```

FastAPI만 별도로 실행할 때:

```bash
.venv\Scripts\python.exe -B -m uvicorn backend.server:app --host 0.0.0.0 --port 8000
```

DB 없이 실행 가능한 백엔드 helper 회귀 테스트:

```bash
.venv\Scripts\python.exe -B -m unittest discover -s backend/tests -v
```
