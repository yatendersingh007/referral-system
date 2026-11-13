# Referral System (Workflow C)

Turns warm LinkedIn connections into reviewed Gmail drafts.

The system takes a single firm name or a target list, finds which
connections from a LinkedIn export work at those firms, checks each firm
against the ICP, finds the contact's email, and drafts a referral email with
a custom pitch deck attached. A person reviews and sends every draft.

## Pipeline

1. A Telegram command starts the run.
2. Firm matching finds connections at each target firm.
3. The ICP gate returns `qualified`, `disqualified` or `parked`. Parked firms
   (AUM or geography not verified) go to the summary for human review. They
   are never drafted and never dropped silently.
4. Apollo finds the contact's email.
5. Personalization research runs only for contacts with an email.
6. The email and pitch deck are drafted. Every LLM-written claim is checked
   against the research by `src/fact_check.py`.
7. The result lands as a Gmail **draft**. Telegram gets a summary.

The order is cheapest check first, so expensive research never runs on a firm
that already failed.

## Safety rules

- The code never sends email. It creates drafts only.
- LinkedIn is never fetched. Profile URLs are used only as identity keys.
- Every failure produces a Telegram message and a database row.
- A contact drafted earlier by another workflow is flagged, not blocked.

## Telegram commands

```
/referral "<Firm Name>"         single-firm run
/referral batch                 target-list batch, firms one per line
/referral batch confirm|cancel  confirm or cancel a large upload or live batch
/referral reload                re-ingest data/connections.csv
/referral resume                continue a crashed or cancelled run
```

## Setup

```bash
pip install -r requirements.txt
python -c "from src import dbc; dbc.apply_migrations()"   # safe to re-run
python -m src.telegram_bot
pytest tests/                                               # local machine only
```

Keep `.env`, `token.json`, `credentials.json` and `data/connections.csv` out
of git. The connections file holds real personal data.

See `BUILD_SPEC_C.md` for the full spec and `HANDOVER.md` for operations.
