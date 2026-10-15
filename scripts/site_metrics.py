#!/usr/bin/env python3
"""Structure metrics for public site pages (plan R-1).

Counts sections, headings, images (lazy / missing alt), `sr-mono` labels,
em-dashes and middots in visible text, CTA labels and bytes. The golden render
test (tests/test_public_render_golden.py) loads this file, so the test and this
script report the same numbers.

  scripts/site_metrics.py page.html [more.html ...]
  scripts/site_metrics.py --base http://127.0.0.1:8471 [/ /portraits ...]
"""

import argparse
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
    "sr_mono",
    "emdash",
    "middot",
    "cta_labels",
    "bytes",
)


def _is_cta(cls):
    return "sr-btn" in cls or "svc-cta" in cls or "btn" in cls


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


def metrics(markup):
    """Return the metrics dict for one page's HTML."""
    parser = _Counter()
    parser.feed(markup)
    visible = " ".join(parser.text)
    return {
        **parser.counts,
        "emdash": visible.count("—"),
        "middot": visible.count("·"),
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
