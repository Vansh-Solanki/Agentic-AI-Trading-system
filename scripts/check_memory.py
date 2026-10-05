"""
Quick DB inspector for trading_agent.db
Run this from the same folder as trading_agent.db (usually the scripts folder):
    python check_memory.py
"""
import sqlite3
import os

DB_PATH = "trading_agent.db"

if not os.path.exists(DB_PATH):
    print(f"Could not find {DB_PATH} in the current folder: {os.getcwd()}")
    print("Files here:", os.listdir("."))
    raise SystemExit(1)

conn = sqlite3.connect(DB_PATH)
cur = conn.cursor()

cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
tables = [row[0] for row in cur.fetchall()]

print(f"\nFound {len(tables)} table(s) in {DB_PATH}:\n")

for t in tables:
    try:
        count = cur.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        print(f"  {t:30s} -> {count} rows")
    except Exception as e:
        print(f"  {t:30s} -> error reading table: {e}")

# If there's an obvious memory-related table, show a sample of recent rows
memory_like = [t for t in tables if "memory" in t.lower() or "signal" in t.lower()]
if memory_like:
    print("\nSample rows from memory-related tables:\n")
    for t in memory_like:
        print(f"--- {t} (up to 5 rows) ---")
        try:
            cur.execute(f"SELECT * FROM {t} LIMIT 5")
            cols = [desc[0] for desc in cur.description]
            print("  columns:", cols)
            for row in cur.fetchall():
                print("  ", row)
        except Exception as e:
            print("  error:", e)
        print()

conn.close()
