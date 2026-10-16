#!/usr/bin/env python3
"""Structure metrics for public site pages (plan R-1).

Counts sections, headings, images (lazy / missing alt), `sr-mono` labels and
the decorative kickers among them (plan D-4), em-dashes and middots in visible
text, CTA labels and bytes. The golden render test
(tests/test_public_render_golden.py) loads this file, so the test and this
script report the same numbers.

  scripts/site_metrics.py page.html [more.html ...]
  scripts/site_metrics.py --base http://127.0.0.1:8471 [/ /portraits ...]
"""

import argparse
import re
import sys
import urllib.request
from html.parser import HTMLParser

ROUTES = [
    "/",
    "/real-estate",
    "/portraits",
    "/food-beverage",
    "/portfolio",
    "/work",
    "/services",
    "/about",
    "/contact",
    "/book",
    "/reels",
]
KEYS = (
    "sections",
    "h1",
    "h2",
    "h3",
    "img",
    "img_lazy",
    "img_noalt",
    "sr_3up",
    "sr_mono",
    "decorative_kickers",
    "emdash",
    "middot",
    "cta_labels",
    "bytes",
)


# Data labels the kicker ration exempts (plan D-4 keep-list): state, chapters, next openings,
# prices, durations, frame numbers, counts and PINs. Links are exempt by tag.
_DATA_LABEL = re.compile(
    r"Now showing|^Chapters$|^Next openings|\$\d|\b\d+:\d\d\b|FR·|^\d+ (still|film)|^PIN\b"
)


def _is_cta(cls):
    return "sr-btn" in cls or "svc-cta" in cls or "btn" in cls


def count_class(markup, cls):
    """Elements whose class list carries `cls` (plan D-6: `.sr-3up` at most once per page)."""
    return sum(cls in m.split() for m in re.findall(r'\bclass="([^"]*)"', markup))


class _Counter(HTMLParser):
    def __init__(self):
        super().__init__()
        self.stack = []
        self.text = []
        self.skip = False
        self.counts = dict.fromkeys(KEYS[:-3], 0)
        self.ctas = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        cls = a.get("class") or ""
        if tag in ("script", "style"):
            self.skip = True
        if tag == "section":
            self.counts["sections"] += 1
        if tag in ("h1", "h2", "h3"):
            self.counts[tag] += 1
        if tag == "img":
            self.counts["img"] += 1
            self.counts["img_lazy"] += a.get("loading") == "lazy"
            self.counts["img_noalt"] += "alt" not in a
        if "sr-mono" in cls.split():
            self.counts["sr_mono"] += 1
        if tag == "a" and _is_cta(cls):
            self.ctas.append("")
        self.stack.append((tag, cls))

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self.skip = False
        if self.stack:
            self.stack.pop()

    def handle_data(self, data):
        text = data.strip()
        if self.skip or not text:
            return
        self.text.append(text)
        if self.stack and self.stack[-1][0] == "a" and self.ctas and _is_cta(self.stack[-1][1]):
            self.ctas[-1] += text


class _Kickers(HTMLParser):
    """Decorative section kickers: `sr-mono` elements that are not links, not per-card
    labels (inside `.sr-card-head`), not data labels (_DATA_LABEL), and not the one named
    structural data-label class (`sr-mono--data`, the F&B included/quoted-separately list headers).
    No tag is exempt as a whole: an `sr-mono` inside an <aside> counts like any other."""

    VOID = set("area base br col embed hr img input link meta source track wbr".split())

    def __init__(self):
        super().__init__()
        self.stack = []
        self.found = []

    def handle_starttag(self, tag, attrs):
        if tag in self.VOID:
            return
        classes = (dict(attrs).get("class") or "").split()
        inside = any("sr-card-head" in c for _t, c, _ in self.stack)
        entry = None
        if "sr-mono" in classes and "sr-mono--data" not in classes and tag != "a" and not inside:
            entry = []
            self.found.append(entry)
        self.stack.append((tag, classes, entry))

    def handle_endtag(self, tag):
        if tag not in self.VOID and self.stack:
            self.stack.pop()

    def handle_data(self, data):
        for _tag, _cls, entry in self.stack:
            if entry is not None:
                entry.append(data)


def decorative_kickers(markup):
    """Texts of the page's decorative kickers (plan D-4; ration ceil(sections/3))."""
    parser = _Kickers()
    parser.feed(markup)
    texts = [" ".join("".join(parts).split()) for parts in parser.found]
    return [t for t in texts if not _DATA_LABEL.search(t)]


def metrics(markup):
    """Return the metrics dict for one page's HTML."""
    parser = _Counter()
    parser.feed(markup)
    visible = " ".join(parser.text)
    return {
        **parser.counts,
        "decorative_kickers": len(decorative_kickers(markup)),
        "emdash": visible.count("—"),
        "middot": visible.count("·"),
        "sr_3up": count_class(markup, "sr-3up"),
        "cta_labels": sorted({label.strip() for label in parser.ctas if label.strip()}),
        "bytes": len(markup.encode("utf-8")),
    }


def line(name, m):
    head = " ".join(f"{k}={m[k]}" for k in KEYS if k != "cta_labels")
    return f"{name}: {head} cta_labels={m['cta_labels']}"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--base", help="origin to GET, e.g. http://127.0.0.1:8471")
    ap.add_argument("paths", nargs="*", help="HTML files, or routes with --base")
    args = ap.parse_args(argv)
    if args.base:
        for route in args.paths or ROUTES:
            url = args.base.rstrip("/") + route
            with urllib.request.urlopen(url) as resp:  # operator-supplied origin
                print(line(route, metrics(resp.read().decode("utf-8"))))
    else:
        if not args.paths:
            ap.error("give HTML files, or --base URL")
        for path in args.paths:
            with open(path, encoding="utf-8") as fh:
                print(line(path, metrics(fh.read())))
    return 0


if __name__ == "__main__":
    sys.exit(main())
