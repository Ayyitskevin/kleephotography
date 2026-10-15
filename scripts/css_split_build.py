#!/usr/bin/env python3
"""R-2 step 2: cut the sheets, and prove the cut is cascade-neutral.

  scripts/css_split_build.py build  [--ref REF]   # write static/{mise-site,screening-admin,screening}.css
  scripts/css_split_build.py verify [--ref REF]   # compare the working-tree sheets with REF's

Everything is derived from the base ref's sheets, so `build` is reproducible and idempotent.

What the cut does
  * mise-site.css      the subset of mise.css the marketing pages can use (css_split_measure.py)
  * screening.css      loses the admin-only rules of the COMMAND DECK block; the 57 public rules
                       and 6 at-rules that sat in that block stay where they were
  * screening-admin.css  the base sheet's text from the COMMAND DECK header to the end of the file,
                       verbatim: admin-only rules AND the public rules among and after them

Why the admin sheet repeats public rules: a later sheet wins ties, so moving only the admin
rules after the public rules that used to follow them could flip a tie. Repeating the whole
tail after them restores "last occurrence" order exactly (`verify` checks it mechanically).
"""

from __future__ import annotations

import argparse
import re
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import css_split_measure as m  # noqa: E402
from css_split_lib import Sheet, sizes  # noqa: E402

REPO = m.REPO
STATIC = REPO / "static"
DECK_MARK = "═══ " + m.DECK_HEADER

ADMIN_HEADER = """/* screening-admin.css: the admin shell's half of the Screening Room sheet (plan R-2)
   Loaded only by admin/base_admin.html, after screening.css.

   This is the tail of the old single-file screening.css, VERBATIM, from the COMMAND DECK header
   to the end of the file. Besides the admin-only deck rules it repeats the public rules that sat
   among and after them (breakpoints, motion, the shared UI primitives). The repeat is on purpose:
   when two rules tie, the later one wins, and moving only the admin rules out would let them
   overtake public rules that used to come after them. Repeating the tail after them keeps every
   rule's last occurrence in its old order (scripts/css_split_build.py verify checks this).
   Edit a shared rule in BOTH sheets, or edit it only here when it is admin-only.
   Plan: ops/CSS-SPLIT-R2-MEASUREMENT.md. */

"""

SITE_HEADER = """mise-site.css: the part of mise.css the marketing pages can use (plan R-2)
   Loaded by site/base_site.html in place of mise.css, into the same layer(mise). Rules are
   verbatim and in mise.css order; scripts/css_split_measure.py subset prints this file.
   mise.css stays the full sheet for the admin shell, the client pages and the booking flow."""

DECK_NOTE = (
    "/* THE COMMAND DECK (admin shell) moved to static/screening-admin.css (plan R-2). The public\n"
    "   rules that sat inside it (breakpoints, reveal stagger, motion, hover) stay here, in order. */\n"
)


def base_sheets(ref: str) -> dict[str, Sheet]:
    return {
        n: Sheet(n, m.blob(ref, "static/" + n))
        for n in ["mise.css", "screening-room-tokens.css", "screening.css"]
    }


def deck_start(text: str) -> int:
    a = text.index(DECK_MARK)
    return text.rfind("\n", 0, a) + 1


def admin_units(scr: Sheet) -> list[tuple[int, int, int, int]]:
    """Top-level units after the deck header whose rules are ALL admin-only."""
    lo, _hi = m.deck_range(scr)
    out = []
    for u in scr.units:
        if u[2] < lo or u[2] >= _hi:
            continue
        rules = scr.rules[u[2] : u[3]]
        if all(m.is_admin_rule(r) for r in rules):
            out.append(u)
        elif any(m.is_admin_rule(r) for r in rules):
            raise SystemExit(
                f"STOP: a deck unit mixes admin and public rules at line {rules[0].line}"
            )
    return out


def cut_screening(scr: Sheet) -> str:
    """The base text without the admin-only deck units (and the comments that lead them)."""
    text, drop = scr.text, admin_units(scr)
    out, pos, first = [], 0, True
    prev_end = 0
    ends = {u[0]: u for u in scr.units}
    # unit k is deleted from the end of unit k-1 (so its leading comments go with it) to its own end
    order = sorted(scr.units)
    prev_of = {order[i][0]: (order[i - 1][1] if i else 0) for i in range(len(order))}
    dset = {u[0] for u in drop}
    spans = []
    for u in order:
        if u[0] in dset:
            a, b = prev_of[u[0]], u[1]
            if spans and spans[-1][1] == a:
                spans[-1] = (spans[-1][0], b)
            else:
                spans.append((a, b))
    for a, b in spans:
        out.append(text[pos:a])
        if first:
            out.append("\n\n" + DECK_NOTE)
            first = False
        pos = b
    out.append(text[pos:])
    del prev_end, ends
    return "".join(out)


def build(ref: str) -> dict[str, str]:
    sheets = base_sheets(ref)
    a = m.analyse(ref)
    site = m.emit(sheets["mise.css"], a["keep"], SITE_HEADER, "")
    scr = sheets["screening.css"]
    admin = ADMIN_HEADER + scr.text[deck_start(scr.text) :]
    return {
        "mise-site.css": site,
        "screening.css": cut_screening(scr),
        "screening-admin.css": admin,
    }


# ── verification ─────────────────────────────────────────────────────────────
def node_matches(pages, stack, script_attrs):
    """{page: {node_index: [rule sig, ...] in cascade order}} for a stack of Sheets."""
    out = {}
    for pg in pages:
        pos = {id(n): i for i, n in enumerate(pg["nodes"])}
        per = defaultdict(list)
        for sheet in stack:
            for r in sheet.rules:
                if r.kind != "style":
                    continue
                seen = set()
                for sel in r.selectors:
                    for n in m.selector_candidates(pg, sel):
                        if id(n) in seen:
                            continue
                        if m.match_selector(n, sel, script_attrs):
                            seen.add(id(n))
                for nid in seen:
                    per[pos[nid]].append(r.sig)
        out[pg["name"]] = per
    return out


def verify(ref: str) -> list[str]:
    log: list[str] = []
    ok = True

    def check(cond: bool, msg: str) -> None:
        nonlocal ok
        ok &= bool(cond)
        log.append(("PASS  " if cond else "FAIL  ") + msg)

    old = base_sheets(ref)
    new = {
        n: Sheet(n, (STATIC / n).read_text())
        for n in ["mise-site.css", "screening.css", "screening-admin.css"]
    }
    scr = old["screening.css"]
    lo, hi = m.deck_range(scr)

    # 1. the three new files are exactly what `build` makes from the ref (no hand edits)
    built = build(ref)
    for n, text in built.items():
        check((STATIC / n).read_text() == text, f"{n} equals build({ref[:7]})")

    # 2. admin pages: last-occurrence order of every old screening.css rule is unchanged
    base_sigs = [r.sig for r in scr.rules]
    new_scr, new_admin = new["screening.css"], new["screening-admin.css"]
    drop = admin_units(scr)
    dropped = {i for u in drop for i in range(u[2], u[3])}
    kept_old = [i for i in range(len(scr.rules)) if i not in dropped]
    check(
        [r.sig for r in new_scr.rules] == [base_sigs[i] for i in kept_old],
        "new screening.css = old screening.css minus the admin-only deck rules, same order",
    )
    start_rule = next(r.idx for r in scr.rules if r.start >= deck_start(scr.text))
    check(
        [r.sig for r in new_admin.rules] == base_sigs[start_rule:],
        "screening-admin.css holds every rule from the deck header to EOF, same order",
    )
    stream = [("s", i) for i in kept_old] + [("a", i) for i in range(start_rule, len(scr.rules))]
    last = {}
    for pos, (_k, i) in enumerate(stream):
        last[i] = pos
    order_new = sorted(last, key=lambda i: last[i])
    check(
        order_new == list(range(len(scr.rules))),
        "admin stack: last occurrences are in the original order",
    )

    # 3. what leaves screening.css can only match the admin shell
    tokens_ok = True
    for sel_rule in (scr.rules[i] for i in dropped):
        if not all(m.is_admin_sel(x) for x in sel_rule.selectors):
            tokens_ok = False
    check(
        tokens_ok,
        f"all {len(dropped)} dropped rules need .sr-admin or .admin-shell in every selector",
    )
    leaks = []
    for p in m.ls(ref, "templates") + m.ls(ref, "app") + m.ls(ref, "static"):
        if p.startswith(("templates/admin/", "app/admin/")) or p.endswith(".css"):
            continue
        if p.endswith((".html", ".py", ".js")):
            t = m.blob(ref, p)
            t = (
                m.strip_comments(t, "tpl" if p.endswith(".html") else "js")
                if not p.endswith(".py")
                else t
            )
            if p.endswith(".py"):
                t = " ".join(m.py_strings(t))
            if p.endswith(".js"):  # reading a class (classList.contains) never puts it on a page
                t = re.sub(r"classList\.contains\([^)]*\)", " ", t)
            if re.search(r"(?<![-\w])(sr-admin|admin-shell)(?![-\w])", t):
                leaks.append(p)
    check(
        not leaks, f"no template, script or non-admin module emits sr-admin / admin-shell {leaks}"
    )
    base_admin = m.blob(ref, "templates/admin/base_admin.html")
    check("sr sr-admin" in base_admin, "base_admin.html is where body.sr-admin comes from")
    others = [
        p
        for p in m.ls(ref, "templates/admin")
        if p != "templates/admin/base_admin.html"
        and re.search(
            r"(?<![-\w])(sr-admin|admin-shell)(?![-\w])", m.strip_comments(m.blob(ref, p), "tpl")
        )
        and "admin/base_admin.html" not in m.blob(ref, p)
        and "{% extends" in m.blob(ref, p)
    ]
    check(not others, f"admin templates that wear the deck classes all extend base_admin {others}")

    # 4. marketing pages: per-element matched-rule sequences are identical, old stack vs new stack
    uni = m.marketing_universe(ref)
    pages = m.load_corpus(ref, uni["script_attrs"])
    old_stack = [old["mise.css"], old["screening-room-tokens.css"], old["screening.css"]]
    new_stack = [new["mise-site.css"], old["screening-room-tokens.css"], new["screening.css"]]
    a, b = (
        node_matches(pages, old_stack, uni["script_attrs"]),
        node_matches(pages, new_stack, uni["script_attrs"]),
    )
    bad = [(pn, i) for pn in a for i in set(a[pn]) | set(b[pn]) if a[pn].get(i) != b[pn].get(i)]
    nel = sum(len(p["nodes"]) for p in pages)
    check(
        not bad,
        f"marketing: matched-rule sequences identical for {nel} elements on {len(pages)} pages {bad[:3]}",
    )

    # 5. mise-site.css is a verbatim ordered subset of mise.css
    msigs = [r.sig for r in old["mise.css"].rules]
    pos, sub_ok = 0, True
    for r in new["mise-site.css"].rules:
        try:
            pos = msigs.index(r.sig, pos) + 1
        except ValueError:
            sub_ok = False
            break
    check(sub_ok, "mise-site.css rules appear in mise.css verbatim and in the same order")
    log.append("RESULT " + ("PASS" if ok else "FAIL"))
    return log


def sizes_table(ref: str) -> str:
    old = {
        n: m.blob(ref, "static/" + n)
        for n in ["mise.css", "screening-room-tokens.css", "screening.css", "mise-admin.css"]
    }
    new = {
        n: (STATIC / n).read_text()
        for n in ["mise-site.css", "screening.css", "screening-admin.css"]
    }

    def row(name, files):
        raw = sum(sizes(t)[0] for t in files)
        gz = sum(sizes(t)[1] for t in files)
        return raw, gz

    tok = old["screening-room-tokens.css"]
    surfaces = {
        "Marketing visitor (home, spokes, portfolio, services, ...)": (
            [old["mise.css"], tok, old["screening.css"]],
            [new["mise-site.css"], tok, new["screening.css"]],
        ),
        "Client pages (gallery, portal, money docs) and booking flow": (
            [old["mise.css"], tok, old["screening.css"]],
            [old["mise.css"], tok, new["screening.css"]],
        ),
        "Admin shell": (
            [old["mise.css"], old["mise-admin.css"], tok, old["screening.css"]],
            [
                old["mise.css"],
                old["mise-admin.css"],
                tok,
                new["screening.css"],
                new["screening-admin.css"],
            ],
        ),
    }
    lines = [
        "| Surface (CSS only; fonts.css and JS unchanged) | Before raw | Before gz | After raw | After gz | gz saved |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for k, (a, b) in surfaces.items():
        (ar, ag), (br, bg) = row(k, a), row(k, b)
        lines.append(f"| {k} | {ar:,} | {ag:,} | {br:,} | {bg:,} | {ag - bg:+,} |")
    lines += ["", "| Sheet | Raw | gz |", "|---|---:|---:|"]
    for n, t in [*old.items(), *new.items()]:
        tag = "before" if n in old and t is old[n] else "after"
        lines.append(f"| {n} ({tag}) | {sizes(t)[0]:,} | {sizes(t)[1]:,} |")
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["build", "verify", "sizes"])
    ap.add_argument("--ref", default=m.BASE_REF)
    args = ap.parse_args()
    if args.cmd == "build":
        for n, text in build(args.ref).items():
            (STATIC / n).write_text(text)
            print(f"wrote static/{n}: {sizes(text)[0]:,} B raw, {sizes(text)[1]:,} B gz")
    elif args.cmd == "sizes":
        print(sizes_table(args.ref))
    else:
        log = verify(args.ref)
        print("\n".join(log))
        sys.exit(0 if log[-1] == "RESULT PASS" else 1)


if __name__ == "__main__":
    main()
