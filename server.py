"""
FamTrack(모여) - 가족 위치공유 앱 백엔드
Flask + PostgreSQL (Render.com), DATABASE_URL 환경변수로 연결
"""

import os
import string
import random
import requests
from datetime import datetime, timezone
from flask import Flask, request, jsonify, g, send_from_directory
from flask_cors import CORS
import psycopg2
import psycopg2.extras

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")

app = Flask(__name__, static_folder=STATIC_DIR)
CORS(app)

DATABASE_URL = os.environ.get("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError(
        "DATABASE_URL 환경변수가 설정되어 있지 않습니다. "
        "Render의 Postgres Internal Database URL을 등록해주세요."
    )

KAKAO_REST_API_KEY = os.environ.get("KAKAO_REST_API_KEY")


# ---------- DB ----------

def get_db():
    db = getattr(g, "_database", None)
    if db is None:
        db = g._database = psycopg2.connect(
            DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor
        )
    return db


@app.teardown_appcontext
def close_db(exception):
    db = getattr(g, "_database", None)
    if db is not None:
        db.close()


def init_db():
    conn = psycopg2.connect(DATABASE_URL)
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS families (
            id SERIAL PRIMARY KEY,
            code TEXT UNIQUE NOT NULL,
            name TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS members (
            id SERIAL PRIMARY KEY,
            family_id INTEGER NOT NULL REFERENCES families(id),
            name TEXT NOT NULL,
            emoji TEXT DEFAULT '👤',
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS locations (
            id SERIAL PRIMARY KEY,
            member_id INTEGER NOT NULL REFERENCES members(id),
            lat DOUBLE PRECISION NOT NULL,
            lng DOUBLE PRECISION NOT NULL,
            accuracy DOUBLE PRECISION,
            speed DOUBLE PRECISION,
            address TEXT,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS places (
            id SERIAL PRIMARY KEY,
            family_id INTEGER NOT NULL REFERENCES families(id),
            name TEXT NOT NULL,
            lat DOUBLE PRECISION NOT NULL,
            lng DOUBLE PRECISION NOT NULL,
            radius_m INTEGER DEFAULT 150,
            icon TEXT DEFAULT '📍',
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS place_targets (
            place_id INTEGER NOT NULL REFERENCES places(id),
            member_id INTEGER NOT NULL REFERENCES members(id),
            PRIMARY KEY (place_id, member_id)
        );

        CREATE TABLE IF NOT EXISTS geofence_events (
            id SERIAL PRIMARY KEY,
            member_id INTEGER NOT NULL REFERENCES members(id),
            place_id INTEGER NOT NULL REFERENCES places(id),
            event_type TEXT NOT NULL,
            created_at TEXT NOT NULL,
            seen INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS member_place_state (
            member_id INTEGER NOT NULL,
            place_id INTEGER NOT NULL,
            is_inside INTEGER DEFAULT 0,
            PRIMARY KEY (member_id, place_id)
        );

        ALTER TABLE locations ADD COLUMN IF NOT EXISTS speed DOUBLE PRECISION;
        ALTER TABLE locations ADD COLUMN IF NOT EXISTS address TEXT;
        ALTER TABLE locations ADD COLUMN IF NOT EXISTS battery_level INTEGER;
        ALTER TABLE locations ADD COLUMN IF NOT EXISTS battery_charging BOOLEAN;
        """
    )
    conn.commit()
    cur.close()
    conn.close()


def gen_family_code(length=6):
    chars = string.ascii_uppercase + string.digits
    return "".join(random.choices(chars, k=length))


def haversine_m(lat1, lng1, lat2, lng2):
    from math import radians, sin, cos, sqrt, atan2
    R = 6371000
    phi1, phi2 = radians(lat1), radians(lat2)
    dphi = radians(lat2 - lat1)
    dlambda = radians(lng2 - lng1)
    a = sin(dphi / 2) ** 2 + cos(phi1) * cos(phi2) * sin(dlambda / 2) ** 2
    return 2 * R * atan2(sqrt(a), sqrt(1 - a))


def reverse_geocode(lat, lng):
    """카카오 좌표->행정동 변환 REST API. 실패하면 None 반환."""
    if not KAKAO_REST_API_KEY:
        return None
    try:
        res = requests.get(
            "https://dapi.kakao.com/v2/local/geo/coord2regioncode.json",
            params={"x": lng, "y": lat},
            headers={"Authorization": f"KakaoAK {KAKAO_REST_API_KEY}"},
            timeout=3,
        )
        if res.status_code != 200:
            return None
        docs = res.json().get("documents", [])
        for d in docs:
            if d.get("region_type") == "H":  # 행정동 우선
                return d.get("address_name")
        if docs:
            return docs[0].get("address_name")
    except Exception:
        return None
    return None


def now_iso():
    return datetime.now(timezone.utc).isoformat()


# ---------- API: 가족 그룹 ----------

@app.route("/api/family/create", methods=["POST"])
def create_family():
    data = request.get_json(force=True)
    family_name = (data.get("family_name") or "우리가족").strip()
    member_name = (data.get("member_name") or "").strip()
    emoji = data.get("emoji", "👤")
    if not member_name:
        return jsonify({"error": "member_name이 필요합니다"}), 400

    db = get_db()
    cur = db.cursor()

    code = None
    for _ in range(10):
        candidate = gen_family_code()
        cur.execute("SELECT 1 FROM families WHERE code=%s", (candidate,))
        if not cur.fetchone():
            code = candidate
            break
    if not code:
        return jsonify({"error": "코드 생성 실패, 다시 시도해주세요"}), 500

    cur.execute(
        "INSERT INTO families (code, name, created_at) VALUES (%s, %s, %s) RETURNING id",
        (code, family_name, now_iso()),
    )
    family_id = cur.fetchone()["id"]

    cur.execute(
        "INSERT INTO members (family_id, name, emoji, created_at) VALUES (%s, %s, %s, %s) RETURNING id",
        (family_id, member_name, emoji, now_iso()),
    )
    member_id = cur.fetchone()["id"]
    db.commit()

    return jsonify(
        {
            "family_id": family_id,
            "family_code": code,
            "family_name": family_name,
            "member_id": member_id,
            "member_name": member_name,
        }
    )


@app.route("/api/family/join", methods=["POST"])
def join_family():
    data = request.get_json(force=True)
    code = (data.get("family_code") or "").strip().upper()
    member_name = (data.get("member_name") or "").strip()
    emoji = data.get("emoji", "👤")
    if not code or not member_name:
        return jsonify({"error": "family_code와 member_name이 필요합니다"}), 400

    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT * FROM families WHERE code=%s", (code,))
    family = cur.fetchone()
    if not family:
        return jsonify({"error": "가족 코드를 찾을 수 없습니다"}), 404

    cur.execute("SELECT COUNT(*) as c FROM members WHERE family_id=%s", (family["id"],))
    member_count = cur.fetchone()["c"]
    if member_count >= 8:
        return jsonify({"error": "가족 인원이 너무 많습니다"}), 400

    cur.execute(
        "INSERT INTO members (family_id, name, emoji, created_at) VALUES (%s, %s, %s, %s) RETURNING id",
        (family["id"], member_name, emoji, now_iso()),
    )
    member_id = cur.fetchone()["id"]
    db.commit()

    return jsonify(
        {
            "family_id": family["id"],
            "family_code": code,
            "family_name": family["name"],
            "member_id": member_id,
            "member_name": member_name,
        }
    )


@app.route("/api/family/<int:family_id>/members", methods=["GET"])
def list_members(family_id):
    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT id, name, emoji FROM members WHERE family_id=%s", (family_id,))
    rows = cur.fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/members/<int:member_id>", methods=["DELETE"])
def delete_member(member_id):
    db = get_db()
    cur = db.cursor()
    cur.execute("DELETE FROM geofence_events WHERE member_id=%s", (member_id,))
    cur.execute("DELETE FROM member_place_state WHERE member_id=%s", (member_id,))
    cur.execute("DELETE FROM place_targets WHERE member_id=%s", (member_id,))
    cur.execute("DELETE FROM locations WHERE member_id=%s", (member_id,))
    cur.execute("DELETE FROM members WHERE id=%s", (member_id,))
    db.commit()
    return jsonify({"ok": True})


# ---------- API: 위치 ----------

@app.route("/api/location/update", methods=["POST"])
def update_location():
    data = request.get_json(force=True)
    member_id = data.get("member_id")
    lat = data.get("lat")
    lng = data.get("lng")
    accuracy = data.get("accuracy")
    speed = data.get("speed")
    battery_level = data.get("battery_level")
    battery_charging = data.get("battery_charging")
    if member_id is None or lat is None or lng is None:
        return jsonify({"error": "member_id, lat, lng가 필요합니다"}), 400

    address = reverse_geocode(lat, lng)

    db = get_db()
    cur = db.cursor()
    cur.execute(
        "INSERT INTO locations (member_id, lat, lng, accuracy, speed, address, "
        "battery_level, battery_charging, updated_at) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (member_id, lat, lng, accuracy, speed, address, battery_level, battery_charging, now_iso()),
    )
    db.commit()

    events = check_geofences(db, member_id, lat, lng)
    db.commit()

    return jsonify({"ok": True, "events": events})


def is_target_member(db, place_id, member_id):
    cur = db.cursor()
    cur.execute("SELECT member_id FROM place_targets WHERE place_id=%s", (place_id,))
    targets = cur.fetchall()
    if not targets:
        return True
    target_ids = {t["member_id"] for t in targets}
    return member_id in target_ids


def check_geofences(db, member_id, lat, lng):
    cur = db.cursor()
    cur.execute("SELECT * FROM members WHERE id=%s", (member_id,))
    member = cur.fetchone()
    if not member:
        return []

    cur.execute("SELECT * FROM places WHERE family_id=%s", (member["family_id"],))
    places = cur.fetchall()

    fired = []
    for place in places:
        if not is_target_member(db, place["id"], member_id):
            continue

        dist = haversine_m(lat, lng, place["lat"], place["lng"])
        is_inside_now = 1 if dist <= place["radius_m"] else 0

        cur.execute(
            "SELECT is_inside FROM member_place_state WHERE member_id=%s AND place_id=%s",
            (member_id, place["id"]),
        )
        state = cur.fetchone()
        was_inside = state["is_inside"] if state else 0

        if is_inside_now != was_inside:
            event_type = "arrive" if is_inside_now else "leave"
            cur.execute(
                "INSERT INTO geofence_events (member_id, place_id, event_type, created_at) VALUES (%s, %s, %s, %s)",
                (member_id, place["id"], event_type, now_iso()),
            )
            fired.append(
                {
                    "place_name": place["name"],
                    "place_icon": place["icon"],
                    "event_type": event_type,
                    "member_name": member["name"],
                }
            )

        if state:
            cur.execute(
                "UPDATE member_place_state SET is_inside=%s WHERE member_id=%s AND place_id=%s",
                (is_inside_now, member_id, place["id"]),
            )
        else:
            cur.execute(
                "INSERT INTO member_place_state (member_id, place_id, is_inside) VALUES (%s, %s, %s)",
                (member_id, place["id"], is_inside_now),
            )

    return fired


@app.route("/api/family/<int:family_id>/locations", methods=["GET"])
def get_family_locations(family_id):
    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT id, name, emoji FROM members WHERE family_id=%s", (family_id,))
    members = cur.fetchall()

    result = []
    for m in members:
        cur.execute(
            "SELECT lat, lng, accuracy, speed, address, battery_level, battery_charging, updated_at "
            "FROM locations WHERE member_id=%s ORDER BY id DESC LIMIT 1",
            (m["id"],),
        )
        loc = cur.fetchone()
        result.append(
            {
                "member_id": m["id"],
                "name": m["name"],
                "emoji": m["emoji"],
                "location": dict(loc) if loc else None,
            }
        )
    return jsonify(result)


# ---------- API: 장소(지오펜스) ----------

@app.route("/api/family/<int:family_id>/places", methods=["GET"])
def list_places(family_id):
    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT * FROM places WHERE family_id=%s", (family_id,))
    rows = cur.fetchall()
    result = []
    for r in rows:
        cur.execute("SELECT member_id FROM place_targets WHERE place_id=%s", (r["id"],))
        targets = cur.fetchall()
        place = dict(r)
        place["target_member_ids"] = [t["member_id"] for t in targets]
        result.append(place)
    return jsonify(result)


@app.route("/api/family/<int:family_id>/places", methods=["POST"])
def add_place(family_id):
    data = request.get_json(force=True)
    name = (data.get("name") or "").strip()
    lat = data.get("lat")
    lng = data.get("lng")
    radius_m = data.get("radius_m", 150)
    icon = data.get("icon", "📍")
    target_member_ids = data.get("target_member_ids", [])
    if not name or lat is None or lng is None:
        return jsonify({"error": "name, lat, lng가 필요합니다"}), 400

    db = get_db()
    cur = db.cursor()
    cur.execute(
        "INSERT INTO places (family_id, name, lat, lng, radius_m, icon, created_at) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id",
        (family_id, name, lat, lng, radius_m, icon, now_iso()),
    )
    place_id = cur.fetchone()["id"]
    for mid in target_member_ids:
        cur.execute(
            "INSERT INTO place_targets (place_id, member_id) VALUES (%s, %s) "
            "ON CONFLICT DO NOTHING",
            (place_id, mid),
        )
    db.commit()
    return jsonify({
        "id": place_id, "name": name, "lat": lat, "lng": lng,
        "radius_m": radius_m, "icon": icon, "target_member_ids": target_member_ids,
    })


@app.route("/api/places/<int:place_id>", methods=["DELETE"])
def delete_place(place_id):
    db = get_db()
    cur = db.cursor()
    cur.execute("DELETE FROM geofence_events WHERE place_id=%s", (place_id,))
    cur.execute("DELETE FROM member_place_state WHERE place_id=%s", (place_id,))
    cur.execute("DELETE FROM place_targets WHERE place_id=%s", (place_id,))
    cur.execute("DELETE FROM places WHERE id=%s", (place_id,))
    db.commit()
    return jsonify({"ok": True})


# ---------- API: 지오펜스 알림 조회 ----------

@app.route("/api/family/<int:family_id>/events", methods=["GET"])
def get_events(family_id):
    """seen=0인 새 이벤트들을 가져오고 seen=1로 마킹한다."""
    db = get_db()
    cur = db.cursor()
    cur.execute(
        """
        SELECT ge.id, ge.event_type, ge.created_at,
               m.name as member_name, m.emoji as member_emoji,
               p.name as place_name, p.icon as place_icon
        FROM geofence_events ge
        JOIN members m ON m.id = ge.member_id
        JOIN places p ON p.id = ge.place_id
        WHERE m.family_id = %s AND ge.seen = 0
        ORDER BY ge.id ASC
        """,
        (family_id,),
    )
    rows = cur.fetchall()
    ids = [r["id"] for r in rows]
    if ids:
        cur.execute(
            "UPDATE geofence_events SET seen=1 WHERE id = ANY(%s)",
            (ids,),
        )
        db.commit()
    return jsonify([dict(r) for r in rows])


# ---------- 정적 파일 ----------

@app.route("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.route("/<path:path>")
def static_files(path):
    return send_from_directory(app.static_folder, path)


@app.route("/health")
def health():
    return jsonify({"status": "ok", "time": now_iso()})


init_db()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)
