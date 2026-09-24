import os
import random
import string
import threading
import time
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

SUITS = ["♠", "♥", "♦", "♣"]
RANKS = ["2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K", "A"]
RANK_VALUE = {r: i for i, r in enumerate(RANKS, start=2)}

TRICK_PAUSE = 2.0  # حداقل چند ثانیه کارت‌های وسط میز بعد از تکمیل یه دست نمایش داده بشن
ROUND_PAUSE = 2.0  # حداکثر چند ثانیه خلاصه‌ی برنده/بازنده‌ی هر دست نمایش داده بشه

rooms = {}
lock = threading.Lock()

# ---------- بازی نقطه‌چین (Dots and Boxes) ----------
dots_rooms = {}
dots_lock = threading.Lock()
DOTS_GRID = {50: (5, 10), 100: (10, 10), 150: (10, 15)}  # (rows, cols) تعداد نقطه‌ها


def new_deck():
    deck = [f"{r}{s}" for s in SUITS for r in RANKS]
    random.shuffle(deck)
    return deck


def card_suit(c):
    return c[-1]


def card_rank(c):
    return c[:-1]


def sort_hand(hand):
    return sorted(hand, key=lambda c: (SUITS.index(card_suit(c)), RANK_VALUE[card_rank(c)]))


def gen_code():
    while True:
        code = "".join(random.choices(string.ascii_uppercase + string.digits, k=5))
        if code not in rooms:
            return code


def get_player(room, uid):
    return next(p for p in room["players"] if p["id"] == uid)


def team_of(room, uid):
    idx = next(i for i, p in enumerate(room["players"]) if p["id"] == uid)
    return idx % 2


def next_player(room, uid):
    """نفر بعدی در جهت گردش نوبت (برای نوبت‌دهی داخل دست، همون جهت قبلی)."""
    n = len(room["players"])
    idx = next(i for i, p in enumerate(room["players"]) if p["id"] == uid)
    return room["players"][(idx + 1) % n]["id"]


def prev_player(room, uid):
    """نفر قبلی؛ یعنی خلاف جهت next_player — برای چرخش حاکمیت وقتی تیم حاکم می‌بازه."""
    n = len(room["players"])
    idx = next(i for i, p in enumerate(room["players"]) if p["id"] == uid)
    return room["players"][(idx - 1) % n]["id"]


def pick_hakem(room):
    deck = new_deck()
    players = room["players"]
    n = len(players)
    i = 0
    while True:
        card = deck[i % len(deck)]
        if card == "A♠":
            return players[i % n]["id"]
        i += 1


def start_round(room):
    room["deck"] = new_deck()
    room["hands"] = {p["id"]: [] for p in room["players"]}
    room["trick"] = {}
    room["lead_suit"] = None
    room["trick_leader"] = None
    room["tricks_won"] = {0: 0, 1: 0}
    room["trump"] = None
    room["status"] = "choosing_hokm"
    room["last_trick"] = None
    room["trick_pause_until"] = None
    room["trick_winner"] = None
    room["round_result"] = None
    room["round_pause_until"] = None
    room["pending_hakem"] = None
    hakem = room["hakem"]
    for _ in range(5):
        room["hands"][hakem].append(room["deck"].pop())


def resolve_pending_trick(room):
    """اگه یه دست (trick) تکمیل شده و زمان نمایشش (TRICK_PAUSE) گذشته باشه،
    الان جمعش می‌کنیم: امتیاز می‌دیم و نوبت رو به برنده می‌سپاریم.
    تا قبل از اون، کارت‌ها همونجوری روی میز می‌مونن که کلاینت‌ها ببینن‌شون."""
    if not room.get("trick_pause_until"):
        return
    if time.time() < room["trick_pause_until"]:
        return

    winner = room["trick_winner"]
    team = team_of(room, winner)
    room["tricks_won"][team] += 1
    room["last_trick"] = {"cards": dict(room["trick"]), "winner": winner}
    room["trick"] = {}
    room["lead_suit"] = None
    room["turn"] = winner
    room["trick_leader"] = winner
    room["trick_pause_until"] = None
    room["trick_winner"] = None

    # دست (round) به محض اینکه یک تیم به ۷ ترفند برسه تموم می‌شه، نه لزوماً بعد از ۱۳ ترفند
    hand_over = room["tricks_won"][team] >= 7 or all(len(h) == 0 for h in room["hands"].values())
    if hand_over:
        win_team = 0 if room["tricks_won"][0] >= 7 else 1
        room["round_scores"][win_team] += 1
        target = room.get("target_score", 7)

        if room["round_scores"][win_team] >= target:
            room["status"] = "finished"
            room["winner_team"] = win_team
            return

        # قانون حکم: اگه تیم حاکم برنده‌ی این مچ شده باشه، همون حاکم می‌مونه.
        # فقط وقتی تیم حاکم می‌بازه، حاکمیت خلاف عقربه‌ی ساعت به نفر بعدی می‌ره.
        hakem_team = team_of(room, room["hakem"])
        pending_hakem = room["hakem"] if win_team == hakem_team else prev_player(room, room["hakem"])

        winners = [p["name"] for i, p in enumerate(room["players"]) if i % 2 == win_team]
        losers = [p["name"] for i, p in enumerate(room["players"]) if i % 2 != win_team]

        room["round_result"] = {"win_team": win_team, "winners": winners, "losers": losers}
        room["pending_hakem"] = pending_hakem
        room["status"] = "round_end"
        room["round_pause_until"] = time.time() + ROUND_PAUSE


def resolve_round_end(room):
    """بعد از نمایش خلاصه‌ی برنده/بازنده به مدت ROUND_PAUSE، دست بعدی رو شروع می‌کنه."""
    if room.get("status") != "round_end":
        return
    if not room.get("round_pause_until"):
        return
    if time.time() < room["round_pause_until"]:
        return

    room["hakem"] = room.get("pending_hakem") or room["hakem"]
    start_round(room)  # این تابع status رو خودش به choosing_hokm برمی‌گردونه و فیلدهای موقت رو پاک می‌کنه


def public_state(room, uid):
    players_info = [{"id": p["id"], "name": p["name"], "seat": i} for i, p in enumerate(room["players"])]
    return {
        "code": room["code"],
        "host": room["host"],
        "status": room["status"],
        "players": players_info,
        "max_players": room.get("max_players", 4),
        "target_score": room.get("target_score", 7),
        "hakem": room.get("hakem"),
        "trump": room.get("trump"),
        "turn": room.get("turn"),
        "trick": room.get("trick", {}),
        "tricks_won": room.get("tricks_won", {0: 0, 1: 0}),
        "round_scores": room.get("round_scores", {0: 0, 1: 0}),
        "last_trick": room.get("last_trick"),
        "round_result": room.get("round_result"),
        "winner_team": room.get("winner_team"),
        "hand": sort_hand(room["hands"].get(uid, [])) if room.get("hands") else [],
        "chat": room.get("chat", [])[-30:],
    }


def resolve_room(room):
    resolve_pending_trick(room)
    resolve_round_end(room)


def new_room(code, uid, name, max_players, target_score):
    return {
        "code": code, "host": uid,
        "players": [{"id": uid, "name": name}],
        "status": "waiting", "deck": [], "hands": {},
        "hakem": None, "trump": None, "turn": None,
        "trick": {}, "lead_suit": None, "trick_leader": None,
        "tricks_won": {0: 0, 1: 0}, "round_scores": {0: 0, 1: 0},
        "chat": [], "last_trick": None, "winner_team": None,
        "max_players": max_players, "target_score": target_score,
        "trick_pause_until": None, "trick_winner": None,
        "round_result": None, "round_pause_until": None, "pending_hakem": None,
    }


@app.route("/api/create", methods=["POST"])
def create():
    data = request.json
    uid, name = str(data["user_id"]), data.get("name", "بازیکن")
    max_players = int(data.get("max_players", 4))
    if max_players not in (2, 4):
        max_players = 4
    target_score = int(data.get("target_score", 7))
    if target_score not in (3, 5, 7):
        target_score = 7
    with lock:
        code = gen_code()
        rooms[code] = new_room(code, uid, name, max_players, target_score)
    return jsonify({"code": code, "state": public_state(rooms[code], uid)})


@app.route("/api/join", methods=["POST"])
def join():
    data = request.json
    code, uid, name = data["code"].upper(), str(data["user_id"]), data.get("name", "بازیکن")
    with lock:
        room = rooms.get(code)
        if not room:
            return jsonify({"error": "room_not_found"}), 404
        if any(p["id"] == uid for p in room["players"]):
            return jsonify({"state": public_state(room, uid)})
        if room["status"] != "waiting":
            return jsonify({"error": "already_started"}), 400
        if len(room["players"]) >= room.get("max_players", 4):
            return jsonify({"error": "room_full"}), 400
        room["players"].append({"id": uid, "name": name})
    return jsonify({"state": public_state(room, uid)})


@app.route("/api/state")
def state():
    code, uid = request.args.get("code", "").upper(), str(request.args.get("user_id"))
    room = rooms.get(code)
    if not room:
        return jsonify({"error": "room_not_found"}), 404
    with lock:
        resolve_room(room)
    return jsonify({"state": public_state(room, uid)})


@app.route("/api/start", methods=["POST"])
def start_game():
    data = request.json
    code, uid = data["code"].upper(), str(data["user_id"])
    with lock:
        room = rooms.get(code)
        if not room:
            return jsonify({"error": "room_not_found"}), 404
        if room["host"] != uid:
            return jsonify({"error": "not_host"}), 403
        need = room.get("max_players", 4)
        if len(room["players"]) != need:
            return jsonify({"error": "need_more_players"}), 400
        room["hakem"] = pick_hakem(room)
        start_round(room)
    return jsonify({"state": public_state(room, uid)})


@app.route("/api/choose_trump", methods=["POST"])
def choose_trump():
    data = request.json
    code, uid, suit = data["code"].upper(), str(data["user_id"]), data["suit"]
    with lock:
        room = rooms.get(code)
        if not room or room["status"] != "choosing_hokm":
            return jsonify({"error": "invalid_state"}), 400
        if room["hakem"] != uid:
            return jsonify({"error": "not_hakem"}), 403
        room["trump"] = suit
        room["status"] = "playing"
        for p in room["players"]:
            need = 13 - len(room["hands"][p["id"]])
            for _ in range(need):
                room["hands"][p["id"]].append(room["deck"].pop())
        room["turn"] = room["hakem"]
        room["trick_leader"] = room["hakem"]
    return jsonify({"state": public_state(room, uid)})


def trick_winner(room):
    trump = room["trump"]
    lead_suit = room["lead_suit"]

    def strength(c):
        s = card_suit(c)
        r = RANK_VALUE[card_rank(c)]
        if s == trump:
            return (2, r)
        if s == lead_suit:
            return (1, r)
        return (0, r)

    return max(room["trick"].items(), key=lambda kv: strength(kv[1]))[0]


@app.route("/api/play", methods=["POST"])
def play():
    data = request.json
    code, uid, card = data["code"].upper(), str(data["user_id"]), data["card"]
    with lock:
        room = rooms.get(code)
        if not room or room["status"] != "playing":
            return jsonify({"error": "invalid_state"}), 400

        # اگه دست قبلی هنوز روی میز مونده و زمانش تموم شده، همینجا جمعش کن
        resolve_room(room)
        if room["status"] != "playing":
            return jsonify({"state": public_state(room, uid)})

        if room["turn"] != uid:
            return jsonify({"error": "not_your_turn"}), 400
        hand = room["hands"][uid]
        if card not in hand:
            return jsonify({"error": "card_not_in_hand"}), 400
        lead_suit = room["lead_suit"]
        if lead_suit and card_suit(card) != lead_suit:
            if any(card_suit(c) == lead_suit for c in hand):
                return jsonify({"error": "must_follow_suit", "suit": lead_suit}), 400
        hand.remove(card)
        room["trick"][uid] = card
        if room["lead_suit"] is None:
            room["lead_suit"] = card_suit(card)
            room["trick_leader"] = uid

        if len(room["trick"]) < len(room["players"]):
            room["turn"] = next_player(room, uid)
        else:
            # دست تکمیل شد؛ فعلاً پاکش نمی‌کنیم تا همه کارت آخر رو ببینن
            room["trick_winner"] = trick_winner(room)
            room["trick_pause_until"] = time.time() + TRICK_PAUSE
            room["turn"] = None
    return jsonify({"state": public_state(room, uid)})


@app.route("/api/chat", methods=["POST"])
def chat():
    data = request.json
    code, uid, text = data["code"].upper(), str(data["user_id"]), data.get("text", "")[:300]
    with lock:
        room = rooms.get(code)
        if not room:
            return jsonify({"error": "room_not_found"}), 404
        name = get_player(room, uid)["name"] if any(p["id"] == uid for p in room["players"]) else "?"
        room.setdefault("chat", []).append({"name": name, "text": text})
    return jsonify({"ok": True})


def dots_gen_code():
    while True:
        code = "".join(random.choices(string.ascii_uppercase + string.digits, k=5))
        if code not in dots_rooms:
            return code


def dots_new_room(code, uid, name, max_players, dots_count):
    rows, cols = DOTS_GRID.get(dots_count, (10, 10))
    return {
        "code": code, "host": uid,
        "players": [{"id": uid, "name": name}],
        "status": "waiting",
        "max_players": max_players, "dots_count": dots_count,
        "rows": rows, "cols": cols,
        "h_lines": [[None] * (cols - 1) for _ in range(rows)],
        "v_lines": [[None] * cols for _ in range(rows - 1)],
        "boxes": [[None] * (cols - 1) for _ in range(rows - 1)],
        "scores": {uid: 0},
        "turn": None,
        "winners": None,
        "chat": [],
    }


def dots_next_player(room, uid):
    n = len(room["players"])
    idx = next(i for i, p in enumerate(room["players"]) if p["id"] == uid)
    return room["players"][(idx + 1) % n]["id"]


def dots_box_complete(room, br, bc):
    R, C = room["rows"], room["cols"]
    if br < 0 or br > R - 2 or bc < 0 or bc > C - 2:
        return False
    top = room["h_lines"][br][bc]
    bottom = room["h_lines"][br + 1][bc]
    left = room["v_lines"][br][bc]
    right = room["v_lines"][br][bc + 1]
    return top is not None and bottom is not None and left is not None and right is not None


def dots_public_state(room, uid):
    players_info = [{"id": p["id"], "name": p["name"], "seat": i} for i, p in enumerate(room["players"])]
    return {
        "code": room["code"],
        "host": room["host"],
        "status": room["status"],
        "players": players_info,
        "max_players": room.get("max_players"),
        "dots_count": room.get("dots_count"),
        "rows": room["rows"], "cols": room["cols"],
        "h_lines": room["h_lines"], "v_lines": room["v_lines"], "boxes": room["boxes"],
        "scores": room.get("scores", {}),
        "turn": room.get("turn"),
        "winners": room.get("winners"),
        "chat": room.get("chat", [])[-30:],
    }


@app.route("/api/dots/create", methods=["POST"])
def dots_create():
    data = request.json
    uid, name = str(data["user_id"]), data.get("name", "بازیکن")
    max_players = int(data.get("max_players", 2))
    if max_players < 1:
        max_players = 1
    if max_players > 10:
        max_players = 10
    dots_count = int(data.get("dots_count", 100))
    if dots_count not in DOTS_GRID:
        dots_count = 100
    with dots_lock:
        code = dots_gen_code()
        dots_rooms[code] = dots_new_room(code, uid, name, max_players, dots_count)
    return jsonify({"code": code, "state": dots_public_state(dots_rooms[code], uid)})


@app.route("/api/dots/join", methods=["POST"])
def dots_join():
    data = request.json
    code, uid, name = data["code"].upper(), str(data["user_id"]), data.get("name", "بازیکن")
    with dots_lock:
        room = dots_rooms.get(code)
        if not room:
            return jsonify({"error": "room_not_found"}), 404
        if any(p["id"] == uid for p in room["players"]):
            return jsonify({"state": dots_public_state(room, uid)})
        if room["status"] != "waiting":
            return jsonify({"error": "already_started"}), 400
        if len(room["players"]) >= room.get("max_players", 2):
            return jsonify({"error": "room_full"}), 400
        room["players"].append({"id": uid, "name": name})
        room["scores"][uid] = 0
    return jsonify({"state": dots_public_state(room, uid)})


@app.route("/api/dots/state")
def dots_state():
    code, uid = request.args.get("code", "").upper(), str(request.args.get("user_id"))
    room = dots_rooms.get(code)
    if not room:
        return jsonify({"error": "room_not_found"}), 404
    return jsonify({"state": dots_public_state(room, uid)})


@app.route("/api/dots/start", methods=["POST"])
def dots_start():
    data = request.json
    code, uid = data["code"].upper(), str(data["user_id"])
    with dots_lock:
        room = dots_rooms.get(code)
        if not room:
            return jsonify({"error": "room_not_found"}), 404
        if room["host"] != uid:
            return jsonify({"error": "not_host"}), 403
        if room["status"] != "waiting":
            return jsonify({"error": "invalid_state"}), 400
        if not room["players"]:
            return jsonify({"error": "need_more_players"}), 400
        room["status"] = "playing"
        room["turn"] = room["players"][0]["id"]
    return jsonify({"state": dots_public_state(room, uid)})


@app.route("/api/dots/move", methods=["POST"])
def dots_move():
    data = request.json
    code, uid = data["code"].upper(), str(data["user_id"])
    line_type, row, col = data["type"], int(data["row"]), int(data["col"])
    with dots_lock:
        room = dots_rooms.get(code)
        if not room or room["status"] != "playing":
            return jsonify({"error": "invalid_state"}), 400
        if room["turn"] != uid:
            return jsonify({"error": "not_your_turn"}), 400
        R, C = room["rows"], room["cols"]
        if line_type == "h":
            if not (0 <= row < R and 0 <= col < C - 1):
                return jsonify({"error": "invalid_move"}), 400
            if room["h_lines"][row][col] is not None:
                return jsonify({"error": "line_taken"}), 400
            room["h_lines"][row][col] = uid
            candidates = [(row - 1, col), (row, col)]
        elif line_type == "v":
            if not (0 <= row < R - 1 and 0 <= col < C):
                return jsonify({"error": "invalid_move"}), 400
            if room["v_lines"][row][col] is not None:
                return jsonify({"error": "line_taken"}), 400
            room["v_lines"][row][col] = uid
            candidates = [(row, col - 1), (row, col)]
        else:
            return jsonify({"error": "invalid_move"}), 400

        completed = False
        for br, bc in candidates:
            if 0 <= br <= R - 2 and 0 <= bc <= C - 2 and room["boxes"][br][bc] is None \
                    and dots_box_complete(room, br, bc):
                room["boxes"][br][bc] = uid
                room["scores"][uid] = room["scores"].get(uid, 0) + 1
                completed = True

        if not completed:
            room["turn"] = dots_next_player(room, uid)

        total_boxes = (R - 1) * (C - 1)
        filled = sum(1 for r in room["boxes"] for b in r if b is not None)
        if filled >= total_boxes:
            room["status"] = "finished"
            best = max(room["scores"].values()) if room["scores"] else 0
            room["winners"] = [pid for pid, s in room["scores"].items() if s == best]
    return jsonify({"state": dots_public_state(room, uid)})


@app.route("/api/dots/chat", methods=["POST"])
def dots_chat():
    data = request.json
    code, uid, text = data["code"].upper(), str(data["user_id"]), data.get("text", "")[:300]
    with dots_lock:
        room = dots_rooms.get(code)
        if not room:
            return jsonify({"error": "room_not_found"}), 404
        player = next((p for p in room["players"] if p["id"] == uid), None)
        name = player["name"] if player else "?"
        room.setdefault("chat", []).append({"name": name, "text": text})
    return jsonify({"ok": True})


# ---------- جستجوی بازیکن (matchmaking) ----------
MM_STALE = 8     # اگه بازیکن این‌قدر ثانیه پیام نده از صف حذف میشه
MM_KEEP = 60     # چند ثانیه نتیجه‌ی مچ برای بازیکن‌های دیگه نگه داشته میشه
mm_queues = {}   # (max_players, target_score) -> [{"id", "name", "ts"}]
mm_matched = {}  # user_id -> {"code", "ts"}


@app.route("/api/matchmake", methods=["POST"])
def matchmake():
    data = request.json
    uid, name = str(data["user_id"]), data.get("name", "بازیکن")
    n = 2 if int(data.get("max_players", 4)) == 2 else 4
    score = int(data.get("target_score", 7))
    if score not in (3, 5, 7):
        score = 7
    now = time.time()
    with lock:
        for k, v in list(mm_matched.items()):
            if now - v["ts"] > MM_KEEP:
                del mm_matched[k]
        # اگه قبلاً توی یه مچ قرار گرفته و هنوز خبر نگرفته
        if uid in mm_matched:
            return jsonify({"status": "matched", "code": mm_matched.pop(uid)["code"]})

        # بازیکن‌های بی‌جواب رو پاک کن؛ هر بازیکن فقط توی یه صف باشه
        for key, q in mm_queues.items():
            q[:] = [p for p in q
                    if (p["id"] == uid and key == (n, score)) or (p["id"] != uid and now - p["ts"] < MM_STALE)]
        q = mm_queues.setdefault((n, score), [])
        me = next((p for p in q if p["id"] == uid), None)
        if me:
            me["ts"], me["name"] = now, name
        else:
            q.append({"id": uid, "name": name, "ts": now})

        if len(q) < n:
            return jsonify({"status": "searching", "found": len(q)})

        group = q[:n]
        del q[:n]
        code = gen_code()
        room = new_room(code, group[0]["id"], group[0]["name"], n, score)
        room["players"] += [{"id": p["id"], "name": p["name"]} for p in group[1:]]
        rooms[code] = room
        for p in group:
            if p["id"] != uid:
                mm_matched[p["id"]] = {"code": code, "ts": now}
        return jsonify({"status": "matched", "code": code})


@app.route("/api/matchmake_cancel", methods=["POST"])
def matchmake_cancel():
    uid = str(request.json["user_id"])
    with lock:
        for q in mm_queues.values():
            q[:] = [p for p in q if p["id"] != uid]
        mm_matched.pop(uid, None)
    return jsonify({"ok": True})


@app.route("/")
def health():
    return open("index.html", encoding="utf-8").read()


@app.route("/<path:filename>")
def static_files(filename):
    return send_from_directory(".", filename)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
