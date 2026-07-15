import asyncio
import logging
import os
import sqlite3
from datetime import datetime, timedelta

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    LabeledPrice,
    PreCheckoutQuery,
)
from aiogram.client.default import DefaultBotProperties

# ========== НАСТРОЙКИ ==========
BOT_TOKEN = os.getenv("BOT_TOKEN", "ВСТАВЬ_СЮДА_ТОКЕН_ОТ_BOTFATHER")
ADMIN_IDS = [int(x) for x in os.getenv("ADMIN_IDS", "ВСТАВЬ_СВОЙ_TELEGRAM_ID").split(",") if x.strip().isdigit()]
REQUIRED_REFERRALS = int(os.getenv("REQUIRED_REFERRALS", "3"))   # сколько друзей нужно пригласить за 1 ссылку
LINK_DURATION_DAYS = int(os.getenv("LINK_DURATION_DAYS", "2"))  # на сколько дней выдаётся ссылка
DB_PATH = os.getenv("DB_PATH", "vpnbot.db")

# Новостной канал (необязательно). Если указан NEWS_CHANNEL_ID — бот будет
# требовать подписку на канал перед выдачей ссылки. Бот должен быть админом канала.
NEWS_CHANNEL_URL = os.getenv("NEWS_CHANNEL_URL", "")   # например https://t.me/my_channel
NEWS_CHANNEL_ID = os.getenv("NEWS_CHANNEL_ID", "")     # например @my_channel или -1001234567890
REQUIRE_SUBSCRIPTION = bool(NEWS_CHANNEL_ID)

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


def get_all_users(limit: int = 50, offset: int = 0):
    conn = db()
    rows = conn.execute(
        "SELECT * FROM users ORDER BY joined_at DESC LIMIT ? OFFSET ?", (limit, offset)
    ).fetchall()
    conn.close()
    return rows


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


async def is_subscribed(user_id: int) -> bool:
    """Проверяет подписку пользователя на новостной канал. Если канал не настроен — считаем, что подписка не нужна."""
    if not REQUIRE_SUBSCRIPTION:
        return True
    try:
        member = await bot.get_chat_member(chat_id=NEWS_CHANNEL_ID, user_id=user_id)
        return member.status in ("member", "administrator", "creator")
    except Exception:
        # если бот не админ канала или ID неверный — не блокируем пользователей из-за ошибки настройки
        logging.warning("Не удалось проверить подписку на канал, пропускаем проверку")
        return True


def main_menu_kb() -> InlineKeyboardMarkup:
    buttons = [
        [
            InlineKeyboardButton(
                text="🎁 Получить VPN",
                callback_data="get_vpn"
            )
        ],
        [
            InlineKeyboardButton(
                text="📊 Мой статус",
                callback_data="status"
            ),
            InlineKeyboardButton(
                text="🔗 Моя ссылка",
                callback_data="mylink"
            )
        ],
        [
            InlineKeyboardButton(
                text="📱 Как подключить VPN",
                callback_data="how_connect"
            )
        ],
        [
            InlineKeyboardButton(
                text="🆘 Поддержка",
                callback_data="support"
            )
        ],
        [
            InlineKeyboardButton(
                text="💛 Поддержать проект",
                callback_data="donate"
            )
        ],
    ]

    if NEWS_CHANNEL_URL:
        buttons.insert(
            4,
            [
                InlineKeyboardButton(
                    text="📰 Новости канала",
                    url=NEWS_CHANNEL_URL
                )
            ]
        )

    return InlineKeyboardMarkup(inline_keyboard=buttons)


def subscribe_kb() -> InlineKeyboardMarkup:
    buttons = []
    if NEWS_CHANNEL_URL:
        buttons.append([InlineKeyboardButton(text="📰 Подписаться на канал", url=NEWS_CHANNEL_URL)])
    buttons.append([InlineKeyboardButton(text="✅ Я подписался", callback_data="check_sub")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

async def build_mylink_text(user_id: int) -> str:
    bot_username = await get_bot_username()
    ref_link = f"https://t.me/{bot_username}?start=ref{user_id}"

    row = get_user(user_id)

    referrals = row["referral_count"] if row else 0
    needed = max(REQUIRED_REFERRALS - referrals, 0)

    if REQUIRED_REFERRALS > 0:
        percent = int((referrals / REQUIRED_REFERRALS) * 100)
    else:
        percent = 100

    progress_count = min(referrals, REQUIRED_REFERRALS)

    progress = "🟩" * progress_count + "⬜" * (REQUIRED_REFERRALS - progress_count)

    return (
        "🎁 Твой прогресс MSKVPN\n\n"
        f"👥 Приглашено: {referrals}/{REQUIRED_REFERRALS}\n"
        f"{progress} {percent}%\n\n"
        f"🔗 Твоя ссылка:\n<code>{ref_link}</code>\n\n"
        f"Осталось пригласить: {needed}\n\n"
        "После выполнения условий бот автоматически выдаст VPN-доступ 🚀"
    )

async def build_status_text(user_id: int, username: str) -> str:
    row = get_user(user_id)
    if row is None:
        create_user(user_id, username, None)
        row = get_user(user_id)

    link, remaining = link_status(row)
    if link:
        return f"✅ Твоя ссылка активна ещё {fmt_timedelta(remaining)}:\n<code>{link}</code>"

    if row["current_link"]:
        clear_expired_link(user_id)

    needed = max(REQUIRED_REFERRALS - row["referral_count"], 0)
    subscribed = await is_subscribed(user_id)

    if needed > 0:
        progress = min(row["referral_count"], REQUIRED_REFERRALS)

    bar = "🟩" * progress + "⬜" * (REQUIRED_REFERRALS - progress)

    return (
        f"🎁 Твой прогресс MSKVPN\n\n"
        f"👥 Приглашено: {row['referral_count']}/{REQUIRED_REFERRALS}\n"
        f"{bar}\n\n"
        f"📈 Осталось пригласить: {needed}\n\n"
        f"🔗 Нажми «Пригласить друзей» и поделись своей ссылкой."
    )

    await try_auto_issue(user_id)
    return "🎁 Все условия выполнены! Держи новую ссылку — нажми «Мой статус» ещё раз."


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
                f"🎉 Новый участник!\n\n"
f"👤 Кто-то присоединился по твоей ссылке.\n\n"
f"📊 Твой прогресс:\n"
f"{new_count}/{REQUIRED_REFERRALS} приглашений ✅",
            )
        except Exception:
            pass
        await try_auto_issue(referred_by)

    greeting = "👋 Привет! Здесь можно получить доступ бесплатно — просто пригласи друзей."
    if REQUIRE_SUBSCRIPTION:
        greeting += " И не забудь подписаться на наш новостной канал 📰"
    greeting += "\n\nНажми на кнопку ниже 👇"

    await message.answer(greeting, reply_markup=main_menu_kb())


@dp.message(Command("mylink"))
async def cmd_mylink(message: Message):
    user_id = message.from_user.id
    if get_user(user_id) is None:
        create_user(user_id, message.from_user.username or message.from_user.full_name, None)
    text = await build_mylink_text(user_id)
    await message.answer(text, reply_markup=main_menu_kb(), disable_web_page_preview=True)


@dp.message(Command("status"))
async def cmd_status(message: Message):
    user_id = message.from_user.id
    username = message.from_user.username or message.from_user.full_name
    text = await build_status_text(user_id, username)
    await message.answer(text, reply_markup=main_menu_kb(), disable_web_page_preview=True)

@dp.callback_query(F.data == "mylink")
async def cb_mylink(callback: CallbackQuery):
    user_id = callback.from_user.id

    if get_user(user_id) is None:
        create_user(
            user_id,
            callback.from_user.username or callback.from_user.full_name,
            None
        )

    text = await build_mylink_text(user_id)

    bot_username = await get_bot_username()

    share_link = (
        f"https://t.me/share/url?"
        f"url=https://t.me/{bot_username}?start=ref{user_id}"
        f"&text=Я получил VPN бесплатно через MSKVPN 🔐"
    )

    await callback.message.edit_text(
        text,
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="📤 Поделиться ссылкой",
                        url=share_link
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="⬅️ Назад",
                        callback_data="back_menu"
                    )
                ]
            ]
        ),
        disable_web_page_preview=True
    )

    await callback.answer()

@dp.callback_query(F.data == "status")
async def cb_status(callback: CallbackQuery):
    user_id = callback.from_user.id
    username = callback.from_user.username or callback.from_user.full_name
    text = await build_status_text(user_id, username)
    await callback.message.edit_text(text, reply_markup=main_menu_kb(), disable_web_page_preview=True)
    await callback.answer()

@dp.callback_query(F.data == "get_vpn")
async def cb_get_vpn(callback: CallbackQuery):

    row = get_user(callback.from_user.id)

    if row["referral_count"] < REQUIRED_REFERRALS:

        left = REQUIRED_REFERRALS - row["referral_count"]

        await callback.answer()

        await callback.message.edit_text(
            f"🎁 Получение VPN\n\n"
            f"❌ Пока недостаточно приглашений.\n\n"
            f"👥 Приглашено: {row['referral_count']}/{REQUIRED_REFERRALS}\n"
            f"📈 Осталось: {left}",
            reply_markup=main_menu_kb()
        )

        return

    await callback.answer(
        "✅ Условия выполнены. Проверяем доступ...",
        show_alert=True
    )
        else:
        needed = max(REQUIRED_REFERRALS - row["referral_count"], 0)

        text = (
            "🎁 Получение VPN\n\n"
            f"👥 Приглашено друзей: {row['referral_count']}/{REQUIRED_REFERRALS}\n\n"
            f"📌 Осталось пригласить: {needed}\n\n"
            "Используй свою ссылку ниже 👇"
        )

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🔗 Моя ссылка",
                    callback_data="mylink"
                )
            ],
            [
                InlineKeyboardButton(
                    text="⬅️ Назад",
                    callback_data="back_menu"
                )
            ]
        ]
    )

    await callback.message.edit_text(
        text,
        reply_markup=keyboard,
        disable_web_page_preview=True
    )

    await callback.answer()

@dp.callback_query(F.data == "back_menu")
async def cb_back_menu(callback: CallbackQuery):
    await callback.message.edit_text(
        "🏠 Главное меню",
        reply_markup=main_menu_kb()
    )
    await callback.answer()

@dp.callback_query(F.data == "how_connect")
async def cb_how_connect(callback: CallbackQuery):
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🍎 iPhone",
                    callback_data="connect_iphone"
                )
            ],
            [
                InlineKeyboardButton(
                    text="🤖 Android",
                    callback_data="connect_android"
                )
            ],
            [
                InlineKeyboardButton(
                    text="💻 Windows",
                    callback_data="connect_windows"
                )
            ],
            [
                InlineKeyboardButton(
                    text="⬅️ Назад",
                    callback_data="back_menu"
                )
            ],
        ]
    )

    await callback.message.edit_text(
        "📱 Как подключить VPN\n\n"
        "Выбери своё устройство:",
        reply_markup=keyboard
    )

    await callback.answer()

@dp.callback_query(F.data == "connect_iphone")
async def cb_connect_iphone(callback: CallbackQuery):
    await callback.message.edit_text(
        "🍎 Подключение VPN на iPhone\n\n"
        "1️⃣ Установи приложение Hiddify или Streisand\n\n"
        "2️⃣ Скопируй VLESS-ссылку, которую выдаст бот\n\n"
        "3️⃣ Открой приложение\n\n"
        "4️⃣ Нажми ➕ и выбери импорт из буфера обмена\n\n"
        "5️⃣ Включи подключение ✅",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="⬅️ Назад",
                        callback_data="how_connect"
                    )
                ]
            ]
        )
    )
    await callback.answer()

@dp.callback_query(F.data == "connect_android")
async def cb_connect_android(callback: CallbackQuery):
    await callback.message.edit_text(
        "🤖 Подключение VPN на Android\n\n"
        "1️⃣ Установи приложение v2rayNG или Hiddify\n\n"
        "2️⃣ Скопируй VLESS-ссылку\n\n"
        "3️⃣ Импортируй ссылку в приложение\n\n"
        "4️⃣ Нажми подключить ✅",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="⬅️ Назад",
                        callback_data="how_connect"
                    )
                ]
            ]
        )
    )
    await callback.answer()


@dp.callback_query(F.data == "connect_windows")
async def cb_connect_windows(callback: CallbackQuery):
    await callback.message.edit_text(
        "💻 Подключение VPN на Windows\n\n"
        "1️⃣ Установи Hiddify или Nekoray\n\n"
        "2️⃣ Добавь VLESS-ссылку\n\n"
        "3️⃣ Включи подключение ✅",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="⬅️ Назад",
                        callback_data="how_connect"
                    )
                ]
            ]
        )
    )
    await callback.answer()

@dp.callback_query(F.data == "check_sub")
async def cb_check_sub(callback: CallbackQuery):
    user_id = callback.from_user.id
    username = callback.from_user.username or callback.from_user.full_name
    if await is_subscribed(user_id):
        await callback.answer("✅ Подписка подтверждена!", show_alert=True)
        await try_auto_issue(user_id)
        text = await build_status_text(user_id, username)
        await callback.message.edit_text(text, reply_markup=main_menu_kb(), disable_web_page_preview=True)
    else:
        await callback.answer("❌ Пока не вижу твою подписку. Подпишись и попробуй снова.", show_alert=True)


@dp.callback_query(F.data == "donate")
async def cb_donate(callback: CallbackQuery):
    await callback.answer()
    await bot.send_invoice(
        chat_id=callback.from_user.id,
        title="Поддержать автора",
        description="Спасибо, что пользуешься ботом! Этот донат — просто способ сказать спасибо 💛",
        payload="donate_1_star",
        currency="XTR",  # Telegram Stars
        prices=[LabeledPrice(label="Донат", amount=1)],  # 1 звезда
        provider_token="",  # для Stars токен не нужен
    )

@dp.callback_query(F.data == "support")
async def cb_support(callback: CallbackQuery):
    await callback.message.edit_text(
        "🆘 Поддержка\n\n"
        "Если возникли проблемы с подключением VPN:\n\n"
        "Напишите нам:\n"
        "@mskvpn_support \n\n"
        "Мы поможем разобраться ❤️",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="⬅️ Назад",
                        callback_data="back_menu"
                    )
                ]
            ]
        )
    )
    await callback.answer()

@dp.pre_checkout_query()
async def process_pre_checkout(pre_checkout_q: PreCheckoutQuery):
    await bot.answer_pre_checkout_query(pre_checkout_q.id, ok=True)


@dp.message(F.successful_payment)
async def process_successful_payment(message: Message):
    await message.answer("💛 Спасибо большое за поддержку! Это правда приятно.")


async def try_auto_issue(user_id: int):
    """Если у пользователя достаточно рефералов, он подписан на канал (если требуется)
    и нет активной ссылки — выдать новую из пула."""
    row = get_user(user_id)
    if row is None:
        return
    active_link, remaining = link_status(row)
    if active_link:
        return  # уже есть активная

    if row["referral_count"] < REQUIRED_REFERRALS:
        return

    if REQUIRE_SUBSCRIPTION and not await is_subscribed(user_id):
        return

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


@dp.message(Command("users"))
async def cmd_users(message: Message):
    if not is_admin(message.from_user.id):
        return

    args = message.text.split()
    page = int(args[1]) if len(args) > 1 and args[1].isdigit() else 1
    per_page = 20
    offset = (page - 1) * per_page

    rows = get_all_users(limit=per_page, offset=offset)
    if not rows:
        await message.answer("Пользователей на этой странице нет.")
        return

    lines = [f"👥 Пользователи (стр. {page}), всего: {total_users()}\n"]
    for row in rows:
        link, remaining = link_status(row)
        uname = f"@{row['username']}" if row["username"] and not row["username"].isdigit() else row["username"] or "—"
        status = f"🟢 ссылка активна ({fmt_timedelta(remaining)})" if link else "⚪ нет активной ссылки"
        lines.append(
            f"• <code>{row['user_id']}</code> {uname}\n"
            f"  рефералов: {row['referral_count']} | {status}"
        )

    lines.append(f"\nСледующая страница: /users {page + 1}")
    await message.answer("\n".join(lines), disable_web_page_preview=True)


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
