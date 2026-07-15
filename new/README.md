# AI Elite BEMS Next

기존 `kjw413/AI-Elite-BEMS`의 Streamlit 화면을 React 19 + Next.js 호환 Vinext UI로 전환한 사내망용 프런트엔드입니다. 기존 MySQL, 예측모델, LangChain/OpenAI 보고서, 엑셀 업로드 로직은 버리지 않고 FastAPI 브리지로 연결합니다.

## 달라지는 실행 구조

```text
사내 사용자 브라우저
  ├─ :3000  React/Vinext 운영 UI
  └─ :8000  FastAPI 브리지 ── 로컬 MySQL
                           ├─ v5.3 예측모델
                           ├─ AI 보고서 서비스
                           └─ Excel 업로드/검증 서비스
```

- Streamlit의 전체 스크립트 재실행 방식 대신 필요한 API만 요청합니다.
- 브라우저 렌더링과 Python 계산을 분리해 화면 조작이 DB·모델 실행을 반복 트리거하지 않습니다.
- API는 원래 프로젝트의 서비스 모듈을 호출하므로 모델과 업무 규칙을 이중 구현하지 않습니다.
- 브라우저가 API에 직접 연결하므로 기존의 클라이언트 IP 기반 관리자/조회자 구분을 유지합니다.

## 설치 및 실행 (Windows)

권장 폴더 구조:

```text
work/
  AI-Elite-BEMS/       # 기존 프로젝트와 .venv, .env, MySQL 설정
  ai-elite-bems-next/  # 이 프로젝트
```

1. 기존 `AI-Elite-BEMS/SETUP.bat`를 먼저 완료합니다.
2. Node.js 22.13 이상을 설치합니다.
3. 이 폴더의 `SETUP_LOCAL.bat`를 한 번 실행합니다.
4. `CONFIGURE_FIREWALL.bat`를 관리자 권한으로 한 번 실행합니다.
5. 이후에는 `RUN_BEMS_NEXT.bat`로 실행합니다.
6. 같은 사내망 사용자에게 `http://<서버PC이름>:3000`을 공유합니다.

기존 프로젝트가 형제 폴더가 아니면 실행 전에 환경변수로 경로를 지정합니다.

```bat
set BEMS_CORE_ROOT=C:\work\AI-Elite-BEMS
RUN_BEMS_NEXT.bat
```

## 데이터 보호

- DB 비밀번호와 API 키는 기존 `AI-Elite-BEMS/.env`에서만 읽습니다.
- 브라우저나 React 번들에 DB 계정, OpenAI 키, 기상청 키를 넣지 않습니다.
- 외부 클라우드 DB로 데이터를 복제하지 않습니다.
- 관리자 쓰기 기능은 서버 PC 또는 `BEMS_ADMIN_IPS`에 명시한 IP에만 허용됩니다.

## 화면과 기능

- 에너지 통합 대시보드: KPI, 7일 AI 정상범주, YoY, 공장 비교, 이벤트
- 에너지 사용량: 전력/연료/용수/폐수, 설비 분해, 공장 비교, CSV
- 에너지 원단위: MTD/YTD, 목표, 효율 매트릭스
- 생산실적: 계획 대비 실적, 제품 믹스, Top 품목, What-if
- AI 예측: 실행, 이력, 이상감지, 모델/기상 관리
- AI 보고서: 저장본 열람, 관리자 생성/재생성, 인쇄/PDF
- 관리자: 엑셀 업로드, 자동 동기화, 감사 로그, 이벤트 메모

세부 구조와 전환 범위는 [docs/ARCHITECTURE_KR.md](docs/ARCHITECTURE_KR.md)와 [docs/MIGRATION_SCOPE_KR.md](docs/MIGRATION_SCOPE_KR.md)를 참고하세요.

## 개발 명령

```bash
npm run dev
npm run build
npm run lint
```

FastAPI만 별도로 실행할 때:

```bash
python -m uvicorn backend.server:app --host 0.0.0.0 --port 8000
```

