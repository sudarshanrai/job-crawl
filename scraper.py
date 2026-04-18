import os
import json
import re
import resend
from difflib import SequenceMatcher
from playwright.sync_api import sync_playwright

# --- Configuration ---
CONFIG_FILE = "config.json"
CACHE_FILE = "seen_jobs.json"

def load_json(file, default):
    if os.path.exists(file):
        with open(file, "r") as f:
            return json.load(f)
    return default

config = load_json(CONFIG_FILE, {"keywords": ["Java"], "urls": []})
job_cache = load_json(CACHE_FILE, {})

def is_similar(a, b, threshold=0.85):
    """Prevents duplicates if the title changes slightly (e.g., adding 'New')."""
    return SequenceMatcher(None, a.lower(), b.lower()).ratio() > threshold

def is_valid_job_title(text, keywords):
    """
    Advanced filter to separate job titles from site noise.
    """
    text_clean = " ".join(text.split())
    
    # 1. Must contain one of our keywords
    if not any(k.lower() in text_clean.lower() for k in keywords):
        return False
    
    # 2. Kill 'False Positive' patterns (Search counts, pagination, etc.)
    noise_patterns = [
        r"\d+\s*(results|jobs|found|angebote)", # "13 jobs found"
        r"suche nach",                          # "Search for..."
        r"page\s*\d+",                          # "Page 1"
        r"sort by",                             # "Sort by date"
        r"cookie", r"privacy", r"impressum"      # Legal noise
    ]
    if any(re.search(p, text_clean, re.IGNORECASE) for p in noise_patterns):
        return False
    
    # 3. Logic Check: Job titles are usually between 10 and 80 characters
    if not (10 <= len(text_clean) <= 90):
        return False
        
    return True

def extract_jobs_heuristically(page, keywords):
    """
    Analyzes the page for links and headers that look like job listings.
    """
    found_titles = set()
    
    # We target 'Prominent' elements usually used for job titles
    potential_elements = page.query_selector_all("a, h1, h2, h3, .job-title, [class*='title']")
    
    for el in potential_elements:
        try:
            if el.is_visible():
                text = el.inner_text().strip()
                if is_valid_job_title(text, keywords):
                    found_titles.add(" ".join(text.split()))
        except:
            continue
            
    return list(found_titles)

# --- Execution Logic ---
new_discoveries = []

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    context = browser.new_context(user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36")
    
    for url in config.get("urls", []):
        page = context.new_page()
        print(f"🧐 Heuristic Analysis: {url}")
        
        try:
            page.goto(url, wait_until="networkidle", timeout=60000)
            page.wait_for_timeout(3000) # Wait for JS to render titles
            
            current_titles = extract_jobs_heuristically(page, config["keywords"])
            old_titles = job_cache.get(url, [])
            
            # Determine what is actually NEW
            site_new_jobs = []
            for current in current_titles:
                if not any(is_similar(current, old) for old in old_titles):
                    site_new_jobs.append(current)
            
            if site_new_jobs:
                new_discoveries.append({"url": url, "titles": site_new_jobs})
            
            # Update cache with everything seen today
            job_cache[url] = current_titles
            
        except Exception as e:
            print(f"❌ Failed {url}: {e}")
        finally:
            page.close()

    browser.close()

# --- Notifications ---
if new_discoveries:
    resend.api_key = os.getenv("RESEND_API_KEY")
    
    html_content = "<h2>New Job Opportunities Detected</h2>"
    for item in new_discoveries:
        html_content += f"""
        <div style="margin-bottom: 20px; border-left: 4px solid #007bff; padding-left: 10px;">
            <p><strong>Source:</strong> <a href="{item['url']}">{item['url']}</a></p>
            <ul>{"".join([f"<li>{t}</li>" for t in item['titles']])}</ul>
        </div>
        """
    
    try:
        resend.Emails.send({
            "from": "JobAlert <onboarding@resend.dev>",
            "to": [config["receiver_email"]],
            "subject": "Update: New Java Jobs Found",
            "html": html_content
        })
        print(f"✅ Email sent with {len(new_discoveries)} site updates.")
    except Exception as e:
        print(f"📧 Email error: {e}")

# Final save
with open(CACHE_FILE, "w") as f:
    json.dump(job_cache, f, indent=4)
