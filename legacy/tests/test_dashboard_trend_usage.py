from app.pages import dashboard_main as dashboard


def test_home_trend_uses_usage_metrics():
    assert list(dashboard.TREND_USAGE_OPTIONS.values()) == [
        "total_power_kwh",
        "fuel_nm3",
        "water_ton",
        "wastewater_ton",
    ]


def test_home_trend_direction_labels_compare_production_and_usage():
    improved = dashboard._direction_rows([100.0, 110.0], [100.0, 90.0], 100.0, 100.0)
    warning = dashboard._direction_rows([100.0, 90.0], [100.0, 110.0], 100.0, 100.0)

    assert improved[1] == {"label": "생산↑ 사용↓", "tone": "good"}
    assert warning[1] == {"label": "생산↓ 사용↑", "tone": "warn"}

