# migrate_rentals_nullable.py
"""
既存 SQLite DB の rentals テーブルを
battery_id を NULL 許容に変更して置き換えるスクリプト

使い方:
  - このスクリプトを repo に追加して push する
  - variables.py で DATABASE_URL を設定しているなら自動で検出します
  - Render の Shell (one-off) またはローカルで実行してください:
      python migrate_rentals_nullable.py

注意:
  - 実行前に必ず DB のバックアップを作成します（スクリプトも自動で bak を作ります）
  - DATABASE_URL が sqlite 以外ならスクリプトは実行を中止します
"""
import os
import sys
import shutil
import sqlite3
from urllib.parse import urlparse

# try to import DATABASE_URL from your variables.py
try:
    from variables import DATABASE_URL
except Exception as e:
    print("Failed to import DATABASE_URL from variables.py:", e)
    sys.exit(1)

def get_sqlite_path_from_url(url: str):
    # example SQLITE urls:
    #  sqlite:///absolute/path/to/db.sqlite3
    #  sqlite:///./relative/path.db
    #  sqlite:///:memory:
    if not url.startswith("sqlite://"):
        return None
    # strip scheme
    # "sqlite:///" -> absolute path following
    # "sqlite:///:memory:" -> memory
    if url == "sqlite:///:memory:":
        return ":memory:"
    # remove "sqlite:///" prefix
    if url.startswith("sqlite:///"):
        return url[len("sqlite:///"):]
    # fallback (rare)
    return url[len("sqlite://"):]

DB_PATH = get_sqlite_path_from_url(DATABASE_URL)
if not DB_PATH:
    print("DATABASE_URL is not SQLite or cannot determine path. DATABASE_URL:", DATABASE_URL)
    sys.exit(1)

if DB_PATH == ":memory:":
    print("Database is in-memory SQLite; migration not supported by this script.")
    sys.exit(1)

if not os.path.exists(DB_PATH):
    print("SQLite DB file not found at:", DB_PATH)
    sys.exit(1)

bak = DB_PATH + ".bak"
print("Backing up DB to:", bak)
shutil.copyfile(DB_PATH, bak)

conn = sqlite3.connect(DB_PATH)
c = conn.cursor()

try:
    print("Current rentals schema:")
    for row in c.execute("PRAGMA table_info(rentals);"):
        print(row)
    print("Beginning migration...")

    c.execute("PRAGMA foreign_keys = OFF;")
    c.execute("BEGIN TRANSACTION;")

    # Create new table with battery_id NULLable.
    # Adjust columns if your rentals table contains additional columns.
    # The script below covers common columns: id,user_id,battery_id,start_at,end_at,status,price_cents
    c.execute("""
    CREATE TABLE rentals_new (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      user_id INTEGER,
      battery_id INTEGER,
      start_at DATETIME,
      end_at DATETIME,
      status TEXT NOT NULL,
      price_cents INTEGER
    );
    """)

    # Copy data. If your original rentals has extra columns, you must include them here.
    c.execute("""
    INSERT INTO rentals_new (id, user_id, battery_id, start_at, end_at, status, price_cents)
      SELECT id, user_id, battery_id, start_at, end_at, status, price_cents FROM rentals;
    """)

    c.execute("DROP TABLE rentals;")
    c.execute("ALTER TABLE rentals_new RENAME TO rentals;")

    c.execute("COMMIT;")
    c.execute("PRAGMA foreign_keys = ON;")
    print("Migration completed successfully.")
    print("Backup retained at:", bak)

except Exception as e:
    conn.rollback()
    print("Migration failed:", e)
    print("Restoring backup...")
    shutil.copyfile(bak, DB_PATH)
    print("Backup restored.")
    raise
finally:
    conn.close()
