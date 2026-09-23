"""
Job-alert scraper: visits each configured career page, extracts job
listings that match your keywords, and emails you (via Brevo) whenever a
genuinely new listing shows up.

Changelog vs. the original version -- all aimed at the duplicate/repeated
listings you were seeing:

1. Root cause of the duplicates: the extractor selected several tag types
   at once (a, h1-h4, li, tr, [class*="job"], [class*="position"]) and on
   most sites a job "card" is a wrapping <a>/<li> around a <h3> title
   (plus a location tag and a "Read more" link). That wrapper AND the
   heading inside it both match the selector, so one job produced two
   overlapping lines: "Berlin Full Stack Engineer Read more" and
   "Full Stack Engineer". `extract_page_snippet` now keeps only the
   "leaf" matches (elements that don't themselves wrap another match),
   so each card contributes exactly one line, and any CTA text baked
   directly into that line ("... Read more") is stripped by
   `strip_trailing_noise`.

2. Repeats across separate runs: the old cache only remembered the
   *previous* scan's snapshot, so a listing that briefly vanished from a
   page (re-sorted, temporarily unlisted, wording tweaked) and then
   reappeared looked "new" again. The cache is now a persistent,
   normalized "ever seen" record per URL (with first_seen/last_seen
   timestamps), so a listing is only ever flagged as new once. Old
   caches are auto-migrated the first time this runs.

3. Stale entries are pruned after `job_retention_days` (default 45) of
   not appearing, so the cache doesn't grow forever -- and a job that
   genuinely disappears for months and comes back is treated as new
   again, which is usually what you want.

4. Smaller fixes: retry once on a page-load timeout, skip malformed
   URLs in config.json instead of crashing, and the outgoing email's
   sender *name* is no longer accidentally set to a whole sentence.
"""

import os
import re
import json
import html
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse
import urllib.request
import urllib.error
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError, Error as PlaywrightError

CONFIG_FILE = "config.json"
CACHE_FILE = "seen_jobs.json"
CACHE_VERSION = 2
SELECTOR = 'a, h1, h2, h3, h4, li, tr, [class*="job"], [class*="position"]'


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


def dismiss_consent_walls(page):
    """
    Best-effort click-through of cookie/consent banners and "unblock
    content" walls. Silently does nothing if no matching button is found --
    never raises, since most pages won't have one.
    """
    for frame in page.frames:
        for text in CONSENT_BUTTON_TEXTS:
            try:
                locator = frame.get_by_text(text, exact=False)
                if locator.count() > 0:
                    locator.first.click(timeout=1000)
            except Exception:
                pass


def autoscroll_and_expand(page, max_rounds=8):
    """
    Repeatedly scrolls down and clicks any visible "load more" style
    button, to force lazy-loaded / paginated job lists to fully render
    before extraction. Stops early once the page stops growing.
    """
    last_height = 0
    for _ in range(max_rounds):
        page.mouse.wheel(0, 3000)
        page.wait_for_timeout(700)
        for text in LOAD_MORE_TEXTS:
            try:
                btn = page.get_by_text(text, exact=False)
                if btn.count() > 0:
                    btn.first.click(timeout=800)
                    page.wait_for_timeout(700)
            except Exception:
                pass
        try:
            height = page.evaluate("document.body.scrollHeight")
        except Exception:
            break
        if height == last_height:
            break
        last_height = height


def extract_page_snippet(page) -> list:
    """
    Pulls visible text from likely job-related elements, across the main
    page AND every iframe on it, then collapses DOM-nested duplicates.

    A job card is commonly a wrapping <a>/<li> around a heading, e.g.
    <a><span>Berlin</span><h3>Full Stack Engineer</h3><span>Read more
    </span></a>. Our selector matches both the wrapper and the heading, so
    grabbing every match's innerText naively produces two overlapping
    lines for one job. The in-page JS below keeps only "leaf" matches --
    elements that don't themselves contain another matched element -- so
    each card contributes exactly one line. Any CTA text still baked into
    a leaf itself is cleaned up afterwards by strip_trailing_noise.
    """
    all_lines = []
    for frame in page.frames:
        try:
            lines = frame.evaluate(
                r"""(sel) => {
                    const nodes = Array.from(document.querySelectorAll(sel));
                    const leaves = nodes.filter(
                        el => !nodes.some(other => other !== el && el.contains(other))
                    );
                    return leaves
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


def match_jobs_by_keyword(lines: list, keywords: list) -> list:
    """
    Pure keyword matching -- no AI involved. A line counts as a job listing
    if it contains any target keyword (or a known spacing/hyphen variant)
    as a whole phrase, case-insensitive.
    """
    if not keywords:
        return []
    patterns = build_keyword_patterns(keywords)
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


def goto_with_retry(page, url, attempts=2, timeout_ms=60000):
    last_err = None
    for attempt in range(1, attempts + 1):
        try:
            page.goto(url, wait_until="networkidle", timeout=timeout_ms)
            return
        except PlaywrightTimeoutError as e:
            last_err = e
            if attempt < attempts:
                print(f"    ⏳ Timed out loading (attempt {attempt}/{attempts}), retrying...")
    raise last_err


# --- Load config & cache ---
config = load_json(CONFIG_FILE, {"keywords": [], "urls": []})
config["urls"] = valid_urls(config.get("urls", []))
keywords = config.get("keywords", [])
RETENTION_DAYS = config.get("job_retention_days", 45)

job_cache = migrate_cache(load_json(CACHE_FILE, {"_version": CACHE_VERSION, "urls": {}}))

# --- Execution Logic ---
new_discoveries = []

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    context = browser.new_context(user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36")

    for url in config["urls"]:
        page = context.new_page()
        print(f"🔍 Scanning: {url}")

        try:
            goto_with_retry(page, url)
            page.wait_for_timeout(3000)

            # 1. Click through cookie banners / "unblock content" walls
            dismiss_consent_walls(page)
            page.wait_for_timeout(1500)  # let any newly-unlocked iframe attach & load

            # 2. Force lazy-loaded / paginated results to render
            autoscroll_and_expand(page)

            # 3. Pull candidate text from the page AND any iframes on it,
            #    already deduplicated at the DOM level
            condensed_lines = extract_page_snippet(page)

            # 4. Keep only the lines that match a target keyword
            current_titles = sorted(match_jobs_by_keyword(condensed_lines, keywords))

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

            pruned = prune_stale_jobs(jobs, RETENTION_DAYS)

            if site_new_jobs:
                new_discoveries.append({"url": url, "titles": site_new_jobs})

            status = (f"    Found {len(current_titles)} matching jobs this scan "
                      f"({len(site_new_jobs)} brand new, {len(jobs)} tracked total")
            status += f", {pruned} pruned as stale)." if pruned else ")."
            print(status)
            if not current_titles:
                print("    ⚠️  Zero matches -- if this site normally has openings, it likely "
                      "needs a per-site override (e.g. typing into a search box, or hitting "
                      "the ATS's JSON API directly). See notes below the script.")

        except PlaywrightTimeoutError:
            print(f"❌ Failed processing {url}: Page loading timed out (exceeded 60s limit).")
        except PlaywrightError as e:
            print(f"❌ Failed processing {url}: Playwright Browser Error -> {e}")
        except Exception as e:
            print(f"❌ Failed processing {url}: Internal Exception -> {type(e).__name__}: {e}")
        finally:
            page.close()

    browser.close()

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
