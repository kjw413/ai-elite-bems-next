"""
Savings Theme Service
=====================
에너지 절감 테마(savings_theme)와 월별 계획/실적(savings_record) CRUD.

target_service.py 의 절감 목표 저장 패턴(연/공장 단위 조회 + 항목 일괄 upsert)과
monthly_input_service.py 의 "값이 전부 비면 삭제" 관례를 따른다.

이 서비스가 다루지 않는 것 — 의도적 분리:
  · 절감'금액' — 저장하지 않는다. 해당 월 가중평균 단가(Σ비용÷Σ사용량)를 곱해
    조회 시점에 산출한다. 단가는 energy_daily/energy_monthly 가 원천이고 그
    조립은 server.py 가 맡는다(이미 energy_cost() 가 같은 경로를 쓴다).
  · 검증 판정 — 원단위 전후 비교는 energy_daily·production 집계가 필요해
    server.py 쪽에서 수행한다. 이 모듈은 DB 레코드만 책임진다.

테마는 반드시 **물리 공장**에 귀속된다('전사' 같은 집계 라벨 저장 금지) —
검증이 공장 단위 원단위를 비교하고, 집계 라벨로 저장하면 물리 공장별로
펼칠 수 없어 전사 합계가 이중 계상되기 때문이다.
"""
from __future__ import annotations

import logging
from typing import Any, Iterable

from app.database.db_connection import managed_cursor
from app.services.audit_service import get_current_user

logger = logging.getLogger(__name__)

# 절감량의 단위를 정하고, 금액 환산 시 단가 조인 키가 된다.
# water 는 용수 단가가 시스템 관리 대상이 아니라 금액이 산출되지 않는다
# (2026-07-30 결정 — 용수·폐수 처리비는 비중이 작아 사외 파일 수기 관리).
ENERGY_TYPES: tuple[str, ...] = ("power", "fuel", "water")
ENERGY_TYPE_LABELS: dict[str, str] = {"power": "전력", "fuel": "연료", "water": "용수"}
ENERGY_TYPE_UNITS: dict[str, str] = {"power": "kWh", "fuel": "Nm³", "water": "ton"}
# 단가가 있어 금액 환산이 가능한 에너지원 — energy_cost_service.COST_METRICS 와 동기.
PRICED_ENERGY_TYPES: frozenset[str] = frozenset({"power", "fuel"})

THEME_STATUSES: tuple[str, ...] = ("planned", "ongoing", "done", "dropped")
STATUS_LABELS: dict[str, str] = {
    "planned": "계획", "ongoing": "진행", "done": "완료", "dropped": "중단",
}
# 화면 기본 필터('진행+완료')가 쓰는 집합 — 계획 단계와 중단 건은 성과 집계에서 뺀다.
ACTIVE_STATUSES: tuple[str, ...] = ("ongoing", "done")

THEME_CATEGORIES: tuple[str, ...] = (
    "설비교체", "운전개선", "공정개선", "누설저감", "계약변경", "기타",
)

_THEME_COLUMNS = (
    "factory", "year", "title", "energy_type", "category",
    "status", "start_ym", "owner", "invest_amount", "note",
)


def month_key_valid(month_key: str | None) -> bool:
    """'YYYY-MM' 형식 검사. None/빈 값은 "시행월 미입력"이라 유효로 본다."""
    if month_key in (None, ""):
        return True
    parts = str(month_key).split("-")
    if len(parts) != 2 or len(parts[0]) != 4 or len(parts[1]) != 2:
        return False
    try:
        year, month = int(parts[0]), int(parts[1])
    except ValueError:
        return False
    return 1 <= month <= 12 and 2000 <= year <= 2100


def validate_theme(payload: dict[str, Any]) -> str | None:
    """테마 입력 검증 — 문제가 있으면 사용자용 사유 문자열, 없으면 None."""
    if not str(payload.get("title", "")).strip():
        return "테마명을 입력하세요."
    if not str(payload.get("factory", "")).strip():
        return "공장을 선택하세요."
    if payload.get("energy_type") not in ENERGY_TYPES:
        return f"에너지원은 {'/'.join(ENERGY_TYPES)} 중 하나여야 합니다."
    if payload.get("status") not in THEME_STATUSES:
        return f"상태는 {'/'.join(THEME_STATUSES)} 중 하나여야 합니다."
    if not month_key_valid(payload.get("start_ym")):
        return "시행월은 YYYY-MM 형식이어야 합니다."
    try:
        year = int(payload.get("year"))
    except (TypeError, ValueError):
        return "관리 연도가 올바르지 않습니다."
    if not 2000 <= year <= 2100:
        return "관리 연도가 올바르지 않습니다."
    return None


def _normalized_theme_values(payload: dict[str, Any]) -> tuple[Any, ...]:
    """검증을 통과한 payload → _THEME_COLUMNS 순서의 값 튜플."""
    def optional_text(key: str) -> str | None:
        raw = payload.get(key)
        text = str(raw).strip() if raw is not None else ""
        return text or None

    invest_raw = payload.get("invest_amount")
    try:
        invest = float(invest_raw) if invest_raw not in (None, "") else None
    except (TypeError, ValueError):
        invest = None

    return (
        str(payload["factory"]).strip(),
        int(payload["year"]),
        str(payload["title"]).strip(),
        str(payload["energy_type"]),
        optional_text("category"),
        str(payload["status"]),
        optional_text("start_ym"),
        optional_text("owner"),
        invest,
        optional_text("note"),
    )


def list_themes(
    factories: Iterable[str],
    year: int,
    energy_type: str | None = None,
    statuses: Iterable[str] | None = None,
) -> list[dict[str, Any]]:
    """공장 목록·연도로 테마 조회. factories 는 물리 공장 라벨이어야 한다."""
    factory_list = [str(f) for f in factories]
    if not factory_list:
        return []
    conditions = ["year = %s", f"factory IN ({','.join(['%s'] * len(factory_list))})"]
    params: list[Any] = [year, *factory_list]
    if energy_type in ENERGY_TYPES:
        conditions.append("energy_type = %s")
        params.append(energy_type)
    status_list = [s for s in (statuses or []) if s in THEME_STATUSES]
    if status_list:
        conditions.append(f"status IN ({','.join(['%s'] * len(status_list))})")
        params.extend(status_list)
    with managed_cursor(dictionary=True) as (_conn, cursor):
        cursor.execute(
            f"""
            SELECT id, factory, year, title, energy_type, category, status,
                   start_ym, owner, invest_amount, note, updated_at, changed_by
            FROM savings_theme
            WHERE {' AND '.join(conditions)}
            ORDER BY factory, energy_type, title
            """,
            tuple(params),
        )
        return cursor.fetchall()


def get_theme(theme_id: int) -> dict[str, Any] | None:
    with managed_cursor(dictionary=True) as (_conn, cursor):
        cursor.execute(
            """
            SELECT id, factory, year, title, energy_type, category, status,
                   start_ym, owner, invest_amount, note, updated_at, changed_by
            FROM savings_theme WHERE id = %s
            """,
            (theme_id,),
        )
        rows = cursor.fetchall()
    return rows[0] if rows else None


def records_by_theme(
    theme_ids: Iterable[int], year: int,
) -> dict[int, dict[int, dict[str, float | None]]]:
    """theme_id → {month: {planned, actual}}. 미등록 월은 키 자체가 없다.

    actual 의 None(미입력)과 0(실적 없음)을 구분해 그대로 실어 보낸다 —
    화면이 달성률을 계산할 때 미입력 월을 0으로 세면 안 되기 때문이다.
    """
    ids = [int(theme_id) for theme_id in theme_ids]
    if not ids:
        return {}
    placeholders = ",".join(["%s"] * len(ids))
    with managed_cursor(dictionary=True) as (_conn, cursor):
        cursor.execute(
            f"""
            SELECT theme_id, month, planned_qty, actual_qty
            FROM savings_record
            WHERE theme_id IN ({placeholders}) AND year = %s
            ORDER BY theme_id, month
            """,
            (*ids, year),
        )
        rows = cursor.fetchall()
    result: dict[int, dict[int, dict[str, float | None]]] = {}
    for row in rows:
        bucket = result.setdefault(int(row["theme_id"]), {})
        bucket[int(row["month"])] = {
            "planned": float(row["planned_qty"] or 0.0),
            "actual": None if row["actual_qty"] is None else float(row["actual_qty"]),
        }
    return result


def create_theme(payload: dict[str, Any]) -> int:
    """테마 등록 → 생성된 id. 같은 (공장, 연도, 테마명)이 있으면 예외."""
    user = get_current_user()
    values = _normalized_theme_values(payload)
    with managed_cursor(admin=True) as (conn, cursor):
        try:
            cursor.execute(
                f"""
                INSERT INTO savings_theme ({', '.join(_THEME_COLUMNS)}, changed_by)
                VALUES ({', '.join(['%s'] * len(_THEME_COLUMNS))}, %s)
                """,
                (*values, user),
            )
            theme_id = int(cursor.lastrowid)
            conn.commit()
            return theme_id
        except Exception:
            conn.rollback()
            raise


def update_theme(theme_id: int, payload: dict[str, Any]) -> bool:
    """테마 수정 → 대상 행이 있었는지 여부."""
    user = get_current_user()
    values = _normalized_theme_values(payload)
    assignments = ", ".join(f"{col} = %s" for col in _THEME_COLUMNS)
    with managed_cursor(admin=True) as (conn, cursor):
        try:
            cursor.execute(
                f"UPDATE savings_theme SET {assignments}, changed_by = %s WHERE id = %s",
                (*values, user, theme_id),
            )
            affected = cursor.rowcount
            conn.commit()
            return affected > 0
        except Exception:
            conn.rollback()
            raise


def count_records(theme_id: int) -> int:
    """테마에 딸린 월별 레코드 수 — 삭제 확인 문구에 쓴다(CASCADE 로 함께 지워짐)."""
    with managed_cursor(dictionary=True) as (_conn, cursor):
        cursor.execute(
            "SELECT COUNT(*) n FROM savings_record WHERE theme_id = %s", (theme_id,),
        )
        rows = cursor.fetchall()
    return int(rows[0]["n"]) if rows else 0


def delete_theme(theme_id: int) -> bool:
    """테마 삭제 → 대상 행이 있었는지. savings_record 는 FK CASCADE 로 함께 삭제된다."""
    with managed_cursor(admin=True) as (conn, cursor):
        try:
            cursor.execute("DELETE FROM savings_theme WHERE id = %s", (theme_id,))
            affected = cursor.rowcount
            conn.commit()
            return affected > 0
        except Exception:
            conn.rollback()
            raise


def upsert_records(theme_id: int, year: int, items: list[dict[str, Any]]) -> int:
    """월별 계획·실적 일괄 저장 → 적용된 행 수.

    items: [{month, planned_qty, actual_qty}, ...]
    계획 0이고 실적이 비어 있으면(입력 취소) 해당 (테마, 연, 월) 행을 삭제한다 —
    monthly_input_service.upsert_energy_monthly() 와 같은 관례.

    actual_qty 는 빈 값이면 NULL 로 남긴다 — 0 으로 바꾸면 "실적 0"이 되어
    달성률 계산에 잘못 섞인다.
    """
    if not items:
        return 0
    user = get_current_user()
    affected = 0
    with managed_cursor(admin=True) as (conn, cursor):
        try:
            for item in items:
                try:
                    month = int(item.get("month"))
                except (TypeError, ValueError):
                    continue
                if not 1 <= month <= 12:
                    continue

                planned_raw, actual_raw = item.get("planned_qty"), item.get("actual_qty")
                try:
                    planned = float(planned_raw) if planned_raw not in (None, "") else 0.0
                except (TypeError, ValueError):
                    planned = 0.0
                actual: float | None
                if actual_raw in (None, ""):
                    actual = None
                else:
                    try:
                        actual = float(actual_raw)
                    except (TypeError, ValueError):
                        actual = None

                if planned == 0.0 and actual is None:
                    cursor.execute(
                        "DELETE FROM savings_record WHERE theme_id=%s AND year=%s AND month=%s",
                        (theme_id, year, month),
                    )
                else:
                    cursor.execute(
                        """
                        INSERT INTO savings_record
                            (theme_id, year, month, planned_qty, actual_qty, changed_by)
                        VALUES (%s, %s, %s, %s, %s, %s)
                        ON DUPLICATE KEY UPDATE
                            planned_qty = VALUES(planned_qty),
                            actual_qty  = VALUES(actual_qty),
                            changed_by  = VALUES(changed_by)
                        """,
                        (theme_id, year, month, planned, actual, user),
                    )
                affected += 1
            conn.commit()
            return affected
        except Exception:
            conn.rollback()
            raise
