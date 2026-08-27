import os
import json
import re
import urllib.request
import urllib.error
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError, Error as PlaywrightError

CONFIG_FILE = "config.json"
CACHE_FILE = "seen_jobs.json"

def load_json(file, default):
    if os.path.exists(file):
        with open(file, "r") as f:
            return json.load(f)
    return default

config = load_json(CONFIG_FILE, {"keywords": [], "urls": []})
job_cache = load_json(CACHE_FILE, {})

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
    page AND every iframe on it. Many career pages embed a third-party ATS
    (Personio, Greenhouse, Lever...) in a cross-origin iframe that a plain
    page.evaluate() on just the top frame would never see -- Playwright can
    read into those frames directly, which a page's own JS cannot do
    because of same-origin restrictions.
    """
    all_lines = []
    for frame in page.frames:
        try:
            lines = frame.evaluate("""() => {
                const elements = document.querySelectorAll(
                    'a, h1, h2, h3, h4, li, tr, [class*="job"], [class*="position"]'
                );
                return Array.from(elements)
                    .map(el => {
                        const text = el.innerText || el.textContent;
                        return text ? text.trim() : "";
                    })
                    .filter(text => text.length > 5 && text.length < 200);
            }""")
            all_lines.extend(lines)
        except Exception:
            continue  # frame not ready / detached / blocked -- skip it

    # dict.fromkeys() dedupes but keeps first-seen order, unlike set()
    return list(dict.fromkeys(all_lines))


def build_keyword_patterns(keywords: list):
    patterns = []
    for kw in keywords:
        variants = {kw}
        variants.update(COMPOUND_ALIASES.get(kw.lower(), []))
        for variant in variants:
            patterns.append(re.compile(r'\b' + re.escape(variant) + r'\b', re.IGNORECASE))
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


# --- Execution Logic ---
new_discoveries = []

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    context = browser.new_context(user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36")

    for url in config.get("urls", []):
        page = context.new_page()
        print(f"🔍 Scanning: {url}")

        try:
            page.goto(url, wait_until="networkidle", timeout=60000)
            page.wait_for_timeout(3000)

            # 1. Click through cookie banners / "unblock content" walls
            dismiss_consent_walls(page)
            page.wait_for_timeout(1500)  # let any newly-unlocked iframe attach & load

            # 2. Force lazy-loaded / paginated results to render
            autoscroll_and_expand(page)

            # 3. Pull candidate text from the page AND any iframes on it
            condensed_lines = extract_page_snippet(page)

            # 4. Keep only the lines that match a target keyword
            current_titles = sorted(match_jobs_by_keyword(condensed_lines, config["keywords"]))

            old_titles = job_cache.get(url, [])

            # 5. Detect what is genuinely new
            site_new_jobs = [job for job in current_titles if job not in old_titles]

            if site_new_jobs:
                new_discoveries.append({"url": url, "titles": site_new_jobs})

            # Cache the latest snapshot
            job_cache[url] = current_titles
            print(f"    Found {len(current_titles)} matching jobs. ({len(site_new_jobs)} brand new)")
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

    if not all([BREVO_API_KEY, sender_email, receiver_email]):
        print("❌ Missing required environment variables. Check BREVO_API_KEY, JOB_ALERT_SENDER, and JOB_ALERT_RECEIVER.")
    else:
        html_content = "<h2>🔥 New Job Opportunities Detected</h2>"
        for item in new_discoveries:
            html_content += f"""
            <div style="margin-bottom: 20px; border-left: 4px solid #4CAF50; padding-left: 10px;">
                <p><strong>Source:</strong> <a href="{item['url']}">{item['url']}</a></p>
                <ul>{"".join([f"<li>{t}</li>" for t in item['titles']])}</ul>
            </div>
            """

        payload = {
            "sender": {"name": "JobAlert", "email": sender_email},
            "to": [{"email": receiver_email}],
            "subject": "Update: New Tech Jobs Found",
            "htmlContent": html_content
        }

        api_url = "https://api.brevo.com/v3/smtp/email"
        req = urllib.request.Request(
            api_url,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "accept": "application/json",
                "api-key": BREVO_API_KEY,
                "content-type": "application/json"
            },
            method="POST"
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

# Save state
with open(CACHE_FILE, "w") as f:
    json.dump(job_cache, f, indent=4)
