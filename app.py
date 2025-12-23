"""
Flask API サーバ（主要エンドポイント）
- /register, /login
- /stations, /stations/<id>
- /rent, /return, /charge
認証には JWT を使用
"""
from flask import Flask, request, jsonify
from flask_jwt_extended import JWTManager, jwt_required, create_access_token, get_jwt_identity
from db import get_session
from models import User, Station, Battery, Rental
from auth import hash_password, verify_password
from variables import JWT_SECRET_KEY, PRICE_PER_MINUTE_CENTS, RENTAL_DEPOSIT_CENTS
from sqlalchemy import select, func
from sqlalchemy.exc import SQLAlchemyError
from datetime import datetime

app = Flask(__name__)
app.config["JWT_SECRET_KEY"] = JWT_SECRET_KEY
jwt = JWTManager(app)

@app.route("/register", methods=["POST"])
def register():
    data = request.get_json() or {}
    email = data.get("email")
    password = data.get("password")
    if not email or not password:
        return jsonify({"msg":"email and password required"}), 400
    session = get_session()
    try:
        if session.query(User).filter_by(email=email).first():
            return jsonify({"msg":"email already registered"}), 400
        user = User(email=email, password_hash=hash_password(password), balance_cents=0)
        session.add(user)
        session.commit()
        return jsonify({"msg":"registered"}), 201
    except Exception as e:
        session.rollback()
        return jsonify({"msg":"failed", "error": str(e)}), 500
    finally:
        session.close()

@app.route("/login", methods=["POST"])
def login():
    data = request.get_json() or {}
    email = data.get("email")
    password = data.get("password")
    session = get_session()
    try:
        user = session.query(User).filter_by(email=email).first()
        if not user or not verify_password(password, user.password_hash):
            return jsonify({"msg":"bad credentials"}), 401
        token = create_access_token(identity=user.id)
        return jsonify({"access_token": token, "user_id": user.id})
    finally:
        session.close()

@app.route("/stations", methods=["GET"])
def list_stations():
    session = get_session()
    try:
        sts = session.query(Station).all()
        res = []
        for s in sts:
            avail_count = session.query(func.count(Battery.id)).filter_by(station_id=s.id, available=True).scalar()
            res.append({
                "id": s.id, "name": s.name, "lat": s.lat, "lng": s.lng,
                "location": s.location, "available_count": avail_count
            })
        return jsonify(res)
    finally:
        session.close()

@app.route("/stations/<int:sid>", methods=["GET"])
def station_detail(sid):
    session = get_session()
    try:
        s = session.get(Station, sid)
        if not s:
            return jsonify({"msg":"station not found"}), 404
        batteries = session.query(Battery).filter_by(station_id=sid).all()
        return jsonify({
            "id": s.id, "name": s.name, "location": s.location,
            "batteries": [{"id":b.id, "serial":b.serial, "available": b.available, "level": b.battery_level} for b in batteries]
        })
    finally:
        session.close()

@app.route("/rent", methods=["POST"])
@jwt_required()
def rent():
    user_id = get_jwt_identity()
    data = request.get_json() or {}
    battery_id = data.get("battery_id")
    if not battery_id:
        return jsonify({"msg":"battery_id required"}), 400
    session = get_session()
    try:
        # トランザクション開始
        with session.begin():
            # user を取得
            user = session.get(User, user_id)
            if not user:
                raise RuntimeError("user not found")

            # PostgreSQL 等では SELECT ... FOR UPDATE を使ってバッテリー行をロック
            stmt = select(Battery).where(Battery.id==battery_id).with_for_update()
            battery = session.execute(stmt).scalar_one_or_none()
            if not battery:
                raise RuntimeError("battery not found")
            if not battery.available:
                raise RuntimeError("battery not available")

            # 残高チェック（デポジット）
            if user.balance_cents < RENTAL_DEPOSIT_CENTS:
                raise RuntimeError("insufficient balance for deposit")

            # Rental 作成 & Battery を占有
            rental = Rental(user_id=user.id, battery_id=battery.id, status="ongoing")
            session.add(rental)
            battery.available = False
            # commit は with ブロックを抜けた際に自動で行われる
        return jsonify({"msg":"rented", "rental_id": rental.id})
    except Exception as e:
        # rollback は with session.begin() により行われる
        return jsonify({"msg":"failed to rent", "error": str(e)}), 400
    finally:
        session.close()

@app.route("/return", methods=["POST"])
@jwt_required()
def return_battery():
    user_id = get_jwt_identity()
    data = request.get_json() or {}
    rental_id = data.get("rental_id")
    if not rental_id:
        return jsonify({"msg":"rental_id required"}), 400
    session = get_session()
    try:
        with session.begin():
            # ロック付きで rental を取得する
            stmt = select(Rental).where(Rental.id==rental_id).with_for_update()
            rental = session.execute(stmt).scalar_one_or_none()
            if not rental:
                raise RuntimeError("rental not found")
            if rental.status != "ongoing":
                raise RuntimeError("rental not ongoing")
            # battery をロックして取得
            stmt_b = select(Battery).where(Battery.id==rental.battery_id).with_for_update()
            battery = session.execute(stmt_b).scalar_one_or_none()
            if not battery:
                raise RuntimeError("battery not found")

            # 経過時間で料金計算
            start = rental.start_at
            end = datetime.utcnow()
            minutes = max(1, int((end - start).total_seconds() // 60))
            price = minutes * PRICE_PER_MINUTE_CENTS

            # user を取得して残高を更新
            user = session.get(User, rental.user_id)
            if not user:
                raise RuntimeError("user not found")

            # 残高から差し引く（ここでは残高不足でも差し引く — 運用ポリシーに応じて変更可）
            user.balance_cents = user.balance_cents - price
            rental.end_at = end
            rental.price_cents = price
            rental.status = "returned"
            battery.available = True

        # commit 成功
        return jsonify({"msg":"returned", "price_cents": price, "minutes": minutes})
    except Exception as e:
        return jsonify({"msg":"failed to return", "error": str(e)}), 400
    finally:
        session.close()

@app.route("/charge", methods=["POST"])
@jwt_required()
def charge():
    user_id = get_jwt_identity()
    data = request.get_json() or {}
    amount = data.get("amount_cents")
    if amount is None:
        return jsonify({"msg":"amount_cents required"}), 400
    session = get_session()
    try:
        with session.begin():
            user = session.get(User, user_id)
            if not user:
                raise RuntimeError("user not found")
            user.balance_cents += int(amount)
        return jsonify({"msg":"charged", "balance_cents": user.balance_cents})
    except Exception as e:
        return jsonify({"msg":"charge failed", "error": str(e)}), 400
    finally:
        session.close()

@app.route("/me/rentals", methods=["GET"])
@jwt_required()
def my_rentals():
    user_id = get_jwt_identity()
    session = get_session()
    try:
        rentals = session.query(Rental).filter_by(user_id=user_id).all()
        return jsonify([{
            "id": r.id,
            "battery_id": r.battery_id,
            "start_at": r.start_at.isoformat() if r.start_at else None,
            "end_at": r.end_at.isoformat() if r.end_at else None,
            "status": r.status,
            "price_cents": r.price_cents
        } for r in rentals])
    finally:
        session.close()

if __name__ == "__main__":
    # Flask の起動用（開発用）
    app.run(debug=True, host="0.0.0.0", port=5000)

from flask import render_template

@app.route("/")
def login_page():
    return render_template("login.html")

@app.route("/home")
def home_page():
    station = Station.query.first()
    return render_template("home.html", station=station)

@app.route("/stations")
def stations_page():
    stations = Station.query.all()
    return render_template("stations.html", stations=stations)

@app.route("/history")
def history_page():
    rentals = Rental.query.all()
    return render_template("history.html", rentals=rentals)

