import os
import json
import resend
from scrapegraphai.graphs import SmartScraperGraph

CACHE_FILE = "seen_jobs.json"

# 1. Load seen jobs from the cache file
if os.path.exists(CACHE_FILE):
    with open(CACHE_FILE, "r") as f:
        seen_job_urls = set(json.load(f))
else:
    seen_job_urls = set()

# 2. Setup ScrapeGraphAI
graph_config = {
    "llm": {
        "api_key": os.getenv("OPENAI_API_KEY"),
        "model": "gpt-4o-mini",
    }
}

urls = ["https://example.com/careers"] # Add your URLs here
new_jobs = []

for url in urls:
    try:
        smart_scraper_graph = SmartScraperGraph(
            prompt="List all Software Engineer job openings. Return a list of objects with 'title' and 'url'.",
            source=url,
            config=graph_config
        )
        results = smart_scraper_graph.run()
        
        # 3. Filter only NEW jobs
        for job in results:
            if job['url'] not in seen_job_urls:
                new_jobs.append(job)
                seen_job_urls.add(job['url'])
    except Exception as e:
        print(f"Error scraping {url}: {e}")

# 4. Notify via Email only if there are NEW jobs
if new_jobs:
    resend.api_key = os.getenv("RESEND_API_KEY")
    
    html_body = "<h3>New Job Postings:</h3><ul>"
    for job in new_jobs:
        html_body += f"<li><a href='{job['url']}'>{job['title']}</a></li>"
    html_body += "</ul>"

    resend.Emails.send({
        "from": "JobBot <onboarding@resend.dev>",
        "to": ["your-email@example.com"],
        "subject": "New Job Alerts Found",
        "html": html_body
    })

    # 5. Save the updated list back to disk for the GitHub Cache step
    with open(CACHE_FILE, "w") as f:
        json.dump(list(seen_job_urls), f)
else:
    print("No new jobs found. Skipping email.")
