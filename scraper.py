import os
import json
import re
import resend
from playwright.sync_api import sync_playwright

# --- Configuration ---
CONFIG_FILE = "config.json"
# 1. Changed filename to seen_jobs.json as requested
CACHE_FILE = "seen_jobs.json" 

with open(CONFIG_FILE, "r") as f:
    config = json.load(f)

# Load cache: { "site_url": ["snippet1", "snippet2"] }
if os.path.exists(CACHE_FILE):
    with open(CACHE_FILE, "r") as f:
        pattern_cache = json.load(f)
else:
    pattern_cache = {}

def scrape_and_extract_patterns(url):
    found_patterns = set()
    
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36"
        )
        page = context.new_page()
        
        print(f"🔍 Analyzing: {url}")
        try:
            page.goto(url, wait_until="networkidle", timeout=60000)
            
            # Handle Cookie Dialogs
            try:
                cookie_selectors = ["Accept", "Agree", "Allow all", "Accept Cookies", "OK"]
                for text in cookie_selectors:
                    btn = page.get_by_role("button").get_by_text(re.compile(text, re.IGNORECASE)).first
                    if btn.is_visible():
                        btn.click()
                        page.wait_for_timeout(1000)
                        break
            except:
                pass

            page.wait_for_timeout(2000) 
            page_text = page.inner_text("body")
            
            for k in config["keywords"]:
                # The regex: looks for keyword + 40 chars of context
                regex_pattern = rf"(.{{0,40}}\b{re.escape(k)}\b.{{0,40}})"
                matches = re.findall(regex_pattern, page_text, re.IGNORECASE)
                
                for m in matches:
                    clean_snippet = " ".join(m.split())
                    found_patterns.add(clean_snippet)
            
            return list(found_patterns)

        except Exception as e:
            print(f"❌ Failed to process {url}: {e}")
            return None
        finally:
            browser.close()

# --- Main Logic ---
new_discoveries = []

for url in config["urls"]:
    current_patterns = scrape_and_extract_patterns(url)
    
    if current_patterns is not None:
        # Get what we found in the PREVIOUS run for this URL
        old_patterns = pattern_cache.get(url, [])
        
        # 2. Compare: Only keep snippets not present in the last run
        new_stuff = [p for p in current_patterns if p not in old_patterns]
        
        if new_stuff:
            new_discoveries.append({
                "url": url,
                "snippets": new_stuff
            })
        
        # 3. Update the memory with current findings for the NEXT run
        pattern_cache[url] = current_patterns

# --- Notification & Saving ---
if new_discoveries:
    # Use environment variable for security
    resend.api_key = os.getenv("RESEND_API_KEY")
    
    html_body = f"<h2>Found {len(new_discoveries)} New Updates</h2>"
    for item in new_discoveries:
        html_body += f"""
        <div style="margin-bottom: 25px; border-left: 5px solid #f89820; padding-left: 15px;">
            <p><strong>Site:</strong> <a href="{item['url']}">{item['url']}</a></p>
            <ul style="background: #f4f4f4; padding: 10px; font-family: monospace;">
        """
        for s in item['snippets']:
            html_body += f"<li>...{s}...</li>"
        html_body += "</ul></div>"

    try:
        resend.Emails.send({
            "from": "JobTracker <onboarding@resend.dev>",
            "to": [config["receiver_email"]],
            "subject": "Alert: New Java Opportunities Detected",
            "html": html_body
        })
        print(f"✅ Email sent with {len(new_discoveries)} updates.")
    except Exception as e:
        print(f"📧 Email failed: {e}")

# 4. Save updated results to seen_jobs.json regardless of whether an email was sent
# This ensures that "seen" items are remembered even if the email fails.
with open(CACHE_FILE, "w") as f:
    json.dump(pattern_cache, f, indent=4)
