"""Tests against a real PostgreSQL (set TEST_DATABASE_URL; WARNING: drops the lottery tables there)."""
import asyncio, os, time
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, MagicMock, patch

URL = os.environ.get("TEST_DATABASE_URL", "postgresql://postgres@127.0.0.1:5544/lottery")
os.environ.update(BOT_TOKEN="123:TEST", ADMIN_IDS="999", CBE_ACCOUNT="1000TEST Name", DATABASE_URL=URL)

import config, db as dbsync, bot
from bot import db  # async proxy

dbsync.init(URL)
with dbsync._pool.connection() as c:
    c.execute("DROP TABLE IF EXISTS users, cars, payments, tickets, admin_msgs")
dbsync.init(URL)  # recreate schema (also proves init is idempotent)
dbsync.init(URL)
now = int(time.time())
S = dbsync  # sync access for the DB-level tests


def expire(where, params):
    with S._pool.connection() as c:
        c.execute(f"UPDATE tickets SET expires_at=%s WHERE {where}", (now - 5, *params))


# ---------------- DB layer
cid = S.add_car("Jetour", 9000, 4000, None, now + 86400)
u1 = S.upsert_user(1, "Alice", "alice"); assert u1["lang"] == "en" and u1["id"] == 1
S.upsert_user(2, "Bob", None); S.upsert_user(10**11, "BigId", None)
assert S.get_user(10**11)["id"] == 10**11
assert S.reserve(cid, 2, 1, 30, 20) == "ok"
assert S.reserve(cid, 2, 2, 30, 20) == "taken"
assert S.reserve(cid, 4001, 1, 30, 20) == "invalid" and S.reserve(cid, 0, 1, 30, 20) == "invalid"
assert S.reserve(cid, 4000, 1, 30, 20) == "ok"
assert S.reserve(cid, 5, 1, 30, 2) == "limit"
assert S.taken_count(cid) == 2
assert S.bucket_taken(cid, 100) == {0: 1, 40: 1}, S.bucket_taken(cid, 100)
expire("number=2 AND car_id=%s", (cid,))
assert S.taken_count(cid) == 1
assert S.reserve(cid, 2, 2, 30, 20) == "ok"            # expired number can be re-taken
expire("user_id=2", ())
sw = S.sweep_expired(); assert len(sw) == 1 and sw[0][0] == 2 and sw[0][3] == [2] and sw[0][2] == "Jetour", sw
assert S.sweep_expired() == []
pays = S.create_payments(1, "FILE", "photo")
assert len(pays) == 1 and pays[0]["amount"] == 9000 and pays[0]["numbers"] == [4000], pays
pid = pays[0]["payment_id"]
assert S.payment_full(pid)["numbers"] == [4000]
assert S.create_payments(1, "F2", "photo") == []
assert S.reserve(cid, 4000, 2, 30, 20) == "taken"
assert S.user_has_pending(1)
assert S.decide_payment(pid, True, 999) is True and S.decide_payment(pid, False, 999) is False
assert S.car_stats(cid)["sold"] == 1 and not S.user_has_pending(1)
assert S.reserve(cid, 10, 2, 30, 20) == "ok"
p2 = S.create_payments(2, "F", "document")[0]
assert S.decide_payment(p2["payment_id"], False, 999)
assert S.taken_numbers(cid, 1, 100) == set()
S.save_admin_msg(pid, 999, 5); S.save_admin_msg(pid, 999, 6)
assert S.admin_msgs(pid) == [{"chat_id": 999, "message_id": 6}]
assert len(S.sold_rows(cid)) == 1
# concurrency: 12 threads fight for one number -> exactly one winner
import concurrent.futures as cf
for u in range(100, 112): S.upsert_user(u, f"U{u}", None)
with cf.ThreadPoolExecutor(12) as ex:
    res = list(ex.map(lambda u: S.reserve(cid, 777, u, 30, 20), range(100, 112)))
assert res.count("ok") == 1 and res.count("taken") == 11, res
# per-user limit under concurrency
with cf.ThreadPoolExecutor(8) as ex:
    res = list(ex.map(lambda n: S.reserve(cid, 1000 + n, 1, 30, 3), range(8)))
assert res.count("ok") == 2, res  # user 1 already holds... (sold 4000 counts) -> 3 max total
w = S.draw(cid); assert w["number"] == 4000 and w["user"]["id"] == 1
assert S.draw(cid) is None
assert S.get_car(cid)["drawn"] == 1 and S.reserve(cid, 11, 2, 30, 20) == "closed"
print("DB OK")


# ---------------- views + flow (async)
def mk_ctx():
    ctx = MagicMock(); ctx.bot = MagicMock()
    for m in ("send_message", "send_photo", "send_document", "edit_message_caption", "edit_message_text", "delete_message"):
        setattr(ctx.bot, m, AsyncMock(return_value=NS(message_id=77)))
    ctx.args = []; ctx.user_data = {}; return ctx

def mk_cb(uid, data, name="User"):
    q = MagicMock(); q.data = data; q.answer = AsyncMock()
    q.message = MagicMock(chat_id=uid); q.message.delete = AsyncMock()
    q.edit_message_text = AsyncMock(); q.edit_message_reply_markup = AsyncMock()
    return NS(callback_query=q, effective_user=NS(id=uid, full_name=name, username=None),
              effective_chat=NS(id=uid), effective_message=q.message), q

def mk_msg(uid, text=None, photo=False, name="User", caption=None):
    m = MagicMock(); m.text = text; m.caption = caption; m.reply_text = AsyncMock()
    m.photo = [NS(file_id="PH")] if photo else []; m.document = None
    return NS(effective_user=NS(id=uid, full_name=name, username=None), effective_chat=NS(id=uid),
              effective_message=m, callback_query=None), m


async def main_test():
    big = dict(S.get_car(cid)); big["total"] = 4000
    # layouts
    text, kb = await bot.ranges_view(big, "en")
    assert sum(len(r) for r in kb.inline_keyboard) <= 100
    labels = [x.text for r in kb.inline_keyboard for x in r]
    assert labels[:3] == ["0-99", "100-199", "200-299"] and any(l.endswith("4000-4099") for l in labels)
    seen = []
    for b in range(41):
        for h in (0, 1):
            text, kb = await bot.grid_view(big, b, h, "en")
            assert sum(len(r) for r in kb.inline_keyboard) <= 100, (b, h)
            seen += [int(x.text.split()[1]) for r in kb.inline_keyboard for x in r if x.text[0] in "🟢🔴"]
    assert sorted(set(seen)) == list(range(1, 4001))
    text, kb = await bot.grid_view(big, 0, 0, "en")
    assert sum(len(r) for r in kb.inline_keyboard) == 100 and "(0-99)" in text
    assert [x.text for x in kb.inline_keyboard[-2]] == ["🟢 97", "🟢 98", "🟢 99"]
    assert bot.normalize_phone("+251 922 675 655") == "0922675655" and bot.normalize_phone("12345") is None
    print("VIEWS OK")

    ctx = mk_ctx()
    # no cars yet: ordinary user and admin messages
    for a_uid, expect_hint in ((50, False), (999, True)):
        for c in S.all_cars(): S.set_car_field(c["id"], "active", 0)
        await bot.send_car_list(ctx.bot, a_uid, "en")
        sent = ctx.bot.send_message.call_args.args[1]
        assert "No lotteries are open" in sent and ("/addcar" in sent) == expect_hint
    for c in S.all_cars(): S.set_car_field(c["id"], "active", 1)

    car = S.add_car("Flow Car", 9000, 4000, None, now + 86400)
    up, m = mk_msg(50, "/start", name="Carol"); await bot.cmd_start(up, ctx)
    up, q = mk_cb(50, "lang:am", "Carol"); await bot.cb_lang(up, ctx)
    assert S.get_user(50)["lang"] == "am"
    up, m = mk_msg(50, "📝 ይመዝገቡ"); await bot.on_menu(up, ctx)
    assert "ሽልማትዎን" in ctx.bot.send_message.call_args.args[1]
    up, q = mk_cb(50, f"car:{car}"); await bot.cb_car(up, ctx)
    up, q = mk_cb(50, f"cont:{car}"); await bot.cb_continue(up, ctx)
    up, q = mk_cb(50, f"rng:{car}:0:0"); await bot.cb_range_pick(up, ctx)
    up, q = mk_cb(50, f"pick:{car}:2"); await bot.cb_pick(up, ctx)
    txt = ctx.bot.send_message.call_args.args[1]; assert "9,000" in txt and "1000TEST" in txt
    up, q = mk_cb(51, f"pick:{car}:2", "Dan"); await bot.cb_pick(up, ctx)
    assert q.answer.call_args.kwargs.get("show_alert") is True
    ctx.bot.send_photo.reset_mock()
    up, m = mk_msg(50, photo=True); await bot.on_media(up, ctx)
    assert ctx.bot.send_photo.call_args.args[0] == 999 and m.reply_text.call_count == 2
    up, m = mk_msg(50, "0922675655"); await bot.on_text(up, ctx)
    assert S.get_user(50)["phone"] == "0922675655"
    pid = S.pending_payment_ids()[-1]
    up, q = mk_cb(999, f"adm:ok:{pid}", "Admin"); await bot.cb_admin(up, ctx)
    assert ctx.bot.send_message.call_args.args[0] == 50
    assert S.car_stats(car)["sold"] == 1
    up, q = mk_cb(999, f"adm:no:{pid}", "Admin"); await bot.cb_admin(up, ctx)
    assert q.answer.call_args.args[0] == "Already processed."
    up, q = mk_cb(50, f"adm:ok:{pid}"); await bot.cb_admin(up, ctx)
    assert q.answer.call_args.args[0] == "Not authorized."
    up, m = mk_msg(50, "🎟 ቁጥሮቼ"); await bot.on_menu(up, ctx)
    up, m = mk_msg(50, "📅 የዕጣ ቀን"); await bot.on_menu(up, ctx)
    up, m = mk_msg(999, "/addcar Test | 1000 | 100 | 2030-01-01 10:00"); await bot.cmd_addcar(up, ctx)
    assert "created" in m.reply_text.call_args.args[0], m.reply_text.call_args
    up, m = mk_msg(999, photo=True, caption="/addcar PicCar | 1000 | 100 | 2030-01-01 10:00"); await bot.on_media(up, ctx)
    assert "created" in m.reply_text.call_args.args[0] and S.all_cars()[-1]["photo"] == "PH"
    last = S.all_cars()[-1]["id"]
    up, m = mk_msg(999, photo=True, caption=f"/setphoto {last}"); await bot.on_media(up, ctx)
    ctx.args = [str(last), "2031-02-02 12:00"]; up, m = mk_msg(999, "/setdeadline"); await bot.cmd_setdeadline(up, ctx)
    ctx.args = [str(last), "1500"]; up, m = mk_msg(999, "/setprice"); await bot.cmd_setprice(up, ctx)
    ctx.args = [str(last), "200"]; up, m = mk_msg(999, "/settotal"); await bot.cmd_settotal(up, ctx)
    ctx.args = [str(last)]; up, m = mk_msg(999, "/togglecar"); await bot.cmd_togglecar(up, ctx)
    up, m = mk_msg(999, "/cars"); await bot.cmd_cars(up, ctx)
    up, m = mk_msg(999, "/stats"); await bot.cmd_stats(up, ctx)
    up, m = mk_msg(999, "/pending"); await bot.cmd_pending(up, ctx)
    up, m = mk_msg(999, "/admin"); await bot.cmd_admin(up, ctx)
    ctx.args = [str(car), "confirm"]; up, m = mk_msg(999, "/draw"); await bot.cmd_draw(up, ctx)
    assert S.get_car(car)["winner_number"] == 2
    ctx.args = [str(car)]; up, m = mk_msg(999, "/export"); m.reply_document = AsyncMock(); await bot.cmd_export(up, ctx)
    m.reply_document.assert_awaited()
    up, m = mk_msg(50, "/cancel"); await bot.cmd_cancel(up, ctx)
    up, m = mk_msg(50, "/admin"); await bot.cmd_admin(up, ctx)
    assert "Not authorized" in m.reply_text.call_args.args[0]
    # reservation expiry notification
    S.upsert_user(60, "Eve", None)
    car2 = S.add_car("Exp Car", 100, 50, None, now + 5000)
    assert S.reserve(car2, 7, 60, 30, 20) == "ok"; expire("user_id=60", ())
    app = MagicMock(); app.bot = ctx.bot
    task = asyncio.create_task(bot.sweeper(app))
    with patch("asyncio.sleep", AsyncMock(side_effect=[None, asyncio.CancelledError()])):
        try: await task
        except asyncio.CancelledError: pass
    assert ctx.bot.send_message.call_args.args[0] == 60
    print("FLOW OK")

asyncio.run(main_test())

# ---------------- multi-select, pay once
async def multi_test():
    ctx = mk_ctx()
    car = S.add_car("Multi Car", 9000, 4000, None, now + 86400)
    car2 = S.add_car("Second Car", 5000, 4000, None, now + 86400)
    for u in (80, 81, 82): S.upsert_user(u, f"M{u}", None)
    for n in (3, 4, 5):
        up, q = mk_cb(80, f"pick:{car}:{n}"); await bot.cb_pick(up, ctx)
    # ONE payment message, then edited in place (not 3 separate messages)
    assert ctx.bot.send_message.call_count == 1 and ctx.bot.edit_message_text.call_count == 2
    txt = ctx.bot.edit_message_text.call_args.args[0]
    assert "3, 4, 5" in txt and "27,000" in txt and "1000TEST" in txt, txt
    c = S.get_car(car)
    text, kb = await bot.grid_view(c, 0, 0, "en", 80)
    labels = [x.text for r in kb.inline_keyboard for x in r]
    assert {"🟡 3", "🟡 4", "🟡 5", "🟢 6"} <= set(labels) and "Your selection" in text
    assert sum(len(r) for r in kb.inline_keyboard) == 100
    text2, kb2 = await bot.grid_view(c, 0, 0, "en", 81)       # another person sees them as taken
    labels2 = [x.text for r in kb2.inline_keyboard for x in r]
    assert {"🔴 3", "🔴 4", "🔴 5"} <= set(labels2) and "Your selection" not in text2
    # somebody else cannot remove my numbers
    up, q = mk_cb(81, f"unpick:{car}:3"); await bot.cb_unpick(up, mk_ctx())   # each user has their own user_data
    assert S.taken_numbers(car, 1, 10) == {3, 4, 5}
    # I can remove one: total drops
    up, q = mk_cb(80, f"unpick:{car}:4"); await bot.cb_unpick(up, ctx)
    assert S.taken_numbers(car, 1, 10) == {3, 5}
    txt = ctx.bot.edit_message_text.call_args.args[0]; assert "3, 5" in txt and "18,000" in txt, txt
    # add a number on a later page + a second car, then pay ONCE
    up, q = mk_cb(80, f"pick:{car}:150"); await bot.cb_pick(up, ctx)
    up, q = mk_cb(80, f"pick:{car2}:1"); await bot.cb_pick(up, ctx)
    ctx.bot.send_photo.reset_mock()
    up, m = mk_msg(80, photo=True); await bot.on_media(up, ctx)
    pays = [S.payment_full(p) for p in S.pending_payment_ids() if S.payment_full(p)["user_id"] == 80]
    by = {p["car_name"]: p for p in pays}
    assert by["Multi Car"]["numbers"] == [3, 5, 150] and by["Multi Car"]["amount"] == 27000
    assert by["Second Car"]["numbers"] == [1] and by["Second Car"]["amount"] == 5000
    assert ctx.bot.send_photo.call_count == 2                       # admin got 2 requests from one screenshot
    assert m.reply_text.call_args_list[0].args[0].startswith("✅ Screenshot")
    assert S.user_reserved(80) == []                                # nothing left unpaid
    # admin approves the 3-number payment -> all three sold, user told all numbers
    up, q = mk_cb(999, f"adm:ok:{by['Multi Car']['id']}", "Admin"); await bot.cb_admin(up, ctx)
    assert S.car_stats(car)["sold"] == 3
    assert "3, 5, 150" in ctx.bot.send_message.call_args.args[1]
    # removing the only number deletes the payment message
    ctx2 = mk_ctx()
    up, q = mk_cb(82, f"pick:{car}:200"); await bot.cb_pick(up, ctx2)
    up, q = mk_cb(82, f"unpick:{car}:200"); await bot.cb_unpick(up, ctx2)
    ctx2.bot.delete_message.assert_awaited(); assert S.taken_numbers(car, 200, 200) == set()
    # message deleted by the user (Back) -> next pick sends a fresh one instead of failing
    from telegram.error import BadRequest
    up, q = mk_cb(82, f"pick:{car}:201"); await bot.cb_pick(up, ctx2)
    ctx2.bot.edit_message_text = AsyncMock(side_effect=BadRequest("Message to edit not found"))
    n_before = ctx2.bot.send_message.call_count
    up, q = mk_cb(82, f"pick:{car}:202"); await bot.cb_pick(up, ctx2)
    assert ctx2.bot.send_message.call_count == n_before + 1
    # per-car limit still enforced
    old = config.MAX_NUMBERS_PER_USER; config.MAX_NUMBERS_PER_USER = 3
    up, q = mk_cb(82, f"pick:{car}:203"); await bot.cb_pick(up, ctx2)     # 4th number (201,202,203 -> 3rd ok)
    up, q = mk_cb(82, f"pick:{car}:204"); await bot.cb_pick(up, ctx2)
    assert q.answer.call_args.kwargs.get("show_alert") is True and 204 not in S.taken_numbers(car, 204, 204)
    config.MAX_NUMBERS_PER_USER = old
    print("MULTI OK")

asyncio.run(multi_test())

# ---------------- admin panel: change date + reset
async def panel_test():
    ctx = mk_ctx(); T = int(time.time())
    A = S.add_car("Panel A", 1000, 300, None, T + 86400)
    B = S.add_car("Panel B", 2000, 300, None, T + 86400)
    for u in (90, 91, 92): S.upsert_user(u, f"P{u}", f"p{u}")
    # panel lists cars with 2 buttons each, callback data short enough
    text, kb = await bot.panel_view()
    datas = [b.callback_data for r in kb.inline_keyboard for b in r]
    assert f"pnl:date:{A}" in datas and f"pnl:reset:{B}" in datas and all(len(d) <= 64 for d in datas)
    # /admin shows help + panel
    up, m = mk_msg(999, "/admin"); await bot.cmd_admin(up, ctx); assert m.reply_text.call_count == 2
    # non-admin cannot use any panel button
    for d in (f"pnl:date:{A}", f"pnl:ext:{A}:7", f"pnl:rst:{A}", "pnl:main"):
        up, q = mk_cb(90, d); await bot.cb_panel(up, ctx)
        assert q.answer.call_args.args[0] == "Not authorized."
    # --- date: quick extend from the old date
    old = S.get_car(A)["deadline"]
    up, q = mk_cb(999, f"pnl:date:{A}"); await bot.cb_panel(up, ctx)
    up, q = mk_cb(999, f"pnl:ext:{A}:7"); await bot.cb_panel(up, ctx)
    assert S.get_car(A)["deadline"] == old + 7 * 86400
    assert S.get_car(B)["deadline"] == old                      # other car untouched
    # a car whose date already passed is closed; extending re-opens it (from NOW)
    S.set_car_field(B, "deadline", T - 3600)
    assert not bot.is_open(S.get_car(B)) and S.reserve(B, 1, 90, 30, 20) == "closed"
    up, q = mk_cb(999, f"pnl:ext:{B}:3"); await bot.cb_panel(up, ctx)
    nb = S.get_car(B)["deadline"]; assert abs(nb - (T + 3 * 86400)) < 5
    assert bot.is_open(S.get_car(B)) and S.reserve(B, 1, 90, 30, 20) == "ok"
    S.release_user_reserved(90)
    up, q = mk_cb(999, f"pnl:ext:{A}:5"); await bot.cb_panel(up, ctx)     # not an allowed step
    assert S.get_car(A)["deadline"] == old + 7 * 86400
    # --- date: type an exact date
    up, q = mk_cb(999, f"pnl:set:{A}"); await bot.cb_panel(up, ctx)
    assert ctx.user_data["await_date"] == A
    up, m = mk_msg(999, "tomorrow"); await bot.on_text(up, ctx)
    assert "Invalid date" in m.reply_text.call_args.args[0] and ctx.user_data.get("await_date") == A
    up, m = mk_msg(999, "2020-01-01 10:00"); await bot.on_text(up, ctx)
    assert "future" in m.reply_text.call_args.args[0]
    up, m = mk_msg(999, "2031-05-06 18:30"); await bot.on_text(up, ctx)
    assert bot.fmt_date(S.get_car(A)["deadline"]).startswith("2031-05-06 18:30") and "await_date" not in ctx.user_data
    # a normal user typing a date is NOT treated as a date change
    ctx_u = mk_ctx(); ctx_u.user_data["await_date"] = A
    before = S.get_car(A)["deadline"]
    up, m = mk_msg(90, "2032-01-01 10:00"); await bot.on_text(up, ctx_u)
    assert S.get_car(A)["deadline"] == before
    # --- reset: build a round on A (sold + pending + held) and B (must stay)
    ctxs = mk_ctx()
    def buy(uid, car, nums):
        for n in nums: assert S.reserve(car, n, uid, 30, 20) == "ok"
    buy(90, A, [1, 2]); p_sold = S.create_payments(90, "F1", "photo")[0]["payment_id"]; S.decide_payment(p_sold, True, 999)
    buy(91, A, [5]);    p_pend = S.create_payments(91, "F2", "photo")[0]["payment_id"]
    S.save_admin_msg(p_pend, 999, 555)
    buy(92, A, [9])                                              # held, unpaid
    buy(90, B, [4]);    p_b = S.create_payments(90, "F3", "photo")[0]["payment_id"]
    st = S.car_stats(A); assert (st["sold"], st["pending"], st["reserved"]) == (2, 1, 1)
    # confirmation screen changes nothing
    up, q = mk_cb(999, f"pnl:reset:{A}"); await bot.cb_panel(up, ctxs)
    shown = q.edit_message_text.call_args.args[0]
    assert "sold: 2" in shown and "NOT been done" in shown and S.car_stats(A)["sold"] == 2
    up, q = mk_cb(999, "pnl:main"); await bot.cb_panel(up, ctxs); assert S.car_stats(A)["sold"] == 2
    # confirm
    ctxs.bot.send_message.reset_mock()
    up, q = mk_cb(999, f"pnl:rst:{A}"); await bot.cb_panel(up, ctxs)
    doc = ctxs.bot.send_document.call_args
    assert doc.args[0] == 999 and b"number,status" in doc.args[1].getvalue() and b",90" in doc.args[1].getvalue() and b"sold" in doc.args[1].getvalue()
    assert S.taken_numbers(A, 1, 300) == set() and S.car_stats(A) == {"sold": 0, "pending": 0, "reserved": 0}
    assert S.payment_full(p_pend)["status"] == "rejected" and S.payment_full(p_sold)["status"] == "archived"
    assert [p for p in S.pending_payment_ids() if S.payment_full(p)["car_id"] in (A, B)] == [p_b]   # only B's payment is still pending
    assert any(c.args[0] == 91 for c in ctxs.bot.send_message.call_args_list)   # waiting customer told
    ctxs.bot.edit_message_caption.assert_awaited()               # admin's old approve-message marked cancelled
    assert S.car_stats(B)["pending"] == 1 and S.taken_numbers(B, 1, 300) == {4}  # other car untouched
    assert S.get_car(A)["active"] == 1 and S.get_car(A)["drawn"] == 0
    assert S.reserve(A, 1, 92, 30, 20) == "ok"                   # numbers can be bought again
    # a drawn car: reset re-opens it and clears the winner; date change is refused until then
    S.release_user_reserved(92)
    buy(90, A, [7]); pid = S.create_payments(90, "F4", "photo")[0]["payment_id"]; S.decide_payment(pid, True, 999)
    assert S.draw(A)["number"] == 7 and S.get_car(A)["drawn"] == 1
    up, q = mk_cb(999, f"pnl:ext:{A}:7"); await bot.cb_panel(up, ctxs)
    assert q.answer.call_args.args[0].startswith("Already drawn")
    up, q = mk_cb(999, f"pnl:rst:{A}"); await bot.cb_panel(up, ctxs)
    ca = S.get_car(A); assert ca["drawn"] == 0 and ca["winner_number"] is None and S.taken_numbers(A, 1, 300) == set()
    assert any(x["name"] == "Panel A" for x in S.active_cars())
    # backup failure -> nothing is erased
    from telegram.error import TelegramError
    buy(90, A, [8]); 
    ctxf = mk_ctx(); ctxf.bot.send_document = AsyncMock(side_effect=TelegramError("down"))
    up, q = mk_cb(999, f"pnl:rst:{A}"); await bot.cb_panel(up, ctxf)
    assert S.taken_numbers(A, 1, 300) == {8} and "nothing was reset" in q.edit_message_text.call_args.args[0]
    print("PANEL OK")

asyncio.run(panel_test())

# ---------------- unlimited tickets per person
async def unlimited_test():
    assert config.MAX_NUMBERS_PER_USER == 0                       # default = unlimited
    ctx = mk_ctx()
    car = S.add_car("Big Buyer Car", 100, 4000, None, now + 86400)
    S.upsert_user(95, "Whale", None)
    # via the real handler for the first 25 (the old limit was 20)
    for n in range(1, 26):
        up, q = mk_cb(95, f"pick:{car}:{n}"); await bot.cb_pick(up, ctx)
        assert not (q.answer.call_args and q.answer.call_args.kwargs.get("show_alert")), n
    assert len(S.user_reserved(95, car)) == 25
    # scattered numbers (worst case for long lists): every 2nd number up to 600 => 300 more
    for n in range(101, 701, 2):
        assert S.reserve(car, n, 95, 30, 0) == "ok"
    assert len(S.user_reserved(95, car)) == 25 + 300
    up, q = mk_cb(95, f"pick:{car}:3001"); await bot.cb_pick(up, ctx)
    mine = S.user_reserved(95, car); assert len(mine) == 326
    text = ctx.bot.edit_message_text.call_args.args[0]
    assert "32,600" in text and len(text) < 4096, len(text)
    # one screenshot -> one payment with all of them; the admin message fits Telegram's 1024-char caption
    ctx.bot.send_photo.reset_mock()
    up, m = mk_msg(95, photo=True); await bot.on_media(up, ctx)
    pid = [p for p in S.pending_payment_ids() if S.payment_full(p)["user_id"] == 95][0]
    pay = S.payment_full(pid)
    assert len(pay["numbers"]) == 326 and pay["amount"] == 32600
    cap = ctx.bot.send_photo.call_args.kwargs["caption"]
    assert len(cap) <= 1024 and "(326)" in cap and "32,600" in cap, len(cap)
    # compact format
    assert bot.nums_str([3, 4, 5]) == "3, 4, 5"                    # small lists unchanged
    assert bot.nums_str(range(1, 41)) == "1-40"
    assert bot.nums_str(list(range(1, 41)) + [55] + list(range(60, 73))) == "1-40, 55, 60-72"
    assert len(bot.nums_str(range(1, 2000, 2))) < 700 and "total" in bot.nums_str(range(1, 2000, 2))
    # approval message for the customer also fits
    up, q = mk_cb(999, f"adm:ok:{pid}", "Admin"); await bot.cb_admin(up, ctx)
    assert len(ctx.bot.send_message.call_args.args[1]) < 4096 and S.car_stats(car)["sold"] == 326
    # my numbers also fits
    up, m = mk_msg(95, "🎟 My numbers"); await bot.on_menu(up, ctx)
    assert len(ctx.bot.send_message.call_args.args[1]) < 4096
    # a cap can still be configured when wanted
    config.MAX_NUMBERS_PER_USER = 2
    S.upsert_user(96, "Capped", None)
    up, q = mk_cb(96, f"pick:{car}:3101"); await bot.cb_pick(up, mk_ctx())
    up, q = mk_cb(96, f"pick:{car}:3102"); await bot.cb_pick(up, mk_ctx())
    up, q = mk_cb(96, f"pick:{car}:3103"); await bot.cb_pick(up, mk_ctx())
    assert q.answer.call_args.kwargs.get("show_alert") is True and len(S.user_reserved(96, car)) == 2
    config.MAX_NUMBERS_PER_USER = 0
    print("UNLIMITED OK")

asyncio.run(unlimited_test())

# ---------------- admin panel: edit + delete a car
async def edit_delete_test():
    ctx = mk_ctx(); T = int(time.time())
    A = S.add_car("Edit A", 1000, 300, None, T + 86400)
    B = S.add_car("Edit B", 2000, 300, None, T + 86400)
    for u in (93, 94, 95): S.upsert_user(u, f"E{u}", f"e{u}")
    text, kb = await bot.panel_view()
    datas = [b.callback_data for r in kb.inline_keyboard for b in r]
    assert f"pnl:edit:{A}" in datas and f"pnl:del:{B}" in datas and all(len(d) <= 64 for d in datas)
    import re as _re
    pat = _re.compile(r"^pnl:(main|date:\d+|ext:\d+:\d+|set:\d+|reset:\d+|rst:\d+|edit:\d+|fld:\d+:(name|price|total)|pic:\d+|vis:\d+|del:\d+|dlt:\d+)$")
    text, kb = bot.edit_view(S.get_car(A))
    edatas = [b.callback_data for r in kb.inline_keyboard for b in r]
    assert all(pat.match(d) and len(d) <= 64 for d in edatas + datas), edatas
    # non-admin cannot use the new buttons
    for d in (f"pnl:edit:{A}", f"pnl:fld:{A}:price", f"pnl:pic:{A}", f"pnl:vis:{A}", f"pnl:del:{A}", f"pnl:dlt:{A}"):
        up, q = mk_cb(93, d); await bot.cb_panel(up, ctx)
        assert q.answer.call_args.args[0] == "Not authorized."
    assert S.get_car(A) is not None and S.get_car(A)["active"] == 1
    # --- edit menu
    up, q = mk_cb(999, f"pnl:edit:{A}"); await bot.cb_panel(up, ctx)
    assert "Edit A" in q.edit_message_text.call_args.args[0]
    # --- price
    up, q = mk_cb(999, f"pnl:fld:{A}:price"); await bot.cb_panel(up, ctx)
    assert ctx.user_data["await_edit"] == (A, "price")
    up, m = mk_msg(999, "abc"); await bot.on_text(up, ctx)
    assert S.get_car(A)["price"] == 1000 and ctx.user_data.get("await_edit") == (A, "price")
    up, m = mk_msg(999, "0"); await bot.on_text(up, ctx); assert S.get_car(A)["price"] == 1000
    up, m = mk_msg(999, "2,500"); await bot.on_text(up, ctx)
    assert S.get_car(A)["price"] == 2500 and "await_edit" not in ctx.user_data
    assert "updated" in m.reply_text.call_args.args[0]
    assert S.get_car(B)["price"] == 2000                                  # other car untouched
    # --- name
    up, q = mk_cb(999, f"pnl:fld:{A}:name"); await bot.cb_panel(up, ctx)
    up, m = mk_msg(999, "x" * 61); await bot.on_text(up, ctx); assert S.get_car(A)["name"] == "Edit A"
    up, m = mk_msg(999, "Renamed A"); await bot.on_text(up, ctx)
    assert S.get_car(A)["name"] == "Renamed A" and "await_edit" not in ctx.user_data
    # --- total tickets (cannot go below a taken number, nor above the max)
    assert S.reserve(A, 50, 93, 30, 20) == "ok"
    up, q = mk_cb(999, f"pnl:fld:{A}:total"); await bot.cb_panel(up, ctx)
    up, m = mk_msg(999, "40"); await bot.on_text(up, ctx)
    assert S.get_car(A)["total"] == 300 and "Cannot go below 50" in m.reply_text.call_args.args[0]
    up, m = mk_msg(999, str(config.MAX_TOTAL + 1)); await bot.on_text(up, ctx); assert S.get_car(A)["total"] == 300
    up, m = mk_msg(999, "500"); await bot.on_text(up, ctx); assert S.get_car(A)["total"] == 500
    S.release_user_reserved(93)
    # a normal user typing is not treated as an edit
    ctx_u = mk_ctx(); ctx_u.user_data["await_edit"] = (A, "price")
    up, m = mk_msg(93, "7"); await bot.on_text(up, ctx_u); assert S.get_car(A)["price"] == 2500
    # /panel and Panel button cancel pending input
    up, q = mk_cb(999, f"pnl:fld:{A}:price"); await bot.cb_panel(up, ctx)
    up, m = mk_msg(999, "/panel"); await bot.cmd_panel(up, ctx); assert "await_edit" not in ctx.user_data
    up, q = mk_cb(999, f"pnl:pic:{A}"); await bot.cb_panel(up, ctx); assert ctx.user_data["await_photo"] == A
    up, q = mk_cb(999, "pnl:main"); await bot.cb_panel(up, ctx); assert "await_photo" not in ctx.user_data
    # bad field in callback is ignored
    up, q = mk_cb(999, f"pnl:fld:{A}:drawn"); await bot.cb_panel(up, ctx); assert "await_edit" not in ctx.user_data
    # --- hide / show
    up, q = mk_cb(999, f"pnl:vis:{A}"); await bot.cb_panel(up, ctx)
    assert S.get_car(A)["active"] == 0 and all(c["id"] != A for c in S.active_cars())
    up, q = mk_cb(999, f"pnl:vis:{A}"); await bot.cb_panel(up, ctx); assert S.get_car(A)["active"] == 1
    # --- picture
    up, q = mk_cb(999, f"pnl:pic:{A}"); await bot.cb_panel(up, ctx)
    up, m = mk_msg(999); m.document = NS(file_id="DOC"); await bot.on_media(up, ctx)       # a file is refused
    assert S.get_car(A)["photo"] is None and ctx.user_data.get("await_photo") == A
    up, m = mk_msg(999, photo=True); await bot.on_media(up, ctx)
    ca = S.get_car(A); assert ca["photo"] == "PH" and ca["photo_hash"] == "manual" and "await_photo" not in ctx.user_data
    # --- delete: build sold + pending + held on A, and a payment on B
    def buy(uid, car, nums):
        for n in nums: assert S.reserve(car, n, uid, 30, 20) == "ok"
    buy(93, A, [1, 2]); p_sold = S.create_payments(93, "G1", "photo")[0]["payment_id"]; S.decide_payment(p_sold, True, 999)
    buy(94, A, [5]);    p_pend = S.create_payments(94, "G2", "photo")[0]["payment_id"]
    S.save_admin_msg(p_pend, 999, 556)
    buy(95, A, [9])
    buy(93, B, [4]);    p_b = S.create_payments(93, "G3", "photo")[0]["payment_id"]
    ctxd = mk_ctx()
    up, q = mk_cb(999, f"pnl:del:{A}"); await bot.cb_panel(up, ctxd)           # confirmation changes nothing
    shown = q.edit_message_text.call_args.args[0]
    assert "sold: 2" in shown and "NOT been done" in shown and S.get_car(A) is not None
    # backup failure -> nothing deleted
    from telegram.error import TelegramError
    ctxf = mk_ctx(); ctxf.bot.send_document = AsyncMock(side_effect=TelegramError("down"))
    up, q = mk_cb(999, f"pnl:dlt:{A}"); await bot.cb_panel(up, ctxf)
    assert S.get_car(A) is not None and S.car_stats(A)["sold"] == 2 and "nothing was deleted" in q.edit_message_text.call_args.args[0]
    # confirm
    up, q = mk_cb(999, f"pnl:dlt:{A}"); await bot.cb_panel(up, ctxd)
    doc = ctxd.bot.send_document.call_args
    assert doc.args[0] == 999 and b"number,status" in doc.args[1].getvalue() and b"sold" in doc.args[1].getvalue()
    assert S.get_car(A) is None and all(c["id"] != A for c in S.all_cars())
    assert S.taken_numbers(A, 1, 300) == set() and S.payment_full(p_sold) is None and S.payment_full(p_pend) is None
    assert any(c.args[0] == 94 for c in ctxd.bot.send_message.call_args_list)       # waiting customer told
    ctxd.bot.edit_message_caption.assert_awaited()
    assert "was deleted" in q.edit_message_text.call_args.args[0]
    # other car untouched
    assert S.get_car(B) is not None and S.car_stats(B)["pending"] == 1 and S.payment_full(p_b)["status"] == "pending"
    assert S.get_car(B)["price"] == 2000 and S.taken_numbers(B, 1, 300) == {4}
    assert S.pending_payment_ids().count(p_pend) == 0
    # a customer using the deleted car gets a clean "closed", and a stale button does not crash the panel
    assert S.reserve(A, 3, 95, 30, 20) == "closed"
    up, q = mk_cb(999, f"pnl:edit:{A}"); await bot.cb_panel(up, ctxd); assert q.answer.call_args.args[0] == "Car not found."
    up, q = mk_cb(999, f"pnl:dlt:{A}"); await bot.cb_panel(up, ctxd); assert q.answer.call_args.args[0] == "Car not found."
    # the panel and /cars still work
    text, kb = await bot.panel_view(); assert "Edit B" in text and "Renamed A" not in text
    up, m = mk_msg(999, "/cars"); await bot.cmd_cars(up, ctxd)
    # deleting a DRAWN car works too; deleting the last cars leaves a usable panel
    buy(93, B, [7]); pid = S.create_payments(93, "G5", "photo")[0]["payment_id"]
    S.decide_payment(pid, True, 999); S.decide_payment(p_b, True, 999)
    assert S.draw(B) is not None
    up, q = mk_cb(999, f"pnl:dlt:{B}"); await bot.cb_panel(up, ctxd); assert S.get_car(B) is None
    print("EDIT+DELETE OK")

asyncio.run(edit_delete_test())

# ---------------- seeding + photos
with S._pool.connection() as c:
    c.execute("TRUNCATE cars, tickets, payments, admin_msgs RESTART IDENTITY")
assert bot.seed_cars() == 1 and bot.seed_cars() == 0           # only once
cars = S.all_cars()
assert [c["name"] for c in cars] == ["Jetour"], cars
assert all(c["price"] == 3000 and c["total"] == 4000 and c["active"] == 1 for c in cars)
assert all(6.9 * 86400 < c["deadline"] - time.time() < 7.1 * 86400 for c in cars)
assert bot.car_slug("Jetour") == "jetour"
assert [bool(bot.find_car_photo(c)) for c in cars] == [True]    # photos/jetour.jpg is shipped
# photo tests use a temporary photos folder with a generated jetour.jpg
import tempfile, pathlib
from PIL import Image
_tmp = pathlib.Path(tempfile.mkdtemp())
Image.new("RGB", (64, 48), (200, 30, 30)).save(_tmp / "jetour.jpg", "JPEG")
bot.config.PHOTOS_DIR = _tmp
assert [bool(bot.find_car_photo(c)) for c in cars] == [True]
for c in cars:
    im = Image.open(bot.find_car_photo(c)); assert im.format == "JPEG" and sum(im.size) < 10000 and max(im.size) / min(im.size) < 20


async def photo_test():
    ctx = mk_ctx()
    ctx.bot.send_photo = AsyncMock(return_value=NS(message_id=1, photo=[NS(file_id="small"), NS(file_id="FILEID-1")]))
    byname = {c["name"]: c for c in S.all_cars()}
    sino = byname["Jetour"]
    # the car uploads its own local picture the first time
    for car in (sino,):
        ctx.bot.send_photo.reset_mock()
        up, q = mk_cb(70, f"car:{car['id']}"); await bot.cb_car(up, ctx)
        sent = ctx.bot.send_photo.call_args.args[1]
        assert sent == bot.find_car_photo(car).read_bytes(), car["name"]
        assert car["name"] in ctx.bot.send_photo.call_args.kwargs["caption"]
        assert "Sold: <b>0</b>" in ctx.bot.send_photo.call_args.kwargs["caption"]
        row = S.get_car(car["id"]); assert row["photo"] == "FILEID-1" and row["photo_hash"] == bot.file_digest(bot.find_car_photo(car))
    # second view reuses the stored file_id (no re-upload)
    up, q = mk_cb(71, f"car:{sino['id']}"); await bot.cb_car(up, ctx)
    assert ctx.bot.send_photo.call_args.args[1] == "FILEID-1"
    # STALE CACHE (e.g. old picture from an earlier version): local file wins -> re-upload
    S.set_car_photo(sino["id"], "OLD-FILE-ID", "someoldhash")
    up, q = mk_cb(71, f"car:{sino['id']}"); await bot.cb_car(up, ctx)
    assert isinstance(ctx.bot.send_photo.call_args.args[1], bytes)
    assert S.get_car(sino["id"])["photo"] == "FILEID-1"
    S.set_car_photo(sino["id"], "OLD-FILE-ID", None)       # pre-upgrade rows have no hash
    up, q = mk_cb(71, f"car:{sino['id']}"); await bot.cb_car(up, ctx)
    assert isinstance(ctx.bot.send_photo.call_args.args[1], bytes)
    # admin-chosen picture (/setphoto) is never overridden by the folder
    up, m = mk_msg(999, photo=True, caption=f"/setphoto {sino['id']}"); await bot.on_media(up, ctx)
    assert S.get_car(sino["id"])["photo_hash"] == "manual" and S.get_car(sino["id"])["photo"] == "PH"
    up, q = mk_cb(71, f"car:{sino['id']}"); await bot.cb_car(up, ctx)
    assert ctx.bot.send_photo.call_args.args[1] == "PH"
    # car without a local picture uses its stored file_id, or a text card
    nopic = S.add_car("No Pic Car", 100, 50, None, now + 5000)
    ctx.bot.send_message.reset_mock()
    up, q = mk_cb(71, f"car:{nopic}"); await bot.cb_car(up, ctx)
    assert "No Pic Car" in ctx.bot.send_message.call_args.args[1]
    # upload failure falls back to a text card instead of crashing
    from telegram.error import BadRequest
    S.set_car_photo(sino["id"], None, None)
    ctx.bot.send_photo = AsyncMock(side_effect=BadRequest("boom"))
    ctx.bot.send_message.reset_mock()
    up, q = mk_cb(71, f"car:{sino['id']}"); await bot.cb_car(up, ctx)
    assert "Jetour" in ctx.bot.send_message.call_args.args[1]
    # Register lists the car
    ctx2 = mk_ctx()
    S.set_car_field(nopic, "active", 0)
    await bot.send_car_list(ctx2.bot, 70, "en")
    kb = ctx2.bot.send_message.call_args.kwargs["reply_markup"]
    assert [r[0].text for r in kb.inline_keyboard[:1]] == ["🚗 Jetour"]
    print("SEED+PHOTO OK")

asyncio.run(photo_test())

with patch("telegram.ext.Application.run_polling") as rp:
    bot.main(); assert rp.called
print("STARTUP OK")
