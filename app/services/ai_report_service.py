"""
AI Report Service
=================
OpenAI API를 활용하여 에너지 데이터를 분석하고 텍스트 리포트를 자동 생성 및 관리(저장/조회)합니다.
"""
# 이 파일은 AI 월간 리포트를 생성하고 저장합니다.

from typing import Optional
from pathlib import Path
from dotenv import load_dotenv

from app.database.db_connection import execute_query, execute_write

# 환경 변수 로드
load_dotenv()


# 리포트 prompt 경로를 확정합니다.
def _resolve_report_prompt_path() -> Path:
    app_dir = Path(__file__).resolve().parent.parent
    return app_dir / "prompts" / "ai_report_prompt.md"


# saved 리포트 값을 가져옵니다.
def get_saved_report(factory: str, year: int, month: int) -> Optional[dict]:
    """DB에 저장된 특정 연월, 공장의 AI 실적 보고서 조회"""
    query = "SELECT * FROM ai_reports WHERE factory = %s AND report_year = %s AND report_month = %s"
    results = execute_query(query, (factory, year, month))
    if results:
        return results[0]
    return None

# 리포트 데이터를 저장합니다.
def save_report(factory: str, year: int, month: int, content: str) -> bool:
    """새로 생성된 AI 실적 보고서를 DB에 저장 (동일 연월/공장 데이터는 덮어쓰기)"""
    query = """
        INSERT INTO ai_reports (factory, report_year, report_month, report_content, created_at, updated_at)
        VALUES (%s, %s, %s, %s, NOW(), NOW())
        ON DUPLICATE KEY UPDATE 
            report_content = VALUES(report_content),
            updated_at = NOW()
    """
    try:
        execute_write(query, (factory, year, month, content))
        return True
    except Exception as e:
        print(f"Error saving AI report: {e}")
        return False
