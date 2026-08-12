# 전사 경산 포함/미포함 구분 · 경산 월별 실적 적재 설계서

> 작성 2026-07-30 · 대상 레포 `AI-Elite-BEMS-next`
>
> **이 문서만 보고 다른 PC에서 단독 구현할 수 있도록** 배경·스키마·수정 지점·함정·검증을
> 모두 담았습니다. 이 대화를 다시 읽을 필요는 없습니다.
> 선행 작업: 경산 2026-04 이전 실적 DB 삭제 완료(2026-07-30, 수동 SQL) ·
> [에너지 단가·비용 적재계획](AI_Elite_BEMS_Next_에너지_단가비용_적재계획.md)

---

## 1. 배경

경산(F50)은 신설 공장이라 **다른 공장과 데이터 시작점이 다릅니다.**

| 구분 | 경산 보유 시작 | 비고 |
|---|---|---|
| 일단위 실적 | **2026-04** | 그 이전 오염분은 2026-07-30 DB에서 삭제 완료 |
| 월단위 실적 | **2025-01** | 사용량·**비용(전력비·연료비)**·생산량 모두 보유. ⚠ 아직 DB에 없음 |
| 타 5개 공장 | 2024-01 | `DB_에너지.xlsx` 기준 |

이 때문에 **전사 집계가 전년비에서 동일 기준 비교가 되지 않습니다.** 전년에는 경산이
없고 금년에는 있으므로 증가분에 경산이 통째로 얹힙니다. 실측 사례가
[`server.py:1757`](../backend/server.py#L1757) 주석에 남아 있습니다:

> 경산이 2025년 0원이라 **전사 전력비가 +13.9%로 보이지만, 경산을 빼면 오히려 감소**다.

현재는 비용 화면의 원인분해에서만 `_fully_priced_members()` 로 우회하고 안내 문구를
띄우는 국소 대응입니다. **전 화면에서 사용자가 직접 기준을 고를 수 있어야 합니다.**

> **경산은 IC(아이스크림) 단일 제품유형 공장입니다**(냉동,
> [`factories.py:26`](../backend/app/domain/factories.py#L26)). 월별 생산량이 품목 분해
> 없는 총량뿐이지만 **그 총량이 곧 IC 실적**이므로, 제품유형 축은 추정 없이 정확히
> 채울 수 있습니다.

## 2. 결정 사항

| 항목 | 결정 | 근거 |
|---|---|---|
| 노출 방식 | 공장 드롭다운에 **`전사(경산 제외)`** 항목 추가 | `factory` 파라미터만 늘리면 전 페이지 자동 적용 |
| 기본 `전사` | **경산 포함(현행 유지)** | 기존 화면 수치 불변 — 회귀 위험 최소 |
| 경산 월별 | **적재한다** (사용량·비용·생산량) | 월별 전년비를 동일 기준으로 만들기 위해 |
| 일별 경로 | **손대지 않는다** | 2026-04 이전이 비어 보이는 것이 정확한 표현 |

**작업 A(전사 구분)와 작업 B(월별 적재)는 서로 독립입니다.** A만 먼저 배포해도 일별·기간
화면에서 즉시 동일 기준 비교가 됩니다. A → B 순서를 권장합니다.

---

# 작업 A — `전사(경산 제외)` 집계 라벨 추가

## A-1. 라벨 등록 (3곳)

`factory_clause()` / `physical_factory_members()` / `expand_factory_members()` 가 모두
**딕셔너리 조회 기반**이라, 아래 3곳에 등록하면 DB 필터는 그대로 동작합니다.

| # | 파일 : 행 | 심볼 | 작업 |
|---|---|---|---|
| 1 | `app/domain/factories.py` : **77** | `AGGREGATE_FACTORY_MEMBERS` | `"전사(경산 제외)": ("남양주1","남양주2","김해","광주","논산")` |
| 2 | `server.py` : **242** | `FACTORY_MEMBERS` | `"전사(경산 제외)": ["남양주1","남양주2","김해","광주","논산"]` |
| 3 | `server.py` : **254** | `PRODUCTION_FACTORY_CODES` | `"전사(경산 제외)": ("F10","F10A","F10B","F20","F30","F40")` |

> `FACTORY_MEMBERS["전사"] = []`(빈 리스트)는 **"WHERE 절 없음 = 전체 행"** 을 뜻합니다.
> 새 라벨은 멤버가 비어 있지 않으므로 `factory_clause()` 가 정상적으로
> `AND factory IN (…5개…)` 를 만듭니다 — 이 함수들 자체는 수정이 필요 없습니다.

## A-2. `is_company_wide()` 헬퍼 신설 + 분기 교체

진짜 작업은 **필터가 아니라 동작을 가르는** `in ("전사", "전체")` 비교들입니다.
새 라벨은 여기 안 걸려서 "일반 공장"으로 취급되고 전사 전용 기능이 조용히 빠집니다.

```python
# app/domain/factories.py — FACTORY_DISPLAY_ORDER 근처에 추가

COMPANY_WIDE_LABELS: frozenset[str] = frozenset(
    {"전사", "전체", "ALL", "전사(경산 제외)"}
)

def is_company_wide(factory: str | None) -> bool:
    """전사 성격의 집계 라벨인지 — 경산 포함/미포함을 모두 포함한다.

    주의: '어떤 공장을 조회하는가'(필터)가 아니라 '전사 전용 기능을 켤 것인가'(동작)를
    판단하는 용도다. 필터는 FACTORY_MEMBERS / AGGREGATE_FACTORY_MEMBERS 가 담당한다.
    """
    return factory in COMPANY_WIDE_LABELS
```

| # | 파일 : 행 | 대상 | 판단 |
|---|---|---|---|
| 4 | `server.py` : **1322** | 공장별 비교 라인 제공 조건 | ✅ 교체 + ⚠ 아래 참고 |
| 5 | `server.py` : **1437** | `target_factory = "ALL" if …` | ✅ 교체 (절감목표는 ALL 공유) |
| 6 | `server.py` : **3033** | 이벤트 주석 필터 | ⚠ 결정 필요 — 6절 참고 |
| 7 | `server.py` : **606** | `physical_factory_members` | ⛔ **수정 금지** — 아래 참고 |
| 8 | `services/production_actual_service.py` : **200** | `if factory in ("전사","전체")` | ✅ 교체 |
| 9 | `services/production_correction_service.py` : **674** | `_energy_factory_codes` | ✅ 교체 |
| 10 | 〃 : **700** | `_prod_factory_codes` | ✅ 교체 |
| 11 | `services/query_service.py` : 67·103·106·123·182 | 전사 합산 경로 | ⚠ legacy 여부 확인 후 — 6절 |

### ⚠ #4 의 함정 — 조건만 고치면 안 된다

[`server.py:1322`](../backend/server.py#L1322) 의 `per_factory_rows` 쿼리는 현재
**공장 필터가 아예 없습니다**:

```python
FROM energy_daily WHERE date BETWEEN %s AND %s
GROUP BY date, factory ORDER BY date
```

조건문만 `is_company_wide()` 로 바꾸면 `전사(경산 제외)` 로 조회해도 비교 라인에는
경산이 그대로 그려집니다. **쿼리에 `factory_clause(factory)` 를 붙여야** 합니다.

### ⛔ #7 을 건드리면 안 되는 이유

```python
def physical_factory_members(factory: str) -> tuple[str, ...]:
    if factory in ("전사", "전체"):          # ← 여기에 is_company_wide 를 넣으면 안 됨
        return tuple(PHYSICAL_FACTORIES)     #    6개 전체(경산 포함)를 반환해 버린다
    members = FACTORY_MEMBERS.get(factory)   # ← 새 라벨은 이 경로로 5개를 정확히 반환
    return tuple(members) if members else (factory,)
```

이 표에서 **유일하게 "그대로 두는"** 항목입니다.

## A-3. 프론트엔드

| # | 파일 : 행 | 작업 |
|---|---|---|
| 12 | `lib/bems-data.ts` : **1** | `factories` 배열에 `"전사(경산 제외)"` 추가 (`"전사"` 바로 뒤) |
| 13 | `components/screens/admin-screen.tsx` : **22** | `eventFactories` 필터에서 새 라벨도 제외 |
| 14 | `components/bems-app.tsx` : **347** | 공장별 비교 관련 분기가 새 라벨을 포함하는지 확인 |

`lib/bems-data.ts:9` `factoryColors` 에는 **추가하지 않아도 됩니다** — 집계 라벨은
공장별 시리즈 색을 쓰지 않습니다.

## A-4. 검증

1. **차감 항등식** — 같은 기간으로 세 번 조회해
   **`전사 − 전사(경산 제외) = 경산`** 이 모든 지표(전력·연료·용수·폐수·생산량·비용)에서
   성립하는지. 어긋나는 지표가 있으면 A-2 표에서 빠뜨린 분기가 있다는 뜻입니다.
2. **회귀** — 기존 `전사` 수치가 작업 전과 **완전히 동일**한지 (기본값을 안 바꿨으므로 필수).
3. **전년비 재현** — 1절의 전력비 사례: `전사` 는 +13.9% 부근,
   `전사(경산 제외)` 는 감소로 나오면 정상.
4. **공장별 비교** — `전사(경산 제외)` 선택 시 비교 차트에 **경산 라인이 없어야** 함
   (A-2 #4 함정 검증).

---

# 작업 B — 경산 월별 실적(2025-01~2026-03) 적재

## B-1. 왜 기존 일별 테이블에 못 넣나

`energy_daily` 는 `UNIQUE(factory, date)`, `production_daily` 는
`UNIQUE(date, factory, item_code)` 의 **일단위** 테이블이고, 모든 기간 집계가
`SUM(...) WHERE date BETWEEN` 입니다. 월 총량을 여기 넣으면:

- **월 1일 대표행** → 일별 차트에 그 하루만 월 총량만큼 치솟는 봉우리
- **일수 균등 분배** → 모든 쿼리가 그대로 동작하지만, 일별 차트·7일 추이·이상탐지에
  **존재하지 않는 평탄한 가짜 데이터**가 실측처럼 섞임

두 번째가 작업량은 가장 적지만, 이 레포는 이미 *"'데이터 없음'과 '실제 0원'을 구분해야
한다"* 는 원칙을 지키고 있습니다([`_round_or_none`](../backend/server.py#L350) 주석).
가짜 일별값은 그 원칙과 정면으로 어긋납니다. → **별도 월별 테이블 2개**로 갑니다.

## B-2. 스키마

```sql
-- 경산 등 일별 실적이 없는 구간의 월 단위 에너지 실적.
-- 단가(원/kWh)는 저장하지 않는다 — 비용÷사용량으로 언제나 계산 가능하고,
-- 저장하면 두 값이 어긋날 여지가 생긴다(energy_daily 원단위 컬럼의 전철).
-- 생산량도 저장하지 않는다 — 분모는 production_monthly 가 단일 출처.
CREATE TABLE IF NOT EXISTS energy_monthly (
    id              INT AUTO_INCREMENT PRIMARY KEY,
    factory         VARCHAR(50) NOT NULL,
    month_key       CHAR(7)     NOT NULL,        -- 'YYYY-MM' (YEAR_MONTH 예약어 회피)

    total_power_kwh DOUBLE NOT NULL DEFAULT 0,
    fuel_nm3        DOUBLE NOT NULL DEFAULT 0,
    water_ton       DOUBLE NOT NULL DEFAULT 0,
    wastewater_ton  DOUBLE NOT NULL DEFAULT 0,
    power_cost_krw  DOUBLE NOT NULL DEFAULT 0,
    fuel_cost_krw   DOUBLE NOT NULL DEFAULT 0,

    source          VARCHAR(20) NOT NULL DEFAULT 'manual',
    created_at      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,

    UNIQUE KEY uq_energy_monthly (factory, month_key)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 품목 축은 원본에 없으므로 버리고 제품유형(category2)까지만 받는다.
-- 경산은 IC 단일 유형이라 총량을 그대로 category2='IC' 로 적재한다(안분·추정 없음).
CREATE TABLE IF NOT EXISTS production_monthly (
    id          INT AUTO_INCREMENT PRIMARY KEY,
    factory     VARCHAR(20) NOT NULL,            -- 한글 라벨('경산') — energy_monthly 와 통일
    month_key   CHAR(7)     NOT NULL,
    category2   VARCHAR(50) NOT NULL,            -- 경산은 항상 'IC'
    planned_qty DOUBLE NOT NULL DEFAULT 0,       -- 월 계획(없으면 0)
    actual_qty  DOUBLE NOT NULL DEFAULT 0,       -- 월 실적 (kg — production_daily 와 같은 단위)

    source      VARCHAR(20) NOT NULL DEFAULT 'manual',
    created_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,

    UNIQUE KEY uq_production_monthly (factory, month_key, category2)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
```

**결정 3가지와 그 이유:**

| 결정 | 이유 |
|---|---|
| `category2` 를 `NOT NULL` | MySQL 은 UNIQUE 인덱스에서 `NULL` 을 서로 다른 값으로 취급해 **중복 행이 조용히 들어옵니다.** (`production_daily.category2` 는 `DEFAULT NULL` 이지만 그쪽 UNIQUE 는 `item_code` 기반이라 무사) |
| 단가 컬럼 없음 | 비용÷사용량으로 계산 — 저장하면 어긋날 여지 |
| `actual_qty` 단위는 **kg** | `production_daily.actual_qty` 와 동일 단위. ton 변환은 소비 측(`_monthly_production_ton`)이 이미 `/1000` 을 함 |

`factory` 컬럼은 **한글 라벨(`'경산'`)** 을 씁니다 — `energy_daily` 와 같은 표기이고,
`production_daily` 의 F-code(`F50`)와는 다릅니다. 폴백 서비스가 이 차이를 흡수합니다.

## B-3. ⚠ 테이블 생성 — schema.sql 만으로는 안 생긴다

**이 프로젝트에서 가장 빠지기 쉬운 함정입니다.**

- `schema.sql` 의 `CREATE TABLE` 을 실행하는 건 `init_db()`
  ([`db_connection.py:184`](../backend/app/database/db_connection.py#L184)) 인데,
  **이 함수를 호출하는 코드가 저장소에 없습니다.** (legacy Streamlit 기동 코드의 잔재.
  현재 배포는 `uvicorn backend.server:app` 뿐)
- `backend/tools/apply_migrations.py` 는 **`ALTER TABLE ADD/DROP COLUMN` 만** 처리합니다.
  신규 테이블은 만들지 못합니다.

→ **`schema.sql` 에 DDL 을 추가하는 것만으로는 운영 DB에 테이블이 생기지 않습니다.**
   적재 스크립트는 `Table 'energy_monthly' doesn't exist` 로 실패합니다.

**권장 대응 — `apply_migrations.py` 를 테이블 생성까지 확장:**

```python
# app/database/db_connection.py — _PENDING_COLUMN_MIGRATIONS 근처

# 신규 테이블(멱등). schema.sql 에도 같은 DDL 을 넣어 신규 설치를 커버하고,
# 기존 DB 에는 이 목록을 통해 apply_migrations.py 가 생성한다.
_PENDING_TABLE_CREATES: list[tuple[str, str]] = [
    ("energy_monthly", "CREATE TABLE IF NOT EXISTS energy_monthly (...)"),
    ("production_monthly", "CREATE TABLE IF NOT EXISTS production_monthly (...)"),
]
```

`apply_migrations.py` 의 `report_pending()` / `main()` 에 이 목록 처리를 추가하세요
(존재 확인은 `INFORMATION_SCHEMA.TABLES`). **`schema.sql` 에도 같은 DDL 을 반드시
함께 넣어야** 신규 설치가 커버됩니다 — 두 곳이 단일 출처가 아니라는 점을 감수하는
대신, 기존 `_PENDING_COLUMN_MIGRATIONS` 와 같은 관례를 따르는 선택입니다.

실행:

```bash
.venv/Scripts/python.exe backend/tools/apply_migrations.py --dry-run   # 확인
.venv/Scripts/python.exe backend/tools/apply_migrations.py             # 적용
```

## B-4. 적재 스크립트

과거 고정 구간(2025-01~2026-03)이면 **1회성 스크립트**로 충분합니다.
`backend/tools/load_gyeongsan_monthly.py` 신설을 권장합니다.

- 입력 원본(엑셀/수기)에서 아래 정규화 형태로 읽어 UPSERT
- `ON DUPLICATE KEY UPDATE` 로 멱등하게 — 몇 번 돌려도 안전
- `source` 에 출처를 남길 것(`'manual-2026-07'` 등) — 나중에 추적 가능

| 대상 | 필요한 컬럼 |
|---|---|
| `energy_monthly` | `month_key`, 전력량, 연료량, 용수량, 폐수량, 전력비, 연료비 |
| `production_monthly` | `month_key`, 생산 실적(kg), (있으면) 계획 |

> ⚠ **단위 확인 필수** — 원본이 ton 이면 `actual_qty` 에 넣기 전 `×1000`,
> 연료가 천 Nm³ 이면 `×1000`. 적재 후 B-7 검증 1번(자릿수 대조)으로 반드시 확인하세요.
> 경계월인 **2026-04 는 적재하지 않습니다** — 일별 데이터가 이미 있습니다(B-5 규칙 1).
> 적재 범위는 **2025-01 ~ 2026-03** 입니다.

## B-5. 폴백 규칙 — 3가지

### 규칙 1 · 한 달 안에서 두 소스를 섞지 않는다

> `(공장, 월)` 단위로 판단한다. 해당 월에 일별 행이 **하나라도** 있으면 일별 SUM 을 쓰고,
> 하나도 없을 때만 월별 테이블 값을 쓴다.

경계월(경산 2026-04)에서 두 소스가 겹쳐 **이중 계상되는 것을 막는** 규칙입니다.

### 규칙 2 · ⚠ 판단은 반드시 **물리 공장 단위**로

**가장 큰 버그 위험 지점입니다.** `전사` 로 조회하면 2025-05 에도 다른 5개 공장의
일별 행이 있으므로 "일별 데이터가 있는 달"로 판정되고, **경산 월별이 영원히 안 붙습니다.**

> 집계 라벨은 `physical_factory_members()` 로 물리 공장까지 펼친 뒤,
> **공장별로 규칙 1을 각각 적용**하고 그 결과를 합산한다.

```
전사 2025-05 =  남양주1(일별) + 남양주2(일별) + 김해(일별)
              + 광주(일별)  + 논산(일별)  + 경산(월별 폴백)
```

### 규칙 3 · 월 경계로 정렬된 집계에만 적용

> `GROUP BY YEAR(date), MONTH(date)` 로 월을 만드는 집계(전년비·월별 추이·연간)에만
> 폴백을 태운다. **임의 기간 `WHERE date BETWEEN` SUM 에는 태우지 않는다.**

월 총량은 일 단위로 쪼갤 수 없어, 부분월을 포함한 임의 기간(에너지 `recent`/`range`,
생산실적 `range` 모드)에서는 정확히 잘라낼 방법이 없습니다. 일수 안분은 추정치를
실측처럼 보이게 만들어 B-1 의 원칙에 어긋납니다. 해당 모드에서는 **경산 과거 구간을
비워 두고**, 이미 비용 화면이 쓰는 `coverage` · `comparable_note` 패턴
([`server.py:1759`](../backend/server.py#L1759))으로 안내하세요.

### 단일 출처로 둘 것

세 규칙의 판단을 **`app/services/monthly_fallback_service.py`(신설) 한 곳**에 두고
아래 경로가 전부 그것을 경유하게 하세요 — 규칙이 복제되면 반드시 어긋납니다.

```python
def monthly_energy(factory: str, month_from: date, month_to: date) -> dict[tuple[int, int], dict[str, float]]:
    """(연, 월) → {total_power_kwh, fuel_nm3, water_ton, wastewater_ton,
    power_cost_krw, fuel_cost_krw}. 규칙 1·2 적용 후 물리 공장 합산."""

def monthly_production_kg(factory: str, month_from: date, month_to: date) -> dict[tuple[int, int], float]:
    """(연, 월) → 생산 kg. 규칙 1·2 적용."""

def fallback_months(factory: str, month_from: date, month_to: date) -> set[tuple[int, int]]:
    """월별 폴백이 적용된 (연, 월) 집합 — 화면 안내 문구용."""
```

## B-6. 폴백 적용 지점

**에너지·비용** — 월 `GROUP BY` 쿼리:

| 화면 | 위치 |
|---|---|
| 에너지 전년대비 | [`server.py:1316`](../backend/server.py#L1316) `energy()` 의 `GROUP BY y, m` 쿼리 |
| 대시보드 월별 전년비 | [`server.py:1002`](../backend/server.py#L1002) `dashboard()` |
| 에너지 비용 월별 | [`server.py:1669`](../backend/server.py#L1669) `energy_cost()` 의 `monthly` 블록 |

**생산량** — [`server.py:1544`](../backend/server.py#L1544) `_monthly_production_ton()`
**한 곳이 단일 관문입니다.** 오늘 비용 작업에서 신설된 함수로 `(연,월) → ton` 을 만드는
유일한 경로라, 여기만 고치면 아래가 모두 따라옵니다.

| 화면 | 위치 |
|---|---|
| 원단위 월별 추이 | [`server.py:1371`](../backend/server.py#L1371) `intensity_analysis()` — 분모가 생산량 |
| 생산실적 월별 전년비·Burn-up | [`server.py:1909`](../backend/server.py#L1909) `production()` |

> ⛔ `actual_production_kg()`([`server.py:761`](../backend/server.py#L761))는 **임의 기간
> 합계**라 규칙 3에 따라 폴백 대상이 **아닙니다.** 이 함수를 쓰는 KPI(연 누계 ton 등)는
> 경산 과거분이 빠진 값이 나옵니다 — 6절 참고.

## B-7. 가능한 것 / 불가능한 것

경산이 IC 단일 유형이라 제약이 **품목 축과 일 축 두 가지로** 줄어듭니다.

| 화면 | 경산 2025-01~2026-03 | 근거 |
|---|---|---|
| 원단위 월별 추이·전년비 | ✅ **가능** | 에너지·생산량 월별 모두 있음 |
| 에너지 사용량 월별 전년비 | ✅ **가능** | 〃 |
| 에너지 **비용** 월별 전년비 | ✅ **가능** | 전력비·연료비 보유 확인됨 |
| 제품유형별 **월별** 생산량 | ✅ **가능** | 총량 = IC 실적 |
| 생산실적 월별 전년비·Burn-up | ✅ **가능** | 총량이면 충분 |
| 제품유형별 **일일** 생산량 | ❌ 불가 | 일 축 없음 |
| 품목 순위(주요 품목 / 미달·초과 Top) | ❌ 불가 | 품목별 실적이 원본에 없음 |
| 7일 방향 비교·이상탐지·AI 예측 | ❌ 불가 | 일 축 없음 (경산은 이미 `PREDICTION_FACTORIES` 제외) |

> `production()` 의 `year` 모드는 `daily_output` 을 `{date: "N월", IC:…, MY:…}` 형태로
> 만듭니다([`server.py:2057`](../backend/server.py#L2057) `monthly_map` 부근). 폴백 구간은
> **`IC` 에 총량을 넣고 나머지 유형을 0 으로** 두면 형태가 그대로 맞습니다.
> 프론트의 `cat2ActiveKeys` 가 값 0 인 유형을 이미 걸러 내므로
> ([`bems-app.tsx:607`](../components/bems-app.tsx#L607)) 범례·차트·표에 IC 만 나옵니다 —
> **프론트엔드 수정 없이 동작합니다.**

## B-8. 검증

1. **적재 자릿수** — 적재 직후 원본과 대조. 단위 실수(kg↔ton, Nm³↔천Nm³)를 여기서 잡습니다.
   ```sql
   SELECT month_key, total_power_kwh, power_cost_krw, fuel_nm3, fuel_cost_krw
   FROM energy_monthly WHERE factory='경산' ORDER BY month_key;

   SELECT month_key, category2, actual_qty FROM production_monthly
   WHERE factory='경산' ORDER BY month_key;
   ```
   **자체 검산**: `power_cost_krw ÷ total_power_kwh` 가 타 공장 전력단가(160~195원)
   범위에 들어오면 단위가 맞습니다. 10배·1000배로 튀면 단위 실수입니다.

2. **⚠ 이중 계상 (경계월)** — 가장 중요한 검증. 경산 2026-04 는 일별이 있으므로
   **월별 테이블에 행이 없어야** 하고, 화면 값이 일별 SUM 과 정확히 일치해야 합니다.
   ```sql
   SELECT COUNT(*) FROM energy_monthly WHERE factory='경산' AND month_key >= '2026-04';
   -- 반드시 0
   ```

3. **⚠ 규칙 2 (물리 공장 단위)** — `전사` 로 2025년 월별 전년비를 조회했을 때
   **경산 월별이 실제로 합산되는지.** `전사 − 전사(경산 제외)` 가 2025년 각 월에서
   경산 월별 값과 일치하면 정상입니다. 0 이 나오면 규칙 2를 물리 공장 단위가 아니라
   집계 라벨 단위로 판정한 것입니다 — B-5 규칙 2의 함정에 빠진 상태입니다.

4. **경산 단독** — 일별 차트는 2026-04 이전이 비어 있고, 월별 차트는 2025-01부터 나오는지.

5. **회귀** — 타 5개 공장 수치가 작업 전과 완전히 동일한지 (폴백이 엉뚱한 공장에
   적용되지 않았는지).

6. **원단위 타당성** — 경산 2025년 월별 원단위가 2026년 값과 비슷한 자릿수인지.
   크게 어긋나면 에너지·생산량 중 한쪽 단위가 틀렸다는 신호입니다.

---

## 5. 커밋 분리 권장

| 커밋 | 범위 | 확인 |
|---|---|---|
| 1 | 작업 A 전체 (A-1·A-2·A-3) | A-4 검증 1~4 |
| 2 | B-2 스키마 + B-3 마이그레이션 도구 확장 | `SHOW TABLES LIKE '%_monthly'` |
| 3 | B-4 적재 스크립트 + 실행 | B-8 검증 1·2 |
| 4 | B-5 폴백 서비스 + B-6 적용 | B-8 검증 3~6 |

작업 A는 단독으로 유용하므로 **1번을 먼저 배포**하고 B를 이어가는 순서를 권장합니다.

## 6. 구현 중 결정할 것 (판단 위임)

1. **A-2 #6 이벤트 주석** — `전사(경산 제외)` 조회 시 경산 이벤트를 숨길지.
   숨기는 쪽이 일관되지만, 이벤트는 참고 정보라 그대로 두는 것도 무방합니다.
2. **A-2 #11 `query_service.py`** — Streamlit legacy 잔재인지 현재도 쓰이는지 확인 후
   판단. `server.py` 가 `import_core` 로 부르지 않으면 교체 대상에서 빼세요.
3. **B-6 ⛔ 임의 기간 KPI** — 연 누계 ton 등에서 경산 과거분이 빠지는 것을 그대로 둘지,
   `comparable_note` 로 안내를 붙일지.
4. **라벨 문구** — `전사(경산 제외)` 가 최종안인지. 신설 공장이 또 생기면
   `전사(신설 제외)` 같은 일반화가 더 오래 갈 수 있습니다.
5. **적재 원본 위치** — 수기 엑셀이면 B-4 대로 1회성 스크립트로 충분하고,
   MIS 월별 화면에서 계속 갱신되면 `daily_energy_sync_service` 처럼 상시 동기화가
   필요합니다. 2026-03 에서 끝나는 고정 과거분이면 1회성으로 끝냅니다.
