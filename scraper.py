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


def extract_page_snippet(page) -> list:
    """
    Pulls visible text from elements likely to contain job listings
    (links, headings, and anything whose class hints at "job"/"position").
    Strips empty/junk entries and de-duplicates while preserving order.
    """
    lines = page.evaluate("""() => {
        const elements = document.querySelectorAll('a, h1, h2, h3, h4, [class*="job"], [class*="position"]');
        return Array.from(elements)
            .map(el => {
                const text = el.innerText || el.textContent;
                return text ? text.trim() : "";
            })
            .filter(text => text.length > 5 && text.length < 200);
    }""")

    # dict.fromkeys() dedupes but keeps first-seen order, unlike set()
    return list(dict.fromkeys(lines))


def match_jobs_by_keyword(lines: list, keywords: list) -> list:
    """
    Pure keyword matching -- no AI involved.
    A line counts as a job listing if it contains any target keyword as a
    whole word (case-insensitive), e.g. "Backend" matches "Senior Backend
    Engineer" but a bare substring match wouldn't accidentally fire on
    unrelated words that merely contain the letters.
    """
    if not keywords:
        return []
    patterns = [re.compile(r'\b' + re.escape(kw) + r'\b', re.IGNORECASE) for kw in keywords]
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
            page.wait_for_timeout(4000)

            # 1. Pull candidate text from the page
            condensed_lines = extract_page_snippet(page)

            # 2. Keep only the lines that match a target keyword
            current_titles = sorted(match_jobs_by_keyword(condensed_lines, config["keywords"]))

            old_titles = job_cache.get(url, [])

            # 3. Detect what is genuinely new
            site_new_jobs = [job for job in current_titles if job not in old_titles]

            if site_new_jobs:
                new_discoveries.append({"url": url, "titles": site_new_jobs})

            # Cache the latest snapshot
            job_cache[url] = current_titles
            print(f"    Found {len(current_titles)} matching jobs. ({len(site_new_jobs)} brand new)")

        except PlaywrightTimeoutError:
            print(f"❌ Failed processing {url}: Page loading timed out (exceeded 60s limit).")
        except PlaywrightError as e:
            # Catches driver/browser specific issues like DNS failures, SSL bugs, blockages
            print(f"❌ Failed processing {url}: Playwright Browser Error -> {e}")
        except Exception as e:
            # Catches unexpected runtime script errors
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
