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
import base64
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


class RejectUntrustedUnsafeOrigins:
    """Reject cross-origin state changes before they reach an API handler.

    Implemented as a raw ASGI middleware, not `@app.middleware("http")`
    (Starlette's BaseHTTPMiddleware). BaseHTTPMiddleware runs the downstream app in a
    separate task via call_next(); when an unhandled exception occurs downstream, that
    task boundary can swallow the response our own `@app.exception_handler(Exception)`
    already built — including the Access-Control-Allow-Origin header CORSMiddleware
    added to it — so the client sees a bare, header-less response. A pure ASGI
    middleware just forwards `send` directly, so nothing downstream gets lost, no
    matter where CORSMiddleware sits relative to this one. (2026-07: this was the
    actual cause behind an admin panel button raising a plain "Failed to fetch" in the
    browser for what was really a normal, readable 500 JSON error underneath.)
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request = Request(scope, receive=receive)
        method = request.method.upper()
        preflight_method = request.headers.get("access-control-request-method", "").upper()
        unsafe_methods = {"POST", "PUT", "PATCH", "DELETE"}
        is_unsafe = method in unsafe_methods
        is_unsafe_preflight = method == "OPTIONS" and preflight_method in unsafe_methods
        origin = request.headers.get("origin")
        if (is_unsafe or is_unsafe_preflight) and origin and _canonical_origin(origin) not in ALLOWED_ORIGINS:
            response = JSONResponse(status_code=403, content={"detail": "허용되지 않은 Origin입니다."})
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)


app.add_middleware(RejectUntrustedUnsafeOrigins)
# CORSMiddleware registered last (= outermost in the ASGI stack), matching the
# conventional recommendation so Access-Control-Allow-Origin is applied as close to
# the client as possible.
app.add_middleware(
    CORSMiddleware,
    allow_origins=sorted(ALLOWED_ORIGINS),
    allow_credentials=False,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "Accept"],
)


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
    max_date: date | None = None,
) -> tuple[date, date]:
    """조회 모드별 기간 확정 (월별/기간별/연간). 잘못된 범위는 400.

    월별 모드는 base(기준일)가 그 달의 며칠이든 항상 1일~말일 전체를 반환한다.
    실적 데이터가 그 달 말일 전에 끊겨 있으면(= max_date) 거기까지로만 제한한다.
    (2026-07-21 버그 수정: 이전엔 period_to를 base로 반환해, 예컨대 7/15을 기준일로
    고르면 7/16~7/31 실적이 있어도 표시되지 않았다 — 어떤 날짜를 골랐든 그 달의
    실적이 있는 데까지는 전부 보여야 하므로 base.day와 무관하게 계산한다.)
    """
    if mode == "month":
        month_end = date(base.year, base.month, calendar.monthrange(base.year, base.month)[1])
        period_to = min(month_end, max_date) if max_date is not None else month_end
        return base.replace(day=1), period_to
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
    """에너지·원단위 일별 조회 구간 확정 — 기간 미지정 시 당월 1일~기준일.

    과거 '기준일 역산 30일' 방식은 31일인 달을 완결 조회해도 1일이 누락되는
    결함이 있어 월 단위 시맨틱으로 변경했다 (2026-07-18).
    """
    if date_from is None and date_to is None:
        return base.replace(day=1), base
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


def weighted_intensity_yoy(
    monthly_usage: dict[tuple[int, int], float],
    monthly_production_kg: dict[tuple[int, int], float],
    year: int,
) -> dict[str, Any] | None:
    """전년대비 원단위 누계 — 단순 평균이 아닌 가중 평균(Σ사용량 ÷ Σ생산톤).

    legacy 원단위 페이지의 누계 규칙과 동일하되, 금년 실적이 있는 월들만
    전년과 같은 기간으로 합산한다(동월 누계 — 왜곡 방지).
    """
    months = sorted(
        m for (y, m) in monthly_usage
        if y == year and monthly_production_kg.get((y, m), 0.0) > 0
    )
    if not months:
        return None

    def cumulative(target_year: int) -> float | None:
        usage = sum(monthly_usage.get((target_year, m), 0.0) for m in months)
        prod_ton = sum(monthly_production_kg.get((target_year, m), 0.0) for m in months) / 1000
        return usage / prod_ton if prod_ton > 0 else None

    current = cumulative(year)
    previous = cumulative(year - 1)
    return {
        "months": len(months),
        "lastMonth": months[-1],
        "current": round(current, 2) if current is not None else None,
        "previous": round(previous, 2) if previous is not None else None,
        "change": rate_change(current, previous) if (current is not None and previous) else None,
    }


def build_production_insights(
    *,
    plan: float | None,
    actual: float,
    progress: float | None,
    cat2_plan: dict[str, float],
    cat2_actual: dict[str, float],
) -> list[str]:
    """생산실적 자동 인사이트 — legacy _generate_insights 규칙의 이식.

    진척률 구간 판정 + 최대 제품유형 + 부진 제품유형(진척 80% 미만)을
    문장 리스트로 반환한다.
    """
    messages: list[str] = []
    if plan is None or plan <= 0 or progress is None:
        messages.append(f"📊 누계 실적 {actual:,.0f} ton (계획 데이터 없음)")
    elif progress >= 100:
        messages.append(f"🎯 누계 진척률 {progress:.1f}% — 계획 초과 달성")
    elif progress >= 90:
        messages.append(f"✅ 누계 진척률 {progress:.1f}% — 정상 추세")
    elif progress >= 70:
        messages.append(f"🟡 누계 진척률 {progress:.1f}% — 잔여 기간 주의")
    else:
        messages.append(f"⚠️ 누계 진척률 {progress:.1f}% — 가속 필요")

    ranked = sorted(cat2_actual.items(), key=lambda item: item[1], reverse=True)
    if ranked and ranked[0][1] > 0:
        top_key, top_actual = ranked[0]
        top_plan = cat2_plan.get(top_key, 0.0)
        piece = f"🏭 최대 제품유형: {top_key} (실적 {top_actual:,.0f} ton"
        if top_plan > 0:
            piece += f", 진척 {top_actual / top_plan * 100:.1f}%"
        messages.append(piece + ")")
    with_plan = [
        (key, cat2_actual.get(key, 0.0) / cat2_plan[key] * 100)
        for key in cat2_plan if cat2_plan[key] > 0
    ]
    if with_plan:
        worst_key, worst_progress = min(with_plan, key=lambda item: item[1])
        if worst_progress < 80:
            messages.append(f"📉 부진 제품유형: {worst_key} (진척 {worst_progress:.1f}%)")
    return messages


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
async def unhandled_error(request: Request, exc: Exception):
    """Catch-all 500. Starlette wires bare-`Exception` handlers into
    ServerErrorMiddleware — the true outermost ASGI layer, outside our own
    CORSMiddleware — so a response built here bypasses CORSMiddleware entirely and
    reaches the browser with no Access-Control-Allow-Origin header. The browser then
    reports a CORS-blocked "Failed to fetch" instead of surfacing this JSON body, so we
    add the same header CORSMiddleware would have added for an allowed Origin.
    """
    logger.error("Unhandled API exception.", exc_info=(type(exc), exc, exc.__traceback__))
    response = JSONResponse(status_code=500, content={"detail": "내부 서버 오류가 발생했습니다."})
    origin = request.headers.get("origin")
    canonical = _canonical_origin(origin) if origin else None
    if canonical and canonical in ALLOWED_ORIGINS:
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Vary"] = "Origin"
    return response


@app.get("/api/v1/health")
def health() -> dict[str, Any]:
    row = fetch_one("SELECT MAX(updated_at) AS updated_at, COUNT(*) AS records FROM energy_daily")
    return {"status": "ok", "database": "mysql", **json_safe(row or {})}


@app.get("/api/v1/data-status")
def data_status() -> dict[str, Any]:
    """원본별 최신 보유 일자와 지연 일수 — 현업 화면 상단 신선도 배지용.

    지금까지 동기화 상태는 관리자 탭에만 있어, 현업은 지금 보는 숫자가 어제까지인지
    사흘 전까지인지 알 방법이 없었다. 지연 판정 기준(2일)은 주말/공휴일에 생산이
    없어 하루 비는 것이 정상이기 때문이다.
    """
    energy = fetch_one("SELECT MAX(date) max_date, MAX(updated_at) updated_at FROM energy_daily") or {}
    production = fetch_one("SELECT MAX(date) max_date, MAX(updated_at) updated_at FROM production_daily") or {}
    today = date.today()

    def entry(row: dict[str, Any]) -> dict[str, Any]:
        last_date = normalize_date(row.get("max_date"))
        lag = (today - last_date).days if last_date else None
        return {
            "lastDate": last_date,
            "lagDays": lag,
            "stale": lag is not None and lag > 2,
            "updatedAt": row.get("updated_at"),
        }

    # 공장별 최신 일자 — 전체 최신일에 못 미치는 공장은 그 날짜 집계·메일에서 통째로
    # 빠지는데, 화면에는 조용히 사라져 "실적이 안 뜬다"로 보인다. 명시적으로 알린다.
    energy_max = normalize_date(energy.get("max_date"))
    factory_rows = fetch_all("SELECT factory, MAX(date) max_date FROM energy_daily GROUP BY factory")
    lagging = []
    for row in factory_rows:
        factory_last = normalize_date(row.get("max_date"))
        if energy_max is None or factory_last is None or factory_last >= energy_max:
            continue
        lagging.append({
            "factory": str(row.get("factory")),
            "lastDate": factory_last,
            "behindDays": (energy_max - factory_last).days,
        })
    lagging.sort(key=lambda item: item["behindDays"], reverse=True)

    return json_safe({
        "energy": entry(energy),
        "production": entry(production),
        "laggingFactories": lagging,
        "today": today,
    })


@app.get("/api/v1/session")
def session(request: Request) -> dict[str, str]:
    client_ip = request.client.host if request.client else "unknown"
    return {
        "role": "admin" if client_is_admin(request) else "viewer",
        "clientIp": client_ip,
        "serverName": socket.gethostname(),
    }


@app.get("/api/v1/settings/page-visibility")
def get_page_visibility() -> dict[str, bool]:
    """조회 사용자 사이드바에 노출할 페이지 여부 — 모든 세션(admin·viewer)이 조회 가능."""
    service = import_core("app.services.page_visibility_service")
    return json_safe(service.get_visibility())


class PageVisibilityRequest(BaseModel):
    pages: dict[str, bool]


@app.put("/api/v1/settings/page-visibility")
def update_page_visibility(payload: PageVisibilityRequest, request: Request) -> dict[str, bool]:
    """페이지 노출 설정 저장 (관리자 전용)."""
    require_admin(request)
    service = import_core("app.services.page_visibility_service")
    return json_safe(service.set_visibility(payload.pages))


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
               COALESCE(SUM(water_ton),0) water,
               COALESCE(SUM(wastewater_ton),0) wastewater
        FROM energy_daily WHERE date BETWEEN %s AND %s
        """ + clause,
        (date_from, date_to, *values),
    ) or {}
    if actual_records is None:
        frame = fetch_actual_production_frame(date_from, date_to)
        actual_records = actual_production_records(frame)
    totals = {key: scalar(row.get(key)) for key in ("power", "fuel", "water", "wastewater")}
    totals["production"] = actual_production_kg(actual_records, factory, date_from, date_to)
    return totals


def rate_change(current: float, previous: float) -> float:
    return round((current / previous - 1) * 100, 1) if previous else 0.0


def factory_yoy_entry(factory: str, current: dict[str, float], previous: dict[str, float]) -> dict[str, Any]:
    """공장 1곳의 당월 vs 전년 동기 원단위·사용량·생산량 비교 블록.

    legacy 대시보드 '월간 원단위/사용량/생산량 전년비'(get_monthly_yoy_summary)의
    지표 구성을 따른다 — 원단위 3종 + 폐수/용수 비율, 사용량 4종, 생산량(ton).
    사용량 표시는 7일 추이와 같은 단위(전력 MWh, 연료 Nm³, 용수·폐수 ton).
    """
    def pair(cur: float | None, prev: float | None, digits: int = 2) -> dict[str, float | None]:
        return {
            "current": round(cur, digits) if cur is not None else None,
            "previous": round(prev, digits) if prev is not None else None,
        }

    def intensity_of(values: dict[str, float], key: str) -> float | None:
        prod_ton = values.get("production", 0.0) / 1000
        return values[key] / prod_ton if prod_ton > 0 else None

    def wwratio_of(values: dict[str, float]) -> float | None:
        water = values.get("water", 0.0)
        return values.get("wastewater", 0.0) / water if water > 0 else None

    return {
        "factory": factory,
        "intensity": {
            "power": pair(intensity_of(current, "power"), intensity_of(previous, "power"), 1),
            "fuel": pair(intensity_of(current, "fuel"), intensity_of(previous, "fuel")),
            "water": pair(intensity_of(current, "water"), intensity_of(previous, "water")),
            "wwratio": pair(wwratio_of(current), wwratio_of(previous)),
        },
        "usage": {
            "power": pair(current["power"] / 1000, previous["power"] / 1000, 1),
            "fuel": pair(current["fuel"], previous["fuel"], 1),
            "water": pair(current["water"], previous["water"], 1),
            "wastewater": pair(current["wastewater"], previous["wastewater"], 1),
        },
        "production": pair(current["production"] / 1000, previous["production"] / 1000, 1),
    }


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
    yoy_factories = []
    for display_factory in DISPLAY_FACTORIES:
        current_factory = aggregate_period(display_factory, month_start, base, actual_records=actual_records)
        previous_factory = aggregate_period(display_factory, prev_start, prev_base, actual_records=actual_records)
        yoy_factories.append(factory_yoy_entry(display_factory, current_factory, previous_factory))
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

    # 공장별 에너지 사용 비율 도넛 — legacy _render_energy_composition (YTD 누계)
    composition_rows = fetch_all(
        """
        SELECT factory, SUM(total_power_kwh)/1000 power, SUM(fuel_nm3) fuel,
               SUM(water_ton) water, SUM(wastewater_ton) wastewater
        FROM energy_daily WHERE date BETWEEN %s AND %s GROUP BY factory
        """,
        (date(base.year, 1, 1), base),
    )
    composition: dict[str, dict[str, float | str]] = {}
    for row in composition_rows:
        name = "남양주" if row["factory"] in ("남양주1", "남양주2") else str(row["factory"])
        entry = composition.setdefault(name, {"factory": name, "power": 0.0, "fuel": 0.0, "water": 0.0, "wastewater": 0.0})
        for key in ("power", "fuel", "water", "wastewater"):
            entry[key] = round(scalar(entry[key]) + scalar(row.get(key)), 1)

    event_clause, event_values = factory_clause(factory)
    events = fetch_all(
        "SELECT id, event_date, factory, target, tag, severity, note FROM event_annotation WHERE 1=1" + event_clause + " ORDER BY event_date DESC, id DESC LIMIT 5",
        tuple(event_values),
    )
    # 단순 이탈 COUNT가 아니라 판정 규칙(단발/반복/지속편차)을 적용한다 —
    # 90% 밴드에서는 정상이어도 10%가 이탈해 상시 경고가 되기 때문.
    alert_banner = band_alert_banner(band_rule_evaluation(factory, base))
    return json_safe({
        "baseDate": base.isoformat(),
        "factory": factory,
        "updatedAt": max_row.get("updated_at") or datetime.now(),
        "alert": alert_banner,
        "metrics": metrics,
        "trend": trend,
        "yoy": yoy,
        "factoryComparison": comparisons,
        "yoyFactories": yoy_factories,
        "yoyPeriod": {
            "currentFrom": month_start.isoformat(), "currentTo": base.isoformat(),
            "previousFrom": prev_start.isoformat(), "previousTo": prev_base.isoformat(),
        },
        "composition": list(composition.values()),
        "compositionLabel": f"{base.year}년 1월~{base.month}월 누계",
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
               SUM(water_ton)/1000 water, SUM(wastewater_ton)/1000 wastewater,
               SUM(freezing_power_kwh)/1000 freezing, SUM(air_compressor_kwh)/1000 compressor
        FROM energy_daily WHERE date BETWEEN %s AND %s
        """ + clause + " GROUP BY date ORDER BY date",
        (window_from, window_to, *values),
    )
    # 전력 설비 분해(legacy 일별 추이) — 기타 = 전체 − 냉동 − 공압, 음수 방지.
    for row in rows:
        row["other"] = round(max(0.0, scalar(row.get("power")) - scalar(row.get("freezing")) - scalar(row.get("compressor"))), 2)
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
    # 공장별 비교 라인(legacy compare_factories) — 전사 조회일 때만 제공.
    daily_by_factory: list[dict[str, Any]] = []
    if factory in ("전사", "전체"):
        per_factory_rows = fetch_all(
            """
            SELECT date, factory, SUM(total_power_kwh)/1000 power, SUM(fuel_nm3)/1000 fuel,
                   SUM(water_ton)/1000 water, SUM(wastewater_ton)/1000 wastewater
            FROM energy_daily WHERE date BETWEEN %s AND %s
            GROUP BY date, factory ORDER BY date
            """,
            (window_from, window_to),
        )
        merged: dict[date, dict[str, Any]] = {}
        for row in per_factory_rows:
            row_date = normalize_date(row.get("date"))
            if row_date is None:
                continue
            name = "남양주" if row["factory"] in ("남양주1", "남양주2") else str(row["factory"])
            bucket = merged.setdefault(row_date, {"date": row_date.strftime("%m.%d")})
            metrics_bucket = bucket.setdefault("metrics", {})
            factory_bucket = metrics_bucket.setdefault(name, {"power": 0.0, "fuel": 0.0, "water": 0.0, "wastewater": 0.0})
            for key in ("power", "fuel", "water", "wastewater"):
                factory_bucket[key] = round(scalar(factory_bucket[key]) + scalar(row.get(key)), 2)
        daily_by_factory = [merged[key] for key in sorted(merged)]
    return json_safe({
        "baseDate": base,
        "mode": "range" if ranged else "recent",
        "dateFrom": window_from,
        "dateTo": window_to,
        "daily": [{**row, "date": row["date"].strftime("%m.%d")} for row in rows],
        "dailyByFactory": daily_by_factory,
        "equipment": equipment_rows,
        "factories": list(combined.values()),
        "yoyYear": base.year,
        "yoy": build_energy_yoy(yoy_rows, base.year),
        "coverage": period_coverage(factory, window_from, window_to),
    })


def period_coverage(factory: str, date_from: date, date_to: date) -> dict[str, Any]:
    """선택 기간의 데이터 결측일 — 부분 결측을 완전한 집계로 오인하지 않게 한다."""
    clause, values = factory_clause(factory)
    row = fetch_one(
        "SELECT COUNT(DISTINCT date) days FROM energy_daily WHERE date BETWEEN %s AND %s" + clause,
        (date_from, date_to, *values),
    ) or {}
    expected = (date_to - date_from).days + 1
    present = int(row.get("days") or 0)
    return {"expectedDays": expected, "presentDays": present, "missingDays": max(0, expected - present)}


@app.get("/api/v1/intensity")
def intensity_analysis(
    factory: str = "전사",
    metric: str = "power",
    requested_date: date | None = Query(None, alias="date"),
    date_from: date | None = Query(None),
    date_to: date | None = Query(None),
) -> dict[str, Any]:
    """원단위 분석: 일별 추이 + 월별 금년/전년/목표 추이 + 가중 누계 + 공장 매트릭스."""
    spec = INTENSITY_METRICS.get(metric)
    if spec is None:
        raise HTTPException(status_code=400, detail=f"지원하지 않는 지표입니다: {metric}")
    usage_col = spec["column"]

    max_row = fetch_one("SELECT MAX(date) max_date FROM energy_daily") or {}
    base = bounded_base_date(requested_date, max_row.get("max_date"))
    window_from, window_to = resolve_energy_window(base, date_from, date_to)
    history_start = min(date(base.year - 1, 1, 1), window_from)
    actual_frame = fetch_actual_production_frame(history_start, base)
    actual_records = actual_production_records(actual_frame)

    clause, values = factory_clause(factory)
    daily_rows = fetch_all(
        f"""
        SELECT date, SUM({usage_col}) usage_sum
        FROM energy_daily WHERE date BETWEEN %s AND %s
        """ + clause + " GROUP BY date ORDER BY date",
        (window_from, window_to, *values),
    )
    daily_production = actual_production_daily_kg(actual_records, factory, window_from, window_to)
    daily = []
    for row in daily_rows:
        row_date = normalize_date(row.get("date"))
        if row_date is None:
            continue
        prod_ton = daily_production.get(row_date, 0.0) / 1000
        value = scalar(row.get("usage_sum")) / prod_ton if prod_ton > 0 else None
        daily.append({
            "date": row_date.strftime("%m.%d"),
            "value": round(value, 2) if value is not None else None,
            # 누계 토글용 원자료 — 클라이언트가 Σ사용량÷Σ생산톤 누계선을 재계산한다.
            "usage": round(scalar(row.get("usage_sum")), 2),
            "productionTon": round(prod_ton, 3),
        })
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
    monthly_usage: dict[tuple[int, int], float] = {}
    monthly_map: dict[tuple[int, int], float] = {}
    for row in monthly_rows:
        key = (int(row["y"]), int(row["m"]))
        monthly_usage[key] = scalar(row.get("usage_sum"))
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
            # 연간 차트의 '누계 추이 보기' 토글용 원자료 — 클라이언트가 1월부터의
            # 가중 누계(Σ사용량÷Σ생산톤)를 재계산한다 (legacy 규칙).
            "currentUsage": round(monthly_usage.get((base.year, month), 0.0), 2),
            "currentTon": round(monthly_production.get((base.year, month), 0.0) / 1000, 3),
            "previousUsage": round(monthly_usage.get((base.year - 1, month), 0.0), 2),
            "previousTon": round(monthly_production.get((base.year - 1, month), 0.0) / 1000, 3),
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

    # 원단위 변동 원인분해 — 원단위는 사용량÷생산량이라 악화 원인이 둘 중 어느 쪽인지
    # 값 자체로는 알 수 없다. 2요인 정확 분해로 잔차 없이 나눈다.
    usage_key = {"total_power_kwh": "power", "fuel_nm3": "fuel", "water_ton": "water"}[usage_col]

    def build_bridge(start: date, prev_start: date) -> dict[str, Any] | None:
        current_totals = aggregate_period(factory, start, base, actual_records=actual_records)
        previous_totals = aggregate_period(factory, prev_start, prev_base, actual_records=actual_records)
        usage_curr, ton_curr = current_totals[usage_key], current_totals["production"] / 1000
        usage_prev, ton_prev = previous_totals[usage_key], previous_totals["production"] / 1000
        if ton_curr <= 0 or ton_prev <= 0 or usage_prev <= 0:
            return None
        # ΔI = (U₁-U₀)/P₁ + U₀·(1/P₁ - 1/P₀) — 두 항의 합이 정확히 ΔI가 되어 잔차가 없다.
        usage_effect = (usage_curr - usage_prev) / ton_curr
        production_effect = usage_prev * (1 / ton_curr - 1 / ton_prev)
        return {
            "previous": round(usage_prev / ton_prev, 2),
            "current": round(usage_curr / ton_curr, 2),
            "usageEffect": round(usage_effect, 2),
            "productionEffect": round(production_effect, 2),
            "usagePrev": round(usage_prev, 1), "usageCurr": round(usage_curr, 1),
            "usageChange": rate_change(usage_curr, usage_prev),
            "tonPrev": round(ton_prev, 1), "tonCurr": round(ton_curr, 1),
            "tonChange": rate_change(ton_curr, ton_prev),
        }

    bridge = {
        "mtd": build_bridge(base.replace(day=1), prev_base.replace(day=1)),
        "ytd": build_bridge(base.replace(month=1, day=1), prev_base.replace(month=1, day=1)),
    }

    return json_safe({
        "baseDate": base,
        "metric": metric,
        "unit": spec["unit"],
        "year": base.year,
        "targetPct": target_pct,
        "mode": "range" if date_from is not None else "recent",
        "dateFrom": window_from,
        "dateTo": window_to,
        "daily": daily,
        "summary": summary,
        "monthly": monthly,
        "yoyCumulative": weighted_intensity_yoy(monthly_usage, monthly_production, base.year),
        "matrix": matrix,
        "bridge": bridge,
        "coverage": period_coverage(factory, window_from, window_to),
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
    period_from, period_to = resolve_production_period(mode, base, date_from, date_to, normalize_date(max_row.get("max_date")))
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
    # 광주 전용 — 자사 완제품(production_daily)에는 안 잡히는 외부판매 재공품
    # (탈지분유·살균유 등)의 품목별 구성비. 신뢰 대상 밖 공장은 빈 리스트.
    wip_mix = import_core("app.services.production_correction_service").get_wip_mix(
        factory, period_from, period_to,
    )
    # 광주 전용 — 유틸리티가 실제로 소비되는 총 생산량(완제품+재공품 환산, ton).
    # 자사 완제품 실적(daily_output의 cat2 합)만으로는 탈지분유·생크림 등
    # 판매용 반제품 생산이 빠져 원단위 분모(accounted_kg)와 어긋나 보인다 —
    # 같은 정의를 트렌드 차트에도 노출해 실질 생산량을 비교할 수 있게 한다.
    wip_daily_kg: dict[date, float] = {}
    if factory == "광주":
        wip_frame = import_core("app.services.production_correction_service").get_wip_daily("광주")
        for row in _table_records(wip_frame):
            row_date = normalize_date(row.get("date"))
            kg = optional_scalar(row.get("total_wip_kg"))
            if row_date is not None and kg is not None and period_from <= row_date <= period_to:
                wip_daily_kg[row_date] = wip_daily_kg.get(row_date, 0.0) + kg
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
    monthly_plan_actual: list[dict[str, Any]] = []
    if mode == "year":
        monthly_map = {int(row["month_no"]): row for row in daily if row.get("month_no") is not None}
        # 전년 동월의 제품유형별 실적 — 유형별 전년비를 같은 차트에서 보기 위해 함께 싣는다.
        previous_rows = fetch_all(
            "SELECT MONTH(date) month_no," + cat2_select +
            " FROM production_daily WHERE date BETWEEN %s AND %s"
            + clause + " GROUP BY month_no ORDER BY month_no",
            (date(base.year - 1, 1, 1), date(base.year - 1, 12, 31), *values),
        )
        previous_map = {int(row["month_no"]): row for row in previous_rows if row.get("month_no") is not None}
        wip_monthly_ton: dict[int, float] = {}
        for wip_date, kg in wip_daily_kg.items():
            wip_monthly_ton[wip_date.month] = wip_monthly_ton.get(wip_date.month, 0.0) + kg / 1000
        for month in range(1, 13):
            row = monthly_map.get(month, {})
            previous_row = previous_map.get(month, {})
            entry = {
                "date": f"{month}월",
                **{key: round(scalar(row.get(key)), 3) for key in cat2_keys},
                **{f"prev{key}": round(scalar(previous_row.get(key)), 3) for key in cat2_keys},
            }
            if wip_monthly_ton:
                cat2_total = sum(scalar(row.get(key)) for key in cat2_keys)
                entry["utilityProd"] = round(cat2_total + wip_monthly_ton.get(month, 0.0), 3)
            daily_output.append(entry)
        # 월별 계획 대비 실적 — 생산계획은 주 단위로 수립·집계되므로 연 누계(Burn-up)보다
        # 월 단위 달성률이 현장의 계획 관리 주기와 맞는다. 진행 중인 달은 월 전체가 아니라
        # 기준일까지의 실적이라 달성률이 낮게 보이므로 partial 플래그로 구분한다.
        monthly_plan_rows = fetch_all(
            """
            SELECT m, SUM(planned_qty) plan FROM (
              SELECT factory,item_code,MONTH(date) m,MAX(planned_qty)/1000 planned_qty
              FROM production_daily WHERE date BETWEEN %s AND %s
            """ + clause + " GROUP BY factory,item_code,m) p GROUP BY m ORDER BY m",
            (period_from, period_to, *values),
        )
        monthly_plan = {int(row["m"]): scalar(row.get("plan")) for row in monthly_plan_rows}
        last_actual_month = max(monthly_map) if monthly_map else 0
        for month in range(1, 13):
            plan_value = monthly_plan.get(month, 0.0)
            row = monthly_map.get(month, {})
            actual_value = sum(scalar(row.get(key)) for key in cat2_keys)
            measured = month <= last_actual_month
            monthly_plan_actual.append({
                "month": f"{month}월",
                "plan": round(plan_value, 1) if plan_value > 0 else None,
                # 실적은 마지막 실적 월까지만 — 미래 월을 0으로 두면 달성률 0%로 오독된다.
                "actual": round(actual_value, 1) if measured else None,
                "rate": round(actual_value / plan_value * 100, 1) if (measured and plan_value > 0) else None,
                "partial": measured and month == base.month,
            })
    else:
        # 월별·기간별 모두 기간 전체를 그대로 반환한다 — 과거 [-14:] 절단은
        # 월초 일자가 누락되는 결함이었음 (2026-07-18 수정).
        for row in daily:
            row_date = normalize_date(row.get("date"))
            if row_date is None:
                continue
            entry = {
                "date": row_date.strftime("%m.%d") if mode == "month" else row_date.isoformat(),
                **{key: round(scalar(row.get(key)), 3) for key in cat2_keys},
            }
            if wip_daily_kg:
                cat2_total = sum(scalar(row.get(key)) for key in cat2_keys)
                entry["utilityProd"] = round(cat2_total + wip_daily_kg.get(row_date, 0.0) / 1000, 3)
            daily_output.append(entry)

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

    # 계획 미달/초과 Top (legacy under/over 탭) — 계획 지표가 유효한 기간에만
    under_items: list[dict[str, Any]] = []
    over_items: list[dict[str, Any]] = []
    if plan_allowed:
        gap_rows = fetch_all(
            """
            SELECT item_name name, SUM(plan) plan, SUM(actual) actual
            FROM (
              SELECT factory, item_code, MAX(item_name) item_name,
                     MAX(planned_qty)/1000 plan, SUM(actual_qty)/1000 actual
              FROM production_daily WHERE date BETWEEN %s AND %s
            """ + clause + """
              GROUP BY factory,item_code,YEAR(date),MONTH(date)
            ) item_totals
            GROUP BY item_code,item_name HAVING SUM(plan) > 0
            """,
            (period_from, period_to, *values),
        )
        ranked = []
        for row in gap_rows:
            gap_plan = scalar(row.get("plan"))
            gap_actual = scalar(row.get("actual"))
            ranked.append({
                "name": row.get("name"),
                "plan": round(gap_plan, 3),
                "actual": round(gap_actual, 3),
                "variance": round(gap_actual - gap_plan, 3),
                "rate": round(gap_actual / gap_plan * 100, 1) if gap_plan > 0 else None,
            })
        under_items = sorted((r for r in ranked if r["variance"] < 0), key=lambda r: r["variance"])[:8]
        over_items = sorted((r for r in ranked if r["variance"] > 0), key=lambda r: r["variance"], reverse=True)[:8]

    # 제품유형별 계획·실적 breakdown → 자동 인사이트 (legacy _generate_insights)
    cat2_plan_rows = fetch_all(
        """
        SELECT cat2, SUM(plan) plan FROM (
          SELECT CASE WHEN category2 IN ('IC','MY','FM','SN') THEN category2 ELSE 'ETC' END cat2,
                 factory, item_code, MAX(planned_qty)/1000 plan
          FROM production_daily WHERE date BETWEEN %s AND %s
        """ + clause + """
          GROUP BY cat2,factory,item_code,YEAR(date),MONTH(date)
        ) t GROUP BY cat2
        """,
        (period_from, period_to, *values),
    ) if plan_allowed else []
    cat2_plan = {str(row["cat2"]): scalar(row.get("plan")) for row in cat2_plan_rows}
    cat2_actual = {key: sum(scalar(row.get(key)) for row in daily) for key in cat2_keys}
    insights = build_production_insights(
        plan=plan if plan_allowed else None,
        actual=actual,
        progress=(actual / plan * 100) if plan_allowed and plan > 0 else None,
        cat2_plan=cat2_plan,
        cat2_actual=cat2_actual,
    )

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

    # 연간 모드 월별 전년비 (legacy '연간 월별 실적 — 전년비')
    monthly_yoy: list[dict[str, Any]] = []
    if mode == "year":
        yoy_rows = fetch_all(
            """
            SELECT YEAR(date) y, MONTH(date) m, SUM(actual_qty)/1000 total
            FROM production_daily WHERE date BETWEEN %s AND %s
            """ + clause + " GROUP BY y, m ORDER BY y, m",
            (date(base.year - 1, 1, 1), date(base.year, 12, 31), *values),
        )
        yoy_map = {(int(row["y"]), int(row["m"])): scalar(row.get("total")) for row in yoy_rows}
        for month in range(1, 13):
            current_total = yoy_map.get((base.year, month))
            previous_total = yoy_map.get((base.year - 1, month))
            monthly_yoy.append({
                "month": f"{month}월",
                "current": round(current_total, 1) if current_total is not None else None,
                "previous": round(previous_total, 1) if previous_total is not None else None,
            })
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
        "monthlyPlan": monthly_plan_actual,
        "mix": [{"name": row["name"], "value": round(scalar(row["value"]) / mix_total * 100, 1)} for row in mix_rows],
        "wipMix": wip_mix,
        "topItems": top_items,
        "underItems": under_items,
        "overItems": over_items,
        "insights": insights,
        "monthlyYoy": monthly_yoy,
    })


def shift_month(year: int, month: int, delta: int) -> tuple[int, int]:
    """(연, 월)에 delta개월을 더한 (연, 월) 반환."""
    index = year * 12 + (month - 1) + delta
    return index // 12, index % 12 + 1


@app.get("/api/v1/production/items")
def production_item_options(
    factory: str = "전사",
    requested_date: date | None = Query(None, alias="date"),
) -> dict[str, Any]:
    """품목 추이·비교 섹션의 선택지 — 선택 공장의 최근 12개월 실적 상위 300개 품목.

    제품유형(category)을 함께 내려 프런트가 '유형 선행 필터 → 품목 검색' 순서로
    목록을 좁힐 수 있게 한다. 유형 필터가 걸리면 후보가 크게 줄기 때문에 상위
    100개로는 특정 유형이 통째로 비는 경우가 생겨 300개로 넓혔다.
    """
    max_row = fetch_one("SELECT MAX(date) max_date FROM production_daily") or {}
    base = bounded_base_date(requested_date, max_row.get("max_date"))
    codes = PRODUCTION_FACTORY_CODES.get(factory, (factory,))
    placeholders = ",".join(["%s"] * len(codes))
    rows = fetch_all(
        f"""
        SELECT item_code code, MAX(item_name) name,
               MAX(CASE WHEN category2 IN ('IC','MY','FM','SN') THEN category2 ELSE 'ETC' END) category,
               SUM(actual_qty)/1000 actual
        FROM production_daily WHERE date BETWEEN %s AND %s AND factory IN ({placeholders})
        GROUP BY item_code ORDER BY actual DESC LIMIT 300
        """,
        (base - timedelta(days=365), base, *codes),
    )
    return json_safe({"baseDate": base, "items": [
        {
            "code": str(row["code"]),
            "name": row.get("name") or str(row["code"]),
            "category": str(row.get("category") or "ETC"),
            "actual": round(scalar(row.get("actual")), 1),
        }
        for row in rows
    ]})


@app.get("/api/v1/production/item-trend")
def production_item_trend(
    items: str,
    factory: str = "전사",
    requested_date: date | None = Query(None, alias="date"),
    mode: str = "month",
    date_from: date | None = Query(None),
    date_to: date | None = Query(None),
) -> dict[str, Any]:
    """선택 품목(최대 5개)의 실적 추이 — x축이 조회 모드의 시간 범위·단위를 그대로 따른다.

    month : 해당 월 1일~말일 (일 단위)
    range : 지정 기간 (일 단위)
    year  : 해당 연도 1~12월 (월 단위)

    같은 탭의 다른 차트(일일 생산량·Burn-up·월별 전년비)와 축을 맞춰 나란히 읽히게
    한다. 각 지점에 전년 동기 값(prevYear)을 붙여 품목 전년비 섹션이 같은 응답을
    재사용한다 — 품목간 비교 섹션은 actual만 그리고 prevYear는 쓰지 않는다.
    """
    if mode not in PRODUCTION_MODES:
        raise HTTPException(status_code=400, detail=f"지원하지 않는 조회 모드입니다: {mode}")
    codes_selected = [code.strip() for code in items.split(",") if code.strip()][:5]
    if not codes_selected:
        raise HTTPException(status_code=400, detail="items에 품목 코드를 1~5개 지정하세요.")
    max_row = fetch_one("SELECT MAX(date) max_date FROM production_daily") or {}
    data_max = normalize_date(max_row.get("max_date"))
    base = bounded_base_date(requested_date, max_row.get("max_date"))

    # 연간은 (연, 월) 키의 12개월, 월간·기간별은 date 키의 일 단위.
    monthly = mode == "year"
    if monthly:
        period_keys: list[Any] = [(base.year, month) for month in range(1, 13)]
        labels = [f"{month}월" for month in range(1, 13)]
        prev_keys: list[Any] = [(base.year - 1, month) for month in range(1, 13)]
        fetch_from, fetch_to = date(base.year - 1, 1, 1), date(base.year, 12, 31)
    else:
        period_from, period_to = resolve_production_period(mode, base, date_from, date_to, data_max)
        span = (period_to - period_from).days + 1
        period_keys = [period_from + timedelta(days=offset) for offset in range(span)]
        labels = [key.strftime("%m.%d") for key in period_keys]
        # 전년 동일자 — 2/29는 previous_year_date가 2/28로 클램프한다.
        prev_keys = [previous_year_date(key) for key in period_keys]
        fetch_from, fetch_to = date(period_from.year - 1, period_from.month, 1), period_to
    if data_max is not None:
        fetch_to = min(fetch_to, data_max)

    factory_codes = PRODUCTION_FACTORY_CODES.get(factory, (factory,))
    factory_ph = ",".join(["%s"] * len(factory_codes))
    item_ph = ",".join(["%s"] * len(codes_selected))
    period_select = "YEAR(date) y, MONTH(date) m" if monthly else "date d"
    period_group = "item_code, y, m" if monthly else "item_code, d"
    rows = fetch_all(
        f"""
        SELECT item_code code, MAX(item_name) name, {period_select},
               SUM(actual_qty)/1000 actual
        FROM production_daily
        WHERE date BETWEEN %s AND %s AND factory IN ({factory_ph}) AND item_code IN ({item_ph})
        GROUP BY {period_group}
        """,
        (fetch_from, fetch_to, *factory_codes, *codes_selected),
    )
    by_item: dict[str, dict[str, Any]] = {}
    for row in rows:
        entry = by_item.setdefault(str(row["code"]), {"name": str(row["code"]), "values": {}})
        key = (int(row["y"]), int(row["m"])) if monthly else normalize_date(row.get("d"))
        if key is not None:
            entry["values"][key] = scalar(row.get("actual"))
        if row.get("name"):
            entry["name"] = str(row["name"])
    output_items = []
    for code in codes_selected:
        entry = by_item.get(code, {"name": code, "values": {}})
        values: dict[Any, float] = entry["values"]
        series = []
        for index, key in enumerate(period_keys):
            actual_value = values.get(key)
            prev_year_value = values.get(prev_keys[index])
            series.append({
                "period": labels[index],
                "actual": round(actual_value, 2) if actual_value is not None else None,
                "prevYear": round(prev_year_value, 2) if prev_year_value is not None else None,
            })
        latest = None
        indices_with_data = [i for i, key in enumerate(period_keys) if values.get(key) is not None]
        if indices_with_data:
            index = indices_with_data[-1]
            current_value = values[period_keys[index]]
            prev_period_value = values.get(period_keys[index - 1]) if index > 0 else None
            prev_year_value = values.get(prev_keys[index])
            latest = {
                "period": labels[index],
                "actual": round(current_value, 2),
                "prevChange": rate_change(current_value, prev_period_value) if prev_period_value else None,
                "yoyChange": rate_change(current_value, prev_year_value) if prev_year_value else None,
            }
        output_items.append({"code": code, "name": entry["name"], "series": series, "latest": latest})
    return json_safe({
        "baseDate": base,
        "mode": mode,
        "granularity": "month" if monthly else "day",
        "items": output_items,
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


# 사용자 화면 용어 — 통계 용어(run rule/CUSUM) 대신 현업이 바로 읽히는 말을 쓴다.
BAND_SIGNAL_LABELS = {
    "alert": "반복 이탈",   # run rule 통과: 연속 2일 이상 또는 최근 7일 내 3회 이상
    "watch": "단발 이탈",   # 하루만 벗어남 — 90% 밴드에서는 통계적으로 흔함
    "drift": "지속 편차",   # 밴드 안이지만 여러 날 계속 한쪽으로 치우침(CUSUM)
}


def band_rule_evaluation(
    factory: str,
    base: date,
    window_days: int = 30,
    recent_days: int = 7,
) -> dict[str, Any]:
    """정상범주 이탈에 판정 규칙을 적용한 결과.

    P05~P95는 90% 구간이라 정상 상태에서도 하루 판정의 10%는 밴드를 벗어난다.
    단일일 이탈을 그대로 경보하면 배너가 상시 경고가 되어 아무도 안 보게 되므로
    (알람 피로), anomaly_rules_service의 단발/반복/지속편차 판정을 그대로 쓴다.

    판정은 반드시 물리 공장 시계열 단위로 수행한다 — 집계 후 판정하면 한 공장의
    연속 이탈이 다른 공장 값에 상쇄돼 묻힌다. 조회창은 recent_days보다 길게 잡아
    창 경계에 걸친 연속 이탈과 편차 누적을 인지하게 한다.
    """
    empty = {"alertCount": 0, "watchCount": 0, "driftCount": 0, "signals": [], "flags": []}
    clause, values = factory_clause(factory)
    rows = fetch_all(
        """
        SELECT factory, target, pred_date, band_status, band_position, actual_value, pred_value
        FROM prediction_log WHERE pred_date BETWEEN %s AND %s
        """ + clause + " ORDER BY factory, target, pred_date",
        (base - timedelta(days=window_days - 1), base, *values),
    )
    if not rows:
        return empty
    try:
        import pandas as pd

        service = import_core("app.services.anomaly_rules_service")
        result = service.evaluate_band_rules(pd.DataFrame(rows), base, recent_days=recent_days)
    except Exception as exc:
        # 판정 실패가 화면 전체를 막지 않도록 — 신호 없음으로 강등하고 로그만 남긴다.
        logger.error("Band rule evaluation failed.", exc_info=(type(exc), exc, exc.__traceback__))
        return empty

    row_flags = result.get("row_flags")
    flags: list[dict[str, Any]] = []
    if row_flags is not None and not row_flags.empty:
        for row in row_flags.to_dict("records"):
            flag_date = normalize_date(row.get("pred_date"))
            if flag_date is None:
                continue
            flags.append({
                "factory": str(row.get("factory")),
                "target": str(row.get("target")),
                "date": flag_date,
                "severity": str(row.get("severity")),
                "rules": str(row.get("rules") or ""),
            })

    # 같은 (공장, 지표)의 연속 이탈은 날짜마다 한 건씩 잡히므로 시리즈 단위로 묶는다 —
    # "남양주1 연료"가 4줄 반복되면 목록만 길어지고 조치 대상 수를 오해하게 된다.
    signals: list[dict[str, Any]] = []
    latest_alert: dict[tuple[str, str], dict[str, Any]] = {}
    for flag in flags:
        if flag["severity"] != "alert":
            continue
        key = (flag["factory"], flag["target"])
        existing = latest_alert.get(key)
        if existing is None or flag["date"] > existing["date"]:
            latest_alert[key] = {
                "kind": "alert",
                "label": BAND_SIGNAL_LABELS["alert"],
                "factory": flag["factory"],
                "target": flag["target"],
                "date": flag["date"],
                "detail": flag["rules"],
            }
    signals.extend(latest_alert.values())
    for drift in result.get("drift_signals", []):
        bias = drift.get("mean_bias_pct")
        direction = "높음" if drift.get("direction") == "over" else "낮음"
        detail = f"{drift.get('days', 0)}일 연속 예측보다 {direction}"
        if bias is not None:
            detail += f" (평균 {bias:+.1f}%)"
        signals.append({
            "kind": "drift",
            "label": BAND_SIGNAL_LABELS["drift"],
            "factory": str(drift.get("factory")),
            "target": str(drift.get("target")),
            "date": drift.get("start_date"),
            "detail": detail,
        })
    # 최신 신호부터 — 경보를 지속 편차보다 앞에 둔다.
    signals.sort(key=lambda item: (item["kind"] != "alert", -(item["date"].toordinal() if item.get("date") else 0)))
    return {
        "alertCount": int(result.get("n_alert", 0)),
        "watchCount": int(result.get("n_watch", 0)),
        "driftCount": len(result.get("drift_signals", [])),
        "signals": signals[:8],
        "flags": flags,
    }


def band_alert_banner(evaluation: dict[str, Any]) -> dict[str, Any]:
    """판정 결과를 대시보드 배너 문구로 — 단발 이탈만 있으면 경고로 올리지 않는다."""
    alert_count = evaluation.get("alertCount", 0)
    drift_count = evaluation.get("driftCount", 0)
    watch_count = evaluation.get("watchCount", 0)
    if alert_count:
        parts = [f"반복 이탈 {alert_count}건"]
        if drift_count:
            parts.append(f"지속 편차 {drift_count}건")
        return {
            "level": "warning",
            "title": f"조치 필요 — {' · '.join(parts)}",
            "description": "같은 방향 이탈이 이틀 이상 이어졌거나 최근 7일 안에 3회 이상 반복된 건입니다. AI 예측 화면에서 원인을 확인하세요.",
            **evaluation,
        }
    if drift_count:
        return {
            "level": "warning",
            "title": f"점검 권장 — 지속 편차 {drift_count}건",
            "description": "정상범주 안이지만 여러 날 계속 한쪽으로 치우쳐 있습니다. 하루만 보면 정상이라 놓치기 쉬운 유형입니다.",
            **evaluation,
        }
    if watch_count:
        return {
            "level": "normal",
            "title": "이상 신호 없음",
            "description": f"단발 이탈 {watch_count}건이 있으나 정상범주(90%) 특성상 흔한 수준으로, 반복되지 않아 조치 대상이 아닙니다.",
            **evaluation,
        }
    return {
        "level": "normal",
        "title": "AI 이상 신호 없음",
        "description": "최근 7일 예측 밴드 기준으로 반복 이탈과 지속 편차가 없습니다.",
        **evaluation,
    }


@app.get("/api/v1/predictions")
def predictions(
    factory: str = "전사",
    requested_date: date | None = Query(None, alias="date"),
) -> dict[str, Any]:
    max_row = fetch_one("SELECT MAX(pred_date) max_date FROM prediction_log") or {}
    base = bounded_base_date(requested_date, max_row.get("max_date"))
    rows = aggregate_prediction_rows(factory, base)
    # 대시보드 배너와 같은 판정 규칙을 쓴다 — 두 화면이 다른 기준으로 다른 건수를
    # 보여주면 어느 쪽을 믿어야 할지 알 수 없다.
    evaluation = band_rule_evaluation(factory, base)
    status = {"normal": 0, "warning": 0, "alert": 0, "unknown": 0, "label": "정상"}
    for row in rows:
        if row["band_status"] == "inside":
            status["normal"] += 1
        elif row["band_status"] in ("over", "under"):
            status["alert"] += 1
        else:
            status["unknown"] += 1
    status.update({
        "repeated": evaluation["alertCount"],
        "single": evaluation["watchCount"],
        "drift": evaluation["driftCount"],
    })
    status["label"] = (
        "조치 필요" if evaluation["alertCount"]
        else "점검 권장" if evaluation["driftCount"]
        else "미확정" if status["unknown"] and not status["normal"]
        else "정상"
    )
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
    return json_safe({
        "baseDate": base,
        "status": status,
        "latest": latest,
        "model": model,
        "signals": evaluation["signals"],
    })


PREDICTION_TARGETS = ("전력", "연료", "용수")


@app.get("/api/v1/predictions/gap")
def prediction_gap_series(
    factory: str = "전사",
    target: str = "전력",
    requested_date: date | None = Query(None, alias="date"),
    date_from: date | None = Query(None),
    date_to: date | None = Query(None),
) -> dict[str, Any]:
    """예측 대비 실측 괴리 시계열 — 기간을 지정해 편향 추이를 본다.

    단일 시점 판정만으로는 '매일 조금씩 계속 높은' 유형을 못 본다. 절대값(예측·실측)과
    괴리율(%)을 함께 돌려주어 화면이 2단으로 그린다: 위는 수준, 아래는 편향 방향.
    """
    if target not in PREDICTION_TARGETS:
        raise HTTPException(status_code=400, detail=f"지원하지 않는 지표입니다: {target}")
    max_row = fetch_one("SELECT MAX(pred_date) max_date FROM prediction_log") or {}
    base = bounded_base_date(requested_date, max_row.get("max_date"))
    window_to = date_to or base
    window_from = date_from or (window_to - timedelta(days=29))
    if window_from > window_to:
        raise HTTPException(status_code=400, detail="시작일은 종료일보다 늦을 수 없습니다.")
    if (window_to - window_from).days > 365:
        raise HTTPException(status_code=400, detail="조회 기간은 최대 366일입니다.")

    rows = aggregate_prediction_rows(
        factory, window_to, date_from=window_from, target=target, limit=400,
    )
    series = []
    for row in sorted(rows, key=lambda item: item["pred_date"]):
        predicted = optional_scalar(row.get("predicted"))
        actual = optional_scalar(row.get("actual"))
        lower = optional_scalar(row.get("lower_band"))
        upper = optional_scalar(row.get("upper_band"))
        gap = actual - predicted if (actual is not None and predicted is not None) else None
        gap_pct = (gap / predicted * 100) if (gap is not None and predicted) else None
        series.append({
            "date": row["pred_date"].strftime("%m.%d"),
            "fullDate": row["pred_date"],
            "predicted": round(predicted, 2) if predicted is not None else None,
            "actual": round(actual, 2) if actual is not None else None,
            "lower": round(lower, 2) if lower is not None else None,
            "upper": round(upper, 2) if upper is not None else None,
            "band": [round(lower, 2), round(upper, 2)] if (lower is not None and upper is not None) else None,
            "gap": round(gap, 2) if gap is not None else None,
            "gapPct": round(gap_pct, 1) if gap_pct is not None else None,
            "status": row["band_status"],
        })
    measured = [point for point in series if point["gapPct"] is not None]
    outside = [point for point in series if point["status"] in ("over", "under")]
    summary = {
        "days": len(series),
        "measuredDays": len(measured),
        "outsideDays": len(outside),
        # 평균 괴리율은 편향(한쪽 쏠림), 평균 절대 괴리율은 정확도 — 둘은 다른 질문에 답한다.
        "meanGapPct": round(sum(point["gapPct"] for point in measured) / len(measured), 1) if measured else None,
        "meanAbsGapPct": round(sum(abs(point["gapPct"]) for point in measured) / len(measured), 1) if measured else None,
    }
    return json_safe({
        "factory": factory, "target": target,
        "dateFrom": window_from, "dateTo": window_to,
        "series": series, "summary": summary,
    })


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
    factory = None if payload.factory in (None, "", "전사", "전체") else payload.factory
    if factory is not None and factory not in FACTORY_MEMBERS and factory not in PREDICTION_FACTORIES:
        raise HTTPException(status_code=400, detail="v5.3 모델 학습 대상 공장이 아닙니다. (경산은 예측 미지원)")
    service = import_core("app.services.usage_prediction_v5_service")
    result = service.generate_missing_prediction_history(factory, payload.date_from, payload.date_to)
    return json_safe(result)


@app.post("/api/v1/predictions/backfill-actuals")
def backfill_actuals(request: Request) -> dict[str, Any]:
    """prediction_log 실측값 역채움 (관리자 전용, 기존 서비스 위임)."""
    require_admin(request)
    service = import_core("app.services.usage_prediction_v5_service")
    updated = service.backfill_actuals()
    return {"updated_rows": int(updated)}


@app.get("/api/v1/predictions/monitoring")
def prediction_monitoring(factory: str = "전사") -> dict[str, Any]:
    """모델 성능/offset 감지 요약 — legacy 예측 이력 탭의 모니터링 패널 이식.

    실측값이 있는 예측 이력을 prediction_monitoring_service에 위임해
    bias·패턴 일치·offset 판정을 반환한다.
    """
    service = import_core("app.services.prediction_monitoring_service")
    members = prediction_factory_members(factory)
    placeholders = ",".join(["%s"] * len(members))
    rows = fetch_all(
        f"""
        SELECT factory, pred_date, target, pred_value, actual_value
        FROM prediction_log
        WHERE actual_value IS NOT NULL AND factory IN ({placeholders})
        ORDER BY pred_date DESC LIMIT 5000
        """,
        tuple(members),
    )
    frame = service.pd.DataFrame(rows)
    monitoring = service.build_prediction_monitoring_summary(frame)
    overall = service.get_monitoring_overall_status(monitoring)
    return json_safe({"overall": overall, "rows": _table_records(monitoring)})


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


FEATURE_IMPORTANCE_TARGETS = ("전력", "연료", "용수")


@app.get("/api/v1/model/feature-importance")
def model_feature_importance(factory: str, target: str = "전력") -> dict[str, Any]:
    """활성 v5 모델의 변수 영향도 Top 5 — legacy 예측 화면의 한국어 설명 이식."""
    if factory not in PREDICTION_FACTORIES:
        raise HTTPException(status_code=400, detail="변수 영향도는 개별 공장 단위로만 제공합니다.")
    if target not in FEATURE_IMPORTANCE_TARGETS:
        raise HTTPException(status_code=400, detail="target은 전력·연료·용수 중 하나여야 합니다.")
    service = import_core("app.services.v5_explainability")
    items = service.get_v5_feature_importance(factory, target, top_n=5)
    return json_safe({
        "factory": factory,
        "target": target,
        "items": items,
        "summary": service.explain_top_features_korean(items),
    })


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


def build_mail_report(period: str, ref_date: date | None):
    """주기별 리포트 빌드 — 발송·미리보기가 같은 tools/mail 파이프라인을 공유한다."""
    if period == "weekly":
        return import_core("tools.mail.period_report_builder").build_weekly_report()
    if period == "monthly":
        return import_core("tools.mail.period_report_builder").build_monthly_report()
    return import_core("tools.mail.daily_report_builder").build_daily_report(ref_date=ref_date)


def inline_images_to_data_uris(html: str, inline_images: list[Any]) -> str:
    """메일 본문의 cid: 참조를 data URI로 치환 — 브라우저 미리보기용."""
    for image in inline_images or []:
        encoded = base64.b64encode(image.data).decode("ascii")
        html = html.replace(f"cid:{image.cid}", f"data:image/{image.mime_subtype};base64,{encoded}")
    return html


@app.get("/api/v1/mail/preview")
def mail_preview(
    request: Request,
    period: str = "daily",
    requested_date: date | None = Query(None, alias="date"),
) -> dict[str, Any]:
    """메일 리포트 HTML 미리보기 (관리자 전용) — 발송 없이 본문만 생성한다."""
    require_admin(request)
    normalized = normalize_mail_period(period)
    try:
        report = build_mail_report(normalized, requested_date)
        html = inline_images_to_data_uris(report.html, report.inline_images)
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Mail report preview failed (period=%s).", normalized, exc_info=True)
        raise HTTPException(status_code=502, detail="리포트 생성에 실패했습니다. 서버 로그를 확인하세요.") from exc
    return json_safe({
        "period": normalized,
        "label": MAIL_PERIOD_LABELS[normalized],
        "subject": report.subject,
        "refDate": report.ref_date,
        "recordCount": report.record_count,
        "html": html,
    })


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
        report = build_mail_report(period, payload.ref_date)
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
