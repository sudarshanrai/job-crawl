import os
import json
import resend
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright
from urllib.parse import urljoin

# Load config
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
        # Launching with specific args for GitHub Actions
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        
        print(f"Navigating to {url}...")
        page.goto(url, wait_until="networkidle", timeout=60000)
        
        # Get HTML and Parse
        soup = BeautifulSoup(page.content(), "html.parser")
        
        # Since we don't have specific selectors, we look for ALL anchors (links)
        # and filter them by keywords later.
        links = soup.find_all("a", href=True)
        
        for link in links:
            title = link.get_text(strip=True)
            href = link['href']
            full_url = urljoin(url, href)
            
            if title and len(title) > 5: # Ignore tiny links like "Home" or "Back"
                found.append({"title": title, "url": full_url})
        
        browser.close()
    return found

# 2. Corrected Loop (url_item is a STRING here)
for url_item in config["urls"]:
    try:
        # url_item is just the string from your list
        results = scrape_site(url_item)
        
        for job in results:
            if job['url'] not in seen_job_urls:
                # Check if keywords (Java, Backend) are in the link text
                if any(k.lower() in job['title'].lower() for k in config["keywords"]):
                    job['source_site'] = url_item
                    new_jobs.append(job)
                    seen_job_urls.add(job['url'])
    except Exception as e:
        print(f"Error on {url_item}: {e}")

# 3. Notification Logic
if new_jobs:
    resend.api_key = os.getenv("RESEND_API_KEY")
    
    html_body = f"<h2>New Job Matches Found ({len(new_jobs)})</h2>"
    for job in new_jobs:
        html_body += f"""
        <div style="margin-bottom: 10px; border-left: 3px solid #007bff; padding-left: 10px;">
            <p><strong>{job['title']}</strong><br>
            Source: {job['source_site']}<br>
            <a href="{job['url']}">View Listing</a></p>
        </div><hr>
        """

    resend.Emails.send({
        "from": "JobBot <onboarding@resend.dev>",
        "to": [config["receiver_email"]],
        "subject": f"Alert: {len(new_jobs)} Jobs Found",
        "html": html_body
    })

    with open(CACHE_FILE, "w") as f:
        json.dump(list(seen_job_urls), f)
    print(f"Found {len(new_jobs)} new jobs. Email sent.")
else:
    print("No new matches found.")
