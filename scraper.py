import os
import json
import re
import resend
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
    Minimizes tokens by pulling visible elements that likely contain text/links.
    Drops script, style tags, and ultra-long legal footers.
    """
    # Evaluate a small script inside playwright to extract text clean lines
    lines = page.evaluate("""() => {
        const elements = document.querySelectorAll('a, h1, h2, h3, h4, [class*="job"], [class*="position"]');
        return Array.from(elements)
            .map(el => el.innerText.trim())
            .filter(text => text.length > 5 && text.length < 200);
    }""")
    
    # Keep unique items to further optimize token footprint
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
            model='gemini-1.5-flash',
            contents=[prompt, page_content],
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=list[str], # Forces Gemini to return exactly JSON: ["Title 1", "Title 2"]
                temperature=0.1 # Low temperature ensures consistency
            ),
        )
        return json.loads(response.text)
    except Exception as e:
        print(f"🤖 Gemini Analysis Error: {e}")
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
            page.wait_for_timeout(4000) # Give extra time for JS frameworks to spin up lists
            
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
            print(f"   Found {len(current_titles)} relevant jobs. ({len(site_new_jobs)} brand new)")
            
        except Exception as e:
            print(f"❌ Failed processing {url}: {e}")
        finally:
            page.close()

    browser.close()

# --- Notifications ---
if new_discoveries:
    resend.api_key = os.getenv("RESEND_API_KEY")
    
    html_content = "<h2>🔥 New Job Opportunities Detected</h2>"
    for item in new_discoveries:
        html_content += f"""
        <div style="margin-bottom: 20px; border-left: 4px solid #4CAF50; padding-left: 10px;">
            <p><strong>Source:</strong> <a href="{item['url']}">{item['url']}</a></p>
            <ul>{"".join([f"<li>{t}</li>" for t in item['titles']])}</ul>
        </div>
        """
    
    try:
        resend.Emails.send({
            "from": "JobAlert <onboarding@resend.dev>",
            "to": [config["receiver_email"]],
            "subject": "Update: New Tech Jobs Found",
            "html": html_content
        })
        print(f"📧 Notification sent successfully!")
    except Exception as e:
        print(f"📧 Email delivery error: {e}")

# Save state
with open(CACHE_FILE, "w") as f:
    json.dump(job_cache, f, indent=4)
