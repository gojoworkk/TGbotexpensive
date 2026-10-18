"""Простой Telegram-бот для учёта расходов.

Пишешь «кофе 300» → выбираешь категорию кнопкой → расход сохранён.
Запуск: python bot.py (токен в .env или переменной окружения BOT_TOKEN).
"""

import asyncio
import logging
import os
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
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
    kb.adjust(2)
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
    "/stats — статистика\n/categories — категории"
)


# ---------- обработчики ----------

router = Router()

# Неподтверждённые расходы: id сообщения с кнопками → (название, сумма)
pending: dict[int, tuple[str, float]] = {}


class NewCategory(StatesGroup):
    name = State()


@router.message(CommandStart())
@router.message(Command("help"))
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    get_categories(message.from_user.id)
    await message.answer(HELP_TEXT, reply_markup=menu_kb())


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
    await state.set_state(NewCategory.name)
    await state.update_data(expense_msg_id=call.message.message_id)
    await call.message.answer("Напиши название новой категории, можно с эмодзи. Например: <b>🎮 Игры</b>")
    await call.answer()


@router.callback_query(F.data == "addcat")
async def on_add_category(call: CallbackQuery, state: FSMContext):
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


# ---------- запуск ----------

async def main():
    from aiogram.client.default import DefaultBotProperties

    logging.basicConfig(level=logging.INFO)
    bot = Bot(load_token(), default=DefaultBotProperties(parse_mode="HTML"))
    dp = Dispatcher()
    dp.include_router(router)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
