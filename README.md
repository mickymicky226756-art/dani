# Car Lottery Telegram Bot

Telegram bot for a car raffle: language choice (አማርኛ / English / Afaan Oromo), car picker with
live availability + countdown, number grid (🟢 free / 🔴 taken), 30-minute reservation,
payment-screenshot upload, phone capture, and admin approval.

## Setup (Python 3.10+)
```bash
pip install -r requirements.txt
# edit .env  -> set DATABASE_URL, ADMIN_IDS, CBE_ACCOUNT, TELEBIRR_NUMBER
python bot.py
```
1. Start the bot, send `/myid`, put that number in `ADMIN_IDS` in `.env`, restart.
2. Admins must press /start on the bot once so it can DM them payment screenshots.
3. The three cars in `cars_seed.json` (BYD LEOPARD 8, FSR, Sino Cassoni) are created automatically the first time the bot starts on an empty database. Edit that file BEFORE the first start to change prices / ticket counts / draw time. Afterwards use /setprice, /settotal, /setdeadline.
   Car pictures: put `byd-leopard-8.jpg`, `fsr.jpg`, `sino-cassoni.jpg` in the `photos` folder (see photos/README.txt).
   To create another car manually (send a photo with this caption):
   `/addcar Sino Cassoni | 9000 | 4000 | 2026-11-01 20:00`
   (name | price in birr | number of tickets | draw date/time, UTC+3 by default)

## Customer flow (several numbers, one payment)
A customer can tap as many numbers as they like (unlimited by default; set `MAX_NUMBERS_PER_USER` in `.env` to a number to cap it). Their own numbers turn 🟡 (tap again to remove). One payment message lists all numbers and the total and is updated in place; one screenshot pays for everything and the admin approves it once.

## Customer flow
/start → language → **Register** → pick car → **Continue** → pick range → pick number →
number held for 30 min + payment info → send screenshot → send phone → admin approves → numbers confirmed.
`/cancel` releases held numbers. Expired holds are released automatically (user is notified).

## Admin panel (buttons)
`/panel` (also shown by `/admin`): for every car a **📅 Date** button (+1 / +3 / +7 / +30 days, or type an exact date) and a **🧹 Reset** button.
Reset asks for confirmation, first sends you a CSV backup of the tickets, then frees ALL numbers of that car, cancels waiting payments (customers are told) and re-opens the car for the next round (set the new date afterwards). Other cars are never touched.

## Admin commands (`/admin` shows them)
/addcar, /setphoto, /cars, /setdeadline, /setprice, /settotal, /togglecar,
/pending, /stats, /export <id> (CSV), /draw <id> confirm, /broadcast <text>

## Notes
- Tickets are numbered 1..total (max 9,899 per car). Ranges are 0-99, 100-199, ...; a 100-number range is shown as two pages of 50 because Telegram allows only 100 buttons per message.
- Data is stored in PostgreSQL (`DATABASE_URL` in `.env`, e.g. Neon). Tables are created automatically on first start.
- "No lotteries are open right now" means no car exists yet: an admin must create each one with `/addcar` (see above).
- `test_bot.py` is a developer test that DROPS and recreates the lottery tables in `TEST_DATABASE_URL`: never point it at your real database.
- Keep `.env` private. Revoke/regenerate the token in @BotFather if it was ever shared.
- Check that running a lottery is licensed/permitted where you operate.
- Please have a native speaker review the Amharic / Afaan Oromo texts in `i18n.py`.
