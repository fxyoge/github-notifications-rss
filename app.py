#!/usr/bin/env python3
"""
Simple GitHub notifications -> RSS bridge.

- Polls the GitHub REST API /notifications endpoint
- Filters threads by reason and repository
- Exposes a tiny HTTP endpoint that returns an RSS 2.0 feed

Configure via environment variables (see load_config()).
"""

import os
import sys
import logging
from datetime import datetime, timezone, timedelta
from email.utils import format_datetime

import requests
from flask import Flask, Response, jsonify


# -----------------------------------------------------------------------------
# Logging
# -----------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)

logger = logging.getLogger("github-notifications-rss")

app = Flask(__name__)


# -----------------------------------------------------------------------------
# Helper functions
# -----------------------------------------------------------------------------


def getenv_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "y", "on")


def getenv_list(name: str):
    value = os.getenv(name, "")
    if not value:
        return []
    return [v.strip() for v in value.split(",") if v.strip()]


def load_config():
    token = os.getenv("GITHUB_TOKEN")

    if not token:
        logger.warning(
            "GITHUB_TOKEN is not set. /feed will return 503 until you configure it."
        )

    cfg = {
        # GitHub API
        "token": token,
        "api_url": os.getenv("GITHUB_API_URL", "https://api.github.com").rstrip("/"),

        # Notification query behaviour
        "participating_only": getenv_bool("GITHUB_NOTIF_PARTICIPATING_ONLY", True),
        "include_read": getenv_bool("GITHUB_NOTIF_INCLUDE_READ", False),
        "per_page": int(os.getenv("GITHUB_NOTIF_PER_PAGE", "50")),  # max 50
        "max_pages": int(os.getenv("GITHUB_NOTIF_MAX_PAGES", "3")),

        # Filtering
        "include_reasons": set(getenv_list("GITHUB_NOTIF_REASONS_INCLUDE")),
        "exclude_reasons": set(getenv_list("GITHUB_NOTIF_REASONS_EXCLUDE")),
        "include_repos": set(getenv_list("GITHUB_NOTIF_REPOS_INCLUDE")),
        "exclude_repos": set(getenv_list("GITHUB_NOTIF_REPOS_EXCLUDE")),

        # RSS metadata
        "rss_title": os.getenv("RSS_TITLE", "GitHub notifications RSS"),
        "rss_link": os.getenv("RSS_LINK", "https://github.com/notifications"),
        "rss_description": os.getenv(
            "RSS_DESCRIPTION",
            "Custom feed built from your GitHub notifications",
        ),

        # Cache
        "cache_ttl_seconds": int(os.getenv("CACHE_TTL_SECONDS", "60")),
        # HTML descriptions
        "rss_html_description": getenv_bool("RSS_HTML_DESCRIPTION", True),
    }

    # Sanity bounds
    cfg["per_page"] = max(1, min(cfg["per_page"], 50))
    cfg["max_pages"] = max(1, cfg["max_pages"])
    cfg["cache_ttl_seconds"] = max(0, cfg["cache_ttl_seconds"])

    logger.info("Config loaded:")
    logger.info("  api_url = %s", cfg["api_url"])
    logger.info("  participating_only = %s", cfg["participating_only"])
    logger.info("  include_read = %s", cfg["include_read"])
    logger.info("  per_page = %d, max_pages = %d", cfg["per_page"], cfg["max_pages"])
    logger.info(
        "  include_reasons = %s, exclude_reasons = %s",
        cfg["include_reasons"] or "(none)",
        cfg["exclude_reasons"] or "(none)",
    )
    logger.info(
        "  include_repos = %s, exclude_repos = %s",
        cfg["include_repos"] or "(none)",
        cfg["exclude_repos"] or "(none)",
    )
    logger.info("  cache_ttl_seconds = %d", cfg["cache_ttl_seconds"])
    logger.info("  rss_html_description = %s", cfg["rss_html_description"])

    return cfg


CONFIG = load_config()


def github_headers():
    return {
        "Authorization": f"Bearer {CONFIG['token']}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "github-notifications-rss",
    }


# -----------------------------------------------------------------------------
# GitHub API + filtering
# -----------------------------------------------------------------------------


def fetch_notifications():
    """
    Fetch notifications from GitHub, following pagination up to max_pages.
    Raises requests.RequestException on network/HTTP problems.
    """
    url = f"{CONFIG['api_url']}/notifications"

    params = {
        "per_page": CONFIG["per_page"],
        "all": "true" if CONFIG["include_read"] else "false",
        "participating": "true" if CONFIG["participating_only"] else "false",
    }

    notifications = []
    page = 1

    while page <= CONFIG["max_pages"]:
        params["page"] = page
        logger.info("Requesting notifications page %d", page)

        resp = requests.get(
            url,
            headers=github_headers(),
            params=params,
            timeout=10,
        )

        if resp.status_code == 304:
            logger.info("GitHub returned 304 Not Modified")
            break

        resp.raise_for_status()

        page_items = resp.json()
        if not page_items:
            logger.info("No further notifications on page %d", page)
            break

        notifications.extend(page_items)

        link_header = resp.headers.get("Link", "")
        if 'rel="next"' not in link_header:
            break

        page += 1

    logger.info("Fetched %d notifications from GitHub", len(notifications))
    return notifications


def filter_notifications(notifications):
    """
    Apply simple in-Python filters:
      - include / exclude reasons
      - include / exclude repos (full_name: owner/repo)
    """
    inc_reasons = CONFIG["include_reasons"]
    exc_reasons = CONFIG["exclude_reasons"]
    inc_repos = CONFIG["include_repos"]
    exc_repos = CONFIG["exclude_repos"]

    filtered = []

    for n in notifications:
        reason = n.get("reason")
        repo = n.get("repository") or {}
        repo_full_name = repo.get("full_name")

        if inc_reasons and reason not in inc_reasons:
            continue
        if exc_reasons and reason in exc_reasons:
            continue

        if inc_repos and repo_full_name not in inc_repos:
            continue
        if exc_repos and repo_full_name in exc_repos:
            continue

        filtered.append(n)

    logger.info("Filtered down to %d notifications", len(filtered))
    return filtered


def subject_html_url(notification):
    """
    Try to turn the API subject URL into a human-facing HTML URL.

    Example:
      API:  https://api.github.com/repos/owner/repo/issues/123
      HTML: https://github.com/owner/repo/issues/123
    """
    subject = notification.get("subject") or {}
    api_url = subject.get("url") or ""
    repo = notification.get("repository") or {}
    repo_html = repo.get("html_url") or ""

    if api_url.startswith("https://api.github.com/repos/") and repo_html:
        rest = api_url.split("/repos/", 1)[1]
        parts = rest.split("/", 2)

        if len(parts) >= 3:
            tail = parts[2]
        else:
            tail = ""

        if tail.startswith("commits/"):
            sha = tail.split("/", 1)[1]
            return f"{repo_html}/commit/{sha}"
        elif tail:
            return f"{repo_html}/{tail}"

    if repo_html:
        return repo_html

    return CONFIG["rss_link"]


# -----------------------------------------------------------------------------
# RSS generation + caching
# -----------------------------------------------------------------------------


def build_rss(notifications):
    """
    Build an RSS 2.0 XML string from the notification list.

    If CONFIG['rss_html_description'] is True, descriptions will contain a small HTML block
    with tags and metadata. Otherwise a plain text description is used.
    """
    from xml.sax.saxutils import escape

    now = format_datetime(datetime.now(timezone.utc))
    use_html_description = CONFIG["rss_html_description"]

    def format_reason(reason):
        if not reason:
            return "other"
        mapping = {
            "mention": "mention",
            "author": "author",
            "assign": "assigned",
            "review_requested": "review requested",
            "approval_requested": "approval requested",
            "comment": "comment",
            "state_change": "state change",
            "subscribed": "subscribed",
            "ci_activity": "CI",
            "team_mention": "team mention",
            "security_alert": "security alert",
            "manual": "manual",
            "invitation": "invitation",
        }
        return mapping.get(reason, reason)

    def format_subject_type(subject_type):
        if not subject_type:
            return "Other"
        mapping = {
            "Issue": "Issue",
            "PullRequest": "Pull request",
            "Commit": "Commit",
            "Release": "Release",
        }
        return mapping.get(subject_type, subject_type)

    item_xml_chunks = []

    for n in notifications:
        subject = n.get("subject") or {}
        repo = n.get("repository") or {}

        repo_full_name = repo.get("full_name", "unknown/repo")
        subject_title = subject.get("title", "(no title)")
        subject_type_raw = subject.get("type")
        subject_type_label = format_subject_type(subject_type_raw)

        title = f"[{repo_full_name}] {subject_title}"
        link = subject_html_url(n)
        guid = n.get("id")

        updated_at = n.get("updated_at")
        if updated_at:
            try:
                dt = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
            except ValueError:
                dt = None
        else:
            dt = None

        pub_date = format_datetime(dt) if dt else now

        reason_raw = n.get("reason")
        reason_label = format_reason(reason_raw)
        unread = bool(n.get("unread"))
        unread_tag = "🔔" if unread else ""
        repo_html = repo.get("html_url")

        if use_html_description:
            html_desc = f"""
<p>
  <strong>[{escape(reason_label)}]</strong>
  <span>[{escape(subject_type_label)}]</span>
  <span>{escape(unread_tag)}</span>
</p>
<p>{escape(subject_title)}</p>
<p>
  <strong>Repo:</strong> {escape(repo_full_name)}<br>
  <strong>Reason:</strong> {escape(reason_raw or "unknown")}<br>
  <strong>Type:</strong> {escape(subject_type_label)}<br>
  <strong>Unread:</strong> {"yes" if unread else "no"}<br>
"""
            if repo_html:
                html_desc += f'  <strong>Repo link:</strong> <a href="{escape(repo_html)}">{escape(repo_full_name)}</a><br>\n'
            if dt:
                html_desc += f"  <strong>Updated:</strong> {escape(dt.isoformat())}<br>\n"

            html_desc += "</p>"

            description_content = html_desc
        else:
            tags = f"[{reason_label}] [{subject_type_label}]"
            if unread:
                tags += " 🔔"
            description_content = (
                f"{tags}\n"
                f"Title: {subject_title}\n"
                f"Repo: {repo_full_name}\n"
                f"Reason: {reason_raw or 'unknown'}\n"
                f"Type: {subject_type_label}\n"
                f"Unread: {'yes' if unread else 'no'}"
            )
            if repo_html:
                description_content += f"\nRepo link: {repo_html}"
            if dt:
                description_content += f"\nUpdated: {dt.isoformat()}"

        description_escaped = escape(description_content)

        item_xml = f"""
  <item>
    <title>{escape(title)}</title>
    <link>{escape(link)}</link>
    <guid isPermaLink="false">{escape(str(guid))}</guid>
    <pubDate>{pub_date}</pubDate>
    <description>{description_escaped}</description>
  </item>"""
        item_xml_chunks.append(item_xml)

    items_str = "\n".join(item_xml_chunks)

    channel_image = f"""
  <image>
    <url>https://github.githubassets.com/favicons/favicon.png</url>
    <title>{escape(CONFIG['rss_title'])}</title>
    <link>{escape(CONFIG['rss_link'])}</link>
  </image>
"""

    rss = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
<channel>
  <title>{CONFIG['rss_title']}</title>
  <link>{CONFIG['rss_link']}</link>
  <description>{CONFIG['rss_description']}</description>
  <lastBuildDate>{now}</lastBuildDate>{channel_image}
{items_str}
</channel>
</rss>
"""
    return rss


CACHE = {
    "rss": None,
    "last_fetch": None,   # datetime in UTC
    "last_error": None,   # str or None
}


def cache_expired():
    ttl = CONFIG["cache_ttl_seconds"]
    if ttl <= 0:
        return True
    if CACHE["last_fetch"] is None:
        return True
    return datetime.now(timezone.utc) - CACHE["last_fetch"] > timedelta(seconds=ttl)


def get_rss_with_cache():
    """
    Fetch RSS from GitHub with caching and error handling.

    - Uses in-memory cache to avoid hammering the API on every request.
    - On GitHub/network error, serves stale cache if available.
    """
    if not CONFIG["token"]:
        raise RuntimeError("GITHUB_TOKEN is not configured")

    if not cache_expired() and CACHE["rss"] is not None and CACHE["last_error"] is None:
        logger.info("Serving RSS from cache")
        return CACHE["rss"]

    logger.info("Cache expired or empty, querying GitHub")

    try:
        notifications = fetch_notifications()
        notifications = filter_notifications(notifications)
        rss_xml = build_rss(notifications)

        CACHE["rss"] = rss_xml
        CACHE["last_fetch"] = datetime.now(timezone.utc)
        CACHE["last_error"] = None

        return rss_xml

    except requests.RequestException as e:
        logger.error("GitHub API request failed: %s", e)
        CACHE["last_error"] = str(e)

        if CACHE["rss"] is not None:
            logger.warning("Serving stale RSS from cache due to error")
            return CACHE["rss"]

        raise

    except Exception as e:
        logger.exception("Unexpected error while generating RSS: %s", e)
        CACHE["last_error"] = str(e)

        if CACHE["rss"] is not None:
            logger.warning("Serving stale RSS from cache due to unexpected error")
            return CACHE["rss"]

        raise


# -----------------------------------------------------------------------------
# HTTP endpoints
# -----------------------------------------------------------------------------


@app.route("/")
@app.route("/feed")
def feed():
    if not CONFIG["token"]:
        return Response(
            "GITHUB_TOKEN is not configured on the server\n",
            status=503,
            mimetype="text/plain",
        )

    try:
        rss_xml = get_rss_with_cache()
        return Response(rss_xml, mimetype="application/rss+xml")
    except RuntimeError as e:
        logger.error("Runtime error in /feed: %s", e)
        return Response(str(e) + "\n", status=503, mimetype="text/plain")
    except requests.RequestException:
        return Response(
            "Failed to fetch notifications from GitHub\n",
            status=502,
            mimetype="text/plain",
        )
    except Exception:
        return Response(
            "Internal server error\n",
            status=500,
            mimetype="text/plain",
        )


@app.route("/health")
def health():
    last_fetch = CACHE["last_fetch"]
    last_error = CACHE["last_error"]

    if last_error is None:
        status = "ok"
    elif CACHE["rss"] is not None:
        status = "degraded"
    else:
        status = "error"

    return jsonify(
        {
            "status": status,
            "last_fetch": last_fetch.isoformat() if last_fetch else None,
            "last_error": last_error,
            "cache_ttl_seconds": CONFIG["cache_ttl_seconds"],
        }
    )


def main():
    host = os.getenv("BIND_ADDR", "0.0.0.0")
    port = int(os.getenv("BIND_PORT", "8000"))
    debug = getenv_bool("FLASK_DEBUG", False)
    logger.info("Starting server on %s:%d (debug=%s)", host, port, debug)
    app.run(host=host, port=port, debug=debug)


if __name__ == "__main__":
    main()

