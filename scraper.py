import os
import json
import resend
from scrapegraphai.graphs import SmartScraperGraph

# 1. Gemini Configuration
# Ensure you have GEMINI_API_KEY in your GitHub Secrets
graph_config = {
    "llm": {
        "api_key": os.getenv("GEMINI_API_KEY"),
        "model": "google_genai/gemini-1.5-flash", # Fast and free-tier friendly
    },
    "verbose": True,
    "headless": True
}

CACHE_FILE = "seen_jobs.json"

# Load cache
if os.path.exists(CACHE_FILE):
    with open(CACHE_FILE, "r") as f:
        seen_job_urls = set(json.load(f))
else:
    seen_job_urls = set()

urls = ["https://example.com/careers"] # List your URLs here
new_jobs = []

for url in urls:
    try:
        # Prompting Gemini to extract jobs
        smart_scraper_graph = SmartScraperGraph(
            prompt="List all Software Engineer job openings. Return JSON with 'title' and 'url'.",
            source=url,
            config=graph_config
        )
        results = smart_scraper_graph.run()
        
        # results might be a list or a dict depending on the page; handle accordingly
        if isinstance(results, list):
            for job in results:
                if job.get('url') and job['url'] not in seen_job_urls:
                    new_jobs.append(job)
                    seen_job_urls.add(job['url'])
    except Exception as e:
        print(f"Error at {url}: {e}")

# 2. Notification Logic
if new_jobs:
    resend.api_key = os.getenv("RESEND_API_KEY")
    
    html_body = "<h2>New Jobs Found</h2><ul>"
    for job in new_jobs:
        html_body += f"<li><a href='{job['url']}'>{job['title']}</a></li>"
    html_body += "</ul>"

    resend.Emails.send({
        "from": "JobBot <onboarding@resend.dev>",
        "to": ["your-email@example.com"], # Update this
        "subject": "New Job Alert (Gemini)",
        "html": html_body
    })

    # Save cache
    with open(CACHE_FILE, "w") as f:
        json.dump(list(seen_job_urls), f)
else:
    print("No new updates.")
