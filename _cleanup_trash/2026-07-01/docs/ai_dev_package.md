# AI Developer Package – FEMS (Code-Based)

This document is a compact code-first package summary for the current FEMS project.

---

## Goal

Build and maintain a local FEMS dashboard that:

- ingests Excel-based energy data,
- stores it in MySQL,
- renders Streamlit dashboards,
- tracks upload and audit history,
- generates AI monthly energy reports.

---

## Technology

Frontend: Streamlit  
Application: Python  
Data Processing: Pandas  
Charts: Plotly  
Database: MySQL  
AI Reporting: OpenAI + LangChain  

---

## Current Module Map

```text
app/
  main.py
  pages/
    dashboard_main.py
    energy_factory_power.py
    energy_factory_fuel_water.py
    energy_equipment_power.py
    energy_intensity.py
    data_upload.py
    audit_log.py
    ai_report.py
  services/
    upload_service.py
    validation_service.py
    query_service.py
    audit_service.py
    ai_report_service.py
    ai_db_service.py
  database/
    db_connection.py
    schema.sql
  utils/
    excel_parser.py
```

---

## Upload Flow

1. Read Excel file  
2. Validate extension, sheets, and required columns  
3. Replace empty numeric values with `0`  
4. Reject non-numeric values  
5. Upsert by `(factory, date)` into `energy_daily`  
6. Record changed numeric fields in `energy_daily_audit`  
7. Record upload batch status in `upload_batch`  

---

## UI Structure

- Dashboard
- Production Performance
- Energy Analysis
- Savings Management
- AI Energy Analysis
- Data Upload (admin only)
- Audit Log (admin only)

Current placeholders:

- Production Performance
- Savings pages
- AI Prediction page

---

## Run System

Preferred:

```bash
SETUP.bat
WEB 실행.bat
```

Manual:

```bash
python -c "import sys; sys.path.insert(0,'.'); from app.database.db_connection import init_db; init_db()"
streamlit run app/main.py
```

---

## Source of Truth

Use this priority order:

1. `app/main.py`
2. `app/database/schema.sql`
3. `app/services/*.py`
4. supporting docs
