import sqlite3
import os

# Adjust this if your db lives somewhere else
DB_PATH = os.path.join(os.path.dirname(__file__), "..", "trading_agent.db")

conn = sqlite3.connect(DB_PATH)
cur = conn.cursor()

cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
tables = cur.fetchall()
print("Tables:", tables)

conn.close()
