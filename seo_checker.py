import os
import json
import sys
import argparse
import re
import threading
import time
import itertools
from typing import Optional, Dict, Any, List
from datetime import datetime, timezone

from bs4 import BeautifulSoup
import requests

# Silence urllib3's NotOpenSSLWarning on macOS with LibreSSL to reduce noise
try:
    import warnings
    from urllib3.exceptions import NotOpenSSLWarning
    warnings.filterwarnings("ignore", category=NotOpenSSLWarning)
except Exception:
    pass

# Import your individual check functions
from title_meta_checker import check_title_and_meta
from headings_checker import check_headings
from schema_checker import check_schema
from mobile_checker import check_mobile_responsiveness
from fetch_utils import fetch_html
from robots_sitemap_checker import check_robots_and_sitemaps
from links_checker import check_internal_links
from image_checker import check_images
from canonical_checker import check_canonical_and_hreflang
from indexability_checker import check_indexability
from faq_checker import check_faq

def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run SEO checks on a URL")
    parser.add_argument("urls", nargs="*", help="One or more target URLs to audit")
    parser.add_argument("--timeout", type=int, default=20, help="Request timeout in seconds (default: 20)")
    parser.add_argument("--use-scraperapi", action="store_true", help="Use ScraperAPI if SCRAPERAPI_KEY is set")
    parser.add_argument("--max-links", type=int, default=25, help="Max internal links to verify (default: 25)")
    parser.add_argument("--output-json", type=str, help="Path to write JSON results")
    parser.add_argument("--output-csv", type=str, help="Path to write CSV summary")
    parser.add_argument("--quiet", action="store_true", help="Suppress non-error console output")
    parser.add_argument(
        "--format",
        choices=["table", "json", "both"],
        default="table",
        help="Console output format (default: table)",
    )
    parser.add_argument(
        "--threshold", type=float, default=80.0,
        help="QA pass threshold as percentage (default: 80)")
    parser.add_argument(
        "--url-file", type=str, help="Optional file with URLs (one per line) to include"
    )
    parser.add_argument(
        "--history-file", type=str, default=".seo_checker_history.jsonl",
        help="Path to history file (JSON Lines). Default: .seo_checker_history.jsonl"
    )
    parser.add_argument(
        "--no-history", action="store_true",
        help="Disable appending results to the history file"
    )
    parser.add_argument(
        "--show-history", nargs="?", const=20, type=int,
        help="Show the last N history entries (default 20) and exit"
    )
    parser.add_argument(
        "--keyword", type=str,
        help="Optional target keyword to verify in title/description"
    )
    # Firewall/proxy options
    parser.add_argument(
        "--proxy", type=str,
        help="HTTP(S) proxy URL to use for all requests (overrides env HTTP(S)_PROXY)"
    )
    parser.add_argument(
        "--ca-bundle", type=str,
        help="Path to custom CA bundle file for TLS verification (sets REQUESTS_CA_BUNDLE)"
    )
    parser.add_argument(
        "--insecure", action="store_true",
        help="Disable TLS verification (not recommended)."
    )
    return parser.parse_args(argv)

def run_all_checks(url: str, *, timeout: int = 20, use_scraperapi: bool = False, max_links: int = 25, quiet: bool = False, keyword: Optional[str] = None) -> Dict[str, Any]:
    """
    Runs all defined SEO checks on a given URL.
    
    Args:
        url (str): The URL of the website to check.
        
    Returns:
        dict: A dictionary containing the results of all checks.
    """
    if not quiet:
        print(f"Starting SEO checks for: {url}")
    results = {}

    spinner = Spinner("Fetching HTML", enabled=_tty_color_enabled() and not quiet)
    spinner.start()
    html = fetch_html(url, timeout=timeout, use_scraperapi=use_scraperapi)
    spinner.stop("Fetched HTML" if html else "Fetch failed")
    if not html:
        return {"error": "Failed to access the URL. Consider setting SCRAPERAPI_KEY for tougher sites."}

    soup = BeautifulSoup(html, 'html.parser')

    def _spin_step(label: str, fn):
        sp = Spinner(f"{label}", enabled=_tty_color_enabled() and not quiet)
        sp.start()
        try:
            out = fn()
            sp.stop(f"{label} done")
            return out
        except Exception as e:
            sp.stop(f"{label} failed")
            return {"status": "error", "message": f"{label} error: {e}"}

    # Run each check and store the results
    results['title_meta'] = _spin_step("Title & Meta", lambda: check_title_and_meta(soup, keyword=keyword))
    results['headings'] = _spin_step("Headings", lambda: check_headings(soup))
    results['schema'] = _spin_step("Schema", lambda: check_schema(soup))
    results['mobile_responsiveness'] = _spin_step("Mobile", lambda: check_mobile_responsiveness(url))
    # New checks
    results['robots_sitemaps'] = _spin_step("Robots & Sitemaps", lambda: check_robots_and_sitemaps(url, timeout=timeout, use_scraperapi=use_scraperapi))
    results['internal_links'] = _spin_step("Internal Links", lambda: check_internal_links(html, url, timeout=timeout, max_links=max_links))
    results['images'] = _spin_step("Images", lambda: check_images(soup))
    results['canonical_hreflang'] = _spin_step("Canonical & Hreflang", lambda: check_canonical_and_hreflang(soup, url))
    results['indexability'] = _spin_step("Indexability", lambda: check_indexability(url, soup))
    results['faq'] = _spin_step("FAQ", lambda: check_faq(soup))

    # Cross-check: author meta vs schema authors
    def _author_match():
        tm_author = (results.get('title_meta', {}).get('author', {}) or {}).get('content')
        sc_authors = [a.strip() for a in (results.get('schema', {}).get('authors') or []) if isinstance(a, str)]
        if tm_author and sc_authors:
            if any(tm_author.lower() == a.lower() for a in sc_authors):
                results['title_meta']['author']['status'] = 'ok'
                results['title_meta']['author']['message'] = 'Author matches schema.'
            else:
                results['title_meta']['author']['status'] = 'warning'
                results['title_meta']['author']['message'] = 'Author meta does not match schema author(s).'
        return True
    _spin_step("Author Match", _author_match)

    if not quiet:
        print(colorize("All checks completed.", "info"))
    return results

def _truncate(value: Any, limit: int = 80) -> str:
    s = ", ".join(value) if isinstance(value, list) else str(value)
    return s if len(s) <= limit else s[: limit - 1] + "…"

ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")

def _strip_ansi(s: str) -> str:
    return ANSI_RE.sub("", s)

def _tty_color_enabled() -> bool:
    return sys.stdout.isatty() and os.environ.get("NO_COLOR") is None

def colorize(text: str, status: Optional[str] = None) -> str:
    if not _tty_color_enabled():
        return text
    s = (status or "").lower()
    # Map statuses and common words to colors
    if s in ("pass", "ok", "found", "yes", "present", "info", "success"):
        color = "32"  # green
    elif s in ("warning", "warn"):
        color = "33"  # yellow
    elif s in ("fail", "missing", "no", "error"):
        color = "31"  # red
    else:
        # fallback neutral (cyan for info text if explicitly requested)
        color = "36" if s == "info" else "0"
    return f"\x1b[{color}m{text}\x1b[0m" if color != "0" else text

def _color_for_cell(header: str, value: str) -> Optional[str]:
    key = header.strip().lower()
    val = (value or "").strip().lower()
    if key in ("status", "present"):
        # Normalize booleans
        if val in ("true", "yes"):
            return "pass"
        if val in ("false", "no"):
            return "fail"
        return val  # already a status string
    return None

def _pad_visible(s: str, width: int) -> str:
    # Pad based on visible length (exclude ANSI sequences)
    length = len(_strip_ansi(s))
    if length < width:
        return s + (" " * (width - length))
    return s

def _print_table(title: str, headers: List[str], rows: List[List[Any]]):
    print(f"\n== {title} ==")
    # Compute column widths by visible length
    cols = len(headers)
    widths = [len(h) for h in headers]
    raw_rows: List[List[str]] = []
    for row in rows:
        raw = ["" if i >= len(row) or row[i] is None else str(row[i]) for i in range(cols)]
        raw_rows.append(raw)
        for i in range(cols):
            vis_len = len(_strip_ansi(raw[i]))
            if vis_len > widths[i]:
                widths[i] = vis_len
    # Header
    header_line = "| " + " | ".join(h.ljust(widths[i]) for i, h in enumerate(headers)) + " |"
    sep_line = "|-" + "-|-".join("-" * widths[i] for i in range(cols)) + "-|"
    print(header_line)
    print(sep_line)
    # Rows with color applied to status-like columns
    for raw in raw_rows:
        colored_cells: List[str] = []
        for i in range(cols):
            header = headers[i]
            val = raw[i]
            status_hint = _color_for_cell(header, val)
            cell = colorize(val, status_hint) if status_hint else val
            colored_cells.append(_pad_visible(cell, widths[i]))
        print("| " + " | ".join(colored_cells) + " |")

class Spinner:
    def __init__(self, message: str, enabled: bool = True):
        self.message = message
        self.enabled = enabled
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._frames = itertools.cycle(["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"])  # Braille spinner

    def start(self):
        if not self.enabled:
            return
        def run():
            while not self._stop.is_set():
                frame = next(self._frames)
                sys.stdout.write(f"\r{frame} {self.message}    ")
                sys.stdout.flush()
                time.sleep(0.08)
        self._thread = threading.Thread(target=run, daemon=True)
        self._thread.start()

    def stop(self, final_message: Optional[str] = None):
        if not self.enabled:
            return
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=0.2)
        msg = final_message or self.message
        sys.stdout.write(f"\r✔ {msg}        \n")
        sys.stdout.flush()

def _status_to_percent(status: Optional[str]) -> float:
    if not status:
        return 0.0
    s = str(status).lower()
    if s in ("pass", "ok", "found", "yes"):
        return 100.0
    if s in ("warning",):
        return 50.0
    return 0.0

def print_results_as_tables(results: Dict[str, Any], url: str):
    # Summary (score) if available
    score_info = results.get('_score_summary')
    if score_info:
        _print_table(
            "Summary",
            ["Score", "Max", "Percent", "Result"],
            [[score_info.get('score'), score_info.get('max'), f"{score_info.get('percent'):.1f}%", score_info.get('result')]],
        )

    # Per-section scores (percent)
    section_scores = results.get('_section_scores')
    if section_scores:
        rows = [[k.replace('_', ' ').title(), f"{v:.1f}%"] for k, v in section_scores.items()]
        _print_table("Section Scores", ["Item", "Percent"], rows)

    # Title & Meta
    tm = results.get('title_meta', {})
    _print_table(
        "Title & Meta",
        ["Item", "Status", "Chars", "Percent", "Content/Message"],
        [
            [
                "Title",
                tm.get('title', {}).get('status'),
                len(tm.get('title', {}).get('content') or "") if tm.get('title') else "",
                f"{_status_to_percent(tm.get('title', {}).get('status')):.0f}%",
                _truncate(tm.get('title', {}).get('content') or tm.get('title', {}).get('message', "")),
            ],
            [
                "Meta Description",
                tm.get('meta_description', {}).get('status'),
                len(tm.get('meta_description', {}).get('content') or "") if tm.get('meta_description') else "",
                f"{_status_to_percent(tm.get('meta_description', {}).get('status')):.0f}%",
                _truncate(tm.get('meta_description', {}).get('content') or tm.get('meta_description', {}).get('message', "")),
            ],
            [
                "Author",
                tm.get('author', {}).get('status'),
                len(tm.get('author', {}).get('content') or "") if tm.get('author') else "",
                f"{_status_to_percent(tm.get('author', {}).get('status')):.0f}%",
                _truncate(tm.get('author', {}).get('content') or tm.get('author', {}).get('message', "")),
            ],
        ],
    )

    # Headings
    hd = results.get('headings', {})
    _print_table(
        "Headings",
        ["Item", "Status", "Percent", "Details"],
        [
            [
                "H1",
                hd.get('h1_status'),
                f"{_status_to_percent(hd.get('h1_status')):.0f}%",
                _truncate(hd.get('h1_content', "")) or "",
            ],
            [
                "Hierarchy",
                hd.get('h_hierarchy'),
                f"{_status_to_percent(hd.get('h_hierarchy')):.0f}%",
                "Levels: " + ",".join(map(str, hd.get('h_tags_found', []))) if hd.get('h_tags_found') else "",
            ],
        ],
    )

    # Schema
    sc = results.get('schema', {})
    _print_table(
        "Schema (JSON-LD)",
        ["Status", "Percent", "Blocks", "Types"],
        [["pass" if sc.get('schema_found') else "fail", f"{(100.0 if sc.get('schema_found') else 0.0):.0f}%", len(sc.get('schemas', [])), ", ".join(sorted(set([str(t) for t in sc.get('types', [])]))[:6])]],
    )
    # FAQ
    faq = results.get('faq', {})
    _print_table(
        "FAQ",
        ["H3 Present", "Schema FAQPage", "Status", "Message"],
        [["yes" if faq.get('h3_count', 0) > 0 else "no", "yes" if sc.get('faqpage_found') else "no", faq.get('status', ''), _truncate(faq.get('message', ''))]],
    )

    # Mobile
    mb = results.get('mobile_responsiveness', {})
    _print_table(
        "Mobile Responsiveness",
        ["Status", "Percent", "Message"],
        [[mb.get('status'), f"{_status_to_percent(mb.get('status')):.0f}%", _truncate(mb.get('message', ""))]],
    )

    # Indexability
    ix = results.get('indexability', {})
    _print_table(
        "Indexability",
        ["Status", "Percent", "Meta", "Header", "Message"],
        [[ix.get('status', ''), f"{_status_to_percent(ix.get('status')):.0f}%", _truncate(ix.get('meta_robots', '') or ''), _truncate(ix.get('x_robots_tag', '') or ''), _truncate(ix.get('message', ''))]],
    )

    # Spelling removed

    # Robots & Sitemaps
    rs = results.get('robots_sitemaps', {})
    robots_present = rs.get('robots', {}).get('present')
    _print_table(
        "Robots",
        ["Present", "Percent", "URL"],
        [["yes" if robots_present else "no", f"{(100.0 if robots_present else 0.0):.0f}%", rs.get('robots', {}).get('url', "")]],
    )
    val = rs.get('sitemaps', {}).get('validated', [])
    _print_table(
        "Sitemaps",
        ["Status", "Percent", "URL", "Message"],
        (
            [[
                v.get('status'),
                f"{_status_to_percent(v.get('status')):.0f}%",
                v.get('sitemap_url'),
                v.get('message')
            ] for v in (val if val else [])]
            or [[rs.get('sitemaps', {}).get('status', 'fail'), f"{_status_to_percent(rs.get('sitemaps', {}).get('status')):.0f}%", '', 'No sitemaps validated']]
        ),
    )

    # Internal Links
    il = results.get('internal_links', {})
    checked = int(il.get('checked') or 0)
    broken_ct = len(il.get('broken') or [])
    il_percent = (100.0 * (1.0 - broken_ct / float(checked))) if checked > 0 else 0.0
    _print_table(
        "Internal Links",
        ["Total", "Checked", "Contextual", "Status", "Percent", "Message"],
        [[il.get('total_internal', 0), checked, il.get('contextual_links', 0), il.get('status', ''), f"{il_percent:.0f}%", _truncate(il.get('message', ''))]],
    )
    if il.get('broken'):
        _print_table(
            "Broken Internal Links (sample)",
            ["Link"],
            [[_truncate(link, 120)] for link in il.get('broken', [])[:20]],
        )

    # Images
    im = results.get('images', {})
    total_imgs = int(im.get('total_images') or 0)
    miss_ct = len(im.get('missing_alt') or [])
    img_percent = (100.0 * (1.0 - miss_ct / float(total_imgs))) if total_imgs > 0 else 0.0
    _print_table(
        "Images",
        ["Total", "Status", "Percent", "Message"],
        [[total_imgs, im.get('status', ''), f"{img_percent:.0f}%", _truncate(im.get('message', ''))]],
    )
    if im.get('missing_alt'):
        _print_table(
            "Images Missing Alt (sample)",
            ["Src"],
            [[_truncate(src, 120)] for src in im.get('missing_alt', [])[:20]],
        )

    # Canonical & Hreflang
    ch = results.get('canonical_hreflang', {})
    can = ch.get('canonical', {})
    _print_table(
        "Canonical",
        ["Status", "Percent", "Message", "URL", "Multiple"],
        [[can.get('status'), f"{_status_to_percent(can.get('status')):.0f}%", _truncate(can.get('message', '')), _truncate(can.get('url', '')), str(can.get('multiple', False))]],
    )
    hre = ch.get('hreflang', {})
    entries = hre.get('entries', [])
    _print_table(
        "Hreflang Entries",
        ["Lang", "URL"],
        [[e.get('lang'), _truncate(e.get('url', ''), 120)] for e in entries[:20]] or [["-", "-"]],
    )
    if hre.get('duplicates') or hre.get('invalid'):
        _print_table(
            "Hreflang Notes",
            ["Status", "Percent", "Duplicates", "Invalid"],
            [[hre.get('status'), f"{_status_to_percent(hre.get('status')):.0f}%", ",".join(hre.get('duplicates', [])), ",".join(hre.get('invalid', []))]],
        )

def _append_history(history_path: str, entries: List[Dict[str, Any]]):
    os.makedirs(os.path.dirname(history_path) or ".", exist_ok=True)
    with open(history_path, "a", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")

def _show_history(history_path: str, limit: int = 20):
    try:
        with open(history_path, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except FileNotFoundError:
        print(f"No history file found at {history_path}")
        return
    # Take last N
    items = []
    for line in lines[-limit:]:
        try:
            items.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    rows = []
    for it in items:
        summ = it.get("score_summary", {})
        rows.append([
            it.get("timestamp", ""),
            _truncate(it.get("url", ""), 80),
            f"{summ.get('percent', 0):.1f}%",
            summ.get("result", ""),
        ])
    _print_table("History (most recent)", ["Timestamp", "URL", "%", "Result"], rows)

    # Per-section table (if section scores available)
    sec_rows = []
    for it in items:
        secs = it.get("section_scores", {})
        if not secs:
            continue
        sec_rows.append([
            it.get("timestamp", ""),
            _truncate(it.get("url", ""), 60),
            f"{secs.get('title_meta', 0):.0f}%",
            f"{secs.get('headings', 0):.0f}%",
            f"{secs.get('schema', 0):.0f}%",
            f"{secs.get('mobile', 0):.0f}%",
            f"{secs.get('robots', 0):.0f}%",
            f"{secs.get('sitemaps', 0):.0f}%",
            f"{secs.get('internal_links', 0):.0f}%",
            f"{secs.get('images', 0):.0f}%",
            f"{secs.get('indexability', 0):.0f}%",
            f"{secs.get('canonical', 0):.0f}%",
            f"{secs.get('hreflang', 0):.0f}%",
        ])
    if sec_rows:
        _print_table(
            "History Section Scores",
            [
                "Timestamp",
                "URL",
                "Title/Meta",
                "Headings",
                "Schema",
                "Mobile",
                "Robots",
                "Sitemaps",
                "Internal",
                "Images",
                "Index",
                "Canonical",
                "Hreflang",
            ],
            sec_rows,
        )

if __name__ == "__main__":
    args = parse_args()
    # Apply proxy/TLS options early via environment so all modules honor them
    if getattr(args, 'proxy', None):
        os.environ["HTTP_PROXY"] = args.proxy
        os.environ["HTTPS_PROXY"] = args.proxy
    if getattr(args, 'ca_bundle', None):
        os.environ["REQUESTS_CA_BUNDLE"] = args.ca_bundle
    if getattr(args, 'insecure', False):
        os.environ["SEO_CHECKER_INSECURE"] = "1"
    # Show history and exit, if requested
    if args.show_history is not None:
        _show_history(args.history_file, args.show_history)
        sys.exit(0)
    # Build URL list (positional + optional file)
    urls: List[str] = list(args.urls)
    if args.url_file:
        try:
            with open(args.url_file, "r", encoding="utf-8") as f:
                urls.extend([line.strip() for line in f if line.strip() and not line.strip().startswith('#')])
        except Exception as e:
            print(f"Warning: could not read --url-file: {e}")

    # Deduplicate while preserving order
    seen = set()
    unique_urls: List[str] = []
    for u in urls:
        if u not in seen:
            unique_urls.append(u)
            seen.add(u)

    if not unique_urls:
        print("No URLs provided. Supply one or more URLs, use --url-file, or run with --show-history.")
        sys.exit(2)

    # Compute percentage score helper
    def _points_from_status(status: str) -> float:
        if not status:
            return 0.0
        s = str(status).lower()
        if s in ("pass", "ok", "found"):
            return 1.0
        if s in ("warning",):
            return 0.5
        return 0.0

    def compute_score(all_results: Dict[str, Any]) -> Dict[str, Any]:
        score = 0.0
        max_points = 0.0
        # Title & Meta (3)
        tm = all_results.get('title_meta', {})
        score += _points_from_status(tm.get('title', {}).get('status'))
        score += _points_from_status(tm.get('meta_description', {}).get('status'))
        score += _points_from_status(tm.get('author', {}).get('status'))
        max_points += 3
        # Headings (2)
        hd = all_results.get('headings', {})
        score += _points_from_status(hd.get('h1_status'))
        score += _points_from_status(hd.get('h_hierarchy'))
        max_points += 2
        # Schema (1)
        sc = all_results.get('schema', {})
        score += 1.0 if sc.get('schema_found') else 0.0
        max_points += 1
        # Mobile (1)
        mb = all_results.get('mobile_responsiveness', {})
        score += _points_from_status(mb.get('status'))
        max_points += 1
        # Indexability (1)
        ix_all = all_results.get('indexability', {})
        score += _points_from_status(ix_all.get('status'))
        max_points += 1
        # Robots (1)
        rs = all_results.get('robots_sitemaps', {})
        score += 1.0 if rs.get('robots', {}).get('present') else 0.0
        max_points += 1
        # Sitemaps (1)
        score += _points_from_status(rs.get('sitemaps', {}).get('status'))
        max_points += 1
        # Internal links (1)
        il = all_results.get('internal_links', {})
        score += _points_from_status(il.get('status'))
        max_points += 1
        # Images (1)
        im = all_results.get('images', {})
        score += _points_from_status(im.get('status'))
        max_points += 1
        # Canonical (1) + Hreflang (1)
        ch = all_results.get('canonical_hreflang', {})
        score += _points_from_status(ch.get('canonical', {}).get('status'))
        score += _points_from_status(ch.get('hreflang', {}).get('status'))
        max_points += 2
        percent = (score / max_points * 100.0) if max_points else 0.0
        qa_result = "PASS" if percent >= args.threshold else "FAIL"
        return {
            'score': round(score, 2),
            'max': int(max_points),
            'percent': round(percent, 1),
            'threshold': args.threshold,
            'result': qa_result,
        }

    # Run for each URL
    outputs: List[Dict[str, Any]] = []
    for site_url in unique_urls:
        res = run_all_checks(
            site_url,
            timeout=args.timeout,
            use_scraperapi=args.use_scraperapi,
            max_links=args.max_links,
            quiet=args.quiet,
            keyword=args.keyword,
        )
        res['_score_summary'] = compute_score(res)
        # Compute per-section percentages
        def _status_to_percent(s: Optional[str]) -> float:
            if not s:
                return 0.0
            s = str(s).lower()
            if s in ("pass", "ok", "found"):
                return 100.0
            if s in ("warning",):
                return 50.0
            return 0.0

        section_scores: Dict[str, float] = {}
        # Title & Meta: average of three (title, description, author)
        tm = res.get('title_meta', {})
        title_pct = _status_to_percent(tm.get('title', {}).get('status'))
        meta_pct = _status_to_percent(tm.get('meta_description', {}).get('status'))
        author_pct = _status_to_percent(tm.get('author', {}).get('status'))
        section_scores['title_meta'] = (title_pct + meta_pct + author_pct) / 3.0

        # Headings: H1 + hierarchy
        hd = res.get('headings', {})
        h1_pct = _status_to_percent(hd.get('h1_status'))
        hier_pct = _status_to_percent(hd.get('h_hierarchy'))
        section_scores['headings'] = (h1_pct + hier_pct) / 2.0

        # Schema
        sc = res.get('schema', {})
        section_scores['schema'] = 100.0 if sc.get('schema_found') else 0.0

        # Mobile
        mb = res.get('mobile_responsiveness', {})
        section_scores['mobile'] = _status_to_percent(mb.get('status'))

        # Robots
        rs = res.get('robots_sitemaps', {})
        section_scores['robots'] = 100.0 if rs.get('robots', {}).get('present') else 0.0
        # Sitemaps
        section_scores['sitemaps'] = _status_to_percent(rs.get('sitemaps', {}).get('status'))

        # Internal links: ratio based
        il = res.get('internal_links', {})
        checked = max(1, int(il.get('checked') or 0))
        broken_count = len(il.get('broken') or [])
        if (il.get('checked') or 0) > 0:
            section_scores['internal_links'] = max(0.0, 100.0 * (1.0 - broken_count / float(checked)))
        else:
            section_scores['internal_links'] = 0.0

        # Images: ratio based with poor-alt penalty
        im = res.get('images', {})
        total_imgs = int(im.get('total_images') or 0)
        missing_alt = len(im.get('missing_alt') or [])
        poor_alt = len(im.get('poor_alt') or [])
        base_pct = (100.0 * (1.0 - missing_alt / float(total_imgs))) if total_imgs > 0 else 0.0
        penalty = min(25.0, 100.0 * (poor_alt / float(total_imgs))) if total_imgs > 0 else 0.0
        section_scores['images'] = max(0.0, base_pct - penalty)

        # Indexability
        ix = res.get('indexability', {})
        section_scores['indexability'] = _status_to_percent(ix.get('status'))

        # Canonical & hreflang
        ch = res.get('canonical_hreflang', {})
        section_scores['canonical'] = _status_to_percent(ch.get('canonical', {}).get('status'))
        section_scores['hreflang'] = _status_to_percent(ch.get('hreflang', {}).get('status'))

        # Spelling removed

        res['_section_scores'] = section_scores
        outputs.append({'url': site_url, 'results': res})

        if not args.quiet and args.format in ("table", "both"):
            print_results_as_tables(res, site_url)

    # Console JSON (aggregated)
    if not args.quiet and args.format in ("json", "both"):
        print("\n--- SEO Check Results (JSON) ---")
        print(json.dumps(outputs, indent=2))

    # Output files
    if args.output_json:
        with open(args.output_json, "w", encoding="utf-8") as f:
            json.dump(outputs, f, ensure_ascii=False, indent=2)

    if args.output_csv:
        import csv
        with open(args.output_csv, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["url", "check", "status", "message"])
            for item in outputs:
                u = item['url']
                r = item['results']
                # Title & Meta
                tm = r.get('title_meta', {})
                writer.writerow([u, "title", tm.get('title', {}).get('status'), tm.get('title', {}).get('message', "")])
                writer.writerow([u, "meta_description", tm.get('meta_description', {}).get('status'), tm.get('meta_description', {}).get('message', "")])
                writer.writerow([u, "author", tm.get('author', {}).get('status'), tm.get('author', {}).get('content', "") or tm.get('author', {}).get('message', "")])
                # Headings
                hd = r.get('headings', {})
                writer.writerow([u, "h1", hd.get('h1_status'), hd.get('h_hierarchy')])
                # Schema
                sc = r.get('schema', {})
                writer.writerow([u, "schema", "pass" if sc.get('schema_found') else "fail", f"{len(sc.get('schemas', []))} blocks"]) 
                # Mobile
                mb = r.get('mobile_responsiveness', {})
                writer.writerow([u, "mobile", mb.get('status'), mb.get('message', "")])
                # Robots/Sitemaps
                rs = r.get('robots_sitemaps', {})
                writer.writerow([u, "robots", "pass" if rs.get('robots',{}).get('present') else "fail", rs.get('robots',{}).get('url') or ""])
                writer.writerow([u, "sitemaps", rs.get('sitemaps',{}).get('status'), ",".join([v.get('sitemap_url','') for v in rs.get('sitemaps',{}).get('validated',[])])])
                # Internal links
                il = r.get('internal_links', {})
                writer.writerow([u, "internal_links", il.get('status'), il.get('message')])
                # Images
                im = r.get('images', {})
                writer.writerow([u, "images", im.get('status'), im.get('message')])
                # Indexability
                ix = r.get('indexability', {})
                writer.writerow([u, "indexability", ix.get('status'), ix.get('message')])
                # FAQ
                fq = r.get('faq', {})
                writer.writerow([u, "faq", fq.get('status'), fq.get('message')])
                # Spelling removed
                # Canonical/hreflang
                ch = r.get('canonical_hreflang', {})
                writer.writerow([u, "canonical", ch.get('canonical',{}).get('status'), ch.get('canonical',{}).get('message')])
                writer.writerow([u, "hreflang", ch.get('hreflang',{}).get('status'), ch.get('hreflang',{}).get('message')])

    # Append to history
    if not args.no_history:
        history_entries: List[Dict[str, Any]] = []
        # Use timezone-aware UTC timestamp
        now = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
        for item in outputs:
            r = item['results']
            summ = r.get('_score_summary', {})
            history_entries.append({
                "timestamp": now,
                "url": item['url'],
                "score_summary": summ,
                "section_scores": r.get('_section_scores', {}),
            })
        _append_history(args.history_file, history_entries)

    # Determine exit code across multiple URLs
    exit_code = 0
    any_fetch_error = any(item['results'].get('error') for item in outputs)
    any_below_threshold = any(item['results'].get('_score_summary', {}).get('percent', 0.0) < args.threshold for item in outputs)
    if any_fetch_error:
        exit_code = 2
    elif any_below_threshold:
        exit_code = 1

    sys.exit(exit_code)
