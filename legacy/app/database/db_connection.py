"""
DB Connection Module (MySQL)
====================
MySQL 연결 관리 및 스키마 초기화.
"""
# 이 파일은 데이터베이스 연결과 초기 설정을 담당합니다.

import os
import socket
import logging
import threading
from contextlib import contextmanager
import mysql.connector
from mysql.connector import Error
from mysql.connector import pooling
from pathlib import Path
from dotenv import load_dotenv

# .env 파일 로드
load_dotenv()

# DB 설정 (기본값)
DB_HOST = os.getenv("DB_HOST", "localhost")
DB_PORT = int(os.getenv("DB_PORT", 3306))
DB_NAME = os.getenv("DB_NAME", "fems_db")
DB_POOL_SIZE = int(os.getenv("DB_POOL_SIZE", "8"))

logger = logging.getLogger(__name__)
_POOL_LOCK = threading.Lock()
_POOLS: dict[tuple[bool, str, str, str], pooling.MySQLConnectionPool] = {}

# loopback / IPv6 localhost 표현들 — 모두 호스트 PC로 간주
_LOOPBACK_IPS = frozenset({"127.0.0.1", "::1", "::ffff:127.0.0.1"})


def _get_client_ip() -> str | None:
    """현재 Streamlit 요청을 보낸 클라이언트의 IP. 실패 시 None."""
    try:
        from streamlit.runtime.scriptrunner import get_script_run_ctx
        from streamlit.runtime import get_instance
        ctx = get_script_run_ctx()
        if ctx is None or ctx.session_id is None:
            return None
        session_info = get_instance()._session_mgr.get_session_info(ctx.session_id)
        if session_info is None or getattr(session_info, "client", None) is None:
            return None
        return session_info.client.request.remote_ip
    except Exception:
        return None


def _is_host_machine_ip(ip: str | None) -> bool:
    """IP가 서버를 돌리는 PC와 동일한지 판단.

    loopback(127.0.0.1, ::1) 또는 서버가 가진 LAN IP 중 하나에 해당하면 True.
    같은 PC에서 자기 LAN IP로 접속해도(192.168.x.x 등) 호스트로 인식되도록 보강.
    """
    if not ip:
        return False
    if ip in _LOOPBACK_IPS:
        return True
    try:
        host_ips = set(socket.gethostbyname_ex(socket.gethostname())[2])
        return ip in host_ips
    except Exception:
        return False


# 관리자 여부를 확인합니다.
def is_admin() -> bool:
    """접속 클라이언트가 호스트 PC면 관리자(root), 외부 PC면 viewer로 자동 분류.

    - 터미널 실행(DB 초기화 등): 항상 관리자
    - Streamlit 세션: 클라이언트 IP가 loopback/호스트 자체 IP일 때만 관리자
    - 그 외(외부 PC, 판별 실패): viewer (안전 fallback)
    """
    try:
        from streamlit.runtime import exists
        if not exists():
            return True  # 터미널 실행 — DB 초기화·동기화 스크립트 등
        return _is_host_machine_ip(_get_client_ip())
    except Exception:
        return False


# credentials 값을 가져옵니다.
def get_credentials(admin: bool = False):
    """상태 또는 강제 요청에 따라 DB 계정 정보 반환."""
    # admin 매개변수가 True인 경우 무조건 관리자 계정 반환
    if admin:
        return os.getenv("DB_ADMIN_USER", "root"), os.getenv("DB_ADMIN_PASSWORD", "")
    
    # 그 외에는 Streamlit 세션 상태에 따라 결정
    if is_admin():
        return os.getenv("DB_ADMIN_USER", "root"), os.getenv("DB_ADMIN_PASSWORD", "")
    else:
        return os.getenv("DB_VIEWER_USER", "fems_viewer"), os.getenv("DB_VIEWER_PASSWORD", "viewer1234")


# 스키마 경로 값을 가져옵니다.
def _get_schema_path() -> str:
    """schema.sql 파일 경로 반환."""
    return str(Path(__file__).resolve().parent / "schema.sql")


# 연결 값을 가져옵니다.
def get_connection(with_db: bool = True, admin: bool = False) -> mysql.connector.MySQLConnection:
    """세션 상태 또는 강제 관리자 요청에 따라 연결 반환."""
    user, password = get_credentials(admin=admin)

    config = {
        "host": DB_HOST,
        "port": DB_PORT,
        "user": user,
        "password": password,
    }
    if with_db:
        config["database"] = DB_NAME
    
    try:
        if DB_POOL_SIZE > 0:
            key = (with_db, user, password, DB_NAME if with_db else "")
            with _POOL_LOCK:
                pool = _POOLS.get(key)
                if pool is None:
                    pool = pooling.MySQLConnectionPool(
                        pool_name=f"fems_{'db' if with_db else 'server'}_{len(_POOLS) + 1}",
                        pool_size=DB_POOL_SIZE,
                        pool_reset_session=True,
                        **config,
                    )
                    _POOLS[key] = pool
            return pool.get_connection()

        return mysql.connector.connect(**config)
    except Error as e:
        user, _ = get_credentials(admin=admin)
        logger.exception("Error connecting to MySQL with user '%s': %s", user, e)
        raise


@contextmanager
def managed_connection(with_db: bool = True, admin: bool = False):
    """Yield a DB connection and always return it to the pool/close it."""
    conn = get_connection(with_db=with_db, admin=admin)
    try:
        yield conn
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        conn.close()


@contextmanager
def managed_cursor(
    with_db: bool = True,
    admin: bool = False,
    dictionary: bool = False,
    buffered: bool = False,
):
    """Yield (conn, cursor) and close both safely."""
    conn = get_connection(with_db=with_db, admin=admin)
    cursor = None
    try:
        cursor = conn.cursor(dictionary=dictionary, buffered=buffered)
        yield conn, cursor
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        if cursor is not None:
            cursor.close()
        conn.close()


# 초기값을 준비합니다.
def init_db():
    """데이터베이스 생성 및 스키마 초기화."""
    schema_path = _get_schema_path()
    with open(schema_path, "r", encoding="utf-8") as f:
        schema_sql = f.read()

    # 1. DB 자체를 생성하기 위해 DB 지정 없이 연결 (관리자 권한 필수)
    with managed_cursor(with_db=False, admin=True) as (conn, cursor):
        cursor.execute(f"CREATE DATABASE IF NOT EXISTS {DB_NAME} DEFAULT CHARACTER SET utf8mb4;")
        conn.commit()

    # 2. DB 선택 후 테이블 생성 (관리자 권한 필수)
    with managed_cursor(with_db=True, admin=True) as (conn, cursor):
        try:
            # SQL 문장을 ';' 기준으로 분리하여 개별 실행 (주석 제외)
            sql_statements = [s.strip() for s in schema_sql.split(';') if s.strip()]
            for statement in sql_statements:
                if statement:
                    cursor.execute(statement)
            conn.commit()
            print(f"Database '{DB_NAME}' and tables initialized successfully.")
        except Error as e:
            print(f"Error initializing database: {e}")
            raise

    # 2-b. 멱등 ALTER 마이그레이션 — 기존 DB에 신규 컬럼이 없으면 추가
    try:
        _apply_idempotent_migrations()
    except Exception as e:
        print(f"Warning: schema migration step failed: {e}")

    # 3. 조회용 계정(fems_viewer) 자동 생성 및 권한 부여 (관리자 권한 필수)
    try:
        with managed_cursor(with_db=False, admin=True) as (conn, cursor):
            # get_credentials 대신 직접 환경변수에서 조회용 계정 정보를 가져옴 (is_admin 영향 방지)
            v_user = os.getenv("DB_VIEWER_USER", "fems_viewer")
            v_pw = os.getenv("DB_VIEWER_PASSWORD", "viewer1234")

            # 'root' 계정을 실수로 수정하는 것 방지
            if v_user == "root":
                print("Warning: Skipping viewer account initialization because DB_VIEWER_USER is set to 'root'.")
            else:
                # % (외부 접속용) 및 localhost (로컬 접속용) 모두 생성
                for host in ['%', 'localhost']:
                    cursor.execute(f"CREATE USER IF NOT EXISTS '{v_user}'@'{host}' IDENTIFIED BY '{v_pw}';")
                    cursor.execute(f"ALTER USER '{v_user}'@'{host}' IDENTIFIED BY '{v_pw}';")
                    cursor.execute(f"GRANT SELECT ON {DB_NAME}.* TO '{v_user}'@'{host}';")

                cursor.execute("FLUSH PRIVILEGES;")
                conn.commit()
                print(f"Viewer account '{v_user}' initialized for both '%' and 'localhost'.")
    except Exception as e:
        print(f"Warning: Could not initialize viewer account automatically: {e}")


# v5.2(분위수 회귀) 도입에 따른 멱등 ALTER 마이그레이션.
# - 기존 v5.1 점추정 DB(이미 운영 중)는 ALTER로 컬럼만 덧붙임.
# - 신규 설치는 schema.sql의 CREATE에서 컬럼이 이미 포함되어 ALTER가 스킵됨.
# - INFORMATION_SCHEMA로 컬럼 존재 여부를 확인하므로 재실행해도 안전.
_PENDING_COLUMN_MIGRATIONS: list[tuple[str, str, str]] = [
    # (table, column, ALTER fragment)
    ("prediction_log",   "pred_p05",      "ADD COLUMN pred_p05 DOUBLE DEFAULT NULL AFTER pred_value"),
    ("prediction_log",   "pred_p95",      "ADD COLUMN pred_p95 DOUBLE DEFAULT NULL AFTER pred_p05"),
    ("prediction_log",   "band_status",   "ADD COLUMN band_status VARCHAR(16) DEFAULT NULL AFTER mape"),
    ("prediction_log",   "band_position", "ADD COLUMN band_position DOUBLE DEFAULT NULL AFTER band_status"),
    ("anomaly_analysis", "pred_p05",      "ADD COLUMN pred_p05 DOUBLE DEFAULT NULL AFTER pred_value"),
    ("anomaly_analysis", "pred_p95",      "ADD COLUMN pred_p95 DOUBLE DEFAULT NULL AFTER pred_p05"),
    ("anomaly_analysis", "band_status",   "ADD COLUMN band_status VARCHAR(16) DEFAULT NULL AFTER mape"),
    ("anomaly_analysis", "band_position", "ADD COLUMN band_position DOUBLE DEFAULT NULL AFTER band_status"),
]

_PENDING_INDEX_MIGRATIONS: list[tuple[str, str, str]] = [
    # (table, index_name, CREATE INDEX fragment)
    ("prediction_log", "idx_pred_band_status",
     "CREATE INDEX idx_pred_band_status ON prediction_log (band_status, pred_date)"),
]

# 폐기된 컬럼의 멱등 DROP. 컬럼이 존재할 때만 ALTER DROP 을 1회 수행한다.
# - 폐수 원단위(energy_daily.wastewater_per_ton_ton)는 폐수/용수 비로 대체되어 제거.
#   raw 폐수량/용수량(wastewater_ton/water_ton)은 그대로 보존되므로 비율 재계산 가능.
_PENDING_COLUMN_DROPS: list[tuple[str, str, str]] = [
    # (table, column, ALTER fragment)
    ("energy_daily", "wastewater_per_ton_ton", "DROP COLUMN wastewater_per_ton_ton"),
]


def _apply_idempotent_migrations() -> None:
    """INFORMATION_SCHEMA 기반으로 ALTER 누락분만 적용."""
    with managed_cursor(with_db=True, admin=True) as (conn, cur):
        for table, column, alter_fragment in _PENDING_COLUMN_MIGRATIONS:
            cur.execute(
                """
                SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS
                WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s AND COLUMN_NAME = %s
                """,
                (DB_NAME, table, column),
            )
            (exists_count,) = cur.fetchone()
            if exists_count == 0:
                cur.execute(f"ALTER TABLE {table} {alter_fragment}")
                print(f"  migration: {table}.{column} added")

        for table, column, alter_fragment in _PENDING_COLUMN_DROPS:
            cur.execute(
                """
                SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS
                WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s AND COLUMN_NAME = %s
                """,
                (DB_NAME, table, column),
            )
            (exists_count,) = cur.fetchone()
            if exists_count > 0:
                cur.execute(f"ALTER TABLE {table} {alter_fragment}")
                print(f"  migration: {table}.{column} dropped")

        for table, index_name, create_sql in _PENDING_INDEX_MIGRATIONS:
            cur.execute(
                """
                SELECT COUNT(*) FROM INFORMATION_SCHEMA.STATISTICS
                WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s AND INDEX_NAME = %s
                """,
                (DB_NAME, table, index_name),
            )
            (exists_count,) = cur.fetchone()
            if exists_count == 0:
                cur.execute(create_sql)
                print(f"  migration: index {index_name} on {table} created")

        conn.commit()


# 조회을 실행합니다.
def execute_query(query: str, params: tuple = ()) -> list[dict]:
    """SELECT 쿼리 실행 후 dict 리스트 반환."""
    with managed_cursor(dictionary=True) as (_conn, cursor):
        cursor.execute(query, params)
        result = cursor.fetchall()
        return result


# 쓰기을 실행합니다.
def execute_write(query: str, params: tuple = ()) -> int:
    """INSERT/UPDATE/DELETE 실행 후 lastrowid 반환."""
    with managed_cursor() as (conn, cursor):
        try:
            cursor.execute(query, params)
            conn.commit()
            return cursor.lastrowid
        except Exception:
            # 실패 시 트랜잭션 롤백 (롤백 자체 실패는 원래 예외를 가리지 않도록 무시)
            try:
                conn.rollback()
            except Exception:
                pass
            raise


# many을 실행합니다.
def execute_many(query: str, params_list: list[tuple]) -> int:
    """다수 행 INSERT/UPDATE 실행 후 영향받은 행 수 반환."""
    with managed_cursor() as (conn, cursor):
        try:
            cursor.executemany(query, params_list)
            conn.commit()
            return cursor.rowcount
        except Exception:
            # 실패 시 트랜잭션 롤백 (롤백 자체 실패는 원래 예외를 가리지 않도록 무시)
            try:
                conn.rollback()
            except Exception:
                pass
            raise
