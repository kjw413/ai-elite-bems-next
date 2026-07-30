# 에너지 단가·비용·COD 적재 작업계획

> 작성 2026-07-30 · 대상 레포 `AI-Elite-BEMS-next` · 선행 작업(`AI-Elite-MIS_RPA`) 완료됨
>
> 이 문서는 워크트리에서 단독으로 진행할 수 있도록 배경·입력 스펙·수정 지점·검증 절차를
> 모두 담았습니다. MIS RPA 레포를 열어 볼 필요는 없습니다.

## 1. 목적

MIS 에너지 수집 화면이 `유틸리티 일자별 사용량 추이` → `원단위 실적입력(일단위)` 로
바뀌면서 **전력비·전력단가·연료비·연료단가·원수COD·배출수COD** 6개 항목이 새로 수집되기
시작했습니다. 이 값들은 현재 `DB_에너지.xlsx` 까지는 들어오지만 `energy_daily` 테이블에
컬럼이 없어 **DB·화면·메일에는 도달하지 못합니다.** 이 작업의 목표는 그 경로를 잇는 것입니다.

## 2. 선행 작업 (RPA 쪽, 완료)

| 항목 | 변경 내용 |
|---|---|
| 수집 화면 | `원단위 실적입력(일단위)` 단일 화면. 구 화면 수집 코드·좌표 제거 |
| **파일 단일화** | `RawDB_에너지.xlsx` **하나만** 남음 (행=일자, 열=항목). 중간 산출물 `DB_에너지.xlsx` 와 재가공 단계(`build_dataset`) **폐지** |
| **웹앱 입력 경로** | `v5_common.PATH_ENERGY_SOURCE` 가 **`RawDB_에너지.xlsx`** 를 가리켜야 함 ⚠ |
| 신규 항목 | 전력비·전력단가·연료비·연료단가·원수COD·배출수COD 열 추가 |
| 믹스생산량·원단위 | **수집 중단.** 열은 과거 값 보존용으로 유지, 신규 날짜는 공백 |
| 데이터 기간 | 생산실적과 시간축을 맞추기 위해 2021~2023 삭제 → **2024-01 부터** |

### 왜 파일을 단일화했나

행=일자 tidy 형태는 `DB_생산실적.xlsx` 의 `daily` 시트, `DB_재공품.xlsx` 와 통일된
방향이고, **이 레포 파서가 그 형태를 이미 그대로 읽습니다.** A열이 날짜라
`_parse_korean_excel` 의 전치 감지(`is_transposed`)가 `False` 가 되어 전치 분기를 타지
않고, 머리글 한글 부분매칭만으로 컬럼이 잡힙니다. 실제로 돌려 검증했습니다:

```
is_transposed=False
필수 컬럼 누락: 없음  → validate_columns 통과
중복 매핑: 없음
미매핑(무시됨): ['전력비[원]','전력단가[원/kWh]','연료비[원]','연료단가[원/N㎥]',
                '원수COD[ppm]','배출수COD[ppm]']
```

즉 **경로만 바꾸면 기존 10개 항목은 그대로 적재되고, 신규 6개는 조용히 무시**됩니다.
이 작업은 그 6개를 매핑에 추가해 무시되지 않게 만드는 것입니다.

### 믹스생산량·원단위 수집을 중단한 근거

이 레포가 이미 생산실적을 분모로 쓰고 있기 때문입니다 —
[`query_service.py`](../backend/app/services/query_service.py) 의 주석 그대로:

```python
# RawDB_에너지의 mix_prod_kg는 원본 보존용이며 화면·원단위 계산에는 사용하지 않는다.
df = overlay_actual_production(df)
```

소비처 전부가 `overlay_actual_production*()` 로 `mix_prod_kg` 를
`production_daily.actual_qty` 합계로 덮어쓴 뒤 `recalc_unit_rates()` 로 원단위를
재계산합니다 — 화면(`query_service`), 분석(`anomaly_diagnosis_service`),
예측(`usage_prediction_v5_service`), 메일(`daily_report_builder`).
즉 `energy_daily` 에 저장된 원단위 3개 컬럼은 **이미 아무도 읽지 않습니다.**

전환 전 검증: 믹스생산량[kg] 과 생산실적 `actual_qty` 합계는 6개 공장 전부 비율 **1.000**
(남양주1 1.000 / 남양주2 1.003 / 김해 1.011 / 광주·논산·경산 1.000). 일별로는 약 6% 날짜가
1% 이상 어긋나는데(믹스 제조일과 포장일의 시차), 이 레포가 이미 생산실적 기준으로 정의를
확정해 놓았으므로 새로 결정할 사항은 없습니다.

## 3. 입력 파일 스펙 — `RawDB_에너지.xlsx`

- 위치: `SAMPLED_DB_DIR` (`E:\DB_MIS`), 파일명 **`RawDB_에너지.xlsx`**
- 시트: `남양주1` `남양주2` `김해` `광주` `논산` `경산`
- 구조: **행 = 일자, 열 = 항목** (tidy — `DB_생산실적.daily` 와 같은 방향)
- A1 = `날짜`, A2 부터 날짜 셀(`datetime`)이 위→아래로 누적. 기간 **2024-01-01 ~**
- 시트당 17열. 열 순서는 MIS 화면 순서를 따르고 legacy 항목이 뒤에 붙습니다.

| 열 | 머리글 | 상태 |
|---:|---|---|
| A | `날짜` | 키 |
| B | `냉동전력량[kWh]` | 수집 |
| C | `공압기[kWh]` | 수집 |
| D | `전력량[kWh]` | 수집 |
| **E** | **`전력비[원]`** | **신규 — 적재 대상** |
| **F** | **`전력단가[원/kWh]`** | **신규** |
| G | `연료량[N㎥]` | 수집 |
| **H** | **`연료비[원]`** | **신규** |
| **I** | **`연료단가[원/N㎥]`** | **신규** |
| J | `용수량[ton]` | 수집 |
| K | `폐수량[ton]` | 수집 |
| **L** | **`원수COD[ppm]`** | **신규** |
| **M** | **`배출수COD[ppm]`** | **신규** |
| N | `믹스생산량[kg]` | legacy — 과거 값만, 신규 날짜 공백 |
| O | `전력원단위[kWh/mix-ton]` | legacy |
| P | `연료원단위[N㎥/mix-ton]` | legacy |
| Q | `용수원단위[ton/mix-ton]` | legacy |

> 열 순서에 의존하지 마세요 — 파서가 **머리글 문자열 부분매칭**으로 컬럼을 찾습니다.
> 머리글 문자열의 단일 출처는 RPA 레포 `energy_builder.FIELDS` 입니다.

`전력비 = 전력량 × 전력단가`, `연료비 = 연료량 × 연료단가` 관계가 성립합니다(단가는 소수
2자리 표시 반올림). 전력단가는 일별로 변동하고, 연료단가는 월 단위로 고정입니다.

## 4. 작업 — 6개 항목을 `energy_daily` 까지 적재

권장 컬럼명:

| 엑셀 라벨 | 컬럼명 | 타입 |
|---|---|---|
| `전력비[원]` | `power_cost_krw` | DOUBLE |
| `전력단가[원/kWh]` | `power_price_krw_kwh` | DOUBLE |
| `연료비[원]` | `fuel_cost_krw` | DOUBLE |
| `연료단가[원/N㎥]` | `fuel_price_krw_nm3` | DOUBLE |
| `원수COD[ppm]` | `influent_cod_ppm` | DOUBLE |
| `배출수COD[ppm]` | `effluent_cod_ppm` | DOUBLE |

RPA 쪽 `energy_builder.FIELDS` 의 `key` 와 동일한 이름이라 양쪽 대조가 쉽습니다.

### 4-1. 수정 지점 (행 번호는 2026-07-30 기준)

동일한 정보가 여러 곳에 **복제**돼 있습니다. 컬럼 목록 3곳, 한글 라벨 매핑 3곳,
행 필터 키 2곳 — 하나라도 빠지면 값이 조용히 사라지므로 표를 순서대로 소화하세요.

| # | 파일 : 행 | 심볼 | 작업 |
|---|---|---|---|
| 0 | `services/v5_common.py` : 124 | `PATH_ENERGY_SOURCE` | 파일명을 **`RawDB_에너지.xlsx`** 로. ⚠ 이것부터 |
| 1 | `database/schema.sql` : 6~33 | `CREATE TABLE energy_daily` | 6개 컬럼 추가. `DOUBLE NOT NULL DEFAULT 0` (기존 관례) — 신규 설치용 |
| 2 | `database/db_connection.py` : **243** | `_PENDING_COLUMN_MIGRATIONS` | 기존 DB 용 멱등 `ADD COLUMN` 6줄:<br>`("energy_daily", "power_cost_krw", "ADD COLUMN power_cost_krw DOUBLE NOT NULL DEFAULT 0 AFTER water_per_ton_ton")` |
| 3 | `utils/excel_parser.py` : **17** | `EXPECTED_COLUMNS` | 6개 추가. `NUMERIC_COLUMNS`(32행) 는 `EXPECTED_COLUMNS[1:]` 라 자동 반영 |
| 4 | 〃 : **35** | `COLUMN_DISPLAY_NAMES` | 한글 표시명 6개 |
| 5 | 〃 : **123** | `kor_to_eng` (지역 dict) | 라벨→컬럼 매핑 6개 |
| 6 | `services/daily_energy_sync_service.py` : **90** | `_KOR_SUBSTR_MAP` | 5번의 복제본 — 같은 6개 추가 |
| 7 | 〃 : **45** | `_INSERT_COLUMNS` | 6개 추가 (252행 INSERT 문이 이 목록 기반) |
| 8 | `services/upload_service.py` : **32** | `INSERT_COLUMNS` | 7번과 **별개의 세 번째 목록.** 수동 업로드 경로용 — 함께 추가 |
| 9 | `services/v5_common.py` : **128** | `ENERGY_UPLOAD_TO_MODEL_COLUMNS` | 컬럼↔엑셀 라벨 매핑 6개 (라벨 문자열은 3항 표와 정확히 일치시킬 것) |
| 10 | `services/query_service.py` : **23** | `USAGE_COLUMNS` | 화면 노출 범위 결정 후 — 4-3 참고 |
| 11 | `tools/mail/daily_report_builder.py` : 91~107 | 지표 정의 | 메일 노출 범위 결정 후 |

0~9 가 "DB 까지 잇기", 10~11 은 "사용자에게 보이기" 입니다. **별도 커밋/PR 로 나누는 것을 권장**합니다.

**`excel_parser.py:102` 의 `metric_keys` 는 건드릴 필요가 없습니다** — 전치형 분기에서만
쓰이고, tidy 입력은 그 분기를 타지 않습니다. 자세한 내용은 4-2.

### 4-2. 행 필터(`metric_keys`)는 이제 적용되지 않습니다

전치형 입력이던 시절에는 두 파서가 **A열 라벨이 키에 부분매칭되는 행만 남겨서**, 신규
라벨 6개가 조용히 버려지는 함정이 있었습니다. 입력이 tidy(행=일자)로 바뀌면서
`is_transposed=False` 가 되어 **그 분기 자체를 타지 않으므로 함정이 사라졌습니다.**

```python
# 두 파서 공통 — 이 조건이 False 이므로 아래 metric_mask 블록을 건너뜀
first_col_values = [str(v).replace(" ", "") for v in df.iloc[:, 0].dropna().values]
is_transposed = any("냉동전력량" in v or "전력량" in v for v in first_col_values)
```

다만 **잠재적 함정으로 남아 있습니다.** 누군가 입력을 다시 전치형으로 되돌리면(또는
A열에 항목명이 들어오면) 행 필터가 되살아나 신규 6개가 사라집니다. 그때는:

- `daily_energy_sync_service.py:127` — `_KOR_SUBSTR_MAP` 에서 파생되므로 90행만 고치면 됨
- `excel_parser.py:102` — **하드코딩 리스트**라 별도로 직접 추가해야 함

컬럼 매핑(표 5·6)에 키를 추가할 때 이 두 곳의 모양 차이를 기억해 두세요.

**부분매칭 키 선정 주의** — 비교는 `str(v).strip().lower().replace(" ", "")` 후 `k in v` 입니다.

| 넣을 키 | 판정 |
|---|---|
| `전력비`, `전력단가`, `연료비`, `연료단가`, `원수cod`, `배출수cod` | ✅ 안전 — 기존 라벨과 충돌 없음 |
| `단가`, `비용`, `cod` | ❌ 짧아서 여러 라벨을 함께 삼킴 |
| `원수cod` vs `배출수cod` | ⚠ `배출수COD[ppm]` 에는 `원수cod` 가 포함되지 **않음**(`출수cod`) — 다행히 안전하나, 순회 순서대로 첫 매칭이 이기므로 `배출수cod` 를 앞에 두면 더 확실 |

기존 키와의 충돌도 확인했습니다: `전력량` ⊄ `전력비[원]`·`전력단가[원/kWh]`,
`연료량` ⊄ `연료비[원]`·`연료단가[원/N㎥]`, `용수량` ⊄ `원수COD[ppm]` — 모두 안전합니다.

### 4-3. 화면·메일 노출 시 집계 방식

`USAGE_COLUMNS` 는 월 집계에서 `sum` 됩니다([query_service.py:158](../backend/app/services/query_service.py#L158) 부근).

- **비용**(`power_cost_krw`, `fuel_cost_krw`) → `sum` 이 맞습니다.
- **단가**(`power_price_krw_kwh`, `fuel_price_krw_nm3`) → `sum` 은 **무의미합니다.**
  `SUM(비용) / SUM(사용량)` 가중평균으로 재계산해야 합니다. `recalc_unit_rates()` 와 같은
  패턴이므로 `UNIT_CALC_MAP` 방식을 참고해 단가용 재계산을 추가하는 편이 자연스럽습니다.
- **COD**(`influent_cod_ppm`, `effluent_cod_ppm`) → 농도이므로 `sum` 이 아니라 `mean`
  (엄밀히는 유량가중이지만 일별 유량이 없으므로 단순평균).

이 세 종류를 `USAGE_COLUMNS` 에 그냥 넣으면 단가·COD 가 합산돼 터무니없는 값이 나옵니다.

## 5. (선택) 미사용 원단위 컬럼 정리

`power_per_ton_kwh` · `fuel_per_ton_nm3` · `water_per_ton_ton` 은 매 조회마다
`recalc_unit_rates()` 로 재계산되므로 저장값이 쓰이지 않습니다. 두 가지 선택:

- **최소(권장)**: `schema.sql` 22~25행 주석에 "저장값 미사용 — overlay 후 재계산" 을 명시
- **정리**: `db_connection.py:264` `_PENDING_COLUMN_DROPS` 에 추가해 제거.
  `wastewater_per_ton_ton` 선례가 바로 그 자리에 있습니다. 단 `EXPECTED_COLUMNS` ·
  `validate_columns` 가 필수 컬럼으로 검사하므로 파서 3곳을 함께 손봐야 하고,
  `mix_prod_kg` 는 overlay 의 **대입 대상이므로 반드시 남겨야 합니다.**

`RawDB_에너지.xlsx` 의 믹스·원단위 열(N~Q)은 과거 값 보존용이므로 **엑셀 쪽은 건드리지 않습니다.**

## 6. 검증 절차

0. **경로** — `PATH_ENERGY_SOURCE` 가 `RawDB_에너지.xlsx` 를 가리키는지 먼저 확인.
   `DB_에너지.xlsx` 를 계속 가리키면 파일이 없어 sync 가 조용히 건너뜁니다.
1. **마이그레이션** — 서버 기동 시 `_apply_idempotent_migrations()` 로그에
   `migration: energy_daily.power_cost_krw added` 6줄이 찍히는지 확인
2. **파싱** — `_parse_korean_excel(PATH_ENERGY_SOURCE)` 를 단독 호출해 확인:
   - `is_transposed` 가 `False` (A열이 날짜라 전치 분기를 타지 않아야 함)
   - 반환 DataFrame 에 6개 신규 컬럼이 있고 값이 `0` 이 아닌지
   - 행 수가 시트당 900+ 인지 (2024-01-01 부터 누적)
3. **적재** — `2026-07-01` 기준 아래 값이 `energy_daily` 에 들어갔는지 대조
   (`RawDB_에너지.xlsx` 실측값, 2026-07-30 확인):

   | 공장 | 전력량 | 전력비 | 전력단가 | 연료량 | 연료비 | 연료단가 | 원수COD | 배출수COD |
   |---|---:|---:|---:|---:|---:|---:|---:|---:|
   | 남양주1 | 35,136 | 6,823,950.9 | 194.22 | 2,614 | 2,496,056.32 | 954.88 | 520 | 10 |
   | 남양주2 | 110,664 | 21,342,933.62 | 192.86 | 5,442 | 5,196,456.96 | 954.88 | 1,380 | 10 |
   | 김해 | 74,640 | 14,158,346 | 189.69 | 4,485 | 4,497,558 | 1,002.8 | 771 | 14 |
   | 광주 | 35,424 | 5,667,840 | 160 | 3,102 | 2,957,043.54 | 953.27 | 478 | 2.1 |
   | 논산 | 61,141.5 | 11,612,880 | 189.93 | 2,174 | 1,802,246 | 829 | 748 | 4.8 |
   | 경산 | 115,654 | 22,246,200 | 192.35 | 4,987 | 4,777,546 | 958 | (없음) | 17 |

   ```sql
   SELECT factory, total_power_kwh, power_cost_krw, power_price_krw_kwh,
          fuel_nm3, fuel_cost_krw, fuel_price_krw_nm3,
          influent_cod_ppm, effluent_cod_ppm
   FROM energy_daily WHERE date = '2026-07-01' ORDER BY factory;
   ```

   **자체 검산**: `전력비 ÷ 전력량 ≈ 전력단가`, `연료비 ÷ 연료량 ≈ 연료단가` 가 모든
   공장에서 성립하면 열 매핑이 정상입니다(단가는 소수 2자리 표시 반올림이라 ±0.1% 허용).
   비용과 단가가 뒤바뀌면 이 검산에서 즉시 드러납니다.
   경산 `원수COD` 는 원본이 비어 있으므로 `0` 이 정상입니다.
4. **정합성 회귀** — 기존 10개 항목 값과 화면 원단위가 작업 전과 동일한지 확인.
   `mix_prod_kg` 가 신규 날짜에 `0` 이어도 화면 원단위는 정상이어야 합니다
   (overlay 가 생산실적으로 덮어쓰므로).
5. **집계** — 월 단위 화면에서 단가가 합산되지 않고 가중평균으로 나오는지 확인 (4-3 항)

## 7. 워크트리에서 진행하기

`main` 을 건드리지 않고 이 레포 안에서 독립 작업 트리를 만듭니다.

```bat
cd /d E:\AI-Elite-BEMS-next
git worktree add ..\BEMS-energy-cost -b feature/energy-cost-columns
cd /d E:\BEMS-energy-cost
```

- **`.env` 는 worktree 에 복사되지 않습니다**(git 추적 대상이 아님). `E:\AI-Elite-BEMS-next\.env`
  를 복사해 넣으세요. `SAMPLED_DB_DIR=E:\DB_MIS` 와 `PATH_ENERGY_SOURCE` 가 가리키는
  `DB_에너지.xlsx` 는 원본 레포와 **같은 파일을 공유**합니다(읽기 전용이므로 안전).
- **DB 는 공유됩니다.** 마이그레이션(`ADD COLUMN`)은 워크트리에서 실행해도 운영 DB 에
  그대로 적용됩니다. 되돌리기 어려운 작업이므로 실행 전 확인하세요:
  ```sql
  -- 되돌리기
  ALTER TABLE energy_daily DROP COLUMN power_cost_krw, DROP COLUMN power_price_krw_kwh, ...;
  ```
  `ADD COLUMN ... DEFAULT 0` 은 기존 행에 영향을 주지 않으므로 데이터 손실 위험은 없습니다.
- 작업 완료 후 정리:
  ```bat
  cd /d E:\AI-Elite-BEMS-next
  git worktree remove ..\BEMS-energy-cost
  ```

### 커밋 분리 권장

| 커밋 | 범위 | 확인 |
|---|---|---|
| 1 | 스키마 + 마이그레이션 (표 1~2) | `DESCRIBE energy_daily` 에 6개 컬럼 |
| 2 | 파서 + 적재 (표 0·3~9) | 검증 절차 0~4 |
| 3 | 화면·메일 노출 (표 10~11) | 검증 절차 5 |

## 8. 별건

1. **`energy_daily` 의 2021~2023 행.** 에너지 데이터는 생산실적과 시간축을 맞추려
   2024-01 부터로 잘렸지만, MySQL `energy_daily` 에는 과거 sync 로 들어간 2021~2023 행이
   남아 있습니다. 엑셀에서 사라진 날짜를 sync 가 지우지는 않습니다(UPSERT 전용).
   화면·분석에서 그 기간 원단위가 NaN 으로 보이는 것을 원치 않으면 정리하세요:
   ```sql
   SELECT MIN(date), MAX(date), COUNT(*) FROM energy_daily WHERE date < '2024-01-01';
   -- DELETE FROM energy_daily WHERE date < '2024-01-01';   -- 확인 후 실행
   ```
2. **`production_daily` 의 2024년 이전 커버리지.** `DB_생산실적.xlsx` 의 `daily` 시트는
   2024-01-01 부터입니다. 그 이전 날짜가 DB 에 없으면 overlay 가 `mix_prod_kg=0` 을 넣어
   해당 기간 원단위가 NaN 이 됩니다 — 에너지 데이터를 2024-01 부터로 맞춘 이유입니다.
3. **RPA 레포 담당(완료 대기)**: `DB_에너지.xlsx` 의 믹스생산량·원단위를
   `RawDB_에너지.xlsx` 로 옮긴 뒤 파일을 backup 으로 치우는 1회 스크립트
   (`migrate_energy_drop_db_file.py`). 이 작업이 끝나기 전에 `PATH_ENERGY_SOURCE` 를
   바꾸면 믹스생산량 과거값이 일시적으로 비어 보입니다(원단위는 overlay 라 영향 없음).
