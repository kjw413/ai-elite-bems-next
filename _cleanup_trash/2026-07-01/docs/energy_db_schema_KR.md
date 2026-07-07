# 에너지 DB 스키마 (코드 기준)

이 문서는 `app/database/schema.sql` 기준으로 작성된 MySQL 스키마 요약입니다.

---

## 1. 개요

- 데이터베이스: `MySQL`
- 문자셋: `utf8mb4`
- 수치 타입: `DOUBLE`
- 핵심 기준 테이블: `energy_daily`

실제 저장 공장 코드는 다음과 같습니다.

- `남양주1`
- `남양주2`
- `김해`
- `광주`
- `논산`

`남양주`과 `전사`는 조회 시 계산되는 파생 그룹이며 DB 원본 코드가 아닙니다.

---

## 2. 테이블: energy_daily

공장별 일일 에너지 및 생산 데이터 저장 테이블입니다.

| 컬럼 | 타입 | NULL | 설명 |
| --- | --- | --- | --- |
| `id` | `INT AUTO_INCREMENT PK` | X | 자동 증가 ID |
| `factory` | `VARCHAR(50)` | X | 공장 코드 |
| `date` | `DATE` | X | 기준 일자 |
| `freezing_power_kwh` | `DOUBLE` | X | 냉동전력량 |
| `air_compressor_kwh` | `DOUBLE` | X | 공압기 전력 |
| `total_power_kwh` | `DOUBLE` | X | 총 전력량 |
| `fuel_nm3` | `DOUBLE` | X | 연료량 |
| `water_ton` | `DOUBLE` | X | 용수량 |
| `wastewater_ton` | `DOUBLE` | X | 폐수량 |
| `mix_prod_kg` | `DOUBLE` | X | 믹스생산량 |
| `power_per_ton_kwh` | `DOUBLE` | X | 전력 원단위 |
| `fuel_per_ton_nm3` | `DOUBLE` | X | 연료 원단위 |
| `water_per_ton_ton` | `DOUBLE` | X | 용수 원단위 |
| `created_at` | `DATETIME` | X | 생성 시각 |
| `updated_at` | `DATETIME` | X | 수정 시각 |
| `changed_by` | `TEXT` | O | 마지막 변경자 |

제약 조건:

- 고유키: `(factory, date)`

---

## 3. 테이블: energy_daily_audit

숫자 컬럼 변경 이력을 저장하는 감사 테이블입니다.

| 컬럼 | 타입 | NULL | 설명 |
| --- | --- | --- | --- |
| `id` | `INT AUTO_INCREMENT PK` | X | 자동 증가 ID |
| `factory` | `VARCHAR(50)` | X | 공장 코드 |
| `date` | `DATE` | X | 기준 일자 |
| `column_name` | `VARCHAR(100)` | X | 변경된 컬럼명 |
| `old_value` | `TEXT` | O | 이전 값 |
| `new_value` | `TEXT` | O | 새 값 |
| `change_type` | `VARCHAR(50)` | X | 변경 유형 |
| `changed_at` | `DATETIME` | X | 변경 시각 |
| `changed_by` | `TEXT` | O | 변경자 |

현재 코드에서 사용되는 대표 `change_type` 값:

- `UPLOAD`
- `MANUAL`
- `WEB_ROW_EDIT`

---

## 4. 테이블: upload_batch

엑셀 업로드 배치 결과를 기록하는 테이블입니다.

| 컬럼 | 타입 | NULL | 설명 |
| --- | --- | --- | --- |
| `id` | `INT AUTO_INCREMENT PK` | X | 자동 증가 ID |
| `filename` | `VARCHAR(255)` | X | 파일명 |
| `uploaded_at` | `DATETIME` | X | 업로드 시각 |
| `uploaded_by` | `TEXT` | O | 업로드 사용자 |
| `record_count` | `INT` | X | 처리 건수 |
| `status` | `VARCHAR(50)` | X | `success` 또는 `fail` |
| `error_message` | `TEXT` | O | 실패 메시지 |

---

## 5. 테이블: ai_reports

AI 실적 보고서를 공장/연월 단위로 저장하는 테이블입니다.

| 컬럼 | 타입 | NULL | 설명 |
| --- | --- | --- | --- |
| `id` | `INT AUTO_INCREMENT PK` | X | 자동 증가 ID |
| `factory` | `VARCHAR(50)` | X | 분석 대상 공장 |
| `report_year` | `INT` | X | 보고 연도 |
| `report_month` | `INT` | X | 보고 월 |
| `report_content` | `MEDIUMTEXT` | X | 보고서 본문 |
| `created_at` | `DATETIME` | X | 생성 시각 |
| `updated_at` | `DATETIME` | X | 최종 수정 시각 |

제약 조건:

- 고유키: `(factory, report_year, report_month)`

---

## 6. 실제 기준 파일

스키마의 최종 기준은 아래 파일입니다.

- `app/database/schema.sql`
