import os
import json
import re
import resend
from playwright.sync_api import sync_playwright

# --- Configuration ---
# Ensure your config.json contains: {"urls": [...], "keywords": ["Java"], "receiver_email": "..."}
CONFIG_FILE = "config.json"
CACHE_FILE = "pattern_cache.json"

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
        # Launching with a realistic User-Agent to avoid immediate blocks
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36"
        )
        page = context.new_page()
        
        print(f"🔍 Analyzing: {url}")
        try:
            # 1. Navigate with a generous timeout for slow sites
            page.goto(url, wait_until="networkidle", timeout=60000)
            
            # 2. Handle Cookie Dialogs (Search for common 'Accept' patterns)
            # This looks for buttons with text like 'Accept', 'Agree', 'Allow'
            try:
                cookie_selectors = ["Accept", "Agree", "Allow all", "Accept Cookies", "OK", "Consen"]
                for text in cookie_selectors:
                    btn = page.get_by_role("button").get_by_text(re.compile(text, re.IGNORECASE)).first
                    if btn.is_visible():
                        btn.click()
                        page.wait_for_timeout(1000) # Wait for overlay to fade
                        break
            except:
                pass # If no button is found, just continue

            # 3. Extra wait for slow-rendering Angular/React job boards
            page.wait_for_timeout(2000) 
            
            # 4. Extract visible text only
            page_text = page.inner_text("body")
            
            # 5. Regex Pattern Extraction
            for k in config["keywords"]:
                # Matches keyword with 40 chars of context on either side
                # \b ensures we match "Java" but not "JavaScript"
                regex_pattern = rf"(.{{0,40}}\b{re.escape(k)}\b.{{0,40}})"
                matches = re.findall(regex_pattern, page_text, re.IGNORECASE)
                
                for m in matches:
                    clean_snippet = " ".join(m.split()) # Remove extra whitespace/newlines
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
        old_patterns = pattern_cache.get(url, [])
        
        # Filter for patterns we haven't seen on this specific URL before
        new_stuff = [p for p in current_patterns if p not in old_patterns]
        
        if new_stuff:
            new_discoveries.append({
                "url": url,
                "snippets": new_stuff
            })
        
        # Update the cache with latest findings
        pattern_cache[url] = current_patterns

# --- Notification ---
if new_discoveries:
    resend.api_key = os.getenv("RESEND_API_KEY")
    
    html_body = "<h2>New Java Patterns Detected</h2>"
    for item in new_discoveries:
        html_body += f"""
        <div style="margin-bottom: 25px; border-left: 5px solid #f89820; padding-left: 15px;">
            <p><strong>Site:</strong> <a href="{item['url']}">{item['url']}</a></p>
            <p><strong>New Contextual Matches:</strong></p>
            <ul style="background: #f4f4f4; padding: 10px; list-style: none; font-family: monospace;">
        """
        for s in item['snippets']:
            html_body += f"<li style='margin-bottom: 5px;'>...{s}...</li>"
        html_body += "</ul></div>"

    try:
        resend.Emails.send({
            "from": "JobTracker <onboarding@resend.dev>",
            "to": [config["receiver_email"]],
            "subject": f"Alert: {len(new_discoveries)} sites updated with new Java activity",
            "html": html_body
        })
        
        # Save updated cache to disk
        with open(CACHE_FILE, "w") as f:
            json.dump(pattern_cache, f)
        print(f"✅ Success: {len(new_discoveries)} updates sent.")
    except Exception as e:
        print(f"📧 Email failed: {e}")
else:
    print("💤 No new patterns found since last check.")
