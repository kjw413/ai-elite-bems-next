-- ============================================================
-- 마이그레이션: 공장 코드 → 한글 공장명 변환 (안전판)
--   F10A → 남양주1
--   F10B → 남양주2
--   F10  → 남양주          (집계 코드. 보통 DB에는 저장되지 않지만 안전하게 포함)
--   F20  → 김해
--   F30  → 광주
--   F40  → 논산
--
-- 영향 받는 테이블 (factory 컬럼 존재):
--   - energy_daily        (UNIQUE: factory+date)            → 중복 제거 후 RENAME
--   - energy_daily_audit  (감사 이력, UNIQUE 없음)            → 단순 RENAME
--   - ai_reports          (UNIQUE: factory+year+month)       → 중복 제거 후 RENAME
--   - prediction_log      (UNIQUE: factory+pred_date+target) → 중복 제거 후 RENAME
--   - production_daily    → 마이그레이션 대상 아님 (F-코드 그대로 유지, 표시 시점에서 매핑)
--
-- 안전성 보강 — 처음 작성한 단순 UPDATE 스크립트는 이미 같은 (factory, date) 키로
-- 한글 행이 존재하는 케이스에서 UNIQUE 제약 위반으로 전체 트랜잭션이 실패하는 문제가
-- 있었습니다. 본 안전판은 다음을 보장합니다.
--   1) "한글 행이 이미 있고 동일 자연키 충돌"이 발생하는 F-코드 행은 DELETE.
--      (운영 데이터 검증 결과 energy_daily 는 값까지 동일했고,
--       prediction_log 는 한글 행이 더 최근 모델 실행 결과라 한글 행 보존이 더 안전.)
--   2) 충돌하지 않는 F-코드 행만 그대로 RENAME.
--   3) 모든 작업은 단일 트랜잭션 — 도중 실패 시 ROLLBACK.
--   4) 멱등성: 두 번 실행해도 결과가 같음. 이미 한글로 변환된 환경에서는
--      0건 처리되고 정상 종료됩니다.
--
-- 사용법:
--   USE <DB_NAME>;
--   SOURCE app/database/migrations/2026_04_30_rename_factory_codes.sql;
-- 또는 mysql 클라이언트:
--   mysql -u <user> -p <DB_NAME> < app/database/migrations/2026_04_30_rename_factory_codes.sql
-- 또는 본 프로젝트 헬퍼:
--   py -3 tools/run_factory_code_migration.py
-- ============================================================

START TRANSACTION;

-- ----------------------------------------------------------------
-- 1) energy_daily — 중복(F-코드 + 한글) 제거 후 잔여만 RENAME
--    UNIQUE(factory, date). 운영 데이터 검증 결과 F-코드 행과 한글 행은
--    같은 date 에서 사용량/생산량 값이 동일하게 들어가 있었으므로
--    F-코드 행을 모두 삭제해도 데이터 손실은 없습니다.
-- ----------------------------------------------------------------
DELETE ed FROM energy_daily ed
JOIN energy_daily ko
  ON ed.date = ko.date
WHERE ed.factory = 'F10A' AND ko.factory = '남양주1';
DELETE ed FROM energy_daily ed
JOIN energy_daily ko
  ON ed.date = ko.date
WHERE ed.factory = 'F10B' AND ko.factory = '남양주2';
DELETE ed FROM energy_daily ed
JOIN energy_daily ko
  ON ed.date = ko.date
WHERE ed.factory = 'F10'  AND ko.factory = '남양주';
DELETE ed FROM energy_daily ed
JOIN energy_daily ko
  ON ed.date = ko.date
WHERE ed.factory = 'F20'  AND ko.factory = '김해';
DELETE ed FROM energy_daily ed
JOIN energy_daily ko
  ON ed.date = ko.date
WHERE ed.factory = 'F30'  AND ko.factory = '광주';
DELETE ed FROM energy_daily ed
JOIN energy_daily ko
  ON ed.date = ko.date
WHERE ed.factory = 'F40'  AND ko.factory = '논산';

-- 충돌하지 않는 잔여 F-코드는 그대로 RENAME (안전)
UPDATE energy_daily SET factory = '남양주1' WHERE factory = 'F10A';
UPDATE energy_daily SET factory = '남양주2' WHERE factory = 'F10B';
UPDATE energy_daily SET factory = '남양주'  WHERE factory = 'F10';
UPDATE energy_daily SET factory = '김해'    WHERE factory = 'F20';
UPDATE energy_daily SET factory = '광주'    WHERE factory = 'F30';
UPDATE energy_daily SET factory = '논산'    WHERE factory = 'F40';

-- ----------------------------------------------------------------
-- 2) energy_daily_audit — UNIQUE 없음, 단순 RENAME
-- ----------------------------------------------------------------
UPDATE energy_daily_audit SET factory = '남양주1' WHERE factory = 'F10A';
UPDATE energy_daily_audit SET factory = '남양주2' WHERE factory = 'F10B';
UPDATE energy_daily_audit SET factory = '남양주'  WHERE factory = 'F10';
UPDATE energy_daily_audit SET factory = '김해'    WHERE factory = 'F20';
UPDATE energy_daily_audit SET factory = '광주'    WHERE factory = 'F30';
UPDATE energy_daily_audit SET factory = '논산'    WHERE factory = 'F40';

-- ----------------------------------------------------------------
-- 3) ai_reports — UNIQUE(factory, report_year, report_month). 중복 제거 후 RENAME
-- ----------------------------------------------------------------
DELETE ar FROM ai_reports ar
JOIN ai_reports ko
  ON ar.report_year = ko.report_year AND ar.report_month = ko.report_month
WHERE ar.factory = 'F10A' AND ko.factory = '남양주1';
DELETE ar FROM ai_reports ar
JOIN ai_reports ko
  ON ar.report_year = ko.report_year AND ar.report_month = ko.report_month
WHERE ar.factory = 'F10B' AND ko.factory = '남양주2';
DELETE ar FROM ai_reports ar
JOIN ai_reports ko
  ON ar.report_year = ko.report_year AND ar.report_month = ko.report_month
WHERE ar.factory = 'F10'  AND ko.factory = '남양주';
DELETE ar FROM ai_reports ar
JOIN ai_reports ko
  ON ar.report_year = ko.report_year AND ar.report_month = ko.report_month
WHERE ar.factory = 'F20'  AND ko.factory = '김해';
DELETE ar FROM ai_reports ar
JOIN ai_reports ko
  ON ar.report_year = ko.report_year AND ar.report_month = ko.report_month
WHERE ar.factory = 'F30'  AND ko.factory = '광주';
DELETE ar FROM ai_reports ar
JOIN ai_reports ko
  ON ar.report_year = ko.report_year AND ar.report_month = ko.report_month
WHERE ar.factory = 'F40'  AND ko.factory = '논산';

UPDATE ai_reports SET factory = '남양주1' WHERE factory = 'F10A';
UPDATE ai_reports SET factory = '남양주2' WHERE factory = 'F10B';
UPDATE ai_reports SET factory = '남양주'  WHERE factory = 'F10';
UPDATE ai_reports SET factory = '김해'    WHERE factory = 'F20';
UPDATE ai_reports SET factory = '광주'    WHERE factory = 'F30';
UPDATE ai_reports SET factory = '논산'    WHERE factory = 'F40';

-- ----------------------------------------------------------------
-- 4) prediction_log — UNIQUE(factory, pred_date, target). 중복 제거 후 RENAME
--    F-코드 행과 한글 행이 같은 (date,target) 으로 양립하면 한글 행이 더
--    최근 모델 실행 결과이므로 F-코드 행을 삭제해 한글 행을 보존합니다.
-- ----------------------------------------------------------------
DELETE p FROM prediction_log p
JOIN prediction_log ko
  ON p.pred_date = ko.pred_date AND p.target = ko.target
WHERE p.factory = 'F10A' AND ko.factory = '남양주1';
DELETE p FROM prediction_log p
JOIN prediction_log ko
  ON p.pred_date = ko.pred_date AND p.target = ko.target
WHERE p.factory = 'F10B' AND ko.factory = '남양주2';
DELETE p FROM prediction_log p
JOIN prediction_log ko
  ON p.pred_date = ko.pred_date AND p.target = ko.target
WHERE p.factory = 'F10'  AND ko.factory = '남양주';
DELETE p FROM prediction_log p
JOIN prediction_log ko
  ON p.pred_date = ko.pred_date AND p.target = ko.target
WHERE p.factory = 'F20'  AND ko.factory = '김해';
DELETE p FROM prediction_log p
JOIN prediction_log ko
  ON p.pred_date = ko.pred_date AND p.target = ko.target
WHERE p.factory = 'F30'  AND ko.factory = '광주';
DELETE p FROM prediction_log p
JOIN prediction_log ko
  ON p.pred_date = ko.pred_date AND p.target = ko.target
WHERE p.factory = 'F40'  AND ko.factory = '논산';

UPDATE prediction_log SET factory = '남양주1' WHERE factory = 'F10A';
UPDATE prediction_log SET factory = '남양주2' WHERE factory = 'F10B';
UPDATE prediction_log SET factory = '남양주'  WHERE factory = 'F10';
UPDATE prediction_log SET factory = '김해'    WHERE factory = 'F20';
UPDATE prediction_log SET factory = '광주'    WHERE factory = 'F30';
UPDATE prediction_log SET factory = '논산'    WHERE factory = 'F40';

-- ----------------------------------------------------------------
-- 5) production_daily — 변환 대상에서 제외
--    이 테이블은 사내 DW 추출 시점부터 F-코드를 그대로 보존하고,
--    UI(production_performance.py) 가 표시 시점에서 한글 매핑하도록 설계되어 있습니다.
--    여기서 변환하면 production_performance.py의 _DISPLAY_TO_FACTORY_CODE 매핑이 깨지므로
--    의도적으로 SKIP 합니다.
-- ----------------------------------------------------------------

-- ----------------------------------------------------------------
-- 검증 쿼리 (실행 후 production_daily 외 모두 0이어야 정상)
-- ----------------------------------------------------------------
-- SELECT 'energy_daily',       COUNT(*) FROM energy_daily       WHERE factory IN ('F10A','F10B','F10','F20','F30','F40');
-- SELECT 'energy_daily_audit', COUNT(*) FROM energy_daily_audit WHERE factory IN ('F10A','F10B','F10','F20','F30','F40');
-- SELECT 'ai_reports',         COUNT(*) FROM ai_reports         WHERE factory IN ('F10A','F10B','F10','F20','F30','F40');
-- SELECT 'prediction_log',     COUNT(*) FROM prediction_log     WHERE factory IN ('F10A','F10B','F10','F20','F30','F40');

COMMIT;
