"""Простой Telegram-бот для учёта расходов.

Пишешь «кофе 300» → выбираешь категорию кнопкой → расход сохранён.
Запуск: python bot.py (токен в .env или переменной окружения BOT_TOKEN).
"""

import asyncio
import csv
import io
import logging
import os
import sqlite3
import time
from datetime import datetime, timedelta
from pathlib import Path

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    LabeledPrice,
    Message,
    PreCheckoutQuery,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

BASE_DIR = Path(__file__).parent
DB_PATH = BASE_DIR / "expenses.db"

DEFAULT_CATEGORIES = [
    "🍔 Еда",
    "🚕 Такси",
    "🛒 Продукты",
    "🏠 Жильё",
    "🚌 Транспорт",
    "💊 Здоровье",
    "🎉 Развлечения",
    "👕 Одежда",
    "📱 Связь и подписки",
    "📦 Другое",
]

# Премиум: подписка в Telegram Stars
PREMIUM_PRICE_STARS = 1          # цена за 30 дней, в звёздах
PREMIUM_PERIOD = 30 * 24 * 3600  # Telegram поддерживает только 30 дней
FREE_CUSTOM_CATEGORIES = 3       # своих категорий без премиума (сверх базовых)

PERIODS = {
    "today": "сегодня",
    "week": "7 дней",
    "month": "этот месяц",
    "all": "всё время",
}


# ---------- настройки ----------

def load_token() -> str:
    env_file = BASE_DIR / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            key, _, value = line.partition("=")
            if key.strip() and value and key.strip() not in os.environ:
                os.environ[key.strip()] = value.strip().strip('"').strip("'")
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise SystemExit("Нет токена: создай файл .env со строкой BOT_TOKEN=... (см. .env.example)")
    return token


# ---------- база данных ----------

db = sqlite3.connect(DB_PATH)
db.row_factory = sqlite3.Row
db.executescript(
    """
    CREATE TABLE IF NOT EXISTS categories (
        id      INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        name    TEXT NOT NULL,
        UNIQUE (user_id, name)
    );
    CREATE TABLE IF NOT EXISTS expenses (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id    INTEGER NOT NULL,
        category   TEXT NOT NULL,
        amount     REAL NOT NULL,
        comment    TEXT,
        created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS users (
        user_id       INTEGER PRIMARY KEY,
        premium_until INTEGER NOT NULL DEFAULT 0  -- unix time
    );
    CREATE TABLE IF NOT EXISTS recurring (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id    INTEGER NOT NULL,
        category   TEXT NOT NULL,
        amount     REAL NOT NULL,
        comment    TEXT,
        day        INTEGER NOT NULL,          -- день месяца, 1–28
        last_month TEXT NOT NULL DEFAULT ''   -- 'YYYY-MM', когда последний раз записали
    );
    """
)
db.commit()


def get_categories(user_id: int) -> list[sqlite3.Row]:
    rows = db.execute(
        "SELECT id, name FROM categories WHERE user_id = ? ORDER BY id", (user_id,)
    ).fetchall()
    if not rows:
        db.executemany(
            "INSERT INTO categories (user_id, name) VALUES (?, ?)",
            [(user_id, name) for name in DEFAULT_CATEGORIES],
        )
        db.commit()
        return get_categories(user_id)
    return rows


def add_category(user_id: int, name: str) -> int:
    db.execute("INSERT OR IGNORE INTO categories (user_id, name) VALUES (?, ?)", (user_id, name))
    db.commit()
    return db.execute(
        "SELECT id FROM categories WHERE user_id = ? AND name = ?", (user_id, name)
    ).fetchone()["id"]


def get_category(user_id: int, cat_id: int) -> sqlite3.Row | None:
    return db.execute(
        "SELECT id, name FROM categories WHERE user_id = ? AND id = ?", (user_id, cat_id)
    ).fetchone()


def delete_category(user_id: int, cat_id: int) -> None:
    db.execute("DELETE FROM categories WHERE user_id = ? AND id = ?", (user_id, cat_id))
    db.commit()


def custom_categories_count(user_id: int) -> int:
    names = [c["name"] for c in get_categories(user_id)]
    return sum(1 for name in names if name not in DEFAULT_CATEGORIES)


def can_add_category(user_id: int) -> bool:
    return is_premium(user_id) or custom_categories_count(user_id) < FREE_CUSTOM_CATEGORIES


# ---------- премиум ----------

def premium_until(user_id: int) -> int:
    row = db.execute("SELECT premium_until FROM users WHERE user_id = ?", (user_id,)).fetchone()
    return row["premium_until"] if row else 0


def is_premium(user_id: int) -> bool:
    return premium_until(user_id) > time.time()


def set_premium_until(user_id: int, until: int) -> None:
    db.execute(
        """INSERT INTO users (user_id, premium_until) VALUES (?, ?)
           ON CONFLICT (user_id) DO UPDATE SET premium_until = excluded.premium_until""",
        (user_id, until),
    )
    db.commit()


# ---------- регулярные расходы ----------

def add_recurring(user_id: int, category: str, amount: float, comment: str, day: int) -> None:
    today = datetime.now()
    # Если день в этом месяце уже прошёл — начнём со следующего месяца
    last_month = today.strftime("%Y-%m") if day < today.day else ""
    db.execute(
        """INSERT INTO recurring (user_id, category, amount, comment, day, last_month)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (user_id, category, amount, comment, day, last_month),
    )
    db.commit()


def get_recurring(user_id: int) -> list[sqlite3.Row]:
    return db.execute(
        "SELECT * FROM recurring WHERE user_id = ? ORDER BY day", (user_id,)
    ).fetchall()


def delete_recurring(user_id: int, rec_id: int) -> None:
    db.execute("DELETE FROM recurring WHERE user_id = ? AND id = ?", (user_id, rec_id))
    db.commit()


def apply_due_recurring() -> list[tuple[int, sqlite3.Row]]:
    """Записывает регулярные расходы, у которых наступил день. Только для премиума."""
    today = datetime.now()
    month = today.strftime("%Y-%m")
    due = db.execute(
        """SELECT r.* FROM recurring r JOIN users u ON u.user_id = r.user_id
           WHERE r.day <= ? AND r.last_month != ? AND u.premium_until > ?""",
        (today.day, month, int(time.time())),
    ).fetchall()
    done = []
    for r in due:
        expense_id = add_expense(r["user_id"], r["category"], r["amount"], r["comment"])
        db.execute("UPDATE recurring SET last_month = ? WHERE id = ?", (month, r["id"]))
        done.append((expense_id, r))
    db.commit()
    return done


def export_csv(user_id: int) -> bytes:
    rows = db.execute(
        "SELECT created_at, category, amount, comment FROM expenses WHERE user_id = ? ORDER BY id",
        (user_id,),
    ).fetchall()
    buf = io.StringIO()
    writer = csv.writer(buf, delimiter=";")  # «;» — чтобы Excel сразу разложил по колонкам
    writer.writerow(["Дата", "Категория", "Сумма", "Комментарий"])
    for r in rows:
        writer.writerow([r["created_at"], r["category"], f"{r['amount']:g}".replace(".", ","), r["comment"]])
    return buf.getvalue().encode("utf-8-sig")  # BOM — чтобы Excel не ломал кириллицу


def add_expense(user_id: int, category: str, amount: float, comment: str) -> int:
    cur = db.execute(
        "INSERT INTO expenses (user_id, category, amount, comment, created_at) VALUES (?, ?, ?, ?, ?)",
        (user_id, category, amount, comment, datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
    )
    db.commit()
    return cur.lastrowid


def delete_expense(user_id: int, expense_id: int) -> bool:
    cur = db.execute("DELETE FROM expenses WHERE user_id = ? AND id = ?", (user_id, expense_id))
    db.commit()
    return cur.rowcount > 0


def period_start(period: str) -> str:
    now = datetime.now()
    if period == "today":
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    elif period == "week":
        start = (now - timedelta(days=6)).replace(hour=0, minute=0, second=0, microsecond=0)
    elif period == "month":
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    else:
        return "0000"
    return start.strftime("%Y-%m-%d %H:%M:%S")


def stats_text(user_id: int, period: str) -> str:
    since = period_start(period)
    rows = db.execute(
        """SELECT category, SUM(amount) AS total, COUNT(*) AS cnt
           FROM expenses WHERE user_id = ? AND created_at >= ?
           GROUP BY category ORDER BY total DESC""",
        (user_id, since),
    ).fetchall()
    if not rows:
        return f"📊 За {PERIODS[period]} расходов нет."

    total = sum(r["total"] for r in rows)
    lines = [f"📊 <b>Расходы за {PERIODS[period]}</b>", f"Всего: <b>{money(total)}</b>", ""]
    for r in rows:
        percent = r["total"] / total * 100
        lines.append(f"{r['category']} — {money(r['total'])} ({percent:.0f}%, {r['cnt']} шт.)")

    last = db.execute(
        """SELECT category, amount, comment, created_at FROM expenses
           WHERE user_id = ? AND created_at >= ? ORDER BY id DESC LIMIT 5""",
        (user_id, since),
    ).fetchall()
    lines += ["", "<b>Последние:</b>"]
    for r in last:
        date = datetime.strptime(r["created_at"], "%Y-%m-%d %H:%M:%S").strftime("%d.%m %H:%M")
        lines.append(f"{date} · {r['category']} · {money(r['amount'])} · {r['comment']}")
    return "\n".join(lines)


# ---------- помощники ----------

def money(value: float) -> str:
    text = f"{value:,.0f}" if value == int(value) else f"{value:,.2f}"
    return text.replace(",", " ") + " ₽"


def parse_expense(text: str) -> tuple[str, float] | None:
    """«кофе 300» → ("кофе", 300.0). Последнее слово — сумма."""
    parts = text.strip().rsplit(maxsplit=1)
    if not parts:
        return None
    raw_amount = parts[-1].replace(",", ".")
    try:
        amount = float(raw_amount)
    except ValueError:
        return None
    if amount <= 0:
        return None
    name = parts[0] if len(parts) == 2 else ""
    return name, amount


def categories_kb(user_id: int):
    kb = InlineKeyboardBuilder()
    for cat in get_categories(user_id):
        kb.button(text=cat["name"], callback_data=f"cat:{cat['id']}")
    kb.button(text="➕ Своя категория", callback_data="newcat")
    kb.button(text="✖️ Отмена", callback_data="cancel")
    kb.adjust(2)
    return kb.as_markup()


def menu_kb():
    kb = InlineKeyboardBuilder()
    kb.button(text="📊 Статистика", callback_data="stats:month")
    kb.button(text="🗂 Категории", callback_data="cats")
    kb.button(text="🔁 Регулярные", callback_data="rec")
    kb.button(text="📤 Экспорт", callback_data="export")
    kb.button(text="⭐ Премиум", callback_data="premium")
    kb.adjust(2, 2, 1)
    return kb.as_markup()


def premium_offer_kb(invoice_link: str):
    kb = InlineKeyboardBuilder()
    kb.button(text=f"⭐ Оформить за {PREMIUM_PRICE_STARS} ⭐ / мес", url=invoice_link)
    kb.button(text="⬅️ Меню", callback_data="home")
    kb.adjust(1)
    return kb.as_markup()


def recurring_kb(user_id: int):
    kb = InlineKeyboardBuilder()
    for r in get_recurring(user_id):
        kb.button(
            text=f"🗑 {r['day']}-го · {r['comment']} · {money(r['amount'])}",
            callback_data=f"delrec:{r['id']}",
        )
    kb.button(text="➕ Добавить", callback_data="addrec")
    kb.button(text="⬅️ Меню", callback_data="menu")
    kb.adjust(1)
    return kb.as_markup()


def rec_categories_kb(user_id: int):
    kb = InlineKeyboardBuilder()
    for cat in get_categories(user_id):
        kb.button(text=cat["name"], callback_data=f"rcat:{cat['id']}")
    kb.button(text="✖️ Отмена", callback_data="home")
    kb.adjust(2)
    return kb.as_markup()


def days_kb():
    kb = InlineKeyboardBuilder()
    for day in range(1, 29):
        kb.button(text=str(day), callback_data=f"rday:{day}")
    kb.button(text="✖️ Отмена", callback_data="home")
    kb.adjust(7)
    return kb.as_markup()


def stats_kb(current: str):
    kb = InlineKeyboardBuilder()
    for key, title in PERIODS.items():
        kb.button(text=("• " if key == current else "") + title.capitalize(), callback_data=f"stats:{key}")
    kb.button(text="⬅️ Меню", callback_data="menu")
    kb.adjust(4, 1)
    return kb.as_markup()


def manage_cats_kb(user_id: int):
    kb = InlineKeyboardBuilder()
    for cat in get_categories(user_id):
        kb.button(text=f"🗑 {cat['name']}", callback_data=f"delcat:{cat['id']}")
    kb.button(text="➕ Добавить", callback_data="addcat")
    kb.button(text="⬅️ Меню", callback_data="menu")
    kb.adjust(2)
    return kb.as_markup()


HELP_TEXT = (
    "👋 Я считаю твои расходы.\n\n"
    "Просто напиши: <b>кофе 300</b> или <b>такси 1000</b> — "
    "в конце сумма, перед ней название. Потом выбери категорию кнопкой.\n\n"
    "/stats — статистика\n/categories — категории\n"
    "/recurring — регулярные расходы ⭐\n/export — выгрузка в Excel ⭐\n/premium — премиум"
)

PREMIUM_TEXT = (
    "⭐ <b>Премиум</b> — {price} ⭐ в месяц\n\n"
    "• Сколько угодно своих категорий (без премиума — {free})\n"
    "• 🔁 Регулярные расходы: бот сам записывает их каждый месяц\n"
    "• 📤 Выгрузка всех расходов в Excel (CSV)\n\n"
    "Подписка продлевается каждые 30 дней, отменить можно в настройках Telegram."
)

PAY_SUPPORT_TEXT = (
    "По вопросам оплаты напиши владельцу бота. "
    "Подписку можно отменить в Telegram: Настройки → Мои звёзды."
)


# ---------- обработчики ----------

router = Router()

# Неподтверждённые расходы: id сообщения с кнопками → (название, сумма)
pending: dict[int, tuple[str, float]] = {}


class NewCategory(StatesGroup):
    name = State()


class NewRecurring(StatesGroup):
    text = State()
    details = State()


async def premium_invoice_link(bot: Bot) -> str:
    return await bot.create_invoice_link(
        title="Премиум",
        description="Свои категории без лимита, регулярные расходы, выгрузка в Excel",
        payload="premium",
        currency="XTR",  # Telegram Stars
        prices=[LabeledPrice(label="Премиум на 30 дней", amount=PREMIUM_PRICE_STARS)],
        subscription_period=PREMIUM_PERIOD,
    )


async def send_premium(message: Message, user_id: int, reason: str = ""):
    """Показывает статус премиума или предлагает его оформить."""
    until = premium_until(user_id)
    if until > time.time():
        date = datetime.fromtimestamp(until).strftime("%d.%m.%Y")
        await message.answer(f"⭐ Премиум активен до {date}.", reply_markup=home_kb())
        return
    text = PREMIUM_TEXT.format(price=PREMIUM_PRICE_STARS, free=FREE_CUSTOM_CATEGORIES)
    if reason:
        text = f"🔒 {reason}\n\n{text}"
    link = await premium_invoice_link(message.bot)
    await message.answer(text, reply_markup=premium_offer_kb(link))


@router.message(CommandStart())
@router.message(Command("help"))
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    get_categories(message.from_user.id)
    await message.answer(HELP_TEXT, reply_markup=menu_kb())


@router.message(Command("premium"))
async def cmd_premium(message: Message, state: FSMContext):
    await state.clear()
    await send_premium(message, message.from_user.id)


@router.message(Command("paysupport"))
async def cmd_paysupport(message: Message):
    await message.answer(PAY_SUPPORT_TEXT)


@router.message(Command("export"))
async def cmd_export(message: Message, state: FSMContext):
    await state.clear()
    await send_export(message, message.from_user.id)


@router.message(Command("recurring"))
async def cmd_recurring(message: Message, state: FSMContext):
    await state.clear()
    await send_recurring(message, message.from_user.id)


async def send_export(message: Message, user_id: int):
    if not is_premium(user_id):
        await send_premium(message, user_id, "Выгрузка в Excel доступна в премиуме.")
        return
    filename = f"expenses_{datetime.now():%Y-%m-%d}.csv"
    await message.answer_document(
        BufferedInputFile(export_csv(user_id), filename=filename),
        caption="📤 Все твои расходы. Открывается в Excel или Google Таблицах.",
    )


async def send_recurring(message: Message, user_id: int):
    if not is_premium(user_id):
        await send_premium(message, user_id, "Регулярные расходы доступны в премиуме.")
        return
    await message.answer(
        "🔁 <b>Регулярные расходы</b>\n"
        "Бот сам записывает их каждый месяц в нужный день. Нажми, чтобы удалить:",
        reply_markup=recurring_kb(user_id),
    )


# ---------- оплата ----------

@router.pre_checkout_query()
async def on_pre_checkout(query: PreCheckoutQuery):
    await query.answer(ok=True)


@router.message(F.successful_payment)
async def on_successful_payment(message: Message):
    payment = message.successful_payment
    user_id = message.from_user.id
    if payment.subscription_expiration_date:
        until = payment.subscription_expiration_date
    else:
        until = max(premium_until(user_id), int(time.time())) + PREMIUM_PERIOD
    set_premium_until(user_id, until)
    date = datetime.fromtimestamp(until).strftime("%d.%m.%Y")
    if payment.is_recurring and not payment.is_first_recurring:
        await message.answer(f"⭐ Подписка продлена до {date}. Спасибо!")
    else:
        await message.answer(f"🎉 Премиум активирован до {date}!", reply_markup=menu_kb())


# ---------- регулярные расходы: ввод ----------

@router.message(NewRecurring.text, F.text)
async def new_recurring_text(message: Message, state: FSMContext):
    parsed = parse_expense(message.text)
    if not parsed or not parsed[0]:
        await message.answer("Не понял 🤔 Напиши название и сумму, например: <b>интернет 600</b>")
        return
    name, amount = parsed
    await state.set_state(NewRecurring.details)
    await state.update_data(name=name, amount=amount)
    await message.answer(
        f"🔁 {name} — <b>{money(amount)}</b>\nВыбери категорию:",
        reply_markup=rec_categories_kb(message.from_user.id),
    )


@router.message(Command("stats"))
async def cmd_stats(message: Message):
    await message.answer(stats_text(message.from_user.id, "month"), reply_markup=stats_kb("month"))


@router.message(Command("categories"))
async def cmd_categories(message: Message):
    await message.answer(
        "🗂 Твои категории. Нажми, чтобы удалить:", reply_markup=manage_cats_kb(message.from_user.id)
    )


@router.message(NewCategory.name, F.text)
async def new_category_name(message: Message, state: FSMContext):
    name = message.text.strip()[:40]
    data = await state.get_data()
    await state.clear()
    user_id = message.from_user.id
    if not can_add_category(user_id):
        await send_premium(message, user_id, f"Без премиума можно до {FREE_CUSTOM_CATEGORIES} своих категорий.")
        return
    add_category(user_id, name)

    expense_msg_id = data.get("expense_msg_id")
    expense = pending.pop(expense_msg_id, None) if expense_msg_id else None
    if expense:
        title, amount = expense
        expense_id = add_expense(user_id, name, amount, title or name)
        await message.answer(saved_text(name, amount, title), reply_markup=undo_kb(expense_id))
    else:
        await message.answer(
            f"✅ Категория «{name}» добавлена.", reply_markup=manage_cats_kb(user_id)
        )


@router.message(F.text)
async def on_text(message: Message):
    parsed = parse_expense(message.text)
    if not parsed:
        await message.answer("Не понял 🤔 Напиши так: <b>кофе 300</b>")
        return
    name, amount = parsed
    title = f"{name} — " if name else ""
    sent = await message.answer(
        f"💸 {title}<b>{money(amount)}</b>\nВыбери категорию:",
        reply_markup=categories_kb(message.from_user.id),
    )
    pending[sent.message_id] = (name, amount)


def saved_text(category: str, amount: float, comment: str) -> str:
    note = f"\n💬 {comment}" if comment else ""
    return f"✅ Записал: {category} — <b>{money(amount)}</b>{note}"


def undo_kb(expense_id: int):
    kb = InlineKeyboardBuilder()
    kb.button(text="↩️ Отменить", callback_data=f"undo:{expense_id}")
    kb.button(text="⬅️ Меню", callback_data="home")
    kb.adjust(2)
    return kb.as_markup()


def home_kb():
    kb = InlineKeyboardBuilder()
    kb.button(text="⬅️ Меню", callback_data="home")
    return kb.as_markup()


@router.callback_query(F.data.startswith("cat:"))
async def on_category(call: CallbackQuery):
    expense = pending.pop(call.message.message_id, None)
    if not expense:
        await call.answer("Эта запись уже обработана", show_alert=True)
        return
    cat = get_category(call.from_user.id, int(call.data.split(":")[1]))
    if not cat:
        pending[call.message.message_id] = expense
        await call.answer("Категория не найдена", show_alert=True)
        return
    name, amount = expense
    expense_id = add_expense(call.from_user.id, cat["name"], amount, name or cat["name"])
    await call.message.edit_text(saved_text(cat["name"], amount, name), reply_markup=undo_kb(expense_id))
    await call.answer()


@router.callback_query(F.data == "newcat")
async def on_new_category(call: CallbackQuery, state: FSMContext):
    if not can_add_category(call.from_user.id):
        await call.answer()
        await send_premium(call.message, call.from_user.id, f"Без премиума можно до {FREE_CUSTOM_CATEGORIES} своих категорий.")
        return
    await state.set_state(NewCategory.name)
    await state.update_data(expense_msg_id=call.message.message_id)
    await call.message.answer("Напиши название новой категории, можно с эмодзи. Например: <b>🎮 Игры</b>")
    await call.answer()


@router.callback_query(F.data == "addcat")
async def on_add_category(call: CallbackQuery, state: FSMContext):
    if not can_add_category(call.from_user.id):
        await call.answer()
        await send_premium(call.message, call.from_user.id, f"Без премиума можно до {FREE_CUSTOM_CATEGORIES} своих категорий.")
        return
    await state.set_state(NewCategory.name)
    await state.update_data(expense_msg_id=None)
    await call.message.answer("Напиши название новой категории, можно с эмодзи. Например: <b>🎮 Игры</b>")
    await call.answer()


@router.callback_query(F.data == "cancel")
async def on_cancel(call: CallbackQuery):
    pending.pop(call.message.message_id, None)
    await call.message.edit_text("✖️ Отменено", reply_markup=home_kb())
    await call.answer()


@router.callback_query(F.data.startswith("undo:"))
async def on_undo(call: CallbackQuery):
    if delete_expense(call.from_user.id, int(call.data.split(":")[1])):
        await call.message.edit_text("↩️ Запись удалена", reply_markup=home_kb())
    await call.answer()


@router.callback_query(F.data == "home")
async def on_home(call: CallbackQuery, state: FSMContext):
    """Меню новым сообщением — чтобы запись о расходе осталась в чате."""
    await state.clear()
    await call.message.answer(HELP_TEXT, reply_markup=menu_kb())
    await call.answer()


@router.callback_query(F.data.startswith("stats:"))
async def on_stats(call: CallbackQuery):
    period = call.data.split(":")[1]
    await call.message.edit_text(stats_text(call.from_user.id, period), reply_markup=stats_kb(period))
    await call.answer()


@router.callback_query(F.data == "cats")
async def on_cats(call: CallbackQuery):
    await call.message.edit_text(
        "🗂 Твои категории. Нажми, чтобы удалить:", reply_markup=manage_cats_kb(call.from_user.id)
    )
    await call.answer()


@router.callback_query(F.data.startswith("delcat:"))
async def on_delete_category(call: CallbackQuery):
    delete_category(call.from_user.id, int(call.data.split(":")[1]))
    await call.message.edit_reply_markup(reply_markup=manage_cats_kb(call.from_user.id))
    await call.answer("Удалено")


@router.callback_query(F.data == "menu")
async def on_menu(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.message.edit_text(HELP_TEXT, reply_markup=menu_kb())
    await call.answer()


@router.callback_query(F.data == "premium")
async def on_premium(call: CallbackQuery):
    await call.answer()
    await send_premium(call.message, call.from_user.id)


@router.callback_query(F.data == "export")
async def on_export(call: CallbackQuery):
    await call.answer()
    await send_export(call.message, call.from_user.id)


@router.callback_query(F.data == "rec")
async def on_recurring(call: CallbackQuery):
    await call.answer()
    await send_recurring(call.message, call.from_user.id)


@router.callback_query(F.data == "addrec")
async def on_add_recurring(call: CallbackQuery, state: FSMContext):
    await call.answer()
    if not is_premium(call.from_user.id):
        await send_premium(call.message, call.from_user.id, "Регулярные расходы доступны в премиуме.")
        return
    await state.set_state(NewRecurring.text)
    await call.message.answer("Напиши название и сумму регулярного расхода, например: <b>интернет 600</b>")


@router.callback_query(F.data.startswith("rcat:"))
async def on_recurring_category(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    cat = get_category(call.from_user.id, int(call.data.split(":")[1]))
    if "amount" not in data or not cat:
        await call.answer("Начни заново: 🔁 Регулярные → ➕ Добавить", show_alert=True)
        return
    await state.update_data(category=cat["name"])
    await call.message.edit_text(
        f"🔁 {data['name']} — <b>{money(data['amount'])}</b> · {cat['name']}\n"
        "Какого числа каждого месяца записывать?",
        reply_markup=days_kb(),
    )
    await call.answer()


@router.callback_query(F.data.startswith("rday:"))
async def on_recurring_day(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    if "category" not in data:
        await call.answer("Начни заново: 🔁 Регулярные → ➕ Добавить", show_alert=True)
        return
    day = int(call.data.split(":")[1])
    await state.clear()
    add_recurring(call.from_user.id, data["category"], data["amount"], data["name"], day)
    await call.message.edit_text(
        f"✅ Регулярный расход добавлен: {data['category']} — <b>{money(data['amount'])}</b>, "
        f"каждое {day}-е число.\n💬 {data['name']}",
        reply_markup=recurring_kb(call.from_user.id),
    )
    await call.answer()


@router.callback_query(F.data.startswith("delrec:"))
async def on_delete_recurring(call: CallbackQuery):
    delete_recurring(call.from_user.id, int(call.data.split(":")[1]))
    await call.message.edit_reply_markup(reply_markup=recurring_kb(call.from_user.id))
    await call.answer("Удалено")


# ---------- запуск ----------

async def recurring_loop(bot: Bot):
    """Раз в полчаса записывает регулярные расходы, у которых наступил день."""
    while True:
        for expense_id, r in apply_due_recurring():
            try:
                await bot.send_message(
                    r["user_id"],
                    "🔁 " + saved_text(r["category"], r["amount"], r["comment"]),
                    reply_markup=undo_kb(expense_id),
                )
            except Exception:
                logging.exception("Не удалось уведомить %s", r["user_id"])
        await asyncio.sleep(30 * 60)


async def main():
    from aiogram.client.default import DefaultBotProperties

    logging.basicConfig(level=logging.INFO)
    bot = Bot(load_token(), default=DefaultBotProperties(parse_mode="HTML"))
    dp = Dispatcher()
    dp.include_router(router)
    recurring_task = asyncio.create_task(recurring_loop(bot))  # noqa: F841 — держим ссылку
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
