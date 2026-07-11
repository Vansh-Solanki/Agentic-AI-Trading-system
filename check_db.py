import sqlite3
from pathlib import Path

db_path = Path(__file__).resolve().parent / "trading.db"
print(f"Checking DB at: {db_path}")
print(f"File exists: {db_path.exists()}")

conn = sqlite3.connect(db_path)
tables = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
print(f"Tables found: {tables}")
conn.close()