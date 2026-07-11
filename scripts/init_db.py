"""First-run script: initializes the SQLite database schema."""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from data.storage.db import init_schema

if __name__ == "__main__":
    init_schema()
    print("Database schema initialized successfully.")
