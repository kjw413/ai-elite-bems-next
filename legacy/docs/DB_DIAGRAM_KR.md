# DB 다이어그램

기준 파일: `app/database/schema.sql`

## 이미지

![DB 구조 한눈에 보기](img/db_schema_overview.png)

![DB 상세 ERD](img/db_schema_erd.png)

주의: 현재 스키마에는 `FOREIGN KEY` 제약이 정의되어 있지 않습니다. 아래 선은 코드에서 사용하는 논리 관계입니다. 특히 `production_daily`는 생산 DW 기준 F-코드(`F10A`, `F10B`, `F20`, `F30`, `F40`)를 중심으로 쓰이고, `energy_daily`, `prediction_log` 등은 한글 공장명을 중심으로 쓰므로 조인 시 공장 코드 매핑이 필요합니다.

## 한눈에 보는 구조

```mermaid
flowchart LR
    U[upload_batch<br/>업로드 배치 이력]
    E[energy_daily<br/>일별 에너지·생산 실적<br/>UK: factory + date]
    A[energy_daily_audit<br/>변경 이력]
    D[production_daily<br/>일별·품목별 생산 DW<br/>UK: date + factory + item_code]
    P[prediction_log<br/>예측 이력·실측 비교<br/>UK: factory + pred_date + target]
    X[anomaly_analysis<br/>AI 이상 원인 진단 캐시<br/>UK: factory + pred_date + target]
    R[ai_reports<br/>월간 AI 보고서<br/>UK: factory + year + month]
    T[savings_target<br/>연간 절감 목표<br/>UK: factory + year + metric]
    M[event_annotation<br/>일별 이벤트 메모]

    U -->|upsert 결과| E
    U -->|변경 로그 생성| A
    E -->|factory + date| A
    E -->|학습 소스 / actual 채움| P
    P -->|동일 자연키 캐시| X
    E -->|진단 컨텍스트| X
    D -.->|date + factory code map| E
    E -->|월 집계| R
    T -.->|factory + year + metric| E
    M -.->|factory + event_date| E

    classDef core fill:#ecfeff,stroke:#0891b2,color:#083344;
    classDef log fill:#f8fafc,stroke:#64748b,color:#0f172a;
    classDef ai fill:#f0fdf4,stroke:#16a34a,color:#14532d;
    classDef prod fill:#fff7ed,stroke:#ea580c,color:#7c2d12;
    class E core;
    class U,A,M,T log;
    class P,X,R ai;
    class D prod;
```

## 상세 ERD

```mermaid
erDiagram
    energy_daily {
        INT id PK
        VARCHAR factory UK
        DATE date UK
        DOUBLE freezing_power_kwh
        DOUBLE air_compressor_kwh
        DOUBLE total_power_kwh
        DOUBLE fuel_nm3
        DOUBLE water_ton
        DOUBLE wastewater_ton
        DOUBLE mix_prod_kg
        DOUBLE power_per_ton_kwh
        DOUBLE fuel_per_ton_nm3
        DOUBLE water_per_ton_ton
        DATETIME created_at
        DATETIME updated_at
        TEXT changed_by
    }

    energy_daily_audit {
        INT id PK
        VARCHAR factory
        DATE date
        VARCHAR column_name
        TEXT old_value
        TEXT new_value
        VARCHAR change_type
        DATETIME changed_at
        TEXT changed_by
    }

    upload_batch {
        INT id PK
        VARCHAR filename
        DATETIME uploaded_at
        TEXT uploaded_by
        INT record_count
        VARCHAR status
        TEXT error_message
    }

    ai_reports {
        INT id PK
        VARCHAR factory UK
        INT report_year UK
        INT report_month UK
        MEDIUMTEXT report_content
        DATETIME created_at
        DATETIME updated_at
    }

    prediction_log {
        INT id PK
        VARCHAR factory UK
        DATE pred_date UK
        VARCHAR target UK
        DOUBLE pred_value
        DOUBLE pred_p05
        DOUBLE pred_p95
        DOUBLE actual_value
        DOUBLE mape
        VARCHAR band_status
        DOUBLE band_position
        DOUBLE mix_prod_kg
        VARCHAR model_path
        DATETIME created_at
        DATETIME updated_at
    }

    production_daily {
        INT id PK
        DATE date UK
        VARCHAR item_code UK
        VARCHAR item_name
        VARCHAR factory UK
        VARCHAR category1
        VARCHAR category2
        DOUBLE planned_qty
        DOUBLE actual_qty
        DATETIME created_at
        DATETIME updated_at
    }

    anomaly_analysis {
        INT id PK
        VARCHAR factory UK
        DATE pred_date UK
        VARCHAR target UK
        DOUBLE pred_value
        DOUBLE pred_p05
        DOUBLE pred_p95
        DOUBLE actual_value
        DOUBLE mape
        VARCHAR band_status
        DOUBLE band_position
        MEDIUMTEXT diagnosis
        MEDIUMTEXT context_snapshot
        VARCHAR model_used
        DATETIME created_at
        DATETIME updated_at
    }

    savings_target {
        INT id PK
        VARCHAR factory UK
        INT year UK
        VARCHAR metric UK
        DOUBLE target_pct
        TEXT note
        DATETIME created_at
        DATETIME updated_at
        TEXT changed_by
    }

    event_annotation {
        INT id PK
        VARCHAR factory
        DATE event_date
        VARCHAR target
        VARCHAR tag
        VARCHAR severity
        TEXT note
        DATETIME created_at
        DATETIME updated_at
        TEXT created_by
    }

    upload_batch ||--o{ energy_daily : upserts
    upload_batch ||--o{ energy_daily_audit : records
    energy_daily o|--o{ energy_daily_audit : factory_date
    energy_daily o|--o{ prediction_log : actual_lookup
    prediction_log ||--o| anomaly_analysis : diagnosis_cache
    energy_daily o|--o{ anomaly_analysis : diagnosis_context
    energy_daily o|--o{ production_daily : date_factory_map
    ai_reports o|--o{ energy_daily : monthly_rollup
    savings_target o|--o{ energy_daily : yearly_kpi
    event_annotation }o--o| energy_daily : event_overlay
```

## 읽는 법

- `energy_daily`가 중심 테이블입니다. 공장별 일자 단위의 에너지 사용량, 생산량, 원단위를 보관합니다.
- `production_daily`는 품목 단위 생산실적입니다. 같은 일자·공장 기준으로 `energy_daily`와 분석에 결합되지만, 공장 코드 체계가 달라 매핑 로직을 거칩니다.
- `prediction_log`는 예측값과 실제값 비교 결과를 저장하고, `anomaly_analysis`는 같은 자연키의 LLM 진단 결과를 캐시합니다.
- `ai_reports`, `savings_target`, `event_annotation`은 각각 월간 보고서, 연간 목표, 현장 이벤트 메모처럼 화면 기능을 보조하는 테이블입니다.
- `upload_batch`는 업로드 작업 단위 기록이며, 행 단위 `batch_id`가 다른 테이블에 저장되지는 않습니다.

