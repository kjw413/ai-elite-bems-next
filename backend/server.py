"""FastAPI bridge for the existing AI-Elite-BEMS Python/MySQL core.

The browser talks directly to this process on port 8000 so the original
client-IP based admin/viewer policy remains meaningful.  Read endpoints use
parameterized SQL.  Model, report and upload actions delegate to the existing
Python services under BEMS_CORE_ROOT instead of reimplementing their logic.

독립화(2026-07-16): 데이터 처리 서비스(app.services.* 등)는 legacy에서
`new/backend/app/`으로 복사한 로컬 사본을 import한다. legacy 폴더는 더 이상
코드 의존 대상이 아니며, .env도 `new/backend/.env`를 우선 사용한다
(legacy/.env는 전환기 fallback). 복사본 대비 수정 내역은
docs/AI_Elite_BEMS_Next_독립화_계획서.md의 발견·수정 로그에 기록한다.
"""

from __future__ import annotations

import asyncio
import calendar
import importlib
import logging
import math
import os
import socket
import sys
import threading
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pymysql
from dotenv import load_dotenv
from fastapi import FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

PROJECT_ROOT = Path(__file__).resolve().parents[1]
logger = logging.getLogger(__name__)
# Strict legacy read-only policy: imported core modules must not create __pycache__.
sys.dont_write_bytecode = True


def _resolve_core_root() -> Path:
    """기존 BEMS(Python 서비스) 루트 탐색.

    우선순위: BEMS_CORE_ROOT 환경변수 → 같은 저장소의 legacy/ → 형제 AI-Elite-BEMS/.
    """
    candidates = []
    env_root = os.getenv("BEMS_CORE_ROOT", "").strip()
    if env_root:
        candidates.append(Path(env_root))
    candidates.append(PROJECT_ROOT.parent / "legacy")
    candidates.append(PROJECT_ROOT.parent / "AI-Elite-BEMS")
    for candidate in candidates:
        resolved = candidate.resolve()
        if (resolved / "app" / "services").exists():
            return resolved
    return candidates[0].resolve() if env_root else (PROJECT_ROOT.parent / "legacy").resolve()


# 로컬 복사본(app 패키지)의 루트. import_core는 이 경로만 사용한다.
LOCAL_CORE_ROOT = PROJECT_ROOT / "backend"

# 전환기 fallback: backend/.env가 우선하고, 없던 키만 legacy/.env에서 보충한다.
CORE_ROOT = _resolve_core_root()
load_dotenv(LOCAL_CORE_ROOT / ".env")
load_dotenv(CORE_ROOT / ".env")

def _discover_local_addresses() -> set[str]:
    addresses = {"127.0.0.1", "::1"}
    try:
        _, _, found = socket.gethostbyname_ex(socket.gethostname())
        addresses.update(found)
    except OSError:
        logger.warning("Unable to discover local server addresses.", exc_info=True)
    return addresses


LOCAL_ADDRESSES = _discover_local_addresses()


def _canonical_origin(value: str) -> str | None:
    """Return a normalized HTTP Origin suitable for exact allowlist checks."""
    try:
        parsed = urlsplit(value.strip())
        if (
            parsed.scheme.lower() not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or parsed.path not in ("", "/")
        ):
            return None
        port = parsed.port
    except (TypeError, ValueError):
        return None
    host = parsed.hostname.lower()
    if ":" in host:
        host = f"[{host}]"
    default_port = 80 if parsed.scheme.lower() == "http" else 443
    port_text = "" if port in (None, default_port) else f":{port}"
    return f"{parsed.scheme.lower()}://{host}{port_text}"


def _default_allowed_origins() -> set[str]:
    hosts = {"localhost", socket.gethostname().lower(), *LOCAL_ADDRESSES}
    origins: set[str] = set()
    for host in hosts:
        url_host = f"[{host}]" if ":" in host else host
        for scheme in ("http", "https"):
            origin = _canonical_origin(f"{scheme}://{url_host}:3000")
            if origin:
                origins.add(origin)
    return origins


def _configured_allowed_origins() -> set[str]:
    configured = os.getenv("BEMS_ALLOWED_ORIGINS", "").strip()
    origins = _default_allowed_origins()
    if not configured:
        return origins
    candidates = configured.split(",")
    configured_origins = {
        normalized
        for candidate in candidates
        if (normalized := _canonical_origin(str(candidate))) is not None
    }
    configured_count = len([item for item in candidates if str(item).strip()])
    if len(configured_origins) < configured_count:
        logger.warning("Ignored invalid BEMS_ALLOWED_ORIGINS entries.")
    return origins | configured_origins


ALLOWED_ORIGINS = _configured_allowed_origins()


# ── 엑셀 → DB 자동 동기화 스케줄러 ──────────────────────────────
# legacy에서는 Streamlit rerun마다 동기화가 실행됐다. 독립 운용에서는 이
# 프로세스가 주기적으로 mtime을 비교해(미변경 시 수 ms) 직접 동기화한다.
# BEMS_SYNC_INTERVAL_SECONDS=0 으로 끌 수 있다(테스트·개발용).
SYNC_INTERVAL_SECONDS = int(os.getenv("BEMS_SYNC_INTERVAL_SECONDS", "120"))
_sync_guard = threading.Lock()
_scheduler_state: dict[str, Any] = {"lastRunAt": None, "lastError": None}


def run_excel_sync(force: bool = False) -> dict[str, Any]:
    """에너지·생산실적 엑셀 동기화 1회 실행 (수동·스케줄 공용, 동시 실행 방지)."""
    with _sync_guard:
        energy_service = import_core("app.services.daily_energy_sync_service")
        production_service = import_core("app.services.production_dw_sync_service")
        energy = energy_service.force_resync() if force else energy_service.auto_sync_once()
        production = production_service.auto_sync_production_once(force=force)
        _scheduler_state["lastRunAt"] = datetime.now()
        _scheduler_state["lastError"] = None
        return {"energy": energy, "production": production}


async def _sync_scheduler(stop_event: asyncio.Event) -> None:
    while not stop_event.is_set():
        try:
            await asyncio.to_thread(run_excel_sync)
        except Exception as exc:
            _scheduler_state["lastError"] = str(exc)
            logger.error("Scheduled excel sync failed.", exc_info=(type(exc), exc, exc.__traceback__))
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=SYNC_INTERVAL_SECONDS)
        except asyncio.TimeoutError:
            continue


@asynccontextmanager
async def _lifespan(_: FastAPI):
    stop_event = asyncio.Event()
    task = (
        asyncio.create_task(_sync_scheduler(stop_event))
        if SYNC_INTERVAL_SECONDS > 0
        else None
    )
    yield
    if task is not None:
        stop_event.set()
        await task


app = FastAPI(title="AI Elite BEMS API", version="1.3.0", docs_url="/api/docs", redoc_url=None, lifespan=_lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=sorted(ALLOWED_ORIGINS),
    allow_credentials=False,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "Accept"],
)


@app.middleware("http")
async def reject_untrusted_unsafe_origins(request: Request, call_next):
    """Reject cross-origin state changes before they reach an API handler."""
    method = request.method.upper()
    preflight_method = request.headers.get("access-control-request-method", "").upper()
    unsafe_methods = {"POST", "PUT", "PATCH", "DELETE"}
    is_unsafe = method in unsafe_methods
    is_unsafe_preflight = method == "OPTIONS" and preflight_method in unsafe_methods
    origin = request.headers.get("origin")
    if (is_unsafe or is_unsafe_preflight) and origin:
        if _canonical_origin(origin) not in ALLOWED_ORIGINS:
            return JSONResponse(status_code=403, content={"detail": "허용되지 않은 Origin입니다."})
    return await call_next(request)


FACTORY_MEMBERS = {
    "전사": [],
    "전체": [],
    "남양주": ["남양주1", "남양주2"],
}
PHYSICAL_FACTORIES = ["남양주1", "남양주2", "김해", "광주", "논산", "경산"]
DISPLAY_FACTORIES = ["남양주", "김해", "광주", "논산", "경산"]

# v5.3 모델은 경산(F50, 2026-07 신규) 학습 데이터가 없다. 예측 실행과
# 집계 완전성 판정은 학습된 공장만 대상으로 하고 경산은 제외한다.
PREDICTION_FACTORIES = ["남양주1", "남양주2", "김해", "광주", "논산"]

PRODUCTION_FACTORY_CODES: dict[str, tuple[str, ...]] = {
    "전사": ("F10", "F10A", "F10B", "F20", "F30", "F40", "F50"),
    "전체": ("F10", "F10A", "F10B", "F20", "F30", "F40", "F50"),
    "남양주": ("F10", "F10A", "F10B"),
    "남양주1": ("F10A", "F10"),
    "남양주2": ("F10B", "F10"),
    "김해": ("F20",),
    "광주": ("F30",),
    "논산": ("F40",),
    "경산": ("F50",),
}

# Historical F10 rows predate the split into F10A/F10B. Assign the parent row
# once so company/Namyangju totals remain correct without double counting.
OPERATIONAL_PRODUCTION_FACTORY_BY_CODE = {
    "F10": "남양주1",
    "F10A": "남양주1",
    "F10B": "남양주2",
    "F20": "김해",
    "F30": "광주",
    "F40": "논산",
    "F50": "경산",
}

# 원단위 지표 → energy_daily 사용량 컬럼 / savings_target.metric 키
INTENSITY_METRICS: dict[str, dict[str, str]] = {
    "power": {"column": "total_power_kwh", "target": "power_per_ton", "unit": "kWh/ton"},
    "fuel": {"column": "fuel_nm3", "target": "fuel_per_ton", "unit": "Nm³/ton"},
    "water": {"column": "water_ton", "target": "water_per_ton", "unit": "ton/ton"},
}


def viewer_credentials() -> tuple[str, str]:
    user = os.getenv("DB_VIEWER_USER", "").strip()
    password = os.getenv("DB_VIEWER_PASSWORD", "")
    if not user or not password.strip():
        logger.error("DB viewer credentials are not configured.")
        raise HTTPException(status_code=503, detail="데이터베이스에 연결할 수 없습니다.")
    return user, password


def db_connect() -> pymysql.Connection:
    user, password = viewer_credentials()
    try:
        return pymysql.connect(
            host=os.getenv("DB_HOST", "127.0.0.1"),
            port=int(os.getenv("DB_PORT", "3306")),
            # Direct bridge queries are read-only. Privileged writes remain delegated
            # to legacy services after require_admin() authorization.
            user=user,
            password=password,
            database=os.getenv("DB_NAME", "fems_db"),
            charset="utf8mb4",
            cursorclass=pymysql.cursors.DictCursor,
            autocommit=True,
            connect_timeout=5,
            read_timeout=30,
        )
    except Exception as exc:  # pragma: no cover - depends on local MySQL
        logger.error("MySQL connection failed.", exc_info=(type(exc), exc, exc.__traceback__))
        raise HTTPException(status_code=503, detail="데이터베이스에 연결할 수 없습니다.") from exc


def fetch_all(sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    connection = db_connect()
    try:
        with connection.cursor() as cursor:
            cursor.execute(sql, params)
            return list(cursor.fetchall())
    finally:
        connection.close()


def fetch_one(sql: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None:
    rows = fetch_all(sql, params)
    return rows[0] if rows else None


def scalar(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
        return number if math.isfinite(number) else default
    except (TypeError, ValueError):
        return default


def optional_scalar(value: Any) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def normalize_date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if value is None:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None


def bounded_base_date(requested: date | None, maximum: Any) -> date:
    max_date = normalize_date(maximum)
    if max_date is None:
        return requested or date.today()
    return min(requested or max_date, max_date)


def previous_year_date(value: date) -> date:
    """Return the same prior-year date, clamping Feb 29 to Feb 28."""
    last_day = calendar.monthrange(value.year - 1, value.month)[1]
    return date(value.year - 1, value.month, min(value.day, last_day))


def is_complete_month_span(date_from: date, date_to: date) -> bool:
    """True when the period starts on day 1 and ends on a month's last day.

    legacy production_modes.is_complete_month_span과 동일 규칙 — 계획 대비
    지표는 완전한 월들로 구성된 범위에서만 의미가 있다.
    """
    if date_from > date_to:
        return False
    return (
        date_from.day == 1
        and date_to.day == calendar.monthrange(date_to.year, date_to.month)[1]
    )


PRODUCTION_MODES = {"month", "range", "year"}
PRODUCTION_RANGE_MAX_DAYS = 1100


def resolve_production_period(
    mode: str,
    base: date,
    date_from: date | None,
    date_to: date | None,
) -> tuple[date, date]:
    """조회 모드별 기간 확정 (월별/기간별/연간). 잘못된 범위는 400."""
    if mode == "month":
        return base.replace(day=1), base
    if mode == "year":
        return date(base.year, 1, 1), date(base.year, 12, 31)
    # range 모드 — 기본값은 기준일 포함 최근 31일
    resolved_to = date_to or base
    resolved_from = date_from or (resolved_to - timedelta(days=30))
    if resolved_from > resolved_to:
        raise HTTPException(status_code=400, detail="시작일은 종료일보다 늦을 수 없습니다.")
    if (resolved_to - resolved_from).days > PRODUCTION_RANGE_MAX_DAYS:
        raise HTTPException(
            status_code=400,
            detail=f"조회 기간은 최대 {PRODUCTION_RANGE_MAX_DAYS}일까지 지정할 수 있습니다.",
        )
    return resolved_from, resolved_to


ENERGY_RANGE_MAX_DAYS = 731
ENERGY_YOY_METRICS = ("power", "fuel", "water", "wastewater")


def resolve_energy_window(base: date, date_from: date | None, date_to: date | None) -> tuple[date, date]:
    """에너지 사용량 일별 조회 구간 확정 — 기간 미지정 시 기준일 역산 30일."""
    if date_from is None and date_to is None:
        return base - timedelta(days=29), base
    if date_from is None or date_to is None:
        raise HTTPException(status_code=400, detail="date_from과 date_to는 함께 지정해야 합니다.")
    if date_from > date_to:
        raise HTTPException(status_code=400, detail="시작일은 종료일보다 늦을 수 없습니다.")
    if (date_to - date_from).days > ENERGY_RANGE_MAX_DAYS:
        raise HTTPException(
            status_code=400,
            detail=f"조회 기간은 최대 {ENERGY_RANGE_MAX_DAYS}일까지 지정할 수 있습니다.",
        )
    return date_from, date_to


def build_energy_yoy(rows: list[dict[str, Any]], year: int) -> list[dict[str, Any]]:
    """금년 vs 전년 월별 사용량 비교 12행 구성.

    legacy 사용량 통합의 '전년대비 분석'과 동일하게 1~12월 자리를 모두 만들고,
    데이터가 없는 월은 None으로 남긴다(누계 계산에서 제외 가능하도록).
    """
    by_key: dict[tuple[int, int], dict[str, Any]] = {}
    for row in rows:
        by_key[(int(row["y"]), int(row["m"]))] = row
    result: list[dict[str, Any]] = []
    for month in range(1, 13):
        entry: dict[str, Any] = {"month": f"{month}월"}
        current = by_key.get((year, month))
        previous = by_key.get((year - 1, month))
        for metric in ENERGY_YOY_METRICS:
            current_value = optional_scalar(current.get(metric)) if current else None
            previous_value = optional_scalar(previous.get(metric)) if previous else None
            entry[metric] = {
                "current": round(current_value, 2) if current_value is not None else None,
                "previous": round(previous_value, 2) if previous_value is not None else None,
            }
        result.append(entry)
    return result


def annual_elapsed_ratio(year: int, as_of: date) -> float:
    """연간 모드 경과율 (0.0~1.0). 연말 착지 예상·기대 누계 계산에 사용."""
    year_start = date(year, 1, 1)
    year_end = date(year, 12, 31)
    if as_of < year_start:
        return 0.0
    if as_of >= year_end:
        return 1.0
    total_days = (year_end - year_start).days + 1
    return ((as_of - year_start).days + 1) / total_days


def physical_factory_members(factory: str) -> tuple[str, ...]:
    if factory in ("전사", "전체"):
        return tuple(PHYSICAL_FACTORIES)
    members = FACTORY_MEMBERS.get(factory)
    return tuple(members) if members else (factory,)


def prediction_factory_members(factory: str) -> tuple[str, ...]:
    """예측 집계용 구성 공장 — 모델 미학습 공장(경산)은 완전성 판정에서 제외."""
    members = tuple(
        member for member in physical_factory_members(factory)
        if member in PREDICTION_FACTORIES
    )
    return members or physical_factory_members(factory)


def json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(item) for item in value]
    if hasattr(value, "to_dict"):
        try:
            return json_safe(value.to_dict(orient="records"))
        except TypeError:
            return json_safe(value.to_dict())
    if hasattr(value, "item"):
        return json_safe(value.item())
    return str(value)


def factory_clause(factory: str, column: str = "factory") -> tuple[str, list[Any]]:
    members = FACTORY_MEMBERS.get(factory)
    if members == []:
        return "", []
    targets = members if members is not None else [factory]
    placeholders = ",".join(["%s"] * len(targets))
    return f" AND {column} IN ({placeholders})", list(targets)


def local_addresses() -> set[str]:
    return set(LOCAL_ADDRESSES)


def client_is_admin(request: Request) -> bool:
    client_ip = request.client.host if request.client else ""
    explicit = {item.strip() for item in os.getenv("BEMS_ADMIN_IPS", "").split(",") if item.strip()}
    return client_ip in local_addresses() or client_ip in explicit


def require_admin(request: Request) -> None:
    if not client_is_admin(request):
        raise HTTPException(status_code=403, detail="호스트 PC 관리자 전용 기능입니다.")


def import_core(module: str):
    """`new/backend/app/` 로컬 복사본에서 코어 모듈을 로드한다 (legacy 미참조)."""
    if not (LOCAL_CORE_ROOT / "app" / "services").exists():
        logger.error("Local BEMS core copy is missing: %s", LOCAL_CORE_ROOT / "app")
        raise HTTPException(status_code=503, detail="BEMS 코어 모듈을 사용할 수 없습니다.")
    root_text = str(LOCAL_CORE_ROOT)
    if root_text not in sys.path:
        sys.path.insert(0, root_text)
    try:
        return importlib.import_module(module)
    except Exception as exc:
        logger.error("Failed to load BEMS core module %s.", module, exc_info=(type(exc), exc, exc.__traceback__))
        raise HTTPException(status_code=503, detail="BEMS 코어 모듈을 사용할 수 없습니다.") from exc


def _table_records(frame: Any) -> list[dict[str, Any]]:
    if frame is None:
        return []
    if hasattr(frame, "to_dict"):
        try:
            raw_rows = frame.to_dict(orient="records")
        except TypeError:
            raw_rows = frame.to_dict()
    else:
        raw_rows = frame
    if isinstance(raw_rows, dict):
        raw_rows = [raw_rows]
    return [row for row in (raw_rows or []) if isinstance(row, dict)]


def fetch_actual_production_frame(date_from: date, date_to: date) -> list[dict[str, Any]]:
    """Load operational production through the viewer DB account plus legacy WIP rules."""
    service = import_core("app.services.production_actual_service")
    try:
        gwangju_conversion = getattr(service, "WIP_MIX_CONVERSION", {}).get("광주", {})
        wip_codes = tuple(str(code) for code in gwangju_conversion)
        factory_codes = tuple(OPERATIONAL_PRODUCTION_FACTORY_BY_CODE)
        factory_placeholders = ",".join(["%s"] * len(factory_codes))
        if wip_codes:
            wip_placeholders = ",".join(["%s"] * len(wip_codes))
            quantity_expression = (
                f"CASE WHEN factory = %s AND item_code IN ({wip_placeholders}) "
                "THEN 0 ELSE actual_qty END"
            )
            leading_params: tuple[Any, ...] = ("F30", *wip_codes)
        else:
            quantity_expression = "actual_qty"
            leading_params = ()
        rows = fetch_all(
            f"""
            SELECT date, factory, SUM({quantity_expression}) actual_prod_kg
            FROM production_daily
            WHERE date BETWEEN %s AND %s
              AND factory IN ({factory_placeholders})
            GROUP BY date, factory
            ORDER BY date, factory
            """,
            (*leading_params, date_from, date_to, *factory_codes),
        )

        split_dates = {
            row_date
            for row in rows
            if str(row.get("factory")) in {"F10A", "F10B"}
            and (row_date := normalize_date(row.get("date"))) is not None
        }
        records: list[dict[str, Any]] = []
        for row in rows:
            row_date = normalize_date(row.get("date"))
            code = str(row.get("factory"))
            production = optional_scalar(row.get("actual_prod_kg"))
            if row_date is None or production is None:
                continue
            if code == "F10" and row_date in split_dates:
                continue
            factory = OPERATIONAL_PRODUCTION_FACTORY_BY_CODE.get(code)
            if factory:
                records.append({"date": row_date, "factory": factory, "actual_prod_kg": production})

        wip_frame = service.get_wip_daily("광주")
        for row in _table_records(wip_frame):
            row_date = normalize_date(row.get("date"))
            production = optional_scalar(row.get("total_wip_kg"))
            if row_date is None or production is None or not date_from <= row_date <= date_to:
                continue
            records.append({"date": row_date, "factory": "광주", "actual_prod_kg": production})
        return records
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Failed to load operational production.", exc_info=(type(exc), exc, exc.__traceback__))
        raise HTTPException(status_code=503, detail="운영 생산실적을 불러올 수 없습니다.") from exc


def actual_production_records(frame: Any) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for row in _table_records(frame):
        row_date = normalize_date(row.get("date"))
        factory = row.get("factory")
        production = optional_scalar(row.get("actual_prod_kg"))
        if row_date is None or factory is None or production is None:
            continue
        records.append({"date": row_date, "factory": str(factory), "actual_prod_kg": production})
    return records


def actual_production_kg(
    records: list[dict[str, Any]],
    factory: str,
    date_from: date,
    date_to: date,
) -> float:
    members = set(physical_factory_members(factory))
    return sum(
        scalar(row.get("actual_prod_kg"))
        for row in records
        if row.get("factory") in members and date_from <= row["date"] <= date_to
    )


def actual_production_daily_kg(
    records: list[dict[str, Any]],
    factory: str,
    date_from: date,
    date_to: date,
) -> dict[date, float]:
    members = set(physical_factory_members(factory))
    daily: dict[date, float] = {}
    for row in records:
        row_date = row["date"]
        if row.get("factory") not in members or not date_from <= row_date <= date_to:
            continue
        daily[row_date] = daily.get(row_date, 0.0) + scalar(row.get("actual_prod_kg"))
    return daily


@app.exception_handler(Exception)
async def unhandled_error(_: Request, exc: Exception):
    logger.error("Unhandled API exception.", exc_info=(type(exc), exc, exc.__traceback__))
    return JSONResponse(status_code=500, content={"detail": "내부 서버 오류가 발생했습니다."})


@app.get("/api/v1/health")
def health() -> dict[str, Any]:
    row = fetch_one("SELECT MAX(updated_at) AS updated_at, COUNT(*) AS records FROM energy_daily")
    return {"status": "ok", "database": "mysql", **json_safe(row or {})}


@app.get("/api/v1/session")
def session(request: Request) -> dict[str, str]:
    client_ip = request.client.host if request.client else "unknown"
    return {
        "role": "admin" if client_is_admin(request) else "viewer",
        "clientIp": client_ip,
        "serverName": socket.gethostname(),
    }


def aggregate_period(
    factory: str,
    date_from: date,
    date_to: date,
    *,
    actual_records: list[dict[str, Any]] | None = None,
) -> dict[str, float]:
    clause, values = factory_clause(factory)
    row = fetch_one(
        """
        SELECT COALESCE(SUM(total_power_kwh),0) power,
               COALESCE(SUM(fuel_nm3),0) fuel,
               COALESCE(SUM(water_ton),0) water
        FROM energy_daily WHERE date BETWEEN %s AND %s
        """ + clause,
        (date_from, date_to, *values),
    ) or {}
    if actual_records is None:
        frame = fetch_actual_production_frame(date_from, date_to)
        actual_records = actual_production_records(frame)
    totals = {key: scalar(row.get(key)) for key in ("power", "fuel", "water")}
    totals["production"] = actual_production_kg(actual_records, factory, date_from, date_to)
    return totals


def rate_change(current: float, previous: float) -> float:
    return round((current / previous - 1) * 100, 1) if previous else 0.0


@app.get("/api/v1/dashboard")
def dashboard(factory: str = "전사", requested_date: date | None = Query(None, alias="date")) -> dict[str, Any]:
    max_row = fetch_one("SELECT MAX(date) max_date, MAX(updated_at) updated_at FROM energy_daily") or {}
    max_date = normalize_date(max_row.get("max_date"))
    base = min(requested_date or date.today(), max_date) if max_date else (requested_date or date.today())
    month_start = base.replace(day=1)
    prev_base = previous_year_date(base)
    prev_start = prev_base.replace(day=1)
    actual_frame = fetch_actual_production_frame(date(base.year - 1, 1, 1), base)
    actual_records = actual_production_records(actual_frame)
    current = aggregate_period(factory, month_start, base, actual_records=actual_records)
    previous = aggregate_period(factory, prev_start, prev_base, actual_records=actual_records)
    prod_ton = current["production"] / 1000
    prev_prod_ton = previous["production"] / 1000

    def intensity(values: dict[str, float], key: str, tonnes: float) -> float | None:
        return values[key] / tonnes if tonnes > 0 else None

    metrics = []
    metric_specs = [
        ("power", "전력 원단위", "kWh/ton", "blue"),
        ("fuel", "연료 원단위", "Nm³/ton", "violet"),
        ("water", "용수 원단위", "ton/ton", "cyan"),
    ]
    for key, label, unit, tone in metric_specs:
        value = intensity(current, key, prod_ton)
        prev_value = intensity(previous, key, prev_prod_ton)
        metrics.append({
            "id": key,
            "label": label,
            "value": round(value, 2) if value is not None else None,
            "unit": unit,
            "change": rate_change(value, prev_value) if value is not None and prev_value else None,
            "tone": tone,
        })
    metrics.append({"id": "production", "label": "누계 생산량", "value": round(prod_ton, 1), "unit": "ton", "change": rate_change(prod_ton, prev_prod_ton), "tone": "emerald"})

    clause, values = factory_clause(factory, "e.factory")
    trend_rows = fetch_all(
        """
        SELECT e.date, SUM(e.total_power_kwh)/1000 actual,
               SUM(e.fuel_nm3) fuel, SUM(e.water_ton) water, SUM(e.wastewater_ton) wastewater
        FROM energy_daily e WHERE e.date BETWEEN %s AND %s
        """ + clause + " GROUP BY e.date ORDER BY e.date",
        (base - timedelta(days=6), base, *values),
    )
    pred_rows = aggregate_prediction_rows(
        factory,
        base,
        date_from=base - timedelta(days=6),
        target="전력",
        limit=7,
    )
    pred_map = {normalize_date(row["pred_date"]): row for row in pred_rows}
    trend_production = actual_production_daily_kg(
        actual_records, factory, base - timedelta(days=6), base,
    )
    trend = []
    for row in trend_rows:
        row_date = normalize_date(row.get("date"))
        if row_date is None:
            continue
        pred = pred_map.get(row_date, {})
        actual = scalar(row.get("actual"))
        predicted = optional_scalar(pred.get("predicted"))
        lower = optional_scalar(pred.get("lower_band"))
        upper = optional_scalar(pred.get("upper_band"))
        trend.append({
            "date": row_date.strftime("%m.%d"),
            "actual": round(actual, 2),
            "predicted": round(predicted, 2) if predicted is not None else None,
            "lower": round(lower, 2) if lower is not None else None,
            "upper": round(upper, 2) if upper is not None else None,
            "production": round(trend_production.get(row_date, 0.0) / 1000, 1),
            "fuel": round(scalar(row.get("fuel")), 1),
            "water": round(scalar(row.get("water")), 1),
            "wastewater": round(scalar(row.get("wastewater")), 1),
        })

    yoy_clause, yoy_values = factory_clause(factory)
    yoy_rows = fetch_all(
        """
        SELECT YEAR(date) y, MONTH(date) m, SUM(total_power_kwh) power
        FROM energy_daily WHERE date BETWEEN %s AND %s
        """ + yoy_clause + " GROUP BY y,m ORDER BY y,m",
        (date(base.year - 1, 1, 1), base, *yoy_values),
    )
    monthly_production: dict[tuple[int, int], float] = {}
    for production_date, production_kg in actual_production_daily_kg(
        actual_records, factory, date(base.year - 1, 1, 1), base,
    ).items():
        key = (production_date.year, production_date.month)
        monthly_production[key] = monthly_production.get(key, 0.0) + production_kg
    yoy_map: dict[tuple[int, int], float] = {}
    for row in yoy_rows:
        key = (int(row["y"]), int(row["m"]))
        prod_ton_month = monthly_production.get(key, 0.0) / 1000
        if prod_ton_month > 0:
            yoy_map[key] = scalar(row.get("power")) / prod_ton_month
    yoy = []
    for month in range(max(1, base.month - 5), base.month + 1):
        current_yoy = yoy_map.get((base.year, month))
        previous_yoy = yoy_map.get((base.year - 1, month))
        yoy.append({
            "month": f"{month}월",
            "current": round(current_yoy, 1) if current_yoy is not None else None,
            "previous": round(previous_yoy, 1) if previous_yoy is not None else None,
        })

    comparisons = []
    for display_factory in DISPLAY_FACTORIES:
        current_factory = aggregate_period(display_factory, month_start, base, actual_records=actual_records)
        previous_factory = aggregate_period(display_factory, prev_start, prev_base, actual_records=actual_records)
        cur_ton = current_factory["production"] / 1000
        prv_ton = previous_factory["production"] / 1000
        cur_value = intensity(current_factory, "power", cur_ton)
        prv_value = intensity(previous_factory, "power", prv_ton)
        if cur_value is not None:
            comparisons.append({
                "factory": display_factory,
                "value": round(cur_value, 1),
                "change": rate_change(cur_value, prv_value) if prv_value else None,
            })

    event_clause, event_values = factory_clause(factory)
    events = fetch_all(
        "SELECT id, event_date, factory, target, tag, severity, note FROM event_annotation WHERE 1=1" + event_clause + " ORDER BY event_date DESC, id DESC LIMIT 5",
        tuple(event_values),
    )
    alert_clause, alert_values = factory_clause(factory)
    alert_row = fetch_one(
        "SELECT COUNT(*) count FROM prediction_log WHERE pred_date BETWEEN %s AND %s AND band_status IN ('over','under')" + alert_clause,
        (base - timedelta(days=6), base, *alert_values),
    ) or {"count": 0}
    alert_count = int(alert_row.get("count") or 0)
    return json_safe({
        "baseDate": base.isoformat(),
        "factory": factory,
        "updatedAt": max_row.get("updated_at") or datetime.now(),
        "alert": {
            "level": "warning" if alert_count else "normal",
            "title": f"AI 정상범주 이탈 {alert_count}건" if alert_count else "AI 이상 신호 없음",
            "description": "최근 7일 예측 밴드 기준입니다. 상세 원인은 AI 예측 화면에서 확인하세요.",
            "count": alert_count,
        },
        "metrics": metrics,
        "trend": trend,
        "yoy": yoy,
        "factoryComparison": comparisons,
        "events": [{**row, "date": row["event_date"].strftime("%m.%d")} for row in events],
    })


@app.get("/api/v1/energy")
def energy(
    factory: str = "전사",
    requested_date: date | None = Query(None, alias="date"),
    date_from: date | None = Query(None),
    date_to: date | None = Query(None),
) -> dict[str, Any]:
    max_row = fetch_one("SELECT MAX(date) max_date FROM energy_daily") or {}
    base = bounded_base_date(requested_date, max_row.get("max_date"))
    window_from, window_to = resolve_energy_window(base, date_from, date_to)
    # 설비 구성·공장별 집계 범위: 기간 지정 시 그 기간, 기본은 기준월 1일~기준일(기존 동작).
    ranged = date_from is not None
    summary_from = window_from if ranged else base.replace(day=1)
    summary_to = window_to if ranged else base
    clause, values = factory_clause(factory)
    rows = fetch_all(
        """
        SELECT date, SUM(total_power_kwh)/1000 power, SUM(fuel_nm3)/1000 fuel,
               SUM(water_ton)/1000 water, SUM(wastewater_ton)/1000 wastewater
        FROM energy_daily WHERE date BETWEEN %s AND %s
        """ + clause + " GROUP BY date ORDER BY date",
        (window_from, window_to, *values),
    )
    equipment = fetch_one(
        """
        SELECT SUM(freezing_power_kwh) freezing, SUM(air_compressor_kwh) compressor,
               SUM(total_power_kwh) total_power
        FROM energy_daily WHERE date BETWEEN %s AND %s
        """ + clause,
        (summary_from, summary_to, *values),
    ) or {}
    total = scalar(equipment.get("total_power"), 1) or 1
    freezing = scalar(equipment.get("freezing"))
    compressor = scalar(equipment.get("compressor"))
    production = max(0.0, total - freezing - compressor)
    equipment_rows = [
        {"name": "냉동", "value": round(freezing / total * 100, 1)},
        {"name": "공압", "value": round(compressor / total * 100, 1)},
        {"name": "생산설비·기타", "value": round(production / total * 100, 1)},
    ]
    factory_rows = fetch_all(
        """
        SELECT factory, SUM(total_power_kwh)/1000 power, SUM(fuel_nm3)/1000 fuel,
               SUM(water_ton)/1000 water, SUM(wastewater_ton)/1000 wastewater
        FROM energy_daily WHERE date BETWEEN %s AND %s GROUP BY factory ORDER BY factory
        """,
        (summary_from, summary_to),
    )
    combined: dict[str, dict[str, float | str]] = {}
    for row in factory_rows:
        name = "남양주" if row["factory"] in ("남양주1", "남양주2") else row["factory"]
        target = combined.setdefault(name, {"factory": name, "power": 0.0, "fuel": 0.0, "water": 0.0, "wastewater": 0.0})
        for key in ("power", "fuel", "water", "wastewater"):
            target[key] = scalar(target[key]) + scalar(row.get(key))
    yoy_rows = fetch_all(
        """
        SELECT YEAR(date) y, MONTH(date) m,
               SUM(total_power_kwh)/1000 power, SUM(fuel_nm3)/1000 fuel,
               SUM(water_ton)/1000 water, SUM(wastewater_ton)/1000 wastewater
        FROM energy_daily WHERE date BETWEEN %s AND %s
        """ + clause + " GROUP BY y, m ORDER BY y, m",
        (date(base.year - 1, 1, 1), date(base.year, 12, 31), *values),
    )
    return json_safe({
        "baseDate": base,
        "mode": "range" if ranged else "recent",
        "dateFrom": window_from,
        "dateTo": window_to,
        "daily": [{**row, "date": row["date"].strftime("%m.%d")} for row in rows],
        "equipment": equipment_rows,
        "factories": list(combined.values()),
        "yoyYear": base.year,
        "yoy": build_energy_yoy(yoy_rows, base.year),
    })


@app.get("/api/v1/intensity")
def intensity_analysis(
    factory: str = "전사",
    metric: str = "power",
    requested_date: date | None = Query(None, alias="date"),
) -> dict[str, Any]:
    """원단위 분석: 월별 금년/전년/목표 추이 + MTD/YTD 요약 + 공장 매트릭스."""
    spec = INTENSITY_METRICS.get(metric)
    if spec is None:
        raise HTTPException(status_code=400, detail=f"지원하지 않는 지표입니다: {metric}")
    usage_col = spec["column"]

    max_row = fetch_one("SELECT MAX(date) max_date FROM energy_daily") or {}
    base = bounded_base_date(requested_date, max_row.get("max_date"))
    history_start = date(base.year - 1, 1, 1)
    actual_frame = fetch_actual_production_frame(history_start, base)
    actual_records = actual_production_records(actual_frame)

    clause, values = factory_clause(factory)
    monthly_rows = fetch_all(
        f"""
        SELECT YEAR(date) y, MONTH(date) m,
               SUM({usage_col}) usage_sum
        FROM energy_daily WHERE date BETWEEN %s AND %s
        """ + clause + " GROUP BY y,m ORDER BY y,m",
        (history_start, base, *values),
    )
    monthly_production: dict[tuple[int, int], float] = {}
    for production_date, production_kg in actual_production_daily_kg(
        actual_records, factory, history_start, base,
    ).items():
        key = (production_date.year, production_date.month)
        monthly_production[key] = monthly_production.get(key, 0.0) + production_kg
    monthly_map: dict[tuple[int, int], float] = {}
    for row in monthly_rows:
        key = (int(row["y"]), int(row["m"]))
        prod_ton = monthly_production.get(key, 0.0) / 1000
        if prod_ton > 0:
            monthly_map[key] = scalar(row.get("usage_sum")) / prod_ton

    target_factory = "ALL" if factory in ("전사", "전체") else factory
    target_row = fetch_one(
        "SELECT target_pct FROM savings_target WHERE factory=%s AND year=%s AND metric=%s",
        (target_factory, base.year, spec["target"]),
    )
    target_pct = optional_scalar(target_row.get("target_pct")) if target_row else None

    monthly = []
    for month in range(1, 13):
        current = monthly_map.get((base.year, month))
        previous = monthly_map.get((base.year - 1, month))
        target_value = previous * (1 - target_pct / 100) if (previous is not None and target_pct is not None) else None
        monthly.append({
            "month": f"{month}월",
            "current": round(current, 2) if current is not None else None,
            "previous": round(previous, 2) if previous is not None else None,
            "target": round(target_value, 2) if target_value is not None else None,
        })

    def period_intensity(f: str, date_from: date, date_to: date) -> float | None:
        totals = aggregate_period(f, date_from, date_to, actual_records=actual_records)
        prod_ton = totals["production"] / 1000
        key = {"total_power_kwh": "power", "fuel_nm3": "fuel", "water_ton": "water"}[usage_col]
        return totals[key] / prod_ton if prod_ton > 0 else None

    prev_base = previous_year_date(base)
    summary = {}
    for label, date_from, prev_from in (
        ("mtd", base.replace(day=1), prev_base.replace(day=1)),
        ("ytd", base.replace(month=1, day=1), prev_base.replace(month=1, day=1)),
    ):
        cur = period_intensity(factory, date_from, base)
        prv = period_intensity(factory, prev_from, prev_base)
        summary[label] = {
            "current": round(cur, 2) if cur is not None else None,
            "previous": round(prv, 2) if prv is not None else None,
            "change": rate_change(cur, prv) if (cur is not None and prv) else None,
        }

    matrix = []
    for display_factory in DISPLAY_FACTORIES:
        cur = period_intensity(display_factory, base.replace(day=1), base)
        prv = period_intensity(display_factory, prev_base.replace(day=1), prev_base)
        if cur is None:
            continue
        matrix.append({
            "factory": display_factory,
            "current": round(cur, 2),
            "previous": round(prv, 2) if prv is not None else None,
            "change": rate_change(cur, prv) if prv else None,
        })

    return json_safe({
        "baseDate": base,
        "metric": metric,
        "unit": spec["unit"],
        "year": base.year,
        "targetPct": target_pct,
        "summary": summary,
        "monthly": monthly,
        "matrix": matrix,
    })


@app.get("/api/v1/production")
def production(
    factory: str = "전사",
    requested_date: date | None = Query(None, alias="date"),
    mode: str = "month",
    date_from: date | None = Query(None),
    date_to: date | None = Query(None),
) -> dict[str, Any]:
    """생산실적 분석 — 월별(기본)·기간별·연간 조회 모드 (legacy 동등 기능)."""
    if mode not in PRODUCTION_MODES:
        raise HTTPException(status_code=400, detail=f"지원하지 않는 조회 모드입니다: {mode}")
    if mode == "range" and date_from is not None and date_to is not None:
        # 명시적 기간은 DB 조회 전에 검증한다 (잘못된 입력에 400을 우선 반환).
        resolve_production_period(mode, date_to, date_from, date_to)
    max_row = fetch_one("SELECT MAX(date) max_date FROM production_daily") or {}
    base = bounded_base_date(requested_date, max_row.get("max_date"))
    period_from, period_to = resolve_production_period(mode, base, date_from, date_to)
    # 계획 대비 지표는 완전한 월들로 구성된 기간에서만 유효 (legacy 규칙)
    plan_allowed = mode != "range" or is_complete_month_span(period_from, period_to)
    codes = PRODUCTION_FACTORY_CODES.get(factory, (factory,))
    placeholders = ",".join(["%s"] * len(codes))
    clause = f" AND factory IN ({placeholders})"
    values = list(codes)
    summary = fetch_one(
        """
        SELECT SUM(actual_qty)/1000 actual, COUNT(DISTINCT item_code) items
        FROM production_daily WHERE date BETWEEN %s AND %s
        """ + clause,
        (period_from, period_to, *values),
    ) or {}
    # 다중 월 기간에서도 계획이 정확하도록 (공장·품목·연·월) 단위 MAX 후 합산
    plan_row = fetch_one(
        """
        SELECT SUM(planned_qty) plan FROM (
          SELECT factory,item_code,YEAR(date) y,MONTH(date) m,
                 MAX(planned_qty)/1000 planned_qty
          FROM production_daily WHERE date BETWEEN %s AND %s
        """ + clause + " GROUP BY factory,item_code,y,m) p",
        (period_from, period_to, *values),
    ) or {}
    actual = scalar(summary.get("actual"))
    plan = scalar(plan_row.get("plan"))

    cat2_select = """
          SUM(CASE WHEN category2='IC' THEN actual_qty ELSE 0 END)/1000 IC,
          SUM(CASE WHEN category2='MY' THEN actual_qty ELSE 0 END)/1000 MY,
          SUM(CASE WHEN category2='FM' THEN actual_qty ELSE 0 END)/1000 FM,
          SUM(CASE WHEN category2='SN' THEN actual_qty ELSE 0 END)/1000 SN,
          SUM(CASE WHEN category2 IS NULL OR category2 NOT IN ('IC','MY','FM','SN')
              THEN actual_qty ELSE 0 END)/1000 ETC
    """
    if mode == "year":
        daily = fetch_all(
            "SELECT MONTH(date) month_no," + cat2_select +
            " FROM production_daily WHERE date BETWEEN %s AND %s"
            + clause + " GROUP BY month_no ORDER BY month_no",
            (period_from, period_to, *values),
        )
    else:
        daily = fetch_all(
            "SELECT date," + cat2_select +
            " FROM production_daily WHERE date BETWEEN %s AND %s"
            + clause + " GROUP BY date ORDER BY date",
            (period_from, period_to, *values),
        )
    mix_rows = fetch_all(
        """
        SELECT COALESCE(category2,'기타') name, SUM(actual_qty) value FROM production_daily
        WHERE date BETWEEN %s AND %s
        """ + clause + " GROUP BY category2 ORDER BY value DESC",
        (period_from, period_to, *values),
    )
    mix_total = sum(scalar(row["value"]) for row in mix_rows) or 1
    top_rows = fetch_all(
        """
        SELECT item_name name, SUM(plan) plan, SUM(actual) actual
        FROM (
          SELECT factory, item_code, MAX(item_name) item_name,
                 MAX(planned_qty)/1000 plan, SUM(actual_qty)/1000 actual
          FROM production_daily WHERE date BETWEEN %s AND %s
        """ + clause + """
          GROUP BY factory,item_code,YEAR(date),MONTH(date)
        ) item_totals
        GROUP BY item_code,item_name ORDER BY actual DESC LIMIT 10
        """,
        (period_from, period_to, *values),
    )

    cat2_keys = ("IC", "MY", "FM", "SN", "ETC")
    daily_output = []
    burnup: list[dict[str, Any]] = []
    if mode == "year":
        monthly_map = {int(row["month_no"]): row for row in daily if row.get("month_no") is not None}
        for month in range(1, 13):
            row = monthly_map.get(month, {})
            daily_output.append({
                "date": f"{month}월",
                **{key: round(scalar(row.get(key)), 3) for key in cat2_keys},
            })
        # 연간 Burn-up — 월별 계획 누계 vs 실적 누계 (legacy _render_annual_burnup)
        monthly_plan_rows = fetch_all(
            """
            SELECT m, SUM(planned_qty) plan FROM (
              SELECT factory,item_code,MONTH(date) m,MAX(planned_qty)/1000 planned_qty
              FROM production_daily WHERE date BETWEEN %s AND %s
            """ + clause + " GROUP BY factory,item_code,m) p GROUP BY m ORDER BY m",
            (period_from, period_to, *values),
        )
        monthly_plan = {int(row["m"]): scalar(row.get("plan")) for row in monthly_plan_rows}
        # 실적선은 마지막 실적 월까지만 그린다 (미래 월로 평탄하게 이어지지 않도록 None)
        last_actual_month = max(monthly_map) if monthly_map else 0
        cum_plan = 0.0
        cum_actual = 0.0
        for month in range(1, 13):
            cum_plan += monthly_plan.get(month, 0.0)
            row = monthly_map.get(month, {})
            cum_actual += sum(scalar(row.get(key)) for key in cat2_keys)
            burnup.append({
                "month": f"{month}월",
                "cumPlan": round(cum_plan, 1),
                "cumActual": round(cum_actual, 1) if month <= last_actual_month else None,
            })
    else:
        window = daily if mode == "range" else daily[-14:]
        for row in window:
            row_date = normalize_date(row.get("date"))
            if row_date is None:
                continue
            daily_output.append({
                "date": row_date.strftime("%m.%d") if mode == "month" else row_date.isoformat(),
                **{key: round(scalar(row.get(key)), 3) for key in cat2_keys},
            })

    top_items = []
    for row in top_rows:
        item_plan = scalar(row.get("plan"))
        item_actual = scalar(row.get("actual"))
        top_items.append({
            "name": row.get("name"),
            "plan": round(item_plan, 3) if plan_allowed else None,
            "actual": round(item_actual, 3),
            "rate": round(item_actual / item_plan * 100, 1) if plan_allowed and item_plan > 0 else None,
        })

    # 진척률·페이스·착지 예상 — 모드별 경과율 기준
    if mode == "month":
        days_in_month = calendar.monthrange(base.year, base.month)[1]
        elapsed = base.day / days_in_month
    elif mode == "year":
        elapsed = annual_elapsed_ratio(base.year, base)
    else:
        elapsed = None
    forecast = actual / elapsed if elapsed else None
    period_days = (period_to - period_from).days + 1
    summary_output = {
        "plan": round(plan, 1) if plan_allowed else None,
        "actual": round(actual, 1),
        "progress": round(actual / plan * 100, 1) if plan_allowed and plan > 0 else None,
        "pace": (
            round((actual / plan / elapsed) * 100, 1)
            if plan_allowed and plan > 0 and elapsed else None
        ),
        "forecast": round(forecast, 1) if forecast is not None else None,
        "items": int(summary.get("items") or 0),
        "days": period_days,
    }
    return json_safe({
        "baseDate": base,
        "mode": mode,
        "dateFrom": period_from,
        "dateTo": period_to,
        "planAllowed": plan_allowed,
        "summary": summary_output,
        "daily": daily_output,
        "burnup": burnup,
        "mix": [{"name": row["name"], "value": round(scalar(row["value"]) / mix_total * 100, 1)} for row in mix_rows],
        "topItems": top_items,
    })


def aggregate_prediction_rows(
    factory: str,
    date_to: date,
    *,
    date_from: date | None = None,
    target: str | None = None,
    limit: int = 60,
) -> list[dict[str, Any]]:
    members = prediction_factory_members(factory)
    placeholders = ",".join(["%s"] * len(members))
    conditions = ["pred_date <= %s", f"factory IN ({placeholders})"]
    params: list[Any] = [date_to, *members]
    if date_from is not None:
        conditions.append("pred_date >= %s")
        params.append(date_from)
    if target is not None:
        conditions.append("target = %s")
        params.append(target)
    source_rows = fetch_all(
        """
        SELECT pred_date, target, factory, SUM(pred_value)/1000 predicted,
               SUM(pred_p05)/1000 lower_band, SUM(pred_p95)/1000 upper_band,
               SUM(actual_value)/1000 actual
        FROM prediction_log WHERE
        """ + " AND ".join(conditions) + " GROUP BY pred_date,target,factory ORDER BY pred_date DESC LIMIT 5000",
        tuple(params),
    )
    grouped: dict[tuple[date, str], dict[str, dict[str, Any]]] = {}
    for row in source_rows:
        pred_date = normalize_date(row.get("pred_date"))
        row_target = row.get("target")
        row_factory = row.get("factory")
        if pred_date is None or row_target is None or row_factory not in members:
            continue
        grouped.setdefault((pred_date, str(row_target)), {})[str(row_factory)] = row

    output: list[dict[str, Any]] = []
    for (pred_date, row_target), by_factory in grouped.items():
        prediction_complete = all(
            member in by_factory
            and optional_scalar(by_factory[member].get("predicted")) is not None
            for member in members
        )
        band_complete = all(
            member in by_factory
            and optional_scalar(by_factory[member].get("lower_band")) is not None
            and optional_scalar(by_factory[member].get("upper_band")) is not None
            for member in members
        )
        actual_complete = all(
            member in by_factory and optional_scalar(by_factory[member].get("actual")) is not None
            for member in members
        )
        predicted = (
            sum(scalar(by_factory[member].get("predicted")) for member in members)
            if prediction_complete else None
        )
        lower = (
            sum(scalar(by_factory[member].get("lower_band")) for member in members)
            if band_complete else None
        )
        upper = (
            sum(scalar(by_factory[member].get("upper_band")) for member in members)
            if band_complete else None
        )
        actual = (
            sum(scalar(by_factory[member].get("actual")) for member in members)
            if actual_complete else None
        )
        band_status = "unknown"
        if band_complete and actual_complete and actual is not None and lower is not None and upper is not None:
            band_status = "over" if actual > upper else "under" if actual < lower else "inside"
        output.append({
            "pred_date": pred_date,
            "target": row_target,
            "predicted": predicted,
            "lower_band": lower,
            "upper_band": upper,
            "actual": actual,
            "band_status": band_status,
        })
    output.sort(key=lambda row: (row["pred_date"], str(row["target"])), reverse=True)
    return output[:limit]


@app.get("/api/v1/predictions")
def predictions(
    factory: str = "전사",
    requested_date: date | None = Query(None, alias="date"),
) -> dict[str, Any]:
    max_row = fetch_one("SELECT MAX(pred_date) max_date FROM prediction_log") or {}
    base = bounded_base_date(requested_date, max_row.get("max_date"))
    rows = aggregate_prediction_rows(factory, base)
    status = {"normal": 0, "warning": 0, "alert": 0, "unknown": 0, "label": "정상"}
    for row in rows:
        if row["band_status"] == "inside":
            status["normal"] += 1
        elif row["band_status"] in ("over", "under"):
            status["alert"] += 1
        else:
            status["unknown"] += 1
    status["label"] = "주의" if status["alert"] else "미확정" if status["unknown"] else "정상"
    model = {"version": "v5.3", "trainedAt": "-", "mape": 0, "coverage": 0, "state": "운영 중"}
    try:
        service = import_core("app.services.usage_prediction_v5_service")
        registry = service.get_model_registry()
        artifact = registry.get("active_artifact") or {}
        model.update({
            "version": registry.get("active_model_key") or registry.get("active_version", "v5.3"),
            "trainedAt": artifact.get("created_at") or registry.get("updated_at", "-"),
        })
    except HTTPException:
        pass
    latest = []
    for row in rows[:12]:
        predicted = optional_scalar(row.get("predicted"))
        lower = optional_scalar(row.get("lower_band"))
        upper = optional_scalar(row.get("upper_band"))
        actual = optional_scalar(row.get("actual"))
        latest.append({
            "date": row["pred_date"],
            "target": row["target"],
            "predicted": round(predicted, 2) if predicted is not None else None,
            "lower": round(lower, 2) if lower is not None else None,
            "upper": round(upper, 2) if upper is not None else None,
            "actual": round(actual, 2) if actual is not None else None,
            "status": row["band_status"],
        })
    return json_safe({"baseDate": base, "status": status, "latest": latest, "model": model})


class PredictionRequest(BaseModel):
    factory: str
    date: date
    mix_prod_kg: float


def _format_prediction_results(pred_date: date, raw_results: dict[str, Any]) -> list[dict[str, Any]]:
    output = []
    for target, row in raw_results.items():
        if not isinstance(row, dict):
            output.append({"date": pred_date, "target": target, "error": "예측 결과 형식 오류"})
            continue
        if row.get("error"):
            output.append({"date": pred_date, "target": target, "error": row.get("error") or "예측 실패"})
            continue
        scale = 1000 if target in ("전력", "연료", "용수") else 1
        predicted = optional_scalar(row.get("pred_p50"))
        if predicted is None:
            predicted = optional_scalar(row.get("pred"))
        lower = optional_scalar(row.get("pred_p05"))
        upper = optional_scalar(row.get("pred_p95"))
        actual = optional_scalar(row.get("actual"))
        status = str(row.get("band_status") or "")
        if lower is None or upper is None or actual is None:
            status = "unknown"
        elif status not in {"inside", "over", "under"}:
            status = "over" if actual > upper else "under" if actual < lower else "inside"
        output.append({
            "date": pred_date,
            "target": target,
            "predicted": predicted / scale if predicted is not None else None,
            "lower": lower / scale if lower is not None else None,
            "upper": upper / scale if upper is not None else None,
            "actual": actual / scale if actual is not None else None,
            "status": status,
        })
    return output


@app.post("/api/v1/predictions/run")
def run_prediction(payload: PredictionRequest, request: Request) -> dict[str, Any]:
    require_admin(request)
    if payload.factory not in FACTORY_MEMBERS and payload.factory not in PREDICTION_FACTORIES:
        raise HTTPException(status_code=400, detail="v5.3 모델 학습 대상 공장이 아닙니다. (경산은 예측 미지원)")
    service = import_core("app.services.usage_prediction_v5_service")
    if payload.factory in FACTORY_MEMBERS:  # 전사/전체/남양주 — 집계 공장은 배치 경로 사용
        batch = service.predict_v5_batch(payload.factory, payload.date, payload.date, save_to_db=False)
        if not batch:
            raise HTTPException(status_code=400, detail="해당 일자는 근무일이 아니거나 예측할 수 없습니다.")
        raw = batch[0]
    else:
        raw = service.predict_v5(payload.factory, payload.date, payload.mix_prod_kg)
    return json_safe({"results": _format_prediction_results(payload.date, raw.get("results", {}))})


class HistoryBackfillRequest(BaseModel):
    factory: str | None = None
    date_from: date
    date_to: date


@app.post("/api/v1/predictions/generate-missing")
def generate_missing_history(payload: HistoryBackfillRequest, request: Request) -> dict[str, Any]:
    """prediction_log 누락 행 일괄 생성 (관리자 전용, 기존 서비스 위임)."""
    require_admin(request)
    if payload.date_from > payload.date_to:
        raise HTTPException(status_code=400, detail="시작일은 종료일보다 늦을 수 없습니다.")
    if (payload.date_to - payload.date_from).days > 92:
        raise HTTPException(status_code=400, detail="한 번에 최대 93일까지 생성할 수 있습니다.")
    service = import_core("app.services.usage_prediction_v5_service")
    factory = None if payload.factory in (None, "", "전사", "전체") else payload.factory
    result = service.generate_missing_prediction_history(factory, payload.date_from, payload.date_to)
    return json_safe(result)


@app.post("/api/v1/predictions/backfill-actuals")
def backfill_actuals(request: Request) -> dict[str, Any]:
    """prediction_log 실측값 역채움 (관리자 전용, 기존 서비스 위임)."""
    require_admin(request)
    service = import_core("app.services.usage_prediction_v5_service")
    updated = service.backfill_actuals()
    return {"updated_rows": int(updated)}


@app.get("/api/v1/sync/status")
def sync_status(request: Request) -> dict[str, Any]:
    require_admin(request)
    energy_service = import_core("app.services.daily_energy_sync_service")
    production_service = import_core("app.services.production_dw_sync_service")
    return json_safe({
        "scheduler": {
            "enabled": SYNC_INTERVAL_SECONDS > 0,
            "intervalSeconds": SYNC_INTERVAL_SECONDS,
            "lastRunAt": _scheduler_state["lastRunAt"],
            "lastError": _scheduler_state["lastError"],
        },
        "energy": energy_service.get_daily_energy_sync_status(),
        "production": production_service.get_sync_state(),
    })


class SyncRunRequest(BaseModel):
    force: bool = False


@app.post("/api/v1/sync/run")
def sync_run(payload: SyncRunRequest, request: Request) -> dict[str, Any]:
    """엑셀 → DB 동기화 즉시 실행 (관리자 전용)."""
    require_admin(request)
    return json_safe(run_excel_sync(force=payload.force))


@app.get("/api/v1/weather/status")
def weather_status(request: Request) -> dict[str, Any]:
    require_admin(request)
    service = import_core("app.services.weather_sync_service")
    return json_safe(service.get_weather_sync_status())


@app.post("/api/v1/weather/sync")
def weather_sync(request: Request) -> dict[str, Any]:
    """기상청 관측 데이터 동기화 (관리자 전용, 관측소별 결과 반환)."""
    require_admin(request)
    service = import_core("app.services.weather_sync_service")
    return json_safe({"stations": service.sync_all_stations()})


@app.get("/api/v1/model/training-status")
def training_status(request: Request) -> dict[str, Any]:
    require_admin(request)
    service = import_core("app.services.usage_prediction_v5_service")
    return json_safe(service.get_training_status())


@app.post("/api/v1/model/retrain")
def trigger_retrain(request: Request) -> dict[str, Any]:
    """v5 모델 재학습 백그라운드 시작 (관리자 전용, 중복 실행은 잠금으로 차단)."""
    require_admin(request)
    service = import_core("app.services.v5_retrain_service")
    result = service.trigger_v5_retrain(trigger_mode="manual")
    if not result.get("started"):
        raise HTTPException(status_code=409, detail=str(result.get("message", "재학습을 시작할 수 없습니다.")))
    return json_safe(result)


class DiagnosisRequest(BaseModel):
    factory: str
    date: date
    target: str
    force_refresh: bool = False


@app.post("/api/v1/predictions/diagnose")
def diagnose_anomaly(payload: DiagnosisRequest, request: Request) -> dict[str, Any]:
    """이상 이벤트 LLM 진단 — 캐시 우선. 재생성(force_refresh)만 관리자 전용(비용 발생)."""
    if payload.factory not in PREDICTION_FACTORIES:
        raise HTTPException(status_code=400, detail="이상 진단은 개별 공장 단위로만 가능합니다.")
    if payload.force_refresh:
        require_admin(request)
    service = import_core("app.services.anomaly_diagnosis_service")
    result = service.get_or_create_diagnosis(
        payload.factory, payload.date, payload.target, force_refresh=payload.force_refresh,
    )
    if result.get("error"):
        raise HTTPException(status_code=400, detail=str(result["error"]))
    return json_safe(result)


@app.get("/api/v1/targets")
def get_targets(year: int) -> dict[str, Any]:
    rows = fetch_all(
        "SELECT factory, metric, target_pct, note, updated_at FROM savings_target WHERE year=%s ORDER BY factory, metric",
        (year,),
    )
    return json_safe({"year": year, "targets": rows})


class TargetItem(BaseModel):
    factory: str
    metric: str
    target_pct: float | None = None


class TargetSaveRequest(BaseModel):
    year: int
    items: list[TargetItem]
    note: str | None = None


@app.put("/api/v1/targets")
def save_targets(payload: TargetSaveRequest, request: Request) -> dict[str, Any]:
    """절감 목표 일괄 저장 (관리자 전용, 기존 target_service 위임)."""
    require_admin(request)
    service = import_core("app.services.target_service")
    affected = service.upsert_targets(
        payload.year,
        [
            item.model_dump() if hasattr(item, "model_dump") else item.dict()
            for item in payload.items
        ],
        note=payload.note,
    )
    return {"affected": int(affected)}


@app.get("/api/v1/events")
def list_events(
    factory: str | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    limit: int = Query(100, le=500),
) -> dict[str, Any]:
    conditions, params = ["1=1"], []
    if factory and factory not in ("전사", "전체"):
        clause, values = factory_clause(factory)
        conditions.append(clause.removeprefix(" AND "))
        params.extend(values)
    if date_from:
        conditions.append("event_date >= %s")
        params.append(date_from)
    if date_to:
        conditions.append("event_date <= %s")
        params.append(date_to)
    params.append(limit)
    rows = fetch_all(
        f"""
        SELECT id, factory, event_date, target, tag, severity, note, created_at, updated_at, created_by
        FROM event_annotation WHERE {' AND '.join(conditions)}
        ORDER BY event_date DESC, id DESC LIMIT %s
        """,
        tuple(params),
    )
    return json_safe({"events": rows})


class EventCreateRequest(BaseModel):
    factory: str
    event_date: date
    note: str
    target: str = "overall"
    tag: str = "기타"
    severity: str = "info"


class EventUpdateRequest(BaseModel):
    note: str
    tag: str | None = None
    severity: str | None = None


@app.post("/api/v1/events")
def create_event(payload: EventCreateRequest, request: Request) -> dict[str, Any]:
    require_admin(request)
    if payload.factory not in PHYSICAL_FACTORIES:
        raise HTTPException(status_code=400, detail="이벤트는 개별 공장에만 등록할 수 있습니다.")
    if not payload.note.strip():
        raise HTTPException(status_code=400, detail="이벤트 내용을 입력하세요.")
    service = import_core("app.services.event_annotation_service")
    new_id = service.add_event(
        factory=payload.factory,
        event_date=payload.event_date.isoformat(),
        note=payload.note,
        target=payload.target,
        tag=payload.tag,
        severity=payload.severity,
    )
    return {"id": int(new_id)}


@app.put("/api/v1/events/{event_id}")
def update_event(event_id: int, payload: EventUpdateRequest, request: Request) -> dict[str, Any]:
    require_admin(request)
    service = import_core("app.services.event_annotation_service")
    ok = service.update_event(event_id, payload.note, tag=payload.tag, severity=payload.severity)
    if not ok:
        raise HTTPException(status_code=404, detail="해당 이벤트를 찾을 수 없습니다.")
    return {"updated": True}


@app.delete("/api/v1/events/{event_id}")
def delete_event(event_id: int, request: Request) -> dict[str, Any]:
    require_admin(request)
    service = import_core("app.services.event_annotation_service")
    ok = service.delete_event(event_id)
    if not ok:
        raise HTTPException(status_code=404, detail="해당 이벤트를 찾을 수 없습니다.")
    return {"deleted": True}


@app.get("/api/v1/audit")
def audit(request: Request) -> dict[str, Any]:
    require_admin(request)
    changes = fetch_all(
        """
        SELECT id, DATE_FORMAT(changed_at,'%%m-%%d %%H:%%i') time, factory, date,
               column_name field, old_value `before`, new_value `after`, change_type type, changed_by user
        FROM energy_daily_audit ORDER BY changed_at DESC LIMIT 100
        """
    )
    uploads = fetch_all(
        """
        SELECT id, filename, uploaded_at uploadedAt, record_count `rows`, status
        FROM upload_batch ORDER BY uploaded_at DESC LIMIT 30
        """
    )
    return json_safe({"changes": changes, "uploads": uploads})


async def _read_valid_excel(file: UploadFile) -> bytes:
    if not file.filename or Path(file.filename).suffix.lower() not in {".xlsx", ".xls"}:
        raise HTTPException(status_code=400, detail=".xlsx 또는 .xls 파일만 업로드할 수 있습니다.")
    content = await file.read()
    if len(content) > 50 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="파일 크기는 50MB 이하여야 합니다.")
    return content


@app.post("/api/v1/upload/preview")
async def upload_preview(request: Request, file: UploadFile = File(...)) -> dict[str, Any]:
    """업로드 dry-run — 파싱·검증 후 공장별 신규/덮어쓰기 건수만 계산 (DB 미변경)."""
    require_admin(request)
    content = await _read_valid_excel(file)
    from io import BytesIO

    service = import_core("app.services.upload_service")
    return json_safe(service.preview_excel(BytesIO(content), file.filename))


@app.post("/api/v1/upload")
async def upload(request: Request, file: UploadFile = File(...)) -> dict[str, Any]:
    require_admin(request)
    content = await _read_valid_excel(file)
    from io import BytesIO

    service = import_core("app.services.upload_service")
    result = service.upload_excel(BytesIO(content), file.filename, save_original=True)
    if not result.get("success"):
        raise HTTPException(status_code=400, detail=json_safe(result))
    return {"status": "success", "rows": int(result.get("record_count", 0)), "message": result.get("message", "")}


class ReportRequest(BaseModel):
    factory: str
    year: int
    month: int


@app.get("/api/v1/reports")
def get_report(factory: str, year: int, month: int) -> dict[str, Any]:
    row = fetch_one(
        """
        SELECT report_content content, created_at, updated_at
        FROM ai_reports WHERE factory=%s AND report_year=%s AND report_month=%s
        """,
        (factory, year, month),
    )
    return json_safe(row or {"content": None, "created_at": None, "updated_at": None})


@app.get("/api/v1/reports/available")
def available_reports(factory: str) -> dict[str, Any]:
    rows = fetch_all(
        "SELECT report_year year, report_month month FROM ai_reports WHERE factory=%s ORDER BY report_year DESC, report_month DESC",
        (factory,),
    )
    return json_safe({"months": rows})


@app.post("/api/v1/reports/generate")
def generate_report(payload: ReportRequest, request: Request) -> dict[str, Any]:
    require_admin(request)
    agent_service = import_core("app.services.ai_db_service")
    report_service = import_core("app.services.ai_report_service")
    content = agent_service.run_agent_report(payload.factory, payload.year, payload.month)
    if (
        not isinstance(content, str)
        or not content.strip()
        or "AI Agent 분석 중 오류" in content
    ):
        raise HTTPException(status_code=502, detail="AI 보고서 생성에 실패했습니다.")
    if not report_service.save_report(payload.factory, payload.year, payload.month, content):
        raise HTTPException(status_code=500, detail="보고서 DB 저장에 실패했습니다.")
    return {"updated_at": datetime.now().strftime("%Y-%m-%d %H:%M"), "content": content}


MAIL_PERIOD_LABELS = {"daily": "일간", "weekly": "주간", "monthly": "월간"}


def normalize_mail_period(value: str) -> str:
    period = (value or "").strip().lower()
    if period not in MAIL_PERIOD_LABELS:
        raise HTTPException(status_code=400, detail="period는 daily·weekly·monthly 중 하나여야 합니다.")
    return period


class MailSendRequest(BaseModel):
    period: str
    # 필드명을 date로 두면 클래스 본문에서 datetime.date 타입을 가려 애너테이션
    # 평가가 깨진다 — 내부 이름은 ref_date, 요청 본문 키는 alias "date"를 유지.
    ref_date: date | None = Field(default=None, alias="date")


@app.post("/api/v1/mail/send")
def send_mail_report(payload: MailSendRequest, request: Request) -> dict[str, Any]:
    """에너지 리포트 메일 즉시 발송 (관리자 전용).

    legacy 대시보드의 '📧 메일 송부'와 동일하게 tools/mail CLI 빌더·발송
    파이프라인을 재사용한다. 일간은 기준일(미지정 시 근무일 D-1 규칙),
    주간·월간은 직전 완결 주·월이 대상이다.
    """
    require_admin(request)
    period = normalize_mail_period(payload.period)
    mail_config = import_core("tools.mail.config")
    config = mail_config.get_mail_config()
    if not config.is_valid:
        raise HTTPException(
            status_code=503,
            detail="메일 설정이 비어 있습니다. .env 항목을 확인하세요: " + ", ".join(config.missing_keys()),
        )
    mail_service = import_core("tools.mail.mail_service")
    try:
        if period == "weekly":
            report = import_core("tools.mail.period_report_builder").build_weekly_report()
        elif period == "monthly":
            report = import_core("tools.mail.period_report_builder").build_monthly_report()
        else:
            report = import_core("tools.mail.daily_report_builder").build_daily_report(ref_date=payload.ref_date)
        message = mail_service.MailMessage(
            subject=report.subject,
            html_body=report.html,
            inline_images=report.inline_images,
        )
        result = mail_service.send_mail(message, config)
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Mail report build/send failed (period=%s).", period, exc_info=True)
        raise HTTPException(status_code=502, detail="메일 발송에 실패했습니다. 서버 로그를 확인하세요.") from exc
    recipients = result.get("to") if isinstance(result, dict) else None
    return json_safe({
        "period": period,
        "label": MAIL_PERIOD_LABELS[period],
        "refDate": report.ref_date,
        "recordCount": report.record_count,
        "to": recipients or config.recipients,
    })
