import os
import json
import re
import resend
from playwright.sync_api import sync_playwright

# --- Configuration ---
with open("config.json", "r") as f:
    config = json.load(f)

CACHE_FILE = "pattern_cache.json"

# Load cache: { "url": ["pattern_hash_1", "pattern_hash_2"] }
if os.path.exists(CACHE_FILE):
    with open(CACHE_FILE, "r") as f:
        pattern_cache = json.load(f)
else:
    pattern_cache = {}

def get_patterns(url):
    """Extracts unique text snippets surrounding the keywords."""
    found_patterns = set()
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        print(f"Analyzing patterns on {url}...")
        try:
            page.goto(url, wait_until="networkidle", timeout=60000)
            # Get visible text only to avoid script/style noise
            page_text = page.inner_text("body")
            
            for k in config["keywords"]:
                # Regex: Finds keyword and captures 40 chars of surrounding context
                # \b ensures we match the whole word (Java, not JavaScript)
                regex_pattern = rf"(.{{0,40}}\b{re.escape(k)}\b.{{0,40}})"
                matches = re.findall(regex_pattern, page_text, re.IGNORECASE)
                
                for m in matches:
                    # Clean up whitespace and normalize for comparison
                    clean_pattern = " ".join(m.split())
                    found_patterns.add(clean_pattern)
                    
            return list(found_patterns)
        except Exception as e:
            print(f"Error analyzing {url}: {e}")
            return None
        finally:
            browser.close()

# --- Main Execution Loop ---
new_discoveries = []

for url_item in config["urls"]:
    current_patterns = get_patterns(url_item)
    
    if current_patterns is not None:
        old_patterns = pattern_cache.get(url_item, [])
        
        # Identify patterns that exist now but didn't last time
        new_patterns = [p for p in current_patterns if p not in old_patterns]
        
        if new_patterns:
            new_discoveries.append({
                "url": url_item,
                "snippets": new_patterns
            })
        
        # Update cache with the current state
        pattern_cache[url_item] = current_patterns

# --- Notification Logic ---
if new_discoveries:
    resend.api_key = os.getenv("RESEND_API_KEY")
    
    html_body = "<h2>New Java Patterns Detected</h2>"
    for discovery in new_discoveries:
        html_body += f"""
        <div style="margin-bottom: 20px; border-left: 4px solid #007bff; padding-left: 10px;">
            <p><strong>Source:</strong> <a href="{discovery['url']}">{discovery['url']}</a></p>
            <p><strong>New Contexts Found:</strong></p>
            <ul style="color: #444; font-family: monospace;">
        """
        for snippet in discovery['snippets']:
            html_body += f"<li>...{snippet}...</li>"
        
        html_body += "</ul></div>"

    try:
        resend.Emails.send({
            "from": "PatternBot <onboarding@resend.dev>",
            "to": [config["receiver_email"]],
            "subject": f"Alert: {len(new_discoveries)} sites have new Java activity",
            "html": html_body
        })
        
        with open(CACHE_FILE, "w") as f:
            json.dump(pattern_cache, f)
        print("Success: New patterns reported.")
    except Exception as e:
        print(f"Email failed: {e}")
else:
    print("No new patterns detected.")
