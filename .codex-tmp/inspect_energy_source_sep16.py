from pathlib import Path

import pandas as pd

source = Path(r"E:\DB_MIS\DB_에너지.xlsx")
print({"exists": source.exists(), "size": source.stat().st_size if source.exists() else None})
if not source.exists():
    raise SystemExit(0)

sheets = pd.read_excel(source, sheet_name=None, engine="openpyxl")
for sheet_name, frame in sheets.items():
    if str(sheet_name).strip() not in {"광주", "김해", "논산", "G40", "F40", "E40"}:
        continue
    first_values = [str(v).replace(" ", "") for v in frame.iloc[:, 0].dropna().values] if len(frame.columns) else []
    transposed = any("냉동전력량" in value or "전력량" in value for value in first_values)
    matches = []
    if transposed:
        for column in frame.columns[1:]:
            parsed = pd.to_datetime(str(column).strip().lstrip("'"), errors="coerce")
            if pd.notna(parsed) and parsed.date().isoformat() == "2026-09-16":
                matches.append(str(column))
    else:
        for column in frame.columns:
            normalized = str(column).lower().replace(" ", "")
            if "일자" in normalized or "날짜" in normalized or normalized == "date":
                parsed = pd.to_datetime(frame[column].astype(str).str.strip().str.lstrip("'"), errors="coerce")
                selected = frame.loc[parsed.dt.date.astype(str) == "2026-09-16"]
                matches.extend(selected.astype(object).where(pd.notna(selected), None).to_dict("records"))
    print({"sheet": str(sheet_name), "shape": frame.shape, "transposed": transposed, "sep16_matches": matches})
