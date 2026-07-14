import asyncio
import logging
import os
import sqlite3
from datetime import datetime, timedelta

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.client.default import DefaultBotProperties

# ========== НАСТРОЙКИ ==========
BOT_TOKEN = os.getenv("BOT_TOKEN", "ВСТАВЬ_СЮДА_ТОКЕН_ОТ_BOTFATHER")
ADMIN_IDS = [int(x) for x in os.getenv("ADMIN_IDS", "ВСТАВЬ_СВОЙ_TELEGRAM_ID").split(",") if x.strip().isdigit()]
REQUIRED_REFERRALS = int(os.getenv("REQUIRED_REFERRALS", "1"))   # сколько друзей нужно пригласить за 1 ссылку
LINK_DURATION_DAYS = int(os.getenv("LINK_DURATION_DAYS", "2"))  # на сколько дней выдаётся ссылка
DB_PATH = os.getenv("DB_PATH", "vpnbot.db")

logging.basicConfig(level=logging.INFO)

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
dp = Dispatcher()


# ========== БАЗА ДАННЫХ ==========
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
            referred_by INTEGER,
            referral_count INTEGER DEFAULT 0,
            current_link TEXT,
            link_issued_at TEXT,
            joined_at TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS links_pool (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            link_text TEXT NOT NULL,
            added_at TEXT
        )
    """)
    conn.commit()
    conn.close()


def get_user(user_id: int):
    conn = db()
    row = conn.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()
    conn.close()
    return row


def create_user(user_id: int, username: str, referred_by: int | None):
    conn = db()
    conn.execute(
        "INSERT OR IGNORE INTO users (user_id, username, referred_by, joined_at) VALUES (?,?,?,?)",
        (user_id, username, referred_by, datetime.utcnow().isoformat()),
    )
    conn.commit()
    conn.close()


def increment_referral(referrer_id: int) -> int:
    conn = db()
    conn.execute("UPDATE users SET referral_count = referral_count + 1 WHERE user_id=?", (referrer_id,))
    conn.commit()
    row = conn.execute("SELECT referral_count FROM users WHERE user_id=?", (referrer_id,)).fetchone()
    conn.close()
    return row["referral_count"] if row else 0


def pop_link_from_pool() -> str | None:
    conn = db()
    row = conn.execute("SELECT id, link_text FROM links_pool ORDER BY id LIMIT 1").fetchone()
    if not row:
        conn.close()
        return None
    conn.execute("DELETE FROM links_pool WHERE id=?", (row["id"],))
    conn.commit()
    conn.close()
    return row["link_text"]


def add_link_to_pool(link_text: str):
    conn = db()
    conn.execute("INSERT INTO links_pool (link_text, added_at) VALUES (?,?)", (link_text, datetime.utcnow().isoformat()))
    conn.commit()
    conn.close()


def pool_count() -> int:
    conn = db()
    row = conn.execute("SELECT COUNT(*) as c FROM links_pool").fetchone()
    conn.close()
    return row["c"]


def issue_link_to_user(user_id: int, link_text: str):
    conn = db()
    conn.execute(
        "UPDATE users SET current_link=?, link_issued_at=?, referral_count = referral_count - ? WHERE user_id=?",
        (link_text, datetime.utcnow().isoformat(), REQUIRED_REFERRALS, user_id),
    )
    conn.commit()
    conn.close()


def clear_expired_link(user_id: int):
    conn = db()
    conn.execute("UPDATE users SET current_link=NULL, link_issued_at=NULL WHERE user_id=?", (user_id,))
    conn.commit()
    conn.close()


def total_users() -> int:
    conn = db()
    row = conn.execute("SELECT COUNT(*) as c FROM users").fetchone()
    conn.close()
    return row["c"]


# ========== ВСПОМОГАТЕЛЬНОЕ ==========
def link_status(user_row) -> tuple[str | None, timedelta | None]:
    """Возвращает (ссылка, оставшееся_время) если ссылка ещё активна, иначе (None, None)."""
    if not user_row["current_link"] or not user_row["link_issued_at"]:
        return None, None
    issued = datetime.fromisoformat(user_row["link_issued_at"])
    expires = issued + timedelta(days=LINK_DURATION_DAYS)
    now = datetime.utcnow()
    if now >= expires:
        return None, None
    return user_row["current_link"], expires - now


def fmt_timedelta(td: timedelta) -> str:
    hours = td.seconds // 3600
    days = td.days
    return f"{days}д {hours}ч" if days else f"{hours}ч"


async def get_bot_username() -> str:
    me = await bot.get_me()
    return me.username


# ========== ХЕНДЛЕРЫ ==========
@dp.message(CommandStart())
async def cmd_start(message: Message):
    user_id = message.from_user.id
    username = message.from_user.username or message.from_user.full_name

    args = message.text.split(maxsplit=1)
    referred_by = None
    if len(args) > 1 and args[1].startswith("ref"):
        try:
            ref_id = int(args[1].replace("ref", ""))
            if ref_id != user_id and get_user(ref_id) is not None:
                referred_by = ref_id
        except ValueError:
            pass

    is_new = get_user(user_id) is None
    create_user(user_id, username, referred_by)

    if is_new and referred_by:
        new_count = increment_referral(referred_by)
        try:
            await bot.send_message(
                referred_by,
                f"🎉 По твоей ссылке зашёл новый пользователь!\n"
                f"Приглашено: {new_count}/{REQUIRED_REFERRALS}",
            )
        except Exception:
            pass
        await try_auto_issue(referred_by)

    bot_username = await get_bot_username()
    ref_link = f"https://t.me/{bot_username}?start=ref{user_id}"

    await message.answer(
        "👋 Привет! Это бот для получения VPN-ссылок.\n\n"
        f"Чтобы получить доступ, пригласи {REQUIRED_REFERRALS} друга(ей) по своей ссылке:\n"
        f"<code>{ref_link}</code>\n\n"
        "Команды:\n"
        "/mylink — получить ссылку для приглашений\n"
        "/status — проверить статус (сколько друзей приглашено, активна ли VPN-ссылка)",
        disable_web_page_preview=True,
    )


@dp.message(Command("mylink"))
async def cmd_mylink(message: Message):
    user_id = message.from_user.id
    if get_user(user_id) is None:
        create_user(user_id, message.from_user.username or message.from_user.full_name, None)

    bot_username = await get_bot_username()
    ref_link = f"https://t.me/{bot_username}?start=ref{user_id}"
    await message.answer(
        f"🔗 Твоя реферальная ссылка:\n<code>{ref_link}</code>\n\n"
        f"Пригласи {REQUIRED_REFERRALS} друга(ей), чтобы получить VPN-доступ на {LINK_DURATION_DAYS} дн.",
        disable_web_page_preview=True,
    )


@dp.message(Command("status"))
async def cmd_status(message: Message):
    user_id = message.from_user.id
    row = get_user(user_id)
    if row is None:
        create_user(user_id, message.from_user.username or message.from_user.full_name, None)
        row = get_user(user_id)

    link, remaining = link_status(row)
    if link:
        await message.answer(
            f"✅ Твоя VPN-ссылка активна ещё {fmt_timedelta(remaining)}:\n<code>{link}</code>",
            disable_web_page_preview=True,
        )
    else:
        if row["current_link"]:
            clear_expired_link(user_id)
        needed = max(REQUIRED_REFERRALS - row["referral_count"], 0)
        if needed == 0:
            await try_auto_issue(user_id)
            await message.answer("🎁 У тебя достаточно приглашений! Держи новую ссылку — набери /status ещё раз.")
        else:
            await message.answer(
                f"📊 Приглашено друзей: {row['referral_count']}/{REQUIRED_REFERRALS}\n"
                f"Ещё нужно: {needed}, чтобы получить VPN-ссылку на {LINK_DURATION_DAYS} дн."
            )


async def try_auto_issue(user_id: int):
    """Если у пользователя достаточно рефералов и нет активной ссылки — выдать новую из пула."""
    row = get_user(user_id)
    if row is None:
        return
    active_link, remaining = link_status(row)
    if active_link:
        return  # уже есть активная
    if row["referral_count"] >= REQUIRED_REFERRALS:
        new_link = pop_link_from_pool()
        if new_link:
            issue_link_to_user(user_id, new_link)
            try:
                await bot.send_message(
                    user_id,
                    f"🎁 Тебе выдана новая VPN-ссылка (действует {LINK_DURATION_DAYS} дн.):\n<code>{new_link}</code>",
                    disable_web_page_preview=True,
                )
            except Exception:
                pass
        else:
            try:
                await bot.send_message(user_id, "⏳ Все условия выполнены, но ссылки закончились. Как только админ добавит новые — ты получишь одну автоматически.")
            except Exception:
                pass


# ========== АДМИН-КОМАНДЫ ==========
def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


@dp.message(Command("addlink"))
async def cmd_addlink(message: Message):
    if not is_admin(message.from_user.id):
        return
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.answer("Использование: /addlink vless://твоя_ссылка")
        return
    add_link_to_pool(args[1].strip())
    await message.answer(f"✅ Ссылка добавлена в пул. Сейчас в пуле: {pool_count()}")


@dp.message(Command("addlinks"))
async def cmd_addlinks(message: Message):
    """Массовое добавление — каждая ссылка с новой строки."""
    if not is_admin(message.from_user.id):
        return
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.answer("Использование:\n/addlinks\nссылка1\nссылка2\nссылка3")
        return
    lines = [l.strip() for l in args[1].splitlines() if l.strip()]
    for l in lines:
        add_link_to_pool(l)
    await message.answer(f"✅ Добавлено {len(lines)} ссылок. Сейчас в пуле: {pool_count()}")


@dp.message(Command("stats"))
async def cmd_stats(message: Message):
    if not is_admin(message.from_user.id):
        return
    await message.answer(
        f"📊 Статистика:\n"
        f"Пользователей: {total_users()}\n"
        f"Свободных ссылок в пуле: {pool_count()}\n"
        f"Нужно рефералов за ссылку: {REQUIRED_REFERRALS}\n"
        f"Срок действия ссылки: {LINK_DURATION_DAYS} дн."
    )


@dp.message(Command("setrequired"))
async def cmd_setrequired(message: Message):
    if not is_admin(message.from_user.id):
        return
    global REQUIRED_REFERRALS
    args = message.text.split(maxsplit=1)
    if len(args) < 2 or not args[1].strip().isdigit():
        await message.answer("Использование: /setrequired 2")
        return
    REQUIRED_REFERRALS = int(args[1].strip())
    await message.answer(f"✅ Теперь нужно {REQUIRED_REFERRALS} реферал(ов) за ссылку.")


# ========== ЗАПУСК ==========
async def main():
    init_db()
    logging.info("Бот запущен, начинаем polling...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
