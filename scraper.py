import os
import json
import resend
from scrapegraphai.graphs import SmartScraperGraph

# 1. Load configuration from the separate file
with open("config.json", "r") as f:
    config = json.load(f)

CACHE_FILE = "seen_jobs.json"
seen_job_urls = set()

if os.path.exists(CACHE_FILE):
    try:
        # Check if file has content before trying to load
        if os.path.getsize(CACHE_FILE) > 0:
            with open(CACHE_FILE, "r") as f:
                content = json.load(f)
                # Ensure the content is a list before converting to set
                if isinstance(content, list):
                    seen_job_urls = set(content)
        else:
            print("Cache file is empty. Starting fresh.")
    except (json.JSONDecodeError, ValueError) as e:
        print(f"Cache corrupted ({e}). Resetting seen_jobs.")

# Gemini Config
graph_config = {
    "llm": {
        "api_key": os.getenv("GEMINI_API_KEY"),
        "model": "google_genai/gemini-1.5-flash",
        "model_tokens": 1000000, # Gemini 1.5 Flash supports up to 1M tokens
    },
    "headless": True
}

new_jobs = []

# 2. Iterate through all URLs and Keywords
for url in config["urls"]:
    for keyword in config["keywords"]:
        try:
            print(f"Searching for {keyword} at {url}...")
            
            prompt = f"List all {keyword} job openings. Return a list of objects with 'title' and 'url'."
            
            smart_scraper_graph = SmartScraperGraph(
                prompt=prompt,
                source=url,
                config=graph_config
            )
            
            results = smart_scraper_graph.run()
            
            if isinstance(results, list):
                for job in results:
                    job_url = job.get('url')
                    if job_url and job_url not in seen_job_urls:
                        # Add metadata so you know which keyword/site it came from
                        job['source_site'] = url
                        new_jobs.append(job)
                        seen_job_urls.add(job_url)
        except Exception as e:
            print(f"Error scraping {url}: {e}")

# 3. Notification Logic
if new_jobs:
    resend.api_key = os.getenv("RESEND_API_KEY")
    
    html_body = f"<h2>New Job Matches Found ({len(new_jobs)})</h2>"
    for job in new_jobs:
        html_body += f"""
        <div style="margin-bottom: 15px; border-left: 4px solid #4CAF50; padding-left: 10px;">
            <p><strong>{job['title']}</strong></p>
            <p>Source: {job['source_site']}</p>
            <a href="{job['url']}">View Job Posting</a>
        </div>
        <hr>
        """

    resend.Emails.send({
        "from": "JobBot <onboarding@resend.dev>",
        "to": [config["receiver_email"]],
        "subject": f"Alert: {len(new_jobs)} New Jobs Found",
        "html": html_body
    })

    # Save updated cache
    with open(CACHE_FILE, "w") as f:
        json.dump(list(seen_job_urls), f)
    print("Email sent successfully.")
else:
    print("No new jobs found this cycle.")
