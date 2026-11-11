# Requirements and hash locks

CI and deployment install the hash lockfiles. Updating only `requirements.txt`
does not change the environment tested or installed. Update both input pins and
locks together, including when finishing a Dependabot PR.

```sh
uv pip compile --generate-hashes --output-file requirements.lock requirements.txt
uv pip compile --generate-hashes --output-file requirements-dev.lock requirements.txt requirements-dev.txt
python scripts/check_requirement_locks.py
uv pip install --python .venv/bin/python --require-hashes -r requirements-dev.lock
```

Review the lock diff, then run all gates in AGENTS.md. Use a new disposable
`MISE_TEST_DATA_DIR` for each suite; reusing integration data in the smoke suite
can invalidate its fixture assumptions. Do not copy production configuration.

The standard-library consistency check runs before dependency installation in
both CI jobs. It checks every direct runtime pin against both locks, every dev
pin against the dev lock, and runtime transitive versions against the dev lock.
It normalizes package names and accepts extras and generated hash continuations.
It deliberately supports this repository's exact-pin format only: empty inputs,
duplicate names, ranges, includes and environment markers fail rather than being
silently ignored. If the manifest format changes, extend the guard and its tests
in that same PR. This is not a resolver or a hash-integrity validator; pip's
`--require-hashes` installation and the full test/security gates remain required.

## Maintenance verification — 2026-11-11

PRs #150/#151 aligned the CodeQL action pins. Their separate merges briefly
produced an incompatible init/analyze pair; #155 added a grouped update rule and
a matching-pin test. Dependabot's next PR, #158, updates the pair together.

PR #149 repaired previously stale Python locks while retaining Stripe 15.3.0.
Stripe 16 changes the SDK's default API version and needs a separate payment
compatibility review. Both #155 and #149 passed hosted CI and CodeQL before merge;
main at `1be8960` also passed its post-merge checks. The verified merge candidates
each passed 485 unit, 1,290 integration and 188 smoke tests locally, plus Ruff.
Provider seams use existing test mocks; this does not verify live payments.

PR #159 initially changed only FastAPI's input pin from 0.162.0 to 0.162.2.
Its completion regenerates both locks and introduces the consistency guard so
the same input-only drift cannot silently pass future CI. No production deploy
or provider operation is part of this maintenance work.

The FastAPI/guard change passed 496 unit tests (including 11 guard cases),
1,290 integration tests, 188 smoke tests and 60 JavaScript tests on Python
3.12.14 with FastAPI 0.162.2 installed from hashes. Ruff, Bandit, migration
numbering and the secrets baseline passed; the runtime audit found no known
vulnerabilities. The guard also rejected the original PR #159 snapshots for
both stale locks. Existing AnyIO/Starlette deprecation warnings remain.
Hosted checks are required before merge; local results do not replace them.
