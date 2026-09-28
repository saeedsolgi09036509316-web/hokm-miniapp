import os
import json
import hashlib
import random
import string
import threading
import time
import psycopg2
import psycopg2.extras
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

# ---- اتصال به دیتابیس Postgres (Supabase) برای ذخیره‌ی دائمی سکه‌ها و کدهای هدیه ----
# آدرس اتصال از Environment Variable به اسم DATABASE_URL خونده میشه (توی Render تنظیمش می‌کنیم)
DATABASE_URL = os.environ.get("DATABASE_URL")
db_lock = threading.Lock()


def get_db_conn():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL تنظیم نشده — باید توی Environment Variables سرویس Render اضافه بشه")
    return psycopg2.connect(DATABASE_URL, sslmode="require")


def init_db():
    """یه جدول ساده‌ی key-value می‌سازه (اگه از قبل نباشه) که کل accounts و giftcodes توش نگه‌داری میشن."""
    if not DATABASE_URL:
        print("⚠️ DATABASE_URL تنظیم نشده؛ دیتا فقط توی حافظه‌ی موقت می‌مونه و با هر دیپلوی از بین میره.")
        return
    try:
        with db_lock:
            conn = get_db_conn()
            try:
                with conn.cursor() as cur:
                    cur.execute("""
                        CREATE TABLE IF NOT EXISTS kv_store (
                            key TEXT PRIMARY KEY,
                            value JSONB NOT NULL,
                            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
                        )
                    """)
                conn.commit()
            finally:
                conn.close()
    except Exception as e:
        print(f"⚠️ خطا توی ساخت جدول دیتابیس: {e}")


def kv_get(key, default):
    if not DATABASE_URL:
        return default
    try:
        with db_lock:
            conn = get_db_conn()
            try:
                with conn.cursor() as cur:
                    cur.execute("SELECT value FROM kv_store WHERE key = %s", (key,))
                    row = cur.fetchone()
                    return row[0] if row else default
            finally:
                conn.close()
    except Exception as e:
        print(f"⚠️ خطا توی خوندن {key} از دیتابیس: {e}")
        return default


def kv_set(key, value):
    if not DATABASE_URL:
        return
    try:
        with db_lock:
            conn = get_db_conn()
            try:
                with conn.cursor() as cur:
                    cur.execute("""
                        INSERT INTO kv_store (key, value, updated_at)
                        VALUES (%s, %s, now())
                        ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()
                    """, (key, psycopg2.extras.Json(value)))
                conn.commit()
            finally:
                conn.close()
    except Exception as e:
        print(f"⚠️ خطا توی ذخیره‌ی {key} توی دیتابیس: {e}")


init_db()

SUITS = ["♠", "♥", "♦", "♣"]
RANKS = ["2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K", "A"]
RANK_VALUE = {r: i for i, r in enumerate(RANKS, start=2)}

TRICK_PAUSE = 2.0  # حداقل چند ثانیه کارت‌های وسط میز بعد از تکمیل یه دست نمایش داده بشن
ROUND_PAUSE = 2.0  # حداکثر چند ثانیه خلاصه‌ی برنده/بازنده‌ی هر دست نمایش داده بشه

# اگه از یه بازیکن این‌قدر ثانیه هیچ درخواستی (poll/heartbeat) نیاد، قطع‌شده حساب میشه
DISCONNECT_TIMEOUT = 12


def touch_player(room, uid):
    """هر بار یه بازیکن درخواستی می‌فرسته (poll یا اکشن)، زمانش رو ثبت می‌کنیم؛
    این خودش جای heartbeat جداگونه رو برای تشخیص قطعی می‌گیره."""
    room.setdefault("last_seen", {})[uid] = time.time()

# ---- اکانت کاربر (آیدی + پسورد) با ذخیره روی دیتابیس Postgres، تا با دیپلوی جدید از بین نره ----
accounts_lock = threading.Lock()


def load_accounts():
    return kv_get("accounts", {})


def save_accounts(data):
    kv_set("accounts", data)


accounts = load_accounts()


def refresh_accounts():
    """قبل از هر عملیاتی که حساب کاربری رو تغییر میده، آخرین نسخه رو از دیتابیس می‌خونیم.
    چون سرور معمولاً با چند پردازش (worker) اجرا میشه، متغیر accounts فقط تو حافظه‌ی
    همون یه پردازشه؛ بدون این کار، وقتی درخواست بعدی به یه پردازش دیگه بخوره،
    موجودی/آیتم‌های قدیمی (قبل از آخرین خرید یا استفاده) رو می‌بینه و دوباره ذخیره می‌کنه،
    که باعث میشه مثلاً تعداد آیتم جاسوس هرچقدر مصرف بشه بازم به عدد قبلی برگرده."""
    global accounts
    if DATABASE_URL:
        accounts = load_accounts()
    return accounts


def hash_pw(pw):
    return hashlib.sha256(("sholex_" + pw).encode("utf-8")).hexdigest()


@app.route("/api/account/auth", methods=["POST"])
def account_auth():
    """اگه آیدی وجود نداره، حساب جدید می‌سازه؛ اگه هست، پسورد رو چک می‌کنه."""
    data = request.get_json(force=True) or {}
    acc_id = str(data.get("id", "")).strip()
    pw = str(data.get("password", ""))
    if len(acc_id) < 3 or len(pw) < 4:
        return jsonify({"ok": False, "error": "آیدی حداقل ۳ و رمز حداقل ۴ کاراکتر باشه"}), 400
    with accounts_lock:
        refresh_accounts()
        acc = accounts.get(acc_id)
        if acc is None:
            acc = {"password": hash_pw(pw), "coins": 0, "owned": ["classic"], "sel": "classic", "items": {}}
            accounts[acc_id] = acc
            save_accounts(accounts)
            is_new = True
        else:
            if acc.get("password") != hash_pw(pw):
                return jsonify({"ok": False, "error": "رمز اشتباهه"}), 401
            is_new = False
        return jsonify({
            "ok": True, "new": is_new,
            "coins": acc.get("coins", 0),
            "owned": acc.get("owned", ["classic"]),
            "sel": acc.get("sel", "classic"),
            "items": acc.get("items", {}),
        })


@app.route("/api/account/sync", methods=["POST"])
def account_sync():
    """بعد از برد سکه گرفتن یا خرید میز، وضعیت جدید رو روی سرور ذخیره می‌کنه."""
    data = request.get_json(force=True) or {}
    acc_id = str(data.get("id", "")).strip()
    pw = str(data.get("password", ""))
    with accounts_lock:
        refresh_accounts()
        acc = accounts.get(acc_id)
        if not acc or acc.get("password") != hash_pw(pw):
            return jsonify({"ok": False, "error": "احراز هویت نامعتبر"}), 401
        if "coins" in data:
            try:
                acc["coins"] = max(0, int(data["coins"]))
            except (TypeError, ValueError):
                pass
        if "owned" in data and isinstance(data["owned"], list):
            acc["owned"] = data["owned"]
        if "sel" in data:
            acc["sel"] = str(data["sel"])
        # نکته‌ی امنیتی: "items" (آیتم‌های قابل‌مصرف مثل جاسوس) از اینجا قابل تنظیم نیست؛
        # فقط از طریق /api/item/buy (با چک واقعی سکه) و /api/spy (با کم‌کردن واقعی موجودی) تغییر می‌کنه،
        # وگرنه هرکسی می‌تونست بدون خرید، مستقیم موجودی آیتمش رو از همینجا ست کنه.
        save_accounts(accounts)
    return jsonify({"ok": True})


@app.route("/api/leaderboard", methods=["GET"])
def leaderboard():
    """۱۰ نفر برتر بر اساس تعداد سکه؛ فقط آیدی و سکه برگردونده میشه، نه رمز یا اطلاعات دیگه."""
    with accounts_lock:
        refresh_accounts()
        rows = sorted(
            ({"id": acc_id, "coins": acc.get("coins", 0)} for acc_id, acc in accounts.items()),
            key=lambda r: r["coins"], reverse=True
        )[:10]
    return jsonify({"ok": True, "top": rows})


# ---- خرید آیتم مصرفی (مثل جاسوس حکم): کاملاً سمت سرور چک میشه، کلاینت فقط درخواست می‌فرسته ----
ITEM_PRICES = {"spy": 2000, "detective": 2000}


@app.route("/api/item/buy", methods=["POST"])
def item_buy():
    """خرید آیتم: قیمت و کسر سکه و افزایش موجودی، همه‌ش اینجا رو سرور انجام میشه
    تا کسی نتونه بدون سکه‌ی واقعی، آیتم مجانی برای خودش بسازه."""
    data = request.get_json(force=True) or {}
    acc_id = str(data.get("id", "")).strip()
    pw = str(data.get("password", ""))
    item = str(data.get("item", ""))
    try:
        amount = int(data.get("amount", 1))
    except (TypeError, ValueError):
        amount = 1
    if item not in ITEM_PRICES or amount <= 0:
        return jsonify({"ok": False, "error": "آیتم نامعتبر"}), 400
    with accounts_lock:
        refresh_accounts()
        acc = accounts.get(acc_id)
        if not acc or acc.get("password") != hash_pw(pw):
            return jsonify({"ok": False, "error": "احراز هویت نامعتبر"}), 401
        cost = ITEM_PRICES[item] * amount
        if acc.get("coins", 0) < cost:
            return jsonify({"ok": False, "error": "سکه‌ی کافی نداری"}), 400
        acc["coins"] -= cost
        items = acc.setdefault("items", {})
        items[item] = items.get(item, 0) + amount
        save_accounts(accounts)
        return jsonify({"ok": True, "coins": acc["coins"], "items": acc["items"]})


# ---- کد هدیه: یه کد که فقط N نفر اول می‌تونن بزنن و سکه بگیرن، بعدش خودکار غیرفعال میشه ----
giftcodes_lock = threading.Lock()
ADMIN_CODE = "Tatalooss1383"  # همون کدیه که تو index.html برای ادمین گذاشتی؛ اگه اونجا عوضش کردی اینجا هم عوض کن


def load_giftcodes():
    return kv_get("giftcodes", {})


def save_giftcodes(data):
    kv_set("giftcodes", data)


giftcodes = load_giftcodes()


def gen_gift_code(n=8):
    alphabet = string.ascii_uppercase + string.digits
    return "".join(random.choice(alphabet) for _ in range(n))


@app.route("/api/giftcode/create", methods=["POST"])
def giftcode_create():
    """ساخت کد هدیه‌ی جدید (فقط با کد ادمین). amount = سکه‌ای که هر نفر می‌گیره، max_uses = حداکثر تعداد نفر."""
    data = request.get_json(force=True) or {}
    if str(data.get("admin_code", "")) != ADMIN_CODE:
        return jsonify({"ok": False, "error": "کد ادمین اشتباهه"}), 401
    try:
        amount = int(data.get("amount", 1000))
        max_uses = int(data.get("max_uses", 5))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "مقدار نامعتبر"}), 400
    if amount <= 0 or max_uses <= 0:
        return jsonify({"ok": False, "error": "مقدار نامعتبر"}), 400
    code = str(data.get("code") or "").strip().upper()
    with giftcodes_lock:
        if not code:
            code = gen_gift_code()
            while code in giftcodes:
                code = gen_gift_code()
        giftcodes[code] = {"amount": amount, "max_uses": max_uses, "used_by": []}
        save_giftcodes(giftcodes)
    return jsonify({"ok": True, "code": code, "amount": amount, "max_uses": max_uses})


@app.route("/api/giftcode/redeem", methods=["POST"])
def giftcode_redeem():
    """کاربر کد رو وارد می‌کنه؛ اگه معتبر باشه، قبلاً نگرفته باشه و ظرفیتش پر نشده باشه، سکه بهش اضافه میشه."""
    data = request.get_json(force=True) or {}
    acc_id = str(data.get("id", "")).strip()
    pw = str(data.get("password", ""))
    code = str(data.get("code", "")).strip().upper()
    if not code:
        return jsonify({"ok": False, "error": "کد رو وارد کن"}), 400
    with accounts_lock:
        refresh_accounts()
        acc = accounts.get(acc_id)
        if not acc or acc.get("password") != hash_pw(pw):
            return jsonify({"ok": False, "error": "احراز هویت نامعتبر"}), 401
        with giftcodes_lock:
            gc = giftcodes.get(code)
            if not gc:
                return jsonify({"ok": False, "error": "همچین کدی پیدا نشد"}), 404
            if acc_id in gc["used_by"]:
                return jsonify({"ok": False, "error": "قبلاً این کد رو زدی"}), 400
            if len(gc["used_by"]) >= gc["max_uses"]:
                return jsonify({"ok": False, "error": "ظرفیت این کد پر شده، دیگه فعال نیست"}), 400
            gc["used_by"].append(acc_id)
            acc["coins"] = acc.get("coins", 0) + gc["amount"]
            save_giftcodes(giftcodes)
            save_accounts(accounts)
            coins_now = acc["coins"]
            remaining = gc["max_uses"] - len(gc["used_by"])
    return jsonify({"ok": True, "coins": coins_now, "amount": gc["amount"], "remaining": remaining})


@app.route("/api/admin/verify", methods=["POST"])
def admin_verify():
    """فقط چک می‌کنه کد ادمین درسته یا نه؛ برای باز کردن پنل ادمین توی کلاینت."""
    data = request.get_json(force=True) or {}
    if str(data.get("admin_code", "")) != ADMIN_CODE:
        return jsonify({"ok": False, "error": "کد ادمین اشتباهه"}), 401
    return jsonify({"ok": True})


@app.route("/api/admin/add_coins", methods=["POST"])
def admin_add_coins():
    """با کد ادمین، به اکانت مشخص‌شده سکه اضافه می‌کنه (روی سرور، نه کلاینت)."""
    data = request.get_json(force=True) or {}
    if str(data.get("admin_code", "")) != ADMIN_CODE:
        return jsonify({"ok": False, "error": "کد ادمین اشتباهه"}), 401
    acc_id = str(data.get("id", "")).strip()
    try:
        amount = int(data.get("amount", 0))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "مقدار نامعتبر"}), 400
    if not acc_id or amount == 0:
        return jsonify({"ok": False, "error": "مقدار نامعتبر"}), 400
    with accounts_lock:
        refresh_accounts()
        acc = accounts.get(acc_id)
        if not acc:
            return jsonify({"ok": False, "error": "اکانتی با این آیدی پیدا نشد"}), 404
        acc["coins"] = max(0, acc.get("coins", 0) + amount)
        save_accounts(accounts)
        coins_now = acc["coins"]
    return jsonify({"ok": True, "coins": coins_now})


rooms = {}
lock = threading.Lock()

# ---------- آمار کاربران آنلاین ----------
presence = {}  # user_id -> زمان آخرین heartbeat
presence_lock = threading.Lock()
ONLINE_WINDOW = 20  # ثانیه؛ اگه این‌قدر از کاربر heartbeat نیاد آفلاین حساب میشه


def online_count():
    now = time.time()
    with presence_lock:
        stale = [uid for uid, ts in presence.items() if now - ts > ONLINE_WINDOW]
        for uid in stale:
            del presence[uid]
        return len(presence)


@app.route("/api/heartbeat", methods=["POST"])
def heartbeat():
    data = request.json or {}
    uid = str(data.get("user_id", "")).strip()
    if uid and uid != "None":
        with presence_lock:
            presence[uid] = time.time()
    return jsonify({"ok": True, "count": online_count()})


@app.route("/api/online")
def online():
    return jsonify({"count": online_count()})

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
    room["spy_used"] = {}
    room["exposed_cards"] = {}
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
        "left_players": room.get("left", []),
        "left_player": room.get("left_player"),
        "spy_result": room.get("spy_used", {}).get(uid),
        "exposed_cards": room.get("exposed_cards", {}).get(uid, []),
    }


def hokm_autoplay_for_left(room):
    """اگه نوبت با یه بازیکن قطع‌شده باشه، به‌جاش بازی می‌کنه تا بقیه معطل نمونن."""
    left = room.get("left") or []
    if not left:
        return
    if room["status"] == "choosing_hokm" and room.get("hakem") in left:
        room["trump"] = random.choice(SUITS)
        room["status"] = "playing"
        for pp in room["players"]:
            need = 13 - len(room["hands"][pp["id"]])
            for _ in range(need):
                room["hands"][pp["id"]].append(room["deck"].pop())
        room["turn"] = room["hakem"]
        room["trick_leader"] = room["hakem"]
    if room["status"] == "playing" and room.get("turn") in left:
        pid = room["turn"]
        hand = room["hands"].get(pid, [])
        if not hand:
            return
        lead_suit = room.get("lead_suit")
        card = next((c for c in hand if lead_suit and card_suit(c) == lead_suit), None) or hand[0]
        hand.remove(card)
        room["trick"][pid] = card
        if room["lead_suit"] is None:
            room["lead_suit"] = card_suit(card)
            room["trick_leader"] = pid
        if len(room["trick"]) < len(room["players"]):
            room["turn"] = next_player(room, pid)
        else:
            room["trick_winner"] = trick_winner(room)
            room["trick_pause_until"] = time.time() + TRICK_PAUSE
            room["turn"] = None


def hokm_mark_left(room, uid):
    """یه بازیکن رفته (چه با دکمه‌ی خروج، چه با قطعی/بی‌پاسخی طولانی)."""
    left = room.setdefault("left", [])
    if uid in left:
        return
    left.append(uid)
    if room["status"] in ("waiting", "finished"):
        return
    n = len(room["players"])
    if n <= 2:
        # بازی دونفره: با رفتن یکی، بازی همون لحظه به نفع حریف تموم میشه
        other = next((pp["id"] for pp in room["players"] if pp["id"] != uid), None)
        if other:
            room["status"] = "finished"
            room["winner_team"] = team_of(room, other)
            room["left_player"] = uid
    else:
        # بیش از دو نفر: فقط نوبتش رو رد می‌کنیم که بقیه معطل نشن
        room["left_player"] = uid
        for _ in range(n):
            t, s = room.get("turn"), room.get("status")
            hokm_autoplay_for_left(room)
            if room.get("turn") == t and room.get("status") == s:
                break


def hokm_check_timeouts(room):
    if room["status"] in ("waiting", "finished"):
        return
    now = time.time()
    last_seen = room.get("last_seen", {})
    for p in room["players"]:
        pid = p["id"]
        if pid in room.get("left", []):
            continue
        ts = last_seen.get(pid)
        if ts is not None and now - ts > DISCONNECT_TIMEOUT:
            hokm_mark_left(room, pid)
            break


def resolve_room(room):
    resolve_pending_trick(room)
    resolve_round_end(room)
    hokm_check_timeouts(room)
    if len(room["players"]) > 2:
        for _ in range(len(room["players"])):
            t, s = room.get("turn"), room.get("status")
            hokm_autoplay_for_left(room)
            if room.get("turn") == t and room.get("status") == s:
                break


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
        "spy_used": {}, "exposed_cards": {},
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
        touch_player(room, uid)
    return jsonify({"state": public_state(room, uid)})


@app.route("/api/state")
def state():
    code, uid = request.args.get("code", "").upper(), str(request.args.get("user_id"))
    room = rooms.get(code)
    if not room:
        return jsonify({"error": "room_not_found"}), 404
    with lock:
        touch_player(room, uid)
        resolve_room(room)
    return jsonify({"state": public_state(room, uid)})


@app.route("/api/leave", methods=["POST"])
def leave_room():
    data = request.json
    code, uid = data["code"].upper(), str(data["user_id"])
    with lock:
        room = rooms.get(code)
        if not room:
            return jsonify({"ok": True})
        if room["status"] == "waiting":
            room["players"] = [p for p in room["players"] if p["id"] != uid]
            if room["players"] and room["host"] == uid:
                room["host"] = room["players"][0]["id"]
            if not room["players"]:
                rooms.pop(code, None)
        else:
            hokm_mark_left(room, uid)
    return jsonify({"ok": True})


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
        touch_player(room, uid)
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
        touch_player(room, uid)
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

        touch_player(room, uid)
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


@app.route("/api/spy", methods=["POST"])
def spy_reveal():
    """آیتم جاسوس: مشخص می‌کنه یه کارت مشخص الان تو دست کدوم بازیکنه.
    فقط برای همون کسی که درخواست داده نشون داده میشه؛ برای صاحب کارت فقط
    دور همون کارت تو دستش قرمز میشه (بدون اینکه بفهمه کی جاسوسیش رو کرده).
    همه‌چی سمت سرور چک میشه: رمز حساب و موجودی واقعی آیتم، تا کسی نتونه
    بدون خرید یا بیشتر از چیزی که داره، از این قابلیت استفاده کنه."""
    data = request.json
    code, uid, card = data["code"].upper(), str(data["user_id"]), str(data.get("card", ""))
    pw = str(data.get("password", ""))
    with lock:
        room = rooms.get(code)
        if not room or room.get("status") != "playing":
            return jsonify({"error": "invalid_state"}), 400
        with accounts_lock:
            refresh_accounts()
            acc = accounts.get(uid)
            if not acc or acc.get("password") != hash_pw(pw):
                return jsonify({"error": "auth"}), 401
            if acc.get("items", {}).get("spy", 0) <= 0:
                return jsonify({"error": "no_item"}), 400
            acc["items"]["spy"] -= 1
            save_accounts(accounts)
            items_left = dict(acc["items"])
        touch_player(room, uid)
        holder = next((p["id"] for p in room["players"] if card in room["hands"].get(p["id"], [])), None)
        if holder:
            room.setdefault("spy_used", {})[uid] = {"card": card, "holder": holder}
            exposed = room.setdefault("exposed_cards", {}).setdefault(holder, [])
            if card not in exposed:
                exposed.append(card)
    return jsonify({"state": public_state(room, uid), "items": items_left, "found": bool(holder)})


@app.route("/api/chat", methods=["POST"])
def chat():
    data = request.json
    code, uid, text = data["code"].upper(), str(data["user_id"]), data.get("text", "")[:300]
    reaction = bool(data.get("reaction", False))
    with lock:
        room = rooms.get(code)
        if not room:
            return jsonify({"error": "room_not_found"}), 404
        name = get_player(room, uid)["name"] if any(p["id"] == uid for p in room["players"]) else "?"
        room.setdefault("chat", []).append({"name": name, "text": text, "reaction": reaction})
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
        "left_players": room.get("left", []),
        "left_player": room.get("left_player"),
    }


def dots_random_empty_line(room):
    R, C = room["rows"], room["cols"]
    empties = [("h", r, c) for r in range(R) for c in range(C - 1) if room["h_lines"][r][c] is None]
    empties += [("v", r, c) for r in range(R - 1) for c in range(C) if room["v_lines"][r][c] is None]
    return random.choice(empties) if empties else None


def dots_apply_move(room, uid, line_type, row, col):
    R, C = room["rows"], room["cols"]
    if line_type == "h":
        if room["h_lines"][row][col] is not None:
            return
        room["h_lines"][row][col] = uid
        candidates = [(row - 1, col), (row, col)]
    else:
        if room["v_lines"][row][col] is not None:
            return
        room["v_lines"][row][col] = uid
        candidates = [(row, col - 1), (row, col)]

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


def dots_autoplay_for_left(room):
    left = room.get("left") or []
    if not left or room["status"] != "playing":
        return
    for _ in range((room["rows"]) * (room["cols"]) * 2 + 4):
        if room.get("turn") not in left:
            return
        mv = dots_random_empty_line(room)
        if not mv:
            return
        line_type, row, col = mv
        dots_apply_move(room, room["turn"], line_type, row, col)
        if room["status"] != "playing":
            return


def dots_mark_left(room, uid):
    left = room.setdefault("left", [])
    if uid in left:
        return
    left.append(uid)
    if room["status"] in ("waiting", "finished"):
        return
    n = len(room["players"])
    if n <= 2:
        other = next((pp["id"] for pp in room["players"] if pp["id"] != uid), None)
        if other:
            room["status"] = "finished"
            room["winners"] = [other]
            room["left_player"] = uid
    else:
        room["left_player"] = uid
        dots_autoplay_for_left(room)


def dots_check_timeouts(room):
    if room["status"] in ("waiting", "finished"):
        return
    now = time.time()
    last_seen = room.get("last_seen", {})
    for p in room["players"]:
        pid = p["id"]
        if pid in room.get("left", []):
            continue
        ts = last_seen.get(pid)
        if ts is not None and now - ts > DISCONNECT_TIMEOUT:
            dots_mark_left(room, pid)
            break


def dots_resolve_room(room):
    dots_check_timeouts(room)
    dots_autoplay_for_left(room)


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
        touch_player(room, uid)
    return jsonify({"state": dots_public_state(room, uid)})


@app.route("/api/dots/state")
def dots_state():
    code, uid = request.args.get("code", "").upper(), str(request.args.get("user_id"))
    room = dots_rooms.get(code)
    if not room:
        return jsonify({"error": "room_not_found"}), 404
    with dots_lock:
        touch_player(room, uid)
        dots_resolve_room(room)
    return jsonify({"state": dots_public_state(room, uid)})


@app.route("/api/dots/leave", methods=["POST"])
def dots_leave():
    data = request.json
    code, uid = data["code"].upper(), str(data["user_id"])
    with dots_lock:
        room = dots_rooms.get(code)
        if not room:
            return jsonify({"ok": True})
        if room["status"] == "waiting":
            room["players"] = [p for p in room["players"] if p["id"] != uid]
            room["scores"].pop(uid, None)
            if room["players"] and room["host"] == uid:
                room["host"] = room["players"][0]["id"]
            if not room["players"]:
                dots_rooms.pop(code, None)
        else:
            dots_mark_left(room, uid)
    return jsonify({"ok": True})


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
        touch_player(room, uid)
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
        touch_player(room, uid)
        dots_resolve_room(room)
        if room["status"] != "playing":
            return jsonify({"state": dots_public_state(room, uid)})
        if room["turn"] != uid:
            return jsonify({"error": "not_your_turn"}), 400
        R, C = room["rows"], room["cols"]
        if line_type == "h":
            if not (0 <= row < R and 0 <= col < C - 1):
                return jsonify({"error": "invalid_move"}), 400
            if room["h_lines"][row][col] is not None:
                return jsonify({"error": "line_taken"}), 400
        elif line_type == "v":
            if not (0 <= row < R - 1 and 0 <= col < C):
                return jsonify({"error": "invalid_move"}), 400
            if room["v_lines"][row][col] is not None:
                return jsonify({"error": "line_taken"}), 400
        else:
            return jsonify({"error": "invalid_move"}), 400

        dots_apply_move(room, uid, line_type, row, col)
    return jsonify({"state": dots_public_state(room, uid)})


@app.route("/api/dots/chat", methods=["POST"])
def dots_chat():
    data = request.json
    code, uid, text = data["code"].upper(), str(data["user_id"]), data.get("text", "")[:300]
    reaction = bool(data.get("reaction", False))
    with dots_lock:
        room = dots_rooms.get(code)
        if not room:
            return jsonify({"error": "room_not_found"}), 404
        player = next((p for p in room["players"] if p["id"] == uid), None)
        name = player["name"] if player else "?"
        room.setdefault("chat", []).append({"name": name, "text": text, "reaction": reaction})
    return jsonify({"ok": True})


# ---------- بازی اسم و فامیل ----------
nf_rooms = {}
nf_lock = threading.Lock()

NF_CATEGORIES_FEW = ["اسم", "فامیل", "شهر", "حیوان", "میوه", "رنگ", "غذا"]
NF_CATEGORIES_MANY = NF_CATEGORIES_FEW + ["اشیاء", "ماشین", "کشور", "ورزش", "شغل"]
NF_CATEGORIES_BY_MODE = {"few": NF_CATEGORIES_FEW, "many": NF_CATEGORIES_MANY}
NF_CATEGORIES = NF_CATEGORIES_FEW  # برای سازگاری با کدهای قدیمی
NF_LETTERS = ["ا", "آ", "ب", "پ", "ت", "ث", "ج", "چ", "ح", "خ", "د", "ذ", "ر", "ز", "ژ", "س", "ش", "ص", "ض", "ط", "ظ", "ع", "غ", "ف", "ق", "ک", "گ", "ل", "م", "ن", "و", "ه", "ی"]
NF_ROUND_TIME_FEW = 60    # ثانیه؛ زمان هر دور وقتی سوال‌ها کمه
NF_ROUND_TIME_MANY = 120  # ثانیه؛ زمان هر دور وقتی سوال‌ها زیاده
NF_ROUND_TIME_BY_MODE = {"few": NF_ROUND_TIME_FEW, "many": NF_ROUND_TIME_MANY}
NF_GRACE_TIME = 5     # بعد از اینکه یه نفر «تمام» زد، بقیه چقدر فرصت دارن
NF_CHALLENGE_TIME = 60  # چند ثانیه بعد از هر دور، بقیه فرصت دارن به یه جواب مشکوک رای بدن
NF_RESULT_PAUSE = 6   # بعد از رسیدگی به رای‌ها، چند ثانیه نتیجه‌ی نهایی نمایش داده بشه

NF_CHAR_MAP = str.maketrans({"ي": "ی", "ك": "ک", "ة": "ه", "ۀ": "ه", "إ": "ا", "أ": "ا", "ٱ": "ا"})

# ---- آیتم «کارآگاه»: یه جواب معتبر (طبق حرفِ همون دور) برای یه دسته‌ی مشخص به بازیکن می‌ده ----
# دیتابیسِ کلمات، دسته به دسته و حرف به حرف. اگه برای یه ترکیبِ خاص (دسته+حرف) کلمه‌ای نباشه،
# یعنی همچین ترکیبی به‌ندرت پیش میاد؛ اونجا به بازیکن می‌گیم «جوابی پیدا نشد» (بدون این‌که آیتمش برگرده).
NF_HINT_WORDS = {
    "اسم": {
        "ا": ["اکبر", "احمد", "امیر", "ابراهیم"], "آ": ["آرش", "آرمین", "آیدا", "آناهیتا"],
        "ب": ["بهرام", "بابک", "بیتا", "بهار"], "پ": ["پرویز", "پویا", "پریسا", "پانیذ"],
        "ت": ["تینا", "تارا", "تورج"], "ج": ["جواد", "جمشید", "جلال"],
        "چ": ["چیستا"], "د": ["داریوش", "داوود", "دنیا", "دلارا"],
        "ر": ["رضا", "رامین", "رویا", "رها"], "ز": ["زهرا", "زینب", "زانیار", "زیبا"],
        "س": ["سارا", "سینا", "سهراب", "سمیرا"], "ش": ["شیرین", "شهرام", "شادی", "شیوا"],
        "ص": ["صادق", "صابر", "صبا", "صنم"], "ط": ["طاها", "طوبی", "طاهر"],
        "ف": ["فرهاد", "فریبا", "فرزاد", "فاطمه"], "ق": ["قاسم", "قباد"],
        "ک": ["کوروش", "کیانا", "کامران", "کیمیا"], "گ": ["گلناز", "گیتی", "گلشن"],
        "ل": ["لیلا", "لادن", "لیدا"], "م": ["محمد", "مریم", "مهدی", "مینا"],
        "ن": ["نیما", "نسرین", "نازنین", "نادر"], "و": ["وحید", "ویدا", "وریا"],
        "ه": ["هومن", "هستی", "هدیه", "هانیه"], "ی": ["یاسمین", "یوسف", "یگانه", "یاسر"],
    },
    "فامیل": {
        "ا": ["احمدی", "اکبری", "امینی"], "آ": ["آقایی", "آزاد", "آبادی"],
        "ب": ["بهرامی", "براتی", "بیگی"], "پ": ["پارسا", "پناهی"],
        "ت": ["تقوی", "ترابی"], "ج": ["جعفری", "جوادی"],
        "چ": ["چراغی"], "د": ["دهقان", "درویشی"],
        "ر": ["رضایی", "رستمی"], "ز": ["زارعی", "زمانی"],
        "س": ["سلطانی", "سعیدی"], "ش": ["شریفی", "شکوری"],
        "ص": ["صادقی", "صالحی"], "ط": ["طاهری", "طالبی"],
        "ف": ["فرجی", "فتحی"], "ق": ["قاسمی", "قربانی"],
        "ک": ["کریمی", "کاظمی"], "گ": ["گودرزی", "گلچین"],
        "ل": ["لطفی"], "م": ["محمدی", "مرادی"],
        "ن": ["نوری", "نجفی"], "و": ["وکیلی"],
        "ه": ["هاشمی", "هاتفی"], "ی": ["یوسفی", "یزدانی"],
    },
    "شهر": {
        "ا": ["اصفهان", "اهواز", "اراک", "ارومیه"], "آ": ["آمل", "آبادان"],
        "ب": ["بندرعباس", "بجنورد", "بروجرد"], "پ": ["پاوه"],
        "ت": ["تهران", "تبریز"], "ج": ["جیرفت", "جهرم"],
        "چ": ["چابهار"], "د": ["دزفول", "دماوند"],
        "ر": ["رشت", "رفسنجان"], "ز": ["زاهدان", "زنجان"],
        "س": ["سنندج", "سبزوار", "ساری"], "ش": ["شیراز", "شهرکرد", "شاهرود"],
        "ص": ["صومعه‌سرا"], "ط": ["طبس"],
        "ف": ["فسا", "فردیس"], "ق": ["قم", "قزوین"],
        "ک": ["کرج", "کرمان", "کاشان"], "گ": ["گرگان", "گناباد"],
        "ل": ["لاهیجان", "لار"], "م": ["مشهد", "مراغه"],
        "ن": ["نیشابور", "نجف‌آباد"], "و": ["ورامین"],
        "ه": ["همدان"], "ی": ["یزد", "یاسوج"],
    },
    "حیوان": {
        "ا": ["اسب", "اردک"], "آ": ["آهو"],
        "ب": ["ببر", "بز", "بوقلمون"], "پ": ["پلنگ", "پروانه"],
        "ت": ["تمساح"], "ج": ["جغد", "جوجه‌تیغی"],
        "چ": ["چکاوک"], "د": ["دلفین"],
        "ر": ["روباه"], "ز": ["زرافه"],
        "س": ["سگ", "سمور"], "ش": ["شیر", "شتر"],
        "ص": [], "ط": ["طاووس", "طوطی"],
        "ف": ["فیل", "فلامینگو"], "ق": ["قناری", "قورباغه"],
        "ک": ["کلاغ", "کانگورو", "کرگدن"], "گ": ["گاو", "گربه", "گوزن"],
        "ل": ["لاک‌پشت", "لاما"], "م": ["مار", "موش", "میمون"],
        "ن": ["نهنگ"], "و": ["وال"],
        "ه": ["هدهد"], "ی": ["یوزپلنگ"],
    },
    "میوه": {
        "ا": ["انار", "انگور", "انبه"], "آ": ["آلو", "آناناس", "آلبالو"],
        "ب": ["به"], "پ": ["پرتقال"],
        "ت": ["توت", "تمشک"], "ج": [],
        "چ": [], "د": [],
        "ر": ["رطب"], "ز": ["زردآلو", "زیتون"],
        "س": ["سیب", "سنجد"], "ش": ["شاتوت"],
        "ص": [], "ط": ["طالبی"],
        "ف": [], "ق": [],
        "ک": ["کیوی"], "گ": ["گلابی", "گوجه"],
        "ل": ["لیمو"], "م": ["موز"],
        "ن": ["نارنگی", "نارگیل"], "و": [],
        "ه": ["هلو"], "ی": [],
    },
    "رنگ": {
        "ا": ["ارغوانی"], "آ": [],
        "ب": ["بنفش"], "پ": ["پرتقالی"],
        "ت": [], "ج": [], "چ": [], "د": [],
        "ر": [], "ز": ["زرد"],
        "س": ["سبز", "سرمه‌ای"], "ش": [],
        "ص": ["صورتی"], "ط": ["طلایی"],
        "ف": ["فیروزه‌ای"], "ق": ["قرمز", "قهوه‌ای"],
        "ک": ["کرم", "کبود"], "گ": [],
        "ل": [], "م": ["مشکی"],
        "ن": ["نارنجی", "نقره‌ای"], "و": [],
        "ه": [], "ی": ["یاسی"],
    },
    "غذا": {
        "ا": ["استانبولی‌پلو"], "آ": ["آش", "آبگوشت"],
        "ب": ["باقالی‌پلو"], "پ": ["پلو"],
        "ت": ["ته‌چین"], "ج": ["جوجه‌کباب"],
        "چ": ["چلوکباب"], "د": ["دلمه"],
        "ر": ["رشته‌پلو"], "ز": ["زرشک‌پلو"],
        "س": ["سبزی‌پلو"], "ش": ["شامی", "شیرین‌پلو"],
        "ص": [], "ط": [],
        "ف": ["فسنجان"], "ق": ["قورمه‌سبزی", "قیمه"],
        "ک": ["کباب", "کوکو"], "گ": [],
        "ل": ["لوبیاپلو"], "م": ["ماکارونی"],
        "ن": ["نان"], "و": [],
        "ه": ["هویج‌پلو"], "ی": [],
    },
    "اشیاء": {
        "ا": ["انگشتر", "اتو"], "آ": ["آینه", "آباژور"],
        "ب": ["بالش", "برس"], "پ": ["پتو", "پرده"],
        "ت": ["تلفن", "تخت"], "ج": ["جارو"],
        "چ": ["چاقو", "چراغ"], "د": ["در", "دفتر"],
        "ر": ["رادیو"], "ز": ["زنگ"],
        "س": ["ساعت", "سنجاق"], "ش": ["شمع", "شانه"],
        "ص": ["صندلی", "صابون"], "ط": ["طناب"],
        "ف": ["فرش", "فنجان"], "ق": ["قاشق", "قفل"],
        "ک": ["کتاب", "کیف"], "گ": ["گلدان"],
        "ل": ["لیوان", "لامپ"], "م": ["میز", "مداد"],
        "ن": ["نردبان"], "و": ["وان"],
        "ه": ["هدفون"], "ی": ["یخچال"],
    },
    "ماشین": {
        "ا": ["ایسوزو"], "آ": ["آئودی"],
        "ب": ["بنز"], "پ": ["پژو", "پورشه"],
        "ت": ["تویوتا"], "ج": ["جیپ"],
        "چ": ["چری"], "د": ["دوو", "دنا"],
        "ر": ["رنو"], "ز": ["زامیاد"],
        "س": ["سمند", "ساینا"], "ش": ["شورولت"],
        "ص": [], "ط": [],
        "ف": ["فراری"], "ق": [],
        "ک": ["کیا"], "گ": [],
        "ل": ["لکسوس", "لندرور"], "م": ["مزدا", "مینی"],
        "ن": ["نیسان"], "و": ["ولوو"],
        "ه": ["هیوندای", "هوندا"], "ی": [],
    },
    "کشور": {
        "ا": ["ایران", "افغانستان", "انگلیس"], "آ": ["آلمان", "آمریکا", "آرژانتین"],
        "ب": ["برزیل", "بلژیک"], "پ": ["پاکستان", "پرتغال"],
        "ت": ["ترکیه", "تایلند"], "ج": ["جامائیکا"],
        "چ": ["چین", "چک"], "د": ["دانمارک"],
        "ر": ["روسیه"], "ز": ["زامبیا", "زیمبابوه"],
        "س": ["سوریه", "سوئد", "سنگاپور"], "ش": ["شیلی"],
        "ص": [], "ط": [],
        "ف": ["فرانسه", "فنلاند"], "ق": ["قطر", "قزاقستان"],
        "ک": ["کانادا", "کوبا", "کره"], "گ": ["گرجستان", "گینه"],
        "ل": ["لبنان", "لیبی"], "م": ["مصر", "مکزیک"],
        "ن": ["نروژ", "نیجریه"], "و": ["ونزوئلا", "ویتنام"],
        "ه": ["هند", "هلند"], "ی": ["یونان", "یمن"],
    },
    "ورزش": {
        "ا": ["اسکیت"], "آ": [],
        "ب": ["بسکتبال", "بوکس"], "پ": ["پینگ‌پنگ"],
        "ت": ["تنیس", "تکواندو"], "ج": ["جودو"],
        "چ": ["چوگان"], "د": ["دوچرخه‌سواری"],
        "ر": ["رزمی"], "ز": [],
        "س": ["سافتبال"], "ش": ["شنا", "شطرنج"],
        "ص": [], "ط": [],
        "ف": ["فوتبال", "فوتسال"], "ق": ["قایقرانی"],
        "ک": ["کشتی", "کاراته", "کبدی"], "گ": ["گلف"],
        "ل": [], "م": ["موتورسواری"],
        "ن": [], "و": ["والیبال"],
        "ه": ["هندبال"], "ی": ["یوگا"],
    },
    "شغل": {
        "ا": ["استاد"], "آ": ["آشپز", "آرایشگر"],
        "ب": ["بنا", "باغبان"], "پ": ["پزشک", "پرستار"],
        "ت": ["تعمیرکار"], "ج": ["جراح"],
        "چ": ["چوپان"], "د": ["دکتر", "دامپزشک"],
        "ر": ["راننده"], "ز": ["زرگر"],
        "س": ["سرباز", "سرآشپز"], "ش": ["شهردار"],
        "ص": ["صندوقدار", "صحاف"], "ط": ["طراح", "طلافروش"],
        "ف": ["فروشنده"], "ق": ["قاضی"],
        "ک": ["کارگر", "کشاورز"], "گ": ["گارسون"],
        "ل": ["لوله‌کش"], "م": ["معلم", "مهندس", "منشی"],
        "ن": ["نجار", "نویسنده", "نقاش"], "و": ["وکیل"],
        "ه": ["هنرمند"], "ی": [],
    },
}
NF_DETECTIVE_PRICE = 2000


def nf_norm(s):
    return (s or "").strip().translate(NF_CHAR_MAP)


def nf_gen_code():
    while True:
        code = "".join(random.choices(string.ascii_uppercase + string.digits, k=5))
        if code not in nf_rooms:
            return code


def nf_next_letter(room):
    remaining = [l for l in NF_LETTERS if l not in room["used_letters"]]
    if not remaining:
        room["used_letters"] = []
        remaining = NF_LETTERS[:]
    letter = random.choice(remaining)
    room["used_letters"].append(letter)
    return letter


def nf_new_room(code, uid, name, max_players, target_rounds, question_mode="few"):
    if question_mode not in NF_CATEGORIES_BY_MODE:
        question_mode = "few"
    return {
        "code": code, "host": uid,
        "players": [{"id": uid, "name": name}],
        "status": "waiting",
        "max_players": max_players, "target_rounds": target_rounds,
        "question_mode": question_mode,
        "categories": NF_CATEGORIES_BY_MODE[question_mode],
        "round_time": NF_ROUND_TIME_BY_MODE[question_mode],
        "round": 0, "letter": None, "used_letters": [],
        "round_end_at": None, "finished_by": None, "grace_until": None,
        "answers": {}, "scores": {uid: 0},
        "round_result": None, "round_result_until": None,
        "challenge_ready": [],
        "winners": None, "chat": [],
    }


def nf_start_round(room):
    room["round"] += 1
    room["letter"] = nf_next_letter(room)
    room["round_end_at"] = time.time() + room.get("round_time", NF_ROUND_TIME_FEW)
    room["finished_by"] = None
    room["grace_until"] = None
    room["answers"] = {p["id"]: {} for p in room["players"]}
    room["status"] = "playing"
    room["round_result"] = None
    room["round_result_until"] = None
    room["challenge_ready"] = []


def nf_resolve_round(room):
    categories = room.get("categories", NF_CATEGORIES_FEW)
    letter = room["letter"]
    per_category = {}
    for cat in categories:
        counts = {}
        valid_of = {}
        for p in room["players"]:
            ans = nf_norm(room["answers"].get(p["id"], {}).get(cat, ""))
            ok = bool(ans) and ans[0] == letter
            valid_of[p["id"]] = ans if ok else ""
            if ok:
                counts[ans] = counts.get(ans, 0) + 1
        per_category[cat] = (valid_of, counts)

    breakdown = {p["id"]: {} for p in room["players"]}
    round_scores = {p["id"]: 0 for p in room["players"]}
    for cat in categories:
        valid_of, counts = per_category[cat]
        for p in room["players"]:
            uid = p["id"]
            ans_raw = room["answers"].get(uid, {}).get(cat, "") or ""
            valid = valid_of[uid]
            if not valid:
                pts = 0
            elif counts[valid] > 1:
                pts = 5
            else:
                pts = 10
            breakdown[uid][cat] = {"answer": ans_raw, "points": pts}
            round_scores[uid] += pts

    # امتیازها هنوز به جمع کل اضافه نمیشن؛ اول یه فرصت رای‌گیری داده میشه
    # (مثلاً جواب «آب پلو» تو دسته‌ی غذا معنی نداره، بقیه می‌تونن روش رای بدن)
    room["round_result"] = {"letter": letter, "breakdown": breakdown, "round_scores": round_scores}
    room["challenges"] = {}
    room["challenge_finalized"] = False
    room["challenge_ready"] = []
    room["round_result_until"] = time.time() + NF_CHALLENGE_TIME
    room["status"] = "round_result"


def nf_finalize_round(room):
    """اعتراض‌های تأییدشده (اکثریت بازیکن‌های فعال) رو اعمال می‌کنه و امتیاز نهایی رو جمع می‌زنه."""
    breakdown = room["round_result"]["breakdown"]
    round_scores = dict(room["round_result"]["round_scores"])
    active_ids = [p["id"] for p in room["players"] if p["id"] not in room.get("left", [])]
    for target, cats in room.get("challenges", {}).items():
        if target not in breakdown:
            continue
        eligible = [pid for pid in active_ids if pid != target]
        threshold = len(eligible) // 2 + 1
        if threshold == 0:
            continue
        for cat, challengers in cats.items():
            valid_challengers = [c for c in challengers if c in eligible]
            entry = breakdown[target].get(cat)
            if entry and entry.get("points", 0) > 0 and len(valid_challengers) >= threshold:
                round_scores[target] -= entry["points"]
                entry["points"] = 0
                entry["rejected"] = True

    for uid, pts in round_scores.items():
        room["scores"][uid] = room["scores"].get(uid, 0) + pts

    room["round_result"]["round_scores"] = round_scores
    room["challenge_finalized"] = True
    room["round_result_until"] = time.time() + NF_RESULT_PAUSE


def nf_mark_left(room, uid):
    left = room.setdefault("left", [])
    if uid in left:
        return
    left.append(uid)
    if room["status"] in ("waiting", "finished"):
        return
    n = len(room["players"])
    if n <= 2:
        # بازی دونفره: با رفتن یکی، بازی همون لحظه به نفع حریف تموم میشه
        other = next((pp["id"] for pp in room["players"] if pp["id"] != uid), None)
        if other:
            room["status"] = "finished"
            room["winners"] = [other]
            room["left_player"] = uid
    else:
        # بیش از دو نفر: بازی از قبل زمان‌محوره، پس بقیه معطل بازیکن رفته نمی‌مونن؛
        # فقط علامت می‌زنیم که رفته
        room["left_player"] = uid


def nf_check_timeouts(room):
    if room["status"] in ("waiting", "finished"):
        return
    now = time.time()
    last_seen = room.get("last_seen", {})
    for p in room["players"]:
        pid = p["id"]
        if pid in room.get("left", []):
            continue
        ts = last_seen.get(pid)
        if ts is not None and now - ts > DISCONNECT_TIMEOUT:
            nf_mark_left(room, pid)
            break


def nf_resolve_room(room):
    nf_check_timeouts(room)
    if room["status"] == "finished":
        return
    if room["status"] == "playing":
        now = time.time()
        timeout = room.get("round_end_at") and now >= room["round_end_at"]
        grace_done = room.get("grace_until") and now >= room["grace_until"]
        if timeout or grace_done:
            nf_resolve_round(room)
    elif room["status"] == "round_result":
        if not room.get("challenge_finalized"):
            active_ids = [p["id"] for p in room["players"] if p["id"] not in room.get("left", [])]
            ready = room.get("challenge_ready", [])
            all_ready = bool(active_ids) and all(pid in ready for pid in active_ids)
            timeout = room.get("round_result_until") and time.time() >= room["round_result_until"]
            if all_ready or timeout:
                nf_finalize_round(room)
        elif room.get("round_result_until") and time.time() >= room["round_result_until"]:
            if room["round"] >= room["target_rounds"]:
                best = max(room["scores"].values()) if room["scores"] else 0
                room["winners"] = [uid for uid, s in room["scores"].items() if s == best]
                room["status"] = "finished"
            else:
                nf_start_round(room)


def nf_public_state(room, uid):
    players_info = [{"id": p["id"], "name": p["name"], "seat": i} for i, p in enumerate(room["players"])]
    return {
        "code": room["code"], "host": room["host"], "status": room["status"],
        "players": players_info, "max_players": room.get("max_players"),
        "target_rounds": room.get("target_rounds"), "round": room.get("round"),
        "question_mode": room.get("question_mode", "few"),
        "letter": room.get("letter"), "categories": room.get("categories", NF_CATEGORIES_FEW),
        "round_end_at": room.get("round_end_at"), "finished_by": room.get("finished_by"),
        "grace_until": room.get("grace_until"),
        "scores": room.get("scores", {}),
        "round_result": room.get("round_result"),
        "round_result_until": room.get("round_result_until"),
        "challenges": room.get("challenges", {}),
        "challenge_finalized": room.get("challenge_finalized", False),
        "challenge_ready": room.get("challenge_ready", []),
        "winners": room.get("winners"),
        "chat": room.get("chat", [])[-30:],
        "left_players": room.get("left", []),
        "left_player": room.get("left_player"),
        "detective_used": room.get("detective_used", {}).get(uid) == room.get("round"),
    }


@app.route("/api/nf/create", methods=["POST"])
def nf_create():
    data = request.json
    uid, name = str(data["user_id"]), data.get("name", "بازیکن")
    max_players = int(data.get("max_players", 4))
    if max_players not in (2, 3, 4, 5, 6):
        max_players = 4
    target_rounds = int(data.get("target_rounds", 5))
    if target_rounds not in (3, 5, 8):
        target_rounds = 5
    question_mode = str(data.get("question_mode", "few"))
    if question_mode not in NF_CATEGORIES_BY_MODE:
        question_mode = "few"
    with nf_lock:
        code = nf_gen_code()
        nf_rooms[code] = nf_new_room(code, uid, name, max_players, target_rounds, question_mode)
    return jsonify({"code": code, "state": nf_public_state(nf_rooms[code], uid)})


@app.route("/api/nf/join", methods=["POST"])
def nf_join():
    data = request.json
    code, uid, name = data["code"].upper(), str(data["user_id"]), data.get("name", "بازیکن")
    with nf_lock:
        room = nf_rooms.get(code)
        if not room:
            return jsonify({"error": "room_not_found"}), 404
        if any(p["id"] == uid for p in room["players"]):
            return jsonify({"state": nf_public_state(room, uid)})
        if room["status"] != "waiting":
            return jsonify({"error": "already_started"}), 400
        if len(room["players"]) >= room.get("max_players", 4):
            return jsonify({"error": "room_full"}), 400
        room["players"].append({"id": uid, "name": name})
        room["scores"][uid] = 0
        touch_player(room, uid)
    return jsonify({"state": nf_public_state(room, uid)})


@app.route("/api/nf/state")
def nf_state():
    code, uid = request.args.get("code", "").upper(), str(request.args.get("user_id"))
    room = nf_rooms.get(code)
    if not room:
        return jsonify({"error": "room_not_found"}), 404
    with nf_lock:
        touch_player(room, uid)
        nf_resolve_room(room)
    return jsonify({"state": nf_public_state(room, uid)})


@app.route("/api/nf/challenge", methods=["POST"])
def nf_challenge():
    data = request.json
    code, uid = data["code"].upper(), str(data["user_id"])
    target = str(data.get("target_id"))
    cat = data.get("category")
    with nf_lock:
        room = nf_rooms.get(code)
        if not room:
            return jsonify({"error": "room_not_found"}), 404
        touch_player(room, uid)
        nf_resolve_room(room)
        if room["status"] != "round_result" or room.get("challenge_finalized"):
            return jsonify({"error": "invalid_state"}), 400
        if cat not in room.get("categories", NF_CATEGORIES_FEW):
            return jsonify({"error": "invalid_category"}), 400
        if uid == target:
            return jsonify({"error": "cannot_challenge_self"}), 400
        entry = room.get("round_result", {}).get("breakdown", {}).get(target, {}).get(cat)
        if not entry or entry.get("points", 0) <= 0:
            return jsonify({"error": "nothing_to_challenge"}), 400
        ch = room.setdefault("challenges", {}).setdefault(target, {}).setdefault(cat, [])
        if uid in ch:
            ch.remove(uid)  # زدن دوباره‌ی دکمه = پس گرفتن اعتراض
        else:
            ch.append(uid)
    return jsonify({"state": nf_public_state(room, uid)})


@app.route("/api/nf/vote_done", methods=["POST"])
def nf_vote_done():
    """بازیکن اعلام می‌کنه رای‌گیریش تموم شده؛ وقتی همه‌ی بازیکن‌های فعال این دکمه رو بزنن،
    بدون صبر کردن تا پایان ۲۵ ثانیه، بلافاصله میره سراغ دور بعد."""
    data = request.json
    code, uid = data["code"].upper(), str(data["user_id"])
    with nf_lock:
        room = nf_rooms.get(code)
        if not room:
            return jsonify({"error": "room_not_found"}), 404
        touch_player(room, uid)
        nf_resolve_room(room)
        if room["status"] != "round_result" or room.get("challenge_finalized"):
            return jsonify({"error": "invalid_state"}), 400
        ready = room.setdefault("challenge_ready", [])
        if uid not in ready:
            ready.append(uid)
        nf_resolve_room(room)  # اگه با این یکی همه آماده شدن، همین الان رد بشه
    return jsonify({"state": nf_public_state(room, uid)})


@app.route("/api/nf/leave", methods=["POST"])
def nf_leave():
    data = request.json
    code, uid = data["code"].upper(), str(data["user_id"])
    with nf_lock:
        room = nf_rooms.get(code)
        if not room:
            return jsonify({"ok": True})
        if room["status"] == "waiting":
            room["players"] = [p for p in room["players"] if p["id"] != uid]
            room["scores"].pop(uid, None)
            if room["players"] and room["host"] == uid:
                room["host"] = room["players"][0]["id"]
            if not room["players"]:
                nf_rooms.pop(code, None)
        else:
            nf_mark_left(room, uid)
    return jsonify({"ok": True})


@app.route("/api/nf/detective", methods=["POST"])
def nf_detective():
    """آیتم کارآگاه: طبق حرفِ همون دور، یه جواب معتبر برای یه دسته‌ی مشخص به بازیکن می‌ده.
    مثل جاسوسِ حکم، همه‌چی (موجودی واقعی آیتم + رمز حساب) سمت سرور چک میشه؛ فقط یک‌بار
    در هر دور قابل استفاده‌ست (با شماره‌ی دور تو room['detective_used'] چک میشه)."""
    data = request.json
    code, uid = data["code"].upper(), str(data["user_id"])
    pw = str(data.get("password", ""))
    category = data.get("category")
    with nf_lock:
        room = nf_rooms.get(code)
        if not room:
            return jsonify({"error": "room_not_found"}), 404
        touch_player(room, uid)
        nf_resolve_room(room)
        if room["status"] != "playing":
            return jsonify({"error": "invalid_state"}), 400
        if category not in room.get("categories", NF_CATEGORIES_FEW):
            return jsonify({"error": "invalid_category"}), 400
        if room.get("detective_used", {}).get(uid) == room.get("round"):
            return jsonify({"error": "already_used"}), 400
        with accounts_lock:
            refresh_accounts()
            acc = accounts.get(uid)
            if not acc or acc.get("password") != hash_pw(pw):
                return jsonify({"error": "auth"}), 401
            if acc.get("items", {}).get("detective", 0) <= 0:
                return jsonify({"error": "no_item"}), 400
            acc["items"]["detective"] -= 1
            save_accounts(accounts)
            items_left = dict(acc["items"])
        room.setdefault("detective_used", {})[uid] = room["round"]
        letter = room.get("letter")
        options = NF_HINT_WORDS.get(category, {}).get(letter, [])
        word = random.choice(options) if options else None
    return jsonify({"state": nf_public_state(room, uid), "items": items_left, "word": word, "found": bool(word)})


@app.route("/api/nf/start", methods=["POST"])
def nf_start():
    data = request.json
    code, uid = data["code"].upper(), str(data["user_id"])
    with nf_lock:
        room = nf_rooms.get(code)
        if not room:
            return jsonify({"error": "room_not_found"}), 404
        if room["host"] != uid:
            return jsonify({"error": "not_host"}), 403
        if room["status"] != "waiting":
            return jsonify({"error": "invalid_state"}), 400
        if len(room["players"]) < 2:
            return jsonify({"error": "need_more_players"}), 400
        touch_player(room, uid)
        nf_start_round(room)
    return jsonify({"state": nf_public_state(room, uid)})


@app.route("/api/nf/submit", methods=["POST"])
def nf_submit():
    data = request.json
    code, uid = data["code"].upper(), str(data["user_id"])
    answers = data.get("answers", {})
    with nf_lock:
        room = nf_rooms.get(code)
        if not room or room["status"] != "playing":
            return jsonify({"error": "invalid_state"}), 400
        if uid not in room["answers"]:
            return jsonify({"error": "not_in_room"}), 400
        touch_player(room, uid)
        cats = room.get("categories", NF_CATEGORIES_FEW)
        room["answers"][uid] = {cat: str(answers.get(cat, ""))[:40] for cat in cats}
        nf_resolve_room(room)
    return jsonify({"state": nf_public_state(room, uid)})


@app.route("/api/nf/finish", methods=["POST"])
def nf_finish():
    data = request.json
    code, uid = data["code"].upper(), str(data["user_id"])
    answers = data.get("answers", {})
    with nf_lock:
        room = nf_rooms.get(code)
        if not room or room["status"] != "playing":
            return jsonify({"error": "invalid_state"}), 400
        if uid not in room["answers"]:
            return jsonify({"error": "not_in_room"}), 400
        touch_player(room, uid)
        cats = room.get("categories", NF_CATEGORIES_FEW)
        room["answers"][uid] = {cat: str(answers.get(cat, ""))[:40] for cat in cats}
        if not room.get("finished_by"):
            room["finished_by"] = uid
            room["grace_until"] = time.time() + NF_GRACE_TIME
        nf_resolve_room(room)
    return jsonify({"state": nf_public_state(room, uid)})


@app.route("/api/nf/chat", methods=["POST"])
def nf_chat():
    data = request.json
    code, uid, text = data["code"].upper(), str(data["user_id"]), data.get("text", "")[:300]
    reaction = bool(data.get("reaction", False))
    with nf_lock:
        room = nf_rooms.get(code)
        if not room:
            return jsonify({"error": "room_not_found"}), 404
        player = next((p for p in room["players"] if p["id"] == uid), None)
        name = player["name"] if player else "?"
        room.setdefault("chat", []).append({"name": name, "text": text, "reaction": reaction})
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


# ---------- جستجوی بازیکن نقطه‌چین ----------
dots_mm_queues = {}   # (max_players, dots_count) -> [{"id", "name", "ts"}]
dots_mm_matched = {}  # user_id -> {"code", "ts"}


@app.route("/api/dots/matchmake", methods=["POST"])
def dots_matchmake():
    data = request.json
    uid, name = str(data["user_id"]), data.get("name", "بازیکن")
    n = int(data.get("max_players", 2))
    if n < 2:
        n = 2
    if n > 10:
        n = 10
    dots_count = int(data.get("dots_count", 100))
    if dots_count not in DOTS_GRID:
        dots_count = 100
    now = time.time()
    with dots_lock:
        for k, v in list(dots_mm_matched.items()):
            if now - v["ts"] > MM_KEEP:
                del dots_mm_matched[k]
        if uid in dots_mm_matched:
            return jsonify({"status": "matched", "code": dots_mm_matched.pop(uid)["code"]})

        for key, q in dots_mm_queues.items():
            q[:] = [p for p in q
                    if (p["id"] == uid and key == (n, dots_count)) or (p["id"] != uid and now - p["ts"] < MM_STALE)]
        q = dots_mm_queues.setdefault((n, dots_count), [])
        me = next((p for p in q if p["id"] == uid), None)
        if me:
            me["ts"], me["name"] = now, name
        else:
            q.append({"id": uid, "name": name, "ts": now})

        if len(q) < n:
            return jsonify({"status": "searching", "found": len(q)})

        group = q[:n]
        del q[:n]
        code = dots_gen_code()
        room = dots_new_room(code, group[0]["id"], group[0]["name"], n, dots_count)
        room["players"] += [{"id": p["id"], "name": p["name"]} for p in group[1:]]
        for p in group[1:]:
            room["scores"][p["id"]] = 0
        dots_rooms[code] = room
        for p in group:
            if p["id"] != uid:
                dots_mm_matched[p["id"]] = {"code": code, "ts": now}
        return jsonify({"status": "matched", "code": code})


@app.route("/api/dots/matchmake_cancel", methods=["POST"])
def dots_matchmake_cancel():
    uid = str(request.json["user_id"])
    with dots_lock:
        for q in dots_mm_queues.values():
            q[:] = [p for p in q if p["id"] != uid]
        dots_mm_matched.pop(uid, None)
    return jsonify({"ok": True})


# ---------- جستجوی بازیکن اسم و فامیل ----------
nf_mm_queues = {}   # (max_players, target_rounds) -> [{"id", "name", "ts"}]
nf_mm_matched = {}  # user_id -> {"code", "ts"}


@app.route("/api/nf/matchmake", methods=["POST"])
def nf_matchmake():
    data = request.json
    uid, name = str(data["user_id"]), data.get("name", "بازیکن")
    n = int(data.get("max_players", 4))
    if n not in (2, 3, 4, 5, 6):
        n = 4
    target_rounds = int(data.get("target_rounds", 5))
    if target_rounds not in (3, 5, 8):
        target_rounds = 5
    question_mode = str(data.get("question_mode", "few"))
    if question_mode not in NF_CATEGORIES_BY_MODE:
        question_mode = "few"
    now = time.time()
    with nf_lock:
        for k, v in list(nf_mm_matched.items()):
            if now - v["ts"] > MM_KEEP:
                del nf_mm_matched[k]
        if uid in nf_mm_matched:
            return jsonify({"status": "matched", "code": nf_mm_matched.pop(uid)["code"]})

        mm_key = (n, target_rounds, question_mode)
        for key, q in nf_mm_queues.items():
            q[:] = [p for p in q
                    if (p["id"] == uid and key == mm_key) or (p["id"] != uid and now - p["ts"] < MM_STALE)]
        q = nf_mm_queues.setdefault(mm_key, [])
        me = next((p for p in q if p["id"] == uid), None)
        if me:
            me["ts"], me["name"] = now, name
        else:
            q.append({"id": uid, "name": name, "ts": now})

        if len(q) < n:
            return jsonify({"status": "searching", "found": len(q)})

        group = q[:n]
        del q[:n]
        code = nf_gen_code()
        room = nf_new_room(code, group[0]["id"], group[0]["name"], n, target_rounds, question_mode)
        room["players"] += [{"id": p["id"], "name": p["name"]} for p in group[1:]]
        for p in group[1:]:
            room["scores"][p["id"]] = 0
        nf_rooms[code] = room
        for p in group:
            if p["id"] != uid:
                nf_mm_matched[p["id"]] = {"code": code, "ts": now}
        return jsonify({"status": "matched", "code": code})


@app.route("/api/nf/matchmake_cancel", methods=["POST"])
def nf_matchmake_cancel():
    uid = str(request.json["user_id"])
    with nf_lock:
        for q in nf_mm_queues.values():
            q[:] = [p for p in q if p["id"] != uid]
        nf_mm_matched.pop(uid, None)
    return jsonify({"ok": True})


# ---------- بازی «کی دروغ میگه؟» ----------
lg_rooms = {}
lg_lock = threading.Lock()

LG_QUESTIONS_NORMAL = [
    "عجیب‌ترین غذایی که تا حالا خوردی چی بود؟",
    "بدترین کاری که تو مدرسه یا دانشگاه کردی چی بود؟",
    "خجالت‌آورترین اتفاقی که برات افتاده چیه؟",
    "عجیب‌ترین خوابی که تا حالا دیدی چی بود؟",
    "بامزه‌ترین دروغی که تا حالا به یکی گفتی چی بود؟",
    "ترسناک‌ترین تجربه‌ی زندگیت چی بوده؟",
    "عجیب‌ترین هدیه‌ای که تا حالا گرفتی چی بود؟",
    "بدترین قرار ملاقاتی که تا حالا داشتی چطور بود؟",
    "خنده‌دارترین اتفاقی که جلوی جمع برات افتاد چی بود؟",
    "عجیب‌ترین ترسی که داری چیه؟",
    "بدترین سوتی‌ای که تا حالا دادی چی بود؟",
    "عجیب‌ترین کاری که موقع تنها بودن می‌کنی چیه؟",
    "بامزه‌ترین لقبی که تا حالا داشتی چی بوده؟",
    "احمقانه‌ترین دلیلی که برای دیر رسیدن یه‌جا آوردی چی بود؟",
    "عجیب‌ترین چیزی که خریدی و بعدش پشیمون شدی چی بود؟",
    "بامزه‌ترین اتفاقی که تو یه مهمونی برات افتاد چی بود؟",
    "عجیب‌ترین کاری که برای جلب توجه یکی کردی چی بود؟",
    "خنده‌دارترین چیزی که تا حالا تو گوگل سرچ کردی چی بود؟",
    "بدترین هدیه‌ای که تا حالا دادی چی بود؟",
    "عجیب‌ترین عادتی که داری چیه؟",
    "بامزه‌ترین اسم مستعاری که تا حالا برای یکی گذاشتی چی بود؟",
    "عجیب‌ترین چیزی که همیشه تو کیف یا جیبته چیه؟",
    "خنده‌دارترین دعوایی که با یکی از دوستات داشتی چی بود؟",
    "بدترین تصمیمی که صبح تازه از خواب بیدار شده گرفتی چی بود؟",
    "عجیب‌ترین چیزی که تا حالا تو خیابون دیدی چی بود؟",
]
LG_QUESTIONS_BOLD = [
    "بی‌پرواترین پیامی که تا حالا برای یکی فرستادی چی بود؟",
    "خجالت‌آورترین اتفاقی که سر یه قرار عاشقانه برات افتاد چی بود؟",
    "عجیب‌ترین دلیلی که برای بهم زدن یه رابطه شنیدی یا گفتی چی بود؟",
    "جسورانه‌ترین کاری که برای جلب توجه کسی که ازش خوشت میومد کردی چیه؟",
    "بامزه‌ترین سوتی‌ای که موقع فلرت کردن دادی چی بود؟",
    "عجیب‌ترین چیزی که تو پروفایلت گذاشتی تا کسی جذب بشه چی بود؟",
    "بدترین دلیل رد کردن یه پیشنهاد دوستی که شنیدی یا گفتی چی بود؟",
    "خجالت‌آورترین چیزی که یکی موقع خداحافظی بهت گفت چی بود؟",
    "عجیب‌ترین قرار اولی که تا حالا داشتی چطور بود؟",
    "بی‌ادبانه‌ترین سوالی که یه غریبه ازت پرسید چی بود؟",
    "خنده‌دارترین دروغی که برای جا زدن پیش کسی گفتی چی بود؟",
    "عجیب‌ترین چیزی که یکی موقع پیام دادن بهت گفت چی بود؟",
    "بدترین لقبی که یه معشوق قدیمی برات گذاشته بود چی بود؟",
    "جسورانه‌ترین کاری که موقع هیجان‌زدگی انجام دادی چیه؟",
    "عجیب‌ترین جایی که تا حالا با یکی قرار گذاشتی کجا بود؟",
    "خجالت‌آورترین چیزی که خانواده‌ت درباره‌ی رابطه‌هات بهت گفتن چی بود؟",
    "بامزه‌ترین اتفاقی که موقع معرفی کردن دوست‌پسر یا دوست‌دخترت به خانواده افتاد چی بود؟",
    "عجیب‌ترین چیزی که تو جمع دوستانت درباره‌ی روابط عشقیت افشا شد چی بود؟",
    "بی‌پرواترین کاری که تو یه مهمونی جلوی همه کردی چی بود؟",
    "خنده‌دارترین اتفاقی که موقع در آغوش گرفتن یا بوسیدن پیش اومد چی بود؟",
    "عجیب‌ترین دلیلی که یکی گفت چرا ساعت‌ها جواب پیامتو نداده چی بود؟",
    "جسورانه‌ترین کامنتی که زیر عکس یکی گذاشتی چی بود؟",
    "بدترین تجربه‌ای که از یه اپ دوست‌یابی داشتی چی بود؟",
    "عجیب‌ترین چیزی که تا حالا برای دل بردن کسی خریدی چی بود؟",
    "بی‌پرواترین اعترافی که تا حالا به یکی کردی چی بود؟",
]
LG_QUESTIONS_BY_MODE = {"normal": LG_QUESTIONS_NORMAL, "bold": LG_QUESTIONS_BOLD}
LG_QUESTIONS = LG_QUESTIONS_NORMAL  # برای سازگاری با کدهای قدیمی
LG_WRITE_TIME = 60     # ثانیه؛ زمان نوشتن جواب
LG_VOTE_TIME = 45      # ثانیه؛ زمان رای دادن به دروغگو
LG_RESULT_PAUSE = 7    # ثانیه؛ نمایش نتیجه قبل از دور بعد
LG_CORRECT_POINTS = 10  # امتیاز هر کسی که درست دروغگو رو پیدا کنه
LG_LIAR_ESCAPE_POINTS = 20  # امتیاز دروغگو اگه هیچ‌کس گولش رو نخوره


def lg_gen_code():
    while True:
        code = "".join(random.choices(string.ascii_uppercase + string.digits, k=5))
        if code not in lg_rooms:
            return code


def lg_new_room(code, uid, name, max_players, target_rounds, question_mode="normal"):
    if question_mode not in LG_QUESTIONS_BY_MODE:
        question_mode = "normal"
    return {
        "code": code, "host": uid,
        "players": [{"id": uid, "name": name}],
        "status": "waiting",
        "max_players": max_players, "target_rounds": target_rounds,
        "question_mode": question_mode,
        "round": 0, "used_questions": [], "question": None, "liar_id": None,
        "round_end_at": None, "answers": {}, "ready": [],
        "vote_end_at": None, "votes": {}, "answer_order": [],
        "scores": {uid: 0},
        "round_result": None, "round_result_until": None,
        "winners": None, "chat": [],
    }


def lg_start_round(room):
    room["round"] += 1
    pool = LG_QUESTIONS_BY_MODE.get(room.get("question_mode", "normal"), LG_QUESTIONS_NORMAL)
    available = [q for q in pool if q not in room["used_questions"]]
    if not available:
        room["used_questions"] = []
        available = pool[:]
    q = random.choice(available)
    room["used_questions"].append(q)
    room["question"] = q
    room["liar_id"] = random.choice([p["id"] for p in room["players"] if p["id"] not in room.get("left", [])])
    room["round_end_at"] = time.time() + LG_WRITE_TIME
    room["answers"] = {}
    room["ready"] = []
    room["votes"] = {}
    room["answer_order"] = []
    room["status"] = "writing"
    room["round_result"] = None
    room["round_result_until"] = None


def lg_active_ids(room):
    return [p["id"] for p in room["players"] if p["id"] not in room.get("left", [])]


def lg_start_voting(room):
    active = lg_active_ids(room)
    for pid in active:
        room["answers"].setdefault(pid, "")
    order = active[:]
    random.shuffle(order)
    room["answer_order"] = order
    room["votes"] = {}
    room["vote_end_at"] = time.time() + LG_VOTE_TIME
    room["status"] = "voting"


def lg_finalize_round(room):
    liar = room["liar_id"]
    votes = room["votes"]
    correct = [voter for voter, target in votes.items() if target == liar]
    for voter in correct:
        room["scores"][voter] = room["scores"].get(voter, 0) + LG_CORRECT_POINTS
    if not correct:
        room["scores"][liar] = room["scores"].get(liar, 0) + LG_LIAR_ESCAPE_POINTS
    reveal = []
    for pid in room["answer_order"]:
        votes_for = [voter for voter, target in votes.items() if target == pid]
        reveal.append({
            "id": pid, "text": room["answers"].get(pid, ""),
            "is_liar": pid == liar, "votes_received": len(votes_for), "voters": votes_for,
        })
    room["round_result"] = {
        "question": room["question"], "liar_id": liar,
        "reveal": reveal, "correct_voters": correct,
    }
    room["round_result_until"] = time.time() + LG_RESULT_PAUSE
    room["status"] = "round_result"


def lg_mark_left(room, uid):
    left = room.setdefault("left", [])
    if uid in left:
        return
    left.append(uid)
    if room["status"] in ("waiting", "finished"):
        return
    n = len(room["players"])
    if n <= 2:
        other = next((pp["id"] for pp in room["players"] if pp["id"] != uid), None)
        if other:
            room["status"] = "finished"
            room["winners"] = [other]
            room["left_player"] = uid
    else:
        room["left_player"] = uid


def lg_check_timeouts(room):
    if room["status"] in ("waiting", "finished"):
        return
    now = time.time()
    last_seen = room.get("last_seen", {})
    for p in room["players"]:
        pid = p["id"]
        if pid in room.get("left", []):
            continue
        ts = last_seen.get(pid)
        if ts is not None and now - ts > DISCONNECT_TIMEOUT:
            lg_mark_left(room, pid)
            break


def lg_resolve_room(room):
    lg_check_timeouts(room)
    if room["status"] == "finished":
        return
    now = time.time()
    if room["status"] == "writing":
        active = lg_active_ids(room)
        all_ready = bool(active) and all(pid in room.get("ready", []) for pid in active)
        timeout = room.get("round_end_at") and now >= room["round_end_at"]
        if all_ready or timeout:
            lg_start_voting(room)
    elif room["status"] == "voting":
        active = lg_active_ids(room)
        all_voted = bool(active) and all(pid in room["votes"] for pid in active)
        timeout = room.get("vote_end_at") and now >= room["vote_end_at"]
        if all_voted or timeout:
            lg_finalize_round(room)
    elif room["status"] == "round_result":
        if room.get("round_result_until") and now >= room["round_result_until"]:
            if room["round"] >= room["target_rounds"]:
                best = max(room["scores"].values()) if room["scores"] else 0
                room["winners"] = [uid for uid, s in room["scores"].items() if s == best]
                room["status"] = "finished"
            else:
                lg_start_round(room)


def lg_public_state(room, uid):
    players_info = [{"id": p["id"], "name": p["name"]} for p in room["players"]]
    status = room["status"]
    answer_options = None
    if status in ("voting", "round_result", "finished") and room.get("answer_order"):
        answer_options = [{"idx": i, "text": room["answers"].get(pid, ""), "mine": pid == uid}
                           for i, pid in enumerate(room["answer_order"])]
    return {
        "code": room["code"], "host": room["host"], "status": status,
        "players": players_info, "max_players": room.get("max_players"),
        "target_rounds": room.get("target_rounds"), "round": room.get("round"),
        "question_mode": room.get("question_mode", "normal"),
        "question": room.get("question"),
        "is_liar": (uid == room.get("liar_id")) if status in ("writing", "voting") else None,
        "round_end_at": room.get("round_end_at"),
        "answered_ids": list(room.get("answers", {}).keys()),
        "ready_ids": list(room.get("ready", [])),
        "vote_end_at": room.get("vote_end_at"),
        "answer_options": answer_options,
        "voted_ids": list(room.get("votes", {}).keys()),
        "my_vote_idx": (room.get("answer_order", []).index(room["votes"][uid])
                         if uid in room.get("votes", {}) and room["votes"][uid] in room.get("answer_order", []) else None),
        "scores": room.get("scores", {}),
        "round_result": room.get("round_result"),
        "round_result_until": room.get("round_result_until"),
        "winners": room.get("winners"),
        "chat": room.get("chat", [])[-30:],
        "left_players": room.get("left", []),
        "left_player": room.get("left_player"),
    }


@app.route("/api/lg/create", methods=["POST"])
def lg_create():
    data = request.json
    uid, name = str(data["user_id"]), data.get("name", "بازیکن")
    max_players = int(data.get("max_players", 4))
    if max_players not in (3, 4, 5, 6, 7, 8):
        max_players = 4
    target_rounds = int(data.get("target_rounds", 5))
    if target_rounds not in (3, 5, 8):
        target_rounds = 5
    question_mode = str(data.get("question_mode", "normal"))
    if question_mode not in LG_QUESTIONS_BY_MODE:
        question_mode = "normal"
    with lg_lock:
        code = lg_gen_code()
        lg_rooms[code] = lg_new_room(code, uid, name, max_players, target_rounds, question_mode)
    return jsonify({"code": code, "state": lg_public_state(lg_rooms[code], uid)})


@app.route("/api/lg/join", methods=["POST"])
def lg_join():
    data = request.json
    code, uid, name = data["code"].upper(), str(data["user_id"]), data.get("name", "بازیکن")
    with lg_lock:
        room = lg_rooms.get(code)
        if not room:
            return jsonify({"error": "room_not_found"}), 404
        if any(p["id"] == uid for p in room["players"]):
            return jsonify({"state": lg_public_state(room, uid)})
        if room["status"] != "waiting":
            return jsonify({"error": "already_started"}), 400
        if len(room["players"]) >= room.get("max_players", 4):
            return jsonify({"error": "room_full"}), 400
        room["players"].append({"id": uid, "name": name})
        room["scores"][uid] = 0
        touch_player(room, uid)
    return jsonify({"state": lg_public_state(room, uid)})


@app.route("/api/lg/state")
def lg_state():
    code, uid = request.args.get("code", "").upper(), str(request.args.get("user_id"))
    room = lg_rooms.get(code)
    if not room:
        return jsonify({"error": "room_not_found"}), 404
    with lg_lock:
        touch_player(room, uid)
        lg_resolve_room(room)
    return jsonify({"state": lg_public_state(room, uid)})


@app.route("/api/lg/leave", methods=["POST"])
def lg_leave():
    data = request.json
    code, uid = data["code"].upper(), str(data["user_id"])
    with lg_lock:
        room = lg_rooms.get(code)
        if not room:
            return jsonify({"ok": True})
        if room["status"] == "waiting":
            room["players"] = [p for p in room["players"] if p["id"] != uid]
            room["scores"].pop(uid, None)
            if room["players"] and room["host"] == uid:
                room["host"] = room["players"][0]["id"]
            if not room["players"]:
                lg_rooms.pop(code, None)
        else:
            lg_mark_left(room, uid)
    return jsonify({"ok": True})


@app.route("/api/lg/start", methods=["POST"])
def lg_start():
    data = request.json
    code, uid = data["code"].upper(), str(data["user_id"])
    with lg_lock:
        room = lg_rooms.get(code)
        if not room:
            return jsonify({"error": "room_not_found"}), 404
        if room["host"] != uid:
            return jsonify({"error": "not_host"}), 403
        if room["status"] != "waiting":
            return jsonify({"error": "invalid_state"}), 400
        if len(room["players"]) < 3:
            return jsonify({"error": "need_more_players"}), 400
        touch_player(room, uid)
        lg_start_round(room)
    return jsonify({"state": lg_public_state(room, uid)})


@app.route("/api/lg/answer", methods=["POST"])
def lg_answer():
    data = request.json
    code, uid = data["code"].upper(), str(data["user_id"])
    text = str(data.get("text", ""))[:200]
    ready = bool(data.get("ready", False))
    with lg_lock:
        room = lg_rooms.get(code)
        if not room or room["status"] != "writing":
            return jsonify({"error": "invalid_state"}), 400
        if uid not in lg_active_ids(room):
            return jsonify({"error": "not_in_room"}), 400
        touch_player(room, uid)
        room["answers"][uid] = text
        if ready and uid not in room.get("ready", []):
            room.setdefault("ready", []).append(uid)
        lg_resolve_room(room)
    return jsonify({"state": lg_public_state(room, uid)})


@app.route("/api/lg/vote", methods=["POST"])
def lg_vote():
    data = request.json
    code, uid = data["code"].upper(), str(data["user_id"])
    idx = data.get("target_idx")
    with lg_lock:
        room = lg_rooms.get(code)
        if not room or room["status"] != "voting":
            return jsonify({"error": "invalid_state"}), 400
        touch_player(room, uid)
        order = room.get("answer_order", [])
        if not isinstance(idx, int) or idx < 0 or idx >= len(order):
            return jsonify({"error": "invalid_target"}), 400
        target = order[idx]
        if target == uid:
            return jsonify({"error": "cannot_vote_self"}), 400
        room["votes"][uid] = target
        lg_resolve_room(room)
    return jsonify({"state": lg_public_state(room, uid)})


@app.route("/api/lg/chat", methods=["POST"])
def lg_chat():
    data = request.json
    code, uid, text = data["code"].upper(), str(data["user_id"]), data.get("text", "")[:300]
    reaction = bool(data.get("reaction", False))
    with lg_lock:
        room = lg_rooms.get(code)
        if not room:
            return jsonify({"error": "room_not_found"}), 404
        player = next((p for p in room["players"] if p["id"] == uid), None)
        name = player["name"] if player else "?"
        room.setdefault("chat", []).append({"name": name, "text": text, "reaction": reaction})
    return jsonify({"ok": True})


# ---------- جستجوی بازیکن «کی دروغ میگه؟» ----------
lg_mm_queues = {}
lg_mm_matched = {}


@app.route("/api/lg/matchmake", methods=["POST"])
def lg_matchmake():
    data = request.json
    uid, name = str(data["user_id"]), data.get("name", "بازیکن")
    n = int(data.get("max_players", 4))
    if n not in (3, 4, 5, 6, 7, 8):
        n = 4
    target_rounds = int(data.get("target_rounds", 5))
    if target_rounds not in (3, 5, 8):
        target_rounds = 5
    question_mode = str(data.get("question_mode", "normal"))
    if question_mode not in LG_QUESTIONS_BY_MODE:
        question_mode = "normal"
    now = time.time()
    with lg_lock:
        for k, v in list(lg_mm_matched.items()):
            if now - v["ts"] > MM_KEEP:
                del lg_mm_matched[k]
        if uid in lg_mm_matched:
            return jsonify({"status": "matched", "code": lg_mm_matched.pop(uid)["code"]})

        mm_key = (n, target_rounds, question_mode)
        for key, q in lg_mm_queues.items():
            q[:] = [p for p in q
                    if (p["id"] == uid and key == mm_key) or (p["id"] != uid and now - p["ts"] < MM_STALE)]
        q = lg_mm_queues.setdefault(mm_key, [])
        me = next((p for p in q if p["id"] == uid), None)
        if me:
            me["ts"], me["name"] = now, name
        else:
            q.append({"id": uid, "name": name, "ts": now})

        if len(q) < n:
            return jsonify({"status": "searching", "found": len(q)})

        group = q[:n]
        del q[:n]
        code = lg_gen_code()
        room = lg_new_room(code, group[0]["id"], group[0]["name"], n, target_rounds, question_mode)
        room["players"] += [{"id": p["id"], "name": p["name"]} for p in group[1:]]
        for p in group[1:]:
            room["scores"][p["id"]] = 0
        lg_rooms[code] = room
        for p in group:
            if p["id"] != uid:
                lg_mm_matched[p["id"]] = {"code": code, "ts": now}
        return jsonify({"status": "matched", "code": code})


@app.route("/api/lg/matchmake_cancel", methods=["POST"])
def lg_matchmake_cancel():
    uid = str(request.json["user_id"])
    with lg_lock:
        for q in lg_mm_queues.values():
            q[:] = [p for p in q if p["id"] != uid]
        lg_mm_matched.pop(uid, None)
    return jsonify({"ok": True})


# ---------- بازی بلوف ----------
bf_rooms = {}
bf_lock = threading.Lock()

BF_MIN_PLAYERS = 3
BF_MAX_PLAYERS = 6
BF_CHALLENGE_TIME = 10.0   # ثانیه؛ فرصت بقیه برای فریاد زدن «بلوف!»
BF_REVEAL_PAUSE = 3.0     # ثانیه؛ نمایش نتیجه‌ی بلوف قبل از نوبت بعدی


def bf_gen_code():
    while True:
        code = "".join(random.choices(string.ascii_uppercase + string.digits, k=5))
        if code not in bf_rooms:
            return code


def bf_new_room(code, uid, name, max_players):
    return {
        "code": code, "host": uid,
        "players": [{"id": uid, "name": name}],
        "status": "waiting", "max_players": max_players,
        "hands": {}, "turn": None,
        "required_rank_idx": None, "free_start": True, "round_starter": None,
        "pile": [], "last_play": None, "challenge_deadline": None,
        "reveal": None, "reveal_until": None, "pending_winner": None,
        "winner": None, "left": [], "left_player": None,
        "last_seen": {}, "chat": [],
    }


def bf_active_ids(room):
    return [p["id"] for p in room["players"] if p["id"] not in room.get("left", [])]


def bf_next_active(room, uid):
    ids = [p["id"] for p in room["players"]]
    n = len(ids)
    idx = ids.index(uid)
    for step in range(1, n + 1):
        cand = ids[(idx + step) % n]
        if cand not in room.get("left", []):
            return cand
    return uid


def bf_start_game(room):
    deck = new_deck()
    players = room["players"]
    hands = {p["id"]: [] for p in players}
    for i, c in enumerate(deck):
        hands[players[i % len(players)]["id"]].append(c)
    for pid in hands:
        hands[pid] = sort_hand(hands[pid])
    room["hands"] = hands
    room["turn"] = random.choice([p["id"] for p in players])
    room["required_rank_idx"] = None
    room["free_start"] = True
    room["round_starter"] = None
    room["pile"] = []
    room["last_play"] = None
    room["challenge_deadline"] = None
    room["reveal"] = None
    room["reveal_until"] = None
    room["pending_winner"] = None
    room["winner"] = None
    room["status"] = "playing"


def bf_mark_left(room, uid):
    left = room.setdefault("left", [])
    if uid in left:
        return
    left.append(uid)
    if room["status"] in ("waiting", "finished"):
        return
    active = bf_active_ids(room)
    if len(active) <= 1:
        room["status"] = "finished"
        room["winner"] = active[0] if active else None
        room["left_player"] = uid
        return
    room["left_player"] = uid
    if room["status"] == "playing" and room.get("turn") == uid:
        room["turn"] = bf_next_active(room, uid)
    # اگه کسی که رتبه‌ی این دور رو اعلام کرده بود ترک کنه، دیگه هیچ‌وقت نوبت بهش برنمی‌گرده؛
    # پس برای اینکه رتبه برای همیشه قفل نمونه، نوبتِ بعدی آزاد میشه
    if room.get("round_starter") == uid:
        room["free_start"] = True


def bf_check_timeouts(room):
    if room["status"] in ("waiting", "finished"):
        return
    now = time.time()
    last_seen = room.get("last_seen", {})
    for p in room["players"]:
        pid = p["id"]
        if pid in room.get("left", []):
            continue
        ts = last_seen.get(pid)
        if ts is not None and now - ts > DISCONNECT_TIMEOUT:
            bf_mark_left(room, pid)
            break


def bf_resolve_play(room, caller):
    """caller=None یعنی کسی توی مهلت شک نکرد و ادعا قبول شد؛
    caller=uid یعنی اون بازیکن فریاد زده «بلوف!»."""
    lp = room["last_play"]
    by, cards, claimed = lp["by"], lp["cards"], lp["claimed_rank"]

    winner = None  # کسی که تشخیصش درست بوده (چه بلوف نگفته و راست بوده، چه بلوف گفته و درست گرفته)
    if caller is None:
        taken_by, result = None, "accepted"
    else:
        honest = all(card_rank(c) == claimed for c in cards)
        if honest:
            # بلوف نبود؛ خودِ شک‌کننده جریمه میشه و کل تلمبار رو می‌بره، ولی چون طرفِ راست‌گو درست تشخیص داده بود
            # (با گفتن حقیقت)، آزادیِ انتخاب رتبه‌ی بعدی مال خودِ اونه
            taken_by, result = caller, "wrong_call"
            winner = by
        else:
            # بلوف بود و گیر افتاد؛ خودِ بلوف‌زن جریمه میشه و کل تلمبار میفته دستش، ولی چون شک‌کننده درست
            # تشخیص داده بود، آزادیِ انتخاب رتبه‌ی بعدی مال خودِ اونه
            taken_by, result = by, "bluff_caught"
            winner = caller
        room["hands"][taken_by].extend(room["pile"])
        room["hands"][taken_by] = sort_hand(room["hands"][taken_by])
        room["pile"] = []

    room["reveal"] = {
        "by": by, "claimed_rank": claimed, "count": len(cards),
        "caller": caller, "result": result,
        "cards": cards if caller is not None else None,
        "taken_by": taken_by,
    }
    room["reveal_until"] = time.time() + BF_REVEAL_PAUSE
    room["last_play"] = None
    room["challenge_deadline"] = None

    if winner is not None:
        # یکی بلوف گفت (چه درست چه غلط) → نوبت و آزادیِ انتخاب رتبه مستقیم میره پیش کسی که تشخیصش درست بود،
        # صرف‌نظر از اینکه تلمبار الان دست کیه
        room["turn"] = winner
        room["free_start"] = True
        room["round_starter"] = winner
    else:
        # کسی بلوف نگفت؛ نوبت عادی به نفر بعدی می‌رسه و رتبه‌ی لازم عوض نمیشه، مگر اینکه دور کامل بشه و
        # نوبت دوباره به همون کسی برسه که این رتبه رو اول اعلام کرده بود
        next_uid = bf_next_active(room, by)
        room["turn"] = next_uid
        if next_uid == room.get("round_starter"):
            room["free_start"] = True

    room["pending_winner"] = by if len(room["hands"][by]) == 0 else None
    room["status"] = "reveal"


def bf_resolve_room(room):
    bf_check_timeouts(room)
    if room["status"] == "finished":
        return
    now = time.time()
    if room["status"] == "challenge_window":
        if room.get("challenge_deadline") and now >= room["challenge_deadline"]:
            bf_resolve_play(room, caller=None)
    elif room["status"] == "reveal":
        if room.get("reveal_until") and now >= room["reveal_until"]:
            room["reveal"] = None
            room["reveal_until"] = None
            if room.get("pending_winner"):
                room["status"] = "finished"
                room["winner"] = room["pending_winner"]
                room["pending_winner"] = None
            else:
                room["status"] = "playing"


def bf_public_state(room, uid):
    players_info = [{
        "id": p["id"], "name": p["name"],
        "cards_left": len(room["hands"].get(p["id"], [])) if room["status"] != "waiting" else None,
    } for p in room["players"]]
    my_hand = sort_hand(room["hands"].get(uid, [])) if room["status"] != "waiting" else []
    last_play_public = None
    if room.get("last_play"):
        lp = room["last_play"]
        last_play_public = {"by": lp["by"], "claimed_rank": lp["claimed_rank"], "count": lp["count"]}
    return {
        "code": room["code"], "host": room["host"], "status": room["status"],
        "players": players_info, "max_players": room.get("max_players"),
        "hand": my_hand,
        "turn": room.get("turn"),
        "free_start": room.get("free_start", False),
        "required_rank": RANKS[room["required_rank_idx"]] if room.get("required_rank_idx") is not None else None,
        "pile_count": len(room.get("pile", [])),
        "last_play": last_play_public,
        "challenge_deadline": room.get("challenge_deadline"),
        "reveal": room.get("reveal"),
        "reveal_until": room.get("reveal_until"),
        "winner": room.get("winner"),
        "chat": room.get("chat", [])[-30:],
        "left_players": room.get("left", []),
        "left_player": room.get("left_player"),
    }


@app.route("/api/bf/create", methods=["POST"])
def bf_create():
    data = request.json
    uid, name = str(data["user_id"]), data.get("name", "بازیکن")
    max_players = int(data.get("max_players", 4))
    if max_players not in (3, 4, 5, 6):
        max_players = 4
    with bf_lock:
        code = bf_gen_code()
        bf_rooms[code] = bf_new_room(code, uid, name, max_players)
    return jsonify({"code": code, "state": bf_public_state(bf_rooms[code], uid)})


@app.route("/api/bf/join", methods=["POST"])
def bf_join():
    data = request.json
    code, uid, name = data["code"].upper(), str(data["user_id"]), data.get("name", "بازیکن")
    with bf_lock:
        room = bf_rooms.get(code)
        if not room:
            return jsonify({"error": "room_not_found"}), 404
        if any(p["id"] == uid for p in room["players"]):
            return jsonify({"state": bf_public_state(room, uid)})
        if room["status"] != "waiting":
            return jsonify({"error": "already_started"}), 400
        if len(room["players"]) >= room.get("max_players", 4):
            return jsonify({"error": "room_full"}), 400
        room["players"].append({"id": uid, "name": name})
        touch_player(room, uid)
    return jsonify({"state": bf_public_state(room, uid)})


@app.route("/api/bf/state")
def bf_state():
    code, uid = request.args.get("code", "").upper(), str(request.args.get("user_id"))
    room = bf_rooms.get(code)
    if not room:
        return jsonify({"error": "room_not_found"}), 404
    with bf_lock:
        touch_player(room, uid)
        bf_resolve_room(room)
    return jsonify({"state": bf_public_state(room, uid)})


@app.route("/api/bf/leave", methods=["POST"])
def bf_leave():
    data = request.json
    code, uid = data["code"].upper(), str(data["user_id"])
    with bf_lock:
        room = bf_rooms.get(code)
        if not room:
            return jsonify({"ok": True})
        if room["status"] == "waiting":
            room["players"] = [p for p in room["players"] if p["id"] != uid]
            if room["players"] and room["host"] == uid:
                room["host"] = room["players"][0]["id"]
            if not room["players"]:
                bf_rooms.pop(code, None)
        else:
            bf_mark_left(room, uid)
    return jsonify({"ok": True})


@app.route("/api/bf/start", methods=["POST"])
def bf_start():
    data = request.json
    code, uid = data["code"].upper(), str(data["user_id"])
    with bf_lock:
        room = bf_rooms.get(code)
        if not room:
            return jsonify({"error": "room_not_found"}), 404
        if room["host"] != uid:
            return jsonify({"error": "not_host"}), 403
        if room["status"] != "waiting":
            return jsonify({"error": "invalid_state"}), 400
        if len(room["players"]) < BF_MIN_PLAYERS:
            return jsonify({"error": "need_more_players"}), 400
        touch_player(room, uid)
        bf_start_game(room)
    return jsonify({"state": bf_public_state(room, uid)})


@app.route("/api/bf/play", methods=["POST"])
def bf_play():
    data = request.json
    code, uid = data["code"].upper(), str(data["user_id"])
    cards = data.get("cards") or []
    claimed_rank = data.get("claimed_rank")
    with bf_lock:
        room = bf_rooms.get(code)
        if not room or room["status"] != "playing":
            return jsonify({"error": "invalid_state"}), 400
        if room.get("turn") != uid:
            return jsonify({"error": "not_your_turn"}), 400
        if not isinstance(cards, list) or not (1 <= len(cards) <= 4):
            return jsonify({"error": "invalid_count"}), 400
        hand = list(room["hands"].get(uid, []))
        remaining = hand[:]
        for c in cards:
            if c not in remaining:
                return jsonify({"error": "invalid_cards"}), 400
            remaining.remove(c)
        touch_player(room, uid)

        if room.get("free_start"):
            if claimed_rank not in RANKS:
                return jsonify({"error": "invalid_rank"}), 400
            room["required_rank_idx"] = RANKS.index(claimed_rank)
            room["free_start"] = False
            room["round_starter"] = uid
        rank = RANKS[room["required_rank_idx"]]

        room["hands"][uid] = remaining
        room["last_play"] = {"by": uid, "cards": list(cards), "claimed_rank": rank, "count": len(cards)}
        room["pile"].extend(cards)
        room["challenge_deadline"] = time.time() + BF_CHALLENGE_TIME
        room["status"] = "challenge_window"
    return jsonify({"state": bf_public_state(room, uid)})


@app.route("/api/bf/call", methods=["POST"])
def bf_call():
    data = request.json
    code, uid = data["code"].upper(), str(data["user_id"])
    with bf_lock:
        room = bf_rooms.get(code)
        if not room or room["status"] != "challenge_window":
            return jsonify({"error": "invalid_state"}), 400
        lp = room.get("last_play")
        if not lp or lp["by"] == uid:
            return jsonify({"error": "cannot_call"}), 400
        if uid not in bf_active_ids(room):
            return jsonify({"error": "not_in_room"}), 400
        touch_player(room, uid)
        bf_resolve_play(room, caller=uid)
    return jsonify({"state": bf_public_state(room, uid)})


@app.route("/api/bf/chat", methods=["POST"])
def bf_chat():
    data = request.json
    code, uid, text = data["code"].upper(), str(data["user_id"]), data.get("text", "")[:300]
    reaction = bool(data.get("reaction", False))
    with bf_lock:
        room = bf_rooms.get(code)
        if not room:
            return jsonify({"error": "room_not_found"}), 404
        player = next((p for p in room["players"] if p["id"] == uid), None)
        name = player["name"] if player else "?"
        room.setdefault("chat", []).append({"name": name, "text": text, "reaction": reaction})
    return jsonify({"ok": True})


# ---------- جستجوی بازیکن بلوف ----------
bf_mm_queues = {}
bf_mm_matched = {}


@app.route("/api/bf/matchmake", methods=["POST"])
def bf_matchmake():
    data = request.json
    uid, name = str(data["user_id"]), data.get("name", "بازیکن")
    n = int(data.get("max_players", 4))
    if n not in (3, 4, 5, 6):
        n = 4
    now = time.time()
    with bf_lock:
        for k, v in list(bf_mm_matched.items()):
            if now - v["ts"] > MM_KEEP:
                del bf_mm_matched[k]
        if uid in bf_mm_matched:
            return jsonify({"status": "matched", "code": bf_mm_matched.pop(uid)["code"]})

        q = bf_mm_queues.setdefault(n, [])
        q[:] = [p for p in q if (p["id"] == uid) or (now - p["ts"] < MM_STALE)]
        me = next((p for p in q if p["id"] == uid), None)
        if me:
            me["ts"], me["name"] = now, name
        else:
            q.append({"id": uid, "name": name, "ts": now})

        if len(q) < n:
            return jsonify({"status": "searching", "found": len(q)})

        group = q[:n]
        del q[:n]
        code = bf_gen_code()
        room = bf_new_room(code, group[0]["id"], group[0]["name"], n)
        room["players"] += [{"id": p["id"], "name": p["name"]} for p in group[1:]]
        bf_rooms[code] = room
        for p in group:
            if p["id"] != uid:
                bf_mm_matched[p["id"]] = {"code": code, "ts": now}
        return jsonify({"status": "matched", "code": code})


@app.route("/api/bf/matchmake_cancel", methods=["POST"])
def bf_matchmake_cancel():
    uid = str(request.json["user_id"])
    with bf_lock:
        for q in bf_mm_queues.values():
            q[:] = [p for p in q if p["id"] != uid]
        bf_mm_matched.pop(uid, None)
    return jsonify({"ok": True})


# ---------- بازی منچ ----------
mc_rooms = {}
mc_lock = threading.Lock()

MC_COLORS = ["red", "blue", "yellow", "green"]
MC_START_OFFSET = {"red": 0, "blue": 13, "yellow": 26, "green": 39}
MC_START_CELLS = {0, 13, 26, 39}  # خونه‌ی اول (ورودی) هر رنگ؛ فقط خودِ اون رنگ حق وایسادن روش رو داره
MC_SAFE_CELLS = {0, 13, 26, 39, 8, 21, 34, 47}
MC_BOT_DELAY = (0.6, 1.2)  # ثانیه؛ مکث مصنوعی قبل از حرکت ربات


def mc_gen_code():
    while True:
        code = "".join(random.choices(string.ascii_uppercase + string.digits, k=5))
        if code not in mc_rooms:
            return code


def mc_new_room(code, uid, name, max_players, num_pieces):
    return {
        "code": code, "host": uid,
        "players": [{"id": uid, "name": name}],
        "status": "waiting", "max_players": max_players, "num_pieces": num_pieces,
        "bot_difficulty": {}, "pieces": {}, "turn": None, "dice": None,
        "bot_action_at": None, "winner": None,
        "left": [], "left_player": None,
        "last_seen": {}, "chat": [],
        "last_dice": {}, "last_roller": None,
    }


def mc_active_ids(room):
    return [p["id"] for p in room["players"] if p["id"] not in room.get("left", [])]


def mc_next_active(room, uid):
    ids = [p["id"] for p in room["players"]]
    n = len(ids)
    idx = ids.index(uid)
    for step in range(1, n + 1):
        cand = ids[(idx + step) % n]
        if cand not in room.get("left", []):
            return cand
    return uid


def mc_color_of(room, uid):
    idx = next(i for i, p in enumerate(room["players"]) if p["id"] == uid)
    return MC_COLORS[idx]


def mc_is_bot(uid):
    return uid.startswith("bot_")


def mc_start_game(room):
    m = room["num_pieces"]
    room["pieces"] = {p["id"]: [-1] * m for p in room["players"]}
    room["turn"] = random.choice([p["id"] for p in room["players"]])
    room["dice"] = None
    room["bot_action_at"] = None
    room["winner"] = None
    room["status"] = "playing"
    room["last_dice"] = {}
    room["last_roller"] = None


def mc_mark_left(room, uid):
    left = room.setdefault("left", [])
    if uid in left:
        return
    left.append(uid)
    if room["status"] in ("waiting", "finished"):
        return
    active = [i for i in mc_active_ids(room) if not mc_is_bot(i)]
    # اگه دیگه هیچ آدمی توی اتاق نمونده، بازی رو (بدون برنده) تموم کن
    if not active:
        room["status"] = "finished"
        room["winner"] = None
        room["left_player"] = uid
        return
    room["left_player"] = uid
    # دونفره (یا وقتی فقط یه بازیکن باقی مونده): با رفتن حریف، بازی همون لحظه به نفع نفر باقی‌مونده تموم میشه
    remaining = mc_active_ids(room)
    if room["status"] == "playing" and len(remaining) == 1:
        room["status"] = "finished"
        room["winner"] = remaining[0]
        room["dice"] = None
        room["bot_action_at"] = None
        return
    if room["status"] == "playing" and room.get("turn") == uid:
        room["turn"] = mc_next_active(room, uid)
        room["dice"] = None
        room["bot_action_at"] = None


def mc_check_timeouts(room):
    if room["status"] in ("waiting", "finished"):
        return
    now = time.time()
    last_seen = room.get("last_seen", {})
    for p in room["players"]:
        pid = p["id"]
        if mc_is_bot(pid) or pid in room.get("left", []):
            continue
        ts = last_seen.get(pid)
        if ts is not None and now - ts > DISCONNECT_TIMEOUT:
            mc_mark_left(room, pid)
            break


def mc_legal_moves(room, uid, dice):
    legal = []
    pieces = room["pieces"][uid]
    offset = MC_START_OFFSET[mc_color_of(room, uid)]
    own_start_occupied = any(p == 0 for p in pieces)
    for i, p in enumerate(pieces):
        if p == -1:
            # تا وقتی یه مهره‌ی خودت رو خونه‌ی اول (خونه‌ی خروج) وایساده، نمی‌تونی مهره‌ی جدید بیاری بیرون
            if dice == 6 and not own_start_occupied:
                legal.append(i)
        elif p == 57:
            continue
        elif p + dice <= 57:
            new_p = p + dice
            if 0 <= new_p <= 50:
                abs_cell = (offset + new_p) % 52
                # نمی‌تونی رو خونه‌ی اول (ورودی) یه رنگ دیگه بشینی؛ فقط خونه‌ی اول خودت مجازه
                if abs_cell in MC_START_CELLS and abs_cell != offset:
                    continue
            legal.append(i)
    return legal


def mc_apply_move(room, uid, piece_idx, dice):
    pieces = room["pieces"][uid]
    p = pieces[piece_idx]
    offset = MC_START_OFFSET[mc_color_of(room, uid)]
    new_p = 0 if p == -1 else p + dice
    pieces[piece_idx] = new_p
    captured = False
    finished = new_p == 57
    if 0 <= new_p <= 50:
        abs_cell = (offset + new_p) % 52
        if abs_cell not in MC_SAFE_CELLS:
            for other in room["players"]:
                oid = other["id"]
                if oid == uid or oid in room.get("left", []):
                    continue
                ooffset = MC_START_OFFSET[mc_color_of(room, oid)]
                opieces = room["pieces"][oid]
                for j, op in enumerate(opieces):
                    if 0 <= op <= 50 and (ooffset + op) % 52 == abs_cell:
                        opieces[j] = -1
                        captured = True
    return captured, finished


def mc_check_win(room, uid):
    if all(p == 57 for p in room["pieces"][uid]):
        room["status"] = "finished"
        room["winner"] = uid
        return True
    return False


def mc_advance_turn(room, uid, extra):
    room["dice"] = None
    room["bot_action_at"] = None
    room["turn"] = uid if extra else mc_next_active(room, uid)


def mc_score_move(room, uid, idx, dice):
    p = room["pieces"][uid][idx]
    offset = MC_START_OFFSET[mc_color_of(room, uid)]
    new_p = 0 if p == -1 else p + dice
    score = 0.0
    if new_p == 57:
        score += 100
    if p == -1:
        score += 15
    if 0 <= new_p <= 50:
        abs_cell = (offset + new_p) % 52
        if abs_cell not in MC_SAFE_CELLS:
            for other in room["players"]:
                oid = other["id"]
                if oid == uid or oid in room.get("left", []):
                    continue
                ooffset = MC_START_OFFSET[mc_color_of(room, oid)]
                for op in room["pieces"][oid]:
                    if 0 <= op <= 50 and (ooffset + op) % 52 == abs_cell:
                        score += 40
        else:
            score += 5
    score += new_p * 0.3
    return score


def mc_bot_choose(room, uid, legal, dice, difficulty):
    if difficulty == "easy" or len(legal) == 1:
        return random.choice(legal)
    scored = sorted(((mc_score_move(room, uid, i, dice), i) for i in legal), reverse=True)
    threshold = 0.9 if difficulty == "hard" else 0.55
    return scored[0][1] if random.random() < threshold else random.choice(legal)


def mc_bot_take_turn(room, uid):
    difficulty = room.get("bot_difficulty", {}).get(uid, "normal")
    dice = random.randint(1, 6)
    room["dice"] = dice
    room.setdefault("last_dice", {})[uid] = dice
    room["last_roller"] = uid
    legal = mc_legal_moves(room, uid, dice)
    if not legal:
        mc_advance_turn(room, uid, extra=False)
        return
    idx = mc_bot_choose(room, uid, legal, dice, difficulty)
    captured, finished = mc_apply_move(room, uid, idx, dice)
    if mc_check_win(room, uid):
        return
    mc_advance_turn(room, uid, extra=(dice == 6 or captured or finished))


def mc_resolve_room(room):
    mc_check_timeouts(room)
    if room["status"] != "playing":
        return
    turn_uid = room.get("turn")
    if turn_uid and mc_is_bot(turn_uid):
        now = time.time()
        if room.get("bot_action_at") is None:
            room["bot_action_at"] = now + random.uniform(*MC_BOT_DELAY)
        elif now >= room["bot_action_at"]:
            mc_bot_take_turn(room, turn_uid)


def mc_public_state(room, uid):
    players_info = [{
        "id": p["id"], "name": p["name"], "color": mc_color_of(room, p["id"]),
        "is_bot": mc_is_bot(p["id"]),
    } for p in room["players"]]
    my_turn = room.get("turn") == uid
    return {
        "code": room["code"], "host": room["host"], "status": room["status"],
        "players": players_info, "max_players": room.get("max_players"),
        "num_pieces": room.get("num_pieces"),
        "pieces": room.get("pieces", {}),
        "turn": room.get("turn"),
        "dice": room.get("dice"),
        "last_dice": room.get("last_dice", {}),
        "last_roller": room.get("last_roller"),
        "legal_moves": mc_legal_moves(room, uid, room["dice"]) if (my_turn and room.get("dice") is not None) else [],
        "winner": room.get("winner"),
        "chat": room.get("chat", [])[-30:],
        "left_players": room.get("left", []),
        "left_player": room.get("left_player"),
    }


@app.route("/api/mc/create", methods=["POST"])
def mc_create():
    data = request.json
    uid, name = str(data["user_id"]), data.get("name", "بازیکن")
    max_players = int(data.get("max_players", 4))
    if max_players not in (2, 3, 4):
        max_players = 4
    num_pieces = int(data.get("num_pieces", 4))
    if num_pieces not in (2, 3, 4):
        num_pieces = 4
    with mc_lock:
        code = mc_gen_code()
        mc_rooms[code] = mc_new_room(code, uid, name, max_players, num_pieces)
        touch_player(mc_rooms[code], uid)
    return jsonify({"state": mc_public_state(mc_rooms[code], uid)})


@app.route("/api/mc/join", methods=["POST"])
def mc_join():
    data = request.json
    code, uid, name = data["code"].upper(), str(data["user_id"]), data.get("name", "بازیکن")
    with mc_lock:
        room = mc_rooms.get(code)
        if not room:
            return jsonify({"error": "room_not_found"}), 404
        if any(p["id"] == uid for p in room["players"]):
            return jsonify({"state": mc_public_state(room, uid)})
        if room["status"] != "waiting":
            return jsonify({"error": "already_started"}), 400
        if len(room["players"]) >= room.get("max_players", 4):
            return jsonify({"error": "room_full"}), 400
        room["players"].append({"id": uid, "name": name})
        touch_player(room, uid)
    return jsonify({"state": mc_public_state(room, uid)})


@app.route("/api/mc/state")
def mc_state():
    code, uid = request.args.get("code", "").upper(), str(request.args.get("user_id"))
    room = mc_rooms.get(code)
    if not room:
        return jsonify({"error": "room_not_found"}), 404
    with mc_lock:
        touch_player(room, uid)
        mc_resolve_room(room)
    return jsonify({"state": mc_public_state(room, uid)})


@app.route("/api/mc/leave", methods=["POST"])
def mc_leave():
    data = request.json
    code, uid = data["code"].upper(), str(data["user_id"])
    with mc_lock:
        room = mc_rooms.get(code)
        if not room:
            return jsonify({"ok": True})
        if room["status"] == "waiting":
            room["players"] = [p for p in room["players"] if p["id"] != uid]
            if room["players"] and room["host"] == uid:
                room["host"] = next((p["id"] for p in room["players"] if not mc_is_bot(p["id"])), room["players"][0]["id"])
            if not any(not mc_is_bot(p["id"]) for p in room["players"]):
                mc_rooms.pop(code, None)
        else:
            mc_mark_left(room, uid)
    return jsonify({"ok": True})


@app.route("/api/mc/add_bot", methods=["POST"])
def mc_add_bot():
    data = request.json
    code, uid = data["code"].upper(), str(data["user_id"])
    difficulty = str(data.get("difficulty", "normal"))
    if difficulty not in ("easy", "normal", "hard"):
        difficulty = "normal"
    with mc_lock:
        room = mc_rooms.get(code)
        if not room:
            return jsonify({"error": "room_not_found"}), 404
        if room["host"] != uid:
            return jsonify({"error": "not_host"}), 403
        if room["status"] != "waiting":
            return jsonify({"error": "invalid_state"}), 400
        if len(room["players"]) >= room.get("max_players", 4):
            return jsonify({"error": "room_full"}), 400
        bot_id = "bot_" + "".join(random.choices(string.ascii_lowercase + string.digits, k=6))
        bot_name = {"easy": "ربات (آسون)", "normal": "ربات (معمولی)", "hard": "ربات (سخت)"}[difficulty]
        room["players"].append({"id": bot_id, "name": bot_name})
        room.setdefault("bot_difficulty", {})[bot_id] = difficulty
    return jsonify({"state": mc_public_state(room, uid)})


@app.route("/api/mc/remove_bot", methods=["POST"])
def mc_remove_bot():
    data = request.json
    code, uid, bot_id = data["code"].upper(), str(data["user_id"]), str(data.get("bot_id"))
    with mc_lock:
        room = mc_rooms.get(code)
        if not room:
            return jsonify({"error": "room_not_found"}), 404
        if room["host"] != uid:
            return jsonify({"error": "not_host"}), 403
        if room["status"] != "waiting":
            return jsonify({"error": "invalid_state"}), 400
        room["players"] = [p for p in room["players"] if p["id"] != bot_id]
        room.get("bot_difficulty", {}).pop(bot_id, None)
    return jsonify({"state": mc_public_state(room, uid)})


@app.route("/api/mc/start", methods=["POST"])
def mc_start():
    data = request.json
    code, uid = data["code"].upper(), str(data["user_id"])
    with mc_lock:
        room = mc_rooms.get(code)
        if not room:
            return jsonify({"error": "room_not_found"}), 404
        if room["host"] != uid:
            return jsonify({"error": "not_host"}), 403
        if room["status"] != "waiting":
            return jsonify({"error": "invalid_state"}), 400
        if len(room["players"]) < 2:
            return jsonify({"error": "need_more_players"}), 400
        touch_player(room, uid)
        mc_start_game(room)
    return jsonify({"state": mc_public_state(room, uid)})


@app.route("/api/mc/roll", methods=["POST"])
def mc_roll():
    data = request.json
    code, uid = data["code"].upper(), str(data["user_id"])
    with mc_lock:
        room = mc_rooms.get(code)
        if not room:
            return jsonify({"error": "room_not_found"}), 404
        mc_resolve_room(room)
        if room["status"] != "playing":
            return jsonify({"state": mc_public_state(room, uid)})
        if room.get("turn") != uid:
            return jsonify({"error": "not_your_turn"}), 400
        if room.get("dice") is not None:
            return jsonify({"error": "already_rolled"}), 400
        touch_player(room, uid)
        dice = random.randint(1, 6)
        room["dice"] = dice
        room.setdefault("last_dice", {})[uid] = dice
        room["last_roller"] = uid
        legal = mc_legal_moves(room, uid, dice)
        if not legal:
            mc_advance_turn(room, uid, extra=False)
    return jsonify({"state": mc_public_state(room, uid)})


@app.route("/api/mc/move", methods=["POST"])
def mc_move():
    data = request.json
    code, uid = data["code"].upper(), str(data["user_id"])
    piece_idx = int(data.get("piece_index", -1))
    with mc_lock:
        room = mc_rooms.get(code)
        if not room or room["status"] != "playing":
            return jsonify({"error": "invalid_state"}), 400
        if room.get("turn") != uid:
            return jsonify({"error": "not_your_turn"}), 400
        dice = room.get("dice")
        if dice is None:
            return jsonify({"error": "not_rolled"}), 400
        legal = mc_legal_moves(room, uid, dice)
        if piece_idx not in legal:
            return jsonify({"error": "invalid_move"}), 400
        touch_player(room, uid)
        captured, finished = mc_apply_move(room, uid, piece_idx, dice)
        if not mc_check_win(room, uid):
            mc_advance_turn(room, uid, extra=(dice == 6 or captured or finished))
    return jsonify({"state": mc_public_state(room, uid)})


@app.route("/api/mc/chat", methods=["POST"])
def mc_chat():
    data = request.json
    code, uid, text = data["code"].upper(), str(data["user_id"]), data.get("text", "")[:300]
    reaction = bool(data.get("reaction", False))
    with mc_lock:
        room = mc_rooms.get(code)
        if not room:
            return jsonify({"error": "room_not_found"}), 404
        player = next((p for p in room["players"] if p["id"] == uid), None)
        name = player["name"] if player else "?"
        room.setdefault("chat", []).append({"name": name, "text": text, "reaction": reaction})
    return jsonify({"ok": True})


# ---------- جستجوی بازیکن منچ ----------
mc_mm_queues = {}
mc_mm_matched = {}


@app.route("/api/mc/matchmake", methods=["POST"])
def mc_matchmake():
    data = request.json
    uid, name = str(data["user_id"]), data.get("name", "بازیکن")
    n = int(data.get("max_players", 4))
    if n not in (2, 3, 4):
        n = 4
    num_pieces = int(data.get("num_pieces", 4))
    if num_pieces not in (2, 3, 4):
        num_pieces = 4
    now = time.time()
    with mc_lock:
        for k, v in list(mc_mm_matched.items()):
            if now - v["ts"] > MM_KEEP:
                del mc_mm_matched[k]
        if uid in mc_mm_matched:
            return jsonify({"status": "matched", "code": mc_mm_matched.pop(uid)["code"]})

        q = mc_mm_queues.setdefault(n, [])
        q[:] = [p for p in q if (p["id"] == uid) or (now - p["ts"] < MM_STALE)]
        me = next((p for p in q if p["id"] == uid), None)
        if me:
            me["ts"], me["name"] = now, name
        else:
            q.append({"id": uid, "name": name, "ts": now})

        if len(q) < n:
            return jsonify({"status": "searching", "found": len(q)})

        group = q[:n]
        del q[:n]
        code = mc_gen_code()
        room = mc_new_room(code, group[0]["id"], group[0]["name"], n, num_pieces)
        room["players"] += [{"id": p["id"], "name": p["name"]} for p in group[1:]]
        mc_rooms[code] = room
        for p in group:
            if p["id"] != uid:
                mc_mm_matched[p["id"]] = {"code": code, "ts": now}
        return jsonify({"status": "matched", "code": code})


@app.route("/api/mc/matchmake_cancel", methods=["POST"])
def mc_matchmake_cancel():
    uid = str(request.json["user_id"])
    with mc_lock:
        for q in mc_mm_queues.values():
            q[:] = [p for p in q if p["id"] != uid]
        mc_mm_matched.pop(uid, None)
    return jsonify({"ok": True})


# ---------- بازی محاصره (دو نفره، حرکت روی شبکه + گذاشتن دیوار) ----------
qr_rooms = {}
qr_lock = threading.Lock()
QR_N = 9
QR_WALLS = 10


def qr_gen_code():
    while True:
        code = "".join(random.choices(string.ascii_uppercase + string.digits, k=5))
        if code not in qr_rooms:
            return code


def qr_new_room(code, uid, name):
    N = QR_N
    return {
        "code": code, "host": uid,
        "players": [{"id": uid, "name": name}],
        "status": "waiting",
        "pos": {}, "goal_row": {}, "walls_left": {},
        "h_walls": [[False] * (N - 1) for _ in range(N - 1)],
        "v_walls": [[False] * (N - 1) for _ in range(N - 1)],
        "turn": None, "winner": None,
        "chat": [], "left": [], "left_player": None,
    }


def qr_other(room, uid):
    return next(p["id"] for p in room["players"] if p["id"] != uid)


def qr_blocked(hw, vw, r1, c1, r2, c2):
    N = QR_N
    if r1 == r2:
        row, j = r1, min(c1, c2)
        for i in (row - 1, row):
            if 0 <= i <= N - 2 and vw[i][j]:
                return True
        return False
    if c1 == c2:
        col, i = c1, min(r1, r2)
        for j in (col - 1, col):
            if 0 <= j <= N - 2 and hw[i][j]:
                return True
        return False
    return True


def qr_edge_blocked(room, r1, c1, r2, c2):
    return qr_blocked(room["h_walls"], room["v_walls"], r1, c1, r2, c2)


def qr_legal_moves(room, uid):
    N = QR_N
    r, c = room["pos"][uid]
    other = qr_other(room, uid)
    orow, ocol = room["pos"][other]
    moves = set()
    for dr, dc in ((-1, 0), (1, 0), (0, -1), (0, 1)):
        nr, nc = r + dr, c + dc
        if not (0 <= nr < N and 0 <= nc < N):
            continue
        if qr_edge_blocked(room, r, c, nr, nc):
            continue
        if (nr, nc) == (orow, ocol):
            jr, jc = nr + dr, nc + dc
            if 0 <= jr < N and 0 <= jc < N and not qr_edge_blocked(room, nr, nc, jr, jc):
                moves.add((jr, jc))
            else:
                if dr != 0:
                    for ddc in (-1, 1):
                        sr, sc = nr, nc + ddc
                        if 0 <= sr < N and 0 <= sc < N and not qr_edge_blocked(room, nr, nc, sr, sc):
                            moves.add((sr, sc))
                else:
                    for ddr in (-1, 1):
                        sr, sc = nr + ddr, nc
                        if 0 <= sr < N and 0 <= sc < N and not qr_edge_blocked(room, nr, nc, sr, sc):
                            moves.add((sr, sc))
        else:
            moves.add((nr, nc))
    return moves


def qr_reachable(hw, vw, start, goal_row):
    from collections import deque
    N = QR_N
    seen = {tuple(start)}
    q = deque([tuple(start)])
    while q:
        r, c = q.popleft()
        if r == goal_row:
            return True
        for dr, dc in ((-1, 0), (1, 0), (0, -1), (0, 1)):
            nr, nc = r + dr, c + dc
            if 0 <= nr < N and 0 <= nc < N and (nr, nc) not in seen and not qr_blocked(hw, vw, r, c, nr, nc):
                seen.add((nr, nc))
                q.append((nr, nc))
    return False


def qr_wall_free(room, wtype, i, j):
    N = QR_N
    if not (0 <= i <= N - 2 and 0 <= j <= N - 2):
        return False
    if wtype == "h":
        if room["h_walls"][i][j]:
            return False
        if j > 0 and room["h_walls"][i][j - 1]:
            return False
        if j < N - 2 and room["h_walls"][i][j + 1]:
            return False
        if room["v_walls"][i][j]:
            return False
    else:
        if room["v_walls"][i][j]:
            return False
        if i > 0 and room["v_walls"][i - 1][j]:
            return False
        if i < N - 2 and room["v_walls"][i + 1][j]:
            return False
        if room["h_walls"][i][j]:
            return False
    return True


def qr_try_place_wall(room, uid, wtype, i, j):
    if room["walls_left"].get(uid, 0) <= 0:
        return False, "no_walls_left"
    if not qr_wall_free(room, wtype, i, j):
        return False, "invalid_wall"
    hw = [row[:] for row in room["h_walls"]]
    vw = [row[:] for row in room["v_walls"]]
    if wtype == "h":
        hw[i][j] = True
    else:
        vw[i][j] = True
    for p in room["players"]:
        pid = p["id"]
        if not qr_reachable(hw, vw, room["pos"][pid], room["goal_row"][pid]):
            return False, "blocks_path"
    room["h_walls"] = hw
    room["v_walls"] = vw
    room["walls_left"][uid] -= 1
    return True, None


def qr_check_win(room, uid):
    r, c = room["pos"][uid]
    if r == room["goal_row"][uid]:
        room["status"] = "finished"
        room["winner"] = uid
        return True
    return False


def qr_public_state(room, uid):
    players_info = [{"id": p["id"], "name": p["name"], "seat": i} for i, p in enumerate(room["players"])]
    my_turn = room.get("turn") == uid and room["status"] == "playing"
    return {
        "code": room["code"], "host": room["host"], "status": room["status"],
        "players": players_info,
        "N": QR_N,
        "pos": room.get("pos", {}),
        "goal_row": room.get("goal_row", {}),
        "walls_left": room.get("walls_left", {}),
        "h_walls": room["h_walls"], "v_walls": room["v_walls"],
        "turn": room.get("turn"),
        "legal_moves": sorted(list(qr_legal_moves(room, uid))) if my_turn else [],
        "winner": room.get("winner"),
        "chat": room.get("chat", [])[-30:],
        "left_players": room.get("left", []),
        "left_player": room.get("left_player"),
    }


def qr_mark_left(room, uid):
    left = room.setdefault("left", [])
    if uid in left:
        return
    left.append(uid)
    if room["status"] in ("waiting", "finished"):
        return
    other = next((p["id"] for p in room["players"] if p["id"] != uid), None)
    if other:
        room["status"] = "finished"
        room["winner"] = other
        room["left_player"] = uid


def qr_check_timeouts(room):
    if room["status"] in ("waiting", "finished"):
        return
    now = time.time()
    last_seen = room.get("last_seen", {})
    for p in room["players"]:
        pid = p["id"]
        if pid in room.get("left", []):
            continue
        ts = last_seen.get(pid)
        if ts is not None and now - ts > DISCONNECT_TIMEOUT:
            qr_mark_left(room, pid)
            break


def qr_resolve_room(room):
    qr_check_timeouts(room)


@app.route("/api/qr/create", methods=["POST"])
def qr_create():
    data = request.json
    uid, name = str(data["user_id"]), data.get("name", "بازیکن")
    with qr_lock:
        code = qr_gen_code()
        qr_rooms[code] = qr_new_room(code, uid, name)
        touch_player(qr_rooms[code], uid)
    return jsonify({"code": code, "state": qr_public_state(qr_rooms[code], uid)})


@app.route("/api/qr/join", methods=["POST"])
def qr_join():
    data = request.json
    code, uid, name = data["code"].upper(), str(data["user_id"]), data.get("name", "بازیکن")
    with qr_lock:
        room = qr_rooms.get(code)
        if not room:
            return jsonify({"error": "room_not_found"}), 404
        if any(p["id"] == uid for p in room["players"]):
            return jsonify({"state": qr_public_state(room, uid)})
        if room["status"] != "waiting":
            return jsonify({"error": "already_started"}), 400
        if len(room["players"]) >= 2:
            return jsonify({"error": "room_full"}), 400
        room["players"].append({"id": uid, "name": name})
        touch_player(room, uid)
    return jsonify({"state": qr_public_state(room, uid)})


@app.route("/api/qr/state")
def qr_state():
    code, uid = request.args.get("code", "").upper(), str(request.args.get("user_id"))
    room = qr_rooms.get(code)
    if not room:
        return jsonify({"error": "room_not_found"}), 404
    with qr_lock:
        touch_player(room, uid)
        qr_resolve_room(room)
    return jsonify({"state": qr_public_state(room, uid)})


@app.route("/api/qr/leave", methods=["POST"])
def qr_leave():
    data = request.json
    code, uid = data["code"].upper(), str(data["user_id"])
    with qr_lock:
        room = qr_rooms.get(code)
        if not room:
            return jsonify({"ok": True})
        if room["status"] == "waiting":
            room["players"] = [p for p in room["players"] if p["id"] != uid]
            if room["players"] and room["host"] == uid:
                room["host"] = room["players"][0]["id"]
            if not room["players"]:
                qr_rooms.pop(code, None)
        else:
            qr_mark_left(room, uid)
    return jsonify({"ok": True})


@app.route("/api/qr/start", methods=["POST"])
def qr_start():
    data = request.json
    code, uid = data["code"].upper(), str(data["user_id"])
    with qr_lock:
        room = qr_rooms.get(code)
        if not room:
            return jsonify({"error": "room_not_found"}), 404
        if room["host"] != uid:
            return jsonify({"error": "not_host"}), 403
        if room["status"] != "waiting":
            return jsonify({"error": "invalid_state"}), 400
        if len(room["players"]) != 2:
            return jsonify({"error": "need_more_players"}), 400
        touch_player(room, uid)
        N = QR_N
        p0, p1 = room["players"][0]["id"], room["players"][1]["id"]
        mid = N // 2
        room["pos"] = {p0: [0, mid], p1: [N - 1, mid]}
        room["goal_row"] = {p0: N - 1, p1: 0}
        room["walls_left"] = {p0: QR_WALLS, p1: QR_WALLS}
        room["status"] = "playing"
        room["turn"] = p0
        room["winner"] = None
    return jsonify({"state": qr_public_state(room, uid)})


@app.route("/api/qr/move", methods=["POST"])
def qr_move():
    data = request.json
    code, uid = data["code"].upper(), str(data["user_id"])
    row, col = int(data["row"]), int(data["col"])
    with qr_lock:
        room = qr_rooms.get(code)
        if not room or room["status"] != "playing":
            return jsonify({"error": "invalid_state"}), 400
        touch_player(room, uid)
        qr_resolve_room(room)
        if room["status"] != "playing":
            return jsonify({"state": qr_public_state(room, uid)})
        if room["turn"] != uid:
            return jsonify({"error": "not_your_turn"}), 400
        if (row, col) not in qr_legal_moves(room, uid):
            return jsonify({"error": "invalid_move"}), 400
        room["pos"][uid] = [row, col]
        if not qr_check_win(room, uid):
            room["turn"] = qr_other(room, uid)
    return jsonify({"state": qr_public_state(room, uid)})


@app.route("/api/qr/wall", methods=["POST"])
def qr_wall():
    data = request.json
    code, uid = data["code"].upper(), str(data["user_id"])
    wtype, i, j = data["wtype"], int(data["i"]), int(data["j"])
    with qr_lock:
        room = qr_rooms.get(code)
        if not room or room["status"] != "playing":
            return jsonify({"error": "invalid_state"}), 400
        touch_player(room, uid)
        qr_resolve_room(room)
        if room["status"] != "playing":
            return jsonify({"state": qr_public_state(room, uid)})
        if room["turn"] != uid:
            return jsonify({"error": "not_your_turn"}), 400
        if wtype not in ("h", "v"):
            return jsonify({"error": "invalid_move"}), 400
        ok, err = qr_try_place_wall(room, uid, wtype, i, j)
        if not ok:
            return jsonify({"error": err}), 400
        room["turn"] = qr_other(room, uid)
    return jsonify({"state": qr_public_state(room, uid)})


@app.route("/api/qr/chat", methods=["POST"])
def qr_chat():
    data = request.json
    code, uid, text = data["code"].upper(), str(data["user_id"]), data.get("text", "")[:300]
    reaction = bool(data.get("reaction", False))
    with qr_lock:
        room = qr_rooms.get(code)
        if not room:
            return jsonify({"ok": True})
        player = next((p for p in room["players"] if p["id"] == uid), None)
        name = player["name"] if player else "?"
        room.setdefault("chat", []).append({"name": name, "text": text, "reaction": reaction})
    return jsonify({"ok": True})


# ---------- جستجوی بازیکن محاصره ----------
qr_mm_queue = []
qr_mm_matched = {}


@app.route("/api/qr/matchmake", methods=["POST"])
def qr_matchmake():
    data = request.json
    uid, name = str(data["user_id"]), data.get("name", "بازیکن")
    now = time.time()
    with qr_lock:
        for k, v in list(qr_mm_matched.items()):
            if now - v["ts"] > MM_KEEP:
                del qr_mm_matched[k]
        if uid in qr_mm_matched:
            return jsonify({"status": "matched", "code": qr_mm_matched.pop(uid)["code"]})
        qr_mm_queue[:] = [p for p in qr_mm_queue if (p["id"] == uid) or (now - p["ts"] < MM_STALE)]
        me = next((p for p in qr_mm_queue if p["id"] == uid), None)
        if me:
            me["ts"], me["name"] = now, name
        else:
            qr_mm_queue.append({"id": uid, "name": name, "ts": now})
        if len(qr_mm_queue) < 2:
            return jsonify({"status": "searching", "found": len(qr_mm_queue)})
        group = qr_mm_queue[:2]
        del qr_mm_queue[:2]
        code = qr_gen_code()
        room = qr_new_room(code, group[0]["id"], group[0]["name"])
        room["players"].append({"id": group[1]["id"], "name": group[1]["name"]})
        qr_rooms[code] = room
        for p in group:
            if p["id"] != uid:
                qr_mm_matched[p["id"]] = {"code": code, "ts": now}
        return jsonify({"status": "matched", "code": code})


@app.route("/api/qr/matchmake_cancel", methods=["POST"])
def qr_matchmake_cancel():
    uid = str(request.json["user_id"])
    with qr_lock:
        qr_mm_queue[:] = [p for p in qr_mm_queue if p["id"] != uid]
        qr_mm_matched.pop(uid, None)
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
