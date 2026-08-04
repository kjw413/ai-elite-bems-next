# 절감 테마 엑셀 템플릿 다운로드 · 일괄 업로드 설계

> 작성 2026-08-03 · 대상 레포 `AI-Elite-BEMS-next`
>
> 선행: 절감 테마 CRUD·검증([savings_theme_service.py](../backend/app/services/savings_theme_service.py),
> [savings_verification_service.py](../backend/app/services/savings_verification_service.py)) 구현 완료
>
> **이 문서만 보고 단독 구현할 수 있도록** 양식·검증 규칙·수정 지점·함정을 모두 담았습니다.

---

## 1. 배경과 목표

현재 관리자 화면의 절감 테마 등록은 **테마 1건 → 폼 저장 → 행 선택 → 월별 12칸 입력 → 저장**의
반복입니다. 테마가 10건이면 폼 10회 + 월별 그리드 10회, 입력 칸으로는 **240칸(10 × 12개월 × 2)**
을 일일이 채워야 합니다. 현장에서 절감 계획은 이미 엑셀로 관리되고 있으므로, 그 파일을 그대로
올리는 경로가 있으면 이 공수가 통째로 사라집니다.

기존에 같은 문제를 이미 두 번 풀었습니다 — 참고할 자산이 있습니다:

| 자산 | 위치 | 이 작업에서 쓰는 부분 |
|---|---|---|
| 엑셀 업로드 2단계(미리보기→반영) | [`upload_service.py`](../backend/app/services/upload_service.py) · `/upload/preview`, `/upload` | 파이프라인 구조·미리보기 UX·`_read_valid_excel` |
| 파일 선택 UI | `admin-screen.tsx` `DataPanel` (`.file-picker`, `FormData`) | 프론트 업로드 컴포넌트 패턴 |
| 붙여넣기 그리드 | `admin-screen.tsx` `MonthlyBackfillPanel` | 대안으로 검토했으나 아래 §2에서 기각 |

`openpyxl 3.1.5`가 이미 설치돼 있어(`requirements-core.txt`) 신규 의존성이 없습니다.

## 2. 방식 결정 — 왜 "붙여넣기"가 아니라 "파일"인가

`MonthlyBackfillPanel`에 이미 엑셀 붙여넣기(Ctrl+V) 그리드가 있으므로 그 패턴을 재사용하는
안도 있었습니다. 기각한 이유:

- 붙여넣기 그리드는 **한 테마의 12개월**을 채우는 데는 좋지만, 테마 10건은 **테마마다 그리드를
  열어 10번 붙여넣어야** 합니다 — 공수가 절반밖에 안 줄어듭니다.
- 테마 마스터(테마명·공장·에너지원·분류·시행월·담당·투자비)는 그리드 형태가 아니라 **행 단위
  레코드**입니다. 붙여넣기 그리드로 표현하기 어색합니다.
- 현장 원본이 이미 `.xlsx` 파일이라, 파일을 그대로 올리는 편이 중간 단계가 없습니다.

→ **템플릿 다운로드 + 파일 업로드**로 간다. 기존 붙여넣기 그리드는 그대로 둔다(월별 백필용).

---

## 3. 엑셀 양식

### 3-1. 시트 구성 — 단일 시트

테마 마스터와 월별 계획/실적을 **한 행에 모두** 담습니다. 시트를 둘로 나누면(테마 시트 +
월별 시트) 사용자가 테마명으로 두 시트를 수동 대조해야 하고, 오타 하나로 연결이 끊깁니다.

시트명: **`절감테마`** (이름 고정 — 못 찾으면 첫 번째 시트를 사용하고 경고)

### 3-2. 열 정의

요구사항: *"현재 절감 등록 탭에서 상태 빼고 모두 포함"* + *"월별 절감 계획/실적"*.

현재 폼 필드는 테마명·공장·에너지원·분류·**상태**·시행월·담당·투자비·메모입니다. 여기서
**상태를 제외**하고, 월별 계획/실적 24열을 붙입니다.

| # | 열 머리글 | 필수 | 형식 / 허용값 | 비고 |
|---:|---|:---:|---|---|
| A | `테마명` | ✅ | 텍스트(200자 이내) | 같은 공장·연도에서 유일해야 함(UPSERT 키) |
| B | `공장` | ✅ | 남양주1 / 남양주2 / 김해 / 광주 / 논산 / 경산 | 집계 라벨(전사·남양주) 금지 |
| C | `에너지원` | ✅ | 전력 / 연료 / 용수 | 한글 라벨로 입력 → 내부 코드 변환 |
| D | `분류` | | 설비교체 / 운전개선 / 공정개선 / 누설저감 / 계약변경 / 기타 | 비우면 NULL |
| E | `시행월` | | `YYYY-MM` (예: 2026-03) | 비우면 검증이 "판정 보류" |
| F | `담당` | | 텍스트(60자) | |
| G | `투자비(원)` | | 숫자 | 회수기간 산출용 |
| H | `메모` | | 텍스트 | |
| I~T | `1월 계획` … `12월 계획` | | 숫자 | 단위는 에너지원별(kWh/Nm³/ton) |
| U~AF | `1월 실적` … `12월 실적` | | 숫자 **또는 공란** | **공란 = 미입력**(0과 다름) |

> **상태(status)는 양식에 없습니다.** 신규 등록은 `planned`, 기존 테마 갱신은 **현재 상태를
> 유지**합니다(§4-3). 상태는 진행 중 수시로 바뀌는 값이라 일괄 업로드로 덮으면 현장에서
> 바꿔둔 값이 되돌아갑니다.

### 3-3. 단위 — ton 변환 없음

`MonthlyBackfillPanel`은 생산량을 화면에서 ton으로 받아 kg로 저장(×1000)하지만, **절감량은
변환하지 않습니다.** `savings_record.planned_qty`/`actual_qty`는 에너지원의 자연 단위
(kWh·Nm³·ton)를 그대로 쓰고 현재 관리자 그리드도 같은 단위로 입력받습니다. 양식도 동일하게
두어 **화면 입력값과 엑셀 값이 1:1**이 되게 합니다 — 두 경로의 단위가 다르면 반드시 사고가 납니다.

### 3-4. 템플릿 다운로드에 담을 것

빈 양식이 아니라 **현재 등록된 테마를 채워서** 내려줍니다(`?year=&factory=`).

- 이미 등록된 테마가 있으면 그 값이 채워진 채로 내려와 **수정 → 재업로드** 경로가 그대로 생김
- 등록된 게 없으면 머리글 + 예시 1행(회색 안내 서식)
- 헤더 행 고정(freeze panes), 열 너비 지정, 숫자 열 표시형식 `#,##0`
- **드롭다운 검증**(openpyxl `DataValidation`)을 공장·에너지원·분류 열에 걸어 오타를 원천 차단
- 첫 시트 뒤에 **`안내`** 시트 — 필수/선택 열, 허용값, "공란 = 미입력", 상태 제외 사유

---

## 4. 백엔드

### 4-1. 신규 서비스 `savings_excel_service.py`

`upload_service.py`의 구조(파싱 → 검증 → 미리보기/반영)를 따르되, 대상 테이블만 다릅니다.
DB 접근은 기존 `savings_theme_service`에 위임하고 이 모듈은 **엑셀 ↔ payload 변환과 검증**만
담당합니다.

```python
TEMPLATE_SHEET = "절감테마"
HEADER_ROW = 1

def build_template(themes: list[dict], records: dict[int, dict], year: int) -> bytes:
    """현재 등록분을 채운 .xlsx 바이트. themes 가 비면 머리글+예시행만."""

def parse_workbook(content: bytes) -> tuple[list[ParsedTheme], list[RowError]]:
    """시트 → (테마 payload + 월별 items) 목록, 행/열 단위 오류 목록."""

def preview(parsed: list[ParsedTheme], existing: list[dict]) -> dict:
    """dry-run — 신규/갱신 건수, 테마별 월 입력 칸 수, 경고. DB 미변경."""
```

### 4-2. 검증 규칙 — 실패는 **행 단위로 모아서** 보고

`upload_service.validate_all()`이 이미 쓰는 방식입니다. 첫 오류에서 멈추지 말고 전 행을 검사해
한 번에 돌려줘야, 사용자가 엑셀을 한 번만 고칩니다.

| 검사 | 실패 시 |
|---|---|
| 시트 존재 / 머리글 일치 | 전체 거부 — "양식이 다릅니다. 템플릿을 다시 받으세요." |
| 테마명 공란 | 그 행 거부 |
| 공장이 물리 공장 목록에 없음 | 그 행 거부 (집계 라벨 입력이 여기 걸림) |
| 에너지원이 전력/연료/용수 아님 | 그 행 거부 |
| 시행월이 `YYYY-MM` 아님 | 그 행 거부 |
| 월별 값이 숫자로 안 읽힘 | 그 행 거부 + 어느 칸인지 명시 |
| **파일 안에서 (공장, 테마명) 중복** | 그 행 거부 — DB UPSERT 전에 잡아야 함 |
| 투자비가 음수 | 그 행 거부 |

숫자 파싱은 프론트 `parseNumericCell`과 같은 규칙(천단위 콤마·`₩`·공백 제거)을 백엔드에도
둡니다 — 엑셀 셀이 문자열로 들어오는 경우가 실제로 있습니다.

### 4-3. UPSERT 정책

키는 `savings_theme` 의 `UNIQUE (factory, year, title)` 입니다.

- **없으면 INSERT** — `status`는 `planned`
- **있으면 UPDATE** — `status`는 **DB 현재값 유지**(§3-2), 나머지 열은 파일 값으로 덮어씀
- 월별: `upsert_records(theme_id, year, items)` 재사용 — 계획 0 + 실적 공란이면 그 행 삭제
- **파일에 없는 기존 테마는 건드리지 않는다** — 업로드는 "동기화"가 아니라 "추가·갱신"

> ⚠ `upsert_records`는 **넘긴 월만** 처리합니다. 12개월 전부를 items로 만들어 보내면 값이 빈
> 달은 삭제되어, 파일에서 비운 달이 DB에서도 지워집니다 — 이것이 의도한 동작입니다(파일이
> 그 연도의 진실). 이 점을 미리보기 문구에 명시할 것.

### 4-4. 엔드포인트 3개

| Method | Path | 권한 | 반환 |
|---|---|---|---|
| `GET` | `/api/v1/savings/template` | admin | `.xlsx` 바이트 (`StreamingResponse`) |
| `POST` | `/api/v1/savings/upload/preview` | admin | 신규/갱신 건수 · 행별 오류 (DB 미변경) |
| `POST` | `/api/v1/savings/upload` | admin | 반영 건수 |

- 쿼리: `template?year=2026&factory=남양주1`(factory 생략 시 전 공장)
- 업로드 본문은 `multipart/form-data`, `_read_valid_excel()` 재사용(확장자·50MB 제한)
- 파일명: `절감테마_2026.xlsx` — `Content-Disposition` 에 **RFC 5987 `filename*=UTF-8''...`**
  로 인코딩할 것. 한글 파일명을 `filename=` 에 그대로 넣으면 브라우저가 깨뜨립니다.

```python
@app.get("/api/v1/savings/template")
def savings_template(request: Request, year: int, factory: str | None = None):
    require_admin(request)
    ...
    return StreamingResponse(
        BytesIO(content),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}"},
    )
```

> `StreamingResponse`는 아직 이 프로젝트에서 쓰인 적이 없습니다 — `server.py` 상단
> `from fastapi.responses import JSONResponse` 에 함께 import 해야 합니다.

---

## 5. 프론트엔드

`SavingsThemePanel`(admin-screen.tsx) 상단에 업로드 카드를 추가합니다. 기존 폼·목록·월별
그리드는 **그대로 둡니다** — 1~2건 고칠 때는 폼이 더 빠릅니다.

```
┌─ 절감 테마 일괄 등록 ─────────────────────────────┐
│ [연도 2026] [공장 전체 ▾]                          │
│  ⬇ 템플릿 다운로드    📄 파일 선택    ▶ 1단계·검증  │
└───────────────────────────────────────────────────┘
        ↓ (검증 통과 시)
┌─ 미리보기 — DB 미반영 ────────────────────────────┐
│ 신규 7건 · 갱신 3건 · 월별 입력 118칸              │
│ ┌────────────────────────────────────────────┐    │
│ │ 테마명        공장    구분  시행월  신규/갱신│    │
│ └────────────────────────────────────────────┘    │
│              [2단계 · DB 반영]                     │
└───────────────────────────────────────────────────┘
```

- 2단계 버튼은 `window.confirm`으로 건수를 다시 확인(`DataPanel`과 동일)
- 오류가 있으면 **행 번호 · 열 이름 · 사유** 표로 표시 — 엑셀에서 바로 찾아 고치게
- 반영 성공 시 목록 새로고침(`load()`), 파일 입력 초기화
- 다운로드는 `apiRequest`(JSON 전제)로는 안 됩니다 — `fetch` + `blob()` + `URL.createObjectURL`
  로 별도 처리하거나, `bems-api.ts` 에 `apiDownload(path)` 헬퍼를 신설

---

## 6. 수정 지점

| # | 파일 | 작업 |
|---|---|---|
| 1 | `backend/app/services/savings_excel_service.py` | **신규** — 템플릿 생성·파싱·검증·미리보기 |
| 2 | `backend/server.py` | 엔드포인트 3개 + `StreamingResponse` import |
| 3 | `backend/app/services/savings_theme_service.py` | `upsert_theme_by_key()` 추가(있으면 UPDATE·상태 유지, 없으면 INSERT) |
| 4 | `lib/bems-api.ts` | `apiDownload(path, filename)` 헬퍼 |
| 5 | `components/screens/admin-screen.tsx` | `SavingsThemePanel` 에 업로드 카드 + 미리보기 |
| 6 | `app/globals.css` | 오류 표 스타일(기존 `.upload-panel`·`.form-message` 재사용, 소폭) |
| 7 | `backend/tests/test_server_helpers.py` | 파싱·검증·UPSERT 정책 단위테스트 |

## 7. 테스트 계획

**단위** — DB 없이 순수 함수로:
1. 정상 양식 파싱 → payload 구조·월별 items 개수
2. 각 검증 실패 케이스가 **행 번호와 열 이름**을 정확히 지목하는지
3. 파일 내 (공장, 테마명) 중복 감지
4. 실적 공란 → `None`, 실적 `0` → `0.0` (**미입력과 0의 구분** — 이 프로젝트의 핵심 관례)
5. 숫자 파싱: `"1,234"`, `"₩1,234"`, `" 1234 "` → 1234
6. 라운드트립: `build_template()` 결과를 `parse_workbook()` 에 넣으면 원본 payload 복원

**통합** — 실제 DB로:
7. 템플릿 다운로드 → 값 채움 → 업로드 → 목록/월별 값이 화면 입력과 동일한지
8. 같은 파일 재업로드(멱등) — 갱신 0건 증가, 값 불변
9. 상태를 `done`으로 바꾼 뒤 재업로드 → **상태가 `done`으로 유지되는지**(§4-3 핵심)
10. 업로드 후 `/savings` 의 금액·달성률·검증이 정상 산출되는지

## 8. 구현 순서

| 단계 | 범위 | 확인 |
|---|---|---|
| 1 | `savings_excel_service` 파싱·검증 + 단위테스트 1~6 | 테스트 통과 |
| 2 | `build_template()` + `GET /savings/template` | 브라우저에서 받아 엑셀로 열림·드롭다운 동작 |
| 3 | `upsert_theme_by_key()` + 업로드 2개 엔드포인트 | 통합테스트 7~9 |
| 4 | 프론트 업로드 카드 + 미리보기 | 실제 10건 파일로 왕복 |

1~3단계가 백엔드 완결이라 그 시점에 `curl`로 전 경로를 검증할 수 있습니다.

## 9. 리스크 · 결정 필요

| # | 사안 | 대응 |
|---:|---|---|
| 1 | **상태 열 제외** | 요구사항대로 제외. 기존 테마의 상태는 유지(§4-3). 일괄로 상태를 바꾸고 싶다는 요구가 나오면 열을 추가하되 "공란이면 유지" 규칙 필요 |
| 2 | **파일에 없는 테마** | 삭제하지 않음(추가·갱신 전용). "파일에 없으면 지운다"가 필요하면 별도 옵션으로 — 기본값으로 두면 사고가 납니다 |
| 3 | 월별 24열로 가로가 넓음 | 계획/실적을 위아래 2행으로 나누는 안도 있으나, 행=테마 1:1이 깨져 파싱·UPSERT가 복잡해집니다. 24열 유지 + 헤더 고정으로 대응 |
| 4 | 연도가 파일에 없음 | 업로드 화면의 연도 선택값을 사용. 파일에 `연도` 열을 두면 화면 선택과 어긋날 때 어느 쪽이 맞는지 모호해집니다 |
| 5 | 한글 파일명 | RFC 5987 인코딩 필수(§4-4) |
| 6 | 용수 테마 | 금액이 산출되지 않지만(단가 미관리) 등록·절감량 관리는 정상. 템플릿 `안내` 시트에 명시 |
