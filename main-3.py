import os
import io
import json
import random
import string
import logging
import datetime
import libsql_experimental as libsql
from aiohttp import web
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application

logging.basicConfig(level=logging.INFO)

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
WEBHOOK_HOST = os.environ["WEBHOOK_HOST"]
WEBHOOK_PATH = f"/webhook/{TELEGRAM_TOKEN}"
PORT = int(os.environ.get("PORT", 10000))

# Admin Telegram ID'lari (vergul bilan ajratilgan), masalan: "123456789,987654321"
ADMIN_IDS = set(
    int(x) for x in os.environ.get("ADMIN_IDS", "").replace(" ", "").split(",") if x
)

# Turso (bulutdagi doimiy SQLite) ulanish ma'lumotlari - Render Environment'da o'rnatiladi
TURSO_URL = os.environ["TURSO_DATABASE_URL"]
TURSO_TOKEN = os.environ["TURSO_AUTH_TOKEN"]

bot = Bot(token=TELEGRAM_TOKEN)
dp = Dispatcher(storage=MemoryStorage())


# ============================================================
# MA'LUMOTLAR BAZASI (Turso - doimiy, bepul, bulutda)
# ============================================================
# Quyidagi wrapper klasslar sqlite3'ning conn.execute(...).fetchone()["ustun"]
# uslubini saqlab qoladi - shu sababli pastdagi barcha SQL kodlar o'zgarishsiz ishlaydi.

class DictCursor:
    def __init__(self, raw_cursor):
        self._cursor = raw_cursor

    def _cols(self):
        desc = self._cursor.description
        if desc:
            return [d[0] for d in desc]
        return None

    def fetchone(self):
        row = self._cursor.fetchone()
        if row is None:
            return None
        cols = self._cols() or [str(i) for i in range(len(row))]
        return dict(zip(cols, row))

    def fetchall(self):
        rows = self._cursor.fetchall()
        if not rows:
            return []
        cols = self._cols() or [str(i) for i in range(len(rows[0]))]
        return [dict(zip(cols, r)) for r in rows]


class TursoConn:
    def __init__(self):
        self._conn = libsql.connect(TURSO_URL, auth_token=TURSO_TOKEN)

    def execute(self, sql, params=()):
        cur = self._conn.execute(sql, params)
        return DictCursor(cur)

    def executemany(self, sql, seq):
        for params in seq:
            self._conn.execute(sql, params)

    def executescript(self, script):
        for stmt in script.split(";"):
            stmt = stmt.strip()
            if stmt:
                self._conn.execute(stmt)

    def commit(self):
        self._conn.commit()

    def close(self):
        pass

    def cursor(self):
        return self


def get_db():
    return TursoConn()


def init_db():
    conn = get_db()
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS customers (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        telegram_id INTEGER UNIQUE NOT NULL,
        full_name TEXT,
        phone TEXT,
        code TEXT UNIQUE NOT NULL,
        referred_by_code TEXT,
        spin_available INTEGER DEFAULT 0,
        created_at TEXT
    );

    CREATE TABLE IF NOT EXISTS spins (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        customer_id INTEGER NOT NULL,
        prize_name TEXT NOT NULL,
        awarded INTEGER DEFAULT 0,
        created_at TEXT,
        awarded_at TEXT,
        FOREIGN KEY(customer_id) REFERENCES customers(id)
    );

    CREATE TABLE IF NOT EXISTS referrals (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        referrer_code TEXT NOT NULL,
        referred_code TEXT NOT NULL,
        status TEXT DEFAULT 'pending',
        amount INTEGER DEFAULT 0,
        created_at TEXT,
        paid_at TEXT
    );

    CREATE TABLE IF NOT EXISTS prizes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        probability REAL NOT NULL,
        active INTEGER DEFAULT 1
    );

    CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT
    );
    """)
    conn.commit()

    prizes_count = list(conn.execute("SELECT COUNT(*) c FROM prizes").fetchone().values())[0]
    if prizes_count == 0:
        conn.executemany(
            "INSERT INTO prizes (name, probability, active) VALUES (?, ?, 1)",
            [
                ("Polik (kilamcha)", 40.0),
                ("Brizgavik", 25.0),
                ("Podnomer ramkasi", 20.0),
                ("Bepul tuning xizmati", 10.0),
                ("Katta sovg'a", 5.0),
            ],
        )
        conn.commit()

    settings_count = list(
        conn.execute("SELECT COUNT(*) c FROM settings WHERE key='referral_amount'").fetchone().values()
    )[0]
    if settings_count == 0:
        conn.execute("INSERT INTO settings (key, value) VALUES ('referral_amount', '500000')")
        conn.commit()

    conn.close()


def gen_code():
    return "".join(random.choices(string.digits, k=6))


def get_setting(key, default=None):
    conn = get_db()
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    conn.close()
    return row["value"] if row else default


def pick_prize():
    conn = get_db()
    prizes = conn.execute("SELECT * FROM prizes WHERE active=1").fetchall()
    conn.close()
    if not prizes:
        return "Sovg'a"
    names = [p["name"] for p in prizes]
    weights = [p["probability"] for p in prizes]
    return random.choices(names, weights=weights, k=1)[0]


def now_str():
    return datetime.datetime.utcnow().isoformat()


# ============================================================
# HOLATLAR
# ============================================================

class Reg(StatesGroup):
    waiting_phone = State()


# ============================================================
# /START — RO'YXATDAN O'TISH + TELEFON + REFERAL + BIRINCHI SPIN
# ============================================================

@dp.message(CommandStart())
async def start_handler(message: types.Message, state: FSMContext):
    conn = get_db()
    existing = conn.execute(
        "SELECT * FROM customers WHERE telegram_id=?", (message.from_user.id,)
    ).fetchone()

    if existing:
        conn.close()
        await show_main_menu(message, existing)
        return

    # Referal kodini deep-link parametridan olish: /start 123456
    ref_code = None
    parts = message.text.split(maxsplit=1)
    if len(parts) > 1:
        candidate = parts[1].strip()
        row = conn.execute("SELECT * FROM customers WHERE code=?", (candidate,)).fetchone()
        if row and row["telegram_id"] != message.from_user.id:
            ref_code = candidate
    conn.close()

    await state.update_data(ref_code=ref_code)

    contact_kb = types.ReplyKeyboardMarkup(
        keyboard=[[types.KeyboardButton(text="📱 Raqamni ulashish", request_contact=True)]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )
    await message.answer(
        "Salom! MS AUTOCREDIT botiga xush kelibsiz.\n\n"
        "Ro'yxatdan o'tish uchun telefon raqamingizni ulashing:",
        reply_markup=contact_kb,
    )
    await state.set_state(Reg.waiting_phone)


@dp.message(Reg.waiting_phone, F.contact)
async def phone_received(message: types.Message, state: FSMContext):
    data = await state.get_data()
    ref_code = data.get("ref_code")

    conn = get_db()
    code = gen_code()
    while conn.execute("SELECT 1 FROM customers WHERE code=?", (code,)).fetchone():
        code = gen_code()

    conn.execute(
        "INSERT INTO customers (telegram_id, full_name, phone, code, referred_by_code, spin_available, created_at) "
        "VALUES (?, ?, ?, ?, ?, 0, ?)",
        (
            message.from_user.id,
            message.from_user.full_name,
            message.contact.phone_number,
            code,
            ref_code,
            now_str(),
        ),
    )
    conn.commit()
    customer_id = conn.execute("SELECT id FROM customers WHERE code=?", (code,)).fetchone()["id"]

    if ref_code:
        amount = int(get_setting("referral_amount", "500000"))
        conn.execute(
            "INSERT INTO referrals (referrer_code, referred_code, status, amount, created_at) "
            "VALUES (?, ?, 'pending', ?, ?)",
            (ref_code, code, amount, now_str()),
        )
        conn.commit()

    # Birinchi (avtomatik) aylantirish
    prize = pick_prize()
    conn.execute(
        "INSERT INTO spins (customer_id, prize_name, awarded, created_at) VALUES (?, ?, 0, ?)",
        (customer_id, prize, now_str()),
    )
    conn.commit()
    conn.close()

    bot_username = (await bot.get_me()).username
    ref_link = f"https://t.me/{bot_username}?start={code}"

    await message.answer(
        f"✅ Ro'yxatdan o'tdingiz!\n\n"
        f"🔑 Sizning kodingiz: <code>{code}</code>\n"
        f"(Xarid vaqtida shu kodni sotuvchiga ayting)\n\n"
        f"🎉 Tabriklaymiz! Barabandan yutuqingiz:\n"
        f"🎁 <b>{prize}</b>\n\n"
        f"Sovg'ani olish uchun do'konga tashrif buyuring va kodingizni ko'rsating.\n\n"
        f"👥 Do'stlaringizni taklif qiling va pul bonusiga ega bo'ling:\n{ref_link}",
        parse_mode="HTML",
        reply_markup=types.ReplyKeyboardRemove(),
    )
    await state.clear()

    class FakeMsgUser:
        pass

    conn = get_db()
    customer = conn.execute("SELECT * FROM customers WHERE telegram_id=?", (message.from_user.id,)).fetchone()
    conn.close()
    await show_main_menu(message, customer)


# ============================================================
# ASOSIY MENYU
# ============================================================

def build_main_kb(customer):
    rows = [
        [types.KeyboardButton(text="🚗 Avtomobillar")],
        [types.KeyboardButton(text="🎁 Mening bonuslarim")],
        [types.KeyboardButton(text="👥 Do'stimni taklif qilish")],
    ]
    if customer["spin_available"]:
        rows.insert(0, [types.KeyboardButton(text="🎡 Barabanni aylantirish")])
    return types.ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


async def show_main_menu(message, customer):
    await message.answer(
        f"🏠 Asosiy menyu\n🔑 Sizning kodingiz: <code>{customer['code']}</code>",
        parse_mode="HTML",
        reply_markup=build_main_kb(customer),
    )


@dp.message(F.text == "🚗 Avtomobillar")
async def show_cars_info(message: types.Message):
    await message.answer(
        "🚗 Avtomobillar haqida to'liq ma'lumot va narxlarni Web App orqali ko'rishingiz mumkin.\n"
        "(Web App havolasi tez orada qo'shiladi)"
    )


@dp.message(F.text == "🎁 Mening bonuslarim")
async def show_my_bonuses(message: types.Message):
    conn = get_db()
    customer = conn.execute(
        "SELECT * FROM customers WHERE telegram_id=?", (message.from_user.id,)
    ).fetchone()
    if not customer:
        await message.answer("Avval /start bosing.")
        conn.close()
        return

    spins = conn.execute(
        "SELECT * FROM spins WHERE customer_id=? ORDER BY id DESC", (customer["id"],)
    ).fetchall()
    referrals = conn.execute(
        "SELECT * FROM referrals WHERE referrer_code=? ORDER BY id DESC", (customer["code"],)
    ).fetchall()
    conn.close()

    text = f"🔑 Kodingiz: <code>{customer['code']}</code>\n\n🎁 Yutuqlaringiz:\n"
    if spins:
        for s in spins:
            status = "✅ olingan" if s["awarded"] else "⏳ do'konda kutilmoqda"
            text += f"— {s['prize_name']} ({status})\n"
    else:
        text += "Hozircha yutuq yo'q.\n"

    text += "\n👥 Taklif qilganlaringiz:\n"
    if referrals:
        for r in referrals:
            status = "✅ to'landi" if r["status"] == "paid" else "⏳ kutilmoqda"
            text += f"— {r['referred_code']}: {r['amount']:,} so'm ({status})\n"
    else:
        text += "Hozircha hech kimni taklif qilmadingiz.\n"

    await message.answer(text, parse_mode="HTML")


@dp.message(F.text == "👥 Do'stimni taklif qilish")
async def show_referral_link(message: types.Message):
    conn = get_db()
    customer = conn.execute(
        "SELECT * FROM customers WHERE telegram_id=?", (message.from_user.id,)
    ).fetchone()
    conn.close()
    if not customer:
        await message.answer("Avval /start bosing.")
        return

    bot_username = (await bot.get_me()).username
    ref_link = f"https://t.me/{bot_username}?start={customer['code']}"
    amount = get_setting("referral_amount", "500000")
    await message.answer(
        f"👥 Do'stingizni shu havola orqali taklif qiling:\n{ref_link}\n\n"
        f"Do'stingiz xarid qilsa, sizga <b>{int(amount):,} so'm</b> bonus beriladi!",
        parse_mode="HTML",
    )


@dp.message(F.text == "🎡 Barabanni aylantirish")
async def spin_wheel(message: types.Message):
    conn = get_db()
    customer = conn.execute(
        "SELECT * FROM customers WHERE telegram_id=?", (message.from_user.id,)
    ).fetchone()
    if not customer or not customer["spin_available"]:
        await message.answer("Hozircha aylantirish huquqingiz yo'q.")
        conn.close()
        return

    prize = pick_prize()
    conn.execute(
        "INSERT INTO spins (customer_id, prize_name, awarded, created_at) VALUES (?, ?, 0, ?)",
        (customer["id"], prize, now_str()),
    )
    conn.execute("UPDATE customers SET spin_available=0 WHERE id=?", (customer["id"],))
    conn.commit()
    conn.close()

    await message.answer(
        f"🎉 Tabriklaymiz!\n🎁 Yutuqingiz: <b>{prize}</b>\n\n"
        f"Sovg'ani olish uchun do'konga tashrif buyuring.",
        parse_mode="HTML",
    )

    conn = get_db()
    customer = conn.execute("SELECT * FROM customers WHERE telegram_id=?", (message.from_user.id,)).fetchone()
    conn.close()
    await message.answer("🏠 Asosiy menyu", reply_markup=build_main_kb(customer))


# ============================================================
# ADMIN PANEL (Web App orqali kirish tugmasi)
# ============================================================

ADMIN_WEBAPP_URL = os.environ.get("ADMIN_WEBAPP_URL", "")


@dp.message(F.text == "/admin")
async def admin_entry(message: types.Message):
    if message.from_user.id not in ADMIN_IDS:
        await message.answer("Sizda ruxsat yo'q.")
        return
    if not ADMIN_WEBAPP_URL:
        await message.answer("Admin panel manzili sozlanmagan (ADMIN_WEBAPP_URL).")
        return
    kb = types.InlineKeyboardMarkup(
        inline_keyboard=[[
            types.InlineKeyboardButton(text="🛠 Admin panelni ochish", web_app=types.WebAppInfo(url=ADMIN_WEBAPP_URL))
        ]]
    )
    await message.answer("🛠 Admin panel:", reply_markup=kb)


# ============================================================
# ADMIN API (Web App'dan chaqiriladi)
# ============================================================

CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type",
}


def is_admin(admin_id):
    try:
        return int(admin_id) in ADMIN_IDS
    except (TypeError, ValueError):
        return False


async def options_handler(request):
    return web.Response(headers=CORS_HEADERS)


async def api_search_customer(request):
    data = await request.json()
    if not is_admin(data.get("admin_id")):
        return web.json_response({"ok": False, "error": "Ruxsat yo'q"}, status=403, headers=CORS_HEADERS)

    code = data.get("code", "").strip()
    conn = get_db()
    customer = conn.execute("SELECT * FROM customers WHERE code=?", (code,)).fetchone()
    if not customer:
        conn.close()
        return web.json_response({"ok": False, "error": "Mijoz topilmadi"}, headers=CORS_HEADERS)

    spins = conn.execute(
        "SELECT * FROM spins WHERE customer_id=? ORDER BY id DESC", (customer["id"],)
    ).fetchall()
    referrals_out = conn.execute(
        "SELECT * FROM referrals WHERE referrer_code=? ORDER BY id DESC", (customer["code"],)
    ).fetchall()
    conn.close()

    return web.json_response({
        "ok": True,
        "customer": dict(customer),
        "spins": [dict(s) for s in spins],
        "referrals_given": [dict(r) for r in referrals_out],
    }, headers=CORS_HEADERS)


async def api_confirm_prize(request):
    data = await request.json()
    if not is_admin(data.get("admin_id")):
        return web.json_response({"ok": False, "error": "Ruxsat yo'q"}, status=403, headers=CORS_HEADERS)

    spin_id = data.get("spin_id")
    conn = get_db()
    spin = conn.execute("SELECT * FROM spins WHERE id=?", (spin_id,)).fetchone()
    if not spin:
        conn.close()
        return web.json_response({"ok": False, "error": "Yutuq topilmadi"}, headers=CORS_HEADERS)

    conn.execute("UPDATE spins SET awarded=1, awarded_at=? WHERE id=?", (now_str(), spin_id))
    conn.execute("UPDATE customers SET spin_available=1 WHERE id=?", (spin["customer_id"],))
    conn.commit()
    conn.close()
    return web.json_response({"ok": True}, headers=CORS_HEADERS)


async def api_confirm_referral(request):
    data = await request.json()
    if not is_admin(data.get("admin_id")):
        return web.json_response({"ok": False, "error": "Ruxsat yo'q"}, status=403, headers=CORS_HEADERS)

    ref_id = data.get("referral_id")
    conn = get_db()
    conn.execute("UPDATE referrals SET status='paid', paid_at=? WHERE id=?", (now_str(), ref_id))
    conn.commit()
    conn.close()
    return web.json_response({"ok": True}, headers=CORS_HEADERS)


async def api_get_prizes(request):
    admin_id = request.query.get("admin_id")
    if not is_admin(admin_id):
        return web.json_response({"ok": False, "error": "Ruxsat yo'q"}, status=403, headers=CORS_HEADERS)
    conn = get_db()
    prizes = conn.execute("SELECT * FROM prizes ORDER BY id").fetchall()
    conn.close()
    return web.json_response({"ok": True, "prizes": [dict(p) for p in prizes]}, headers=CORS_HEADERS)


async def api_update_prizes(request):
    data = await request.json()
    if not is_admin(data.get("admin_id")):
        return web.json_response({"ok": False, "error": "Ruxsat yo'q"}, status=403, headers=CORS_HEADERS)

    conn = get_db()
    for p in data.get("prizes", []):
        conn.execute(
            "UPDATE prizes SET name=?, probability=?, active=? WHERE id=?",
            (p["name"], float(p["probability"]), int(p.get("active", 1)), p["id"]),
        )
    conn.commit()
    conn.close()
    return web.json_response({"ok": True}, headers=CORS_HEADERS)


async def api_stats(request):
    admin_id = request.query.get("admin_id")
    if not is_admin(admin_id):
        return web.json_response({"ok": False, "error": "Ruxsat yo'q"}, status=403, headers=CORS_HEADERS)

    conn = get_db()
    total_customers = list(conn.execute("SELECT COUNT(*) c FROM customers").fetchone().values())[0]
    total_spins = list(conn.execute("SELECT COUNT(*) c FROM spins").fetchone().values())[0]
    prize_breakdown_raw = conn.execute(
        "SELECT prize_name, COUNT(*) c, SUM(awarded) awarded_c FROM spins GROUP BY prize_name"
    ).fetchall()
    prize_breakdown = [
        {"prize_name": list(row.values())[0], "c": list(row.values())[1], "awarded_c": list(row.values())[2]}
        for row in prize_breakdown_raw
    ]
    ref_pending_vals = list(conn.execute(
        "SELECT COUNT(*) c, COALESCE(SUM(amount),0) s FROM referrals WHERE status='pending'"
    ).fetchone().values())
    ref_paid_vals = list(conn.execute(
        "SELECT COUNT(*) c, COALESCE(SUM(amount),0) s FROM referrals WHERE status='paid'"
    ).fetchone().values())
    conn.close()

    return web.json_response({
        "ok": True,
        "total_customers": total_customers,
        "total_spins": total_spins,
        "prize_breakdown": prize_breakdown,
        "referral_pending_count": ref_pending_vals[0],
        "referral_pending_sum": ref_pending_vals[1],
        "referral_paid_count": ref_paid_vals[0],
        "referral_paid_sum": ref_paid_vals[1],
    }, headers=CORS_HEADERS)


async def api_list_customers(request):
    admin_id = request.query.get("admin_id")
    if not is_admin(admin_id):
        return web.json_response({"ok": False, "error": "Ruxsat yo'q"}, status=403, headers=CORS_HEADERS)
    conn = get_db()
    customers = conn.execute("SELECT * FROM customers ORDER BY id DESC LIMIT 200").fetchall()
    conn.close()
    return web.json_response({"ok": True, "customers": [dict(c) for c in customers]}, headers=CORS_HEADERS)


# ============================================================
# WEBHOOK VA ILOVANI ISHGA TUSHIRISH
# ============================================================

async def on_startup(app: web.Application):
    init_db()
    await bot.set_webhook(f"{WEBHOOK_HOST}{WEBHOOK_PATH}")


def main():
    app = web.Application()

    routes = [
        ("POST", "/api/admin/search-customer", api_search_customer),
        ("POST", "/api/admin/confirm-prize", api_confirm_prize),
        ("POST", "/api/admin/confirm-referral", api_confirm_referral),
        ("GET", "/api/admin/prizes", api_get_prizes),
        ("POST", "/api/admin/prizes", api_update_prizes),
        ("GET", "/api/admin/stats", api_stats),
        ("GET", "/api/admin/customers", api_list_customers),
    ]
    for method, path, handler in routes:
        app.router.add_route(method, path, handler)
        app.router.add_route("OPTIONS", path, options_handler)

    SimpleRequestHandler(dispatcher=dp, bot=bot).register(app, path=WEBHOOK_PATH)
    setup_application(app, dp, bot=bot)
    app.on_startup.append(on_startup)
    web.run_app(app, host="0.0.0.0", port=PORT)


if __name__ == "__main__":
    main()
