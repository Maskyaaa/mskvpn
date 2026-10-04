import asyncio
import logging
import os
import random
import re
import sqlite3
from datetime import datetime

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from aiogram.client.default import DefaultBotProperties


# ============================================================
# XUSEME — CONFIG
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_IDS = {
    int(x.strip())
    for x in os.getenv("ADMIN_IDS", "").split(",")
    if x.strip().isdigit()
}

DB_PATH = os.getenv("DB_PATH", "xuseme.db")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not configured")


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)

bot = Bot(
    token=BOT_TOKEN,
    default=DefaultBotProperties(parse_mode="HTML"),
)

dp = Dispatcher()


# ============================================================
# DATABASE
# ============================================================

def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = db()

    conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            first_seen TEXT NOT NULL,
            last_seen TEXT NOT NULL,
            checks_count INTEGER DEFAULT 0,
            generations_count INTEGER DEFAULT 0
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS favorites (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            username TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(user_id, username)
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS watchlist (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            username TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(user_id, username)
        )
    """)

    conn.commit()
    conn.close()


def ensure_user(user_id: int, username: str | None):
    now = datetime.utcnow().isoformat()

    conn = db()

    existing = conn.execute(
        "SELECT user_id FROM users WHERE user_id = ?",
        (user_id,),
    ).fetchone()

    if existing:
        conn.execute(
            """
            UPDATE users
            SET username = ?, last_seen = ?
            WHERE user_id = ?
            """,
            (username or "", now, user_id),
        )
    else:
        conn.execute(
            """
            INSERT INTO users
            (user_id, username, first_seen, last_seen)
            VALUES (?, ?, ?, ?)
            """,
            (user_id, username or "", now, now),
        )

    conn.commit()
    conn.close()


def increment_check(user_id: int):
    conn = db()
    conn.execute(
        """
        UPDATE users
        SET checks_count = checks_count + 1
        WHERE user_id = ?
        """,
        (user_id,),
    )
    conn.commit()
    conn.close()


def increment_generation(user_id: int):
    conn = db()
    conn.execute(
        """
        UPDATE users
        SET generations_count = generations_count + 1
        WHERE user_id = ?
        """,
        (user_id,),
    )
    conn.commit()
    conn.close()


def add_favorite(user_id: int, username: str) -> bool:
    conn = db()

    try:
        conn.execute(
            """
            INSERT INTO favorites
            (user_id, username, created_at)
            VALUES (?, ?, ?)
            """,
            (user_id, username, datetime.utcnow().isoformat()),
        )
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False
    finally:
        conn.close()


def remove_favorite(user_id: int, username: str):
    conn = db()
    conn.execute(
        """
        DELETE FROM favorites
        WHERE user_id = ? AND username = ?
        """,
        (user_id, username),
    )
    conn.commit()
    conn.close()


def get_favorites(user_id: int):
    conn = db()
    rows = conn.execute(
        """
        SELECT username
        FROM favorites
        WHERE user_id = ?
        ORDER BY id DESC
        """,
        (user_id,),
    ).fetchall()
    conn.close()

    return [row["username"] for row in rows]


def add_watch(user_id: int, username: str) -> bool:
    conn = db()

    try:
        conn.execute(
            """
            INSERT INTO watchlist
            (user_id, username, created_at)
            VALUES (?, ?, ?)
            """,
            (user_id, username, datetime.utcnow().isoformat()),
        )
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False
    finally:
        conn.close()


def remove_watch(user_id: int, username: str):
    conn = db()
    conn.execute(
        """
        DELETE FROM watchlist
        WHERE user_id = ? AND username = ?
        """,
        (user_id, username),
    )
    conn.commit()
    conn.close()


def get_watchlist(user_id: int):
    conn = db()
    rows = conn.execute(
        """
        SELECT username
        FROM watchlist
        WHERE user_id = ?
        ORDER BY id DESC
        """,
        (user_id,),
    ).fetchall()
    conn.close()

    return [row["username"] for row in rows]


# ============================================================
# USERNAME ENGINE
# ============================================================

USERNAME_RE = re.compile(r"^[a-zA-Z0-9_]{5,32}$")


def normalize_username(value: str) -> str:
    value = value.strip()

    if value.startswith("@"):
        value = value[1:]

    return value.lower()


def username_format_is_valid(username: str) -> bool:
    return bool(USERNAME_RE.fullmatch(username))


def calculate_score(username: str) -> float:
    """
    Предварительный XUSEME Score.

    Это НЕ рыночная стоимость и НЕ гарантия популярности.
    Реальный availability/status будет подключён отдельно.
    """

    score = 10.0
    length = len(username)

    if length < 5:
        return 0.0

    if length >= 15:
        score -= 2.0
    elif length >= 12:
        score -= 1.0

    if "_" in username:
        score -= 1.0

    digit_count = sum(char.isdigit() for char in username)

    if digit_count:
        score -= min(digit_count * 0.4, 2.0)

    if re.search(r"(.)\1\1", username.lower()):
        score -= 1.0

    if len(set(username.lower())) <= max(2, length // 3):
        score -= 1.5

    if username.isalpha():
        score += 0.5

    if username.lower() == username:
        score += 0.2

    return max(0.0, min(10.0, round(score, 1)))


def score_label(score: float) -> str:
    if score >= 9:
        return "Exceptional"
    if score >= 8:
        return "Excellent"
    if score >= 7:
        return "Strong"
    if score >= 5:
        return "Good"
    if score >= 3:
        return "Average"

    return "Weak"


# ============================================================
# GENERATOR
# ============================================================

GENERATOR_PREFIXES = [
    "neo",
    "x",
    "use",
    "meta",
    "real",
    "get",
    "the",
    "my",
    "its",
    "hey",
]

GENERATOR_WORDS = [
    "nova",
    "zone",
    "core",
    "wave",
    "void",
    "mode",
    "base",
    "flux",
    "byte",
    "mint",
    "sync",
    "peak",
    "labs",
    "hub",
]


def generate_usernames(count: int = 8):
    result = set()

    while len(result) < count:
        style = random.randint(1, 4)

        if style == 1:
            name = random.choice(GENERATOR_PREFIXES) + random.choice(GENERATOR_WORDS)

        elif style == 2:
            name = random.choice(GENERATOR_WORDS) + random.choice(
                GENERATOR_WORDS
            )

        elif style == 3:
            name = random.choice(GENERATOR_PREFIXES) + str(
                random.randint(10, 999)
            )

        else:
            name = (
                random.choice(GENERATOR_WORDS)
                + "_"
                + random.choice(GENERATOR_WORDS)
            )

        name = normalize_username(name)

        if 5 <= len(name) <= 32:
            result.add(name)

    return list(result)


# ============================================================
# KEYBOARDS
# ============================================================

def main_menu():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🔎 Проверить юзернейм",
                    callback_data="check",
                )
            ],
            [
                InlineKeyboardButton(
                    text="✨ Найти свободный",
                    callback_data="find",
                ),
                InlineKeyboardButton(
                    text="⚡ Генератор",
                    callback_data="generate",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="👁 Watchlist",
                    callback_data="watchlist",
                ),
                InlineKeyboardButton(
                    text="⭐ Избранное",
                    callback_data="favorites",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="💎 XUSEME PRO",
                    callback_data="pro",
                )
            ],
        ]
    )


def back_menu():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="← Главное меню",
                    callback_data="menu",
                )
            ]
        ]
    )


def username_actions(username: str):
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="⭐ В избранное",
                    callback_data=f"fav:{username}",
                )
            ],
            [
                InlineKeyboardButton(
                    text="👁 Следить",
                    callback_data=f"watch:{username}",
                )
            ],
            [
                InlineKeyboardButton(
                    text="🔄 Проверить ещё раз",
                    callback_data=f"recheck:{username}",
                )
            ],
            [
                InlineKeyboardButton(
                    text="← Главное меню",
                    callback_data="menu",
                )
            ],
        ]
    )


# ============================================================
# TEXT
# ============================================================

WELCOME_TEXT = (
    "<b>XUSEME</b>\n\n"
    "Твой инструмент для поиска сильных Telegram-юзернеймов.\n\n"
    "Проверяй, оценивай, находи и отслеживай имена."
)

CHECK_TEXT = (
    "<b>🔎 Проверка юзернейма</b>\n\n"
    "Отправь мне юзернейм в формате:\n"
    "<code>@username</code>\n\n"
    "Я проверю формат и покажу XUSEME Score.\n\n"
    "Статус доступности подключим следующим модулем."
)


# ============================================================
# /START
# ============================================================

@dp.message(CommandStart())
async def cmd_start(message: Message):
    user = message.from_user

    ensure_user(
        user.id,
        user.username,
    )

    await message.answer(
        WELCOME_TEXT,
        reply_markup=main_menu(),
    )


# ============================================================
# CHECK
# ============================================================

@dp.callback_query(F.data == "check")
async def callback_check(callback: CallbackQuery):
    await callback.answer()

    await callback.message.edit_text(
        CHECK_TEXT,
        reply_markup=back_menu(),
    )


@dp.message()
async def text_handler(message: Message):
    user = message.from_user

    ensure_user(
        user.id,
        user.username,
    )

    text = (message.text or "").strip()

    if not text:
        return

    if text.startswith("/"):
        return

    username = normalize_username(text)

    if not username_format_is_valid(username):
        await message.answer(
            "<b>Некорректный юзернейм</b>\n\n"
            "Используй только латинские буквы, цифры и `_`.\n"
            "Длина: от 5 до 32 символов.\n\n"
            "Пример: <code>@username</code>",
            reply_markup=back_menu(),
        )
        return

    increment_check(user.id)

    score = calculate_score(username)
    label = score_label(score)

    await message.answer(
        f"<b>@{username}</b>\n\n"
        f"⭐ XUSEME Score: <b>{score}/10</b>\n"
        f"🏷 {label}\n\n"
        "🟡 <b>Статус:</b> проверка доступности ещё не подключена.\n\n"
        "Я не буду выдавать тебе ложный статус «свободен» "
        "по одной ссылке t.me — настоящий checker подключим следующим этапом.",
        reply_markup=username_actions(username),
    )


# ============================================================
# FAVORITES
# ============================================================

@dp.callback_query(F.data.startswith("fav:"))
async def callback_favorite(callback: CallbackQuery):
    username = callback.data.split(":", 1)[1]
    user_id = callback.from_user.id

    added = add_favorite(user_id, username)

    if added:
        await callback.answer("⭐ Добавлено в избранное")
    else:
        remove_favorite(user_id, username)
        await callback.answer("Убрано из избранного")


@dp.callback_query(F.data == "favorites")
async def callback_favorites(callback: CallbackQuery):
    await callback.answer()

    favorites = get_favorites(callback.from_user.id)

    if not favorites:
        text = (
            "<b>⭐ Избранное</b>\n\n"
            "Здесь пока пусто.\n\n"
            "Проверяй юзернеймы и добавляй понравившиеся."
        )
    else:
        lines = ["<b>⭐ Избранное</b>\n"]

        for username in favorites[:30]:
            lines.append(f"• <code>@{username}</code>")

        text = "\n".join(lines)

    await callback.message.edit_text(
        text,
        reply_markup=back_menu(),
    )


# ============================================================
# WATCHLIST
# ============================================================

@dp.callback_query(F.data.startswith("watch:"))
async def callback_watch(callback: CallbackQuery):
    username = callback.data.split(":", 1)[1]
    user_id = callback.from_user.id

    added = add_watch(user_id, username)

    if added:
        await callback.answer(
            "👁 Добавлено в Watchlist"
        )
    else:
        await callback.answer(
            "Этот юзернейм уже отслеживается."
        )


@dp.callback_query(F.data == "watchlist")
async def callback_watchlist(callback: CallbackQuery):
    await callback.answer()

    watches = get_watchlist(callback.from_user.id)

    if not watches:
        text = (
            "<b>👁 Watchlist</b>\n\n"
            "Ты пока ничего не отслеживаешь.\n\n"
            "Добавляй юзернеймы после проверки."
        )
    else:
        lines = ["<b>👁 Watchlist</b>\n"]

        for username in watches[:30]:
            lines.append(f"• <code>@{username}</code>")

        text = "\n".join(lines)

    await callback.message.edit_text(
        text,
        reply_markup=back_menu(),
    )


# ============================================================
# GENERATOR
# ============================================================

@dp.callback_query(F.data == "generate")
async def callback_generate(callback: CallbackQuery):
    user_id = callback.from_user.id

    increment_generation(user_id)

    names = generate_usernames(8)

    lines = [
        "<b>⚡ XUSEME Generator</b>\n",
        "Вот несколько вариантов:\n",
    ]

    for name in names:
        score = calculate_score(name)
        lines.append(
            f"<code>@{name}</code>  ·  {score}/10"
        )

    lines.append(
        "\n<i>Важно: генератор пока не подтверждает "
        "доступность имён.</i>"
    )

    await callback.answer()

    await callback.message.edit_text(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="⚡ Ещё варианты",
                        callback_data="generate",
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="← Главное меню",
                        callback_data="menu",
                    )
                ],
            ]
        ),
    )


# ============================================================
# FIND
# ============================================================

@dp.callback_query(F.data == "find")
async def callback_find(callback: CallbackQuery):
    await callback.answer()

    await callback.message.edit_text(
        "<b>✨ Найти свободный</b>\n\n"
        "Этот раздел будет искать реальные доступные "
        "Telegram-юзернеймы по заданным параметрам.\n\n"
        "Сейчас мы подключаем настоящий Telegram checker, "
        "поэтому я не буду показывать тебе фальшивые результаты.",
        reply_markup=back_menu(),
    )


# ============================================================
# PRO
# ============================================================

@dp.callback_query(F.data == "pro")
async def callback_pro(callback: CallbackQuery):
    await callback.answer()

    await callback.message.edit_text(
        "<b>💎 XUSEME PRO</b>\n\n"
        "В PRO планируем:\n\n"
        "• больше проверок\n"
        "• массовую проверку\n"
        "• расширенный поиск\n"
        "• больше Watchlist\n"
        "• фильтры генератора\n"
        "• поиск редких имён\n"
        "• расширенный XUSEME Score\n\n"
        "<i>Тарифы подключим после готовности основного "
        "движка XUSEME.</i>",
        reply_markup=back_menu(),
    )


# ============================================================
# RECHECK
# ============================================================

@dp.callback_query(F.data.startswith("recheck:"))
async def callback_recheck(callback: CallbackQuery):
    username = callback.data.split(":", 1)[1]

    score = calculate_score(username)
    label = score_label(score)

    await callback.answer()

    await callback.message.edit_text(
        f"<b>@{username}</b>\n\n"
        f"⭐ XUSEME Score: <b>{score}/10</b>\n"
        f"🏷 {label}\n\n"
        "🟡 <b>Статус:</b> настоящий Telegram checker "
        "будет подключён следующим этапом.",
        reply_markup=username_actions(username),
    )


# ============================================================
# MAIN MENU
# ============================================================

@dp.callback_query(F.data == "menu")
async def callback_menu(callback: CallbackQuery):
    await callback.answer()

    await callback.message.edit_text(
        WELCOME_TEXT,
        reply_markup=main_menu(),
    )


# ============================================================
# STARTUP
# ============================================================

async def main():
    init_db()

    me = await bot.get_me()

    logging.info(
        "XUSEME started as @%s",
        me.username,
    )

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())