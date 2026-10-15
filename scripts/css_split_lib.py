"""Small CSS/HTML cascade toolkit for the R-2 stylesheet split (stdlib only).

Not a general CSS engine. It parses the repo's own sheets into rules, parses
selectors well enough to compute specificity, and matches them against rendered
HTML *optimistically* (a dynamic pseudo-class or a script-set attribute is
assumed to match). Over-matching is the safe direction for a "this rule can
never apply here" proof.
"""

from __future__ import annotations

import gzip
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser

GROUP_AT = {"media", "supports", "layer", "container", "document"}
ATOMIC_AT = {
    "view-transition",
    "font-face",
    "keyframes",
    "-webkit-keyframes",
    "property",
    "page",
    "counter-style",
}
VOID = {
    "area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta",
    "param", "source", "track", "wbr",
}  # fmt: skip


# ── CSS parsing ──────────────────────────────────────────────────────────────
@dataclass
class Rule:
    sheet: str
    idx: int  # document order within the sheet (leaf rules and atomic at-rules)
    kind: str  # "style" | "atomic" | "stmt"
    ctx: tuple  # enclosing group at-rule preludes, outermost first
    prelude: str  # selector text, or the at-rule prelude
    body: str
    start: int  # offset of the rule text (prelude start) in the sheet
    end: int  # offset just past the closing brace / semicolon
    line: int
    selectors: list = field(default_factory=list)
    props: list = field(default_factory=list)  # (name, important)

    @property
    def sig(self) -> str:
        return (
            " | ".join(self.ctx)
            + " || "
            + " ".join(self.prelude.split())
            + " {"
            + " ".join(self.body.split())
            + "}"
        )


def _skip_comment_or_string(s: str, i: int) -> int:
    """Return the index after the comment/string starting at i, or i if none."""
    c = s[i]
    if c == "/" and s.startswith("/*", i):
        j = s.find("*/", i + 2)
        return len(s) if j < 0 else j + 2
    if c in "\"'":
        j = i + 1
        while j < len(s) and s[j] != c:
            j += 2 if s[j] == "\\" else 1
        return j + 1
    return i


def _scan(s: str, i: int, stops: str) -> int:
    """Index of the first char in `stops` at paren/bracket depth 0, skipping comments/strings."""
    depth = 0
    while i < len(s):
        j = _skip_comment_or_string(s, i)
        if j != i:
            i = j
            continue
        c = s[i]
        if depth <= 0 and c in stops:
            return i
        if c in "([":
            depth += 1
        elif c in ")]":
            depth -= 1
        i += 1
    return -1


def _match_brace(s: str, i: int) -> int:
    """i is at '{'; return the index of the matching '}'."""
    depth = 0
    while i < len(s):
        j = _skip_comment_or_string(s, i)
        if j != i:
            i = j
            continue
        if s[i] == "{":
            depth += 1
        elif s[i] == "}":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    raise ValueError("unbalanced braces")


def split_commas(sel: str) -> list[str]:
    out, depth, cur, i = [], 0, [], 0
    while i < len(sel):
        j = _skip_comment_or_string(sel, i)
        if j != i:
            cur.append(sel[i:j])
            i = j
            continue
        c = sel[i]
        if c in "([":
            depth += 1
        elif c in ")]":
            depth -= 1
        if c == "," and depth == 0:
            out.append("".join(cur).strip())
            cur = []
        else:
            cur.append(c)
        i += 1
    out.append("".join(cur).strip())
    return [x for x in out if x]


def parse_decls(body: str) -> list[tuple[str, bool]]:
    props, i = [], 0
    while i < len(body):
        j = _scan(body, i, ";")
        chunk = body[i : len(body) if j < 0 else j]
        chunk = re.sub(r"/\*.*?\*/", "", chunk, flags=re.S).strip()
        if ":" in chunk:
            name, _, val = chunk.partition(":")
            props.append((name.strip().lower(), "!important" in val.replace(" ", "").lower()))
        if j < 0:
            break
        i = j + 1
    return props


class Sheet:
    def __init__(self, name: str, text: str):
        self.name, self.text, self.rules = name, text, []
        # top-level units in document order: (start, end, first_rule_idx, last_rule_idx + 1)
        self.units: list[tuple[int, int, int, int]] = []
        self._nl = [m.start() for m in re.finditer("\n", text)]
        self._parse(0, len(text), ())

    def line_of(self, off: int) -> int:
        import bisect

        return bisect.bisect_left(self._nl, off) + 1

    def _parse(self, i: int, stop: int, ctx: tuple) -> None:
        s = self.text
        prev = -1
        while True:
            if i <= prev:  # progress guard
                raise ValueError(f"{self.name}: no progress at offset {i}")
            prev = i
            # skip whitespace and comments
            while i < stop:
                if s[i].isspace():
                    i += 1
                elif s.startswith("/*", i):
                    j = s.find("*/", i + 2)
                    i = stop if j < 0 else j + 2
                else:
                    break
            if i >= stop:
                return
            j = _scan(s, i, "{;}")
            if j < 0 or j >= stop:
                raise ValueError(f"{self.name}: unterminated at {self.line_of(i)}")
            prelude = s[i:j].strip()
            if s[j] == ";":
                self._add("stmt", ctx, prelude, "", i, j + 1)
                i = j + 1
                continue
            if s[j] == "}":
                raise ValueError(f"{self.name}: stray }} at line {self.line_of(j)}")
            close = _match_brace(s, j)
            body = s[j + 1 : close]
            if prelude.startswith("@"):
                m = re.match(r"@([-\w]+)", prelude)
                nm = m.group(1).lower()
                if nm in GROUP_AT:
                    first = len(self.rules)
                    self._parse(j + 1, close, ctx + (" ".join(prelude.split()),))
                    if not ctx:
                        self.units.append((i, close + 1, first, len(self.rules)))
                elif nm in ATOMIC_AT:
                    self._add("atomic", ctx, prelude, body, i, close + 1)
                else:
                    raise ValueError(
                        f"{self.name}: unknown at-rule {prelude[:40]} line {self.line_of(i)}"
                    )
            else:
                if "{" in body:
                    raise ValueError(
                        f"{self.name}: nested rule in {prelude[:40]} line {self.line_of(i)}"
                    )
                self._add("style", ctx, prelude, body, i, close + 1)
            i = close + 1

    def _add(self, kind, ctx, prelude, body, start, end):
        r = Rule(
            self.name, len(self.rules), kind, ctx, prelude, body, start, end, self.line_of(start)
        )
        if kind == "style":
            r.selectors = split_commas(prelude)
            r.props = parse_decls(body)
        if not ctx:
            self.units.append((start, end, len(self.rules), len(self.rules) + 1))
        self.rules.append(r)

    def src(self, r: Rule) -> str:
        return self.text[r.start : r.end]


def sizes(text: str) -> tuple[int, int]:
    b = text.encode()
    return len(b), len(gzip.compress(b, 9, mtime=0))


# ── selectors ────────────────────────────────────────────────────────────────
@dataclass
class Compound:
    tag: str | None = None
    ids: list = field(default_factory=list)
    classes: list = field(default_factory=list)
    attrs: list = field(default_factory=list)  # (name, op, value)
    pseudos: list = field(default_factory=list)  # (name, arg|None)
    comb: str = " "  # combinator linking this compound to the PREVIOUS one


_IDENT = r"(?:\\.|[-\w\u0080-￿])+"


def parse_selector(sel: str) -> list[Compound]:
    s, i, out, cur, comb = sel.strip(), 0, [], None, " "
    n = len(s)
    prev = -1
    while i < n:
        if i <= prev:  # progress guard: the position must advance every iteration
            raise ValueError(f"no progress parsing selector {sel!r} at {i}")
        prev = i
        c = s[i]
        if c.isspace() or c in ">+~":
            j = i
            seen = None
            while j < n and (s[j].isspace() or s[j] in ">+~"):
                if s[j] in ">+~":
                    seen = s[j]
                j += 1
            if cur is not None:
                out.append(cur)
                cur = None
            comb = seen or " "
            i = j
            continue
        if cur is None:
            cur = Compound(comb=comb)
        if c == "*":
            cur.tag = "*"
            i += 1
        elif c == "#":
            m = re.compile(_IDENT).match(s, i + 1)
            cur.ids.append(m.group(0))
            i = m.end()
        elif c == ".":
            m = re.compile(_IDENT).match(s, i + 1)
            cur.classes.append(m.group(0).replace("\\", ""))
            i = m.end()
        elif c == "[":
            j = _scan(s, i + 1, "]")
            if j < 0:
                raise ValueError(f"unterminated [ in selector {sel!r}")
            body = s[i + 1 : j]
            m = re.match(
                r"\s*([-\w:]+)\s*(?:([~|^$*]?=)\s*(?:\"([^\"]*)\"|'([^']*)'|([^\s\]]+)))?", body
            )
            val = next((g for g in (m.group(3), m.group(4), m.group(5)) if g is not None), None)
            cur.attrs.append((m.group(1).lower(), m.group(2), val))
            i = j + 1
        elif c == ":":
            m = re.compile(r"::?(" + _IDENT + ")").match(s, i)
            name = m.group(1).lower()
            i = m.end()
            arg = None
            if i < n and s[i] == "(":
                j = _scan(s, i + 1, ")")
                if j < 0:
                    raise ValueError(f"unterminated ( in selector {sel!r}")
                arg = s[i + 1 : j]
                i = j + 1
            cur.pseudos.append((name, arg, m.group(0).startswith("::")))
        else:
            m = re.compile(_IDENT).match(s, i)
            if not m:
                raise ValueError(f"cannot parse selector {sel!r} at {i}")
            cur.tag = m.group(0).lower()
            i = m.end()
    if cur is not None:
        out.append(cur)
    return out


_FUNC_MAX = {"is", "not", "has", "matches", "-webkit-any", "-moz-any"}
LEGACY_PSEUDO_ELEMENTS = {"before", "after", "first-line", "first-letter"}


def specificity(sel: str) -> tuple[int, int, int]:
    a = b = c = 0
    for comp in parse_selector(sel):
        a += len(comp.ids)
        b += len(comp.classes) + len(comp.attrs)
        if comp.tag and comp.tag != "*":
            c += 1
        for name, arg, dbl in comp.pseudos:
            if dbl or name in LEGACY_PSEUDO_ELEMENTS:
                c += 1
            elif name == "where":
                pass
            elif name in _FUNC_MAX and arg:
                best = max((specificity(x) for x in split_commas(arg)), default=(0, 0, 0))
                a, b, c = a + best[0], b + best[1], c + best[2]
            elif name in ("nth-child", "nth-last-child") and arg:
                b += 1
                m = re.search(r"\bof\s+(.*)$", arg)
                if m:
                    best = max(
                        (specificity(x) for x in split_commas(m.group(1))), default=(0, 0, 0)
                    )
                    a, b, c = a + best[0], b + best[1], c + best[2]
            else:
                b += 1
    return (a, b, c)


# ── DOM and matching ─────────────────────────────────────────────────────────
class Node:
    __slots__ = ("tag", "attrs", "parent", "children", "classes", "id")

    def __init__(self, tag, attrs, parent):
        self.tag, self.attrs, self.parent, self.children = tag, dict(attrs), parent, []
        self.classes = set((self.attrs.get("class") or "").split())
        self.id = self.attrs.get("id")


class _DomBuilder(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = Node("#root", [], None)
        self.cur = self.root
        self.nodes = []

    def handle_starttag(self, tag, attrs):
        n = Node(tag, [(k, v if v is not None else "") for k, v in attrs], self.cur)
        self.cur.children.append(n)
        self.nodes.append(n)
        if tag not in VOID:
            self.cur = n

    def handle_startendtag(self, tag, attrs):
        n = Node(tag, [(k, v if v is not None else "") for k, v in attrs], self.cur)
        self.cur.children.append(n)
        self.nodes.append(n)

    def handle_endtag(self, tag):
        n = self.cur
        while n is not self.root and n.tag != tag:
            n = n.parent
        if n is not self.root:
            self.cur = n.parent


def parse_html(text: str) -> list[Node]:
    b = _DomBuilder()
    b.feed(text)
    return b.nodes


def _sibs(n: Node):
    return n.parent.children if n.parent else [n]


def _match_attr(n: Node, name, op, val, script_set: set[str]) -> bool:
    if name not in n.attrs:
        return name in script_set  # a script may add it at runtime: optimistic
    if op is None:
        return True
    have = n.attrs[name]
    return {
        "=": have == val,
        "~=": val in have.split(),
        "|=": have == val or have.startswith(val + "-"),
        "^=": have.startswith(val),
        "$=": have.endswith(val),
        "*=": val in have,
    }[op]


def match_compound(n: Node, c: Compound, script_set: set[str]) -> bool:
    if c.tag and c.tag != "*" and n.tag != c.tag:
        return False
    if any(i != n.id for i in c.ids):
        return False
    if any(k not in n.classes for k in c.classes):
        return False
    if any(not _match_attr(n, a, o, v, script_set) for a, o, v in c.attrs):
        return False
    for name, arg, dbl in c.pseudos:
        if dbl or name in LEGACY_PSEUDO_ELEMENTS:
            continue  # applies to a generated box of n: treat as n matching
        if name in ("is", "where", "matches", "-webkit-any", "-moz-any") and arg:
            if not any(match_selector(n, x, script_set) for x in split_commas(arg)):
                return False
        elif name == "not" and arg:
            # optimistic: fails only if some alternative matches AND is purely static
            for x in split_commas(arg):
                comps = parse_selector(x)
                if (
                    len(comps) == 1
                    and _static(comps[0])
                    and match_compound(n, comps[0], script_set)
                ):
                    return False
        elif name == "first-child" and _sibs(n)[0] is not n:
            return False
        elif name == "last-child" and _sibs(n)[-1] is not n:
            return False
        elif name == "only-child" and len(_sibs(n)) != 1:
            return False
        elif name == "empty" and n.children:
            return False
        elif name == "root" and n.tag != "html":
            return False
        # every other pseudo-class (hover, focus, checked, nth-*, has, ...): optimistic
    return True


def _static(c: Compound) -> bool:
    return not c.pseudos and not c.attrs


def match_selector(n: Node, sel: str, script_set: set[str], _cache={}) -> bool:
    comps = _cache.get(sel)
    if comps is None:
        comps = _cache[sel] = parse_selector(sel)
    return _match_from(n, comps, len(comps) - 1, script_set)


def _match_from(n: Node, comps, k: int, script_set) -> bool:
    if not match_compound(n, comps[k], script_set):
        return False
    if k == 0:
        return True
    comb = comps[k].comb
    if comb == ">":
        p = n.parent
        return p is not None and p.tag != "#root" and _match_from(p, comps, k - 1, script_set)
    if comb == " ":
        p = n.parent
        while p is not None and p.tag != "#root":
            if _match_from(p, comps, k - 1, script_set):
                return True
            p = p.parent
        return False
    sibs = _sibs(n)
    pos = sibs.index(n)
    if comb == "+":
        return pos > 0 and _match_from(sibs[pos - 1], comps, k - 1, script_set)
    return any(_match_from(s2, comps, k - 1, script_set) for s2 in sibs[:pos])  # "~"


# ── universe extraction (what could ever appear on a surface) ────────────────
TOKEN = re.compile(r"[A-Za-z_][-\w]*")


def tokens(text: str) -> set[str]:
    return set(TOKEN.findall(text))
