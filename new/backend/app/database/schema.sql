-- ============================================================
-- 제조 데이터 관리 PoC – MySQL Schema
-- ============================================================

-- 1. 에너지 일별 실적 테이블
CREATE TABLE IF NOT EXISTS energy_daily (
    id              INT AUTO_INCREMENT PRIMARY KEY,
    factory         VARCHAR(50)  NOT NULL,
    date            DATE         NOT NULL,

    -- 에너지/유틸리티 측정값
    freezing_power_kwh      DOUBLE NOT NULL DEFAULT 0,
    air_compressor_kwh      DOUBLE NOT NULL DEFAULT 0,
    total_power_kwh         DOUBLE NOT NULL DEFAULT 0,
    fuel_nm3                DOUBLE NOT NULL DEFAULT 0,
    water_ton               DOUBLE NOT NULL DEFAULT 0,
    wastewater_ton          DOUBLE NOT NULL DEFAULT 0,

    -- 생산량
    mix_prod_kg             DOUBLE NOT NULL DEFAULT 0,

    -- 원단위 (폐수 원단위는 폐기 — 폐수/용수 비로 대체, 화면/메일에서 즉석 계산)
    power_per_ton_kwh       DOUBLE NOT NULL DEFAULT 0,
    fuel_per_ton_nm3        DOUBLE NOT NULL DEFAULT 0,
    water_per_ton_ton       DOUBLE NOT NULL DEFAULT 0,

    -- 메타
    created_at      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    changed_by      TEXT,

    UNIQUE(factory, date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 2. 변경 이력(감사) 테이블
CREATE TABLE IF NOT EXISTS energy_daily_audit (
    id              INT AUTO_INCREMENT PRIMARY KEY,
    factory         VARCHAR(50)  NOT NULL,
    date            DATE         NOT NULL,
    column_name     VARCHAR(100) NOT NULL,
    old_value       TEXT,
    new_value       TEXT,
    change_type     VARCHAR(50)  NOT NULL DEFAULT 'MANUAL',
    changed_at      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    changed_by      TEXT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 3. 업로드 배치 기록 테이블
CREATE TABLE IF NOT EXISTS upload_batch (
    id              INT AUTO_INCREMENT PRIMARY KEY,
    filename        VARCHAR(255) NOT NULL,
    uploaded_at     DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    uploaded_by     TEXT,
    record_count    INT  NOT NULL DEFAULT 0,
    status          VARCHAR(50)  NOT NULL DEFAULT 'success',
    error_message   TEXT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 4. AI 실적 보고서 이력 테이블
CREATE TABLE IF NOT EXISTS ai_reports (
    id              INT AUTO_INCREMENT PRIMARY KEY,
    factory         VARCHAR(50)  NOT NULL,
    report_year     INT          NOT NULL,
    report_month    INT          NOT NULL,
    report_content  MEDIUMTEXT   NOT NULL,
    created_at      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE(factory, report_year, report_month)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 5. 에너지 예측 이력 테이블
--    v5.1(점추정) 호환: pred_value(=P50)/mape 유지.
--    v5.2(분위수+CQR) 추가: pred_p05/pred_p95(밴드 하·상한),
--      band_status('inside'|'over'|'under'), band_position(±, 정규화 거리).
--    이상 판정의 기본 기준은 band_status, mape는 참고용으로 계속 계산.
CREATE TABLE IF NOT EXISTS prediction_log (
    id              INT AUTO_INCREMENT PRIMARY KEY,
    factory         VARCHAR(50)  NOT NULL,
    pred_date       DATE         NOT NULL,
    target          VARCHAR(20)  NOT NULL,
    pred_value      DOUBLE       NOT NULL,
    pred_p05        DOUBLE       DEFAULT NULL,
    pred_p95        DOUBLE       DEFAULT NULL,
    actual_value    DOUBLE       DEFAULT NULL,
    mape            DOUBLE       DEFAULT NULL,
    band_status     VARCHAR(16)  DEFAULT NULL,
    band_position   DOUBLE       DEFAULT NULL,
    mix_prod_kg     DOUBLE       NOT NULL DEFAULT 0,
    model_path      VARCHAR(500) DEFAULT NULL,
    created_at      DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at      DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE(factory, pred_date, target),
    INDEX idx_pred_band_status (band_status, pred_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 6. 일별 생산실적 테이블 (사내 DW에서 추출한 품목 단위 실적)
--    - 출처: app/services/production_dw_service.py (Raw_생산실적/*.xlsx 통합)
--    - 동기화: app/services/production_dw_sync_service.py 가 매 rerun mtime 비교로 UPSERT (변경 시에만)
--    - 0인 일자도 보존 (상관관계 분석 / 일자 완전성)
--    - category1(보관유형) 과 category2(제품유형)은 독립 차원
--      예) 김해 멸균 유음료: (category1=상온, category2=MY)
CREATE TABLE IF NOT EXISTS production_daily (
    id              INT AUTO_INCREMENT PRIMARY KEY,
    date            DATE         NOT NULL,
    item_code       VARCHAR(50)  NOT NULL,
    item_name       VARCHAR(200) NOT NULL DEFAULT '',
    factory         VARCHAR(20)  NOT NULL,            -- 남양주/김해/광주/논산
    category1       VARCHAR(50)  NOT NULL,            -- 보관유형: 냉동/냉장/상온
    category2       VARCHAR(50)  DEFAULT NULL,        -- 제품유형: IC=아이스크림 / MY=유음료 / FM=발효유 / SN=스낵
    planned_qty     DOUBLE       NOT NULL DEFAULT 0,  -- 월간 누계 계획 (해당 월 모든 일자에 동일)
    actual_qty      DOUBLE       NOT NULL DEFAULT 0,  -- 일별 실적

    created_at      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,

    UNIQUE KEY uq_prod_daily (date, factory, item_code),
    INDEX idx_prod_factory_date (factory, date),
    INDEX idx_prod_cat1 (category1, date),
    INDEX idx_prod_cat2 (category2, date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 7. AI 이상 원인 진단 결과 캐시 테이블
--    - prediction_log 의 한 행(=공장·일자·항목)에 대해 LLM 진단을 한번 호출한 뒤
--      동일 키 재요청 시 LLM 재호출 없이 바로 반환하기 위한 캐시
--    - context_snapshot: LLM 에 보낸 컨텍스트 원본(재현/감사 용)
CREATE TABLE IF NOT EXISTS anomaly_analysis (
    id              INT AUTO_INCREMENT PRIMARY KEY,
    factory         VARCHAR(50)  NOT NULL,
    pred_date       DATE         NOT NULL,
    target          VARCHAR(20)  NOT NULL,
    pred_value      DOUBLE       NOT NULL,
    pred_p05        DOUBLE       DEFAULT NULL,
    pred_p95        DOUBLE       DEFAULT NULL,
    actual_value    DOUBLE       NOT NULL,
    mape            DOUBLE       NOT NULL,
    band_status     VARCHAR(16)  DEFAULT NULL,
    band_position   DOUBLE       DEFAULT NULL,
    diagnosis       MEDIUMTEXT   NOT NULL,
    context_snapshot MEDIUMTEXT  DEFAULT NULL,
    model_used      VARCHAR(64)  DEFAULT NULL,
    created_at      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY uq_anom (factory, pred_date, target)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 8. 절감 목표 테이블 (KPI 요약 카드의 "목표 대비 X%" 산출 기준)
--    - factory: ALL(전사) / 남양주 / 김해 / 광주 / 논산
--    - metric: power_per_ton / fuel_per_ton / water_per_ton / mix_prod
--      (폐수 원단위는 폐기 — 폐수/용수 비는 목표 없이 현황만 표시하므로 metric 없음)
--    - target_pct: 전년 대비 절감률(원단위 4종) 또는 증가율(생산량). 양수 입력.
--      • 원단위: 목표값 = 전년동기값 × (1 - target_pct/100), 낮을수록 좋음
--      • 생산량: 목표값 = 전년동기값 × (1 + target_pct/100), 높을수록 좋음
--    - 미입력 시 KPI 카드의 "목표 대비" 막대는 비표시.
CREATE TABLE IF NOT EXISTS savings_target (
    id              INT AUTO_INCREMENT PRIMARY KEY,
    factory         VARCHAR(50)  NOT NULL,
    year            INT          NOT NULL,
    metric          VARCHAR(40)  NOT NULL,
    target_pct      DOUBLE       NOT NULL,
    note            TEXT,
    created_at      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    changed_by      TEXT,
    UNIQUE KEY uq_target (factory, year, metric)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 9. 이벤트 메모(annotation) 테이블 — 이상치/스파이크 발생 시 실무자 원인·조치 메모
--    - 차트 marker 와 검색 페이지에서 동시에 사용
--    - target: power / fuel / water / wastewater / production / overall(전반)
--    - tag: 센서고장 / 설비정비 / 생산변경 / 외부요인 / 기타
--    - severity: info / warn / critical (배너 색상 구분용)
CREATE TABLE IF NOT EXISTS event_annotation (
    id              INT AUTO_INCREMENT PRIMARY KEY,
    factory         VARCHAR(50)  NOT NULL,
    event_date      DATE         NOT NULL,
    target          VARCHAR(20)  NOT NULL DEFAULT 'overall',
    tag             VARCHAR(40)  NOT NULL DEFAULT '기타',
    severity        VARCHAR(20)  NOT NULL DEFAULT 'info',
    note            TEXT         NOT NULL,
    created_at      DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at      DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    created_by      TEXT,
    INDEX idx_event_factory_date (factory, event_date),
    INDEX idx_event_target_date (target, event_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

