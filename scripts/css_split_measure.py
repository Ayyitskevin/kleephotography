#!/usr/bin/env python3
"""R-2 step 1: measure which stylesheet supplies each rule the marketing pages use.

Reads everything from a git ref (default: the dispatch base 84c03fd) so the numbers
reproduce after the cut lands:

  scripts/css_split_measure.py measure [--ref REF] [--md OUT.md] [--json OUT.json]
  scripts/css_split_measure.py subset  [--ref REF]          # print mise-site.css to stdout

Method (stdlib only, see scripts/css_split_lib.py):
  1. Parse mise.css / screening-room-tokens.css / screening.css into rules.
  2. Parse every rendered marketing golden (both themes, seeded + empty) into a DOM and
     match every selector against it, *optimistically* (dynamic pseudo-classes and
     script-set attributes count as matching), so a rule reported as unmatched can
     never apply to that page.
  3. Build the marketing "universe": every word that appears in a template reachable from
     site/base_site.html, in the marketing JS, or in a marketing Python string. A rule can
     only apply on some marketing page state if each class / id / tag it requires is in the
     universe. The kept set is the universe-satisfiable rules (a superset of the rules
     the goldens match), so states the goldens do not render stay covered.
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from css_split_lib import (  # noqa: E402
    Rule,
    Sheet,
    match_selector,
    parse_html,
    parse_selector,
    sizes,
    tokens,
)

REPO = Path(__file__).resolve().parent.parent
BASE_REF = "84c03fd"
DECK_HEADER = "THE COMMAND DECK"
TAIL_HEADER = "SHARED UI PRIMITIVES"
SHEETS = ["mise.css", "screening-room-tokens.css", "screening.css"]
ALWAYS_TAGS = {"html", "body", "head", "title", "meta", "link", "script", "style", "noscript"}


def git(*args: str, ref: str | None = None) -> str:
    return subprocess.run(
        ["git", *args], cwd=REPO, check=True, capture_output=True, text=True
    ).stdout


def blob(ref: str | None, path: str) -> str:
    """File text at a git ref, or from the working tree when ref is None."""
    if ref is None:
        return (REPO / path).read_text()
    return git("show", f"{ref}:{path}")


def ls(ref: str | None, prefix: str) -> list[str]:
    if ref is None:
        base = REPO / prefix
        found = [base] if base.is_file() else sorted(p for p in base.rglob("*") if p.is_file())
        return [str(p.relative_to(REPO)) for p in found]
    return git("ls-tree", "-r", "--name-only", ref, prefix).split()


# ── surfaces ────────────────────────────────────────────────────────────────
def is_admin_sel(sel: str) -> bool:
    return any("sr-admin" in c.classes or "admin-shell" in c.classes for c in parse_selector(sel))


def is_admin_rule(r: Rule) -> bool:
    return r.kind == "style" and all(is_admin_sel(x) for x in r.selectors)


def deck_range(sheet: Sheet) -> tuple[int, int]:
    """Rule index range [lo, hi) of the COMMAND DECK block: from its header comment to the
    next section header (SHARED UI PRIMITIVES)."""
    t = sheet.text
    a, b = t.index("═══ " + DECK_HEADER), t.index("═══ " + TAIL_HEADER)
    a = t.rfind("\n", 0, a) + 1
    b = t.rfind("\n", 0, b) + 1
    idx = [r.idx for r in sheet.rules if a <= r.start < b]
    return idx[0], idx[-1] + 1


def reachable_templates(ref: str) -> tuple[set[str], list[str]]:
    files = {p: blob(ref, p) for p in ls(ref, "templates")}
    ext = {p: re.findall(r"{%-?\s*extends\s+[\"']([^\"']+)", t) for p, t in files.items()}
    names = {p[len("templates/") :]: p for p in files}
    seeds = {"site/base_site.html"}
    changed = True
    while changed:  # everything whose extends-chain reaches base_site
        changed = False
        for p, e in ext.items():
            n = p[len("templates/") :]
            if n not in seeds and any(x in seeds for x in e):
                seeds.add(n)
                changed = True
    reach, todo, dynamic = set(), list(seeds) + ["base.html"], []
    while todo:
        n = todo.pop()
        if n in reach or n not in names:
            continue
        reach.add(n)
        t = files[names[n]]
        for m in re.finditer(
            r"{%-?\s*(?:include|import|from|extends)\s+([^%]+?)(?:\s+(?:as|import|with|ignore|context)\b.*)?-?%}",
            t,
        ):
            expr = m.group(1).strip()
            lit = re.fullmatch(r"[\"']([^\"']+)[\"']", expr)
            if lit:
                todo.append(lit.group(1))
            else:
                dynamic.append(f"{n}: {expr}")
    return {names[n] for n in reach}, dynamic


def strip_comments(text: str, kind: str) -> str:
    """Comments never emit classes: drop Jinja/HTML comments (templates) or // and /* */ (JS)."""
    if kind == "tpl":
        text = re.sub(r"{#.*?#}", " ", text, flags=re.S)
        return re.sub(r"<!--.*?-->", " ", text, flags=re.S)
    text = re.sub(r"/\*.*?\*/", " ", text, flags=re.S)
    return re.sub(r"(?m)^\s*//.*$", " ", text)


def py_strings(src: str):
    """String constants of a module, minus docstrings."""
    tree = ast.parse(src)
    doc = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
            and node.body
        ):
            first = node.body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                doc.add(id(first.value))
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in doc:
            yield node.value


def marketing_universe(ref: str) -> dict:
    tpls, dynamic = reachable_templates(ref)
    classes: set[str] = set()
    tags: set[str] = set(ALWAYS_TAGS)
    script_attrs: set[str] = set()
    for p in sorted(tpls):
        t = strip_comments(blob(ref, p), "tpl")
        classes |= tokens(t)
        tags |= {x.lower() for x in re.findall(r"<([A-Za-z][\w-]*)", t)}
        script_attrs |= set(re.findall(r"setAttribute\(\s*[\"']([\w:-]+)", t))
    js = ["static/site.js", "static/portfolio-filter.js", "static/lightbox.js"]
    for p in js:
        t = strip_comments(blob(ref, p), "js")
        classes |= tokens(t)
        script_attrs |= set(re.findall(r"setAttribute\(\s*[\"']([\w:-]+)", t))
        script_attrs |= {
            "data-" + re.sub(r"([A-Z])", lambda m: "-" + m.group(1).lower(), k)
            for k in re.findall(r"dataset\.(\w+)", t)
        }
        script_attrs |= {"aria-" + k for k in re.findall(r"aria-(\w+)", t)}
    py = [
        p
        for p in ls(ref, "app")
        if p.endswith(".py")
        and (p.startswith("app/public/") or p in ("app/specialties.py", "app/render.py"))
    ]
    for p in py:
        for value in py_strings(blob(ref, p)):
            classes |= tokens(value)
    return {
        "templates": sorted(tpls),
        "dynamic_includes": dynamic,
        "js": js,
        "py": py,
        "classes": classes,
        "tags": tags,
        "script_attrs": script_attrs,
    }


def load_corpus(ref: str, script_attrs: set[str]) -> list[dict]:
    pages = []
    for p in sorted(ls(ref, "tests/golden/site")):
        if not p.endswith(".html"):
            continue
        nodes = parse_html(blob(ref, p))
        idx = {"class": defaultdict(list), "id": defaultdict(list), "tag": defaultdict(list)}
        for n in nodes:
            idx["tag"][n.tag].append(n)
            if n.id:
                idx["id"][n.id].append(n)
            for c in n.classes:
                idx["class"][c].append(n)
        pages.append({"name": Path(p).stem, "nodes": nodes, "idx": idx})
    return pages


def selector_candidates(page: dict, sel: str):
    comps = parse_selector(sel)
    last = comps[-1]
    idx = page["idx"]
    if last.ids:
        return idx["id"].get(last.ids[0], [])
    if last.classes:
        return min((idx["class"].get(c, []) for c in last.classes), key=len)
    if last.tag and last.tag != "*":
        return idx["tag"].get(last.tag, [])
    return page["nodes"]


def rule_matches_page(rule: Rule, page: dict, script_attrs: set[str]) -> bool:
    for sel in rule.selectors:
        for n in selector_candidates(page, sel):
            if match_selector(n, sel, script_attrs):
                return True
    return False


# ── universe test ────────────────────────────────────────────────────────────
def selector_satisfiable(sel: str, uni: dict) -> bool:
    for comp in parse_selector(sel):
        if comp.tag and comp.tag != "*" and comp.tag not in uni["tags"]:
            return False
        if any(c not in uni["classes"] for c in comp.classes):
            return False
        if any(i not in uni["classes"] for i in comp.ids):
            return False
        for name, _op, _val in comp.attrs:
            if name not in uni["attr_names"]:
                return False
    return True


def keyframe_refs(text: str) -> set[str]:
    return set(re.findall(r"animation(?:-name)?\s*:[^;{}]*", text))


def classify(rules: list[Rule], uni: dict) -> dict[int, bool]:
    keep: dict[int, bool] = {}
    for r in rules:
        if r.kind == "style":
            keep[r.idx] = any(selector_satisfiable(s, uni) for s in r.selectors)
    # keyframes: kept when a kept rule names them
    used = " ".join(r.body for r in rules if keep.get(r.idx))
    for r in rules:
        if r.kind == "atomic":
            name = r.prelude.split(None, 1)[1].strip() if " " in r.prelude else ""
            keep[r.idx] = (
                bool(name)
                and re.search(r"(?<![-\w])" + re.escape(name) + r"(?![-\w])", used) is not None
            )
    return keep


def emit(sheet: Sheet, keep: dict[int, bool], title: str, notes: str) -> str:
    """Kept rules, verbatim and in original order, re-wrapped in their original at-rule
    conditions. Adjacent kept rules under the same condition share one wrapper."""
    out: list[str] = []
    open_ctx: tuple = ()
    for r in sheet.rules:
        if not keep.get(r.idx):
            continue
        if r.ctx != open_ctx:
            common = 0
            while common < min(len(r.ctx), len(open_ctx)) and r.ctx[common] == open_ctx[common]:
                common += 1
            for _ in open_ctx[common:]:
                out.append("}\n")
            for c in r.ctx[common:]:
                out.append(c + " {\n")
            open_ctx = r.ctx
        out.append(sheet.src(r) + "\n")
    for _ in open_ctx:
        out.append("}\n")
    return f"/* {title}\n{notes} */\n" + "".join(out)


# ── main analysis ────────────────────────────────────────────────────────────
def analyse(ref: str) -> dict:
    uni = marketing_universe(ref)
    sheets = {n: Sheet(n, blob(ref, "static/" + n)) for n in SHEETS}
    sheets["mise-admin.css"] = Sheet("mise-admin.css", blob(ref, "static/mise-admin.css"))
    uni["attr_names"] = set()
    for p in uni["templates"]:
        uni["attr_names"] |= {
            a.lower() for a in tokens(strip_comments(blob(ref, p), "tpl"))
        }  # any word may be a boolean attribute
    uni["attr_names"] |= uni["script_attrs"] | {"class", "id", "type", "lang", "dir"}
    pages = load_corpus(ref, uni["script_attrs"])
    mise = sheets["mise.css"]
    keep = classify(mise.rules, uni)
    # which rules do the goldens actually match?
    matched: dict[tuple[str, int], set[str]] = defaultdict(set)
    for name in SHEETS:
        for r in sheets[name].rules:
            if r.kind != "style":
                continue
            for pg in pages:
                if rule_matches_page(r, pg, uni["script_attrs"]):
                    matched[(name, r.idx)].add(pg["name"])
    miss = [
        (sheets["mise.css"].rules[i].line, sheets["mise.css"].rules[i].prelude[:90])
        for (n, i) in matched
        if n == "mise.css" and not keep[i]
    ]
    assert not miss, f"universe must cover golden matches: {miss[:20]}"
    return {"uni": uni, "sheets": sheets, "pages": pages, "keep": keep, "matched": matched}


def report(ref: str) -> tuple[str, dict]:
    a = analyse(ref)
    sheets, keep, matched, uni, pages = a["sheets"], a["keep"], a["matched"], a["uni"], a["pages"]
    mise, scr, tok = (
        sheets["mise.css"],
        sheets["screening.css"],
        sheets["screening-room-tokens.css"],
    )
    lo, hi = deck_range(scr)
    site_css = emit(
        mise,
        keep,
        "mise-site.css: the subset of mise.css the marketing pages can use (R-2)",
        "   Same @import layer(mise) as mise.css; rules verbatim, original order.",
    )
    deck_rules = [r for r in scr.rules if lo <= r.idx < hi]
    deck_text = scr.text[deck_rules[0].start : deck_rules[-1].end]
    non_deck = scr.text.replace(deck_text, "")
    rows = {}
    for n, s in [("mise.css", mise), ("screening.css", scr), ("screening-room-tokens.css", tok)]:
        rows[n] = sizes(s.text)
    rows["mise-admin.css"] = sizes(sheets["mise-admin.css"].text)
    rows["fonts.css"] = sizes(blob(ref, "static/fonts.css"))
    rows["[deck] screening.css COMMAND DECK block"] = sizes(deck_text)
    rows["[subset] mise-site.css"] = sizes(site_css)
    rows["[rest] screening.css without the deck"] = sizes(non_deck)
    kept = [r for r in mise.rules if keep.get(r.idx)]
    golden_hit = [r for r in mise.rules if ("mise.css", r.idx) in matched]
    lines = [
        "# R-2 measurement: which sheet supplies the rules the marketing pages use",
        "",
        f"Generated by `scripts/css_split_measure.py measure --ref {ref}`. Stdlib parser and an optimistic",
        "selector matcher (`scripts/css_split_lib.py`): a rule listed as unmatched can never apply to that page.",
        "",
        f"Corpus: {len(pages)} rendered marketing goldens (both themes, seeded and empty), "
        f"{sum(len(p['nodes']) for p in pages)} elements. Universe: {len(uni['templates'])} templates reachable from "
        f"`site/base_site.html`, {len(uni['js'])} marketing scripts, {len(uni['py'])} Python modules.",
        "",
        "## Sheet sizes (raw bytes / gzip -9)",
        "",
        "| Sheet | Raw | gz |",
        "|---|---:|---:|",
    ]
    lines += [f"| {k} | {v[0]:,} | {v[1]:,} |" for k, v in rows.items()]
    lines += [
        "",
        "## mise.css rules, by what they do for marketing",
        "",
        f"- {len(mise.rules)} rules/at-rules in `mise.css`.",
        f"- {len(golden_hit)} style rules match at least one element of a golden (so they are live today).",
        f"- {len(kept)} kept in `mise-site.css` (universe-satisfiable; includes the {len(golden_hit)} above, and "
        f"{len(kept) - len(golden_hit)} rules kept only because a class/id/tag they need exists in a reachable template, "
        "script or Python string, e.g. a state the goldens do not render).",
        f"- {len(mise.rules) - len(kept)} dropped: each needs a class, id, tag or attribute that no marketing template, "
        "script or Python string contains.",
        "",
    ]
    # per-token table
    tok_rules: dict[str, dict[str, set]] = defaultdict(lambda: defaultdict(set))
    page_count: dict[str, set] = defaultdict(set)
    for pg in pages:
        for n in pg["nodes"]:
            for c in n.classes:
                page_count["." + c].add(pg["name"])
            if n.id:
                page_count["#" + n.id].add(pg["name"])
            page_count[n.tag].add(pg["name"])
    for n_, s in [("mise.css", mise), ("tokens", tok), ("screening.css", scr)]:
        for r in s.rules:
            if r.kind != "style" or (s.name, r.idx) not in matched:
                continue
            col = n_
            if n_ == "screening.css" and lo <= r.idx < hi:
                col = "deck"
            toks = set()
            for sel in r.selectors:
                for comp in parse_selector(sel):
                    toks |= {"." + c for c in comp.classes} | {"#" + i for i in comp.ids}
                    if comp.tag and comp.tag != "*":
                        toks.add(comp.tag)
            for t in toks:
                tok_rules[t][col].add(r.idx)
    lines += [
        "## Every class, id and element the goldens use, and the sheet that supplies matching rules",
        "",
        "Counts are rules (in that sheet) whose selector mentions the token and match an element of some golden. "
        "`deck` is the COMMAND DECK block of `screening.css`.",
        "",
        "| Token | Pages | mise.css | tokens | screening.css | deck |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for t in sorted(page_count, key=lambda x: (x[0] not in ".#", x)):
        c = tok_rules.get(t, {})
        lines.append(
            f"| `{t}` | {len(page_count[t])} | {len(c.get('mise.css', ()))} | {len(c.get('tokens', ()))} | "
            f"{len(c.get('screening.css', ()))} | {len(c.get('deck', ()))} |"
        )
    # tokens with no rule anywhere
    bare = [t for t in sorted(page_count) if t[0] in ".#" and not tok_rules.get(t)]
    lines += [
        "",
        f"Classes/ids the goldens use that no rule matches ({len(bare)}): "
        + ", ".join(f"`{b}`" for b in bare),
        "",
    ]
    # the block is NOT purely admin: split it by selector
    adm = [r for r in deck_rules if is_admin_rule(r)]
    pub = [r for r in deck_rules if r.kind == "style" and not is_admin_rule(r)]
    mixed = [
        r
        for r in deck_rules
        if r.kind == "style" and any(is_admin_sel(x) for x in r.selectors) and not is_admin_rule(r)
    ]
    deck_hit = [r for r in deck_rules if r.kind == "style" and (scr.name, r.idx) in matched]
    tail_admin = [r for r in scr.rules[hi:] if is_admin_rule(r)]
    lines += [
        "## What is inside the COMMAND DECK block",
        "",
        f"Lines {deck_rules[0].line}-{deck_rules[-1].line} of `screening.css` hold "
        f"{len(adm)} admin-only rules (every selector needs `.sr-admin` or `.admin-shell`), "
        f"{len(pub)} public/shared rules (responsive breakpoints, reveal stagger, ticker, view transitions, "
        f"film-grain and Ken Burns motion, card and filmstrip hover) and "
        f"{len(deck_rules) - len(adm) - len(pub)} at-rules (keyframes, `@view-transition`). "
        f"{len(mixed)} rules mix both kinds. {len(deck_hit)} block rules match a marketing golden, "
        "all of them from the public group:",
    ]
    assert all(not is_admin_rule(r) for r in deck_hit), (
        "an admin-only rule matched a marketing page"
    )
    for r in deck_hit:
        lines.append(
            f"- line {r.line}: `{r.prelude[:100]}` ({len(matched[(scr.name, r.idx)])} pages)"
        )
    lines += [
        "",
        f"Outside the block, {len(tail_admin)} more admin-only rules sit in the SHARED UI PRIMITIVES tail "
        f"(lines {tail_admin[0].line}-{tail_admin[-1].line}); R-2 leaves them where they are.",
        "",
    ]
    lines += ["", "## Dynamic includes the universe could not resolve", ""]
    lines += [f"- {d}" for d in uni["dynamic_includes"]] or ["- none"]
    lines.append("")
    return "\n".join(lines), {"a": a, "site_css": site_css, "rows": rows, "lo": lo, "hi": hi}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["measure", "subset"])
    ap.add_argument("--ref", default=BASE_REF)
    ap.add_argument("--md")
    ap.add_argument("--json")
    args = ap.parse_args()
    md, ctx = report(args.ref)
    if args.cmd == "subset":
        sys.stdout.write(ctx["site_css"])
        return
    if args.md:
        Path(args.md).write_text(md)
    else:
        print(md)
    if args.json:
        Path(args.json).write_text(
            json.dumps({k: list(v) for k, v in ctx["rows"].items()}, indent=1)
        )


if __name__ == "__main__":
    main()
