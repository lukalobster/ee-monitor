# EuropeElects Facebook Monitor

A GitHub Actions workflow that automatically monitors the [EuropeElects Facebook page](https://www.facebook.com/EuropeElects/) every **10 minutes** and saves any new post (text + image) to this repository.

## How It Works

```
Every 10 minutes (GitHub Actions cron)
    │
    ▼
monitor.py runs
    │
    ├─ Fetches https://www.facebook.com/EuropeElects/ via scrape.do
    │   (JavaScript rendering + residential proxies)
    │
    ├─ Parses the latest post (text + image URL)
    │
    ├─ Computes a hash of the post text as a stable post ID
    │
    ├─ Compares with last_post_id.txt
    │   ├─ SAME  → exit, nothing to do
    │   └─ NEW   → download image, append to posts.md, update last_post_id.txt
    │
    └─ git commit & push (only if something changed)
```

## Repository Structure

```
.
├── .github/
│   └── workflows/
│       └── monitor.yml      ← GitHub Actions workflow (runs every 10 min)
├── images/                  ← Downloaded post images (auto-created)
├── monitor.py               ← Main scraping & comparison script
├── requirements.txt         ← Python dependencies
├── posts.md                 ← All captured posts (appended over time)
├── last_post_id.txt         ← ID of the last seen post (auto-managed)
└── README.md
```

## Setup Instructions

### 1. Fork or clone this repository

Create a new **private or public** GitHub repository and push these files to it.

### 2. Add your scrape.do API token as a secret

1. Go to your repository on GitHub.
2. Navigate to **Settings → Secrets and variables → Actions**.
3. Click **New repository secret**.
4. Name: `SCRAPEDO_TOKEN`
5. Value: your scrape.do API token (found in your [scrape.do dashboard](https://app.scrape.do/)).

### 3. Enable GitHub Actions

Go to the **Actions** tab of your repository and enable workflows if prompted.

### 4. (Optional) Trigger a manual run

Go to **Actions → EuropeElects Facebook Monitor → Run workflow** to test it immediately without waiting 10 minutes.

## Output Format

New posts are appended to `posts.md` in this format:

```markdown
## Post detected at 2026-04-01 08:00:00 UTC

**Source:** https://www.facebook.com/EuropeElects/posts/...

**Post ID (hash):** `a1b2c3d4e5f6g7h8`

### Text

Poland, United Surveys poll for WP.pl · 27–29 March 2026 · 1000 respondents
PiS: 35.5% …

### Image

![Post image](images/a1b2c3d4e5f6g7h8.jpg)

*Original URL:* https://scontent-iad3-2.xx.fbcdn.net/…

---
```

## Notes

- **GitHub Actions cron minimum**: GitHub's scheduler runs at most once per minute, so `*/10 * * * *` is the finest granularity available. In practice there may be a delay of a few minutes during peak times.
- **scrape.do credits**: Each run consumes one scrape.do API credit. At 6 runs/hour × 24 hours = **144 credits/day**. The free tier provides 1,000 credits/month; a paid plan is recommended for continuous monitoring.
- **Image storage**: Images are committed directly to the repository. For long-running monitors, consider moving images to an external storage (S3, etc.) to keep the repo size manageable.
- **Facebook anti-scraping**: Facebook actively tries to block scrapers. The `super=true` (residential proxy) option in scrape.do significantly improves reliability. If scraping fails, the workflow will log an error but will not crash — it simply tries again on the next run.
