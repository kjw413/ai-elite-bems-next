"""
Page State Persistence
======================
페이지 간 이동에도 위젯의 값(필터/선택/입력)을 유지하기 위한 헬퍼.

Streamlit 기본 동작
-------------------
위젯이 화면에서 사라지면(페이지 이동 등) Streamlit은 다음 rerun에서 그 위젯의
``session_state[key]`` 항목을 자동으로 삭제합니다. 따라서 사용자가 다른 페이지에
다녀와 원래 페이지로 돌아오면 필터가 default 값으로 되돌아갑니다.

해결 패턴 (shadow-key 미러링)
-----------------------------
각 위젯 key마다 별도의 *shadow* key( ``_p_<key>`` )를 두고:

1. 위젯이 활성 상태(``key`` 가 session_state 에 존재)인 동안엔 매 렌더마다
   현재 값을 shadow 로 백업한다.
2. 사용자가 페이지를 이동하면 위젯이 unmount → Streamlit 이 ``key`` 를 삭제하지만
   shadow 는 일반 session_state 항목이라 그대로 남는다.
3. 페이지 재방문 시 ``persist()`` 가 shadow 값을 다시 ``key`` 로 복원한 뒤
   위젯이 그려져 직전 값이 그대로 노출된다.

사용 예
-------
::

    from app.utils.page_state import persist

    persist("dash_factory", default=["전사"])
    factories = st.multiselect(
        "공장", options=ALL_FACTORIES, key="dash_factory",
    )

키 명명 규칙
-----------
- 페이지를 식별할 수 있는 prefix 권장 (``dash_``, ``energy_pwr_``, ``ai_pred_`` 등).
- key는 앱 전체에서 unique 해야 함 — 두 페이지가 같은 key 를 공유하면
  의도치 않게 값이 섞임.
"""
from __future__ import annotations

from typing import Any

import streamlit as st


_SHADOW_PREFIX = "_p_"


def persist(key: str, default: Any | None = None) -> None:
    """위젯 그리기 직전에 호출해 페이지 이동에도 값을 유지시킵니다.

    Parameters
    ----------
    key : str
        위젯에 그대로 전달할 ``key`` 값.
    default : Any, optional
        앱 시작 후 첫 진입 시 사용할 초기값. ``None`` 이면 위젯의 기본값에 맡김.

    동작
    ----
    1. ``key`` 가 이미 session_state 에 있으면 → shadow 로 백업 (활성 상태 유지)
    2. 없는데 shadow 가 있으면 → shadow 에서 ``key`` 로 복원 (재방문 케이스)
    3. 둘 다 없는데 default 가 주어졌으면 → default 로 초기화 (첫 진입)
    """
    shadow = _SHADOW_PREFIX + key

    if key in st.session_state:
        # widget 이 활성 상태 — 현재 값을 shadow 로 백업
        st.session_state[shadow] = st.session_state[key]
    elif shadow in st.session_state:
        # 페이지 재방문 — shadow 에서 widget key 로 복원
        st.session_state[key] = st.session_state[shadow]
    elif default is not None:
        # 첫 진입 — default 로 초기화
        st.session_state[key] = default
        st.session_state[shadow] = default


def persist_many(keys_with_defaults: dict[str, Any]) -> None:
    """여러 key 를 한 번에 persist. ``persist()`` 의 dict 입력 버전.

    Example
    -------
    ::

        persist_many({
            "dash_factory": ["전사"],
            "dash_unit":    "power_per_ton",
            "dash_base_date": None,
        })
    """
    for key, default in keys_with_defaults.items():
        persist(key, default=default)


def reset_page_state(prefix: str) -> None:
    """주어진 prefix 로 시작하는 모든 persisted state 를 초기화.

    페이지에 "필터 초기화" 같은 버튼을 둘 때 사용.
    위젯 key 와 shadow key 양쪽을 모두 제거해야 default 가 다시 적용됨.
    """
    to_delete = [
        k for k in st.session_state.keys()
        if k.startswith(prefix) or k.startswith(_SHADOW_PREFIX + prefix)
    ]
    for k in to_delete:
        del st.session_state[k]
