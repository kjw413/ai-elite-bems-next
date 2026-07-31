"""
Monthly Fallback Service
========================
경산처럼 데이터 시작일이 다른 공장의 (공장, 월) 구간에서 energy_daily/
production_daily 에 실적이 아예 없을 때 energy_monthly/production_monthly 의
월 총량으로 보충한다. 경산은 일단위 실적이 2026-04부터라, 전년비·월별 추이가
동일 기준으로 비교되지 않는 문제(전사_경산구분_및_월별적재_계획.md 참고)를
이 폴백으로 메운다.

세 가지 규칙 — 어긋나면 이중 계상되거나 가짜 일별값이 실측처럼 섞인다:

  1. 한 달 안에서 두 소스를 섞지 않는다. (공장, 월) 단위로 일별 행이 하나라도
     있으면 일별 SUM 을, 하나도 없을 때만 월별 테이블 값을 쓴다. 경계월(예:
     경산 2026-04, 일별 데이터가 이미 있음)에서 이중 계상을 막는 규칙이다.

  2. 판단은 반드시 물리 공장 단위로 한다. "전사"로 조회하면 다른 5개 공장의
     일별 행이 있으므로 "그 달은 일별 데이터가 있다"고 잘못 판정되어 경산
     월별이 영원히 안 붙는다 — 집계 라벨을 물리 공장으로 펼친 뒤(규칙 1을
     공장별로 각각 적용) 그 결과를 합산해야 한다.

  3. 월 경계로 정렬된 집계(GROUP BY YEAR(date), MONTH(date))에만 이 모듈을
     쓴다. 부분월을 포함하는 임의 기간(WHERE date BETWEEN) 합계에는 쓰지
     않는다 — 월 총량은 일 단위로 쪼갤 수 없어 정확히 잘라낼 방법이 없다.
     이 모듈은 그 판단을 강제하지 않는다 — 호출자가 월 경계 집계에서만
     불러야 한다(server.py 의 세 호출 지점 주석 참고).

이 모듈은 DB 접근만 하고 화면 응답 형식은 모른다 — 호출자가 반환값을 자기
스키마에 맞게 조립한다.
"""
from __future__ import annotations

from datetime import date
from typing import Any

from app.database.db_connection import managed_cursor
from app.domain.factories import FACTORY_KR_TO_CODE, expand_factory_members

MonthKey = tuple[int, int]

# energy_daily/energy_monthly 공용 컬럼 — 순서가 아니라 이름으로 다루므로
# 두 테이블에 실제 존재하는 이름과 정확히 같아야 한다.
_ENERGY_COLUMNS: tuple[str, ...] = (
    "total_power_kwh", "fuel_nm3", "water_ton", "wastewater_ton",
    "power_cost_krw", "fuel_cost_krw",
)


def _month_range(month_from: date, month_to: date) -> list[MonthKey]:
    months: list[MonthKey] = []
    year, month = month_from.year, month_from.month
    end = (month_to.year, month_to.month)
    while (year, month) <= end:
        months.append((year, month))
        month += 1
        if month > 12:
            month, year = 1, year + 1
    return months


def _zero_energy() -> dict[str, float]:
    return {col: 0.0 for col in _ENERGY_COLUMNS}


def _daily_energy_by_member_month(
    members: tuple[str, ...], month_from: date, month_to: date,
) -> dict[tuple[str, int, int], dict[str, float]]:
    """물리 공장별로, energy_daily 행이 하나라도 있는 (연,월)의 SUM.

    행이 0건인 (공장,월)은 이 딕셔너리에 아예 없다 — 그것이 곧 "일별 데이터
    없음" 판정이다(규칙 1). 0건과 "합계가 0인 실측"을 이렇게 구분한다.
    """
    if not members:
        return {}
    placeholders = ",".join(["%s"] * len(members))
    sum_cols = ", ".join(f"SUM({col}) {col}" for col in _ENERGY_COLUMNS)
    with managed_cursor(with_db=True, dictionary=True) as (_conn, cursor):
        cursor.execute(
            f"""
            SELECT factory, YEAR(date) y, MONTH(date) m, COUNT(*) n, {sum_cols}
            FROM energy_daily
            WHERE factory IN ({placeholders}) AND date BETWEEN %s AND %s
            GROUP BY factory, y, m
            """,
            (*members, month_from, month_to),
        )
        rows = cursor.fetchall()
    result: dict[tuple[str, int, int], dict[str, float]] = {}
    for row in rows:
        if int(row["n"]) <= 0:
            continue
        key = (str(row["factory"]), int(row["y"]), int(row["m"]))
        result[key] = {col: float(row.get(col) or 0.0) for col in _ENERGY_COLUMNS}
    return result


def _monthly_energy_by_member_month(
    members: tuple[str, ...], month_from: date, month_to: date,
) -> dict[tuple[str, int, int], dict[str, float]]:
    """energy_monthly 테이블의 (물리공장, 연, 월) → 값. 범위는 month_key 문자열 비교."""
    if not members:
        return {}
    placeholders = ",".join(["%s"] * len(members))
    from_key, to_key = _month_key(month_from.year, month_from.month), _month_key(month_to.year, month_to.month)
    with managed_cursor(with_db=True, dictionary=True) as (_conn, cursor):
        cursor.execute(
            f"""
            SELECT factory, month_key, total_power_kwh, fuel_nm3, water_ton,
                   wastewater_ton, power_cost_krw, fuel_cost_krw
            FROM energy_monthly
            WHERE factory IN ({placeholders}) AND month_key BETWEEN %s AND %s
            """,
            (*members, from_key, to_key),
        )
        rows = cursor.fetchall()
    result: dict[tuple[str, int, int], dict[str, float]] = {}
    for row in rows:
        year, month = (int(part) for part in str(row["month_key"]).split("-"))
        key = (str(row["factory"]), year, month)
        result[key] = {col: float(row.get(col) or 0.0) for col in _ENERGY_COLUMNS}
    return result


def _month_key(year: int, month: int) -> str:
    return f"{year:04d}-{month:02d}"


def monthly_energy(
    factory: str, month_from: date, month_to: date,
) -> dict[MonthKey, dict[str, float]]:
    """(연, 월) → {total_power_kwh, fuel_nm3, water_ton, wastewater_ton,
    power_cost_krw, fuel_cost_krw}.

    규칙 1·2 적용 후 물리 공장 합산. 호출자는 월 경계로 정렬된 집계(규칙 3)
    에서만 이 함수를 써야 한다 — 임의 기간 합계에 쓰면 부분월이 통째로
    폴백값(월 총량)으로 부풀려진다.
    """
    members = expand_factory_members(factory)
    daily = _daily_energy_by_member_month(members, month_from, month_to)
    monthly_table = _monthly_energy_by_member_month(members, month_from, month_to)

    result: dict[MonthKey, dict[str, float]] = {}
    for year, month in _month_range(month_from, month_to):
        totals = _zero_energy()
        for member in members:
            key = (member, year, month)
            source = daily.get(key) or monthly_table.get(key)
            if source is None:
                continue
            for col in _ENERGY_COLUMNS:
                totals[col] += source[col]
        result[(year, month)] = totals
    return result


def fallback_months(
    factory: str, month_from: date, month_to: date,
) -> set[MonthKey]:
    """월별 폴백이 실제로 적용된 (연, 월) 집합 — 화면 안내 문구용.

    "그 달에 참여한 물리 공장 중 하나라도 월별 테이블에서 값을 가져왔다"를
    기준으로 한다. 부분적으로만 폴백된 달(예: 경산만 월별, 나머지 5개는
    일별)도 포함된다 — 그 달의 숫자가 완전히 실측만은 아니라는 뜻이므로.
    """
    members = expand_factory_members(factory)
    daily = _daily_energy_by_member_month(members, month_from, month_to)
    monthly_table = _monthly_energy_by_member_month(members, month_from, month_to)

    months: set[MonthKey] = set()
    for year, month in _month_range(month_from, month_to):
        for member in members:
            key = (member, year, month)
            if key not in daily and key in monthly_table:
                months.add((year, month))
    return months


def _production_daily_code(member: str) -> str | None:
    """물리 공장(한글 라벨) → production_daily 조회용 F-code.

    production_monthly.factory 는 energy_daily 와 통일된 한글 라벨을 쓰지만
    production_daily 는 F-code 를 쓴다 — 이 차이를 여기서만 흡수한다.
    남양주1/2 의 legacy F10 부모코드 분리(2026-05-04 마이그레이션)는 다루지
    않는다 — 이 폴백이 필요한 구간(신설 공장의 과거 결측)에는 해당하지 않는다.
    """
    return FACTORY_KR_TO_CODE.get(member)


def _daily_production_by_member_month(
    members: tuple[str, ...], month_from: date, month_to: date,
) -> dict[tuple[str, int, int], float]:
    code_to_member = {
        code: member
        for member in members
        if (code := _production_daily_code(member)) is not None
    }
    if not code_to_member:
        return {}
    placeholders = ",".join(["%s"] * len(code_to_member))
    with managed_cursor(with_db=True, dictionary=True) as (_conn, cursor):
        cursor.execute(
            f"""
            SELECT factory, YEAR(date) y, MONTH(date) m, COUNT(*) n, SUM(actual_qty) qty
            FROM production_daily
            WHERE factory IN ({placeholders}) AND date BETWEEN %s AND %s
            GROUP BY factory, y, m
            """,
            (*code_to_member, month_from, month_to),
        )
        rows = cursor.fetchall()
    result: dict[tuple[str, int, int], float] = {}
    for row in rows:
        if int(row["n"]) <= 0:
            continue
        member = code_to_member.get(str(row["factory"]))
        if member is None:
            continue
        result[(member, int(row["y"]), int(row["m"]))] = float(row.get("qty") or 0.0)
    return result


def _monthly_production_by_member_month(
    members: tuple[str, ...], month_from: date, month_to: date,
) -> dict[tuple[str, int, int], float]:
    if not members:
        return {}
    placeholders = ",".join(["%s"] * len(members))
    from_key, to_key = _month_key(month_from.year, month_from.month), _month_key(month_to.year, month_to.month)
    with managed_cursor(with_db=True, dictionary=True) as (_conn, cursor):
        cursor.execute(
            f"""
            SELECT factory, month_key, SUM(actual_qty) qty
            FROM production_monthly
            WHERE factory IN ({placeholders}) AND month_key BETWEEN %s AND %s
            GROUP BY factory, month_key
            """,
            (*members, from_key, to_key),
        )
        rows = cursor.fetchall()
    result: dict[tuple[str, int, int], float] = {}
    for row in rows:
        year, month = (int(part) for part in str(row["month_key"]).split("-"))
        result[(str(row["factory"]), year, month)] = float(row.get("qty") or 0.0)
    return result


def production_fallback_months(
    factory: str, month_from: date, month_to: date,
) -> set[MonthKey]:
    """생산량 쪽 월별 폴백이 실제로 적용되는 (연, 월) 집합 — fallback_months()의 생산량 버전.

    에너지와 생산량은 각각 energy_daily/production_daily 커버리지가 다를 수 있어
    (예: 한쪽만 먼저 적재) 별도로 판정한다. 호출자(_monthly_production_ton 등)가
    이 집합이 비어 있으면 추가 조회 없이 기존 값을 그대로 쓴다.
    """
    members = expand_factory_members(factory)
    daily = _daily_production_by_member_month(members, month_from, month_to)
    monthly_table = _monthly_production_by_member_month(members, month_from, month_to)

    months: set[MonthKey] = set()
    for year, month in _month_range(month_from, month_to):
        for member in members:
            key = (member, year, month)
            if key not in daily and key in monthly_table:
                months.add((year, month))
    return months


def monthly_production_kg(
    factory: str, month_from: date, month_to: date,
) -> dict[MonthKey, float]:
    """(연, 월) → 생산 kg. 규칙 1·2 적용 후 물리 공장 합산.

    production_daily.actual_qty 와 production_monthly.actual_qty 는 같은
    단위(kg)다 — 원단위 계산부(_monthly_production_ton 등)가 이미 /1000 을
    적용하므로 여기서는 변환하지 않는다.
    """
    members = expand_factory_members(factory)
    daily = _daily_production_by_member_month(members, month_from, month_to)
    monthly_table = _monthly_production_by_member_month(members, month_from, month_to)

    result: dict[MonthKey, float] = {}
    for year, month in _month_range(month_from, month_to):
        total = 0.0
        for member in members:
            key = (member, year, month)
            if key in daily:
                total += daily[key]
            elif key in monthly_table:
                total += monthly_table[key]
        result[(year, month)] = total
    return result


def monthly_production_category2_kg(
    factory: str, month_from: date, month_to: date,
) -> dict[MonthKey, dict[str, float]]:
    """(연, 월) → {category2: kg}. 생산실적 연간 모드의 유형별 막대에 쓴다.

    production_monthly 는 품목 축이 없어 category2(경산은 항상 'IC')만 채워
    돌려준다 — 일별 실적이 있는 (공장,월)은 규칙 1에 따라 제외한다(그 조합은
    production_daily 를 직접 category2 로 GROUP BY 하는 기존 쿼리가 담당).
    한 공장이 구간 안에서 일부 달은 일별, 일부 달은 월별인 경우(경계월 포함)를
    다뤄야 하므로 공장 단위가 아니라 (공장,연,월) 단위로 걸러낸다.
    """
    members = expand_factory_members(factory)
    if not members:
        return {}
    daily = _daily_production_by_member_month(members, month_from, month_to)

    placeholders = ",".join(["%s"] * len(members))
    from_key, to_key = _month_key(month_from.year, month_from.month), _month_key(month_to.year, month_to.month)
    with managed_cursor(with_db=True, dictionary=True) as (_conn, cursor):
        cursor.execute(
            f"""
            SELECT factory, month_key, category2, SUM(actual_qty) qty
            FROM production_monthly
            WHERE factory IN ({placeholders}) AND month_key BETWEEN %s AND %s
            GROUP BY factory, month_key, category2
            """,
            (*members, from_key, to_key),
        )
        rows = cursor.fetchall()

    result: dict[MonthKey, dict[str, float]] = {}
    for row in rows:
        year, month = (int(part) for part in str(row["month_key"]).split("-"))
        member = str(row["factory"])
        if (member, year, month) in daily:
            continue  # 규칙 1 — 그 (공장,월)은 이미 일별 실적이 있다.
        bucket = result.setdefault((year, month), {})
        category = str(row["category2"])
        bucket[category] = bucket.get(category, 0.0) + float(row.get("qty") or 0.0)
    return result
