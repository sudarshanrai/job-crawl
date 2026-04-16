import os
import json
import resend
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright
from urllib.parse import urljoin

# --- Configuration ---
# Ensure your config.json looks like this:
# {
#   "urls": ["https://example-jobs.com", "https://tech-careers.io"],
#   "keywords": ["Java"],
#   "receiver_email": "you@email.com"
# }

with open("config.json", "r") as f:
    config = json.load(f)

CACHE_FILE = "seen_jobs.json"
seen_job_urls = set()

# Robust Cache Loading
if os.path.exists(CACHE_FILE):
    try:
        with open(CACHE_FILE, "r") as f:
            content = f.read().strip()
            if content:
                data = json.loads(content)
                seen_job_urls = set(data)
    except Exception as e:
        print(f"Cache reset: {e}")

def scrape_site(url):
    """Returns the total number of keyword occurrences on the page."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        print(f"Scanning {url} for {config['keywords']}...")
        try:
            page.goto(url, wait_until="networkidle", timeout=60000)
            # Get all text content from the body, lowercased
            page_content = page.content().lower()
            
            total_matches = 0
            for k in config["keywords"]:
                total_matches += page_content.count(k.lower())
            
            return total_matches
        except Exception as e:
            print(f"Failed to load {url}: {e}")
            return None # Return None to indicate a skip/error
        finally:
            browser.close()

# --- Main Execution Loop ---
updates = []

for url_item in config["urls"]:
    current_count = scrape_site(url_item)
    
    if current_count is not None:
        last_count = counts_cache.get(url_item, 0)
        
        if current_count > last_count:
            diff = current_count - last_count
            updates.append({
                "url": url_item,
                "new_matches": diff,
                "total": current_count
            })
        
        # Update cache with the latest count regardless of change
        counts_cache[url_item] = current_count

# --- Notification Logic ---
if updates:
    resend.api_key = os.getenv("RESEND_API_KEY")
    
    html_body = "<h2>Keyword Increase Detected</h2>"
    for item in updates:
        html_body += f"""
        <div style="margin-bottom: 15px; border-left: 4px solid #f89820; padding-left: 10px;">
            <p><strong>Site:</strong> {item['url']}<br>
            <strong>Change:</strong> +{item['new_matches']} new mentions<br>
            <strong>Current Total:</strong> {item['total']}</p>
            <a href="{item['url']}" style="color: #007bff;">View Site →</a>
        </div>
        """

    try:
        resend.Emails.send({
            "from": "JobBot <onboarding@resend.dev>",
            "to": [config["receiver_email"]],
            "subject": f"Update: {len(updates)} sites have more keyword matches",
            "html": html_body
        })
        
        # Save the updated counts to cache
        with open(CACHE_FILE, "w") as f:
            json.dump(counts_cache, f)
        print("Success: Notification sent and cache updated.")
    except Exception as e:
        print(f"Email failed: {e}")
else:
    print("No increases in keyword mentions detected.")
