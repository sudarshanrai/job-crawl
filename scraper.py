import os
import json
import re
import urllib.request
import urllib.error
from google import genai
from google.genai import types
from playwright.sync_api import sync_playwright

CONFIG_FILE = "config.json"
CACHE_FILE = "seen_jobs.json"

def load_json(file, default):
    if os.path.exists(file):
        with open(file, "r") as f:
            return json.load(f)
    return default

config = load_json(CONFIG_FILE, {"keywords": [], "urls": []})
job_cache = load_json(CACHE_FILE, {})

# Initialize Gemini Client (Requires GEMINI_API_KEY environment variable)
ai_client = genai.Client()

def extract_page_snippet(page) -> str:
    """
    Minimizes tokens safely by pulling visible elements that contain text.
    Defensively strips out empty elements to prevent JS runtime exceptions.
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
    
    # Keep unique items to optimize token footprint
    unique_lines = list(set(lines))
    return "\n".join(unique_lines)

def analyze_jobs_with_ai(page_content: str, keywords: list) -> list:
    """
    Uses Gemini to extract jobs based on strict criteria using Structured Outputs.
    """
    if not page_content.strip():
        return []

    prompt = f"""
    You are an expert HR data parsing assistant.
    Analyze the following extracted text from a company careers website.
    Identify and extract explicit job titles that match or are closely related to these target domains: {', '.join(keywords)}.
    
    Ignore navigation elements, filter terms, country names, or generic dashboard text.
    Extract ONLY valid, specific job open positions.
    """

    try:
        response = ai_client.models.generate_content(
                    model='gemini-2.5-flash',
                    contents=[prompt, page_content],
                    config=types.GenerateContentConfig(
                        response_mime_type="application/json",
                        response_schema=list[str],
                        temperature=0.1
                    ),
        )
        return json.loads(response.text)
    except Exception as e:
        print(f"🤖 Gemini Analysis Error: {type(e).__name__} -> {e}")
        return []

# --- Execution Logic ---
new_discoveries = []

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    context = browser.new_context(user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36")
    
    for url in config.get("urls", []):
        page = context.new_page()
        print(f"🔍 AI-Powered Extraction: {url}")
        
        try:
            page.goto(url, wait_until="networkidle", timeout=60000)
            page.wait_for_timeout(4000) 
            
            # 1. Get Token-Friendly condensed content
            condensed_content = extract_page_snippet(page)
            
            # 2. Let Gemini extract the exact matching titles
            current_titles = analyze_jobs_with_ai(condensed_content, config["keywords"])
            
            old_titles = job_cache.get(url, [])
            
            # 3. Detect what is genuinely new
            site_new_jobs = [job for job in current_titles if job not in old_titles]
            
            if site_new_jobs:
                new_discoveries.append({"url": url, "titles": site_new_jobs})
            
            # Cache the latest snapshot
            job_cache[url] = current_titles
            print(f"    Found {len(current_titles)} relevant jobs. ({len(site_new_jobs)} brand new)")
            
        except p.errors.TimeoutError:
            print(f"❌ Failed processing {url}: Page loading timed out (exceeded 60s limit).")
        except p.errors.Error as e:
            # Catches driver/browser specific issues like DNS failures, SSL bugs, blockages
            print(f"❌ Failed processing {url}: Playwright Browser Error -> {e.message}")
        except Exception as e:
            # Catches unexpected runtime script errors
            print(f"❌ Failed processing {url}: Internal Exception -> {type(e).__name__}: {e}")
        finally:
            page.close()

    browser.close()

# --- Notifications via Brevo HTTP API v3 ---
if new_discoveries:
    # Fetch API Key and Emails from environment variables
    BREVO_API_KEY = os.getenv("BREVO_API_KEY")
    sender_email = os.getenv("JOB_ALERT_SENDER")
    receiver_email = os.getenv("JOB_ALERT_RECEIVER")

    if not all([BREVO_API_KEY, sender_email, receiver_email]):
        print("❌ Missing required environment variables. Check BREVO_API_KEY, JOB_ALERT_SENDER, and JOB_ALERT_RECEIVER.")
    else:
        # Build the HTML Content
        html_content = "<h2>🔥 New Job Opportunities Detected</h2>"
        for item in new_discoveries:
            html_content += f"""
            <div style="margin-bottom: 20px; border-left: 4px solid #4CAF50; padding-left: 10px;">
                <p><strong>Source:</strong> <a href="{item['url']}">{item['url']}</a></p>
                <ul>{"".join([f"<li>{t}</li>" for t in item['titles']])}</ul>
            </div>
            """
        
        # Structure the payload strictly according to Brevo v3 API standards
        payload = {
            "sender": {"name": "JobAlert", "email": sender_email},
            "to": [{"email": receiver_email}],
            "subject": "Update: New Tech Jobs Found",
            "htmlContent": html_content
        }
        
        # Target the transactional email endpoint
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
                # Brevo returns a messageId upon a successful request transmission
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
