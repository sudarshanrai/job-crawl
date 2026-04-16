import os
import json
import resend
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

# 1. Configuration (Example structure)
# Your config.json should now include a "selector" for each URL
# Example: {"urls": [{"link": "https://site.com/jobs", "selector": "a.job-link"}], ...}
with open("config.json", "r") as f:
    config = json.load(f)

CACHE_FILE = "seen_jobs.json"
seen_job_urls = set()

if os.path.exists(CACHE_FILE):
    try:
        with open(CACHE_FILE, "r") as f:
            content = f.read().strip()
            if content:  # Only try to load if there's actually text
                data = json.loads(content)
                if isinstance(data, list):
                    seen_job_urls = set(data)
            else:
                print("Cache file is empty. Initializing...")
    except (json.JSONDecodeError, ValueError):
        print("Cache corrupted. Starting fresh.")
        seen_job_urls = set()
else:
    print("No cache file found. Starting fresh.")

new_jobs = []

def scrape_site(url_data):
    url = url_data["link"]
    selector = url_data["selector"] # The CSS class for the job link
    found = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(url, wait_until="networkidle")
        
        # Get the rendered HTML
        html = page.content()
        soup = BeautifulSoup(html, "html.parser")
        
        # Find all elements matching your selector
        # Example: soup.select("a.job-title-link")
        job_elements = soup.select(selector)
        
        for el in job_elements:
            title = el.get_text(strip=True)
            link = el.get('href')
            
            # Ensure the link is a full URL
            if link and not link.startswith("http"):
                from urllib.parse import urljoin
                link = urljoin(url, link)
            
            if link:
                found.append({"title": title, "url": link})
        
        browser.close()
    return found

# 2. Execution Loop
for url_item in config["urls"]:
    try:
        print(f"Scraping {url_item['link']}...")
        results = scrape_site(url_item)
        
        for job in results:
            if job['url'] not in seen_job_urls:
                # Filter by keyword manually since we aren't using AI
                if any(k.lower() in job['title'].lower() for k in config["keywords"]):
                    job['source_site'] = url_item['link']
                    new_jobs.append(job)
                    seen_job_urls.add(job['url'])
    except Exception as e:
        print(f"Error on {url_item['link']}: {e}")

# 3. Notification (Same as your original logic)
if new_jobs:
    resend.api_key = os.getenv("RESEND_API_KEY")
    html_body = f"<h2>New Job Matches Found ({len(new_jobs)})</h2>"
    for job in new_jobs:
        html_body += f"<p><strong>{job['title']}</strong><br><a href='{job['url']}'>Link</a></p><hr>"

    resend.Emails.send({
        "from": "JobBot <onboarding@resend.dev>",
        "to": [config["receiver_email"]],
        "subject": f"Alert: {len(new_jobs)} New Jobs",
        "html": html_body
    })

    with open(CACHE_FILE, "w") as f:
        json.dump(list(seen_job_urls), f)
    print("Updates sent.")
else:
    print("No new matches.")
