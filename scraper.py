import os
import resend
from scrapegraphai.graphs import SmartScraperGraph

# 1. Setup Configuration
graph_config = {
    "llm": {
        "api_key": os.getenv("OPENAI_API_KEY"),
        "model": "gpt-4o-mini", # Efficient and cheap
    },
    "verbose": True,
}

# 2. Define the Scraping Task
# You can loop through a list of URLs here
urls = ["https://example.com/careers", "https://another-site.io/jobs"]
target_keyword = "Software Engineer"

all_jobs = []

for url in urls:
    smart_scraper_graph = SmartScraperGraph(
        prompt=f"List all {target_keyword} job openings. Return a list of objects with 'title' and 'url'.",
        source=url,
        config=graph_config
    )
    
    result = smart_scraper_graph.run()
    if result:
        all_jobs.extend(result)

# 3. Send Email via Resend if jobs found
if all_jobs:
    resend.api_key = os.getenv("RESEND_API_KEY")
    
    html_content = "<h1>New Job Matches Found!</h1><ul>"
    for job in all_jobs:
        html_content += f"<li><a href='{job['url']}'>{job['title']}</a></li>"
    html_content += "</ul>"

    resend.Emails.send({
        "from": "JobBot <onboarding@resend.dev>",
        "to": ["your-email@example.com"],
        "subject": f"Daily Job Alert: {target_keyword}",
        "html": html_content
    })
