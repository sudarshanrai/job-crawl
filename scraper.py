"""
Job-alert scraper: visits each configured career page, extracts job
listings that match your keywords, and emails you (via Brevo) whenever a
genuinely new listing shows up.

Performance changelog vs. the previous version -- the logic (dedup,
persistent "ever seen" cache, retention pruning, keyword matching) is
UNCHANGED; only how fast it gets there has changed:

1. URLs are now scanned concurrently (Playwright's async API + an
   asyncio.Semaphore) instead of one after another. This is the single
   biggest win once you have more than a couple of career pages -- wall
   time now scales with (number of URLs / max_concurrency) instead of
   the sum of every page's load time. Tune `max_concurrency` in
   config.json (default 5) to your machine's CPU/RAM and to how
   aggressive you're comfortable being against the target sites.

2. `page.goto(..., wait_until="networkidle")` is gone. Many career sites
   run chat widgets, analytics beacons, or polling requests that never
   let the network go fully idle, so that call was frequently eating its
   entire timeout for nothing -- and then eating it again on retry. We
   now load with `domcontentloaded` (fires as soon as the DOM exists,
   not when every subresource has finished) and make one *bounded*
   best-effort attempt at networkidle afterwards. If it doesn't settle
   in time we just proceed to extraction anyway -- autoscroll_and_expand
   and extraction both tolerate a still-settling page.

3. Image/font/video requests are aborted at the network layer
   (`context.route`). They don't contribute to the text we extract, but
   they add bytes and delay "the page is idle." Stylesheets are
   deliberately NOT blocked -- innerText only returns text for elements
   CSS actually renders as visible, so dropping CSS would surface hidden
   nav duplicates / mobile-menu clones and quietly change what gets
   extracted.

4. Consent-wall and "load more" detection used to run one Playwright
   locator round-trip per candidate phrase (17 and 11 phrases
   respectively) per frame, even on pages with no banner at all. Both
   now search every phrase via a single regex-based locator (one round
   trip), and consent dismissal stops as soon as one banner is closed
   instead of still probing the remaining phrases.

5. The leaf-filtering step in extract_page_snippet (added to fix the
   duplicate-listing bug) used to compare every matched element against
   every other matched element with `.contains()` -- O(n^2). It's now a
   single ancestor-walk per element using a Set for O(1) membership
   checks -- same result, roughly O(n * dom_depth) instead. Mostly
   matters on sites where the broad SELECTOR sweeps up thousands of
   nav/footer links.

6. Keyword regexes are compiled once per run instead of once per URL.

7. Flat `wait_for_timeout` sleeps were trimmed throughout -- they were
   sized for a worst case and paid in full every time, not just when
   actually needed.

Everything else -- cache format, retention/pruning, URL validation,
keyword matching, and the Brevo email -- is identical to before.
"""

import os
import re
import json
import html
import asyncio
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse
import urllib.request
import urllib.error
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError, Error as PlaywrightError

CONFIG_FILE = "config.json"
CACHE_FILE = "seen_jobs.json"
CACHE_VERSION = 2
SELECTOR = 'a, h1, h2, h3, h4, li, tr, [class*="job"], [class*="position"]'
DEFAULT_CONCURRENCY = 5
# Blocking these speeds up load / time-to-idle without touching what
# innerText extracts. Stylesheets are excluded on purpose -- see changelog.
BLOCKED_RESOURCE_TYPES = {"image", "media", "font"}


def load_json(file, default):
    if os.path.exists(file):
        with open(file, "r") as f:
            return json.load(f)
    return default


# Buttons that gate content behind a click: cookie banners, and the
# "unblock third-party content" walls German sites commonly put in front of
# embedded ATS widgets (Personio, YouTube, etc.) for GDPR reasons.
CONSENT_BUTTON_TEXTS = [
    "accept all", "accept cookies", "accept", "agree", "i agree", "allow all",
    "unblock content", "accept required service and unblock content",
    "alle akzeptieren", "akzeptieren", "zustimmen", "einverstanden",
    "auswahl bestätigen",
]

# "Load more" style buttons that gate paginated / lazily-loaded job lists.
LOAD_MORE_TEXTS = [
    "load more", "show more", "more jobs", "view more",
    "weitere anzeigen", "mehr anzeigen", "mehr laden", "weitere jobs",
]

# Combined into single regexes so detection is one Playwright round-trip
# per frame instead of one round-trip per candidate phrase.
_CONSENT_REGEX = re.compile("(" + "|".join(re.escape(t) for t in CONSENT_BUTTON_TEXTS) + ")", re.IGNORECASE)
_LOAD_MORE_REGEX = re.compile("(" + "|".join(re.escape(t) for t in LOAD_MORE_TEXTS) + ")", re.IGNORECASE)

# A few tech terms show up as one word, two words, or hyphenated
# ("Fullstack" / "Full Stack" / "Full-Stack"). Map each keyword (lowercased)
# to the extra phrasings it should also match, without loosening matching
# for everything else (so "Java" still won't fire on "JavaScript").
COMPOUND_ALIASES = {
    "fullstack": ["full stack", "full-stack"],
    "frontend": ["front end", "front-end"],
    "backend": ["back end", "back-end"],
}

# Call-to-action text some sites bake into the same element as the job
# title itself ("Full Stack Engineer Read more"). Stripped from the end
# of a line, longest phrase first so "apply now" wins over "apply".
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
    """Repeatedly trims trailing CTA text ('Read more', 'Jetzt bewerben', ...)
    since some sites stack more than one ('... Read more ›')."""
    previous = None
    while previous != text:
        previous = text
        text = _NOISE_PATTERN.sub("", text).strip()
    return text


def normalize_key(text: str) -> str:
    """Canonical form used ONLY for cache identity -- never for display or
    keyword matching -- so a job doesn't look 'new' again just because a
    site re-rendered it with different case or spacing."""
    return re.sub(r"\s+", " ", text).strip().lower()


async def dismiss_consent_walls(page):
    """
    Best-effort click-through of cookie/consent banners and "unblock
    content" walls. Silently does nothing if no matching button is found --
    never raises, since most pages won't have one. Checks all candidate
    phrases in a single locator (one round-trip per frame instead of one
    per phrase) and stops after the first successful click, since one
    dismissed banner is normally enough.
    """
    for frame in page.frames:
        try:
            locator = frame.get_by_text(_CONSENT_REGEX)
            if await locator.count() > 0:
                await locator.first.click(timeout=1000)
                return
        except Exception:
            pass


async def autoscroll_and_expand(page, max_rounds=8):
    """
    Repeatedly scrolls down and clicks any visible "load more" style
    button, to force lazy-loaded / paginated job lists to fully render
    before extraction. Stops early once the page stops growing.
    """
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
    """
    Pulls visible text from likely job-related elements, across the main
    page AND every iframe on it, then collapses DOM-nested duplicates.

    Same "keep only leaf matches" idea as before (a job card is commonly a
    wrapping <a>/<li> around a heading, and our broad selector matches
    both), but done as a single ancestor-walk with a Set for O(1)
    membership checks instead of comparing every matched element against
    every other one with `.contains()`. Identical output, much cheaper on
    pages where SELECTOR sweeps up thousands of nav/footer links.
    """
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
            continue  # frame not ready / detached / blocked -- skip it

    cleaned = (strip_trailing_noise(line) for line in all_lines)
    cleaned = [line for line in cleaned if len(line) > 3]
    # dict.fromkeys() dedupes but keeps first-seen order, unlike set()
    return list(dict.fromkeys(cleaned))


def build_keyword_patterns(keywords: list):
    patterns = []
    for kw in keywords:
        variants = {kw}
        variants.update(COMPOUND_ALIASES.get(kw.lower(), []))
        for variant in variants:
            patterns.append(re.compile(r"\b" + re.escape(variant) + r"\b", re.IGNORECASE))
    return patterns


def match_jobs_by_keyword(lines: list, patterns: list) -> list:
    """
    Pure keyword matching -- no AI involved. A line counts as a job listing
    if it contains any target keyword (or a known spacing/hyphen variant)
    as a whole phrase, case-insensitive. `patterns` is pre-compiled once
    per run (see run_all) rather than rebuilt for every single URL.
    """
    if not patterns:
        return []
    return [line for line in lines if any(p.search(line) for p in patterns)]


def valid_urls(raw_urls: list) -> list:
    """Drops obviously malformed entries instead of letting page.goto()
    crash the whole run on a bad config.json line."""
    good = []
    for u in raw_urls:
        parsed = urlparse(u)
        if parsed.scheme in ("http", "https") and parsed.netloc:
            good.append(u)
        else:
            print(f"⚠️  Skipping invalid URL in config.json: {u!r}")
    return good


def migrate_cache(raw: dict) -> dict:
    """
    Upgrades a cache file to the current schema.

    v1 (original): {"<url>": ["Job Title", ...]}
    v2 (current):  {"_version": 2, "urls": {"<url>": {"jobs": {
                       "<normalized_key>": {"title": ..., "first_seen": ...,
                                             "last_seen": ...}}}}}

    Titles already present in a v1 cache are carried over as already-seen
    so upgrading doesn't re-flag everything as new in one go.
    """
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
    """
    Drops jobs that haven't shown up in a scan for `retention_days`, so
    the cache doesn't grow forever and a listing that vanishes for months
    and later reappears gets treated as new again. Returns how many were
    removed.
    """
    if retention_days <= 0:
        return 0
    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
    stale = [k for k, info in jobs.items() if datetime.fromisoformat(info["last_seen"]) < cutoff]
    for k in stale:
        del jobs[k]
    return len(stale)


async def goto_with_retry(page, url, attempts=2, timeout_ms=45000, settle_timeout_ms=8000):
    """
    Loads with `domcontentloaded` (fires as soon as the DOM exists) instead
    of requiring `networkidle` (which can burn the entire timeout on sites
    with polling/analytics/chat widgets that never go fully quiet). Then
    makes one *bounded* best-effort attempt to let the network settle --
    if it doesn't settle in time we proceed anyway, since
    autoscroll_and_expand and extraction both tolerate a still-settling
    page. On a fast site this returns as soon as things go quiet, not
    after a fixed delay.
    """
    last_err = None
    for attempt in range(1, attempts + 1):
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            try:
                await page.wait_for_load_state("networkidle", timeout=settle_timeout_ms)
            except PlaywrightTimeoutError:
                pass  # fine -- best-effort only, we still extract afterwards
            return
        except PlaywrightTimeoutError as e:
            last_err = e
            if attempt < attempts:
                print(f"    ⏳ Timed out loading (attempt {attempt}/{attempts}), retrying...")
    raise last_err


async def scan_url(context, url, patterns, job_cache, retention_days):
    page = await context.new_page()
    print(f"🔍 Scanning: {url}")
    try:
        await goto_with_retry(page, url)

        # 1. Click through cookie banners / "unblock content" walls
        await dismiss_consent_walls(page)
        await page.wait_for_timeout(800)  # let any newly-unlocked iframe attach & load

        # 2. Force lazy-loaded / paginated results to render
        await autoscroll_and_expand(page)

        # 3. Pull candidate text from the page AND any iframes on it,
        #    already deduplicated at the DOM level
        condensed_lines = await extract_page_snippet(page)

        # 4. Keep only the lines that match a target keyword
        current_titles = sorted(match_jobs_by_keyword(condensed_lines, patterns))

        # 5. Compare against everything ever seen for this URL (not
        #    just last run), keyed by a normalized form of the title
        jobs = get_url_jobs(job_cache, url)
        now_iso = datetime.now(timezone.utc).isoformat()
        site_new_jobs = []
        for title in current_titles:
            key = normalize_key(title)
            if key in jobs:
                jobs[key]["last_seen"] = now_iso
            else:
                jobs[key] = {"title": title, "first_seen": now_iso, "last_seen": now_iso}
                site_new_jobs.append(title)

        pruned = prune_stale_jobs(jobs, retention_days)

        status = (f"    Found {len(current_titles)} matching jobs this scan "
                  f"({len(site_new_jobs)} brand new, {len(jobs)} tracked total")
        status += f", {pruned} pruned as stale)." if pruned else ")."
        print(status)
        if not current_titles:
            print("    ⚠️  Zero matches -- if this site normally has openings, it likely "
                  "needs a per-site override (e.g. typing into a search box, or hitting "
                  "the ATS's JSON API directly). See notes below the script.")

        return {"url": url, "titles": site_new_jobs} if site_new_jobs else None

    except PlaywrightTimeoutError:
        print(f"❌ Failed processing {url}: Page loading timed out.")
    except PlaywrightError as e:
        print(f"❌ Failed processing {url}: Playwright Browser Error -> {e}")
    except Exception as e:
        print(f"❌ Failed processing {url}: Internal Exception -> {type(e).__name__}: {e}")
    finally:
        await page.close()
    return None


async def run_all(urls, keywords, job_cache, retention_days, max_concurrency):
    patterns = build_keyword_patterns(keywords)  # compiled once for the whole run
    semaphore = asyncio.Semaphore(max_concurrency)

    async def bounded_scan(context, url):
        async with semaphore:
            return await scan_url(context, url, patterns, job_cache, retention_days)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        )

        async def maybe_block(route):
            if route.request.resource_type in BLOCKED_RESOURCE_TYPES:
                await route.abort()
            else:
                await route.continue_()

        await context.route("**/*", maybe_block)

        results = await asyncio.gather(*(bounded_scan(context, url) for url in urls))
        await browser.close()

    return [r for r in results if r]


# --- Load config & cache ---
config = load_json(CONFIG_FILE, {"keywords": [], "urls": []})
config["urls"] = valid_urls(config.get("urls", []))
keywords = config.get("keywords", [])
RETENTION_DAYS = config.get("job_retention_days", 45)
MAX_CONCURRENCY = config.get("max_concurrency", DEFAULT_CONCURRENCY)

job_cache = migrate_cache(load_json(CACHE_FILE, {"_version": CACHE_VERSION, "urls": {}}))

# --- Execution Logic ---
new_discoveries = asyncio.run(
    run_all(config["urls"], keywords, job_cache, RETENTION_DAYS, MAX_CONCURRENCY)
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
