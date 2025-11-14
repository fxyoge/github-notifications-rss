# GitHub Notifications RSS

This is a small service that turns your GitHub notifications into an RSS feed.

You give it a GitHub personal access token.  
It calls the `/notifications` API, filters the data, and serves an RSS 2.0 feed that your reader can subscribe to.

## What it does

- Fetches GitHub notifications using the official API
- Can limit to threads where you are actually involved (`participating_only`)
- Can filter by reason (mention, assign, state_change, ci_activity, ...)
- Can include or exclude specific repositories
- Caches results for a short time so it does not hammer the GitHub API
- Exposes two endpoints:
  - `/feed` for the RSS feed
  - `/health` for a simple JSON status

Descriptions can be HTML or plain text, depending on config.

## Example feed item

In a typical RSS reader a single item might look like this:

**Title**

`[timkicker/podliner] Fix MPV logging path on Linux`

**Body**

```bash
[mention] [Pull request] 🔔
Fix MPV logging path on Linux

Repo: timkicker/podliner
Reason: mention
Type: Pull request
Unread: yes
Repo link: https://github.com/timkicker/podliner
Updated: 2025-11-14T06:56:00+00:00
```

## Quick start with Docker

Clone the repo and copy the example env file:

```bash
cp .env.example .env
```

Edit `.env` and set at least:

```bash
GITHUB_TOKEN=ghp_your_token_here
```

Then build and start:

```bash
docker compose up --build -d
```

If your compose file maps port `8083:8000`, the feed is available at:

- Feed: `http://localhost:8083/feed`
- Health: `http://localhost:8083/health`

Add `http://localhost:8083/feed` to your RSS reader and you are done.

## Token and scopes

You need a GitHub Personal Access Token (classic).

- Only public repositories:
  - `public_repo` and `read:user` are usually enough
- With private repositories:
  - include `repo`

## Basic configuration

Most options are set through environment variables. There is a `.env.example` with all of them. The most useful ones:

```bash
# GitHub access
GITHUB_TOKEN=ghp_your_token_here
GITHUB_API_URL=https://api.github.com

# Query behaviour
GITHUB_NOTIF_PARTICIPATING_ONLY=true
GITHUB_NOTIF_INCLUDE_READ=false

# Optional filters
GITHUB_NOTIF_REASONS_INCLUDE=
GITHUB_NOTIF_REASONS_EXCLUDE=subscribed,ci_activity
GITHUB_NOTIF_REPOS_INCLUDE=
GITHUB_NOTIF_REPOS_EXCLUDE=

# RSS output
RSS_TITLE=GitHub notifications RSS
RSS_LINK=https://github.com/notifications
RSS_DESCRIPTION=Custom feed built from your GitHub notifications
RSS_HTML_DESCRIPTION=true

# Cache
CACHE_TTL_SECONDS=60

# Server
BIND_ADDR=0.0.0.0
BIND_PORT=8000
```

You can adjust this later when you know what kind of notifications you want to see or hide. For many setups the defaults should be fine.

## Status endpoint

The `/health` endpoint returns a small JSON payload, for example:

```json
{
  "status": "ok",
  "last_fetch": "2025-11-14T06:56:00+00:00",
  "last_error": null,
  "cache_ttl_seconds": 60
}
```

- `ok` means the last fetch worked
- `degraded` means GitHub failed but an older cached feed is still served
- `error` means there is no valid cache and the last fetch failed

## License

This project is licensed under the MIT License. See `LICENSE` for details.
