# AI Elite BEMS Next 서버 실행 및 웹 접속 가이드

이 문서는 서버 PC에서 AI Elite BEMS Next를 설치·실행하고, 같은 사내망 사용자가 웹으로 접속하는 절차를 정리한다.

## 1. 구성과 접속 주소

```text
서버 PC
  new/RUN_BEMS_NEXT.bat
    ├─ React·Next.js UI      :3000
    └─ FastAPI 브리지        :8000
         └─ new/backend/app 코어(예측 모델·보고서·업로드 서비스)
              ── 로컬 MySQL · E:\Sampled DB 엑셀
```

legacy/ 폴더는 참조·비교 검증용이며 실행에 필요하지 않다 (2026-07-16 독립화).

| 용도 | 서버 PC에서 | 같은 사내망 다른 PC에서 |
|---|---|---|
| BEMS 웹 화면 | `http://localhost:3000` | `http://<서버PC이름>:3000` |
| API 문서 | `http://localhost:8000/api/docs` | `http://<서버PC이름>:8000/api/docs` |
| API 상태 확인 | `http://localhost:8000/api/v1/health` | `http://<서버PC이름>:8000/api/v1/health` |
| 접속 역할 확인 | `http://localhost:8000/api/v1/session` | `http://<서버PC이름>:8000/api/v1/session` |

일반 사용자는 `:3000` 웹 화면만 사용한다. `:8000`은 API와 상태 점검용이며, 외부 인터넷에 공개하지 않는다.

## 2. 최초 1회 준비

서버 PC에서 `new` 폴더를 열고 다음을 확인한다.

1. `backend/.env`(DB·API 키)와 `backend/app/predictive model/`(모델·휴일·기상 자산)이 있다.
   두 항목은 git에 포함되지 않으므로 새 PC 배포 시 기존 서버에서 별도 복사한다.
2. Node.js 22.13 이상과 Python 3.11+가 설치돼 있다.
3. MySQL 서비스가 실행 중이고 조회 전용 계정이 준비돼 있다.

그 다음 `new/SETUP_LOCAL.bat`를 한 번 실행한다. 탐색기에서 더블클릭하거나 명령 프롬프트에서 실행할 수 있다.

```bat
cd /d E:\AI-Elite-BEMS\new
SETUP_LOCAL.bat
```

이 스크립트는 다음을 수행한다.

- `new/.venv`에 `backend/requirements-core.txt`(코어 런타임)와 FastAPI 의존성을 설치한다.
- `package-lock.json` 기준으로 Node 패키지를 설치한다.
- 현재 소스의 Next.js 프로덕션 빌드를 검증한다.
- `legacy/` 폴더는 참조하지도, 변경하지도 않는다.

사내 프록시나 인증서 때문에 `npm ci`가 실패하면 TLS 검증을 끄지 말고 사내 인증서·프록시 설정을 확인한다.

### 방화벽 설정

다른 PC 접속이 필요하면 서버 PC에서 `new/CONFIGURE_FIREWALL.bat`를 **관리자 권한**으로 한 번 실행한다.

```bat
cd /d E:\AI-Elite-BEMS\new
CONFIGURE_FIREWALL.bat
```

이 스크립트는 Domain/Private 프로필에 TCP 3000과 8000 인바운드 규칙을 추가한다. Public 프로필이나 외부 인터넷에 포트를 열지 않는다.

## 3. 필수 환경 설정

FastAPI는 `backend/.env`를 우선 읽고, 전환기 호환을 위해 없는 키만
`legacy/.env`에서 보충한다. 실제 서버에서는 다음 값을 확인한다.

| 변수 | 용도 | 필수 여부 |
|---|---|---|
| `DB_VIEWER_USER` | FastAPI 직접 조회용 MySQL 계정 | 필수 |
| `DB_VIEWER_PASSWORD` | 위 조회 계정 비밀번호 | 필수 |
| `BEMS_ADMIN_IPS` | 서버 PC 외에 관리자 권한을 줄 사내 IP 목록(콤마 구분) | 선택 |
| `BEMS_ALLOWED_ORIGINS` | 기본 목록 외 허용할 프런트 Origin, 예: `http://bems-alias:3000` | 선택 |
| `BEMS_CORE_ROOT` | 전환기 `.env` 보충 경로(기본 `../legacy`)가 다를 때 지정 | 선택 |

`DB_VIEWER_USER`에는 MySQL의 `SELECT` 권한만 부여한다. DB 비밀번호, OpenAI 키, 기상청 키는 `.env.local`이나 브라우저 코드에 넣지 않는다.

기존 프로젝트가 다른 경로에 있으면 같은 명령 창에서 실행 전에 지정한다.

```bat
set BEMS_CORE_ROOT=C:\work\AI-Elite-BEMS\legacy
RUN_BEMS_NEXT.bat
```

프런트와 API가 같은 서버 PC에서 실행되면 API 주소를 따로 설정할 필요가 없다. 브라우저 주소의 호스트명에 `:8000/api/v1`을 자동으로 사용한다. 별도 API 주소가 필요한 경우에만 `.env.local.example`을 `.env.local`로 복사하고 `NEXT_PUBLIC_BEMS_API_BASE`를 설정한다. 이 파일에는 공개해도 되는 URL만 넣는다.

## 4. 서버 시작

서버 PC에서 다음을 실행한다.

```bat
cd /d E:\AI-Elite-BEMS\new
RUN_BEMS_NEXT.bat
```

기본 동작은 다음과 같다.

1. `new/.venv`, legacy 경로, npm, curl과 포트 3000을 점검한다.
2. `BEMS_SKIP_BUILD=1`을 지정하지 않았다면 현재 소스를 프로덕션 빌드한다.
3. FastAPI를 `0.0.0.0:8000`으로 시작하고 최대 20초 동안 `/api/v1/session` 준비 상태를 확인한다.
4. Next.js UI를 `0.0.0.0:3000`으로 시작한다.

성공하면 명령 창에 다음과 비슷한 주소가 표시된다.

```text
BEMS Next running
UI  : http://<서버PC이름>:3000
API : http://<서버PC이름>:8000/api/docs
```

이 명령 창을 닫으면 웹 UI가 종료된다. API는 별도 `BEMS API :8000` 창으로 실행되므로, 완전히 종료하려면 해당 창도 닫는다.

이미 검증된 빌드를 재사용해야 할 때만 다음처럼 실행한다. 소스를 바꾼 뒤에는 사용하지 않는다.

```bat
set BEMS_SKIP_BUILD=1
RUN_BEMS_NEXT.bat
```

## 5. 웹 접속과 권한 확인

### 서버 PC 관리자

서버 PC의 브라우저에서 `http://localhost:3000` 또는 표시된 서버 PC 이름 주소를 연다. 기본 정책상 서버 PC의 loopback·자기 IP는 관리자다.

### 같은 사내망 조회 사용자

다른 PC에서는 다음 주소를 연다.

```text
http://<서버PC이름>:3000
```

기본적으로 다른 사내 PC는 조회 사용자다. 관리자 권한이 필요한 PC는 서버의 `BEMS_ADMIN_IPS`에 해당 PC IP를 추가한 뒤 FastAPI를 재시작한다.

화면 상단의 역할 표시 또는 `/api/v1/session` 응답의 `role` 값으로 판정 결과를 확인한다. 화면에서 버튼이 보이지 않더라도 서버 API가 업로드·보고서 생성·이벤트 수정·예측 실행을 다시 권한 검사한다.

## 6. 시작 후 점검

1. 서버 PC에서 `/api/v1/session`이 JSON을 반환하는지 확인한다.
2. `/api/v1/health`가 `status: "ok"`, `database: "mysql"`을 반환하는지 확인한다.
3. 웹 화면에서 `Local DB` 상태와 올바른 역할이 표시되는지 확인한다.
4. 다른 PC에서 `http://<서버PC이름>:3000` 접속과 viewer 역할을 확인한다.

웹 화면에 `API 연결 실패 · 예시 데이터 표시 중` 경고가 보이면 실제 운영 수치가 아니다. API 주소, 8000 포트, MySQL 연결과 `DB_VIEWER_*` 설정을 먼저 확인한다.

## 7. 메일 리포트 자동화 (선택)

일일·주간·월간 에너지 원단위 메일은 `new/backend/tools/mail`에서 legacy 없이 실행된다.
SMTP·수신자 설정은 `backend/.env`의 기존 메일 키를 그대로 사용한다.

발송 전 테스트(실제 발송 없이 HTML만 `backend/logs/automation`에 저장):

```bat
cd /d E:\AI-Elite-BEMS\new\backend\tools\mail
run_mail.bat daily --dry-run
```

작업 스케줄러 등록(기존 legacy 등록과 같은 작업 이름 `FEMS_Mail_*`을 덮어쓰므로,
실행하면 예약 작업이 new 쪽 실행기로 전환된다):

```bat
cd /d E:\AI-Elite-BEMS\new\backend\tools\mail
REGISTER_MAIL_SCHEDULE.bat
```

해제는 같은 폴더의 `UNREGISTER_MAIL_SCHEDULE.bat`를 실행한다.

## 8. 자주 발생하는 문제

| 증상 | 확인·조치 |
|---|---|
| `new/.venv not found` | `SETUP_LOCAL.bat`를 먼저 실행한다. |
| Node.js 22.13+ 오류 또는 `npm ci` 실패 | Node 버전과 사내 프록시·인증서 설정을 확인한다. TLS 검증을 끄지 않는다. |
| 포트 3000 사용 중 | 기존 BEMS 웹 명령 창을 정상 종료한다. 다른 프로그램이면 담당자 확인 후 처리한다. |
| 포트 8000이 BEMS API가 아님 | 임의 프로세스를 강제 종료하지 말고 포트를 사용하는 서비스 담당자를 확인한다. |
| API가 20초 안에 준비되지 않음 | `BEMS API :8000` 창의 오류, MySQL 실행 여부, `backend/.env`, `DB_VIEWER_*`와 DB grant를 확인한다. |
| 다른 PC에서 접속 불가 | 서버 PC 이름/IP, 같은 사내망 여부, Windows 방화벽 Domain/Private 규칙, 3000 포트를 확인한다. |
| 브라우저 CORS 오류 | 접속 주소와 `BEMS_ALLOWED_ORIGINS`를 `http://호스트:3000`의 정확한 값으로 맞춘다. |
| 원격 PC가 관리자여야 함 | 해당 PC의 고정 또는 예약 IP를 `BEMS_ADMIN_IPS`에 추가하고 FastAPI를 재시작한다. |

## 9. 운영 종료·재시작

정상 종료는 UI를 실행한 명령 창에서 `Ctrl+C`를 누른 뒤, 별도 `BEMS API :8000` 창도 닫는 방식으로 한다. 재시작할 때는 두 프로세스가 종료된 것을 확인한 뒤 `RUN_BEMS_NEXT.bat`를 다시 실행한다.

소스 코드만 바뀌었다면 `RUN_BEMS_NEXT.bat`가 자동으로 다시 빌드한다. Node·Python 의존성이나 잠금파일이 바뀌었다면 `SETUP_LOCAL.bat`를 다시 실행해 설치와 빌드를 검증한다. 일반 재시작만 필요하면 `RUN_BEMS_NEXT.bat`만 실행하면 된다.

## 10. 운영 전 최소 체크리스트

- [ ] 서버 PC의 MySQL·`backend/.env`·`backend/app/predictive model` 준비 확인
- [ ] `DB_VIEWER_USER`와 `DB_VIEWER_PASSWORD` 설정 및 SELECT grant 확인
- [ ] `SETUP_LOCAL.bat` 성공
- [ ] `CONFIGURE_FIREWALL.bat` 관리자 실행
- [ ] `RUN_BEMS_NEXT.bat` 성공 및 `/api/v1/health` 확인
- [ ] 서버 PC admin, 다른 PC viewer 권한 확인
- [ ] 대시보드·생산량·원단위·예측 수치가 legacy와 일치하는지 확인
- [ ] 복사 DB에서 Excel 업로드와 보고서 생성 검증

실제 DB·모델·OpenAI·Excel 결과의 동등성 검증은 아직 운영 서버에서 수행해야 한다. 기능 범위와 남은 항목은 [docs/MIGRATION_SCOPE_KR.md](docs/MIGRATION_SCOPE_KR.md), 전체 전환 계획은 [docs/AI_Elite_BEMS_Next_작업정리_및_향후계획.md](docs/AI_Elite_BEMS_Next_작업정리_및_향후계획.md)를 참고한다.
