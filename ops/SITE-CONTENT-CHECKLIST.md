# Site content checklist — polish round-out

Code for the marketing site is Screening Room–complete. This is the **live
admin** work that fills the doors. Prefer empty honest states over invented proof
([`TRUTHFUL-HTTPS.md`](TRUTHFUL-HTTPS.md)).

## 1. Inventory

In admin, unpublish or unstar anything unverified:

- Testimonials without a real client attribution
- Case studies (`cs_published`) without release + starred hero
- Demo gallery PIN if it is a retired showcase default

## 2. Star & tag (~6 stills + 1–2 films per new vertical)

| Vertical | Tag examples |
|----------|----------------|
| Real Estate | `re/exteriors`, `re/interiors`, `re/twilight`, `re/aerials`, `re/walkthrough` |
| Portraits | `pl/headshots`, `pl/branding`, `pl/family` |
| Food & Bev | unprefixed or `fb/…` (legacy archive already counts) |

Unblocks: home doors, spoke filmstrips, `/portfolio` chips, `/reels`.

## 3. Booking event types

Either create in **Admin → Scheduling**, or on the prod host:

```sh
cd /opt/mise && sudo -u mise .venv/bin/python scripts/ensure-specialty-event-types.py
```

Creates/activates `re-shoot`, `pl-session`, `fb-shoot` with weekday + evening
windows so spoke CTAs deep-link (`/book/re-shoot`, etc.).

## 4. House reel + demo gallery

- Star a strong video so the home house reel isn’t the “threaded…” empty state
- Only set `MISE_DEMO_GALLERY_SLUG` / `MISE_DEMO_GALLERY_PIN` for a real published gallery

## 5. About portrait

Drop a real studio portrait at:

`/opt/mise/static/about-portrait.jpg` (or `.jpeg` / `.png` / `.webp`)

Or set `MISE_ABOUT_PORTRAIT=filename` in `.env`. Without it, About no longer
fakes a “Meet Kevin” frame from a random portfolio still.

## 6. Case studies

Publish `/work/{slug}` only with verified attribution **and** a starred hero photo.

## 7. Press

Admin → Press: set `show_on_site` + past `publish_date`. Footer “Press” only
appears when at least one feature qualifies; `/press` stays honestly empty until then.

## Spot-check after content

`/` · three spokes · `/portfolio` · `/reels` · `/work` · `/about` · `/book/re-shoot`
· `/book/pl-session` · share-debugger OG image on a marketing URL.
