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

# Buyurtmalarga javob beradigan YAGONA admin (masalan Sabrina)
MAIN_ADMIN_ID = int(os.environ.get("MAIN_ADMIN_ID", "0") or 0)

# Do'kon/ofis manzili va joylashuvi (mijozga xarid tasdiqlanganda yuboriladi)
SHOP_ADDRESS = os.environ.get("SHOP_ADDRESS", "Manzil hali sozlanmagan")
SHOP_LAT = os.environ.get("SHOP_LAT")
SHOP_LON = os.environ.get("SHOP_LON")

# Turso (bulutdagi doimiy SQLite) ulanish ma'lumotlari - Render Environment'da o'rnatiladi
TURSO_URL = os.environ["TURSO_DATABASE_URL"]
TURSO_TOKEN = os.environ["TURSO_AUTH_TOKEN"]

bot = Bot(token=TELEGRAM_TOKEN)
dp = Dispatcher(storage=MemoryStorage())


# ============================================================
# MA'LUMOTLAR BAZASI (Turso - doimiy, bepul, bulutda)
# ============================================================
# MUHIM: libsql_experimental drayveri cursor.description orqali ustun
# nomlarini ishonchli qaytarmaydi, shu sababli har bir jadval uchun ustun
# nomlari shu yerda QAT'IY (qo'lda) belgilanadi va SQL natijasi shularga
# mos ravishda dict'ga aylantiriladi - drayverga umuman tayanilmaydi.

TABLE_COLUMNS = {
    "customers": ["id", "telegram_id", "full_name", "phone", "code", "referred_by_code", "spin_available", "created_at"],
    "spins": ["id", "customer_id", "prize_name", "awarded", "created_at", "awarded_at"],
    "referrals": ["id", "referrer_code", "referred_code", "status", "amount", "created_at", "paid_at"],
    "prizes": ["id", "name", "probability", "active"],
    "settings": ["key", "value"],
    "orders": [
        "id", "customer_id", "order_type", "details", "status",
        "admin_reply", "created_at", "answered_at", "confirmed_at",
    ],
}


def guess_table(sql: str):
    sql_low = sql.lower()
    for table in TABLE_COLUMNS:
        if f"from {table}" in sql_low or f"into {table}" in sql_low:
            return table
    return None


class DictCursor:
    def __init__(self, raw_cursor, cols=None):
        self._cursor = raw_cursor
        self._cols_override = cols

    def fetchone(self):
        row = self._cursor.fetchone()
        if row is None:
            return None
        cols = self._cols_override or [str(i) for i in range(len(row))]
        return dict(zip(cols, row))

    def fetchall(self):
        rows = self._cursor.fetchall()
        if not rows:
            return []
        cols = self._cols_override or [str(i) for i in range(len(rows[0]))]
        return [dict(zip(cols, r)) for r in rows]


class TursoConn:
    def __init__(self):
        self._conn = libsql.connect(TURSO_URL, auth_token=TURSO_TOKEN)

    def execute(self, sql, params=(), cols=None):
        cur = self._conn.execute(sql, params)
        # Ustun ro'yxati aniq berilmagan bo'lsa, "SELECT * FROM jadval" so'rovlari
        # uchun jadval nomidan avtomatik aniqlanadi.
        if cols is None and "select *" in sql.lower():
            table = guess_table(sql)
            if table:
                cols = TABLE_COLUMNS[table]
        return DictCursor(cur, cols)

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

    CREATE TABLE IF NOT EXISTS orders (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        customer_id INTEGER NOT NULL,
        order_type TEXT NOT NULL,
        details TEXT,
        status TEXT DEFAULT 'pending',
        admin_reply TEXT,
        created_at TEXT,
        answered_at TEXT,
        confirmed_at TEXT
    );
    """)
    conn.commit()

    prizes_count = list(conn.execute("SELECT COUNT(*) c FROM prizes").fetchone().values())[0]
    if prizes_count == 0:
        conn.executemany(
            "INSERT INTO prizes (name, probability, active) VALUES (?, ?, ?)",
            [
                ("Polik (kilamcha)", 40.0, 1),
                ("Brizgavik", 25.0, 1),
                ("Podnomer ramkasi", 20.0, 1),
                ("Bepul tuning xizmati", 10.0, 1),
                ("Katta sovg'a", 5.0, 1),
                ("Bo'sh joy 6", 0.0, 0),
                ("Bo'sh joy 7", 0.0, 0),
                ("Bo'sh joy 8", 0.0, 0),
                ("Bo'sh joy 9", 0.0, 0),
                ("Bo'sh joy 10", 0.0, 0),
            ],
        )
        conn.commit()
    elif prizes_count == 5:
        # Eski (5 ta joyli) baza - yangi 5 ta bo'sh joy qo'shib qo'yamiz
        conn.executemany(
            "INSERT INTO prizes (name, probability, active) VALUES (?, ?, ?)",
            [
                ("Bo'sh joy 6", 0.0, 0),
                ("Bo'sh joy 7", 0.0, 0),
                ("Bo'sh joy 8", 0.0, 0),
                ("Bo'sh joy 9", 0.0, 0),
                ("Bo'sh joy 10", 0.0, 0),
            ],
        )
        conn.commit()

    settings_count = list(
        conn.execute("SELECT COUNT(*) c FROM settings WHERE key='referral_amount'").fetchone().values()
    )[0]
    if settings_count == 0:
        conn.execute("INSERT INTO settings (key, value) VALUES ('referral_amount', '200000')")
        conn.commit()
    else:
        # Eski standart qiymat (500000) qolib ketgan bo'lsa, bir martalik yangilash
        conn.execute(
            "UPDATE settings SET value='200000' WHERE key='referral_amount' AND value='500000'"
        )
        conn.commit()

    conn.close()


def gen_code():
    return "".join(random.choices(string.digits, k=6))


def get_setting(key, default=None):
    conn = get_db()
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,), cols=["value"]).fetchone()
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
# AVTOMOBIL KATALOGI (buyurtma uchun, faqat ko'rsatish maqsadida)
# ============================================================

ORDER_CARS = {
    "TRACKER-2": {
        "LS PLUS AT": 220_951_000, "LTZ TURBO AT": 244_108_840,
        "PREMIER TURBO AT": 272_656_160, "REDLINE TURBO AT": 282_474_080,
    },
    "ONIX": {
        "3 LT MT": 184_750_000, "LTZ TURBO AT": 199_899_000,
        "PREMIER 2 TURBO AT": 221_640_160, "REDLINE TURBO AT": 230_474_000,
    },
    "COBALT": {"Style MCM": 156_100_000, "Midnight MCM": 165_200_000},
    "DAMAS": {"STAYL": 96_932_000, "VAN": 93_170_000, "KOMBI": 96_449_000},
    "LABO": {"Bazaviy": 96_370_000},
    "CAPTIVA 5": {"Bazaviy": 349_900_000},
}
ORDER_PERCENTS = [25, 30, 40, 50]


def create_order(customer_id, order_type, details):
    conn = get_db()
    conn.execute(
        "INSERT INTO orders (customer_id, order_type, details, status, created_at) "
        "VALUES (?, ?, ?, 'pending', ?)",
        (customer_id, order_type, details, now_str()),
    )
    conn.commit()
    order_id = list(
        conn.execute("SELECT id FROM orders ORDER BY id DESC LIMIT 1", cols=["id"]).fetchone().values()
    )[0]
    conn.close()
    return order_id


async def notify_admin_new_order(order_id, customer, details_text):
    if not MAIN_ADMIN_ID:
        return
    kb = types.InlineKeyboardMarkup(
        inline_keyboard=[[
            types.InlineKeyboardButton(text="✍️ Javob yozish", callback_data=f"order_reply:{order_id}")
        ]]
    )
    await bot.send_message(
        MAIN_ADMIN_ID,
        f"🛒 <b>Yangi buyurtma</b>\n\n"
        f"👤 {customer['full_name']}\n"
        f"📱 {customer['phone']}\n"
        f"🔑 Kod: <code>{customer['code']}</code>\n\n"
        f"{details_text}",
        parse_mode="HTML",
        reply_markup=kb,
    )


# ============================================================
# HOLATLAR
# ============================================================

class Reg(StatesGroup):
    waiting_phone = State()


class OrderFlow(StatesGroup):
    waiting_custom_text = State()


class AdminFlow(StatesGroup):
    waiting_reply = State()


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
        "VALUES (?, ?, ?, ?, ?, 1, ?)",
        (
            message.from_user.id,
            message.from_user.full_name,
            message.contact.phone_number,
            code,
            ref_code or "",
            now_str(),
        ),
    )
    conn.commit()

    if ref_code:
        amount = int(get_setting("referral_amount", "200000"))
        conn.execute(
            "INSERT INTO referrals (referrer_code, referred_code, status, amount, created_at) "
            "VALUES (?, ?, 'pending', ?, ?)",
            (ref_code, code, amount, now_str()),
        )
        conn.commit()

    conn.close()

    bot_username = (await bot.get_me()).username
    ref_link = f"https://t.me/{bot_username}?start={code}"

    await message.answer(
        f"✅ Ro'yxatdan o'tdingiz!\n\n"
        f"🔑 Sizning kodingiz: <code>{code}</code>\n"
        f"(Xarid vaqtida shu kodni sotuvchiga ayting)\n\n"
        f"🎁 Sizga sovg'a tayyorlandi! Pastdagi tugma orqali oling.\n\n"
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

CUSTOMER_WEBAPP_URL = os.environ.get("CUSTOMER_WEBAPP_URL", "")


def build_main_kb(customer):
    rows = []
    if customer["spin_available"]:
        rows.append([types.InlineKeyboardButton(text="🎁 Sovg'amni olish", callback_data="spin_wheel")])
    rows.append([types.InlineKeyboardButton(text="🛒 Buyurtma berish", callback_data="order_menu")])
    rows.append([types.InlineKeyboardButton(text="🎁 Bonuslarim", callback_data="show_bonus")])
    rows.append([types.InlineKeyboardButton(text="👥 Do'stimni taklif qilish", callback_data="show_referral")])
    if CUSTOMER_WEBAPP_URL:
        rows.append([types.InlineKeyboardButton(
            text="🌐 Katalog (Web App)", web_app=types.WebAppInfo(url=CUSTOMER_WEBAPP_URL)
        )])
    return types.InlineKeyboardMarkup(inline_keyboard=rows)


async def show_main_menu(message, customer):
    await message.answer(
        f"🏠 Asosiy menyu\n🔑 Sizning kodingiz: <code>{customer['code']}</code>",
        parse_mode="HTML",
        reply_markup=build_main_kb(customer),
    )


def get_customer_by_tg(telegram_id):
    conn = get_db()
    customer = conn.execute("SELECT * FROM customers WHERE telegram_id=?", (telegram_id,)).fetchone()
    conn.close()
    return customer


@dp.callback_query(F.data == "back_main")
async def back_main_menu(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    customer = get_customer_by_tg(callback.from_user.id)
    if not customer:
        await callback.answer("Avval /start bosing.", show_alert=True)
        return
    await callback.message.edit_text(
        f"🏠 Asosiy menyu\n🔑 Sizning kodingiz: <code>{customer['code']}</code>",
        parse_mode="HTML",
        reply_markup=build_main_kb(customer),
    )
    await callback.answer()


@dp.callback_query(F.data == "show_bonus")
async def show_my_bonuses(callback: types.CallbackQuery):
    customer = get_customer_by_tg(callback.from_user.id)
    if not customer:
        await callback.answer("Avval /start bosing.", show_alert=True)
        return

    conn = get_db()
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
            status = "✅ olingan" if s["awarded"] else "⏳ sotuv bo'limida kutilmoqda"
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

    kb = types.InlineKeyboardMarkup(
        inline_keyboard=[[types.InlineKeyboardButton(text="⬅️ Orqaga", callback_data="back_main")]]
    )
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()


@dp.callback_query(F.data == "show_referral")
async def show_referral_link(callback: types.CallbackQuery):
    customer = get_customer_by_tg(callback.from_user.id)
    if not customer:
        await callback.answer("Avval /start bosing.", show_alert=True)
        return

    bot_username = (await bot.get_me()).username
    ref_link = f"https://t.me/{bot_username}?start={customer['code']}"
    amount = get_setting("referral_amount", "200000")

    kb = types.InlineKeyboardMarkup(
        inline_keyboard=[[types.InlineKeyboardButton(text="⬅️ Orqaga", callback_data="back_main")]]
    )
    await callback.message.edit_text(
        f"👥 Do'stingizni shu havola orqali taklif qiling:\n{ref_link}\n\n"
        f"Do'stingiz xarid qilsa, sizga <b>{int(amount):,} so'm</b> bonus beriladi!",
        parse_mode="HTML",
        reply_markup=kb,
    )
    await callback.answer()


@dp.callback_query(F.data == "spin_wheel")
async def spin_wheel(callback: types.CallbackQuery):
    conn = get_db()
    customer = conn.execute(
        "SELECT * FROM customers WHERE telegram_id=?", (callback.from_user.id,)
    ).fetchone()
    if not customer or not customer["spin_available"]:
        await callback.answer("Hozircha sovg'a olish huquqingiz yo'q.", show_alert=True)
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
    await callback.answer()

    kb = types.InlineKeyboardMarkup(
        inline_keyboard=[[types.InlineKeyboardButton(text="⬅️ Asosiy menyu", callback_data="back_main")]]
    )
    await callback.message.edit_text(
        f"🎉 Tabriklaymiz!\n🎁 Yutuqingiz: <b>{prize}</b>\n\n"
        f"Sovg'ani olish uchun sotuv bo'limiga tashrif buyuring.",
        parse_mode="HTML",
        reply_markup=kb,
    )


# ============================================================
# ADMIN PANEL (Web App orqali kirish tugmasi)
# ============================================================

# ============================================================
# BUYURTMA TIZIMI (katalogdan yoki erkin so'rov orqali)
# ============================================================

@dp.callback_query(F.data == "order_menu")
async def order_menu(callback: types.CallbackQuery):
    kb = types.InlineKeyboardMarkup(
        inline_keyboard=[
            [types.InlineKeyboardButton(text="📋 Katalogdan tanlash", callback_data="order_catalog")],
            [types.InlineKeyboardButton(text="✍️ Erkin so'rov yozish", callback_data="order_custom")],
            [types.InlineKeyboardButton(text="⬅️ Orqaga", callback_data="back_main")],
        ]
    )
    await callback.message.edit_text("🛒 Buyurtma qanday tarzda berilsin?", reply_markup=kb)
    await callback.answer()


@dp.callback_query(F.data == "order_catalog")
async def order_catalog(callback: types.CallbackQuery):
    kb = types.InlineKeyboardMarkup(
        inline_keyboard=[
            [types.InlineKeyboardButton(text=model, callback_data=f"ord_model:{model}")]
            for model in ORDER_CARS
        ] + [[types.InlineKeyboardButton(text="⬅️ Orqaga", callback_data="order_menu")]]
    )
    await callback.message.edit_text("🚗 Avtomobil modelini tanlang:", reply_markup=kb)
    await callback.answer()


@dp.callback_query(F.data.startswith("ord_model:"))
async def order_pick_position(callback: types.CallbackQuery):
    model = callback.data.split(":", 1)[1]
    positions = ORDER_CARS[model]
    kb = types.InlineKeyboardMarkup(
        inline_keyboard=[
            [types.InlineKeyboardButton(
                text=f"{pos} — {price:,.0f} so'm", callback_data=f"ord_pos:{model}:{pos}"
            )]
            for pos, price in positions.items()
        ] + [[types.InlineKeyboardButton(text="⬅️ Orqaga", callback_data="order_catalog")]]
    )
    await callback.message.edit_text(f"🚘 {model} — pozitsiyani tanlang:", reply_markup=kb)
    await callback.answer()


@dp.callback_query(F.data.startswith("ord_pos:"))
async def order_pick_percent(callback: types.CallbackQuery):
    _, model, pos = callback.data.split(":")
    kb = types.InlineKeyboardMarkup(
        inline_keyboard=[
            [types.InlineKeyboardButton(text=f"{p}%", callback_data=f"ord_pct:{model}:{pos}:{p}")]
            for p in ORDER_PERCENTS
        ] + [[types.InlineKeyboardButton(text="⬅️ Orqaga", callback_data=f"ord_model:{model}")]]
    )
    await callback.message.edit_text(
        f"✅ {model} {pos}\n\nBoshlang'ich to'lov necha foiz bo'lsin?", reply_markup=kb
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("ord_pct:"))
async def order_confirm_catalog(callback: types.CallbackQuery):
    _, model, pos, pct = callback.data.split(":")
    price = ORDER_CARS[model][pos]
    customer = get_customer_by_tg(callback.from_user.id)
    if not customer:
        await callback.answer("Avval /start bosing.", show_alert=True)
        return

    details = f"🚗 {model} {pos}\n💰 Narxi: {price:,.0f} so'm\n💵 Boshlang'ich to'lov: {pct}%"
    order_id = create_order(customer["id"], "catalog", details)
    await notify_admin_new_order(order_id, customer, details)

    kb = types.InlineKeyboardMarkup(
        inline_keyboard=[[types.InlineKeyboardButton(text="⬅️ Asosiy menyu", callback_data="back_main")]]
    )
    await callback.message.edit_text(
        "✅ Buyurtmangiz qabul qilindi!\nTez orada sotuvchi siz bilan bog'lanadi.",
        reply_markup=kb,
    )
    await callback.answer()


@dp.callback_query(F.data == "order_custom")
async def order_custom_start(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.edit_text(
        "✍️ So'rovingizni yozing (masalan: qaysi avto, qancha muddat, qanday hisob-kitob kerakligini):"
    )
    await state.set_state(OrderFlow.waiting_custom_text)
    await callback.answer()


@dp.message(OrderFlow.waiting_custom_text)
async def order_custom_receive(message: types.Message, state: FSMContext):
    customer = get_customer_by_tg(message.from_user.id)
    if not customer:
        await message.answer("Avval /start bosing.")
        await state.clear()
        return

    details = f"✍️ Erkin so'rov:\n{message.text}"
    order_id = create_order(customer["id"], "custom", details)
    await notify_admin_new_order(order_id, customer, details)
    await state.clear()

    kb = types.InlineKeyboardMarkup(
        inline_keyboard=[[types.InlineKeyboardButton(text="⬅️ Asosiy menyu", callback_data="back_main")]]
    )
    await message.answer(
        "✅ So'rovingiz qabul qilindi!\nTez orada sotuvchi siz bilan bog'lanadi.",
        reply_markup=kb,
    )


# ---- Admin tomoni: buyurtmaga javob yozish va tasdiqlash ----

@dp.callback_query(F.data.startswith("order_reply:"))
async def admin_order_reply_start(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id != MAIN_ADMIN_ID:
        await callback.answer("Sizda ruxsat yo'q.", show_alert=True)
        return
    order_id = int(callback.data.split(":", 1)[1])
    await state.update_data(order_id=order_id)
    await callback.message.answer("✍️ Mijozga javobingizni yozing:")
    await state.set_state(AdminFlow.waiting_reply)
    await callback.answer()


@dp.message(AdminFlow.waiting_reply)
async def admin_order_reply_send(message: types.Message, state: FSMContext):
    data = await state.get_data()
    order_id = data.get("order_id")
    await state.clear()

    conn = get_db()
    order = conn.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
    if not order:
        conn.close()
        await message.answer("Buyurtma topilmadi.")
        return

    customer = conn.execute("SELECT * FROM customers WHERE id=?", (order["customer_id"],)).fetchone()
    conn.execute(
        "UPDATE orders SET status='answered', admin_reply=?, answered_at=? WHERE id=?",
        (message.text, now_str(), order_id),
    )
    conn.commit()
    conn.close()

    # Mijozga javobni + manzilni + joylashuvni yuborish
    await bot.send_message(
        customer["telegram_id"],
        f"💬 Sotuvchi javobi:\n\n{message.text}\n\n📍 Manzil: {SHOP_ADDRESS}",
    )
    if SHOP_LAT and SHOP_LON:
        try:
            await bot.send_location(customer["telegram_id"], latitude=float(SHOP_LAT), longitude=float(SHOP_LON))
        except (TypeError, ValueError):
            pass

    kb = types.InlineKeyboardMarkup(
        inline_keyboard=[[
            types.InlineKeyboardButton(text="✅ Xarid tasdiqlandi", callback_data=f"order_confirm:{order_id}")
        ]]
    )
    await message.answer("✅ Javob mijozga yuborildi.", reply_markup=kb)


@dp.callback_query(F.data.startswith("order_confirm:"))
async def admin_order_confirm(callback: types.CallbackQuery):
    if callback.from_user.id != MAIN_ADMIN_ID:
        await callback.answer("Sizda ruxsat yo'q.", show_alert=True)
        return
    order_id = int(callback.data.split(":", 1)[1])

    conn = get_db()
    order = conn.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
    if not order:
        conn.close()
        await callback.answer("Buyurtma topilmadi.", show_alert=True)
        return

    customer = conn.execute("SELECT * FROM customers WHERE id=?", (order["customer_id"],)).fetchone()

    conn.execute(
        "UPDATE orders SET status='confirmed', confirmed_at=? WHERE id=?", (now_str(), order_id)
    )
    # Keyingi sovg'a olish huquqini ochish
    conn.execute("UPDATE customers SET spin_available=1 WHERE id=?", (customer["id"],))
    # Agar mijoz kimdir tomonidan taklif qilingan bo'lsa - referal pulini "to'landi" qilish
    conn.execute(
        "UPDATE referrals SET status='paid', paid_at=? WHERE referred_code=? AND status='pending'",
        (now_str(), customer["code"]),
    )
    conn.commit()
    conn.close()

    await bot.send_message(
        customer["telegram_id"],
        "🎉 Xaridingiz tasdiqlandi! Rahmat.\n🎁 Endi yana sovg'a olish huquqingiz ochildi.",
    )
    await callback.message.edit_text(callback.message.text + "\n\n✅ Xarid tasdiqlandi.")
    await callback.answer("Tasdiqlandi!")


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


async def api_customer_info(request):
    """Mijozning o'zi Web App orqali o'z kodi, yutuqlari va referallarini ko'rishi uchun."""
    telegram_id = request.query.get("telegram_id")
    if not telegram_id:
        return web.json_response({"ok": False, "error": "telegram_id kerak"}, status=400, headers=CORS_HEADERS)

    conn = get_db()
    customer = conn.execute("SELECT * FROM customers WHERE telegram_id=?", (telegram_id,)).fetchone()
    if not customer:
        conn.close()
        return web.json_response({"ok": False, "error": "Mijoz topilmadi. Botda /start bosing."}, headers=CORS_HEADERS)

    spins = conn.execute(
        "SELECT * FROM spins WHERE customer_id=? ORDER BY id DESC", (customer["id"],)
    ).fetchall()
    referrals = conn.execute(
        "SELECT * FROM referrals WHERE referrer_code=? ORDER BY id DESC", (customer["code"],)
    ).fetchall()
    referral_amount = get_setting("referral_amount", "200000")
    conn.close()

    bot_username = (await bot.get_me()).username
    ref_link = f"https://t.me/{bot_username}?start={customer['code']}"

    return web.json_response({
        "ok": True,
        "customer": dict(customer),
        "spins": [dict(s) for s in spins],
        "referrals": [dict(r) for r in referrals],
        "referral_amount": int(referral_amount),
        "referral_link": ref_link,
    }, headers=CORS_HEADERS)


async def api_customer_create_order(request):
    """Mijoz Web App orqali buyurtma (katalogdan yoki erkin) yuboradi."""
    data = await request.json()
    telegram_id = data.get("telegram_id")
    order_type = data.get("order_type", "custom")
    details = data.get("details", "")
    if not telegram_id or not details:
        return web.json_response({"ok": False, "error": "Ma'lumot yetarli emas"}, status=400, headers=CORS_HEADERS)

    conn = get_db()
    customer = conn.execute("SELECT * FROM customers WHERE telegram_id=?", (telegram_id,)).fetchone()
    conn.close()
    if not customer:
        return web.json_response({"ok": False, "error": "Mijoz topilmadi"}, headers=CORS_HEADERS)

    order_id = create_order(customer["id"], order_type, details)
    await notify_admin_new_order(order_id, customer, details)
    return web.json_response({"ok": True, "order_id": order_id}, headers=CORS_HEADERS)


async def api_admin_orders(request):
    admin_id = request.query.get("admin_id")
    if not is_admin(admin_id):
        return web.json_response({"ok": False, "error": "Ruxsat yo'q"}, status=403, headers=CORS_HEADERS)

    conn = get_db()
    orders = conn.execute("SELECT * FROM orders ORDER BY id DESC LIMIT 200").fetchall()
    result = []
    for o in orders:
        customer = conn.execute(
            "SELECT * FROM customers WHERE id=?", (o["customer_id"],)
        ).fetchone()
        item = dict(o)
        item["customer_code"] = customer["code"] if customer else "—"
        item["customer_phone"] = customer["phone"] if customer else "—"
        item["customer_name"] = customer["full_name"] if customer else "—"
        result.append(item)
    conn.close()
    return web.json_response({"ok": True, "orders": result}, headers=CORS_HEADERS)


async def api_admin_order_reply(request):
    data = await request.json()
    if not is_admin(data.get("admin_id")):
        return web.json_response({"ok": False, "error": "Ruxsat yo'q"}, status=403, headers=CORS_HEADERS)

    order_id = data.get("order_id")
    reply_text = data.get("reply_text", "").strip()
    if not order_id or not reply_text:
        return web.json_response({"ok": False, "error": "Ma'lumot yetarli emas"}, status=400, headers=CORS_HEADERS)

    conn = get_db()
    order = conn.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
    if not order:
        conn.close()
        return web.json_response({"ok": False, "error": "Buyurtma topilmadi"}, headers=CORS_HEADERS)

    customer = conn.execute("SELECT * FROM customers WHERE id=?", (order["customer_id"],)).fetchone()
    conn.execute(
        "UPDATE orders SET status='answered', admin_reply=?, answered_at=? WHERE id=?",
        (reply_text, now_str(), order_id),
    )
    conn.commit()
    conn.close()

    await bot.send_message(
        customer["telegram_id"],
        f"💬 Sotuvchi javobi:\n\n{reply_text}\n\n📍 Manzil: {SHOP_ADDRESS}",
    )
    if SHOP_LAT and SHOP_LON:
        try:
            await bot.send_location(customer["telegram_id"], latitude=float(SHOP_LAT), longitude=float(SHOP_LON))
        except (TypeError, ValueError):
            pass

    return web.json_response({"ok": True}, headers=CORS_HEADERS)


async def api_admin_order_confirm(request):
    data = await request.json()
    if not is_admin(data.get("admin_id")):
        return web.json_response({"ok": False, "error": "Ruxsat yo'q"}, status=403, headers=CORS_HEADERS)

    order_id = data.get("order_id")
    conn = get_db()
    order = conn.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
    if not order:
        conn.close()
        return web.json_response({"ok": False, "error": "Buyurtma topilmadi"}, headers=CORS_HEADERS)

    customer = conn.execute("SELECT * FROM customers WHERE id=?", (order["customer_id"],)).fetchone()

    conn.execute("UPDATE orders SET status='confirmed', confirmed_at=? WHERE id=?", (now_str(), order_id))
    conn.execute("UPDATE customers SET spin_available=1 WHERE id=?", (customer["id"],))
    conn.execute(
        "UPDATE referrals SET status='paid', paid_at=? WHERE referred_code=? AND status='pending'",
        (now_str(), customer["code"]),
    )
    conn.commit()
    conn.close()

    await bot.send_message(
        customer["telegram_id"],
        "🎉 Xaridingiz tasdiqlandi! Rahmat.\n🎁 Endi yana sovg'a olish huquqingiz ochildi.",
    )
    return web.json_response({"ok": True}, headers=CORS_HEADERS)


# ============================================================
# WEBHOOK VA ILOVANI ISHGA TUSHIRISH
# ============================================================

async def on_startup(app: web.Application):
    init_db()
    await bot.set_webhook(f"{WEBHOOK_HOST}{WEBHOOK_PATH}")


async def health_check(request):
    return web.json_response({"status": "ok"})


def main():
    app = web.Application()
    app.router.add_get("/", health_check)

    routes = [
        ("POST", "/api/admin/search-customer", api_search_customer),
        ("POST", "/api/admin/confirm-prize", api_confirm_prize),
        ("POST", "/api/admin/confirm-referral", api_confirm_referral),
        ("GET", "/api/admin/prizes", api_get_prizes),
        ("POST", "/api/admin/prizes", api_update_prizes),
        ("GET", "/api/admin/stats", api_stats),
        ("GET", "/api/admin/customers", api_list_customers),
        ("GET", "/api/customer/info", api_customer_info),
        ("POST", "/api/customer/create-order", api_customer_create_order),
        ("GET", "/api/admin/orders", api_admin_orders),
        ("POST", "/api/admin/order-reply", api_admin_order_reply),
        ("POST", "/api/admin/order-confirm", api_admin_order_confirm),
    ]
    for method, path, handler in routes:
        app.router.add_route(method, path, handler)

    unique_paths = {path for _, path, _ in routes}
    for path in unique_paths:
        app.router.add_route("OPTIONS", path, options_handler)

    SimpleRequestHandler(dispatcher=dp, bot=bot).register(app, path=WEBHOOK_PATH)
    setup_application(app, dp, bot=bot)
    app.on_startup.append(on_startup)
    web.run_app(app, host="0.0.0.0", port=PORT)


if __name__ == "__main__":
    main()
