import os
from dotenv import load_dotenv
load_dotenv()

# DB 接続文字列。デフォルトはローカル SQLite
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///mobile_battery.db")

# JWT 設定
JWT_SECRET_KEY = os.getenv("JWT_SECRET_KEY", "change_me_in_prod")

# アプリの料金設定（例：1分あたりの円）
PRICE_PER_MINUTE_CENTS = int(os.getenv("PRICE_PER_MINUTE_CENTS", "10"))

# レンタル開始に必要な最低デポジット（cents）
RENTAL_DEPOSIT_CENTS = int(os.getenv("RENTAL_DEPOSIT_CENTS", "1000"))
