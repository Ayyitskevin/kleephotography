# Engineering checks for the solo sales workflow

Tool review requested by Kevin on 2026-11-11. Choose checks by the failure they
can expose; a bigger tool list is not itself a quality improvement.

| Layer | Tools | Repository decision |
| --- | --- | --- |
| Code and dependency hygiene | Ruff, Bandit, CodeQL, pip-audit, detect-secrets, hash locks | Existing gates retained; the dependency audit now includes the development lock. |
| Behavior and regressions | pytest, coverage, existing JavaScript tests | Already enforced; retain example-based route and transaction tests. |
| Combinations and edge cases | Hypothesis | Added as a pinned dev-only dependency; eight sales properties run in the existing unit jobs on Python 3.12 and 3.14. |
| Browser journeys and diagnostics | Playwright and Trace Viewer | Next browser layer, pending a permitted usable environment. Exercise user-visible outcomes using isolated synthetic data and blocked/mocked external transports. |
| Accessibility | axe-core plus keyboard/screen-reader review | Pair with browser journeys, including opened disclosures and validation errors. Automated scans cannot establish complete accessibility. |

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

## Browser follow-up

Use Playwright role/label locators for queue → review → edit/preferences → return,
then explicit manual outcomes and the changed-eligibility view. Run axe with each
relevant disclosure/error state visible. Capture traces for failures; use fixed
browser/OS versions before treating screenshot comparisons as visual regressions.
Human desktop/mobile and keyboard review still matters. The previous Chromium
socket restriction remains a boundary; this change does not bypass it or claim
browser appearance/accessibility verification. Production deployment is separate.

## Verification for this addition

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
