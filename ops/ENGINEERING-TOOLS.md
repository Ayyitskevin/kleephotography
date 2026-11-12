# Engineering checks for the solo sales workflow

Tool review requested by Kevin on 2026-11-11. Choose checks by the failure they
can expose; a bigger tool list is not itself a quality improvement.

| Layer | Tools | Repository decision |
| --- | --- | --- |
| Code and dependency hygiene | Ruff, Bandit, CodeQL, pip-audit, detect-secrets, hash locks | Existing gates retained; the dependency audit now includes the development lock. |
| Behavior and regressions | pytest, coverage, existing JavaScript tests | Already enforced; retain example-based route and transaction tests. |
| Combinations and edge cases | Hypothesis | Added as a pinned dev-only dependency; eight sales properties run in the existing unit jobs on Python 3.12 and 3.14. |
| Browser journeys and diagnostics | Playwright and Trace Viewer | Chromium journeys in CI at desktop and mobile viewports, using synthetic data and blocked external transports. |
| Accessibility | axe-core plus keyboard/screen-reader review | WCAG 2.1 A/AA scans of the queue, opened forms and validation/stale states in CI. Manual review is still needed. |

## Applied now: generated sales invariants

`tests/test_sales_properties.py` checks that additional sources never relax a hold,
source order/duplication does not change the effective policy, one stop wins,
pauses include their last day, blocked channels combine, and conflicting channel
preferences are not silently selected. It also generates Unicode searches and
return identities to verify fixed local review destinations, bounded search
round-trips, and safe fallback for invalid contexts.

The generators describe persisted policy shapes; database constraints and HTTP
write semantics remain covered by existing integration tests. These new tests do
not access a database, start workers, call providers or mutate application code.
The date generator leaves one day at each end for boundary arithmetic. URL inputs
cover Unicode scalar text; raw invalid byte decoding remains outside this slice.

Hypothesis normally searches 100 valid examples per property and shrinks failures
to a smaller reproducer. A passing sample is not exhaustive proof. Explicit
adversarial examples run in addition, including URL delimiters, CR/LF, long search,
non-ASCII digits and oversized origin IDs. Keep a discovered regression as an
`@example` or a dedicated test, rather than relying on the local example cache.

```sh
MISE_ENV_FILE=/nonexistent python -m pytest tests/test_sales_properties.py -q \
  --hypothesis-show-statistics
# Replay a CI-reported seed when investigating; retain normal exploration in CI.
MISE_ENV_FILE=/nonexistent python -m pytest tests/test_sales_properties.py -q \
  --hypothesis-seed=12345
```

The runtime lock remains unchanged. Auditing the development lock exposed an
existing `msgpack==1.2.1` finding (CVE-2026-114613) through pip-audit's CacheControl
dependency. The dev lock now selects 1.2.4, which includes the upstream 1.2.3 fix
and later parser-allocation hardening. This is a development-tool dependency, not
a finding of production exposure. CI now audits both locks. The targeted update
command was:

```sh
uv pip compile --generate-hashes --upgrade-package msgpack==1.2.4 \
  --output-file requirements-dev.lock requirements.txt requirements-dev.txt
```

Regenerate the dev hash lock when updating
Hypothesis; see [DEPENDENCIES.md](DEPENDENCIES.md). `.hypothesis/` is a local replay
cache and is ignored. No new workflow, service account or paid subscription is
required: both existing CI unit jobs discover the tests normally.

## Browser journeys (2026-11-12)

`tests/browser/` has pinned Playwright and axe dependencies with an npm lockfile.
CI installs its Chromium revision on Ubuntu 24.04, then runs two journeys at
1280×900 and 390×844. Dependabot includes this tooling directory; CI audits the
npm lock. These are Chromium viewport checks, not Safari/Firefox or real-phone
coverage, and screenshots are diagnostic evidence rather than visual baselines.

The first journey searches the queue, opens review, operates a disclosure by
keyboard, edits research, changes channel preferences, records historical contact,
explicitly completes an action, moves to the next eligible prospect and records a
stop. It verifies return context and the changed/empty queue views. The second
checks a conflicting channel selection and a stale preference form, retained input,
and reopening current preferences in a separate tab.

Whole-page axe scans include the expanded action/contact forms, research and
preference pages, validation/conflict messages and changed/empty queue states.
No rules or elements are suppressed. Tests fail on WCAG 2.1 A/AA violations,
horizontal page overflow, browser exceptions, server errors or external requests.
Each scan attaches JSON (including inconclusive checks) and a viewport screenshot.
Captures keep the mobile viewport fixed while native page transitions are active.
CI retains the HTML report and failure traces for seven days. There are no retries
that could hide a flaky first attempt.

The test server always creates a new temporary SQLite/media directory, clears
inherited `MISE_*` configuration, disables env-file loading and uses a generated
signing key with a synthetic login password. It binds only to 127.0.0.1:8765;
Playwright refuses an existing server and does not accept a base-URL override.
Application routes, login and CSRF are real. A fixed studio day and seeded prospects
make outcomes reproducible. Lifespan jobs/schedulers stay off and outbound socket
connects are rejected. Browser contexts also reject off-origin requests and block
service workers. Viewports have distinct fixture records and run serially.

From the repo root, with the Python dev lock installed in `.venv`:

```sh
python scripts/check_requirement_locks.py
npm ci --prefix tests/browser
npm audit --prefix tests/browser --audit-level=moderate
.venv/bin/python tests/browser/server.py --check
cd tests/browser
npx playwright install --with-deps chromium
npm test
```

Run the browser installation/tests only in an environment that permits Chromium.
The workspace's earlier socket restriction is not bypassed: local preflight and
existing Python/JS gates run here, while hosted CI performs the browser run.
`--check` verifies disposable fixtures and real login/queue routes without opening
a listening socket. Human visual, complete keyboard and screen-reader review
remain separate; automated scans do not prove full accessibility or WCAG compliance.
Cloudflare Access, background workers and live providers are outside these tests.
Production deployment is separate.

Local preflight passed the synthetic login/queue check, Playwright discovery of
four journeys, npm audit, 504 unit, 1,308 integration, 188 smoke and 60 JavaScript
cases on Python 3.12.14. Ruff lint/format, Bandit, migration/lock checks and the
tracked-file secret scan also passed. Each Python suite used fresh disposable
data. Hosted browser results are reported by the dedicated CI job; no local
Chromium run or manual visual assessment is claimed.

The first hosted run caught a shared-navigation accessible-name mismatch: the
visible `⌘K` shortcut was missing from the command button's accessible name. The
name now includes the visible label as well as the shortcut's spoken description.
The initial run also exposed a test locator matching both a date field and its
preset group; the locator now uses the exact field label.

## Property-test verification (2026-11-11)

On Python 3.12.14, the final candidate passed 504 unit, 1,308 integration,
188 smoke and 60 JavaScript cases. Each Python suite used a fresh disposable
workspace data directory and `MISE_ENV_FILE=/nonexistent`; existing provider
mocks stayed in place. Ruff lint/format, Bandit, the tracked-file secret scan,
migration numbering and requirement-lock consistency also passed. Both runtime
and development dependency audits found no known vulnerabilities after the
msgpack update. The initial
focused property run exercised 800 passing generated examples plus explicit
examples; no defect was found in that sample. Existing AnyIO/Starlette warnings
remain. Browser, keyboard and screen-reader checks were not run.

## Primary references

- [Hypothesis introduction and when to use it](https://hypothesis.readthedocs.io/en/latest/tutorial/introduction.html)
- [Hypothesis domains and generated input coverage](https://hypothesis.readthedocs.io/en/latest/explanation/domain.html)
- [Replaying failures and explicit examples](https://hypothesis.readthedocs.io/en/latest/tutorial/replaying-failures.html)
- [Playwright testing practices](https://playwright.dev/docs/best-practices)
- [Playwright Trace Viewer](https://playwright.dev/docs/trace-viewer-intro)
- [axe-core API and visible-state requirements](https://github.com/dequelabs/axe-core/blob/develop/doc/API.md)
- [Deque automated testing limitations](https://www.deque.com/axe/devtools/)
- [MessagePack upstream release notes](https://github.com/msgpack/msgpack-python/blob/main/CHANGELOG.md)
