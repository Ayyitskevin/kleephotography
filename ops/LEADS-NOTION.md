# Leads → Notion mirror — runbook

**What it does.** Restores the lead-intake wire Odysseus `inquiry_intake` used to
provide, Mise-native and doctrine-clean: every website inquiry (POST `/contact`
and lead-kind `/forms/{slug}`) is stored in Mise's `inquiries` table (system of
record, unchanged), emails Kevin (unchanged), and now ALSO enqueues a
`notion_sync_inquiry` job that mirrors the lead one-way into a Notion "Leads"
database (WINDOW doctrine — display only, never read back). Triage actions in
`/admin/studio` (convert to client/quote, dismiss, and their undos) patch the
same Notion page's Status, so the display stays honest without Notion ever
becoming a second writer.

**Where it lives.**
- `app/notion_sync.py` — `sync_inquiry()` (+ `_inquiry_props`, `_inquiry_status`)
- `app/jobs.py` — `notion_sync_inquiry` handler (queue = retries ×3, survives restarts)
- Enqueue points: `app/public/site.py` (contact), `app/public/forms.py` (lead
  forms), `app/admin/studio.py` (5 triage routes)
- `migrations/067_inquiry_notion.sql` — adds `inquiries.notion_page_id`
  (rollback: `migrations/rollback/067_inquiry_notion.sql`)
- Config: `MISE_NOTION_LEADS_DB` in `.env` (plus existing `MISE_NOTION_TOKEN`)

**Arming it (one-time).** Create a Notion "Leads" database with properties:
Name (title), Email (email), Phone (phone_number), Business (rich_text),
Niche (select), Kind (select), Message (rich_text), Submitted (date),
Status (select), Mise ID (number). Share it with the Mise integration, set
`MISE_NOTION_LEADS_DB=<database id>` in `/opt/mise/.env`, restart mise.
Unset = fully dormant: store + email keep working, mirror is skipped and logged.

**How to check it's alive.**
- `curl -s localhost:8400/healthz` — service up, `jobs_pending` should be 0.
- `journalctl -u mise | grep 'notion lead'` — one `mirrored as page` line per
  new inquiry, `status patched` on triage, `skipped (token=…)` if dormant.
- Failures are loud: the job retries 3×, then the jobs row is marked `failed`
  with the error — `sqlite3 /opt/mise/data/mise.db "SELECT * FROM jobs WHERE
  kind='notion_sync_inquiry' AND status='failed'"`.
- Dry run any time (zero live writes): `python scripts/leads-dryrun.py`.

**How to turn it off.** Remove/blank `MISE_NOTION_LEADS_DB` in `/opt/mise/.env`
and `sudo systemctl restart mise`. That alone kills all Notion lead writes —
intake, storage, and email are unaffected. No code rollback needed; the
`notion_page_id` column and any already-created Notion pages are inert.
