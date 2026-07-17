# Truthful HTTPS baseline — activation and rollback

This runbook activates the public-trust changes without silently turning code
review into a production or provider mutation. Migration 068 preserves rows and
only unpublishes exact invented-content fingerprints.

## Human gates

This pull request contains a migration and changes public/security behavior.
Kevin reviews and merges it. A human separately approves the Cloudflare changes,
Flow deploy, and every live content mutation.

## Before any live mutation

Do these in order. Backups precede admin edits, environment edits, restarts, and
provider changes.

1. Take a fresh SQLite snapshot and verify the snapshot itself.
   `ops/backup.sh` proves `integrity_check`; the restored scratch copy also
   proves that no foreign-key violations were captured:

   ```sh
   cd /opt/mise
   sudo -u mise ./ops/backup.sh
   latest=$(ls -1t /opt/mise/data/backups/mise-*.db.gz | head -1)
   scratch=$(mktemp --suffix=.db)
   trap 'rm -f "$scratch"' EXIT
   gzip -cd "$latest" > "$scratch"
   test "$(sqlite3 "$scratch" 'PRAGMA integrity_check;')" = ok
   test -z "$(sqlite3 "$scratch" 'PRAGMA foreign_key_check;')"
   rm -f "$scratch"
   trap - EXIT
   ```

2. Before editing `/opt/mise/.env`, preserve it outside the deploy tree in a
   timestamped, root-readable `0600` backup. Verify the backup exists without
   printing its contents. Confirm the latest off-machine restore proof is fresh.
   The current off-machine set does not include `receipts/`, so preserve that
   directory separately until the durable-file backup fix lands.

3. Perform a read-only inventory before deciding what to change:

   - every published testimonial and case study;
   - every portfolio-starred asset;
   - every published demo/client delivery gallery, its release status, and whether
     its PIN was created by the retired showcase workflow;
   - `MISE_DEMO_GALLERY_SLUG` and `MISE_DEMO_GALLERY_PIN`;
   - ownership, client release, attribution, and claimed results for all public
     proof.

4. On the old production code, set `MISE_SHOWCASE_SEED=false` and restart Mise
   before manually unpublishing anything. This closes the startup race that could
   recreate the prototype proof. Keep this legacy key in Flow's environment
   through any rollback to pre-PR code; the new code safely ignores it.

5. With human approval, unpublish proof without verified provenance. Rotate the
   retired predictable demo PIN or unpublish the delivery gallery as appropriate.
   Do not auto-unpublish gallery 1 or its assets in SQL: it may now hold a real
   delivery, so those decisions require the live inventory.

## Proxy and environment staging

1. Confirm cloudflared reaches Uvicorn from exactly `127.0.0.1` or `::1`,
   sends `X-Forwarded-Proto: https`, and preserves the original public `Host`
   header. Uvicorn does not use `X-Forwarded-Host` to rewrite Host; a tunnel that
   sends `Host: flow:8400` would create a redirect loop.

2. After the backup and proxy checks, stage these values:

   ```text
   MISE_SHOWCASE_SEED=false
   MISE_BASE_URL=https://kleephotography.com
   MISE_COOKIE_SECURE=true
   MISE_CANONICAL_REDIRECTS=false
   FORWARDED_ALLOW_IPS=127.0.0.1,::1
   ```

   Leave the application redirect disabled for the first restart. An explicit
   stale `MISE_COOKIE_SECURE=false` overrides the secure default and must be
   removed or changed to `true`.

## Cloudflare edge change

1. Confirm valid certificates for the apex and `www` hostnames and keep origin
   encryption in Full (strict) mode.
2. For noncanonical requests, block methods other than GET and HEAD. Do not use a
   301/308 that can replay an unsafe request body across origins.
3. Validate HTTP → HTTPS and `www` → apex with temporary, non-cacheable redirects
   that preserve path and query.
4. Keep HSTS at `max-age=300`. Do not enable `includeSubDomains` or preload in
   this pass.
5. Promote the edge redirects to permanent only after the acceptance checks and
   an observation window. A browser-cached permanent redirect cannot be rolled
   back merely by disabling the provider rule.

The edge is still the transport authority. The application guard is defense in
depth and deliberately rejects noncanonical unsafe methods with 421.

## Deploy sequence

1. Kevin merges the reviewed PR.
2. Deploy the exact merge SHA to Flow by fast-forward only. Restart Mise with
   `MISE_CANONICAL_REDIRECTS=false`; migration 068 should run once.
3. Verify the private health/API bypasses and the temporary public edge behavior.
4. Set `MISE_CANONICAL_REDIRECTS=true`, restart once, and run all acceptance
   checks. If any request loops, immediately restore `false`; application
   redirects carry `Cache-Control: no-store`.
5. After the observation window, promote the validated edge redirects to
   permanent.

## Acceptance

The first two commands must redirect; the third must finish at the HTTPS apex in
no more than two hops. The unsafe request must return a 4xx with no redirect URL.

```sh
curl -sS -o /dev/null -w '%{http_code} %{redirect_url}\n' \
  'http://kleephotography.com/contact?src=baseline'
curl -sS -o /dev/null -w '%{http_code} %{redirect_url}\n' \
  'https://www.kleephotography.com/contact?src=baseline'
curl -sS -L --max-redirs 3 -o /tmp/klee-contact.html \
  -w '%{http_code} %{url_effective} %{num_redirects}\n' \
  'http://www.kleephotography.com/contact?src=baseline'
curl -sS -o /dev/null -w '%{http_code} %{redirect_url}\n' \
  -X POST 'http://www.kleephotography.com/contact'
```

Expected final URL:
`https://kleephotography.com/contact?src=baseline`, status 200, with an apex
canonical link. Also verify:

- `/healthz` remains 200 on the private Flow origin and public apex.
- Bearer-gated `/api/*` routes do not canonical-redirect on the private origin.
- The three retired prototype quotes, the invented tasting-menu tagline, and its
  invented same-week brief are absent from every public surface.
- `"@type": "Review"` and `reviewBody` are absent from public HTML.
- A canonical HTTPS contact submission succeeds once; noncanonical unsafe
  submissions return 4xx, no `Location`, and create no inquiry.
- After a real admin login, browser DevTools shows the session cookie carrying
  `Secure`, `HttpOnly`, and `SameSite=Lax` without exposing the password in
  a command or log.
- `curl -sSI https://kleephotography.com/healthz` includes
  `Strict-Transport-Security: max-age=300`.
- Unit, integration, smoke, Ruff check, Ruff format, and exact-SHA CI all pass.

## Rollback

1. Before permanent promotion, disable the temporary edge rules if they misroute.
   After promotion, remember that previously cached permanent responses may
   outlive the provider rule.
2. Set `MISE_CANONICAL_REDIRECTS=false` and restart Mise if proxy reconstruction
   loops. Keep `MISE_SHOWCASE_SEED=false`, especially if rolling code back.
3. Restore the pre-deploy database only for a broader migration failure. That
   snapshot may contain the prototype rows as published: while traffic remains
   stopped, reapply migration 068's retirement statements or manually unpublish
   the exact proof, then verify public absence before returning traffic.
4. Republish only individually verified testimonials, case studies, galleries,
   and assets. Never restore unverified proof merely to fill an empty layout.
