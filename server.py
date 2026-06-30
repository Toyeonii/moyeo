"""
FamTrack - 가족 위치공유 앱 백엔드
Flask + SQLite, realspy와 동일한 배포 패턴 (Render.com)
"""

import os
import sqlite3
import string
import random
import time
from datetime import datetime
from flask import Flask, request, jsonify, g, send_from_directory
from flask_cors import CORS

app = Flask(__name__, static_folder="static")
CORS(app)

DB_PATH = os.path.join(os.path.dirname(__file__), "famtrack.db")

# ---------- DB ----------

def get_db():
    db = getattr(g, "_database", None)
    if db is None:
        db = g._database = sqlite3.connect(DB_PATH)
        db.row_factory = sqlite3.Row
    return db


@app.teardown_appcontext
def close_db(exception):
    db = getattr(g, "_database", None)
    if db is not None:
        db.close()


def init_db():
    db = sqlite3.connect(DB_PATH)
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS families (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT UNIQUE NOT NULL,
            name TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS members (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            family_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            emoji TEXT DEFAULT '👤',
            created_at TEXT NOT NULL,
            FOREIGN KEY (family_id) REFERENCES families (id)
        );

        CREATE TABLE IF NOT EXISTS locations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            member_id INTEGER NOT NULL,
            lat REAL NOT NULL,
            lng REAL NOT NULL,
            accuracy REAL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (member_id) REFERENCES members (id)
        );

        CREATE TABLE IF NOT EXISTS places (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            family_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            lat REAL NOT NULL,
            lng REAL NOT NULL,
            radius_m INTEGER DEFAULT 150,
            icon TEXT DEFAULT '📍',
            created_at TEXT NOT NULL,
            FOREIGN KEY (family_id) REFERENCES families (id)
        );

        CREATE TABLE IF NOT EXISTS geofence_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            member_id INTEGER NOT NULL,
            place_id INTEGER NOT NULL,
            event_type TEXT NOT NULL, -- 'arrive' | 'leave'
            created_at TEXT NOT NULL,
            seen INTEGER DEFAULT 0,
            FOREIGN KEY (member_id) REFERENCES members (id),
            FOREIGN KEY (place_id) REFERENCES places (id)
        );

        CREATE TABLE IF NOT EXISTS member_place_state (
            member_id INTEGER NOT NULL,
            place_id INTEGER NOT NULL,
            is_inside INTEGER DEFAULT 0,
            PRIMARY KEY (member_id, place_id)
        );
        """
    )
    db.commit()
    db.close()


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


def now_iso():
    return datetime.utcnow().isoformat() + "Z"


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
    for _ in range(10):
        code = gen_family_code()
        exists = db.execute("SELECT 1 FROM families WHERE code=?", (code,)).fetchone()
        if not exists:
            break
    else:
        return jsonify({"error": "코드 생성 실패, 다시 시도해주세요"}), 500

    cur = db.execute(
        "INSERT INTO families (code, name, created_at) VALUES (?, ?, ?)",
        (code, family_name, now_iso()),
    )
    family_id = cur.lastrowid
    cur2 = db.execute(
        "INSERT INTO members (family_id, name, emoji, created_at) VALUES (?, ?, ?, ?)",
        (family_id, member_name, emoji, now_iso()),
    )
    member_id = cur2.lastrowid
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
    family = db.execute("SELECT * FROM families WHERE code=?", (code,)).fetchone()
    if not family:
        return jsonify({"error": "가족 코드를 찾을 수 없습니다"}), 404

    member_count = db.execute(
        "SELECT COUNT(*) as c FROM members WHERE family_id=?", (family["id"],)
    ).fetchone()["c"]
    if member_count >= 8:
        return jsonify({"error": "가족 인원이 너무 많습니다"}), 400

    cur = db.execute(
        "INSERT INTO members (family_id, name, emoji, created_at) VALUES (?, ?, ?, ?)",
        (family["id"], member_name, emoji, now_iso()),
    )
    member_id = cur.lastrowid
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
    rows = db.execute(
        "SELECT id, name, emoji FROM members WHERE family_id=?", (family_id,)
    ).fetchall()
    return jsonify([dict(r) for r in rows])


# ---------- API: 위치 ----------

@app.route("/api/location/update", methods=["POST"])
def update_location():
    data = request.get_json(force=True)
    member_id = data.get("member_id")
    lat = data.get("lat")
    lng = data.get("lng")
    accuracy = data.get("accuracy")
    if member_id is None or lat is None or lng is None:
        return jsonify({"error": "member_id, lat, lng가 필요합니다"}), 400

    db = get_db()
    db.execute(
        "INSERT INTO locations (member_id, lat, lng, accuracy, updated_at) VALUES (?, ?, ?, ?, ?)",
        (member_id, lat, lng, accuracy, now_iso()),
    )
    db.commit()

    events = check_geofences(db, member_id, lat, lng)
    db.commit()

    return jsonify({"ok": True, "events": events})


def check_geofences(db, member_id, lat, lng):
    """현재 위치를 기준으로 등록된 장소들에 대한 출입 상태를 갱신하고
    상태가 바뀐 경우 geofence_events에 기록 후 반환한다."""
    member = db.execute("SELECT * FROM members WHERE id=?", (member_id,)).fetchone()
    if not member:
        return []
    places = db.execute(
        "SELECT * FROM places WHERE family_id=?", (member["family_id"],)
    ).fetchall()

    fired = []
    for place in places:
        dist = haversine_m(lat, lng, place["lat"], place["lng"])
        is_inside_now = 1 if dist <= place["radius_m"] else 0

        state = db.execute(
            "SELECT is_inside FROM member_place_state WHERE member_id=? AND place_id=?",
            (member_id, place["id"]),
        ).fetchone()
        was_inside = state["is_inside"] if state else 0

        if is_inside_now != was_inside:
            event_type = "arrive" if is_inside_now else "leave"
            db.execute(
                "INSERT INTO geofence_events (member_id, place_id, event_type, created_at) VALUES (?, ?, ?, ?)",
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
            db.execute(
                "UPDATE member_place_state SET is_inside=? WHERE member_id=? AND place_id=?",
                (is_inside_now, member_id, place["id"]),
            )
        else:
            db.execute(
                "INSERT INTO member_place_state (member_id, place_id, is_inside) VALUES (?, ?, ?)",
                (member_id, place["id"], is_inside_now),
            )

    return fired


@app.route("/api/family/<int:family_id>/locations", methods=["GET"])
def get_family_locations(family_id):
    db = get_db()
    members = db.execute(
        "SELECT id, name, emoji FROM members WHERE family_id=?", (family_id,)
    ).fetchall()

    result = []
    for m in members:
        loc = db.execute(
            "SELECT lat, lng, accuracy, updated_at FROM locations WHERE member_id=? ORDER BY id DESC LIMIT 1",
            (m["id"],),
        ).fetchone()
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
    rows = db.execute("SELECT * FROM places WHERE family_id=?", (family_id,)).fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/family/<int:family_id>/places", methods=["POST"])
def add_place(family_id):
    data = request.get_json(force=True)
    name = (data.get("name") or "").strip()
    lat = data.get("lat")
    lng = data.get("lng")
    radius_m = data.get("radius_m", 150)
    icon = data.get("icon", "📍")
    if not name or lat is None or lng is None:
        return jsonify({"error": "name, lat, lng가 필요합니다"}), 400

    db = get_db()
    cur = db.execute(
        "INSERT INTO places (family_id, name, lat, lng, radius_m, icon, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (family_id, name, lat, lng, radius_m, icon, now_iso()),
    )
    db.commit()
    return jsonify({"id": cur.lastrowid, "name": name, "lat": lat, "lng": lng, "radius_m": radius_m, "icon": icon})


@app.route("/api/places/<int:place_id>", methods=["DELETE"])
def delete_place(place_id):
    db = get_db()
    db.execute("DELETE FROM places WHERE id=?", (place_id,))
    db.execute("DELETE FROM member_place_state WHERE place_id=?", (place_id,))
    db.commit()
    return jsonify({"ok": True})


# ---------- API: 지오펜스 알림 조회 ----------

@app.route("/api/family/<int:family_id>/events", methods=["GET"])
def get_events(family_id):
    """seen=0인 새 이벤트들을 가져오고 seen=1로 마킹한다."""
    db = get_db()
    rows = db.execute(
        """
        SELECT ge.id, ge.event_type, ge.created_at,
               m.name as member_name, m.emoji as member_emoji,
               p.name as place_name, p.icon as place_icon
        FROM geofence_events ge
        JOIN members m ON m.id = ge.member_id
        JOIN places p ON p.id = ge.place_id
        WHERE m.family_id = ? AND ge.seen = 0
        ORDER BY ge.id ASC
        """,
        (family_id,),
    ).fetchall()
    ids = [r["id"] for r in rows]
    if ids:
        db.execute(
            f"UPDATE geofence_events SET seen=1 WHERE id IN ({','.join('?'*len(ids))})",
            ids,
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


if __name__ == "__main__":
    init_db()
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)
else:
    init_db()
