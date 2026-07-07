---
description: 프로젝트 기능에 대한 워크플로우
---

Step 1: 데이터 수집 및 유효성 검증 (Ingestion & Validation)
동작: 사용자가 업로드한 엑셀 파일 또는 수동 입력 데이터를 파싱하고 검증한다.

Skill 연동: validation_service.py를 호출하여 날짜 형식(YYYY-MM-DD), 필수 컬럼 누락 여부, 수치 데이터의 유효성을 검사한다. 오류 발생 시 즉시 반려하고 상세 사유를 사용자에게 반환한다.

Step 2: 동적 스키마 파악 및 쿼리 실행 (Intelligent Querying)
동작: AI Agent가 직접 DB 스키마를 읽어 분석에 필요한 데이터를 조회한다.

Skill 연동: ai_db_service.py의 SQLDatabase 객체를 통해 테이블 구조를 파악하고, 자연어 요청을 SQL 쿼리로 변환하여 전력, 연료, 용수 실적을 추출한다.

Step 3: 지표 집계 및 전년비 비교 (KPI Synthesis & YoY)
동작: 조회된 데이터를 바탕으로 전사 및 공장별 KPI를 산출하고 전년 동월(YoY) 실적과 대조한다.

Skill 연동: query_service.py를 활용하여 일별 데이터를 월별로 집계하고, 전년도 동일 기간의 가중 평균 원단위를 계산하여 증감률을 도출한다.

Step 4: AI 인사이트 생성 및 리포팅 (AI Analysis & Synthesis)
동작: 시니어 에너지 분석가 페르소나를 장착하여 경영진용 리포트를 작성한다.

Skill 연동: ai_report_prompt.md의 지침에 따라 Key Highlights, 상세 분석, 리스크 및 제언을 포함한 Markdown 형식의 리포트를 생성하며, 앞서 정의한 색상 강조 규칙을 적용한다.

Step 5: 리포트 관리 및 감사 기록 (Storage & Audit)
동작: 생성된 리포트를 DB에 저장하고 모든 데이터 변경 사항을 기록한다.

Skill 연동: ai_report_service.py를 통해 리포트를 ai_reports 테이블에 저장(UPSERT)하고, audit_service.py를 통해 변경자, 변경 시간, 변경 전후 값을 기록하여 데이터의 신뢰성을 확보한다.
