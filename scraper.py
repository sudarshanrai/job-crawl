"""
Job-alert scraper: checks each configured source for job listings that match
your keywords and emails you (via Brevo) whenever a genuinely new listing
shows up.

What's new in this version (dedup, persistent "ever seen" cache, retention
pruning and keyword matching are UNCHANGED):

1. ATS feeds first. A source can now be a public ATS feed (Greenhouse, Lever,
   Recruitee, Personio) instead of a web page. No browser, no cookie banners,
   no popups -- just a fast HTTP request. Plain URL strings in config.json
   still work exactly as before and go through Playwright.

2. Popup handling is much sturdier:
   - `page.add_locator_handler` (Playwright >= 1.42) is registered before
     navigation, so a consent banner that appears late is clicked
     automatically whenever it blocks an action.
   - Dismissal now targets known CMP button IDs (OneTrust, Cookiebot,
     Usercentrics, Borlabs, ...) and real buttons/links by role rather than
     any text node, and loops so stacked popups are all handled.
   - A last-resort overlay remover strips leftover fixed/sticky popups and
     restores page scrolling before extraction.

3. Consent state is remembered across runs (`consent_state.json`), so most
   banners never show again on later runs.

4. Zero-match pages save a screenshot to `debug/` so you can see whether a
   popup was in the way or the selector missed.

Optional config.json keys:
    "keywords": [...], "urls": [...], "job_retention_days": 45,
    "max_concurrency": 5, "persist_consent_state": true,
    "debug_screenshots": true

`urls` entries can be a string (browser scan) or an object:
    {"type": "greenhouse", "slug": "acme"}
    {"type": "lever",      "slug": "acme"}
    {"type": "recruitee",  "slug": "acme"}
    {"type": "personio",   "slug": "acme"}
    optionally with "url": "<link shown in the email>"
"""

import os
import re
import json
import html
import asyncio
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse
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
# Blocking these speeds up load / time-to-idle without touching what
# innerText extracts. Stylesheets are excluded on purpose (hidden-element
# duplicates would otherwise show up in innerText).
BLOCKED_RESOURCE_TYPES = {"image", "media", "font"}
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"


def load_json(file, default):
    if os.path.exists(file):
        with open(file, "r") as f:
            return json.load(f)
    return default


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


def strip_trailing_noise(text: str) -> str:
    previous = None
    while previous != text:
        previous = text
        text = _NOISE_PATTERN.sub("", text).strip()
    return text


def normalize_key(text: str) -> str:
    """Canonical form used ONLY for cache identity."""
    return re.sub(r"\s+", " ", text).strip().lower()


def build_keyword_patterns(keywords: list):
    patterns = []
    for kw in keywords:
        variants = {kw}
        variants.update(COMPOUND_ALIASES.get(kw.lower(), []))
        for variant in variants:
            patterns.append(re.compile(r"\b" + re.escape(variant) + r"\b", re.IGNORECASE))
    return patterns


def match_jobs_by_keyword(lines: list, patterns: list) -> list:
    if not patterns:
        return []
    return [line for line in lines if any(p.search(line) for p in patterns)]


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


async def extract_page_snippet(page) -> list:
    """Visible text from likely job elements (page + iframes), leaf matches only."""
    all_lines = []
    for frame in page.frames:
        try:
            lines = await frame.evaluate(
                r"""(sel) => {
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
                    return nodes
                        .filter(el => !hasMatchedDescendant.has(el))
                        .map(el => (el.innerText || el.textContent || "").replace(/\s+/g, " ").trim())
                        .filter(t => t.length > 5 && t.length < 200);
                }""",
                SELECTOR,
            )
            all_lines.extend(lines)
        except Exception:
            continue

    cleaned = (strip_trailing_noise(line) for line in all_lines)
    cleaned = [line for line in cleaned if len(line) > 3]
    return list(dict.fromkeys(cleaned))


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


def _titles_greenhouse(slug):
    data = json.loads(_http_get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs"))
    return [j["title"] for j in data.get("jobs", []) if j.get("title")]


def _titles_lever(slug):
    data = json.loads(_http_get(f"https://api.lever.co/v0/postings/{slug}?mode=json"))
    return [j["text"] for j in data if j.get("text")]


def _titles_recruitee(slug):
    data = json.loads(_http_get(f"https://{slug}.recruitee.com/api/offers/"))
    return [o["title"] for o in data.get("offers", []) if o.get("title")]


def _titles_personio(slug):
    root = ET.fromstring(_http_get(f"https://{slug}.jobs.personio.de/xml"))
    return [(p.findtext("name") or "").strip() for p in root.iter("position") if (p.findtext("name") or "").strip()]


ATS_FETCHERS = {
    "greenhouse": (_titles_greenhouse, "https://boards.greenhouse.io/{slug}"),
    "lever": (_titles_lever, "https://jobs.lever.co/{slug}"),
    "recruitee": (_titles_recruitee, "https://{slug}.recruitee.com"),
    "personio": (_titles_personio, "https://{slug}.jobs.personio.de"),
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

def migrate_cache(raw: dict) -> dict:
    if raw.get("_version") == CACHE_VERSION:
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


def get_url_jobs(cache: dict, url: str) -> dict:
    return cache["urls"].setdefault(url, {"jobs": {}})["jobs"]


def prune_stale_jobs(jobs: dict, retention_days: int) -> int:
    if retention_days <= 0:
        return 0
    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
    stale = [k for k, info in jobs.items() if datetime.fromisoformat(info["last_seen"]) < cutoff]
    for k in stale:
        del jobs[k]
    return len(stale)


def record_titles(job_cache, key, current_titles, retention_days):
    """Shared by browser and feed scans. Returns (new_titles, pruned, tracked_total)."""
    jobs = get_url_jobs(job_cache, key)
    now_iso = datetime.now(timezone.utc).isoformat()
    new_titles = []
    for title in current_titles:
        k = normalize_key(title)
        if k in jobs:
            jobs[k]["last_seen"] = now_iso
        else:
            jobs[k] = {"title": title, "first_seen": now_iso, "last_seen": now_iso}
            new_titles.append(title)
    pruned = prune_stale_jobs(jobs, retention_days)
    return new_titles, pruned, len(jobs)


def print_status(current_titles, new_titles, tracked, pruned):
    status = (f"    Found {len(current_titles)} matching jobs this scan "
              f"({len(new_titles)} brand new, {tracked} tracked total")
    status += f", {pruned} pruned as stale)." if pruned else ")."
    print(status)


# ---------------------------------------------------------------------------
# Scanning
# ---------------------------------------------------------------------------

async def scan_feed(source, patterns, job_cache, retention_days):
    print(f"📡 Fetching feed: {source['key']}")
    fetcher = ATS_FETCHERS[source["kind"]][0]
    try:
        titles = await asyncio.to_thread(fetcher, source["slug"])
    except urllib.error.HTTPError as e:
        print(f"❌ Feed {source['key']} failed: HTTP {e.code} (check the slug)")
        return None
    except Exception as e:
        print(f"❌ Feed {source['key']} failed: {type(e).__name__}: {e}")
        return None

    current_titles = sorted(set(match_jobs_by_keyword(titles, patterns)))
    new_titles, pruned, tracked = record_titles(job_cache, source["key"], current_titles, retention_days)
    print_status(current_titles, new_titles, tracked, pruned)
    return {"url": source["url"], "titles": new_titles} if new_titles else None


async def save_debug_screenshot(page, key):
    try:
        os.makedirs(DEBUG_DIR, exist_ok=True)
        name = re.sub(r"[^a-zA-Z0-9]+", "_", key)[:80].strip("_")
        path = os.path.join(DEBUG_DIR, f"{name}.png")
        await page.screenshot(path=path, full_page=True)
        print(f"    📸 Saved debug screenshot: {path}")
    except Exception as e:
        print(f"    (could not save screenshot: {e})")


async def scan_url(context, source, patterns, job_cache, retention_days, debug_screenshots):
    url = source["url"]
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
        condensed_lines = await extract_page_snippet(page)

        # 4. Keyword match, 5. compare with everything ever seen
        current_titles = sorted(match_jobs_by_keyword(condensed_lines, patterns))
        new_titles, pruned, tracked = record_titles(job_cache, source["key"], current_titles, retention_days)
        print_status(current_titles, new_titles, tracked, pruned)

        if not current_titles:
            print("    ⚠️  Zero matches -- a popup may have blocked the page, or the site needs "
                  "a per-site override (search box, or an ATS feed entry in config.json).")
            if debug_screenshots:
                await save_debug_screenshot(page, source["key"])

        return {"url": url, "titles": new_titles} if new_titles else None

    except PlaywrightTimeoutError:
        print(f"❌ Failed processing {url}: Page loading timed out.")
    except PlaywrightError as e:
        print(f"❌ Failed processing {url}: Playwright Browser Error -> {e}")
    except Exception as e:
        print(f"❌ Failed processing {url}: Internal Exception -> {type(e).__name__}: {e}")
    finally:
        await page.close()
    return None


async def run_all(sources, keywords, job_cache, retention_days, max_concurrency,
                  persist_consent, debug_screenshots):
    patterns = build_keyword_patterns(keywords)
    semaphore = asyncio.Semaphore(max_concurrency)

    feed_sources = [s for s in sources if s["kind"] != "browser"]
    browser_sources = [s for s in sources if s["kind"] == "browser"]

    async def bounded(coro_fn, *args):
        async with semaphore:
            return await coro_fn(*args)

    tasks = [bounded(scan_feed, s, patterns, job_cache, retention_days) for s in feed_sources]
    results = []

    if browser_sources:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)

            context_kwargs = {"user_agent": USER_AGENT}
            if persist_consent and os.path.exists(CONSENT_STATE_FILE):
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

            tasks += [bounded(scan_url, context, s, patterns, job_cache, retention_days, debug_screenshots)
                      for s in browser_sources]
            results = await asyncio.gather(*tasks)

            if persist_consent:
                try:
                    await context.storage_state(path=CONSENT_STATE_FILE)
                except Exception as e:
                    print(f"⚠️  Could not save consent state: {e}")
            await browser.close()
    else:
        results = await asyncio.gather(*tasks)

    return [r for r in results if r]


# --- Load config & cache ---
config = load_json(CONFIG_FILE, {"keywords": [], "urls": []})
sources = normalize_sources(config.get("urls", []))
keywords = config.get("keywords", [])
RETENTION_DAYS = config.get("job_retention_days", 45)
MAX_CONCURRENCY = config.get("max_concurrency", DEFAULT_CONCURRENCY)
PERSIST_CONSENT = config.get("persist_consent_state", True)
DEBUG_SCREENSHOTS = config.get("debug_screenshots", True)

job_cache = migrate_cache(load_json(CACHE_FILE, {"_version": CACHE_VERSION, "urls": {}}))

# --- Execution Logic ---
new_discoveries = asyncio.run(
    run_all(sources, keywords, job_cache, RETENTION_DAYS, MAX_CONCURRENCY,
            PERSIST_CONSENT, DEBUG_SCREENSHOTS)
)

# --- Notifications via Brevo HTTP API v3 ---
if new_discoveries:
    BREVO_API_KEY = os.getenv("BREVO_API_KEY")
    sender_email = os.getenv("JOB_ALERT_SENDER")
    receiver_email = os.getenv("JOB_ALERT_RECEIVER")
    sender_name = os.getenv("JOB_ALERT_SENDER_NAME", "Job Alert Bot")

    if not all([BREVO_API_KEY, sender_email, receiver_email]):
        print("❌ Missing required environment variables. Check BREVO_API_KEY, JOB_ALERT_SENDER, and JOB_ALERT_RECEIVER.")
    else:
        total_new = sum(len(d["titles"]) for d in new_discoveries)
        html_content = "<h2>🔥 New Job Opportunities Detected</h2>"
        for item in new_discoveries:
            safe_url = html.escape(item["url"])
            list_items = "".join(f"<li>{html.escape(t)}</li>" for t in item["titles"])
            html_content += f"""
            <div style="margin-bottom: 20px; border-left: 4px solid #4CAF50; padding-left: 10px;">
                <p><strong>Source:</strong> <a href="{safe_url}">{safe_url}</a></p>
                <ul>{list_items}</ul>
            </div>
            """

        payload = {
            "sender": {"name": sender_name, "email": sender_email},
            "to": [{"email": receiver_email}],
            "subject": f"Update: {total_new} New Tech Job{'s' if total_new != 1 else ''} Found",
            "htmlContent": html_content,
        }

        api_url = "https://api.brevo.com/v3/smtp/email"
        req = urllib.request.Request(
            api_url,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "accept": "application/json",
                "api-key": BREVO_API_KEY,
                "content-type": "application/json",
            },
            method="POST",
        )

        try:
            print("🚀 Sending notification via Brevo HTTP API...")
            with urllib.request.urlopen(req) as response:
                res_body = json.loads(response.read().decode("utf-8"))
                if "messageId" in res_body:
                    print(f"📧 Notification sent successfully! Message ID: {res_body['messageId']}")
                else:
                    print(f"⚠️ Email sent but payload response structure shifted: {res_body}")
        except urllib.error.HTTPError as e:
            print(f"❌ Brevo API Error (HTTP {e.code}): {e.read().decode('utf-8')}")
        except Exception as e:
            print(f"❌ General failure sending via Brevo API: {e}")
else:
    print("✅ No new jobs since last run -- no email sent.")

# Save state
with open(CACHE_FILE, "w") as f:
    json.dump(job_cache, f, indent=4)
