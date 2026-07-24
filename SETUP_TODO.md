# SETUP_TODO — Workflow C (referral-system)

Build is done (TEST_MODE, see HANDOVER.md). Manual items still needed before
a REAL (non-TEST_MODE) run against live services:

- [x] `data\connections.csv` — Manpreet's LinkedIn export (30,000 rows, loaded)
- [x] `templates\Pitch_Master.pptx` — present; slots located, `templates\pitch_slots.json` generated
- [x] `templates\referral_email.md` — present, parsed and used by src/email_drafter.py
- [x] `BUILD_SPEC_C.md` in project root — present
- [ ] In `.env`, replace `PASTE_NEW_BOTFATHER_TOKEN_HERE` in the `TELEGRAM_BOT_TOKEN_C` line with the real token from BotFather `/newbot`
- [ ] Confirm `AUTHORIZED_USER_IDS` in `.env` lists every real operator Telegram user id (currently just the one from `TELEGRAM_USER_ID`)
- [ ] Set `TEST_MODE=false` in `.env` once ready for a real run against live Gmail/Apollo/Tavily/Exa/LLM
- [ ] Re-authorize Gmail OAuth if `token.json` isn't already scoped to `gmail.compose` for the sending account
- [ ] `pip install rapidfuzz==3.10.1` (and `pymupdf` only if you want to re-run the visual deck QA) — not yet in `requirements.lock.txt`
