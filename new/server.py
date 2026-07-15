"""FastAPI bridge for the existing AI-Elite-BEMS Python/MySQL core.

The browser talks directly to this process on port 8000 so the original
client-IP based admin/viewer policy remains meaningful.  Read endpoints use
parameterized SQL.  Model, report and upload actions delegate to the existing
Python services under BEMS_CORE_ROOT instead of reimplementing their logic.
"""

from __future__ import annotations

import importlib
import math
import os
import socket
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import pymysql
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CORE_ROOT = Path(os.getenv("BEMS_CORE_ROOT", PROJECT_ROOT.parent / "AI-Elite-BEMS")).resolve()
load_dotenv(CORE_ROOT / ".env")

app = FastAPI(title="AI Elite BEMS API", version="1.0.0", docs_url="/api/docs", redoc_url=None)
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"^https?://(localhost|127\.0\.0\.1|[A-Za-z0-9._-]+):3000$",
    allow_credentials=False,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "Accept"],
)


FACTORY_MEMBERS = {
    "전사": [],
    "전체": [],
    "남양주": ["남양주1", "남양주2"],
}
DISPLAY_FACTORIES = ["남양주", "김해", "광주", "논산", "경산"]


def db_connect() -> pymysql.Connection:
    try:
        return pymysql.connect(
            host=os.getenv("DB_HOST", "127.0.0.1"),
            port=int(os.getenv("DB_PORT", "3306")),
            user=os.getenv("DB_ADMIN_USER") or os.getenv("DB_USER", "root"),
            password=os.getenv("DB_ADMIN_PASSWORD") or os.getenv("DB_PASSWORD", ""),
            database=os.getenv("DB_NAME", "bems"),
            charset="utf8mb4",
            cursorclass=pymysql.cursors.DictCursor,
            autocommit=True,
            connect_timeout=5,
            read_timeout=30,
        )
    except Exception as exc:  # pragma: no cover - depends on local MySQL
        raise HTTPException(status_code=503, detail=f"MySQL 연결 실패: {exc}") from exc


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
    addresses = {"127.0.0.1", "::1"}
    try:
        _, _, found = socket.gethostbyname_ex(socket.gethostname())
        addresses.update(found)
    except OSError:
        pass
    return addresses


def client_is_admin(request: Request) -> bool:
    client_ip = request.client.host if request.client else ""
    explicit = {item.strip() for item in os.getenv("BEMS_ADMIN_IPS", "").split(",") if item.strip()}
    return client_ip in local_addresses() or client_ip in explicit


def require_admin(request: Request) -> None:
    if not client_is_admin(request):
        raise HTTPException(status_code=403, detail="호스트 PC 관리자 전용 기능입니다.")


def import_core(module: str):
    if not CORE_ROOT.exists():
        raise HTTPException(status_code=503, detail=f"기존 BEMS 경로를 찾을 수 없습니다: {CORE_ROOT}")
    root_text = str(CORE_ROOT)
    if root_text not in sys.path:
        sys.path.insert(0, root_text)
    try:
        return importlib.import_module(module)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"기존 BEMS 서비스 로드 실패: {exc}") from exc


@app.exception_handler(Exception)
async def unhandled_error(_: Request, exc: Exception):
    return JSONResponse(status_code=500, content={"detail": str(exc)})


@app.get("/api/v1/health")
def health() -> dict[str, Any]:
    row = fetch_one("SELECT MAX(updated_at) AS updated_at, COUNT(*) AS records FROM energy_daily")
    return {"status": "ok", "database": "mysql", "coreRoot": str(CORE_ROOT), **json_safe(row or {})}


@app.get("/api/v1/session")
def session(request: Request) -> dict[str, str]:
    client_ip = request.client.host if request.client else "unknown"
    return {
        "role": "admin" if client_is_admin(request) else "viewer",
        "clientIp": client_ip,
        "serverName": socket.gethostname(),
    }


def aggregate_period(factory: str, date_from: date, date_to: date) -> dict[str, float]:
    clause, values = factory_clause(factory)
    row = fetch_one(
        """
        SELECT COALESCE(SUM(total_power_kwh),0) power,
               COALESCE(SUM(fuel_nm3),0) fuel,
               COALESCE(SUM(water_ton),0) water,
               COALESCE(SUM(mix_prod_kg),0) production
        FROM energy_daily WHERE date BETWEEN %s AND %s
        """ + clause,
        (date_from, date_to, *values),
    ) or {}
    return {key: scalar(row.get(key)) for key in ("power", "fuel", "water", "production")}


def rate_change(current: float, previous: float) -> float:
    return round((current / previous - 1) * 100, 1) if previous else 0.0


@app.get("/api/v1/dashboard")
def dashboard(factory: str = "전사", requested_date: date | None = Query(None, alias="date")) -> dict[str, Any]:
    max_row = fetch_one("SELECT MAX(date) max_date, MAX(updated_at) updated_at FROM energy_daily") or {}
    base = min(requested_date or date.today(), max_row.get("max_date") or date.today())
    month_start = base.replace(day=1)
    prev_base = base.replace(year=base.year - 1)
    prev_start = prev_base.replace(day=1)
    current = aggregate_period(factory, month_start, base)
    previous = aggregate_period(factory, prev_start, prev_base)
    prod_ton = current["production"] / 1000
    prev_prod_ton = previous["production"] / 1000

    def intensity(values: dict[str, float], key: str, tonnes: float) -> float:
        return values[key] / tonnes if tonnes > 0 else 0.0

    metrics = []
    metric_specs = [
        ("power", "전력 원단위", "kWh/ton", "blue"),
        ("fuel", "연료 원단위", "Nm³/ton", "violet"),
        ("water", "용수 원단위", "ton/ton", "cyan"),
    ]
    for key, label, unit, tone in metric_specs:
        value = intensity(current, key, prod_ton)
        prev_value = intensity(previous, key, prev_prod_ton)
        metrics.append({"id": key, "label": label, "value": round(value, 2), "unit": unit, "change": rate_change(value, prev_value), "tone": tone})
    metrics.append({"id": "production", "label": "누계 생산량", "value": round(prod_ton, 1), "unit": "ton", "change": rate_change(prod_ton, prev_prod_ton), "tone": "emerald"})

    clause, values = factory_clause(factory, "e.factory")
    trend_rows = fetch_all(
        """
        SELECT e.date, SUM(e.total_power_kwh)/1000 actual, SUM(e.mix_prod_kg)/1000 production
        FROM energy_daily e WHERE e.date BETWEEN %s AND %s
        """ + clause + " GROUP BY e.date ORDER BY e.date",
        (base - timedelta(days=6), base, *values),
    )
    pred_clause, pred_values = factory_clause(factory, "factory")
    pred_rows = fetch_all(
        """
        SELECT pred_date, SUM(pred_value)/1000 predicted, SUM(pred_p05)/1000 lower_band,
               SUM(pred_p95)/1000 upper_band
        FROM prediction_log WHERE target='전력' AND pred_date BETWEEN %s AND %s
        """ + pred_clause + " GROUP BY pred_date",
        (base - timedelta(days=6), base, *pred_values),
    )
    pred_map = {row["pred_date"]: row for row in pred_rows}
    trend = []
    for row in trend_rows:
        pred = pred_map.get(row["date"], {})
        actual = scalar(row.get("actual"))
        predicted = scalar(pred.get("predicted"), actual)
        trend.append({
            "date": row["date"].strftime("%m.%d"),
            "actual": round(actual, 2),
            "predicted": round(predicted, 2),
            "lower": round(scalar(pred.get("lower_band"), predicted * .93), 2),
            "upper": round(scalar(pred.get("upper_band"), predicted * 1.07), 2),
            "production": round(scalar(row.get("production")), 1),
        })

    yoy_rows = fetch_all(
        """
        SELECT YEAR(date) y, MONTH(date) m, SUM(total_power_kwh) power, SUM(mix_prod_kg)/1000 production
        FROM energy_daily WHERE YEAR(date) IN (%s,%s)
        """ + factory_clause(factory)[0] + " GROUP BY y,m ORDER BY y,m",
        (base.year - 1, base.year, *factory_clause(factory)[1]),
    )
    yoy_map = {(int(row["y"]), int(row["m"])): scalar(row["power"]) / scalar(row["production"], 1) for row in yoy_rows}
    yoy = [{"month": f"{month}월", "current": round(yoy_map.get((base.year, month), 0), 1), "previous": round(yoy_map.get((base.year - 1, month), 0), 1)} for month in range(max(1, base.month - 5), base.month + 1)]

    comparisons = []
    for display_factory in DISPLAY_FACTORIES:
        current_factory = aggregate_period(display_factory, month_start, base)
        previous_factory = aggregate_period(display_factory, prev_start, prev_base)
        cur_ton = current_factory["production"] / 1000
        prv_ton = previous_factory["production"] / 1000
        cur_value = intensity(current_factory, "power", cur_ton)
        prv_value = intensity(previous_factory, "power", prv_ton)
        if cur_value:
            comparisons.append({"factory": display_factory, "value": round(cur_value, 1), "change": rate_change(cur_value, prv_value)})

    event_clause, event_values = factory_clause(factory)
    events = fetch_all(
        "SELECT event_date, factory, tag, severity, note FROM event_annotation WHERE 1=1" + event_clause + " ORDER BY event_date DESC, id DESC LIMIT 5",
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
def energy(factory: str = "전사") -> dict[str, Any]:
    max_row = fetch_one("SELECT MAX(date) max_date FROM energy_daily") or {}
    base = max_row.get("max_date") or date.today()
    clause, values = factory_clause(factory)
    rows = fetch_all(
        """
        SELECT date, SUM(total_power_kwh)/1000 power, SUM(fuel_nm3)/1000 fuel,
               SUM(water_ton)/1000 water, SUM(wastewater_ton)/1000 wastewater
        FROM energy_daily WHERE date BETWEEN %s AND %s
        """ + clause + " GROUP BY date ORDER BY date",
        (base - timedelta(days=29), base, *values),
    )
    equipment = fetch_one(
        """
        SELECT SUM(freezing_power_kwh) freezing, SUM(air_compressor_kwh) compressor,
               SUM(total_power_kwh) total_power
        FROM energy_daily WHERE date BETWEEN %s AND %s
        """ + clause,
        (base.replace(day=1), base, *values),
    ) or {}
    total = scalar(equipment.get("total_power"), 1)
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
        SELECT factory, SUM(total_power_kwh)/1000 power, SUM(fuel_nm3)/1000 fuel, SUM(water_ton)/1000 water
        FROM energy_daily WHERE date BETWEEN %s AND %s GROUP BY factory ORDER BY factory
        """,
        (base.replace(day=1), base),
    )
    combined: dict[str, dict[str, float | str]] = {}
    for row in factory_rows:
        name = "남양주" if row["factory"] in ("남양주1", "남양주2") else row["factory"]
        target = combined.setdefault(name, {"factory": name, "power": 0.0, "fuel": 0.0, "water": 0.0})
        for key in ("power", "fuel", "water"):
            target[key] = scalar(target[key]) + scalar(row.get(key))
    return json_safe({
        "daily": [{**row, "date": row["date"].strftime("%m.%d")} for row in rows],
        "equipment": equipment_rows,
        "factories": list(combined.values()),
    })


@app.get("/api/v1/production")
def production(factory: str = "전사") -> dict[str, Any]:
    max_row = fetch_one("SELECT MAX(date) max_date FROM production_daily") or {}
    base = max_row.get("max_date") or date.today()
    prod_factory = "남양주" if factory in ("남양주1", "남양주2") else factory
    clause, values = factory_clause(prod_factory)
    if prod_factory == "남양주":
        clause, values = " AND factory=%s", ["남양주"]
    summary = fetch_one(
        """
        SELECT SUM(actual_qty) actual, COUNT(DISTINCT item_code) items
        FROM production_daily WHERE YEAR(date)=%s AND MONTH(date)=%s
        """ + clause,
        (base.year, base.month, *values),
    ) or {}
    plan_row = fetch_one(
        """
        SELECT SUM(planned_qty) plan FROM (
          SELECT factory,item_code,MAX(planned_qty) planned_qty FROM production_daily
          WHERE YEAR(date)=%s AND MONTH(date)=%s
        """ + clause + " GROUP BY factory,item_code) p",
        (base.year, base.month, *values),
    ) or {}
    actual = scalar(summary.get("actual"))
    plan = scalar(plan_row.get("plan"))
    elapsed = base.day / 31
    forecast = actual / elapsed if elapsed else actual
    daily = fetch_all(
        """
        SELECT date,
          SUM(CASE WHEN category2='IC' THEN actual_qty ELSE 0 END) IC,
          SUM(CASE WHEN category2='MY' THEN actual_qty ELSE 0 END) MY,
          SUM(CASE WHEN category2='FM' THEN actual_qty ELSE 0 END) FM,
          SUM(CASE WHEN category2='SN' THEN actual_qty ELSE 0 END) SN
        FROM production_daily WHERE YEAR(date)=%s AND MONTH(date)=%s
        """ + clause + " GROUP BY date ORDER BY date",
        (base.year, base.month, *values),
    )
    mix_rows = fetch_all(
        """
        SELECT COALESCE(category2,'기타') name, SUM(actual_qty) value FROM production_daily
        WHERE YEAR(date)=%s AND MONTH(date)=%s
        """ + clause + " GROUP BY category2 ORDER BY value DESC",
        (base.year, base.month, *values),
    )
    mix_total = sum(scalar(row["value"]) for row in mix_rows) or 1
    top_rows = fetch_all(
        """
        SELECT item_name name, MAX(planned_qty) plan, SUM(actual_qty) actual
        FROM production_daily WHERE YEAR(date)=%s AND MONTH(date)=%s
        """ + clause + " GROUP BY item_code,item_name ORDER BY actual DESC LIMIT 10",
        (base.year, base.month, *values),
    )
    return json_safe({
        "summary": {"plan": round(plan), "actual": round(actual), "progress": round(actual / plan * 100, 1) if plan else 0, "pace": round((actual / plan / elapsed) * 100, 1) if plan and elapsed else 0, "forecast": round(forecast), "items": int(summary.get("items") or 0)},
        "daily": [{**row, "date": row["date"].strftime("%m.%d")} for row in daily[-14:]],
        "mix": [{"name": row["name"], "value": round(scalar(row["value"]) / mix_total * 100, 1)} for row in mix_rows],
        "topItems": [{**row, "rate": round(scalar(row["actual"]) / scalar(row["plan"], 1) * 100, 1)} for row in top_rows],
    })


@app.get("/api/v1/predictions")
def predictions(factory: str = "전사") -> dict[str, Any]:
    clause, values = factory_clause(factory)
    rows = fetch_all(
        """
        SELECT pred_date, target, SUM(pred_value)/1000 predicted,
               SUM(pred_p05)/1000 lower_band, SUM(pred_p95)/1000 upper_band,
               SUM(actual_value)/1000 actual,
               CASE WHEN SUM(actual_value) > SUM(pred_p95) THEN 'over'
                    WHEN SUM(actual_value) < SUM(pred_p05) THEN 'under' ELSE 'inside' END band_status
        FROM prediction_log WHERE 1=1
        """ + clause + " GROUP BY pred_date,target ORDER BY pred_date DESC LIMIT 60",
        tuple(values),
    )
    status = {"normal": 0, "warning": 0, "alert": 0, "label": "정상"}
    for row in rows:
        if row["band_status"] == "inside":
            status["normal"] += 1
        else:
            status["alert"] += 1
    status["label"] = "주의" if status["alert"] else "정상"
    model = {"version": "v5.3", "trainedAt": "-", "mape": 0, "coverage": 0, "state": "운영 중"}
    try:
        service = import_core("app.services.usage_prediction_v5_service")
        registry = service.get_model_registry()
        model.update({
            "version": registry.get("active_version", "v5.3"),
            "trainedAt": registry.get("updated_at", "-"),
        })
    except HTTPException:
        pass
    latest = [{
        "date": row["pred_date"], "target": row["target"], "predicted": round(scalar(row["predicted"]), 2),
        "lower": round(scalar(row["lower_band"]), 2), "upper": round(scalar(row["upper_band"]), 2),
        "actual": round(scalar(row["actual"]), 2) if row.get("actual") is not None else None, "status": row["band_status"],
    } for row in rows[:12]]
    return json_safe({"status": status, "latest": latest, "model": model})


class PredictionRequest(BaseModel):
    factory: str
    date: date
    mix_prod_kg: float


@app.post("/api/v1/predictions/run")
def run_prediction(payload: PredictionRequest) -> dict[str, Any]:
    service = import_core("app.services.usage_prediction_v5_service")
    raw = service.predict_v5(payload.factory, payload.date, payload.mix_prod_kg)
    output = []
    for target, row in raw.get("results", {}).items():
        scale = 1000 if target in ("전력", "연료", "용수") else 1
        output.append({
            "date": payload.date,
            "target": target,
            "predicted": scalar(row.get("pred_p50") or row.get("pred")) / scale,
            "lower": scalar(row.get("pred_p05") or row.get("pred")) / scale,
            "upper": scalar(row.get("pred_p95") or row.get("pred")) / scale,
            "actual": scalar(row.get("actual")) / scale if row.get("actual") is not None else None,
            "status": row.get("band_status") or "inside",
        })
    return json_safe({"results": output, "modelPath": raw.get("model_path")})


@app.get("/api/v1/audit")
def audit(_: None = Depends(lambda: None)) -> dict[str, Any]:
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


@app.post("/api/v1/upload")
async def upload(request: Request, file: UploadFile = File(...)) -> dict[str, Any]:
    require_admin(request)
    if not file.filename or Path(file.filename).suffix.lower() not in {".xlsx", ".xls"}:
        raise HTTPException(status_code=400, detail=".xlsx 또는 .xls 파일만 업로드할 수 있습니다.")
    content = await file.read()
    if len(content) > 50 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="파일 크기는 50MB 이하여야 합니다.")
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


@app.post("/api/v1/reports/generate")
def generate_report(payload: ReportRequest, request: Request) -> dict[str, Any]:
    require_admin(request)
    agent_service = import_core("app.services.ai_db_service")
    report_service = import_core("app.services.ai_report_service")
    content = agent_service.run_agent_report(payload.factory, payload.year, payload.month)
    if not report_service.save_report(payload.factory, payload.year, payload.month, content):
        raise HTTPException(status_code=500, detail="보고서 DB 저장에 실패했습니다.")
    return {"updated_at": datetime.now().strftime("%Y-%m-%d %H:%M"), "content": content}