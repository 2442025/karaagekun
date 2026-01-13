"""
Flask API + 画面表示アプリ
モバイルバッテリー貸し出しシステム

【設計意図】
- トランザクション処理を明示的に使用（session.begin()）
- リレーションを持つテーブル設計（User, Station, Battery, Rental）
- 複数テーブルを使ったクエリ（サブクエリ・JOIN）
- 正規化を意識した設計（第3正規形）
- CRUD 操作を網羅
- JWT 認証を使用
- モバイルフレンドリーな UI
"""

from flask import (
    Flask, request, jsonify,
    render_template, redirect, url_for, flash, session as flask_session
)
from flask_jwt_extended import (
    JWTManager, jwt_required,
    create_access_token, get_jwt_identity, get_jwt
)
from sqlalchemy import select, func, and_, or_
from datetime import datetime, timedelta
import logging

from db import get_session, get_session_context
from models import User, Station, Battery, Rental
from auth import hash_password, verify_password
from variables import (
    JWT_SECRET_KEY, PRICE_PER_MINUTE_CENTS, RENTAL_DEPOSIT_CENTS,
    INITIAL_BALANCE_CENTS, DEBUG_MODE
)

# --------------------
# Flask 初期化
# --------------------
app = Flask(__name__)

# データベースの初期化（テーブルがなければ作成する）
with app.app_context():
    from db import engine
    from models import Base
    # ここでSQLを流し込まなくても、models.pyの定義を元にテーブルを自動作成します
    Base.metadata.create_all(bind=engine)

app.config["JWT_SECRET_KEY"] = JWT_SECRET_KEY
app.config["SECRET_KEY"] = "change_me_in_production_123!"  # HTMLフォーム用
app.config["JWT_ACCESS_TOKEN_EXPIRES"] = timedelta(hours=1)
app.config["DEBUG"] = DEBUG_MODE

jwt = JWTManager(app)

@app.context_processor
def inject_template_globals():
    return {
        "PRICE_PER_MINUTE_CENTS": PRICE_PER_MINUTE_CENTS,
        "RENTAL_DEPOSIT_CENTS": RENTAL_DEPOSIT_CENTS,
        "INITIAL_BALANCE_CENTS": INITIAL_BALANCE_CENTS
    }

# ロギング設定
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ====================
# ヘルパー関数
# ====================

def get_user_balance(user_id):
    """ユーザー残高を取得（SQLAlchemy ORM使用）"""
    with get_session_context() as session:
        user = session.get(User, user_id)
        return user.balance_cents if user else 0

def get_available_batteries_count(station_id):
    """指定スタンドの利用可能バッテリー数を取得（JOINクエリ）"""
    with get_session_context() as session:
        count = session.query(func.count(Battery.id)).filter(
            Battery.station_id == station_id,
            Battery.available == True
        ).scalar()
        return count or 0

def get_user_rentals_with_details(user_id):
    """ユーザーの貸出履歴を取得（JOINクエリでバッテリー情報も取得）"""
    with get_session_context() as session:
        rentals = session.query(Rental, Battery, Station).join(
            Battery, Rental.battery_id == Battery.id
        ).outerjoin(
            Station, Battery.station_id == Station.id
        ).filter(
            Rental.user_id == user_id
        ).order_by(Rental.start_at.desc()).all()
        return rentals

# ====================
# 画面ルーティング
# ====================

# ルート "/" はログインページへリダイレクトする（重複定義の解消）
@app.route("/", strict_slashes=False)
def index():
    user_id = flask_session.get("user_id")
    # ログイン済みならホームへ、そうでなければログイン画面へリダイレクト
    if user_id:
        return redirect(url_for("home_page"))
    return redirect(url_for("login_page"))

@app.route("/register", methods=["GET", "POST"], strict_slashes=False)
def register_page():
    """新規登録画面"""
    if request.method == "GET":
        return render_template("register.html")

    email = request.form.get("email")
    password = request.form.get("password")
    confirm_password = request.form.get("confirm_password")

    if not email or not password:
        flash("メールアドレスとパスワードを入力してください", "error")
        return render_template("register.html"), 400

    if password != confirm_password:
        flash("パスワードが一致しません", "error")
        return render_template("register.html"), 400

    with get_session_context() as session:
        try:
            # トランザクション開始
            with session.begin():
                # メール重複チェック（SELECT）
                existing_user = session.query(User).filter_by(email=email).first()
                if existing_user:
                    flash("このメールアドレスは既に登録されています", "error")
                    return render_template("register.html"), 400

                # ユーザー作成（INSERT）
                user = User(
                    email=email,
                    password_hash=hash_password(password),
                    balance_cents=INITIAL_BALANCE_CENTS
                )
                session.add(user)
                session.flush()  # IDを取得するためにflush

                # 初期残高チャージ履歴を作成（INSERT）
                if INITIAL_BALANCE_CENTS > 0:
                    rental = Rental(
                        user_id=user.id,
                        battery_id=None,  # チャージはバッテリーなし
                        status="charged",
                        price_cents=-INITIAL_BALANCE_CENTS  # 負の値でチャージを表現
                    )
                    session.add(rental)

            logger.info(f"User {email} registered with initial balance {INITIAL_BALANCE_CENTS}")
            flash("登録が完了しました。ログインしてください", "success")
            return redirect(url_for("login_page"))

        except Exception as e:
            logger.error(f"Registration failed for {email}: {e}")
            flash("登録に失敗しました。時間をおいて再度お試しください", "error")
            return render_template("register.html"), 500

@app.route("/home", strict_slashes=False)
def home_page():
    """ホーム画面"""
    user_id = flask_session.get("user_id")
    if not user_id:
        return redirect(url_for("login_page"))

    with get_session_context() as session:
        # 全スタンド取得（SELECT）
        stations = session.query(Station).all()
        
        # 各スタンドの利用可能バッテリー数を計算
        station_data = []
        for station in stations:
            available_count = get_available_batteries_count(station.id)
            station_data.append({
                "id": station.id,
                "name": station.name,
                "location": station.location,
                "available_count": available_count
            })

        # ユーザー残高取得
        balance = get_user_balance(user_id)

    return render_template("home.html", 
                         stations=station_data, 
                         balance=balance,
                         user_id=user_id)

@app.route("/stations", strict_slashes=False)
def stations_page():
    """スタンド一覧画面"""
    user_id = flask_session.get("user_id")
    if not user_id:
        return redirect(url_for("login_page"))

    with get_session_context() as session:
        stations = session.query(Station).all()
        station_data = []
        for station in stations:
            available_count = get_available_batteries_count(station.id)
            station_data.append({
                "id": station.id,
                "name": station.name,
                "location": station.location,
                "available_count": available_count
            })

    return render_template("stations.html", stations=station_data)

@app.route("/stations/<int:station_id>", strict_slashes=False)
def station_detail_page(station_id):
    """スタンド詳細画面（利用可能バッテリー一覧）"""
    user_id = flask_session.get("user_id")
    if not user_id:
        return redirect(url_for("login_page"))

    with get_session_context() as session:
        station = session.get(Station, station_id)
        if not station:
            flash("指定されたスタンドは存在しません", "error")
            return redirect(url_for("stations_page"))

        # そのスタンドの利用可能��ッテリーを取得（JOINクエリ）
        batteries = session.query(Battery).filter(
            Battery.station_id == station_id,
            Battery.available == True
        ).all()

        balance = get_user_balance(user_id)

    return render_template("station_detail.html", 
                         station=station, 
                         batteries=batteries,
                         balance=balance)

@app.route("/rent/<int:battery_id>", methods=["GET", "POST"], strict_slashes=False)
def rent_page(battery_id):
    """貸出確認画面"""
    user_id = flask_session.get("user_id")
    if not user_id:
        return redirect(url_for("login_page"))

    with get_session_context() as session:
        battery = session.get(Battery, battery_id)
        if not battery or not battery.available:
            flash("このバッテリーは貸出できません", "error")
            return redirect(url_for("stations_page"))

        user = session.get(User, user_id)
        if user.balance_cents < RENTAL_DEPOSIT_CENTS:
            flash("残高が不足しています。チャージしてください", "error")
            return redirect(url_for("charge_page"))

        balance = user.balance_cents

    if request.method == "POST":
        # 貸出処理（トランザクション）
        try:
            with get_session_context() as session:
                with session.begin():
                    user = session.get(User, user_id)
                    battery = session.get(Battery, battery_id)

                    if not battery or not battery.available:
                        return jsonify({"msg": "battery not available"}), 400

                    if user.balance_cents < RENTAL_DEPOSIT_CENTS:
                        return jsonify({"msg": "insufficient balance"}), 400

                    # 貸出レコード作成（INSERT）
                    rental = Rental(
                        user_id=user.id,
                        battery_id=battery.id,
                        status="ongoing"
                    )
                    battery.available = False
                    session.add(rental)

            flash("バッテリーを貸出しました", "success")
            return redirect(url_for("home_page"))

        except Exception as e:
            logger.error(f"Rent failed for user {user_id}, battery {battery_id}: {e}")
            flash("貸出に失敗しました", "error")
            return redirect(url_for("rent_page", battery_id=battery_id))

    return render_template("rent.html", battery=battery, balance=balance)

@app.route("/return/<int:rental_id>", methods=["GET", "POST"], strict_slashes=False)
def return_page(rental_id):
    """返却画面"""
    user_id = flask_session.get("user_id")
    if not user_id:
        return redirect(url_for("login_page"))

    with get_session_context() as session:
        rental = session.get(Rental, rental_id)
        if not rental or rental.user_id != user_id or rental.status != "ongoing":
            flash("返却できません", "error")
            return redirect(url_for("history_page"))

        battery = session.get(Battery, rental.battery_id)
        station = session.get(Station, battery.station_id) if battery else None

    if request.method == "POST":
        # 返却処理（トランザクション）
        try:
            with get_session_context() as session:
                with session.begin():
                    rental = session.get(Rental, rental_id)
                    battery = session.get(Battery, rental.battery_id)
                    user = session.get(User, user_id)

                    end_time = datetime.utcnow()
                    start_time = rental.start_at
                    minutes = max(1, int((end_time - start_time).total_seconds() // 60))
                    price = minutes * PRICE_PER_MINUTE_CENTS

                    # 残高チェック
                    if user.balance_cents < price:
                        flash("残高が不足しています", "error")
                        return redirect(url_for("charge_page"))

                    # 返却処理（UPDATE）
                    user.balance_cents -= price
                    rental.end_at = end_time
                    rental.price_cents = price
                    rental.status = "returned"
                    battery.available = True

                    # 返却先スタンドを記録（任意）
                    return_station_id = request.form.get("return_station_id")
                    if return_station_id:
                        rental.return_station_id = int(return_station_id)

            flash(f"バッテリーを返却しました。料金: {price}円", "success")
            return redirect(url_for("home_page"))

        except Exception as e:
            logger.error(f"Return failed for rental {rental_id}: {e}")
            flash("返却に失敗しました", "error")
            return redirect(url_for("return_page", rental_id=rental_id))

    with get_session_context() as session:
        stations = session.query(Station).all()

    return render_template("return.html", 
                         rental=rental, 
                         battery=battery, 
                         station=station,
                         stations=stations)

@app.route("/history", strict_slashes=False)
def history_page():
    """利用履歴画面"""
    user_id = flask_session.get("user_id")
    if not user_id:
        return redirect(url_for("login_page"))

    rentals_data = get_user_rentals_with_details(user_id)
    
    # 履歴データを整形
    history_list = []
    for rental, battery, station in rentals_data:
        start_time = rental.start_at.strftime("%Y-%m-%d %H:%M")
        end_time = rental.end_at.strftime("%Y-%m-%d %H:%M") if rental.end_at else "貸出中"
        
        history_list.append({
            "id": rental.id,
            "start_at": start_time,
            "end_at": end_time,
            "battery_serial": battery.serial if battery else "不明",
            "station_name": station.name if station else "不明",
            "price": rental.price_cents if rental.price_cents else 0,
            "status": rental.status
        })

    return render_template("history.html", history=history_list)

@app.route("/charge", methods=["GET", "POST"], strict_slashes=False)
def charge_page():
    """チャージ画面"""
    user_id = flask_session.get("user_id")
    if not user_id:
        return redirect(url_for("login_page"))

    if request.method == "POST":
        # フォーム入力を安全にパース
        amount_str = (request.form.get("amount") or "").strip()
        try:
            amount = int(amount_str)
        except (ValueError, TypeError):
            flash("有効な金額を入力してください", "error")
            return redirect(url_for("charge_page"))

        if amount <= 0:
            flash("有効な金額を入力してください", "error")
            return redirect(url_for("charge_page"))

        try:
            with get_session_context() as session:
                with session.begin():
                    user = session.get(User, user_id)
                    if not user:
                        # 想定外（セッションに user_id があるが DB にユーザーがない）
                        raise RuntimeError("ユーザーが見つかりません")

                    # 単位に注意: 変数名に _CENTS がついていてもテンプレートは「円」を表示しています。
                    # このアプリでは amount をそのまま balance_cents に足す実装になっています。
                    user.balance_cents += amount

                    # チャージ履歴を作成（INSERT）
                    rental = Rental(
                        user_id=user.id,
                        battery_id=None,
                        status="charged",
                        price_cents=-amount  # 負の値でチャージを表現
                    )
                    session.add(rental)

            flash(f"{amount}円をチャージしました", "success")
            return redirect(url_for("home_page"))

        except Exception as e:
            # トレースをログに残す（Render のログで確認可能）
            logger.exception(f"Charge failed for user {user_id}: {e}")
            if DEBUG_MODE:
                flash(f"チャージに失敗しました: {e}", "error")
            else:
                flash("チャージに失敗しました", "error")
            return redirect(url_for("charge_page"))

    balance = get_user_balance(user_id)
    return render_template("charge.html", balance=balance)

@app.route("/logout", strict_slashes=False)
def logout():
    """ログアウト"""
    flask_session.clear()
    flash("ログアウトしました", "info")
    return redirect(url_for("login_page"))

# ====================
# API（JSON）
# ====================

@app.route("/api/login", methods=["POST"], strict_slashes=False)
def api_login():
    """APIログイン"""
    data = request.get_json() or {}
    email = data.get("email")
    password = data.get("password")

    if not email or not password:
        return jsonify({"msg": "email and password required"}), 400

    with get_session_context() as session:
        user = session.query(User).filter_by(email=email).first()
        if not user or not verify_password(password, user.password_hash):
            return jsonify({"msg": "bad credentials"}), 401

        token = create_access_token(identity=user.id)
        return jsonify({
            "access_token": token,
            "user_id": user.id,
            "email": user.email,
            "balance": user.balance_cents
        })

@app.route("/login", methods=["GET", "POST"], strict_slashes=False)
def login_page():
    """ログイン画面 + フォームログイン"""

    if request.method == "GET":
        return render_template("login.html")

    # POST（ログイン処理）
    email = request.form.get("email")
    password = request.form.get("password")

    if not email or not password:
        flash("メールアドレスとパスワードを入力してください", "error")
        return render_template("login.html"), 400

    with get_session_context() as session:
        user = session.query(User).filter_by(email=email).first()
        if not user or not verify_password(password, user.password_hash):
            flash("メールアドレスまたはパスワードが間違っています", "error")
            return render_template("login.html"), 401

        access_token = create_access_token(identity=user.id)
        flask_session["user_id"] = user.id
        flask_session["access_token"] = access_token

        logger.info(f"User {user.email} logged in")
        return redirect(url_for("home_page"))

@app.route("/api/stations", methods=["GET"], strict_slashes=False)
def api_stations():
    """API: スタンド一覧"""
    with get_session_context() as session:
        stations = session.query(Station).all()
        result = []
        for s in stations:
            available = get_available_batteries_count(s.id)
            result.append({
                "id": s.id,
                "name": s.name,
                "location": s.location,
                "available": available
            })
        return jsonify(result)

@app.route("/api/rent", methods=["POST"], strict_slashes=False)
@jwt_required()
def api_rent():
    """API: 貸出"""
    user_id = get_jwt_identity()
    data = request.get_json() or {}
    battery_id = data.get("battery_id")

    if not battery_id:
        return jsonify({"msg": "battery_id required"}), 400

    try:
        with get_session_context() as session:
            with session.begin():
                user = session.get(User, user_id)
                battery = session.get(Battery, battery_id)

                if not battery or not battery.available:
                    return jsonify({"msg": "battery not available"}), 400

                if user.balance_cents < RENTAL_DEPOSIT_CENTS:
                    return jsonify({"msg": "insufficient balance"}), 400

                rental = Rental(
                    user_id=user.id,
                    battery_id=battery.id,
                    status="ongoing"
                )
                battery.available = False
                session.add(rental)

        return jsonify({"msg": "rented", "rental_id": rental.id})
    except Exception as e:
        logger.error(f"API rent failed: {e}")
        return jsonify({"msg": "rental failed"}), 500

@app.route("/api/return", methods=["POST"], strict_slashes=False)
@jwt_required()
def api_return():
    """API: 返却"""
    user_id = get_jwt_identity()
    data = request.get_json() or {}
    rental_id = data.get("rental_id")

    if not rental_id:
        return jsonify({"msg": "rental_id required"}), 400

    try:
        with get_session_context() as session:
            with session.begin():
                rental = session.get(Rental, rental_id)
                if not rental or rental.user_id != user_id or rental.status != "ongoing":
                    return jsonify({"msg": "invalid rental"}), 400

                battery = session.get(Battery, rental.battery_id)
                user = session.get(User, user_id)

                end_time = datetime.utcnow()
                start_time = rental.start_at
                minutes = max(1, int((end_time - start_time).total_seconds() // 60))
                price = minutes * PRICE_PER_MINUTE_CENTS

                if user.balance_cents < price:
                    return jsonify({"msg": "insufficient balance"}), 400

                user.balance_cents -= price
                rental.end_at = end_time
                rental.price_cents = price
                rental.status = "returned"
                battery.available = True

        return jsonify({
            "msg": "returned", 
            "price": price,
            "balance": user.balance_cents
        })
    except Exception as e:
        logger.error(f"API return failed: {e}")
        return jsonify({"msg": "return failed"}), 500

@app.route("/api/charge", methods=["POST"], strict_slashes=False)
@jwt_required()
def api_charge():
    """API: チャージ"""
    user_id = get_jwt_identity()
    data = request.get_json() or {}
    amount = int(data.get("amount", 0))

    if amount <= 0:
        return jsonify({"msg": "invalid amount"}), 400

    try:
        with get_session_context() as session:
            with session.begin():
                user = session.get(User, user_id)
                user.balance_cents += amount

                # チャージ履歴を作成
                rental = Rental(
                    user_id=user.id,
                    battery_id=None,
                    status="charged",
                    price_cents=-amount
                )
                session.add(rental)

        return jsonify({
            "msg": "charged", 
            "balance": user.balance_cents
        })
    except Exception as e:
        logger.error(f"API charge failed: {e}")
        return jsonify({"msg": "charge failed"}), 500

@app.route("/api/user", methods=["GET"], strict_slashes=False)
@jwt_required()
def api_user():
    """API: ユーザー情報取得"""
    user_id = get_jwt_identity()
    with get_session_context() as session:
        user = session.get(User, user_id)
        if not user:
            return jsonify({"msg": "user not found"}), 404

        return jsonify({
            "id": user.id,
            "email": user.email,
            "balance": user.balance_cents,
            "created_at": user.created_at.isoformat()
        })

@app.route("/api/history", methods=["GET"], strict_slashes=False)
@jwt_required()
def api_history():
    """API: 利用履歴取得"""
    user_id = get_jwt_identity()
    rentals_data = get_user_rentals_with_details(user_id)
    
    history = []
    for rental, battery, station in rentals_data:
        history.append({
            "id": rental.id,
            "start_at": rental.start_at.isoformat(),
            "end_at": rental.end_at.isoformat() if rental.end_at else None,
            "battery_serial": battery.serial if battery else None,
            "station_name": station.name if station else None,
            "price": rental.price_cents if rental.price_cents else 0,
            "status": rental.status
        })

    return jsonify(history)

# ====================
# エラーハンドリング
# ====================

@app.errorhandler(404)
def not_found(error):
    return render_template("error.html", message="ページが見つかりません"), 404

@app.errorhandler(500)
def internal_error(error):
    return render_template("error.html", message="サーバーエラーが発生しました"), 500

# ====================
# 起動
# ====================
if __name__ == "__main__":
    # デバッグモードの場合はログを詳細に
    if DEBUG_MODE:
        app.logger.setLevel(logging.DEBUG)
    
    app.run(debug=DEBUG_MODE, host="0.0.0.0", port=5000)

# ====== 一時管理ルート（デバッグ・マイグレーション用） ======
# 注意: セキュリティのため実行後すぐにこのコードを削除してください。
import sqlite3
import shutil
import os
import re
from urllib.parse import urlparse

# 保護キー（短期間だけ使う。Render の環境変数に ADMIN_KEY を設定するのが安全）
ADMIN_KEY = os.environ.get("ADMIN_KEY") or app.config.get("SECRET_KEY", "change_me_in_production_123!")

def _get_sqlite_path_from_url(db_url: str):
    if not db_url or not db_url.startswith("sqlite://"):
        return None
    if db_url == "sqlite:///:memory:":
        return ":memory:"
    # sqlite:///path/to/file => return path/to/file
    if db_url.startswith("sqlite:///"):
        return db_url[len("sqlite:///"):]
    return db_url[len("sqlite://"):]

@app.route("/admin/schema", strict_slashes=False)
def admin_schema():
    """
    現行 rentals テーブルのスキーマを返します。
    呼び出し: /admin/schema?key=<ADMIN_KEY>
    """
    key = request.args.get("key", "")
    if key != ADMIN_KEY:
        return "Forbidden (invalid key)", 403

    try:
        from variables import DATABASE_URL
    except Exception as e:
        return f"Failed to import DATABASE_URL: {e}", 500

    db_path = _get_sqlite_path_from_url(DATABASE_URL)
    if not db_path:
        return f"DATABASE_URL not sqlite or cannot determine path: {DATABASE_URL}", 400

    if not os.path.exists(db_path):
        return f"SQLite DB file not found at: {db_path}", 404

    try:
        conn = sqlite3.connect(db_path)
        c = conn.cursor()
        # PRAGMA table_info と CREATE TABLE 文を出力
        info = "\nPRAGMA table_info(rentals):\n"
        for row in c.execute("PRAGMA table_info(rentals);"):
            info += str(row) + "\n"
        info += "\nCREATE SQL:\n"
        row = c.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='rentals';").fetchone()
        info += (row[0] if row else "No CREATE statement found") + "\n"
        conn.close()
        return "<pre>" + info + "</pre>"
    except Exception as e:
        return f"Error reading DB schema: {e}", 500

@app.route("/admin/migrate-rentals-nullable", methods=["POST", "GET"], strict_slashes=False)
def admin_migrate_rentals_nullable():
    """
    rentals.battery_id を NULL 許容にするマイグレーションを行います（SQLite用）。
    呼び出し: GET/POST /admin/migrate-rentals-nullable?key=<ADMIN_KEY>
    実行前に /admin/schema でスキーマを確認してください。
    """
    key = request.args.get("key", "")
    if key != ADMIN_KEY:
        return "Forbidden (invalid key)", 403

    try:
        from variables import DATABASE_URL
    except Exception as e:
        return f"Failed to import DATABASE_URL: {e}", 500

    db_path = _get_sqlite_path_from_url(DATABASE_URL)
    if not db_path:
        return f"DATABASE_URL not sqlite or cannot determine path: {DATABASE_URL}", 400

    if not os.path.exists(db_path):
        return f"SQLite DB file not found at: {db_path}", 404

    bak = db_path + ".bak"
    try:
        # backup
        shutil.copyfile(db_path, bak)
    except Exception as e:
        return f"Failed to backup DB: {e}", 500

    try:
        conn = sqlite3.connect(db_path)
        c = conn.cursor()

        # Fetch original CREATE TABLE sql
        row = c.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='rentals';").fetchone()
        if not row or not row[0]:
            conn.close()
            return "Cannot find rentals CREATE TABLE statement", 500
        create_sql = row[0]

        # Modify the CREATE TABLE SQL to make battery_id nullable:
        # Replace patterns like "battery_id INTEGER NOT NULL" -> "battery_id INTEGER"
        new_create_sql = re.sub(r"battery_id\s+[^,)]*NOT\s+NULL", "battery_id INTEGER", create_sql, flags=re.IGNORECASE)

        # Ensure table name is rentals_new
        new_create_sql = re.sub(r"CREATE\s+TABLE\s+\"?rentals\"?", "CREATE TABLE IF NOT EXISTS rentals_new", new_create_sql, flags=re.IGNORECASE)

        # Start migration
        c.execute("PRAGMA foreign_keys = OFF;")
        c.execute("BEGIN TRANSACTION;")

        c.execute(new_create_sql)

        # Build column list from existing table
        cols = [row[1] for row in c.execute("PRAGMA table_info(rentals);").fetchall()]
        cols_list = ", ".join([f'"{col}"' for col in cols])

        # Copy data
        c.execute(f'INSERT INTO rentals_new ({cols_list}) SELECT {cols_list} FROM rentals;')

        c.execute("DROP TABLE rentals;")
        c.execute("ALTER TABLE rentals_new RENAME TO rentals;")

        c.execute("COMMIT;")
        c.execute("PRAGMA foreign_keys = ON;")
        conn.close()
        return f"Migration succeeded. Backup saved: {bak}"
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        # restore backup
        shutil.copyfile(bak, db_path)
        return f"Migration failed and DB restored from backup. Error: {e}", 500
# ====== 管理ルートここまで ======
