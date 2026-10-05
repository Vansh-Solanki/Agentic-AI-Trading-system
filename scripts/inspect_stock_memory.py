import sqlite3
import os

DB_PATH = os.path.join(os.path.dirname(__file__), "trading_agent.db")

conn = sqlite3.connect(DB_PATH)
cur = conn.cursor()

# First, see what columns actually exist in stock_memory
cur.execute("PRAGMA table_info(stock_memory)")
print("Columns in stock_memory:")
for col in cur.fetchall():
    print(" ", col)

print()

# Pull ALL of NVDA's rows (key-value schema means multiple rows per ticker)
cur.execute("SELECT * FROM stock_memory WHERE ticker = ?", ("MSFT",))
rows = cur.fetchall()

if not rows:
    print("No rows found for NVDA in stock_memory.")
else:
    col_names = [desc[0] for desc in cur.description]
    print(f"NVDA rows ({len(rows)} total):")
    for row in rows:
        print("---")
        for name, value in zip(col_names, row):
            print(f"  {name}: {value}")

conn.close()