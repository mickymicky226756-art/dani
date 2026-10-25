"""Car Lottery Telegram bot (python-telegram-bot v21+)."""
import asyncio
import csv
import hashlib
import html
import io
import json
import logging
import math
import re
import time
from datetime import datetime
from functools import wraps

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.constants import ParseMode
from telegram.error import BadRequest, TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import config
import db as dbsync
from i18n import LANGS, t


class _AsyncDB:
    """Runs every (blocking) database function in a worker thread."""

    def __getattr__(self, name):
        fn = getattr(dbsync, name)

        async def call(*a, **k):
            return await asyncio.to_thread(fn, *a, **k)

        return call


db = _AsyncDB()

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO
)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("lottery")

HTML = ParseMode.HTML
CAR_EMOJI = ["🚗", "🚙", "🚐"]
ADMIN_MEDIA_RE = re.compile(r"^/(addcar|setphoto)(@\w+)?(\s|$)", re.I)


# ============================================================== helpers
def fmt(n: int) -> str:
    return f"{n:,}"


def e(s) -> str:
    return html.escape(str(s))


def nums_str(nums, cap=600) -> str:
    """'3, 5, 9' for a few numbers; '1-40, 55, 60-72' for many; always short enough for Telegram captions."""
    nums = sorted(nums)
    if len(nums) <= 30:
        out = ", ".join(str(n) for n in nums)
    else:
        runs, start, prev = [], nums[0], nums[0]
        for n in nums[1:]:
            if n == prev + 1:
                prev = n
                continue
            runs.append((start, prev))
            start = prev = n
        runs.append((start, prev))
        out = ", ".join(str(a) if a == b else f"{a}-{b}" for a, b in runs)
    if len(out) > cap:
        out = out[:cap].rsplit(", ", 1)[0] + f" … ({len(nums)} total)"
    return out


def is_admin(uid: int) -> bool:
    return uid in config.ADMIN_IDS


def payment_info() -> str:
    lines = []
    if config.CBE_ACCOUNT:
        lines.append(f"CBE : {e(config.CBE_ACCOUNT)}")
    if config.TELEBIRR_NUMBER:
        lines.append(f"TeleBirr: {e(config.TELEBIRR_NUMBER)}")
    return "\n".join(lines)


def countdown(lang: str, deadline: int) -> str:
    left = deadline - int(time.time())
    if left <= 0:
        return t(lang, "time_reached")
    d, rem = divmod(left, 86400)
    h, rem = divmod(rem, 3600)
    m, s = divmod(rem, 60)
    return "<code>" + t(lang, "countdown", d=d, h=h, m=m, s=s) + "</code>"


def fmt_date(ts: int) -> str:
    dt = datetime.fromtimestamp(ts, config.TZ).strftime("%Y-%m-%d %H:%M")
    return f"{dt} (UTC{config.TZ_OFFSET_HOURS:+d})"


def parse_dt(s: str) -> int:
    dt = datetime.strptime(s.strip(), "%Y-%m-%d %H:%M").replace(tzinfo=config.TZ)
    return int(dt.timestamp())


def car_slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def find_car_photo(car):
    """Local picture for a car: photos/<slug>.jpg|jpeg|png"""
    slug = car_slug(car["name"])
    for ext in (".jpg", ".jpeg", ".png"):
        p = config.PHOTOS_DIR / (slug + ext)
        if p.is_file():
            return p
    return None


_HASH_CACHE = {}


def file_digest(path) -> str:
    st = path.stat()
    key = (str(path), st.st_mtime_ns, st.st_size)
    if key not in _HASH_CACHE:
        _HASH_CACHE.clear()
        _HASH_CACHE[key] = hashlib.sha1(path.read_bytes()).hexdigest()
    return _HASH_CACHE[key]


def seed_cars() -> int:
    """First start only: create the cars listed in cars_seed.json when there are none."""
    if dbsync.count_cars() > 0:
        return 0
    try:
        items = json.loads(config.SEED_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError) as ex:
        log.warning("Could not read %s: %s (create cars with /addcar instead)", config.SEED_FILE, ex)
        return 0
    created = 0
    for it in items:
        try:
            name = str(it["name"]).strip()
            price, total = int(it["price"]), int(it["total"])
            if it.get("draw_date"):
                deadline = parse_dt(it["draw_date"])
            else:
                deadline = int(time.time()) + int(float(it.get("draw_in_days", 7)) * 86400)
            if not name or price <= 0 or not (1 <= total <= config.MAX_TOTAL):
                raise ValueError("invalid values")
        except (KeyError, ValueError, TypeError, AttributeError) as ex:
            log.warning("Skipping bad seed entry %r: %s", it, ex)
            continue
        dbsync.add_car(name, price, total, None, deadline)
        created += 1
    log.info("Created %d car(s) from %s", created, config.SEED_FILE.name)
    return created


def is_open(car) -> bool:
    return bool(car and car["active"] and not car["drawn"] and car["deadline"] > time.time())


async def touch(update: Update) -> dict:
    u = update.effective_user
    return await db.upsert_user(u.id, u.full_name, u.username)


def main_kb(lang: str) -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            [t(lang, "btn_register"), t(lang, "btn_numbers")],
            [t(lang, "btn_draw"), t(lang, "btn_language")],
        ],
        resize_keyboard=True,
    )


MENU_LABELS = {}
for _l in LANGS:
    MENU_LABELS[t(_l, "btn_register")] = "register"
    MENU_LABELS[t(_l, "btn_numbers")] = "numbers"
    MENU_LABELS[t(_l, "btn_draw")] = "draw"
    MENU_LABELS[t(_l, "btn_language")] = "language"


def lang_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("🇪🇹 አማርኛ", callback_data="lang:am"),
                InlineKeyboardButton("🇬🇧 English", callback_data="lang:en"),
            ],
            [InlineKeyboardButton("🇪🇹 Afaan Oromo", callback_data="lang:om")],
        ]
    )


def normalize_phone(raw: str):
    s = re.sub(r"[\s\-()]", "", raw or "")
    m = re.fullmatch(r"(?:\+?251|0)?([79]\d{8})", s)
    return "0" + m.group(1) if m else None


async def safe_edit(query, text, markup=None):
    try:
        await query.edit_message_text(text, parse_mode=HTML, reply_markup=markup)
    except BadRequest as ex:
        if "not modified" not in str(ex).lower():
            raise


# ============================================================== views
async def car_caption(car, lang) -> str:
    taken = await db.taken_count(car["id"])
    return f"🚙 <b>{e(car['name'])}</b>\n\n" + t(
        lang,
        "car_caption",
        price=fmt(car["price"]),
        total=fmt(car["total"]),
        sold=fmt(taken),
        remaining=fmt(car["total"] - taken),
        countdown=countdown(lang, car["deadline"]),
    )


def bucket_bounds(car, b):
    """Real numbers inside bucket b (bucket 0 = 1..99, bucket 1 = 100..199, ...)."""
    return max(b * config.RANGE_SIZE, 1), min(b * config.RANGE_SIZE + config.RANGE_SIZE - 1, car["total"])


def bucket_count(car) -> int:
    return car["total"] // config.RANGE_SIZE + 1


def page_bounds(car, b, h):
    """A bucket with 100 numbers is shown as two pages of 50 (Telegram: max 100 buttons)."""
    lo, hi = bucket_bounds(car, b)
    if hi - lo + 1 <= 99:
        return lo, hi, False
    mid = lo + 50
    return (lo, mid - 1, True) if h == 0 else (mid, hi, True)


async def ranges_view(car, lang):
    size = config.RANGE_SIZE
    bt = await db.bucket_taken(car["id"], size)
    buttons = []
    for b in range(bucket_count(car)):
        lo, hi = bucket_bounds(car, b)
        label = f"{b * size}-{b * size + size - 1}"
        if bt.get(b, 0) >= hi - lo + 1:
            label = "🔴 " + label
        buttons.append(InlineKeyboardButton(label, callback_data=f"rng:{car['id']}:{b}:0"))
    rows = [buttons[i : i + 2] for i in range(0, len(buttons), 2)]
    rows.append([InlineKeyboardButton(t(lang, "btn_back"), callback_data="back:cars")])
    taken = sum(bt.values())
    text = t(lang, "choose_range", avail=fmt(car["total"] - taken), taken=fmt(taken))
    return text, InlineKeyboardMarkup(rows)


async def grid_view(car, b, h, lang, uid=None):
    size = config.RANGE_SIZE
    lo, hi, split = page_bounds(car, b, h)
    taken = await db.taken_numbers(car["id"], lo, hi)
    mine = set()
    if uid is not None:
        mine = {r["number"] for r in await db.user_reserved(uid, car["id"])}
    btns = []
    for n in range(lo, hi + 1):
        if n in mine:
            btns.append(InlineKeyboardButton(f"🟡 {n}", callback_data=f"unpick:{car['id']}:{n}"))
        elif n in taken:
            btns.append(InlineKeyboardButton(f"🔴 {n}", callback_data="taken"))
        else:
            btns.append(InlineKeyboardButton(f"🟢 {n}", callback_data=f"pick:{car['id']}:{n}"))
    rows = [btns[i : i + 4] for i in range(0, len(btns), 4)]
    back_data = f"ranges:{car['id']}" if bucket_count(car) > 1 else "back:cars"
    nav = [InlineKeyboardButton(t(lang, "btn_ranges"), callback_data=back_data)]
    if split:
        _, full_hi = bucket_bounds(car, b)
        full_lo = bucket_bounds(car, b)[0]
        if h == 0:
            nav.append(InlineKeyboardButton(f"➡️ {lo + 50}-{full_hi}", callback_data=f"rng:{car['id']}:{b}:1"))
        else:
            nav.append(InlineKeyboardButton(f"⬅️ {full_lo}-{lo - 1}", callback_data=f"rng:{car['id']}:{b}:0"))
    rows.append(nav)
    if split:
        head_lo, head_hi = lo, hi
    else:
        head_lo, head_hi = b * size, b * size + size - 1
    text = t(lang, "choose_number", lo=head_lo, hi=head_hi)
    if mine:
        text += "\n" + t(lang, "mine_legend")
    return text, InlineKeyboardMarkup(rows)


async def send_car_list(bot, chat_id, lang):
    cars = await db.active_cars()
    if not cars:
        msg = t(lang, "no_cars")
        if is_admin(chat_id):
            msg += "\n\n👑 Admin: no car exists yet. Create one with:\n/addcar Name | price | total tickets | YYYY-MM-DD HH:MM"
        await bot.send_message(chat_id, msg)
        return
    rows = [
        [InlineKeyboardButton(f"{CAR_EMOJI[i % 3]} {c['name']}", callback_data=f"car:{c['id']}")]
        for i, c in enumerate(cars)
    ]
    rows.append([InlineKeyboardButton(t(lang, "btn_back"), callback_data="back:menu")])
    await bot.send_message(
        chat_id, t(lang, "choose_reward"), parse_mode=HTML, reply_markup=InlineKeyboardMarkup(rows)
    )


# ======================================================== user handlers
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await touch(update)
    await update.effective_message.reply_text(t("en", "choose_lang"), reply_markup=lang_kb())


async def cmd_myid(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(f"Your Telegram ID: {update.effective_user.id}")


async def cb_lang(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    code = q.data.split(":")[1]
    if code not in LANGS:
        await q.answer()
        return
    await touch(update)
    await db.set_lang(update.effective_user.id, code)
    await q.answer()
    chat_id = q.message.chat_id
    await ctx.bot.send_message(chat_id, t(code, "lang_set"))
    await ctx.bot.send_message(
        chat_id, t(code, "welcome", brand=config.BRAND_NAME), reply_markup=main_kb(code)
    )


async def on_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    row = await touch(update)
    lang = row["lang"]
    action = MENU_LABELS.get(update.effective_message.text)
    chat_id = update.effective_chat.id
    if action == "register":
        await send_car_list(ctx.bot, chat_id, lang)
    elif action == "numbers":
        await show_my_numbers(ctx, chat_id, update.effective_user.id, lang)
    elif action == "draw":
        await show_draw_dates(ctx, chat_id, lang)
    elif action == "language":
        await update.effective_message.reply_text(t("en", "choose_lang"), reply_markup=lang_kb())


async def show_my_numbers(ctx, chat_id, uid, lang):
    rows = await db.user_tickets(uid)
    if not rows:
        await ctx.bot.send_message(chat_id, t(lang, "my_none"))
        return
    cars = {}
    for r in rows:
        cars.setdefault(r["car_id"], {"name": r["car_name"], "sold": [], "pending": [], "reserved": [], "exp": 0})
        c = cars[r["car_id"]]
        c[r["status"]].append(r["number"])
        if r["status"] == "reserved":
            c["exp"] = max(c["exp"], r["expires_at"])
    parts = [t(lang, "my_title")]
    for c in cars.values():
        lines = [f"\n🚙 <b>{e(c['name'])}</b>"]
        if c["sold"]:
            lines.append(t(lang, "my_confirmed", nums=nums_str(c["sold"])))
        if c["pending"]:
            lines.append(t(lang, "my_pending", nums=nums_str(c["pending"])))
        if c["reserved"]:
            mins = max(1, math.ceil((c["exp"] - time.time()) / 60))
            lines.append(t(lang, "my_reserved", mins=mins, nums=nums_str(c["reserved"])))
        parts.append("\n".join(lines))
    await ctx.bot.send_message(chat_id, "\n".join(parts), parse_mode=HTML)


async def show_draw_dates(ctx, chat_id, lang):
    cars = await db.active_cars()
    if not cars:
        await ctx.bot.send_message(chat_id, t(lang, "no_cars"))
        return
    parts = [t(lang, "draw_title")]
    for c in cars:
        parts.append(
            "\n"
            + t(lang, "draw_line", name=e(c["name"]), date=fmt_date(c["deadline"]), countdown=countdown(lang, c["deadline"]))
        )
    await ctx.bot.send_message(chat_id, "\n".join(parts), parse_mode=HTML)


async def cb_car(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    lang = (await touch(update))["lang"]
    car = await db.get_car(int(q.data.split(":")[1]))
    if not car or not car["active"] or car["drawn"]:
        await q.answer(t(lang, "closed_alert"), show_alert=True)
        return
    await q.answer()
    kb = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(t(lang, "btn_continue"), callback_data=f"cont:{car['id']}"),
                InlineKeyboardButton(t(lang, "btn_back"), callback_data="back:cars"),
            ]
        ]
    )
    caption = await car_caption(car, lang)
    chat_id = q.message.chat_id
    sent = False
    path = find_car_photo(car)
    digest = file_digest(path) if path else None
    # Use the stored Telegram file_id when it is still the right picture:
    #  - an admin chose it ("manual"), or there is no local file, or the local file is unchanged
    use_cached = bool(car["photo"]) and (
        car.get("photo_hash") == "manual" or not path or car.get("photo_hash") == digest
    )
    if use_cached:
        try:
            await ctx.bot.send_photo(chat_id, car["photo"], caption=caption, parse_mode=HTML, reply_markup=kb)
            sent = True
        except TelegramError as ex:
            log.warning("Stored photo of car %s failed: %s", car["id"], ex)
    if not sent and path:
        try:
            m = await ctx.bot.send_photo(
                chat_id, path.read_bytes(), caption=caption, parse_mode=HTML, reply_markup=kb
            )
            sent = True
            await db.set_car_photo(car["id"], m.photo[-1].file_id, digest)  # reuse next time
        except TelegramError as ex:
            log.warning("Could not upload %s: %s", path.name, ex)
    if not sent:
        await ctx.bot.send_message(chat_id, caption, parse_mode=HTML, reply_markup=kb)


async def cb_continue(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    lang = (await touch(update))["lang"]
    car = await db.get_car(int(q.data.split(":")[1]))
    if not is_open(car):
        await q.answer(t(lang, "closed_alert"), show_alert=True)
        return
    await q.answer()
    chat_id = q.message.chat_id
    if bucket_count(car) == 1:
        text, kb = await grid_view(car, 0, 0, lang, update.effective_user.id)
    else:
        text, kb = await ranges_view(car, lang)
    await ctx.bot.send_message(chat_id, text, parse_mode=HTML, reply_markup=kb)


async def cb_ranges(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    lang = (await touch(update))["lang"]
    car = await db.get_car(int(q.data.split(":")[1]))
    if not is_open(car):
        await q.answer(t(lang, "closed_alert"), show_alert=True)
        return
    await q.answer()
    text, kb = await ranges_view(car, lang)
    await safe_edit(q, text, kb)


async def cb_range_pick(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    lang = (await touch(update))["lang"]
    _, cid, b, h = q.data.split(":")
    car = await db.get_car(int(cid))
    b, h = int(b), int(h)
    if not is_open(car) or not (0 <= b < bucket_count(car)) or h not in (0, 1):
        await q.answer(t(lang, "closed_alert"), show_alert=True)
        return
    await q.answer()
    text, kb = await grid_view(car, b, h, lang, update.effective_user.id)
    await safe_edit(q, text, kb)


async def cb_taken(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    lang = (await touch(update))["lang"]
    await update.callback_query.answer(t(lang, "taken_alert"), show_alert=True)


async def refresh_grid(q, car, num, lang, uid):
    """Re-draw the grid page that contains `num` so the person sees the current state."""
    try:
        b = num // config.RANGE_SIZE
        lo, hi, split = page_bounds(car, b, 0)
        h = 1 if (split and num >= lo + 50) else 0
        text, kb = await grid_view(car, b, h, lang, uid)
        await safe_edit(q, text, kb)
    except TelegramError as ex:
        log.debug("grid refresh failed: %s", ex)


async def update_basket(ctx, chat_id, uid, car, lang):
    """One payment message per car: lists ALL numbers the user holds + the total. Edited in place."""
    mine = [r["number"] for r in await db.user_reserved(uid, car["id"])]
    store = ctx.user_data.setdefault("basket", {})
    mid = store.get(car["id"])
    if not mine:
        if mid:
            store.pop(car["id"], None)
            try:
                await ctx.bot.delete_message(chat_id, mid)
            except TelegramError:
                pass
        return
    text = t(
        lang,
        "reserved",
        mins=config.RESERVATION_MINUTES,
        nums=nums_str(mine),
        amount=fmt(car["price"] * len(mine)),
        payment=payment_info(),
    )
    kb = InlineKeyboardMarkup([[InlineKeyboardButton(t(lang, "btn_back"), callback_data="back:cars")]])
    if mid:
        try:
            await ctx.bot.edit_message_text(
                text, chat_id=chat_id, message_id=mid, parse_mode=HTML, reply_markup=kb
            )
            return
        except BadRequest as ex:
            if "not modified" in str(ex).lower():
                return
            # message was deleted (e.g. Back pressed): fall through and send a new one
        except TelegramError:
            pass
    m = await ctx.bot.send_message(chat_id, text, parse_mode=HTML, reply_markup=kb)
    store[car["id"]] = m.message_id


async def cb_pick(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid = update.effective_user.id
    lang = (await touch(update))["lang"]
    _, cid, num = q.data.split(":")
    cid, num = int(cid), int(num)
    car = await db.get_car(cid)
    res = await db.reserve(cid, num, uid, config.RESERVATION_MINUTES, config.MAX_NUMBERS_PER_USER)

    if res == "ok":
        await q.answer()
        await update_basket(ctx, q.message.chat_id, uid, car, lang)
    elif res == "taken":
        await q.answer(t(lang, "taken_alert"), show_alert=True)
    elif res == "limit":
        await q.answer(t(lang, "limit_alert", max=config.MAX_NUMBERS_PER_USER), show_alert=True)
    else:
        await q.answer(t(lang, "closed_alert"), show_alert=True)
        return
    await refresh_grid(q, car, num, lang, uid)


async def cb_unpick(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Tap one of your own 🟡 numbers to remove it from your selection."""
    q = update.callback_query
    uid = update.effective_user.id
    lang = (await touch(update))["lang"]
    _, cid, num = q.data.split(":")
    cid, num = int(cid), int(num)
    car = await db.get_car(cid)
    if not car:
        await q.answer(t(lang, "closed_alert"), show_alert=True)
        return
    await db.release_number(uid, cid, num)
    await q.answer()
    await update_basket(ctx, q.message.chat_id, uid, car, lang)
    await refresh_grid(q, car, num, lang, uid)


async def cb_back(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    lang = (await touch(update))["lang"]
    await q.answer()
    chat_id = q.message.chat_id
    try:
        await q.message.delete()
    except TelegramError:
        pass
    if q.data == "back:cars":
        await send_car_list(ctx.bot, chat_id, lang)
    else:
        await ctx.bot.send_message(chat_id, t(lang, "menu_hint"), reply_markup=main_kb(lang))


async def cmd_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    lang = (await touch(update))["lang"]
    n = await db.release_user_reserved(update.effective_user.id)
    await update.effective_message.reply_text(t(lang, "cancel_done" if n else "cancel_none"))


# ------------------------------------------------------ payment proof
def admin_caption(pay, status=None) -> str:
    who = e(pay["user_name"] or "?")
    if pay["username"]:
        who += f" (@{e(pay['username'])})"
    text = (
        f"💳 <b>Payment #{pay['id']}</b>\n"
        f"🚙 {e(pay['car_name'])}\n"
        f"🎟 Numbers ({len(pay['numbers'])}): <b>{nums_str(pay['numbers'])}</b>\n"
        f"💰 Amount: <b>{fmt(pay['amount'])}</b> birr\n"
        f"👤 {who}\n🆔 <code>{pay['user_id']}</code>\n"
        f"📞 {e(pay['phone']) if pay['phone'] else '— (not provided yet)'}"
    )
    if status:
        text += f"\n\n{status}"
    return text


async def notify_admins(ctx, pid, only=None):
    pay = await db.payment_full(pid)
    if not pay:
        return
    kb = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ Approve", callback_data=f"adm:ok:{pid}"),
                InlineKeyboardButton("❌ Reject", callback_data=f"adm:no:{pid}"),
            ]
        ]
    )
    for aid in ([only] if only else config.ADMIN_IDS):
        try:
            if pay["file_kind"] == "photo":
                m = await ctx.bot.send_photo(aid, pay["file_id"], caption=admin_caption(pay), parse_mode=HTML, reply_markup=kb)
            else:
                m = await ctx.bot.send_document(aid, pay["file_id"], caption=admin_caption(pay), parse_mode=HTML, reply_markup=kb)
            await db.save_admin_msg(pid, aid, m.message_id)
        except TelegramError as ex:
            log.warning("Could not notify admin %s: %s (has the admin pressed /start?)", aid, ex)


async def on_media(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    user = update.effective_user
    if is_admin(user.id) and msg.photo and ADMIN_MEDIA_RE.match(msg.caption or ""):
        return await admin_photo_cmd(update, ctx)

    row = await touch(update)
    lang = row["lang"]
    if msg.photo:
        file_id, kind = msg.photo[-1].file_id, "photo"
    else:
        file_id, kind = msg.document.file_id, "document"

    pays = await db.create_payments(user.id, file_id, kind)
    if not pays:
        await msg.reply_text(t(lang, "no_reservation"))
        return
    await msg.reply_text(t(lang, "shot_received"))
    for p in pays:
        await notify_admins(ctx, p["payment_id"])
    if not row["phone"]:
        await db.set_awaiting_phone(user.id, 1)
        await msg.reply_text(t(lang, "ask_phone"))


async def save_phone(update: Update, ctx, raw: str):
    row = await touch(update)
    lang = row["lang"]
    phone = normalize_phone(raw)
    if not phone:
        await update.effective_message.reply_text(t(lang, "phone_invalid"))
        return
    await db.set_phone(update.effective_user.id, phone)
    await update.effective_message.reply_text(t(lang, "phone_saved"))
    if await db.user_has_pending(update.effective_user.id):
        for aid in config.ADMIN_IDS:
            try:
                await ctx.bot.send_message(
                    aid, f"📞 Phone for {e(row['name'])} (<code>{row['id']}</code>): <b>{e(phone)}</b>", parse_mode=HTML
                )
            except TelegramError:
                pass


async def on_contact(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await save_phone(update, ctx, update.effective_message.contact.phone_number)


async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    row = await touch(update)
    car_id = ctx.user_data.get("await_date")
    if car_id and is_admin(update.effective_user.id):
        await handle_date_input(update, ctx, car_id)
        return
    if row["awaiting_phone"]:
        await save_phone(update, ctx, update.effective_message.text)
    else:
        await update.effective_message.reply_text(t(row["lang"], "unknown"), reply_markup=main_kb(row["lang"]))


# ------------------------------------------------------- admin approval
async def cb_admin(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    admin = update.effective_user
    if not is_admin(admin.id):
        await q.answer("Not authorized.", show_alert=True)
        return
    _, action, pid = q.data.split(":")
    pid = int(pid)
    approve = action == "ok"
    pay = await db.payment_full(pid)  # fetched first: numbers disappear on rejection
    if not pay or not await db.decide_payment(pid, approve, admin.id):
        await q.answer("Already processed.", show_alert=True)
        try:
            await q.edit_message_reply_markup(reply_markup=None)
        except TelegramError:
            pass
        return
    await q.answer("Approved ✅" if approve else "Rejected ❌")
    status = ("✅ APPROVED" if approve else "❌ REJECTED") + f" by {e(admin.full_name)}"
    for m in await db.admin_msgs(pid):
        try:
            await ctx.bot.edit_message_caption(
                chat_id=m["chat_id"],
                message_id=m["message_id"],
                caption=admin_caption(pay, status),
                parse_mode=HTML,
                reply_markup=None,
            )
        except TelegramError as ex:
            log.debug("edit admin msg failed: %s", ex)
    lang = pay["lang"]
    key = "approved_user" if approve else "rejected_user"
    try:
        await ctx.bot.send_message(
            pay["user_id"],
            t(lang, key, car=e(pay["car_name"]), nums=nums_str(pay["numbers"])),
            parse_mode=HTML,
        )
    except TelegramError as ex:
        log.warning("Could not notify user %s: %s", pay["user_id"], ex)


# ============================================================== admin
def admin_only(fn):
    @wraps(fn)
    async def wrapper(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not is_admin(update.effective_user.id):
            await update.effective_message.reply_text("⛔ Not authorized. Your ID: %d" % update.effective_user.id)
            return
        return await fn(update, ctx)

    return wrapper


def cmd_arg(text: str) -> str:
    parts = (text or "").split(None, 1)
    return parts[1].strip() if len(parts) > 1 else ""


ADMIN_HELP = (
    "<b>Admin commands</b>\n\n"
    "/addcar Name | price | total tickets | YYYY-MM-DD HH:MM\n"
    "  (send it as a photo caption to attach the car picture)\n"
    "/setphoto &lt;id&gt; — send as a photo caption to change the picture\n"
    "/panel — buttons: change draw date / reset a car's tickets\n"
    "/cars — list all cars\n"
    "/setdeadline &lt;id&gt; YYYY-MM-DD HH:MM\n"
    "/setprice &lt;id&gt; &lt;price&gt;\n"
    "/settotal &lt;id&gt; &lt;total&gt;\n"
    "/togglecar &lt;id&gt; — show/hide a car\n"
    "/pending — re-send unconfirmed payments\n"
    "/stats — sales summary\n"
    "/export &lt;id&gt; — CSV of sold/pending tickets\n"
    "/draw &lt;id&gt; confirm — pick the winner\n"
    "/broadcast &lt;text&gt; — message all users\n"
    f"\nTimes are UTC{config.TZ_OFFSET_HOURS:+d}."
)


@admin_only
async def cmd_admin(update, ctx):
    await update.effective_message.reply_text(ADMIN_HELP, parse_mode=HTML)
    text, kb = await panel_view()
    await update.effective_message.reply_text(text, parse_mode=HTML, reply_markup=kb)


@admin_only
async def cmd_panel(update, ctx):
    ctx.user_data.pop("await_date", None)
    text, kb = await panel_view()
    await update.effective_message.reply_text(text, parse_mode=HTML, reply_markup=kb)


async def _create_car(arg: str, photo):
    parts = [p.strip() for p in arg.split("|")]
    if len(parts) != 4:
        return "Format: /addcar Name | price | total tickets | YYYY-MM-DD HH:MM"
    name, price_s, total_s, dl_s = parts
    if not name or len(name) > 60:
        return "Name must be 1-60 characters."
    if not (price_s.isdigit() and total_s.isdigit()):
        return "Price and total must be whole numbers."
    price, total = int(price_s), int(total_s)
    if price <= 0 or not (1 <= total <= config.MAX_TOTAL):
        return f"Price must be > 0 and total between 1 and {config.MAX_TOTAL}."
    try:
        deadline = parse_dt(dl_s)
    except ValueError:
        return "Invalid date. Use YYYY-MM-DD HH:MM, e.g. 2026-12-31 20:00"
    if deadline <= time.time():
        return "Deadline must be in the future."
    cid = await db.add_car(name, price, total, photo, deadline)
    extra = "" if photo else f"\nNo picture yet: send a photo with caption /setphoto {cid}"
    return f"✅ Car #{cid} created: {name} — {fmt(price)} birr × {fmt(total)} tickets, draw {fmt_date(deadline)}.{extra}"


@admin_only
async def cmd_addcar(update, ctx):
    res = await _create_car(cmd_arg(update.effective_message.text), None)
    await update.effective_message.reply_text(res)


async def admin_photo_cmd(update, ctx):
    msg = update.effective_message
    cap = msg.caption or ""
    photo = msg.photo[-1].file_id
    cmd = cap.split(None, 1)[0].lower().split("@")[0]
    arg = cmd_arg(cap)
    if cmd == "/addcar":
        await msg.reply_text(await _create_car(arg, photo))
    else:
        if not arg.isdigit() or not await db.get_car(int(arg)):
            await msg.reply_text("Usage: send a photo with caption /setphoto <car id>")
            return
        await db.set_car_photo(int(arg), photo, "manual")
        await msg.reply_text(f"✅ Picture updated for car #{arg}.")


@admin_only
async def cmd_cars(update, ctx):
    cars = await db.all_cars()
    if not cars:
        await update.effective_message.reply_text("No cars yet. Use /addcar.")
        return
    lines = []
    for c in cars:
        s = await db.car_stats(c["id"])
        state = "DRAWN" if c["drawn"] else ("active" if c["active"] else "hidden")
        lines.append(
            f"#{c['id']} {c['name']} [{state}]\n"
            f"   {fmt(c['price'])} birr · sold {s['sold']} · pending {s['pending']} · held {s['reserved']} / {fmt(c['total'])}\n"
            f"   draw {fmt_date(c['deadline'])}" + (" · 🖼" if c["photo"] else " · no picture")
        )
    await update.effective_message.reply_text("\n\n".join(lines))


async def _car_from_args(args):
    if not args or not args[0].isdigit():
        return None
    return await db.get_car(int(args[0]))


@admin_only
async def cmd_setdeadline(update, ctx):
    car = await _car_from_args(ctx.args)
    if not car or len(ctx.args) < 3:
        await update.effective_message.reply_text("Usage: /setdeadline <id> YYYY-MM-DD HH:MM")
        return
    try:
        ts = parse_dt(" ".join(ctx.args[1:3]))
    except ValueError:
        await update.effective_message.reply_text("Invalid date. Use YYYY-MM-DD HH:MM")
        return
    await db.set_car_field(car["id"], "deadline", ts)
    await update.effective_message.reply_text(f"✅ Draw for #{car['id']} set to {fmt_date(ts)}")


@admin_only
async def cmd_setprice(update, ctx):
    car = await _car_from_args(ctx.args)
    if not car or len(ctx.args) < 2 or not ctx.args[1].isdigit() or int(ctx.args[1]) <= 0:
        await update.effective_message.reply_text("Usage: /setprice <id> <price>")
        return
    await db.set_car_field(car["id"], "price", int(ctx.args[1]))
    await update.effective_message.reply_text(f"✅ Price for #{car['id']} is now {fmt(int(ctx.args[1]))} birr (applies to new reservations).")


@admin_only
async def cmd_settotal(update, ctx):
    car = await _car_from_args(ctx.args)
    if not car or len(ctx.args) < 2 or not ctx.args[1].isdigit():
        await update.effective_message.reply_text("Usage: /settotal <id> <total>")
        return
    total = int(ctx.args[1])
    if not (1 <= total <= config.MAX_TOTAL):
        await update.effective_message.reply_text(f"Total must be between 1 and {config.MAX_TOTAL}.")
        return
    top = await db.max_ticket_number(car["id"])
    if total < top:
        await update.effective_message.reply_text(f"Cannot go below {top}: that number is already taken.")
        return
    await db.set_car_field(car["id"], "total", total)
    await update.effective_message.reply_text(f"✅ Car #{car['id']} now has {fmt(total)} tickets.")


@admin_only
async def cmd_togglecar(update, ctx):
    car = await _car_from_args(ctx.args)
    if not car:
        await update.effective_message.reply_text("Usage: /togglecar <id>")
        return
    new = 0 if car["active"] else 1
    await db.set_car_field(car["id"], "active", new)
    await update.effective_message.reply_text(f"Car #{car['id']} is now {'visible' if new else 'hidden'}.")


@admin_only
async def cmd_pending(update, ctx):
    ids = await db.pending_payment_ids()
    if not ids:
        await update.effective_message.reply_text("No pending payments 🎉")
        return
    for pid in ids:
        await notify_admins(ctx, pid, only=update.effective_user.id)


@admin_only
async def cmd_stats(update, ctx):
    cars = await db.all_cars()
    if not cars:
        await update.effective_message.reply_text("No cars yet.")
        return
    user_ids = await db.all_user_ids()
    lines = [f"👥 Users: {len(user_ids)}"]
    for c in cars:
        s = await db.car_stats(c["id"])
        lines.append(
            f"\n#{c['id']} {c['name']}\n"
            f"✅ sold {s['sold']} · ⏳ pending {s['pending']} · 🕒 held {s['reserved']} · "
            f"🟢 free {c['total'] - s['sold'] - s['pending'] - s['reserved']}\n"
            f"💰 confirmed revenue: {fmt(s['sold'] * c['price'])} birr"
        )
    await update.effective_message.reply_text("\n".join(lines))


@admin_only
async def cmd_export(update, ctx):
    car = await _car_from_args(ctx.args)
    if not car:
        await update.effective_message.reply_text("Usage: /export <car id>")
        return
    rows = await db.sold_rows(car["id"])
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["number", "status", "name", "username", "phone", "telegram_id"])
    for r in rows:
        w.writerow([r["number"], r["status"], r["name"], r["username"] or "", r["phone"] or "", r["uid"]])
    data = io.BytesIO(buf.getvalue().encode("utf-8-sig"))
    data.name = f"car_{car['id']}_tickets.csv"
    await update.effective_message.reply_document(data, caption=f"{len(rows)} tickets")


@admin_only
async def cmd_draw(update, ctx):
    car = await _car_from_args(ctx.args)
    if not car:
        await update.effective_message.reply_text("Usage: /draw <car id> confirm")
        return
    if car["drawn"]:
        await update.effective_message.reply_text(
            f"Already drawn: winning number {car['winner_number']} (user {car['winner_user']})."
        )
        return
    st = await db.car_stats(car["id"])
    if len(ctx.args) < 2 or ctx.args[1].lower() != "confirm":
        warn = f"\n⚠️ {st['pending']} payment(s) still pending — confirm them first!" if st["pending"] else ""
        await update.effective_message.reply_text(
            f"Draw for #{car['id']} {car['name']}: {st['sold']} sold tickets.{warn}\n"
            f"This cannot be undone. Run: /draw {car['id']} confirm"
        )
        return
    res = await db.draw(car["id"])
    if not res:
        await update.effective_message.reply_text("No confirmed (sold) tickets to draw from.")
        return
    u = res["user"] or {}
    await update.effective_message.reply_text(
        f"🏆 <b>Winner for {e(car['name'])}</b>\n\n"
        f"Number: <b>{res['number']}</b>\n"
        f"Name: {e(u.get('name', '?'))}\n"
        f"Username: {'@' + e(u['username']) if u.get('username') else '—'}\n"
        f"Phone: {e(u.get('phone') or '—')}\n"
        f"Telegram ID: <code>{u.get('id')}</code>",
        parse_mode=HTML,
    )
    try:
        await ctx.bot.send_message(
            u["id"], t(u.get("lang", "en"), "winner_user", num=res["number"], car=e(car["name"])), parse_mode=HTML
        )
    except (TelegramError, KeyError, TypeError):
        pass


@admin_only
async def cmd_broadcast(update, ctx):
    text = cmd_arg(update.effective_message.text)
    if not text:
        await update.effective_message.reply_text("Usage: /broadcast <text>")
        return
    ids = await db.all_user_ids()
    admin_chat = update.effective_chat.id

    async def run():
        ok = bad = 0
        for uid in ids:
            try:
                await ctx.bot.send_message(uid, text)
                ok += 1
            except TelegramError:
                bad += 1
            await asyncio.sleep(0.05)
        await ctx.bot.send_message(admin_chat, f"📢 Broadcast done: {ok} delivered, {bad} failed.")

    ctx.application.create_task(run())
    await update.effective_message.reply_text(f"📢 Broadcasting to {len(ids)} users…")


# ------------------------------------------------- admin panel (buttons)
def _btn(text, data):
    return InlineKeyboardButton(text, callback_data=data)


async def panel_view():
    cars = await db.all_cars()
    if not cars:
        return "🛠 <b>Admin panel</b>\n\nNo cars yet. Use /addcar.", None
    parts = ["🛠 <b>Admin panel</b>"]
    rows = []
    for c in cars:
        st = await db.car_stats(c["id"])
        state = "DRAWN" if c["drawn"] else ("active" if c["active"] else "hidden")
        parts.append(
            f"\n#{c['id']} <b>{e(c['name'])}</b> [{state}]\n"
            f"📆 {fmt_date(c['deadline'])}\n"
            f"✅ sold {st['sold']} · ⏳ pending {st['pending']} · 🕒 held {st['reserved']} / {fmt(c['total'])}"
        )
        rows.append([_btn(f"📅 Date #{c['id']}", f"pnl:date:{c['id']}"), _btn(f"🧹 Reset #{c['id']}", f"pnl:reset:{c['id']}")])
    return "\n".join(parts), InlineKeyboardMarkup(rows)


def date_menu_view(car, note=""):
    cid = car["id"]
    text = (
        f"📅 <b>{e(car['name'])}</b>\nDraw date: <b>{fmt_date(car['deadline'])}</b>\n"
        f"⏳ {countdown('en', car['deadline'])}\n\nExtend by:"
    )
    if note:
        text = note + "\n\n" + text
    kb = InlineKeyboardMarkup(
        [
            [_btn("+1 day", f"pnl:ext:{cid}:1"), _btn("+3 days", f"pnl:ext:{cid}:3")],
            [_btn("+7 days", f"pnl:ext:{cid}:7"), _btn("+30 days", f"pnl:ext:{cid}:30")],
            [_btn("✏️ Type an exact date", f"pnl:set:{cid}")],
            [_btn("⬅️ Panel", "pnl:main")],
        ]
    )
    return text, kb


async def handle_date_input(update, ctx, car_id):
    msg = update.effective_message
    car = await db.get_car(car_id)
    if not car or car["drawn"]:
        ctx.user_data.pop("await_date", None)
        await msg.reply_text("This car is not available for a date change (drawn or removed).")
        return
    try:
        ts = parse_dt(msg.text)
    except ValueError:
        await msg.reply_text("Invalid date. Send it like 2026-12-31 20:00 (or /panel to cancel).")
        return
    if ts <= time.time():
        await msg.reply_text("The new date must be in the future. Try again (or /panel to cancel).")
        return
    ctx.user_data.pop("await_date", None)
    await db.set_car_field(car_id, "deadline", ts)
    await msg.reply_text(f"✅ {car['name']}: draw date is now {fmt_date(ts)}.")
    text, kb = await panel_view()
    await msg.reply_text(text, parse_mode=HTML, reply_markup=kb)


def csv_file(rows, filename):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["number", "status", "name", "username", "phone", "telegram_id"])
    for r in rows:
        w.writerow([r["number"], r["status"], r["name"], r["username"] or "", r["phone"] or "", r["uid"]])
    data = io.BytesIO(buf.getvalue().encode("utf-8-sig"))
    data.name = filename
    return data


async def cb_panel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    admin = update.effective_user
    if not is_admin(admin.id):
        await q.answer("Not authorized.", show_alert=True)
        return
    parts = q.data.split(":")
    action = parts[1]

    if action == "main":
        ctx.user_data.pop("await_date", None)
        await q.answer()
        text, kb = await panel_view()
        await safe_edit(q, text, kb)
        return

    car = await db.get_car(int(parts[2])) if len(parts) > 2 else None
    if not car:
        await q.answer("Car not found.", show_alert=True)
        return

    if action == "date":
        await q.answer()
        text, kb = date_menu_view(car)
        await safe_edit(q, text, kb)

    elif action == "ext":
        if car["drawn"]:
            await q.answer("Already drawn: reset the car first.", show_alert=True)
            return
        days = int(parts[3])
        if days not in (1, 3, 7, 30):
            await q.answer()
            return
        base = max(car["deadline"], int(time.time()))  # extend from the old date, or from now if it already passed
        new_deadline = base + days * 86400
        await db.set_car_field(car["id"], "deadline", new_deadline)
        await q.answer("Updated ✅")
        car["deadline"] = new_deadline
        text, kb = date_menu_view(car, f"✅ Extended by {days} day(s).")
        await safe_edit(q, text, kb)

    elif action == "set":
        if car["drawn"]:
            await q.answer("Already drawn: reset the car first.", show_alert=True)
            return
        ctx.user_data["await_date"] = car["id"]
        await q.answer()
        await ctx.bot.send_message(
            q.message.chat_id,
            f"✏️ Send the new draw date for <b>{e(car['name'])}</b> as <code>YYYY-MM-DD HH:MM</code> "
            f"(UTC{config.TZ_OFFSET_HOURS:+d}), e.g. <code>2026-12-31 20:00</code>.\nSend /panel to cancel.",
            parse_mode=HTML,
        )

    elif action == "reset":
        await q.answer()
        st = await db.car_stats(car["id"])
        warn = ""
        if st["sold"] and not car["drawn"]:
            warn = f"\n⚠️ The draw has NOT been done and {st['sold']} paid ticket(s) will be erased."
        text = (
            f"🧹 <b>Reset {e(car['name'])}?</b>\n\n"
            f"✅ sold: {st['sold']}\n⏳ waiting confirmation: {st['pending']}\n🕒 held (unpaid): {st['reserved']}\n\n"
            f"ALL numbers of this car become available again for the next round. "
            f"Waiting payments are cancelled and those customers are told. "
            f"A CSV backup of the tickets is sent to you first. Other cars are not touched.{warn}"
        )
        kb = InlineKeyboardMarkup(
            [[_btn("✅ Yes, reset", f"pnl:rst:{car['id']}"), _btn("❌ Cancel", "pnl:main")]]
        )
        await safe_edit(q, text, kb)

    elif action == "rst":
        await q.answer("Working…")
        rows = await db.sold_rows(car["id"])
        try:  # backup first: if it cannot be delivered, nothing is erased
            await ctx.bot.send_document(
                q.message.chat_id,
                csv_file(rows, f"backup_{car_slug(car['name'])}_{int(time.time())}.csv"),
                caption=f"Backup before reset: {len(rows)} ticket(s)",
            )
        except TelegramError as ex:
            log.warning("backup failed: %s", ex)
            await safe_edit(q, "❌ Could not send the CSV backup, so nothing was reset. Try again.", None)
            return
        res = await db.reset_car(car["id"], admin.id)
        for p in res["pending_payments"]:
            try:
                await ctx.bot.send_message(
                    p["user_id"],
                    t(p["lang"], "rejected_user", car=e(p["car_name"]), nums=nums_str(p["numbers"])),
                    parse_mode=HTML,
                )
            except TelegramError:
                pass
            for m in await db.admin_msgs(p["id"]):
                try:
                    await ctx.bot.edit_message_caption(
                        chat_id=m["chat_id"], message_id=m["message_id"],
                        caption=admin_caption(p, "🧹 CANCELLED: round was reset"), parse_mode=HTML, reply_markup=None,
                    )
                except TelegramError:
                    pass
        log.info("Car %s reset by admin %s: %s", car["id"], admin.id, res["removed"])
        rm = res["removed"]
        text = (
            f"✅ <b>{e(car['name'])}</b> is reset: all numbers are available again.\n"
            f"Cleared: sold {rm['sold']}, waiting {rm['pending']}, held {rm['reserved']}.\n\n"
            f"Current draw date: {fmt_date(car['deadline'])}\nSet a new one for the next round:"
        )
        kb = InlineKeyboardMarkup([[_btn(f"📅 Set date #{car['id']}", f"pnl:date:{car['id']}")], [_btn("⬅️ Panel", "pnl:main")]])
        await safe_edit(q, text, kb)


# ============================================================ background
async def sweeper(app: Application):
    """Release expired reservations and tell the users."""
    while True:
        await asyncio.sleep(30)
        try:
            for uid, lang, car_name, nums in await db.sweep_expired():
                try:
                    await app.bot.send_message(
                        uid, t(lang, "expired_user", car=e(car_name), nums=nums_str(nums)), parse_mode=HTML
                    )
                except TelegramError:
                    pass
        except Exception:  # keep the loop alive no matter what
            log.exception("sweeper error")


async def post_init(app: Application):
    app.bot_data["sweeper_task"] = asyncio.create_task(sweeper(app))
    me = await app.bot.get_me()
    log.info("Bot started as @%s", me.username)


async def on_error(update, ctx: ContextTypes.DEFAULT_TYPE):
    log.error("Unhandled error", exc_info=ctx.error)


# ================================================================ main
def build_app() -> Application:
    app = Application.builder().token(config.BOT_TOKEN).post_init(post_init).concurrent_updates(16).build()
    pm = filters.ChatType.PRIVATE

    for name, fn in [
        ("start", cmd_start), ("myid", cmd_myid), ("cancel", cmd_cancel),
        ("admin", cmd_admin), ("panel", cmd_panel), ("addcar", cmd_addcar), ("cars", cmd_cars),
        ("setdeadline", cmd_setdeadline), ("setprice", cmd_setprice),
        ("settotal", cmd_settotal), ("togglecar", cmd_togglecar),
        ("pending", cmd_pending), ("stats", cmd_stats), ("export", cmd_export),
        ("draw", cmd_draw), ("broadcast", cmd_broadcast),
    ]:
        app.add_handler(CommandHandler(name, fn, filters=pm))

    app.add_handler(CallbackQueryHandler(cb_lang, pattern=r"^lang:"))
    app.add_handler(CallbackQueryHandler(cb_car, pattern=r"^car:\d+$"))
    app.add_handler(CallbackQueryHandler(cb_continue, pattern=r"^cont:\d+$"))
    app.add_handler(CallbackQueryHandler(cb_ranges, pattern=r"^ranges:\d+$"))
    app.add_handler(CallbackQueryHandler(cb_range_pick, pattern=r"^rng:\d+:\d+:[01]$"))
    app.add_handler(CallbackQueryHandler(cb_pick, pattern=r"^pick:\d+:\d+$"))
    app.add_handler(CallbackQueryHandler(cb_unpick, pattern=r"^unpick:\d+:\d+$"))
    app.add_handler(CallbackQueryHandler(cb_taken, pattern=r"^taken$"))
    app.add_handler(CallbackQueryHandler(cb_back, pattern=r"^back:(cars|menu)$"))
    app.add_handler(CallbackQueryHandler(cb_admin, pattern=r"^adm:(ok|no):\d+$"))
    app.add_handler(CallbackQueryHandler(cb_panel, pattern=r"^pnl:(main|date:\d+|ext:\d+:\d+|set:\d+|reset:\d+|rst:\d+)$"))

    app.add_handler(MessageHandler(pm & filters.TEXT & filters.Text(list(MENU_LABELS)), on_menu))
    app.add_handler(MessageHandler(pm & filters.CONTACT, on_contact))
    app.add_handler(MessageHandler(pm & (filters.PHOTO | filters.Document.IMAGE), on_media))
    app.add_handler(MessageHandler(pm & filters.TEXT & ~filters.COMMAND, on_text))
    app.add_error_handler(on_error)
    return app


def main():
    if not config.BOT_TOKEN:
        raise SystemExit("BOT_TOKEN is missing. Copy .env.example to .env and fill it in.")
    if not config.DATABASE_URL:
        raise SystemExit("DATABASE_URL is missing. Put your PostgreSQL URL in .env.")
    if not config.ADMIN_IDS:
        log.warning("ADMIN_IDS is empty: nobody can approve payments. Send /myid to the bot, put the number in .env and restart.")
    if not (config.CBE_ACCOUNT or config.TELEBIRR_NUMBER):
        log.warning("No payment details configured (CBE_ACCOUNT / TELEBIRR_NUMBER).")
    dbsync.init(config.DATABASE_URL)
    seed_cars()
    build_app().run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
