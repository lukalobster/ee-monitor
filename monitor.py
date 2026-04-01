#!/usr/bin/env python3
"""
EuropeElects Facebook Page Monitor
-----------------------------------
Fetches the latest post (text + image) from the EuropeElects Facebook page.

Strategy:
  1. Primary  – scrape.do with JS rendering (full desktop Facebook)
  2. Fallback – scrape.do fetching mbasic.facebook.com (lightweight mobile HTML)

Every run:
  - Fetches the latest post from the page
  - Compares it with the last saved post (stored in last_post_id.txt)
  - If new: appends the post to posts.md and downloads the image
  - If same: does nothing and exits cleanly

Environment variables required:
  SCRAPEDO_TOKEN  - Your scrape.do API token
"""

import os
import sys
import hashlib
import requests
from datetime import datetime, timezone
from bs4 import BeautifulSoup

# ── Configuration ──────────────────────────────────────────────────────────────
FACEBOOK_PAGE_URL       = "https://www.facebook.com/EuropeElects/"
FACEBOOK_MBASIC_URL     = "https://mbasic.facebook.com/EuropeElects/"
SCRAPEDO_API            = "https://api.scrape.do/"
POSTS_FILE              = "posts.md"
LAST_ID_FILE            = "last_post_id.txt"
IMAGES_DIR              = "images"

# ── Timestamp helper ───────────────────────────────────────────────────────────

def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

# ── scrape.do fetcher ──────────────────────────────────────────────────────────

def _scrapedo_get(url: str, token: str, render: bool = False,
                  extra_params: dict | None = None) -> str:
    """Make a request through the scrape.do API and return the response text."""
    params: dict = {
        "token":  token,
        "url":    url,
    }
    if render:
        params.update({
            "render":         "true",
            "super":          "true",   # residential proxies
            "customWait":     "5000",   # wait 5 s for JS to settle
            "blockResources": "false",  # allow images
            "device":         "desktop",
            "waitUntil":      "networkidle2",
        })
    else:
        params.update({
            "super": "true",            # residential proxies even for plain HTML
        })
    if extra_params:
        params.update(extra_params)

    resp = requests.get(SCRAPEDO_API, params=params, timeout=120)
    resp.raise_for_status()
    return resp.text


# ── Parsing helpers ────────────────────────────────────────────────────────────

def _text_from_element(el) -> str:
    """Return clean text from a BeautifulSoup element."""
    return el.get_text(separator="\n", strip=True)


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


def _build_post_dict(text: str, image_url: str | None,
                     post_url: str | None) -> dict:
    """Build a normalised post dictionary with a stable content-hash ID."""
    clean_text = (text or "").strip()
    post_id = hashlib.sha256(clean_text.encode()).hexdigest()[:16]
    return {
        "post_id":    post_id,
        "text":       clean_text,
        "image_url":  (image_url or "").strip(),
        "post_url":   (post_url  or "").strip(),
        "fetched_at": _now(),
    }


# ── Strategy 1: full desktop Facebook ─────────────────────────────────────────

def parse_desktop_facebook(html: str) -> dict | None:
    """
    Parse HTML from the full desktop Facebook page.
    Tries multiple selectors in order of reliability.
    """
    soup = BeautifulSoup(html, "lxml")

    # ── A. role="article" divs ──────────────────────────────────────────────
    for container in soup.find_all(attrs={"role": "article"}):
        result = _extract_from_container(container)
        if result and result["text"]:
            return result

    # ── B. <article> tags ───────────────────────────────────────────────────
    for container in soup.find_all("article"):
        result = _extract_from_container(container)
        if result and result["text"]:
            return result

    # ── C. Open Graph meta fallback ─────────────────────────────────────────
    og_desc  = soup.find("meta", property="og:description")
    og_image = soup.find("meta", property="og:image")
    og_url   = soup.find("meta", property="og:url")
    if og_desc and og_desc.get("content"):
        text      = og_desc["content"]
        image_url = og_image["content"] if og_image else None
        post_url  = og_url["content"]   if og_url  else None
        return _build_post_dict(text=text, image_url=image_url, post_url=post_url)

    return None


def _extract_from_container(container) -> dict | None:
    """Extract post text, image, and URL from a post container element."""
    # ── Text ────────────────────────────────────────────────────────────────
    text_chunks: list[str] = []
    for tag in container.find_all(["p", "span", "div"]):
        # Skip deeply nested containers to avoid duplication
        if tag.find(["p", "span", "div"]):
            continue
        t = tag.get_text(separator=" ", strip=True)
        if len(t) > 15:
            text_chunks.append(t)
    text = "\n".join(_dedupe(text_chunks)[:15])

    if not text:
        return None

    # ── Image ────────────────────────────────────────────────────────────────
    image_url = None
    for img in container.find_all("img"):
        src = img.get("src", "")
        if (src
                and "fbcdn.net" in src
                and "emoji" not in src
                and "safe_image" not in src):
            image_url = src
            break

    # ── Post URL ─────────────────────────────────────────────────────────────
    post_url = None
    for a in container.find_all("a", href=True):
        href = a["href"]
        if any(k in href for k in ["/posts/", "story_fbid", "/permalink/"]):
            # Strip tracking parameters
            post_url = href.split("?")[0]
            if not post_url.startswith("http"):
                post_url = "https://www.facebook.com" + post_url
            break

    return _build_post_dict(text=text, image_url=image_url, post_url=post_url)


# ── Strategy 2: mbasic (lightweight mobile HTML) ──────────────────────────────

def parse_mbasic_facebook(html: str) -> dict | None:
    """
    Parse HTML from mbasic.facebook.com – a simple, JS-free version of Facebook.
    Much easier to parse reliably.
    """
    soup = BeautifulSoup(html, "lxml")

    # mbasic posts are inside <div id="recent"> or <div id="structured_composer_async_container">
    # Each story is typically a <div> with an <abbr> timestamp and a <p> for text.

    # ── Find all story blocks ────────────────────────────────────────────────
    # mbasic wraps each post in a div that contains an <abbr> (timestamp) and text
    stories = []

    # Method A: look for divs containing <abbr> (timestamps)
    for abbr in soup.find_all("abbr"):
        parent = abbr.find_parent("div")
        if parent:
            stories.append(parent)

    # Method B: look for <article> or role="article"
    if not stories:
        stories = soup.find_all("article") or soup.find_all(attrs={"role": "article"})

    for story in stories:
        text_chunks: list[str] = []
        for p in story.find_all(["p", "span"]):
            t = p.get_text(separator=" ", strip=True)
            if len(t) > 10:
                text_chunks.append(t)
        text = "\n".join(_dedupe(text_chunks)[:10])

        if not text:
            continue

        # Image
        image_url = None
        for img in story.find_all("img"):
            src = img.get("src", "")
            if src and ("fbcdn" in src or "facebook" in src) and "emoji" not in src:
                image_url = src
                break

        # Post URL
        post_url = None
        for a in story.find_all("a", href=True):
            href = a["href"]
            if any(k in href for k in ["/posts/", "story_fbid", "/permalink/"]):
                post_url = href.split("?")[0]
                if not post_url.startswith("http"):
                    post_url = "https://www.facebook.com" + post_url
                break

        post = _build_post_dict(text=text, image_url=image_url, post_url=post_url)
        if post["text"]:
            return post

    return None


# ── File I/O ───────────────────────────────────────────────────────────────────

def load_last_post_id() -> str:
    """Return the ID of the last saved post, or empty string if none."""
    if os.path.exists(LAST_ID_FILE):
        with open(LAST_ID_FILE, "r", encoding="utf-8") as f:
            return f.read().strip()
    return ""


def save_last_post_id(post_id: str) -> None:
    with open(LAST_ID_FILE, "w", encoding="utf-8") as f:
        f.write(post_id)


def download_image(image_url: str, post_id: str) -> str:
    """
    Download the post image and save it to IMAGES_DIR.
    Returns the local file path, or empty string on failure.
    """
    if not image_url:
        return ""
    os.makedirs(IMAGES_DIR, exist_ok=True)

    # Determine extension from URL
    ext = "jpg"
    for candidate in [".png", ".jpg", ".jpeg", ".webp", ".gif"]:
        if candidate in image_url.lower().split("?")[0]:
            ext = candidate.lstrip(".")
            break

    filename = os.path.join(IMAGES_DIR, f"{post_id}.{ext}")
    try:
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            )
        }
        resp = requests.get(image_url, headers=headers, timeout=60)
        resp.raise_for_status()
        with open(filename, "wb") as f:
            f.write(resp.content)
        print(f"[{_now()}] Image saved → {filename}")
        return filename
    except Exception as e:
        print(f"[{_now()}] WARNING: Could not download image: {e}", file=sys.stderr)
        return ""


def append_post_to_file(post: dict, image_path: str) -> None:
    """Append the new post as a Markdown section to POSTS_FILE."""
    with open(POSTS_FILE, "a", encoding="utf-8") as f:
        f.write(f"\n## Post detected at {post['fetched_at']}\n\n")
        if post["post_url"]:
            f.write(f"**Source:** [{post['post_url']}]({post['post_url']})\n\n")
        f.write(f"**Post ID (hash):** `{post['post_id']}`\n\n")
        f.write("### Text\n\n")
        f.write(post["text"] + "\n\n")
        if image_path:
            f.write("### Image\n\n")
            f.write(f"![Post image]({image_path})\n\n")
            f.write(f"*Original URL:* {post['image_url']}\n\n")
        elif post["image_url"]:
            f.write("### Image\n\n")
            f.write(f"![Post image]({post['image_url']})\n\n")
        f.write("\n---\n")
    print(f"[{_now()}] Post appended to {POSTS_FILE}")


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    print(f"[{_now()}] ── EuropeElects Facebook Monitor starting ──")

    token = os.environ.get("SCRAPEDO_TOKEN", "")
    if not token:
        print("ERROR: SCRAPEDO_TOKEN environment variable is not set.", file=sys.stderr)
        sys.exit(1)

    post: dict | None = None

    # ── Strategy 1: full desktop Facebook with JS rendering ──────────────────
    print(f"[{_now()}] Strategy 1: fetching full desktop Facebook via scrape.do …")
    try:
        html = _scrapedo_get(FACEBOOK_PAGE_URL, token, render=True)
        print(f"[{_now()}] Received {len(html):,} bytes")
        post = parse_desktop_facebook(html)
        if post:
            print(f"[{_now()}] Strategy 1 succeeded.")
        else:
            print(f"[{_now()}] Strategy 1: no post found in HTML.")
    except Exception as e:
        print(f"[{_now()}] Strategy 1 failed: {e}", file=sys.stderr)

    # ── Strategy 2: mbasic (lightweight, no JS required) ─────────────────────
    if not post:
        print(f"[{_now()}] Strategy 2: fetching mbasic.facebook.com via scrape.do …")
        try:
            html = _scrapedo_get(FACEBOOK_MBASIC_URL, token, render=False)
            print(f"[{_now()}] Received {len(html):,} bytes")
            post = parse_mbasic_facebook(html)
            if post:
                print(f"[{_now()}] Strategy 2 succeeded.")
            else:
                print(f"[{_now()}] Strategy 2: no post found in HTML.")
        except Exception as e:
            print(f"[{_now()}] Strategy 2 failed: {e}", file=sys.stderr)

    if not post:
        print(f"[{_now()}] All strategies failed – exiting without changes.")
        # Exit 0 so GitHub Actions doesn't mark the run as failed for a transient issue
        sys.exit(0)

    print(f"[{_now()}] Latest post ID : {post['post_id']}")
    print(f"[{_now()}] Post text (100): {post['text'][:100]!r}")

    # ── Compare with last saved post ──────────────────────────────────────────
    last_id = load_last_post_id()
    if post["post_id"] == last_id:
        print(f"[{_now()}] Post unchanged – nothing to do.")
        sys.exit(0)

    print(f"[{_now()}] New post detected (previous: {last_id or 'none'}) – saving …")

    # ── Download image ────────────────────────────────────────────────────────
    image_path = download_image(post["image_url"], post["post_id"])

    # ── Append to posts file ──────────────────────────────────────────────────
    append_post_to_file(post, image_path)

    # ── Update last post ID ───────────────────────────────────────────────────
    save_last_post_id(post["post_id"])

    print(f"[{_now()}] ── Done ──")


if __name__ == "__main__":
    main()
