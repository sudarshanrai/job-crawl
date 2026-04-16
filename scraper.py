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

new_jobs = []

def scrape_site(url):
    found = []
    with sync_playwright() as p:
        # Launching browser
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        
        print(f"Scanning {url} for {config['keywords']}...")
        try:
            page.goto(url, wait_until="networkidle", timeout=60000)
            html_content = page.content()
            soup = BeautifulSoup(html_content, "html.parser")
            
            # 1. Broad Search: Check if keywords exist anywhere in the page text
            page_text = soup.get_text(separator=' ', strip=True).lower()
            print(page_text)
            if not any(k.lower() in page_text for k in config["keywords"]):
                return [] # Exit early if keyword isn't on the page at all

            # 2. Targeted Search: Find the links related to those keywords
            # We look for links where the text OR the immediate surrounding contains the keyword
            links = soup.find_all("a", href=True)
            for link in links:
                href = link['href']
                full_url = urljoin(url, href)
                
                # Get the link text and its parent's text to catch "Java" in a description
                link_text = link.get_text(strip=True)
                parent_text = link.find_parent().get_text(strip=True) if link.parent else ""
                combined_context = (link_text + " " + parent_text).lower()

                if any(k.lower() in combined_context for k in config["keywords"]):
                    # Basic noise filtering: ignore common nav links
                    if len(link_text) > 2 and full_url not in [url, url + "/"]:
                        found.append({
                            "title": link_text if len(link_text) > 5 else f"Java Opportunity at {url}",
                            "url": full_url
                        })
        except Exception as e:
            print(f"Failed to load {url}: {e}")
        finally:
            browser.close()
    return found

# --- Main Execution Loop ---
for url_item in config["urls"]:
    try:
        results = scrape_site(url_item)
        
        for job in results:
            # Notify ONLY if this specific URL has not been seen before
            if job['url'] not in seen_job_urls:
                job['source_site'] = url_item
                new_jobs.append(job)
                seen_job_urls.add(job['url'])
    except Exception as e:
        print(f"Error processing {url_item}: {e}")

# --- Notification Logic ---
if new_jobs:
    resend.api_key = os.getenv("RESEND_API_KEY")
    
    html_body = f"<h2>New Java Jobs Detected ({len(new_jobs)})</h2>"
    for job in new_jobs:
        html_body += f"""
        <div style="margin-bottom: 15px; border-left: 4px solid #f89820; padding-left: 10px;">
            <p style="font-size: 16px;"><strong>{job['title']}</strong><br>
            <span style="color: #666;">Found on: {job['source_site']}</span><br>
            <a href="{job['url']}" style="color: #007bff;">Open Job Listing →</a></p>
        </div>
        """

    try:
        resend.Emails.send({
            "from": "JobBot <onboarding@resend.dev>",
            "to": [config["receiver_email"]],
            "subject": f"New Java Match: {len(new_jobs)} updates",
            "html": html_body
        })
        
        # Save the updated cache only after successful email
        with open(CACHE_FILE, "w") as f:
            json.dump(list(seen_job_urls), f)
        print(f"Success: {len(new_jobs)} new jobs reported.")
    except Exception as e:
        print(f"Email failed: {e}")
else:
    print("No new Java keywords or links detected since last check.")
