from datetime import date

import pandas as pd

from app.services import production_actual_service as service


def test_overlay_replaces_raw_production_and_recalculates_intensity():
    energy = pd.DataFrame([
        {
            "date": date(2026, 7, 10),
            "factory": "논산",
            "mix_prod_kg": 9000.0,
            "total_power_kwh": 200.0,
            "fuel_nm3": 50.0,
            "water_ton": 20.0,
            "power_per_ton_kwh": 22.22,
            "fuel_per_ton_nm3": 5.56,
            "water_per_ton_ton": 2.22,
        },
        {
            "date": date(2026, 7, 11),
            "factory": "논산",
            "mix_prod_kg": 8000.0,
            "total_power_kwh": 100.0,
            "fuel_nm3": 25.0,
            "water_ton": 10.0,
            "power_per_ton_kwh": 12.5,
            "fuel_per_ton_nm3": 3.125,
            "water_per_ton_ton": 1.25,
        },
    ])
    actual = pd.DataFrame([
        {"date": date(2026, 7, 10), "factory": "논산", "actual_prod_kg": 1000.0},
    ])

    result = service.overlay_actual_production(energy, actual=actual)

    assert energy.loc[0, "mix_prod_kg"] == 9000.0  # 입력 원본은 보존
    assert result.loc[0, "mix_prod_kg"] == 1000.0
    assert result.loc[0, "power_per_ton_kwh"] == 200.0
    assert result.loc[0, "fuel_per_ton_nm3"] == 50.0
    assert result.loc[0, "water_per_ton_ton"] == 20.0
    assert result.loc[1, "mix_prod_kg"] == 0.0
    assert pd.isna(result.loc[1, "power_per_ton_kwh"])


def test_mail_rows_use_actual_production_without_raw_fallback():
    rows = [
        {"date": date(2026, 7, 10), "factory": "논산", "mix_prod_kg": 9000.0},
        {"date": date(2026, 7, 11), "factory": "논산", "mix_prod_kg": 8000.0},
    ]
    actual = pd.DataFrame([
        {"date": date(2026, 7, 10), "factory": "논산", "actual_prod_kg": 1234.0},
    ])

    result = service.overlay_actual_production_rows(
        rows, date(2026, 7, 10), date(2026, 7, 11), actual=actual,
    )

    assert rows[0]["mix_prod_kg"] == 9000.0
    assert [row["mix_prod_kg"] for row in result] == [1234.0, 0.0]


def test_fetch_actual_production_maps_dw_factory_codes(monkeypatch):
    class FakeCursor:
        closed = False

        def execute(self, _sql, _params):
            return None

        def fetchall(self):
            return [
                {"date": date(2026, 7, 10), "factory": "F10A", "actual_prod_kg": 100.0},
                {"date": date(2026, 7, 10), "factory": "F10B", "actual_prod_kg": 200.0},
                {"date": date(2026, 7, 10), "factory": "F40", "actual_prod_kg": 300.0},
            ]

        def close(self):
            self.closed = True

    class FakeConnection:
        closed = False

        def __init__(self):
            self.db_cursor = FakeCursor()

        def cursor(self, dictionary=False):
            assert dictionary is True
            return self.db_cursor

        def close(self):
            self.closed = True

    conn = FakeConnection()
    monkeypatch.setattr(service, "get_connection", lambda: conn)
    monkeypatch.setattr(
        service,
        "get_wip_daily",
        lambda _factory: pd.DataFrame(columns=["date", "total_wip_kg"]),
    )

    result = service.fetch_actual_production(date(2026, 7, 10), date(2026, 7, 10))

    assert conn.closed is True
    assert conn.db_cursor.closed is True
    assert result[["factory", "actual_prod_kg"]].to_dict("records") == [
        {"factory": "남양주1", "actual_prod_kg": 100.0},
        {"factory": "남양주2", "actual_prod_kg": 200.0},
        {"factory": "논산", "actual_prod_kg": 300.0},
    ]


def test_fetch_actual_production_adds_gwangju_wip_and_keeps_wip_only_day(monkeypatch):
    class FakeCursor:
        def execute(self, _sql, _params):
            return None

        def fetchall(self):
            return [
                {"date": date(2026, 7, 10), "factory": "F30", "actual_prod_kg": 1000.0},
                {"date": date(2026, 7, 10), "factory": "F40", "actual_prod_kg": 700.0},
            ]

        def close(self):
            return None

    class FakeConnection:
        def cursor(self, dictionary=False):
            assert dictionary is True
            return FakeCursor()

        def close(self):
            return None

    monkeypatch.setattr(service, "get_connection", lambda: FakeConnection())
    monkeypatch.setattr(
        service,
        "get_wip_daily",
        lambda factory: pd.DataFrame([
            {"date": pd.Timestamp("2026-07-09"), "total_wip_kg": 999.0},
            {"date": pd.Timestamp("2026-07-10"), "total_wip_kg": 250.0},
            {"date": pd.Timestamp("2026-07-11"), "total_wip_kg": 300.0},
            {"date": pd.Timestamp("2026-07-12"), "total_wip_kg": 999.0},
        ]) if factory == "광주" else pd.DataFrame(),
    )

    result = service.fetch_actual_production(date(2026, 7, 10), date(2026, 7, 11))

    assert result.to_dict("records") == [
        {"date": date(2026, 7, 10), "factory": "광주", "actual_prod_kg": 1250.0},
        {"date": date(2026, 7, 10), "factory": "논산", "actual_prod_kg": 700.0},
        {"date": date(2026, 7, 11), "factory": "광주", "actual_prod_kg": 300.0},
    ]


def test_gwangju_operational_wip_definition_has_all_seven_items():
    from app.services.production_correction_service import WIP_MIX_CONVERSION

    assert WIP_MIX_CONVERSION["광주"] == {
        "260014": 10.91954,
        "260016": 1.0,
        "260039": 1.0,
        "260042": 4.0,
        "260047": 1.0,
        "260351": 1.0,
        "260352": 1.0,
    }
