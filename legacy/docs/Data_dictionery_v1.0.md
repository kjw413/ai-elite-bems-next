# 에너지 데이터 사전 (코드 기준)

이 문서는 `app/utils/excel_parser.py`, `app/services/validation_service.py`, `app/database/schema.sql` 기준으로 정리한 데이터 사전입니다.

---

## 1. 적용 범위

- 저장 기준 테이블: `energy_daily`
- 고유키: `(factory, date)`
- 수치 컬럼 타입: `DOUBLE`
- 업로드 시 빈 값: `0`으로 치환

실제 저장 공장 코드:

- `남양주1`
- `남양주2`
- `김해`
- `광주`
- `논산`

업로드 시 허용되는 시트명 별칭:

- `남양주` -> `남양주1`
- `F11` -> `남양주2`

---

## 2. 공통 컬럼

| 컬럼명 | 표시명 | 설명 | 타입 | NULL |
| --- | --- | --- | --- | --- |
| `date` | 날짜 | 기준 일자 | `DATE` | X |
| `factory` | 공장 | 저장 공장 코드 | `VARCHAR(50)` | X |
| `created_at` | 생성일시 | 최초 생성 시각 | `DATETIME` | X |
| `updated_at` | 수정일시 | 마지막 수정 시각 | `DATETIME` | X |
| `changed_by` | 변경자 | 마지막 변경 사용자 | `TEXT` | O |

참고:

- `change_type`는 `energy_daily`가 아니라 `energy_daily_audit`에 저장됩니다.

---

## 3. 측정값 컬럼

| 컬럼명 | 표시명 | 단위 | 타입 | 기본값 |
| --- | --- | --- | --- | --- |
| `freezing_power_kwh` | 냉동전력량 | kWh | `DOUBLE` | `0` |
| `air_compressor_kwh` | 공압기 | kWh | `DOUBLE` | `0` |
| `total_power_kwh` | 전력량 | kWh | `DOUBLE` | `0` |
| `fuel_nm3` | 연료량 | Nm³ | `DOUBLE` | `0` |
| `water_ton` | 용수량 | ton | `DOUBLE` | `0` |
| `wastewater_ton` | 폐수량 | ton | `DOUBLE` | `0` |
| `mix_prod_kg` | 믹스생산량 | kg | `DOUBLE` | `0` |

---

## 4. 원단위 컬럼

일 단위 업로드 데이터에는 아래 원단위 컬럼이 함께 저장됩니다.

| 컬럼명 | 표시명 | 단위 | 타입 | 기본값 |
| --- | --- | --- | --- | --- |
| `power_per_ton_kwh` | 전력 원단위 | kWh/ton | `DOUBLE` | `0` |
| `fuel_per_ton_nm3` | 연료 원단위 | Nm³/ton | `DOUBLE` | `0` |
| `water_per_ton_ton` | 용수 원단위 | ton/ton | `DOUBLE` | `0` |

> 폐수 원단위(`wastewater_per_ton_ton`)는 폐기되었습니다. 대신 화면·메일에서 **폐수/용수** 비(= `wastewater_ton` / `water_ton`, 소수점 2자리)를 raw 값으로 즉석 계산해 표시합니다(별도 컬럼 없음).

정책:

- 일 단위 원단위는 업로드 값을 저장합니다.
- 월별/전년비 집계에서는 원단위를 다시 계산합니다.

---

## 5. 파생 계산 규칙

믹스생산량 ton 환산:

```text
mix_prod_ton = mix_prod_kg / 1000
```

월 원단위 재계산:

```text
월 원단위 = Σ(사용량) / Σ(mix_prod_kg / 1000)
```

즉, 월 원단위는 일 원단위의 합계나 평균이 아닙니다.

---

## 6. 업로드 검증 규칙

| 항목 | 현재 코드 기준 처리 |
| --- | --- |
| 파일 형식 | `.xlsx`, `.xls`만 허용 |
| 시트명 | `남양주1`, `남양주2`, `김해`, `광주`, `논산` 및 하위호환 `남양주`, `F11` 허용 |
| 필수 컬럼 | 누락 시 업로드 실패 |
| 빈 값 | `0`으로 치환 |
| 숫자가 아닌 값 | 업로드 실패 |
| 음수 | 허용 |
| 동일 `(factory, date)` | 기존 행을 업데이트 |

---

## 7. 감사 이력 관련 컬럼

감사 이력은 별도 테이블 `energy_daily_audit`에 저장되며, 주요 컬럼은 다음과 같습니다.

| 컬럼명 | 설명 |
| --- | --- |
| `factory` | 공장 코드 |
| `date` | 기준 일자 |
| `column_name` | 변경 컬럼 |
| `old_value` | 이전 값 |
| `new_value` | 새 값 |
| `change_type` | `UPLOAD`, `MANUAL`, `WEB_ROW_EDIT` |
| `changed_at` | 변경 시각 |
| `changed_by` | 변경 사용자 |

---

## 8. 조회 시 사용하는 파생 공장 코드

DB 원본에는 없지만 UI/집계에서 사용하는 값:

- `남양주` = `남양주1 + 남양주2`
- `전사` = 전체 공장 합산
