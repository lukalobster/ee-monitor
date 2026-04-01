#!/usr/bin/env python3
"""
EuropeElects → Mastodon Poster
--------------------------------
Reads posts.md and last_post_id.txt (written by monitor.py) and, if a new
post has been detected since the last Mastodon publish, posts it to Mastodon
with its image (if any).

This script is COMPLETELY INDEPENDENT from monitor.py.
It does NOT modify monitor.py, posts.md, or last_post_id.txt.
It maintains its own state file: last_mastodon_post_id.txt

Environment variables required:
  MASTODON_INSTANCE_URL  – Base URL of your Mastodon instance
                           e.g. https://mastodon.social
  MASTODON_ACCESS_TOKEN  – Your bot account's access token
"""

import os
import re
import sys
import requests
from datetime import datetime, timezone

# ── Configuration ──────────────────────────────────────────────────────────────
POSTS_FILE              = "posts.md"
LAST_MONITOR_ID_FILE    = "last_post_id.txt"       # written by monitor.py (read-only here)
LAST_MASTODON_ID_FILE   = "last_mastodon_post_id.txt"  # our own state file
IMAGES_DIR              = "images"
MAX_TOOT_LENGTH         = 500   # Mastodon's character limit

# ── Timestamp helper ───────────────────────────────────────────────────────────

def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

# ── State helpers ──────────────────────────────────────────────────────────────

def load_last_mastodon_id() -> str:
    """Return the post_id of the last post we published to Mastodon."""
    if os.path.exists(LAST_MASTODON_ID_FILE):
        with open(LAST_MASTODON_ID_FILE, "r", encoding="utf-8") as f:
            return f.read().strip()
    return ""


def save_last_mastodon_id(post_id: str) -> None:
    with open(LAST_MASTODON_ID_FILE, "w", encoding="utf-8") as f:
        f.write(post_id)


def load_current_monitor_id() -> str:
    """Return the post_id that monitor.py last saved (read-only)."""
    if os.path.exists(LAST_MONITOR_ID_FILE):
        with open(LAST_MONITOR_ID_FILE, "r", encoding="utf-8") as f:
            return f.read().strip()
    return ""

# ── posts.md parser ────────────────────────────────────────────────────────────

def parse_latest_post() -> dict | None:
    """
    Parse posts.md and return the most recent post as a dict:
      {post_id, text, image_path, image_url, post_url, fetched_at}
    Returns None if the file doesn't exist or is empty.
    """
    if not os.path.exists(POSTS_FILE):
        print(f"[{_now()}] {POSTS_FILE} not found – nothing to post.")
        return None

    with open(POSTS_FILE, "r", encoding="utf-8") as f:
        content = f.read()

    if not content.strip():
        print(f"[{_now()}] {POSTS_FILE} is empty – nothing to post.")
        return None

    # Split into individual post blocks on the section heading
    blocks = re.split(r"(?=\n## Post detected at |\A## Post detected at )", content)
    blocks = [b.strip() for b in blocks if b.strip() and "## Post detected at" in b]

    if not blocks:
        print(f"[{_now()}] No post blocks found in {POSTS_FILE}.")
        return None

    # The first block is the most recent (posts.md is newest-first)
    block = blocks[0]

    post: dict = {
        "post_id":    "",
        "text":       "",
        "image_path": "",
        "image_url":  "",
        "post_url":   "",
        "fetched_at": "",
    }

    # ── fetched_at ────────────────────────────────────────────────────────────
    m = re.search(r"## Post detected at (.+)", block)
    if m:
        post["fetched_at"] = m.group(1).strip()

    # ── post_id ───────────────────────────────────────────────────────────────
    m = re.search(r"\*\*Post ID \(hash\):\*\* `([a-f0-9]+)`", block)
    if m:
        post["post_id"] = m.group(1).strip()

    # ── post_url ──────────────────────────────────────────────────────────────
    m = re.search(r"\*\*Source:\*\* \[([^\]]+)\]", block)
    if m:
        post["post_url"] = m.group(1).strip()

    # ── text ──────────────────────────────────────────────────────────────────
    # Text lives between "### Text" and the next "###" or "---"
    m = re.search(r"### Text\s*\n+(.*?)(?=\n###|\n---|\Z)", block, re.DOTALL)
    if m:
        post["text"] = m.group(1).strip()

    # ── image_path (local file) ───────────────────────────────────────────────
    m = re.search(r"!\[Post image\]\((images/[^\)]+)\)", block)
    if m:
        post["image_path"] = m.group(1).strip()

    # ── image_url (remote CDN URL as fallback) ────────────────────────────────
    m = re.search(r"\*Original URL:\* (https://[^\s\n]+)", block)
    if m:
        post["image_url"] = m.group(1).strip()

    return post if post["post_id"] else None

# ── Mastodon API helpers ───────────────────────────────────────────────────────

def _mastodon_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def upload_image_to_mastodon(instance_url: str, token: str,
                              image_path: str, image_url: str) -> str:
    """
    Upload an image to Mastodon and return the media attachment ID.
    Tries the local file first; falls back to downloading from the CDN URL.
    Returns "" on failure.
    """
    image_data: bytes | None = None
    filename = "post_image.jpg"
    mime_type = "image/jpeg"

    # ── Try local file first ──────────────────────────────────────────────────
    if image_path and os.path.exists(image_path):
        with open(image_path, "rb") as f:
            image_data = f.read()
        filename = os.path.basename(image_path)
        ext = filename.rsplit(".", 1)[-1].lower()
        mime_map = {"jpg": "image/jpeg", "jpeg": "image/jpeg",
                    "png": "image/png", "gif": "image/gif",
                    "webp": "image/webp"}
        mime_type = mime_map.get(ext, "image/jpeg")
        print(f"[{_now()}] Using local image: {image_path}")

    # ── Fall back to downloading from CDN ─────────────────────────────────────
    elif image_url:
        print(f"[{_now()}] Local image not found – downloading from CDN …")
        try:
            resp = requests.get(image_url, timeout=60, headers={
                "User-Agent": "Mozilla/5.0 (compatible; EuropeElectsBot/1.0)"
            })
            resp.raise_for_status()
            image_data = resp.content
            # Guess mime type from content-type header
            ct = resp.headers.get("content-type", "image/jpeg").split(";")[0].strip()
            mime_type = ct
            ext_map = {"image/jpeg": "jpg", "image/png": "png",
                       "image/gif": "gif", "image/webp": "webp"}
            filename = "post_image." + ext_map.get(ct, "jpg")
        except Exception as e:
            print(f"[{_now()}] WARNING: Could not download image: {e}", file=sys.stderr)
            return ""

    if not image_data:
        return ""

    # ── Upload to Mastodon /api/v2/media ──────────────────────────────────────
    upload_url = f"{instance_url.rstrip('/')}/api/v2/media"
    try:
        resp = requests.post(
            upload_url,
            headers=_mastodon_headers(token),
            files={"file": (filename, image_data, mime_type)},
            data={"description": "Poll chart from EuropeElects"},
            timeout=60,
        )
        resp.raise_for_status()
        media_id = resp.json().get("id", "")
        print(f"[{_now()}] Image uploaded to Mastodon – media ID: {media_id}")
        return media_id
    except Exception as e:
        print(f"[{_now()}] WARNING: Image upload failed: {e}", file=sys.stderr)
        return ""


def build_toot_text(post: dict) -> str:
    """
    Build the toot text from the post dict.
    Truncates to MAX_TOOT_LENGTH characters, appending the source URL if space allows.
    """
    text = post["text"]
    source = post["post_url"]

    # Append source link
    suffix = f"\n\n🔗 {source}" if source else ""
    suffix += "\n\n#EuropeElects #polls #Europe"

    full = text + suffix
    if len(full) <= MAX_TOOT_LENGTH:
        return full

    # Truncate text to fit within the limit
    available = MAX_TOOT_LENGTH - len(suffix) - 4  # 4 for " …\n\n"
    return text[:available].rstrip() + " …" + suffix


def post_to_mastodon(instance_url: str, token: str,
                     toot_text: str, media_id: str) -> bool:
    """
    Post a status to Mastodon. Returns True on success.
    """
    status_url = f"{instance_url.rstrip('/')}/api/v1/statuses"
    payload: dict = {"status": toot_text}
    if media_id:
        payload["media_ids[]"] = media_id

    try:
        resp = requests.post(
            status_url,
            headers=_mastodon_headers(token),
            data=payload,
            timeout=30,
        )
        resp.raise_for_status()
        toot_url = resp.json().get("url", "")
        print(f"[{_now()}] ✅ Toot posted successfully: {toot_url}")
        return True
    except Exception as e:
        print(f"[{_now()}] ERROR: Failed to post to Mastodon: {e}", file=sys.stderr)
        if hasattr(e, "response") and e.response is not None:
            print(f"[{_now()}] Response: {e.response.text}", file=sys.stderr)
        return False

# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    print(f"[{_now()}] ── EuropeElects Mastodon Poster starting ──")

    # ── Read environment variables ────────────────────────────────────────────
    instance_url = os.environ.get("MASTODON_INSTANCE_URL", "").rstrip("/")
    token        = os.environ.get("MASTODON_ACCESS_TOKEN", "")

    if not instance_url or not token:
        print("ERROR: MASTODON_INSTANCE_URL and MASTODON_ACCESS_TOKEN must be set.",
              file=sys.stderr)
        sys.exit(1)

    # ── Check if monitor.py has a new post ───────────────────────────────────
    current_monitor_id  = load_current_monitor_id()
    last_mastodon_id    = load_last_mastodon_id()

    print(f"[{_now()}] Monitor's latest post ID : {current_monitor_id or '(none)'}")
    print(f"[{_now()}] Last posted to Mastodon  : {last_mastodon_id  or '(none)'}")

    if not current_monitor_id:
        print(f"[{_now()}] No post recorded by monitor yet – nothing to do.")
        sys.exit(0)

    if current_monitor_id == last_mastodon_id:
        print(f"[{_now()}] Already posted this post to Mastodon – nothing to do.")
        sys.exit(0)

    # ── Parse the latest post from posts.md ───────────────────────────────────
    post = parse_latest_post()
    if not post:
        print(f"[{_now()}] Could not parse a post from {POSTS_FILE} – exiting.")
        sys.exit(0)

    print(f"[{_now()}] New post to publish: {post['post_id']}")
    print(f"[{_now()}] Text preview: {post['text'][:100]!r}")

    # ── Upload image (if available) ───────────────────────────────────────────
    media_id = ""
    if post["image_path"] or post["image_url"]:
        media_id = upload_image_to_mastodon(
            instance_url, token, post["image_path"], post["image_url"]
        )

    # ── Build and post the toot ───────────────────────────────────────────────
    toot_text = build_toot_text(post)
    print(f"[{_now()}] Toot ({len(toot_text)} chars):\n{toot_text}\n")

    success = post_to_mastodon(instance_url, token, toot_text, media_id)

    if success:
        save_last_mastodon_id(post["post_id"])
        print(f"[{_now()}] State saved → last_mastodon_post_id.txt")
    else:
        print(f"[{_now()}] Post failed – state NOT updated (will retry next run).")
        sys.exit(1)

    print(f"[{_now()}] ── Done ──")


if __name__ == "__main__":
    main()
