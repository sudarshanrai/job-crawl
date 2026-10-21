"""
Job-alert scraper: checks each configured source for job listings that match
your keywords and emails you (via Brevo) whenever a genuinely new listing
shows up.

What changed in this version (all aimed at NOT MISSING new postings):

1. Same-title jobs. A job's identity is now (title + stable id/link), not the
   title alone. ATS feeds use the job id; browser scans use the job's link.
   A new posting with the same title as an old one is therefore reported.
   Old cache entries (title-only) keep working and are upgraded in place.

2. Re-listed jobs. If a job disappeared for at least `reappear_after_days`
   (default 3) and then comes back, it is reported again. Absence is measured
   against the last *successful* scan, so downtime of this script or a flaky
   scan does not cause false alerts.

3. Email failures no longer swallow jobs. If the email can't be sent, the
   affected sources are rolled back in the cache so the jobs are reported on
   the next run (and the script exits with code 1).

4. Cache safety. Atomic writes + a .bak copy; a corrupt cache falls back to
   the backup instead of crashing or silently resetting.

5. One crashing source can no longer abort the whole run.

6. Silent breakage is surfaced: a source that fails (or returns a blank page)
   `source_failure_warning_after` times in a row (default 3) is mentioned in
   an email, so you don't think "no mail" means "no jobs".

7. Keyword matching: lookarounds instead of \\b (so "c++", ".net", "c#" work),
   invisible characters (soft hyphens, zero-width spaces, nbsp) are removed,
   and a keyword written as "*word" matches as a substring (useful for German
   compounds, e.g. "*entwickler" matches "Softwareentwickler").

8. Brevo request has a timeout and a small retry.

Optional config.json keys:
    "keywords": [...], "urls": [...], "job_retention_days": 45,
    "max_concurrency": 5, "persist_consent_state": true,
    "debug_screenshots": true,
    "reappear_after_days": 3,            # 0 disables re-list alerts
    "source_failure_warning_after": 3,   # 0 disables failure warnings
    "use_job_links": true                # false = identify browser jobs by title only

`urls` entries can be a string (browser scan) or an object:
    {"type": "greenhouse", "slug": "acme"}
    {"type": "lever",      "slug": "acme"}
    {"type": "recruitee",  "slug": "acme"}
    {"type": "personio",   "slug": "acme"}
    optionally with "url": "<link shown in the email>"
"""

import os
import re
import sys
import copy
import json
import html
import time
import shutil
import asyncio
import unicodedata
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode
import urllib.request
import urllib.error
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError, Error as PlaywrightError

CONFIG_FILE = "config.json"
CACHE_FILE = "seen_jobs.json"
CONSENT_STATE_FILE = "consent_state.json"
DEBUG_DIR = "debug"
CACHE_VERSION = 2
SELECTOR = 'a, h1, h2, h3, h4, li, tr, [class*="job"], [class*="position"]'
DEFAULT_CONCURRENCY = 5
DEFAULT_REAPPEAR_DAYS = 3
DEFAULT_FAIL_WARN = 3
# Blocking these speeds up load / time-to-idle without touching what
# innerText extracts. Stylesheets are excluded on purpose (hidden-element
# duplicates would otherwise show up in innerText).
BLOCKED_RESOURCE_TYPES = {"image", "media", "font"}
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"


# ---------------------------------------------------------------------------
# JSON helpers (atomic save, backup recovery)
# ---------------------------------------------------------------------------

def load_json(file, default):
    for candidate in (file, file + ".bak"):
        if os.path.exists(candidate):
            try:
                with open(candidate, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if candidate != file:
                    print(f"⚠️  {file} was unreadable; recovered from {candidate}.")
                return data
            except (json.JSONDecodeError, UnicodeDecodeError) as e:
                print(f"⚠️  Could not parse {candidate}: {e}")
    if os.path.exists(file):
        raise SystemExit(
            f"❌ {file} is corrupt and no valid backup exists. Fix it by hand, or delete it "
            f"(deleting the cache makes every current job count as new)."
        )
    return default


def save_json_atomic(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=4, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    if os.path.exists(path):
        shutil.copy2(path, path + ".bak")
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# Consent / popup handling
# ---------------------------------------------------------------------------

CONSENT_BUTTON_TEXTS = [
    "accept all", "accept cookies", "accept", "agree", "i agree", "allow all",
    "unblock content", "accept required service and unblock content",
    "alle akzeptieren", "akzeptieren", "zustimmen", "einverstanden",
    "auswahl bestätigen",
]

# Known consent-platform "accept" buttons. IDs/classes are far more reliable
# than text and work in any language.
CMP_ACCEPT_SELECTORS = ", ".join([
    "#onetrust-accept-btn-handler",                                   # OneTrust
    "#CybotCookiebotDialogBodyLevelButtonLevelOptinAllowAll",         # Cookiebot
    "#CybotCookiebotDialogBodyButtonAccept",
    "[data-testid='uc-accept-all-button']",                           # Usercentrics
    "button[data-action-type='accept']",
    ".cmpboxbtnyes",                                                  # Consentmanager
    "a.cmptxt_btn_yes",
    ".borlabs-cookie-btn-accept-all, ._brlbs-btn-accept-all",         # Borlabs
    "#cookie-accept, #accept-cookies, .cookie-accept",
    ".cc-allow, .cc-dismiss",                                         # Cookie Consent
    "#didomi-notice-agree-button",                                    # Didomi
    "button.sp_choice_type_11",                                       # Sourcepoint
])

LOAD_MORE_TEXTS = [
    "load more", "show more", "more jobs", "view more",
    "weitere anzeigen", "mehr anzeigen", "mehr laden", "weitere jobs",
]

_CONSENT_REGEX = re.compile("(" + "|".join(re.escape(t) for t in CONSENT_BUTTON_TEXTS) + ")", re.IGNORECASE)
_LOAD_MORE_REGEX = re.compile("(" + "|".join(re.escape(t) for t in LOAD_MORE_TEXTS) + ")", re.IGNORECASE)

# Last-resort: remove leftover fixed/sticky popups and unlock scrolling.
# Guards avoid deleting a big app shell that merely mentions "privacy".
REMOVE_OVERLAYS_JS = r"""
() => {
  const words = /cookie|consent|privacy|gdpr|datenschutz|newsletter/i;
  document.querySelectorAll('div,section,aside,dialog').forEach(el => {
    try {
      const s = getComputedStyle(el);
      if (s.position !== 'fixed' && s.position !== 'sticky') return;
      const text = (el.innerText || '');
      if (text.length > 3000 || el.querySelectorAll('a').length > 40) return;
      const probe = (el.id || '') + ' ' + String(el.className || '') + ' ' + text.slice(0, 300);
      if (words.test(probe)) el.remove();
    } catch (e) {}
  });
  document.documentElement.style.overflow = 'auto';
  document.body.style.overflow = 'auto';
}
"""


def _consent_locator(scope):
    """Known CMP buttons OR any real button/link whose name looks like 'accept'."""
    return (
        scope.locator(CMP_ACCEPT_SELECTORS)
        .or_(scope.get_by_role("button", name=_CONSENT_REGEX))
        .or_(scope.get_by_role("link", name=_CONSENT_REGEX))
    )


async def install_consent_handler(page):
    """
    Registers a handler that clicks a consent button whenever one appears and
    blocks an action (click, fill, ...). Covers banners injected late. Needs
    Playwright >= 1.42; silently skipped on older versions.
    """
    if not hasattr(page, "add_locator_handler"):
        return
    consent = _consent_locator(page).first

    async def accept(locator):
        try:
            await locator.click(timeout=2000)
        except Exception:
            pass

    try:
        await page.add_locator_handler(consent, accept, times=3)
    except Exception:
        pass


async def dismiss_consent_walls(page, max_clicks=3):
    """
    Explicit click-through pass (handlers only fire before clicks, not before
    scrolls/evaluates). Loops so stacked popups are all closed, across the
    main page and every iframe. Never raises.
    """
    for _ in range(max_clicks):
        clicked = False
        for frame in page.frames:
            try:
                locator = _consent_locator(frame)
                if await locator.count() > 0:
                    await locator.first.click(timeout=1500)
                    clicked = True
                    break
            except Exception:
                continue
        if not clicked:
            return
        await page.wait_for_timeout(500)  # let the next popup (if any) appear


async def remove_overlays(page):
    try:
        await page.evaluate(REMOVE_OVERLAYS_JS)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Text cleanup / matching
# ---------------------------------------------------------------------------

COMPOUND_ALIASES = {
    "fullstack": ["full stack", "full-stack"],
    "frontend": ["front end", "front-end"],
    "backend": ["back end", "back-end"],
}

NOISE_SUFFIXES = sorted([
    "read more", "learn more", "view job", "view details", "view position",
    "see details", "see job", "apply now", "apply here", "apply", "details",
    "launch", "open position", "job details",
    "weiterlesen", "mehr erfahren", "mehr dazu", "mehr lesen",
    "jetzt bewerben", "details ansehen", "stelle ansehen", "zur stelle",
    "stellenanzeige ansehen", "anzeigen", "mehr",
], key=len, reverse=True)

_NOISE_PATTERN = re.compile(
    r"\s*(?:" + "|".join(re.escape(n) for n in NOISE_SUFFIXES) + r")\s*[»›→\-]*\s*$",
    re.IGNORECASE,
)

_INVISIBLE_CHARS = re.compile("[\u00ad\u200b\u200c\u200d\u2060\ufeff]")


def clean_text(text: str) -> str:
    """Unicode-normalize (nbsp -> space), drop soft hyphens / zero-width chars, collapse whitespace."""
    text = unicodedata.normalize("NFKC", text or "")
    text = _INVISIBLE_CHARS.sub("", text)
    return re.sub(r"\s+", " ", text).strip()


def strip_trailing_noise(text: str) -> str:
    previous = None
    while previous != text:
        previous = text
        text = _NOISE_PATTERN.sub("", text).strip()
    return text


def normalize_key(text: str) -> str:
    """Canonical title form used for cache identity (.lower(), not casefold, to match old keys)."""
    return clean_text(text).lower()


_TRACKING_PARAMS = re.compile(
    r"^(utm_.*|gclid|fbclid|msclkid|mc_cid|mc_eid|sessionid|jsessionid|phpsessid|sid|_ga|ref|trk)$",
    re.IGNORECASE,
)


def normalize_url(href):
    """Stable form of a job link: no fragment, no tracking/session params, no trailing slash."""
    if not href:
        return None
    try:
        p = urlparse(href.strip())
    except ValueError:
        return None
    if p.scheme not in ("http", "https") or not p.netloc:
        return None
    query = urlencode([(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True)
                       if not _TRACKING_PARAMS.match(k)])
    path = re.sub(r";jsessionid=[^/?#]*", "", p.path, flags=re.IGNORECASE).rstrip("/") or "/"
    return urlunparse((p.scheme.lower(), p.netloc.lower(), path, "", query, ""))


def build_keyword_patterns(keywords: list):
    """
    Whole-word match by default (lookarounds, so "c++" / ".net" / "c#" work).
    A keyword written as "*entwickler" matches as a plain substring instead.
    """
    patterns = []
    for kw in keywords:
        substring = kw.startswith("*")
        base = kw.lstrip("*").strip()
        if not base:
            continue
        variants = {base}
        variants.update(COMPOUND_ALIASES.get(base.lower(), []))
        for variant in variants:
            body = re.escape(clean_text(variant))
            regex = body if substring else r"(?<!\w)" + body + r"(?!\w)"
            patterns.append(re.compile(regex, re.IGNORECASE))
    return patterns


# ---------------------------------------------------------------------------
# Job items: {"title", "uid" (stable id/link or None), "link", "extra"}
# ---------------------------------------------------------------------------

def make_item(title, uid=None, link=None, extra=None):
    return {"title": clean_text(title), "uid": uid, "link": link, "extra": extra}


def match_jobs_by_keyword(items: list, patterns: list) -> list:
    if not patterns:
        return []
    return [it for it in items if any(p.search(it["title"]) for p in patterns)]


def dedupe_items(items: list) -> list:
    """
    - A link shared by several different matched titles is not a usable identity
      (e.g. every job pointing to one generic page) -> fall back to title only.
    - Identical (title, uid) pairs collapse. Same title with DIFFERENT uids stay separate.
    - A title-only item is dropped if the same title already appears with a uid.
    """
    titles_per_uid = {}
    for it in items:
        if it["uid"]:
            titles_per_uid.setdefault(it["uid"], set()).add(normalize_key(it["title"]))
    for it in items:
        if it["uid"] and len(titles_per_uid[it["uid"]]) > 1:
            it["uid"] = None

    uid_titles = {normalize_key(it["title"]) for it in items if it["uid"]}
    seen, out = set(), []
    for it in items:
        tk = normalize_key(it["title"])
        if not it["uid"] and tk in uid_titles:
            continue
        ident = (tk, it["uid"])
        if ident in seen:
            continue
        seen.add(ident)
        out.append(it)
    return sorted(out, key=lambda i: (normalize_key(i["title"]), i["uid"] or ""))


# ---------------------------------------------------------------------------
# Page automation
# ---------------------------------------------------------------------------

async def autoscroll_and_expand(page, max_rounds=8):
    """Scrolls and clicks "load more" buttons until the page stops growing."""
    last_height = 0
    for _ in range(max_rounds):
        await page.mouse.wheel(0, 3000)
        await page.wait_for_timeout(400)
        try:
            btn = page.get_by_text(_LOAD_MORE_REGEX)
            if await btn.count() > 0:
                await btn.first.click(timeout=800)
                await page.wait_for_timeout(500)
        except Exception:
            pass
        try:
            height = await page.evaluate("document.body.scrollHeight")
        except Exception:
            break
        if height == last_height:
            break
        last_height = height


EXTRACT_JS = r"""
(sel) => {
  const nodes = Array.from(document.querySelectorAll(sel));
  const nodeSet = new Set(nodes);
  const hasMatchedDescendant = new Set();
  for (const el of nodes) {
    let p = el.parentElement;
    while (p) {
      if (nodeSet.has(p)) hasMatchedDescendant.add(p);
      p = p.parentElement;
    }
  }
  const realHref = a => {
    const h = (a.getAttribute('href') || '').trim();
    if (!h || h.startsWith('#') || /^(javascript|mailto|tel):/i.test(h)) return null;
    return a.href;
  };
  // Link of the element itself, or of its card if the card holds exactly one distinct link.
  const hrefFor = el => {
    const own = el.closest('a');
    if (own) return realHref(own);
    let p = el.parentElement;
    for (let i = 0; i < 3 && p; i++, p = p.parentElement) {
      const hrefs = new Set(Array.from(p.querySelectorAll('a')).map(realHref).filter(Boolean));
      if (hrefs.size === 1) return Array.from(hrefs)[0];
      if (hrefs.size > 1) return null;
    }
    return null;
  };
  return nodes
    .filter(el => !hasMatchedDescendant.has(el))
    .map(el => ({
      text: (el.innerText || el.textContent || "").replace(/\s+/g, " ").trim(),
      href: hrefFor(el),
    }))
    .filter(r => r.text.length > 5 && r.text.length < 200);
}
"""


async def extract_page_items(page, use_links=True) -> list:
    """Likely job elements (page + iframes), leaf matches only, with their link when known."""
    items, seen = [], set()
    for frame in page.frames:
        try:
            rows = await frame.evaluate(EXTRACT_JS, SELECTOR)
        except Exception:
            continue
        for row in rows:
            title = strip_trailing_noise(clean_text(row["text"]))
            if len(title) <= 3:
                continue
            link = row.get("href") if use_links else None
            norm = normalize_url(link)
            uid = f"url:{norm}" if norm else None
            ident = (normalize_key(title), uid)
            if ident in seen:
                continue
            seen.add(ident)
            items.append(make_item(title, uid, link))
    return items


async def goto_with_retry(page, url, attempts=2, timeout_ms=45000, settle_timeout_ms=8000):
    last_err = None
    for attempt in range(1, attempts + 1):
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            try:
                await page.wait_for_load_state("networkidle", timeout=settle_timeout_ms)
            except PlaywrightTimeoutError:
                pass
            return
        except PlaywrightTimeoutError as e:
            last_err = e
            if attempt < attempts:
                print(f"    ⏳ Timed out loading (attempt {attempt}/{attempts}), retrying...")
    raise last_err


# ---------------------------------------------------------------------------
# ATS feeds (no browser needed)
# ---------------------------------------------------------------------------

def _http_get(url: str, timeout=20) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def _uid(prefix, value):
    return f"{prefix}:{value}" if value not in (None, "") else None


def _items_greenhouse(slug):
    data = json.loads(_http_get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs"))
    return [make_item(j["title"], _uid("greenhouse", j.get("id")), j.get("absolute_url"),
                      (j.get("location") or {}).get("name"))
            for j in data.get("jobs", []) if j.get("title")]


def _items_lever(slug):
    data = json.loads(_http_get(f"https://api.lever.co/v0/postings/{slug}?mode=json"))
    return [make_item(j["text"], _uid("lever", j.get("id")), j.get("hostedUrl"),
                      (j.get("categories") or {}).get("location"))
            for j in data if j.get("text")]


def _items_recruitee(slug):
    data = json.loads(_http_get(f"https://{slug}.recruitee.com/api/offers/"))
    return [make_item(o["title"], _uid("recruitee", o.get("id")), o.get("careers_url"),
                      o.get("location") or o.get("city"))
            for o in data.get("offers", []) if o.get("title")]


def _items_personio(slug):
    root = ET.fromstring(_http_get(f"https://{slug}.jobs.personio.de/xml"))
    items = []
    for p in root.iter("position"):
        name = (p.findtext("name") or "").strip()
        if not name:
            continue
        pid = (p.findtext("id") or "").strip()
        link = f"https://{slug}.jobs.personio.de/job/{pid}" if pid else None
        items.append(make_item(name, _uid("personio", pid), link, (p.findtext("office") or "").strip() or None))
    return items


ATS_FETCHERS = {
    "greenhouse": (_items_greenhouse, "https://boards.greenhouse.io/{slug}"),
    "lever": (_items_lever, "https://jobs.lever.co/{slug}"),
    "recruitee": (_items_recruitee, "https://{slug}.recruitee.com"),
    "personio": (_items_personio, "https://{slug}.jobs.personio.de"),
}


def normalize_sources(raw_sources: list) -> list:
    """
    Turns config entries into uniform dicts: {kind, key, url, slug?}.
    `key` is the cache identity: the URL for browser sources (same as the old
    cache format) or "<type>:<slug>" for ATS feeds.
    """
    sources = []
    for entry in raw_sources:
        if isinstance(entry, str):
            parsed = urlparse(entry)
            if parsed.scheme in ("http", "https") and parsed.netloc:
                sources.append({"kind": "browser", "key": entry, "url": entry})
            else:
                print(f"⚠️  Skipping invalid URL in config.json: {entry!r}")
        elif isinstance(entry, dict):
            kind = str(entry.get("type", "")).lower()
            slug = entry.get("slug")
            if kind in ATS_FETCHERS and slug:
                display = entry.get("url") or ATS_FETCHERS[kind][1].format(slug=slug)
                sources.append({"kind": kind, "key": f"{kind}:{slug}", "url": display, "slug": slug})
            elif entry.get("url") and not kind:
                parsed = urlparse(entry["url"])
                if parsed.scheme in ("http", "https") and parsed.netloc:
                    sources.append({"kind": "browser", "key": entry["url"], "url": entry["url"]})
                    continue
                print(f"⚠️  Skipping invalid source in config.json: {entry!r}")
            else:
                print(f"⚠️  Skipping invalid source in config.json: {entry!r}")
        else:
            print(f"⚠️  Skipping invalid source in config.json: {entry!r}")
    return sources


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------
# Per source:
#   {"jobs": {<key>: {"title", "first_seen", "last_seen", "uid"?}},
#    "last_scan": <iso>, "fails": <int>}
# <key> is the normalized title (plus "#2", "#3"... when several jobs share a
# title). "uid" is optional, so old title-only entries remain valid.

def migrate_cache(raw: dict) -> dict:
    if raw.get("_version") == CACHE_VERSION:
        raw.setdefault("urls", {})
        return raw

    now = datetime.now(timezone.utc).isoformat()
    migrated = {"_version": CACHE_VERSION, "urls": {}}
    for url, value in raw.items():
        if url == "_version":
            continue
        titles = value if isinstance(value, list) else []
        jobs = {
            normalize_key(title): {"title": title, "first_seen": now, "last_seen": now}
            for title in titles
        }
        migrated["urls"][url] = {"jobs": jobs}
    return migrated


def get_source(cache: dict, key: str) -> dict:
    src = cache["urls"].setdefault(key, {"jobs": {}})
    src.setdefault("jobs", {})
    return src


def parse_ts(value):
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def prune_stale_jobs(jobs: dict, retention_days: int) -> int:
    if retention_days <= 0:
        return 0
    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
    stale = [k for k, info in jobs.items()
             if (parse_ts(info.get("last_seen")) or datetime.now(timezone.utc)) < cutoff]
    for k in stale:
        del jobs[k]
    return len(stale)


def note_failure(cache, key):
    src = get_source(cache, key)
    src["fails"] = src.get("fails", 0) + 1


def reset_failures(cache, key):
    get_source(cache, key)["fails"] = 0


def _find_entry(jobs, item, title_key, claimed):
    """
    Entry for this exact job, or None. Rules:
      - same uid                      -> match
      - either side has no uid        -> match on title (legacy / link missing this run)
      - both have uids and they differ -> DIFFERENT job (that's the same-title case)
    Each entry can be claimed once per scan, so two same-title jobs need two entries.
    """
    fallback = None
    for k, info in jobs.items():
        if k in claimed or normalize_key(info.get("title") or k) != title_key:
            continue
        entry_uid = info.get("uid")
        if item["uid"] and entry_uid == item["uid"]:
            return k
        if not item["uid"] or not entry_uid:
            fallback = fallback or k
    return fallback


def _unique_key(jobs, title_key):
    if title_key not in jobs:
        return title_key
    n = 2
    while f"{title_key}#{n}" in jobs:
        n += 1
    return f"{title_key}#{n}"


def record_items(job_cache, key, items, retention_days, reappear_days, trusted_scan=True):
    """
    Shared by browser and feed scans.
    Returns (items_to_report, pruned, tracked_total). Reported = brand new,
    or re-listed after being gone for >= reappear_days.
    """
    src = get_source(job_cache, key)
    jobs = src["jobs"]
    now = datetime.now(timezone.utc)
    now_iso = now.isoformat()
    prev_scan = parse_ts(src.get("last_scan"))
    claimed = set()
    report = []

    for item in items:
        tk = normalize_key(item["title"])
        jkey = _find_entry(jobs, item, tk, claimed)
        if jkey is None:
            jkey = _unique_key(jobs, tk)
            jobs[jkey] = {"title": item["title"], "first_seen": now_iso, "last_seen": now_iso}
            report.append(dict(item, reason="new"))
        else:
            info = jobs[jkey]
            last_seen = parse_ts(info.get("last_seen"))
            if (reappear_days > 0 and prev_scan and last_seen
                    and prev_scan - last_seen >= timedelta(days=reappear_days)):
                info["first_seen"] = now_iso
                report.append(dict(item, reason="relisted"))
            info["last_seen"] = now_iso
        if item["uid"]:
            jobs[jkey]["uid"] = item["uid"]
        claimed.add(jkey)

    if trusted_scan:
        src["last_scan"] = now_iso
    pruned = prune_stale_jobs(jobs, retention_days)
    return report, pruned, len(jobs)


def print_status(matched, reported, tracked, pruned):
    status = (f"    Found {len(matched)} matching jobs this scan "
              f"({len(reported)} to report, {tracked} tracked total")
    status += f", {pruned} pruned as stale)." if pruned else ")."
    print(status)


# ---------------------------------------------------------------------------
# Scanning
# ---------------------------------------------------------------------------

async def scan_feed(source, patterns, job_cache, settings):
    key = source["key"]
    print(f"📡 Fetching feed: {key}")
    fetcher = ATS_FETCHERS[source["kind"]][0]
    try:
        items = await asyncio.to_thread(fetcher, source["slug"])
    except urllib.error.HTTPError as e:
        print(f"❌ Feed {key} failed: HTTP {e.code} (check the slug)")
        note_failure(job_cache, key)
        return None
    except Exception as e:
        print(f"❌ Feed {key} failed: {type(e).__name__}: {e}")
        note_failure(job_cache, key)
        return None

    matched = dedupe_items(match_jobs_by_keyword(items, patterns))
    reported, pruned, tracked = record_items(
        job_cache, key, matched, settings["retention"], settings["reappear"])
    reset_failures(job_cache, key)
    print_status(matched, reported, tracked, pruned)
    return {"key": key, "url": source["url"], "items": reported} if reported else None


async def save_debug_screenshot(page, key):
    try:
        os.makedirs(DEBUG_DIR, exist_ok=True)
        name = re.sub(r"[^a-zA-Z0-9]+", "_", key)[:80].strip("_")
        path = os.path.join(DEBUG_DIR, f"{name}.png")
        await page.screenshot(path=path, full_page=True)
        print(f"    📸 Saved debug screenshot: {path}")
    except Exception as e:
        print(f"    (could not save screenshot: {e})")


async def scan_url(context, source, patterns, job_cache, settings):
    url, key = source["url"], source["key"]
    page = await context.new_page()
    print(f"🔍 Scanning: {url}")
    try:
        # Register the popup handler BEFORE navigation so late banners are caught
        await install_consent_handler(page)
        await goto_with_retry(page, url)

        # 1. Explicit click-through, then give late-injected banners a moment
        await dismiss_consent_walls(page)
        await page.wait_for_timeout(800)
        await dismiss_consent_walls(page)  # second pass catches late banners/iframes

        # 2. Strip anything still blocking the page, then render lazy content
        await remove_overlays(page)
        await autoscroll_and_expand(page)

        # 3. Final cleanup (popups can also appear after scrolling) and extract
        await dismiss_consent_walls(page, max_clicks=1)
        await remove_overlays(page)
        scanned = await extract_page_items(page, settings["use_job_links"])

        if not scanned:
            # A page with no extractable text at all is a broken scan, not "no jobs".
            print("    ⚠️  Page yielded no text -- treating this scan as failed.")
            note_failure(job_cache, key)
            if settings["debug_screenshots"]:
                await save_debug_screenshot(page, key)
            return None

        # 4. Keyword match, 5. compare with everything ever seen
        matched = dedupe_items(match_jobs_by_keyword(scanned, patterns))
        reported, pruned, tracked = record_items(
            job_cache, key, matched, settings["retention"], settings["reappear"])
        reset_failures(job_cache, key)
        print_status(matched, reported, tracked, pruned)

        if not matched:
            print("    ⚠️  Zero matches -- a popup may have blocked the page, or the site needs "
                  "a per-site override (search box, or an ATS feed entry in config.json).")
            if settings["debug_screenshots"]:
                await save_debug_screenshot(page, key)

        return {"key": key, "url": url, "items": reported} if reported else None

    except PlaywrightTimeoutError:
        print(f"❌ Failed processing {url}: Page loading timed out.")
        note_failure(job_cache, key)
    except PlaywrightError as e:
        print(f"❌ Failed processing {url}: Playwright Browser Error -> {e}")
        note_failure(job_cache, key)
    except Exception as e:
        print(f"❌ Failed processing {url}: Internal Exception -> {type(e).__name__}: {e}")
        note_failure(job_cache, key)
    finally:
        await page.close()
    return None


async def run_all(sources, keywords, job_cache, settings):
    patterns = build_keyword_patterns(keywords)
    semaphore = asyncio.Semaphore(settings["concurrency"])

    feed_sources = [s for s in sources if s["kind"] != "browser"]
    browser_sources = [s for s in sources if s["kind"] == "browser"]

    async def bounded(coro_fn, *args):
        async with semaphore:
            return await coro_fn(*args)

    tasks = [bounded(scan_feed, s, patterns, job_cache, settings) for s in feed_sources]

    if browser_sources:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)

            context_kwargs = {"user_agent": USER_AGENT}
            if settings["persist_consent"] and os.path.exists(CONSENT_STATE_FILE):
                context_kwargs["storage_state"] = CONSENT_STATE_FILE
            try:
                context = await browser.new_context(**context_kwargs)
            except Exception as e:
                print(f"⚠️  Could not load {CONSENT_STATE_FILE} ({e}); starting with a fresh browser state.")
                context_kwargs.pop("storage_state", None)
                context = await browser.new_context(**context_kwargs)

            async def maybe_block(route):
                if route.request.resource_type in BLOCKED_RESOURCE_TYPES:
                    await route.abort()
                else:
                    await route.continue_()

            await context.route("**/*", maybe_block)

            tasks += [bounded(scan_url, context, s, patterns, job_cache, settings) for s in browser_sources]
            results = await asyncio.gather(*tasks, return_exceptions=True)

            if settings["persist_consent"]:
                try:
                    await context.storage_state(path=CONSENT_STATE_FILE)
                except Exception as e:
                    print(f"⚠️  Could not save consent state: {e}")
            await browser.close()
    else:
        results = await asyncio.gather(*tasks, return_exceptions=True)

    discoveries = []
    for r in results:
        if isinstance(r, BaseException):
            # One source blowing up must not abort the others.
            print(f"❌ A scan crashed unexpectedly: {type(r).__name__}: {r}")
        elif r:
            discoveries.append(r)
    return discoveries


# ---------------------------------------------------------------------------
# Notification via Brevo HTTP API v3
# ---------------------------------------------------------------------------

def build_email(discoveries, warning_urls):
    content = ""
    if discoveries:
        content += "<h2>🔥 New Job Opportunities Detected</h2>"
        for d in discoveries:
            safe_url = html.escape(d["url"], quote=True)
            lis = ""
            for it in d["items"]:
                label = html.escape(it["title"])
                if it.get("link") and urlparse(it["link"]).scheme in ("http", "https"):
                    label = f'<a href="{html.escape(it["link"], quote=True)}">{label}</a>'
                if it.get("extra"):
                    label += f' <span style="color:#666">&mdash; {html.escape(it["extra"])}</span>'
                if it.get("reason") == "relisted":
                    label += " <em>(re-listed)</em>"
                lis += f"<li>{label}</li>"
            content += f"""
            <div style="margin-bottom: 20px; border-left: 4px solid #4CAF50; padding-left: 10px;">
                <p><strong>Source:</strong> <a href="{safe_url}">{safe_url}</a></p>
                <ul>{lis}</ul>
            </div>
            """
    if warning_urls:
        items = "".join(f"<li>{html.escape(u)}</li>" for u in warning_urls)
        content += ("<h3>⚠️ These sources have failed several runs in a row "
                    "(new jobs there may be going unnoticed)</h3>" f"<ul>{items}</ul>")
    return content


def send_email(discoveries, warning_urls) -> bool:
    api_key = os.getenv("BREVO_API_KEY")
    sender_email = os.getenv("JOB_ALERT_SENDER")
    receiver_email = os.getenv("JOB_ALERT_RECEIVER")
    sender_name = os.getenv("JOB_ALERT_SENDER_NAME", "Job Alert Bot")

    if not all([api_key, sender_email, receiver_email]):
        print("❌ Missing required environment variables. Check BREVO_API_KEY, JOB_ALERT_SENDER, and JOB_ALERT_RECEIVER.")
        return False

    total_new = sum(len(d["items"]) for d in discoveries)
    if total_new:
        subject = f"Update: {total_new} New Tech Job{'s' if total_new != 1 else ''} Found"
    else:
        subject = f"Job alert: {len(warning_urls)} source{'s' if len(warning_urls) != 1 else ''} failing"

    payload = {
        "sender": {"name": sender_name, "email": sender_email},
        "to": [{"email": receiver_email}],
        "subject": subject,
        "htmlContent": build_email(discoveries, warning_urls),
    }
    body = json.dumps(payload).encode("utf-8")
    headers = {"accept": "application/json", "api-key": api_key, "content-type": "application/json"}

    for attempt in range(1, 4):
        req = urllib.request.Request("https://api.brevo.com/v3/smtp/email", data=body,
                                     headers=headers, method="POST")
        try:
            print("🚀 Sending notification via Brevo HTTP API...")
            with urllib.request.urlopen(req, timeout=30) as response:
                res_body = json.loads(response.read().decode("utf-8"))
            if "messageId" in res_body:
                print(f"📧 Notification sent successfully! Message ID: {res_body['messageId']}")
            else:
                print(f"⚠️ Email accepted but response shape changed: {res_body}")
            return True
        except urllib.error.HTTPError as e:
            print(f"❌ Brevo API Error (HTTP {e.code}): {e.read().decode('utf-8', 'replace')}")
            if e.code < 500:          # 4xx won't fix itself by retrying
                return False
        except Exception as e:
            print(f"❌ General failure sending via Brevo API: {e}")
        if attempt < 3:
            time.sleep(3 * attempt)
    return False


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    config = load_json(CONFIG_FILE, {"keywords": [], "urls": []})
    sources = normalize_sources(config.get("urls", []))
    keywords = config.get("keywords", [])
    if not keywords:
        print("⚠️  No keywords configured -- nothing can match.")

    settings = {
        "retention": config.get("job_retention_days", 45),
        "concurrency": config.get("max_concurrency", DEFAULT_CONCURRENCY),
        "persist_consent": config.get("persist_consent_state", True),
        "debug_screenshots": config.get("debug_screenshots", True),
        "reappear": config.get("reappear_after_days", DEFAULT_REAPPEAR_DAYS),
        "use_job_links": config.get("use_job_links", True),
    }
    fail_warn = config.get("source_failure_warning_after", DEFAULT_FAIL_WARN)

    job_cache = migrate_cache(load_json(CACHE_FILE, {"_version": CACHE_VERSION, "urls": {}}))
    snapshot = copy.deepcopy(job_cache)  # for rollback if the email can't be sent

    discoveries = asyncio.run(run_all(sources, keywords, job_cache, settings))

    # Sources that just hit the failure threshold (warn once, not every run)
    warning_sources = []
    if fail_warn > 0:
        warning_sources = [s for s in sources
                           if job_cache["urls"].get(s["key"], {}).get("fails", 0) == fail_warn]
    warning_urls = [s["url"] for s in warning_sources]

    email_ok = True
    if discoveries or warning_urls:
        email_ok = send_email(discoveries, warning_urls)
        if not email_ok:
            # Don't mark jobs as "seen" if the user never heard about them.
            affected = {d["key"] for d in discoveries} | {s["key"] for s in warning_sources}
            for k in affected:
                if k in snapshot["urls"]:
                    job_cache["urls"][k] = snapshot["urls"][k]
                else:
                    job_cache["urls"].pop(k, None)
            print("↩️  Email failed: affected sources were rolled back and will be re-reported next run.")
    else:
        print("✅ No new jobs since last run -- no email sent.")

    save_json_atomic(CACHE_FILE, job_cache)
    if not email_ok:
        sys.exit(1)


if __name__ == "__main__":
    main()
