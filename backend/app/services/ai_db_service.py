"""
SQL Database Agent Service (LangChain)
=====================================
AI가 직접 DB 스키마를 파악하고 쿼리를 실행하여 동적 리포트를 생성합니다.
"""
# 이 파일은 AI가 DB 데이터를 읽어 설명할 수 있게 도와줍니다.

import os
from langchain_openai import ChatOpenAI
from langchain_community.utilities import SQLDatabase
from langchain_community.agent_toolkits import create_sql_agent
from dotenv import load_dotenv
import httpx

# 리포트 prompt 경로 확정 로직은 ai_report_service의 것을 재사용 (문자 단위 동일했던 중복 제거)
from app.services.ai_report_service import _resolve_report_prompt_path
from app.utils.tls import httpx_verify

# 환경 변수 로드
load_dotenv()


# SQL agent 값을 가져옵니다.
def get_sql_agent():
    """SQL Agent 인스턴스 초기화."""
    
    # DB 연결 정보 (Viewer 계정 권장)
    user = os.getenv("DB_VIEWER_USER", "fems_viewer")
    pw = os.getenv("DB_VIEWER_PASSWORD", "viewer1234")
    host = os.getenv("DB_HOST", "localhost")
    port = os.getenv("DB_PORT", "3306")
    db_name = os.getenv("DB_NAME", "fems_db")
    
    # SQLAlchemy URI (pymysql 드라이버 사용)
    db_uri = f"mysql+pymysql://{user}:{pw}@{host}:{port}/{db_name}"
    
    # DB 객체 생성 (agent가 테이블 구조를 읽을 수 있게 함)
    db = SQLDatabase.from_uri(db_uri)
    
    # LLM 설정 (GPT-5.4)
    llm = ChatOpenAI(
        model="gpt-5.4",
        temperature=0, # 분석 정확도를 위해 0으로 설정
        openai_api_key=os.getenv("OPENAI_API_KEY"),
        http_client=httpx.Client(verify=httpx_verify())
    )
    
    # SQL 에이전트 생성
    agent_executor = create_sql_agent(
        llm,
        db=db,
        agent_type="openai-tools",
        verbose=True
    )
    
    return agent_executor

# agent 리포트 작업을 실행합니다.
def run_agent_report(factory: str, year: int, month: int) -> str:
    """Agent를 활용하여 특정 공장/연월의 리포트를 생성합니다."""
    
    # 시스템 프롬프트(지침) 로드
    prompt_path = _resolve_report_prompt_path()
    with prompt_path.open("r", encoding="utf-8") as f:
        instructions = f.read()

    # 에이전트에 전달할 통합 프롬프트 구성
    # AI가 스스로 쿼리해야 하므로 "데이터가 여기 있다"고 주는 게 아니라 "조회해라"라고 지시합니다.
    query_input = (
        f"{instructions}\n\n"
        f"--- [분석 요청] ---\n"
        f"대상: {factory} 사업장 (기준: {year}년 {month}월)\n"
        f"임무: 위 {factory} 사업장의 {year}년 {month}월 실적(전력, 연료, 용수, 생산량)을 DB에서 직접 조회하고, "
        f"전년 동월({year-1}년 {month}월) 실적과 비교하여 전문적인 분석 리포트를 작성하세요.\n"
        f"반드시 모든 수치(절대 사용량 및 원단위)를 포함해야 하며, 전년 대비 증감률을 계산하세요."
    )
    
    try:
        agent = get_sql_agent()
        response = agent.invoke({"input": query_input})
        return response["output"]
    except Exception as e:
        return f"AI Agent 분석 중 오류가 발생했습니다: {str(e)}"
