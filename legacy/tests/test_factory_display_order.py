from app.domain.factories import (
    FACTORY_DISPLAY_ORDER,
    FACTORY_FILTER_OPTIONS,
    FACTORY_QUERY_ORDER,
    YOY_FACTORY_DEFS,
    ordered_factory_labels,
)


def test_aggregate_factories_follow_company_display_order():
    unordered = ["경산", "광주", "남양주", "논산", "김해"]

    assert ordered_factory_labels(unordered) == ["남양주", "김해", "광주", "논산", "경산"]


def test_display_order_places_namyangju_parent_before_children():
    assert FACTORY_DISPLAY_ORDER == (
        "전사",
        "남양주",
        "남양주1",
        "남양주2",
        "김해",
        "광주",
        "논산",
        "경산",
    )
    assert FACTORY_FILTER_OPTIONS[:3] == ("남양주", "남양주1", "남양주2")
    assert [label for label, _ in FACTORY_QUERY_ORDER[:4]] == [
        "ALL",
        "남양주",
        "남양주1",
        "남양주2",
    ]
    assert [label for label, _ in YOY_FACTORY_DEFS[:4]] == [
        "전사",
        "남양주",
        "남양주1",
        "남양주2",
    ]


def test_unknown_factory_labels_stay_visible_after_known_factories():
    assert ordered_factory_labels(["신규B", "김해", "신규A", "남양주"]) == [
        "남양주",
        "김해",
        "신규B",
        "신규A",
    ]
