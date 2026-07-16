# 이 파일은 st.dataframe 표시 시 숫자 컬럼에 천 단위 쉼표 구분자를 일관되게 적용합니다.
from __future__ import annotations

import pandas as pd
import streamlit as st


# ── 양(quantity) 단위 컬럼 자동 감지 패턴 ──
# 생산량·사용량 같은 절대량 컬럼은 소수점 없이 천 단위 쉼표만 노출하는 게 보고서 표준.
# 비율(%)·원단위(per ton 등)는 소수점 유지.
_QUANTITY_HINT_TOKENS = (
    "(kg)", "(ton)", "(톤)", "(Nm³)", "(Nm3)", "(kWh)", "(MWh)", "(L)", "(㎥)", "(m³)", "(m3)",
    "사용량", "생산량", "수량", "kg]", "ton]", "kWh]", "Nm³]", "Nm3]",
)
_RATIO_HINT_TOKENS = (
    "(%)", "%", "비중", "비율", "원단위", "ratio", "rate",
    "/ton", "/mix", "per_ton", "per ton",
)


def _looks_like_quantity_column(col_name: str) -> bool:
    """컬럼명에 양(quantity) 단위 힌트가 있고 비율 힌트가 없으면 True."""
    s = str(col_name)
    if any(tok in s for tok in _RATIO_HINT_TOKENS):
        return False
    return any(tok in s for tok in _QUANTITY_HINT_TOKENS)


# 숫자 컬럼에 대한 column_config 데이터를 만듭니다.
def numeric_column_config(
    df: pd.DataFrame,
    *,
    decimals: int = 2,
    skip_columns: list[str] | None = None,
    integer_columns: list[str] | None = None,
    auto_quantity_int: bool = True,
) -> dict:
    """``st.dataframe`` 의 column_config 인자에 그대로 전달할 dict 반환.

    숫자 컬럼은 천 단위 쉼표 구분자로 포맷되며, 정수만 들어 있는 컬럼은
    소수점 없는 ``%,d`` 형식, 그 외 실수 컬럼은 ``%,.{decimals}f`` 형식으로
    표시됩니다. 비숫자 컬럼은 영향을 주지 않습니다.

    Parameters
    ----------
    df:
        표시할 DataFrame.
    decimals:
        실수 컬럼의 소수점 자리수 (기본 2).
    skip_columns:
        포맷에서 제외할 컬럼 이름 목록 (예: ID, 연도 등 쉼표가 어색한 컬럼).
    integer_columns:
        실수값이라도 무조건 ``%,d`` (정수, 소수점 없음) 으로 표시할 컬럼 명시.
    auto_quantity_int:
        True 면 컬럼명에 (kg)/(ton)/사용량/생산량 같은 양 단위 힌트가 있고
        비율(%)·원단위 힌트가 없을 때 자동으로 정수 포맷 적용 (기본 True).
        예외 처리: 비율/원단위 컬럼은 자동 감지에서 제외되어 소수점 유지.
    """
    skip = set(skip_columns or [])
    forced_int = set(integer_columns or [])
    cfg: dict = {}
    int_fmt = "%,d"
    float_fmt = f"%,.{decimals}f"

    for col in df.columns:
        if col in skip:
            continue
        if not pd.api.types.is_numeric_dtype(df[col]):
            continue

        non_na = df[col].dropna()
        if non_na.empty:
            continue

        # 명시 지정 또는 양 단위 컬럼 자동 감지 → 강제 정수 포맷
        if col in forced_int or (auto_quantity_int and _looks_like_quantity_column(col)):
            cfg[col] = st.column_config.NumberColumn(format=int_fmt)
            continue

        # 정수 dtype 또는 실제 값이 모두 정수인 실수 → %,d
        if pd.api.types.is_integer_dtype(df[col]):
            cfg[col] = st.column_config.NumberColumn(format=int_fmt)
        else:
            try:
                if (non_na.astype(float) % 1 == 0).all():
                    cfg[col] = st.column_config.NumberColumn(format=int_fmt)
                else:
                    cfg[col] = st.column_config.NumberColumn(format=float_fmt)
            except Exception:
                cfg[col] = st.column_config.NumberColumn(format=float_fmt)

    return cfg


# 숫자 컬럼 값을 강제로 numeric 으로 변환합니다.
def coerce_numeric(df: pd.DataFrame, columns: list[str]) -> pd.DataFrame:
    """문자열로 들어온 숫자 컬럼이 column_config 천 단위 쉼표 적용을 받도록 numeric으로 강제 변환."""
    out = df.copy()
    for c in columns:
        if c in out.columns:
            out[c] = pd.to_numeric(out[c], errors="coerce")
    return out
