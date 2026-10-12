#!/usr/bin/env python3
"""
Ontario soccer club registration-platform scanner.

What it does
  1. Reads sources.csv (district and league websites).
  2. Visits each source's homepage plus pages that look like club directories,
     and collects links to member club websites.
  3. Visits each club website in a headless browser, finds registration links
     (following up to 3 "Register / Join / Programs" pages on the club's own site),
     and tags the registration platform from the link domains.
  4. Writes:
       clubs_found.csv              every club link collected, and where it came from
       ontario_clubs_lead_list.csv  one row per club: platform, evidence, status
       summary.csv                  counts by platform, by status, and by source

Usage
  python scrape_clubs.py                      # full run
  python scrape_clubs.py --limit-per-source 3 # quick test: 3 clubs per source
  python scrape_clubs.py --collect-only       # just build clubs_found.csv
  python scrape_clubs.py --seed e2e_league_clubs_lead_list.csv  # add known club URLs

Notes
  - Respects robots.txt and pauses between requests.
  - Collects club-level information only (no personal names or emails).
  - Classification is evidence-based: anything it can't confirm is "Unknown".
"""

import argparse
import asyncio
import csv
import random
import re
import sys
import time
from collections import Counter, defaultdict
from urllib import robotparser
from urllib.parse import urljoin, urlparse

USER_AGENT = "OntarioClubPlatformSurvey/1.0 (club registration platform research)"

# Domain fragment -> platform name. Order matters only for display.
VENDORS = [
    ("powerupsports.com", "PowerUp"),
    ("sportngin.com", "SportsEngine"),
    ("sportsengine.com", "SportsEngine"),
    ("demosphere-secure.com", "Demosphere"),
    ("demosphere.com", "Demosphere"),
    ("playmetrics.com", "PlayMetrics"),
    ("teamsnap.com", "TeamSnap"),
    ("e2esoccer.com", "E2E Soccer"),
    ("rampregistrations.com", "RAMP"),
    ("rampinteractive.com", "RAMP"),
    ("goalline.ca", "GOALLINE"),
    ("leagueapps.com", "LeagueApps"),
    ("teamlinkt.com", "TeamLinkt"),
    ("ivrnet.com", "Ivrnet"),
    ("spordle.com", "Spordle"),
    ("uplifterinc.com", "Uplifter"),
    ("amilia.com", "Amilia"),
    ("jerseywatch.com", "Jersey Watch"),
    ("bluesombrero.com", "Blue Sombrero"),
    ("gotsport.com", "GotSport"),
    ("mbsportsweb.ca", "MBSportsWeb"),
    ("sportsheadz.com", "MBSportsWeb"),
    ("zeffy.com", "Online form/payments"),
    ("jotform.com", "Online form/payments"),
    ("cognitoforms.com", "Online form/payments"),
    ("forms.gle", "Online form/payments"),
    ("docs.google.com/forms", "Online form/payments"),
    ("wufoo.com", "Online form/payments"),
]
REAL_PLATFORMS = {name for _, name in VENDORS} - {"Online form/payments"}

# Words that mark a link as registration-related.
REG_WORDS = re.compile(
    r"regist|sign[\s-]?up|join|enrol|inscri|member\s*zone|log\s*in|login|"
    r"membership|tryout|try-out|player\s*portal",
    re.I,
)
# Words that make an internal page worth following on a club site.
FOLLOW_WORDS = re.compile(r"regist|sign[\s-]?up|join|enrol|inscri|programs?|house\s*league", re.I)
# Words that make a page on a district/league site look like a club directory.
DIRECTORY_WORDS = re.compile(
    r"club|member|director|associat|find|locat|our\s*teams|organi[sz]ations", re.I
)
CLUBLIKE = re.compile(
    r"soccer|socc|futbol|football|fc\b|sc\b|united|athletic|club|academy|youth|minor|"
    r"kicks|strikers|rangers|city|azzurri|hotspurs|wolves",
    re.I,
)

# Hosts that are never club websites.
EXCLUDE_HOSTS = (
    "facebook.com", "instagram.com", "twitter.com", "x.com", "youtube.com", "youtu.be",
    "linkedin.com", "tiktok.com", "google.com", "goo.gl", "apple.com", "wa.me",
    "ontariosoccer.net", "canadasoccer.com", "respectgroupinc.com", "kidsportcanada.ca",
    "jumpstart.canadiantire.ca", "canadiantire.ca", "wordpress.org", "wordpress.com",
    "wix.com", "squarespace.com", "godaddy.com", "mailchimp.com", "list-manage.com",
    "eventbrite.com", "eventbrite.ca", "gofundme.com", "paypal.com", "bit.ly",
    "e2esoccer.com/login", "gstatic.com", "googleapis.com", "cloudflare.com",
)


def host_of(url):
    try:
        h = urlparse(url).netloc.lower()
    except ValueError:
        return ""
    return h[4:] if h.startswith("www.") else h


def club_key(url):
    """Dedupe key: vendor-hosted clubs keep their subdomain; others use the host."""
    return host_of(url).split(":")[0]


def vendor_for(url):
    u = url.lower()
    h = host_of(url)
    # Ignore vendor CDNs and static asset hosts (e.g. cdn2.sportngin.com PDFs).
    if re.match(r"^(cdn\d*|static|assets|img|images)\.", h):
        return None
    for frag, name in VENDORS:
        if frag in u:
            return name
    return None


def is_excluded(url):
    u = url.lower()
    if not u.startswith("http"):
        return True
    return any(x in u for x in EXCLUDE_HOSTS)


# ---------------------------------------------------------------- robots.txt
_robots_cache = {}


def _robots_allowed_sync(url):
    parsed = urlparse(url)
    base = f"{parsed.scheme}://{parsed.netloc}"
    if base not in _robots_cache:
        rp = robotparser.RobotFileParser()
        rp.set_url(base + "/robots.txt")
        try:
            rp.read()
        except Exception:
            rp = None  # unreadable robots.txt: treat as allowed
        _robots_cache[base] = rp
    rp = _robots_cache[base]
    return True if rp is None else rp.can_fetch(USER_AGENT, url)


async def robots_allowed(url):
    return await asyncio.to_thread(_robots_allowed_sync, url)


# ---------------------------------------------------------------- page reading
LINKS_JS = """
() => {
  const out = [];
  for (const a of document.querySelectorAll('a[href]')) {
    const img = a.querySelector('img');
    const text = (a.innerText || (img && (img.alt || img.title)) ||
                  a.getAttribute('aria-label') || a.title || '').trim();
    out.push([a.href, text.slice(0, 120)]);
  }
  for (const f of document.querySelectorAll('iframe[src]')) out.push([f.src, 'iframe']);
  for (const f of document.querySelectorAll('form[action]')) out.push([f.action, 'form']);
  return out;
}
"""


async def read_links(context, url, delay):
    """Return (final_url, [(href, text), ...]) or raise."""
    if not await robots_allowed(url):
        raise PermissionError("blocked by robots.txt")
    page = await context.new_page()
    try:
        resp = await page.goto(url, wait_until="domcontentloaded", timeout=25000)
        if resp is not None and resp.status >= 400:
            raise RuntimeError(f"HTTP {resp.status}")
        await page.wait_for_timeout(1500)  # let JavaScript-built menus render
        links = await page.evaluate(LINKS_JS)
        return page.url, links
    finally:
        await page.close()
        await asyncio.sleep(delay)


# ---------------------------------------------------------------- stage 1: collect clubs
async def collect_from_source(context, src, args):
    found = {}
    pages_to_read = [src["url"]]
    seen_pages = set()
    src_host = host_of(src["url"])
    while pages_to_read and len(seen_pages) < args.max_directory_pages:
        url = pages_to_read.pop(0)
        if url in seen_pages:
            continue
        seen_pages.add(url)
        try:
            _, links = await read_links(context, url, args.delay)
        except Exception as e:
            print(f"  ! {src['name']}: could not read {url} ({e})", file=sys.stderr)
            continue
        for href, text in links:
            href = href.split("#")[0]
            h = host_of(href)
            if not h:
                continue
            if h == src_host:
                # Internal page: follow it if it looks like a club directory.
                if url == src["url"] and DIRECTORY_WORDS.search(href + " " + text):
                    if href not in seen_pages and href not in pages_to_read:
                        pages_to_read.append(href)
                continue
            if is_excluded(href):
                continue
            key = club_key(href)
            if key not in found:
                found[key] = {
                    "club_name": text or key,
                    "club_url": href,
                    "clublike": "yes" if CLUBLIKE.search(text + " " + h) else "check",
                    "found_on": url,
                }
    return found


async def collect_all(context, sources, args):
    clubs = {}
    for src in sources:
        print(f"Collecting clubs from {src['name']} ...")
        found = await collect_from_source(context, src, args)
        print(f"  {len(found)} external links that may be clubs")
        items = list(found.items())
        if args.limit_per_source:
            # Sample mode: only club-like links, picked at random with a fixed seed.
            items = [(k, v) for k, v in items if v["clublike"] == "yes"]
            rng = random.Random(42)
            items = rng.sample(items, min(args.limit_per_source, len(items)))
        for key, info in items:
            entry = clubs.setdefault(key, {**info, "sources": set()})
            entry["sources"].add(f"{src['type']}: {src['name']}")
            if entry["club_name"] == key and info["club_name"] != key:
                entry["club_name"] = info["club_name"]
    return clubs


def load_seed(path, clubs):
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            url = row.get("club_url") or row.get("Website") or row.get("website")
            name = row.get("club_name") or row.get("Club") or ""
            if not url or not url.startswith("http"):
                continue
            key = club_key(url)
            entry = clubs.setdefault(key, {
                "club_name": name or key, "club_url": url, "clublike": "yes",
                "found_on": path, "sources": set(),
            })
            entry["sources"].add(f"seed: {path}")


# ---------------------------------------------------------------- stage 2: check clubs
def classify(hits):
    """hits: list of (platform, url, strong: bool). Returns (platforms, evidence, status)."""
    strong = [h for h in hits if h[2]]
    use = strong or hits
    platforms = sorted({p for p, _, _ in use})
    evidence = "; ".join(sorted({u for _, u, _ in use}))[:500]
    real = [p for p in platforms if p in REAL_PLATFORMS]
    if not platforms:
        status = "Unknown"
    elif real == ["PowerUp"]:
        status = "Enrolled"
    elif "PowerUp" in real:
        status = "Partial"
    elif real == ["E2E Soccer"]:
        status = "Non-enrolled (E2E/Beacon customer)"
    elif real:
        status = "Non-enrolled"
    else:
        status = "Non-enrolled (forms/manual)"
    if use and not strong:
        status += " (weak evidence)"
    return platforms, evidence, status


async def check_club(context, club, args, sem):
    async with sem:
        hits, pages_read, error = [], 0, ""
        start = club["club_url"]
        # The club site itself may live on a vendor (e.g. club.sportngin.com).
        v = vendor_for(start)
        if v:
            hits.append((v, start, True))
        try:
            final_url, links = await read_links(context, start, args.delay)
            pages_read += 1
            club_host = host_of(final_url)
            v = vendor_for(final_url)
            if v:
                hits.append((v, final_url, True))
            follow = []
            for href, text in links:
                v = vendor_for(href)
                if v:
                    strong = bool(REG_WORDS.search(text + " " + href)) or text in ("iframe", "form")
                    hits.append((v, href, strong))
                elif host_of(href) == club_host and FOLLOW_WORDS.search(href + " " + text):
                    href = href.split("#")[0]
                    if href not in follow and href != final_url:
                        follow.append(href)
            for href in follow[:3]:
                try:
                    _, sub = await read_links(context, href, args.delay)
                    pages_read += 1
                except Exception:
                    continue
                for h2, t2 in sub:
                    v = vendor_for(h2)
                    if v:
                        strong = bool(REG_WORDS.search(t2 + " " + h2)) or t2 in ("iframe", "form")
                        hits.append((v, h2, strong))
        except Exception as e:
            error = str(e)[:200]
        platforms, evidence, status = classify(hits)
        if error and status == "Unknown":
            status = "Unknown (site error/blocked)"
        print(f"  {status:<40} {club['club_name'][:40]}")
        return {
            "club_name": club["club_name"],
            "club_url": club["club_url"],
            "sources": " | ".join(sorted(club["sources"])),
            "looks_like_club": club["clublike"],
            "platform": " + ".join(platforms) if platforms else "Unknown",
            "status": status,
            "evidence": evidence,
            "pages_checked": pages_read,
            "error": error,
            "checked_at": time.strftime("%Y-%m-%d"),
        }


# ---------------------------------------------------------------- outputs
def write_csv(path, rows, fields):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)


def write_summary(path, results):
    rows = []
    likely = [r for r in results if r["looks_like_club"] == "yes"]
    for label, data in (("all links", results), ("club-like only", likely)):
        for k, v in Counter(r["status"] for r in data).most_common():
            rows.append({"scope": label, "group": "status", "value": k, "count": v})
        for k, v in Counter(r["platform"] for r in data).most_common():
            rows.append({"scope": label, "group": "platform", "value": k, "count": v})
    by_src = defaultdict(Counter)
    for r in likely:
        for s in r["sources"].split(" | "):
            by_src[s][r["status"]] += 1
    for s, c in sorted(by_src.items()):
        for k, v in c.most_common():
            rows.append({"scope": "club-like only", "group": f"source: {s}", "value": k, "count": v})
    write_csv(path, rows, ["scope", "group", "value", "count"])


# ---------------------------------------------------------------- main
def load_sources(path):
    with open(path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    return [r for r in rows if r.get("enabled", "yes").strip().lower() in ("yes", "y", "1", "true")]


async def main_async(args):
    from playwright.async_api import async_playwright  # imported here so helpers are testable

    sources = load_sources(args.sources)
    print(f"{len(sources)} enabled sources")
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(user_agent=USER_AGENT, accept_downloads=False)
        clubs = await collect_all(context, sources, args)
        if args.seed:
            load_seed(args.seed, clubs)

        found_rows = [
            {"club_name": c["club_name"], "club_url": c["club_url"],
             "looks_like_club": c["clublike"], "sources": " | ".join(sorted(c["sources"])),
             "found_on": c["found_on"]}
            for c in clubs.values()
        ]
        write_csv("clubs_found.csv", found_rows,
                  ["club_name", "club_url", "looks_like_club", "sources", "found_on"])
        print(f"\n{len(clubs)} unique club links written to clubs_found.csv")
        if args.collect_only:
            await browser.close()
            return

        print("\nChecking club websites ...")
        sem = asyncio.Semaphore(args.concurrency)
        results = await asyncio.gather(*(check_club(context, c, args, sem) for c in clubs.values()))
        await browser.close()

    fields = ["club_name", "club_url", "sources", "looks_like_club", "platform", "status",
              "evidence", "pages_checked", "error", "checked_at"]
    results.sort(key=lambda r: (r["looks_like_club"] != "yes", r["status"], r["club_name"].lower()))
    write_csv(args.out, results, fields)
    write_summary(args.summary, results)
    print(f"\nWrote {args.out} and {args.summary}")
    likely = [r for r in results if r["looks_like_club"] == "yes"]
    print("\nClub-like links by status:")
    for k, v in Counter(r["status"] for r in likely).most_common():
        print(f"  {v:>4}  {k}")


def parse_args():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sources", default="sources.csv")
    ap.add_argument("--out", default="ontario_clubs_lead_list.csv")
    ap.add_argument("--summary", default="summary.csv")
    ap.add_argument("--seed", help="optional CSV of known clubs (columns Club/Website or club_name/club_url)")
    ap.add_argument("--limit-per-source", type=int, default=0, help="test mode: clubs per source (0 = all)")
    ap.add_argument("--max-directory-pages", type=int, default=8, help="pages to read per district/league")
    ap.add_argument("--concurrency", type=int, default=4)
    ap.add_argument("--delay", type=float, default=1.0, help="seconds to pause after each page")
    ap.add_argument("--collect-only", action="store_true")
    return ap.parse_args()


if __name__ == "__main__":
    asyncio.run(main_async(parse_args()))
