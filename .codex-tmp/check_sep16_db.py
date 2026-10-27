from app.database.db_connection import get_connection

conn = get_connection()
try:
    cur = conn.cursor(dictionary=True)
    cur.execute(
        "SELECT factory,date,total_power_kwh,fuel_nm3,water_ton,updated_at,changed_by "
        "FROM energy_daily WHERE date=%s AND factory IN (%s,%s,%s) ORDER BY factory",
        ("2026-09-16", "광주", "김해", "논산"),
    )
    print("energy_daily", cur.fetchall())
    cur.execute(
        "SELECT factory,pred_date,target,pred_value,actual_value,mape,created_at,updated_at "
        "FROM prediction_log WHERE pred_date=%s AND factory IN (%s,%s,%s) "
        "ORDER BY factory,target",
        ("2026-09-16", "광주", "김해", "논산"),
    )
    print("prediction_log", cur.fetchall())
finally:
    conn.close()
