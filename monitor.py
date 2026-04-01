#!/usr/bin/env python3
"""
EuropeElects Facebook Page Monitor
-----------------------------------
Fetches the latest post (text + image) from the EuropeElects Facebook page.

Strategy:
  Step 1 – Fetch the page feed via scrape.do (forced English, residential proxy)
            to discover the latest post URL and image.
  Step 2 – Fetch the individual post URL directly to get the full post text
            (the feed only shows a truncated snippet behind a "See more" button).
  Fallback – mbasic.facebook.com if the desktop page fails.

Every run:
  - Fetches the latest post
  - Compares with last_post_id.txt
  - If new: saves text + image to posts.md, commits via GitHub Actions
  - If same: exits cleanly

Environment variables required:
  SCRAPEDO_TOKEN  – Your scrape.do API token
"""

import os
import sys
import hashlib
import requests
from datetime import datetime, timezone
from bs4 import BeautifulSoup

# ── Configuration ──────────────────────────────────────────────────────────────
# Force English locale via the ?locale= parameter and hl= cookie
FACEBOOK_PAGE_URL   = "https://www.facebook.com/EuropeElects/?locale=en_US"
FACEBOOK_MBASIC_URL = "https://mbasic.facebook.com/EuropeElects/?locale=en_US"
SCRAPEDO_API        = "https://api.scrape.do/"
POSTS_FILE          = "posts.md"
LAST_ID_FILE        = "last_post_id.txt"
IMAGES_DIR          = "images"

# UI strings to filter out (in multiple languages) – these are never post content
UI_NOISE = {
    # English
    "like", "comment", "share", "follow", "all reactions", "see more",
    "public", "verified account", "shared with", "news & media website",
    "poll aggregation", "europe elects", "followers", "following",
    "privacy", "terms", "advertising", "cookies", "more",
    # Spanish (exact phrases that appear in the scraped HTML)
    "me gusta", "comentar", "compartir", "seguir", "todas las reacciones",
    "ver más", "público", "cuenta verificada", "compartido con",
    "compartido con: público", "todas las reacciones:",
    # French
    "j'aime", "commenter", "partager", "réactions",
    # German
    "gefällt mir", "kommentieren", "teilen",
    # Italian
    "mi piace", "commenta", "condividi",
}

# Regex patterns for noise that varies slightly (e.g. "39K followers", "1.2M followers")
import re as _re
_NOISE_PATTERNS = [
    _re.compile(r"^\d[\d.,kmKM]* followers?$", _re.I),
    _re.compile(r"^\d[\d.,kmKM]* following$", _re.I),
    _re.compile(r"^compartido con:", _re.I),
    _re.compile(r"^todas las reacciones", _re.I),
    _re.compile(r"^shared with:", _re.I),
    _re.compile(r"^all reactions", _re.I),
]

# ── Timestamp helper ───────────────────────────────────────────────────────────

def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

# ── scrape.do fetcher ──────────────────────────────────────────────────────────

def _scrapedo_get(url: str, token: str, render: bool = True,
                  extra_wait: int = 5000) -> str:
    """Fetch a URL through scrape.do and return the HTML."""
    params: dict = {
        "token":          token,
        "url":            url,
        "super":          "true",       # residential proxies
        "geoCode":        "us",         # US IP → English Facebook
        "setCookies":     "locale=en_US; wd=1920x1080",
    }
    if render:
        params.update({
            "render":         "true",
            "customWait":     str(extra_wait),
            "blockResources": "false",
            "device":         "desktop",
            "waitUntil":      "networkidle2",
        })
    resp = requests.get(SCRAPEDO_API, params=params, timeout=120)
    resp.raise_for_status()
    return resp.text

# ── Text cleaning ──────────────────────────────────────────────────────────────

def _is_noise(text: str) -> bool:
    """Return True if the text chunk is a UI label rather than post content."""
    t = text.strip().lower()
    raw = text.strip()
    if len(t) < 4:
        return True
    if t in UI_NOISE:
        return True
    # Check regex patterns (handles "39K followers", "Compartido con: Público", etc.)
    for pat in _NOISE_PATTERNS:
        if pat.search(raw):
            return True
    # Numeric-only strings (reaction counts, follower counts, etc.)
    if t.replace(",", "").replace(".", "").isdigit():
        return True
    # Very short fragments that are clearly not poll data
    if len(t) < 12 and not any(c.isdigit() for c in t):
        return True
    return False


def _clean_text(chunks: list[str]) -> str:
    """Deduplicate and filter noise from a list of text chunks."""
    seen: set[str] = set()
    out: list[str] = []
    for chunk in chunks:
        c = chunk.strip()
        if not c:
            continue
        if _is_noise(c):
            continue
        if c in seen:
            continue
        seen.add(c)
        out.append(c)
    return "\n".join(out)

# ── Parse the page feed (to get post URL + image) ─────────────────────────────

def parse_feed_for_post_url_and_image(html: str) -> tuple[str, str]:
    """
    Scan the page feed HTML and return (post_url, image_url) for the first post.
    Returns ("", "") if nothing found.
    """
    soup = BeautifulSoup(html, "lxml")

    post_url  = ""
    image_url = ""

    # Look in role="article" containers first, then <article> tags
    containers = soup.find_all(attrs={"role": "article"}) or soup.find_all("article")

    for container in containers:
        # ── Post URL ──────────────────────────────────────────────────────────
        if not post_url:
            for a in container.find_all("a", href=True):
                href = a["href"]
                if any(k in href for k in ["/posts/", "story_fbid", "/permalink/"]):
                    post_url = href.split("?")[0]
                    if not post_url.startswith("http"):
                        post_url = "https://www.facebook.com" + post_url
                    break

        # ── Image ─────────────────────────────────────────────────────────────
        if not image_url:
            for img in container.find_all("img"):
                src = img.get("src", "")
                if (src
                        and "fbcdn.net" in src
                        and "emoji"   not in src
                        and "safe_image" not in src
                        and len(src) > 50):
                    image_url = src
                    break

        if post_url and image_url:
            break

    # OG fallback for image
    if not image_url:
        og_img = soup.find("meta", property="og:image")
        if og_img:
            image_url = og_img.get("content", "")

    # OG fallback for post URL
    if not post_url:
        og_url = soup.find("meta", property="og:url")
        if og_url:
            post_url = og_url.get("content", "")

    return post_url, image_url

# ── Fetch full text from the individual post page ─────────────────────────────

def fetch_post_text(post_url: str, token: str) -> str:
    """
    Fetch the individual post page and extract the full post text.
    This bypasses the "See more" truncation on the feed page.
    """
    if not post_url:
        return ""

    # Append locale to force English
    url = post_url + ("&" if "?" in post_url else "?") + "locale=en_US"

    print(f"[{_now()}] Fetching individual post for full text: {post_url}")
    try:
        html = _scrapedo_get(url, token, render=True, extra_wait=4000)
    except Exception as e:
        print(f"[{_now()}] WARNING: Could not fetch post page: {e}", file=sys.stderr)
        return ""

    soup = BeautifulSoup(html, "lxml")

    # ── Strategy A: data-ad-comet-preview="message" or data-testid="post_message"
    for attr in [
        {"data-ad-comet-preview": "message"},
        {"data-testid": "post_message"},
        {"data-ad-preview": "message"},
    ]:
        el = soup.find(attrs=attr)
        if el:
            text = el.get_text(separator="\n", strip=True)
            if len(text) > 20:
                return text

    # ── Strategy B: look for the largest text block inside role="article"
    containers = soup.find_all(attrs={"role": "article"}) or soup.find_all("article")
    best_text = ""
    for container in containers:
        chunks: list[str] = []
        for tag in container.find_all(["p", "span", "div"]):
            if tag.find(["p", "span", "div"]):
                continue  # skip parent containers, only leaf nodes
            t = tag.get_text(separator=" ", strip=True)
            if t:
                chunks.append(t)
        candidate = _clean_text(chunks)
        if len(candidate) > len(best_text):
            best_text = candidate

    if best_text:
        return best_text

    # ── Strategy C: OG description (always present, may be truncated)
    og_desc = soup.find("meta", property="og:description")
    if og_desc:
        return og_desc.get("content", "")

    return ""

# ── mbasic fallback ────────────────────────────────────────────────────────────

def fetch_via_mbasic(token: str) -> dict | None:
    """
    Fetch mbasic.facebook.com (no JS needed) and return a post dict.
    Used as a fallback when the desktop page fails entirely.
    """
    print(f"[{_now()}] Fallback: fetching mbasic.facebook.com …")
    try:
        html = _scrapedo_get(FACEBOOK_MBASIC_URL, token, render=False)
    except Exception as e:
        print(f"[{_now()}] mbasic fetch failed: {e}", file=sys.stderr)
        return None

    soup = BeautifulSoup(html, "lxml")

    # mbasic wraps each story in a div containing an <abbr> timestamp
    for abbr in soup.find_all("abbr"):
        story = abbr.find_parent("div")
        if not story:
            continue

        chunks: list[str] = []
        for tag in story.find_all(["p", "span"]):
            t = tag.get_text(separator=" ", strip=True)
            if t:
                chunks.append(t)
        text = _clean_text(chunks)
        if not text:
            continue

        image_url = ""
        for img in story.find_all("img"):
            src = img.get("src", "")
            if src and "fbcdn" in src and "emoji" not in src:
                image_url = src
                break

        post_url = ""
        for a in story.find_all("a", href=True):
            href = a["href"]
            if any(k in href for k in ["/posts/", "story_fbid", "/permalink/"]):
                post_url = href.split("?")[0]
                if not post_url.startswith("http"):
                    post_url = "https://www.facebook.com" + post_url
                break

        return _build_post_dict(text=text, image_url=image_url, post_url=post_url)

    return None

# ── Post dict builder ──────────────────────────────────────────────────────────

def _build_post_dict(text: str, image_url: str, post_url: str) -> dict:
    clean = (text or "").strip()
    post_id = hashlib.sha256(clean.encode()).hexdigest()[:16]
    return {
        "post_id":    post_id,
        "text":       clean,
        "image_url":  (image_url or "").strip(),
        "post_url":   (post_url  or "").strip(),
        "fetched_at": _now(),
    }

# ── File I/O ───────────────────────────────────────────────────────────────────

def load_last_post_id() -> str:
    if os.path.exists(LAST_ID_FILE):
        with open(LAST_ID_FILE, "r", encoding="utf-8") as f:
            return f.read().strip()
    return ""


def save_last_post_id(post_id: str) -> None:
    with open(LAST_ID_FILE, "w", encoding="utf-8") as f:
        f.write(post_id)


def download_image(image_url: str, post_id: str) -> str:
    if not image_url:
        return ""
    os.makedirs(IMAGES_DIR, exist_ok=True)
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

    # ── Step 1: Fetch the page feed ───────────────────────────────────────────
    print(f"[{_now()}] Step 1: fetching page feed …")
    try:
        feed_html = _scrapedo_get(FACEBOOK_PAGE_URL, token, render=True, extra_wait=5000)
        print(f"[{_now()}] Feed HTML: {len(feed_html):,} bytes")

        post_url, image_url = parse_feed_for_post_url_and_image(feed_html)
        print(f"[{_now()}] Post URL  : {post_url or '(not found)'}")
        print(f"[{_now()}] Image URL : {(image_url[:80] + '…') if image_url else '(not found)'}")

        # ── Step 2: Fetch the individual post for full text ───────────────────
        text = ""
        if post_url:
            text = fetch_post_text(post_url, token)

        # If we still have no text, try OG description from the feed page
        if not text:
            soup = BeautifulSoup(feed_html, "lxml")
            og_desc = soup.find("meta", property="og:description")
            if og_desc:
                text = og_desc.get("content", "")

        if text or image_url:
            post = _build_post_dict(text=text, image_url=image_url, post_url=post_url)
            print(f"[{_now()}] Text preview: {post['text'][:120]!r}")
        else:
            print(f"[{_now()}] No content found from desktop feed.")

    except Exception as e:
        print(f"[{_now()}] Desktop strategy failed: {e}", file=sys.stderr)

    # ── Fallback: mbasic ──────────────────────────────────────────────────────
    if not post or not post.get("text"):
        mbasic_post = fetch_via_mbasic(token)
        if mbasic_post and mbasic_post.get("text"):
            # If we already have an image from the desktop fetch, keep it
            if post and post.get("image_url") and not mbasic_post.get("image_url"):
                mbasic_post["image_url"] = post["image_url"]
                mbasic_post["post_url"]  = post.get("post_url", mbasic_post["post_url"])
                # Recompute ID with the new text
                mbasic_post["post_id"] = hashlib.sha256(
                    mbasic_post["text"].encode()
                ).hexdigest()[:16]
            post = mbasic_post
            print(f"[{_now()}] mbasic text preview: {post['text'][:120]!r}")

    if not post or (not post.get("text") and not post.get("image_url")):
        print(f"[{_now()}] All strategies failed – exiting without changes.")
        sys.exit(0)

    # ── Compare with last saved post ──────────────────────────────────────────
    last_id = load_last_post_id()
    if post["post_id"] == last_id:
        print(f"[{_now()}] Post unchanged – nothing to do.")
        sys.exit(0)

    print(f"[{_now()}] New post detected (previous: {last_id or 'none'}) – saving …")

    image_path = download_image(post["image_url"], post["post_id"])
    append_post_to_file(post, image_path)
    save_last_post_id(post["post_id"])

    print(f"[{_now()}] ── Done ──")


if __name__ == "__main__":
    main()
