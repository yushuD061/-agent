"""Explicit check/apply tool for the MySQL trade-workbench schema."""
from __future__ import annotations
import argparse, json
from pathlib import Path
from agent.business.config import load_business_config
from agent.business.mysql_database import get_connection
from agent.business.mysql_trade_workbench_repository import MySQLTradeWorkbenchRepository

MIGRATION = Path(__file__).with_name("migrations") / "006_trade_workbench.mysql.sql"

def statements(path: Path = MIGRATION) -> list[str]:
    lines=[line for line in path.read_text(encoding="utf-8").splitlines() if not line.lstrip().startswith("--")]
    return [item.strip() for item in "\n".join(lines).split(";") if item.strip()]

def apply() -> None:
    connection=get_connection(); cursor=connection.cursor()
    try:
        for statement in statements(): cursor.execute(statement)
        connection.commit()
    except Exception:
        connection.rollback(); raise
    finally: cursor.close()

def check() -> dict:
    connection=get_connection(); cursor=connection.cursor()
    try:
        cursor.execute("SELECT table_name FROM information_schema.tables WHERE table_schema=DATABASE()")
        actual={row["table_name"] for row in cursor.fetchall()}; missing=sorted(MySQLTradeWorkbenchRepository.REQUIRED_TABLES-actual)
        cursor.execute("SELECT table_name,column_name FROM information_schema.columns WHERE table_schema=DATABASE()")
        columns={}
        for row in cursor.fetchall(): columns.setdefault(row["table_name"],set()).add(row["column_name"])
        missing_columns=sorted(f"{table}.{column}" for table,required in MySQLTradeWorkbenchRepository.REQUIRED_COLUMNS.items() for column in required-columns.get(table,set()))
        return {"database":load_business_config().mysql_database,"missing_tables":missing,
                "missing_columns":missing_columns,"ready":not missing and not missing_columns}
    finally: cursor.close()

def main() -> int:
    parser=argparse.ArgumentParser(); group=parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--check",action="store_true"); group.add_argument("--apply",action="store_true")
    args=parser.parse_args()
    if load_business_config().database_backend!="mysql": parser.error("BUSINESS_DATABASE_BACKEND=mysql is required")
    if args.apply: apply()
    result=check(); print(json.dumps(result,ensure_ascii=False,sort_keys=True)); return 0 if result["ready"] else 1

if __name__=="__main__": raise SystemExit(main())
