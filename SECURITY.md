# Security policy

Mise is the self-hosted studio app behind [kleephotography.com](https://kleephotography.com).
It is maintained by one person alongside running a photography business. There is no
security team, no SLA, and no bug bounty — but reports are genuinely welcome and taken
seriously, because the app handles real client galleries and real Stripe payments.

## Reporting a vulnerability

Email **kevlee.photographer@gmail.com**.

That is the same address the live site publishes under RFC 9116 at
[`/.well-known/security.txt`](https://kleephotography.com/.well-known/security.txt)
(served by `app/public/site.py`). If this file and that file ever disagree, `security.txt`
is canonical — it is rendered from live config, so it cannot go stale.

**Please do not open a public GitHub issue for a suspected vulnerability.** Email first.

A useful report includes: what you found, the URL or code path, the steps to reproduce,
and what an attacker could actually do with it.

## What to expect

- Acknowledgement when I get to it — realistically days, not hours. Nudge me if a week
  passes with no reply.
- No payment, swag, or bounty. This is a one-person project.
- Credit in the fix commit if you want it, and no credit if you'd rather stay anonymous.
- If you report something real, I'll tell you when it's fixed and deployed.

## Scope

In scope: this repository and the live site at `kleephotography.com`.

Only the current `main` branch is supported. Fixes ship forward; nothing is back-ported to
older commits or tags.

Please **don't**, while testing:

- Access, download, or exfiltrate real client galleries, media, contracts, or payment data.
  If a proof-of-concept requires real client data, stop and describe the issue instead.
- Run automated scanners or load tests against the live site — it is one small box, and
  brute-forcing a client PIN just trips the lockout and pages the owner.
- Attempt social engineering, phishing, or physical access. Out of scope.

Denial of service, missing security headers with no demonstrated impact, and findings from
automated scanners without a working exploit are also out of scope.

## Secrets

If you find a credential, token, or `.env` value committed anywhere in this repository or
its history, treat it as a live secret and report it privately via the address above rather
than in a public issue or pull request.
