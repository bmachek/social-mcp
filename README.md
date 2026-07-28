# social-mcp

[![ko-fi](https://ko-fi.com/img/githubbutton_sm.svg)](https://ko-fi.com/W7X2240HF4)

A self-hosted MCP server that gives Claude the ability to manage your Instagram and Facebook Page — post photos, carousels, and Reels, schedule posts, pull engagement analytics, and run an autopilot that plans and queues two weeks of content from your photo inbox in one go.

You drop files into a folder, ask Claude what to post and when, and it handles the rest: staging, uploading, archiving, and scheduling. Everything runs on your own machine; no third-party service touches your tokens or your media.

---

## What you can do with it

**Post immediately**
- Photo to Instagram feed + Story
- Photo to Facebook Page feed + Story
- Both platforms at once (one command, file archived once)
- Carousel (2–10 photos) to IG, FB, or both
- Reel / video to IG, FB, or both
- Cross-post to Facebook Groups

**Schedule for later**
- Any of the above, at a specific time or relative (`in 2h`, `tomorrow at 19:30`)
- Persistent scheduler survives container restarts

**Analytics**
- Account-level reach, engagement, followers (IG and FB)
- Per-post insights — likes, comments, shares, saves, plays
- Top-performing posts ranked by engagement (useful as caption examples)

**Autopilot**
1. `autopilot_plan` — scans your inbox, deduplicates burst shots by perceptual hash, clusters photos taken close together in time and place into carousels, and proposes a two-week posting schedule
2. You review the plan and write captions
3. `autopilot_commit` — validates and queues everything at once

---

## How it works

The server implements the [Model Context Protocol](https://modelcontextprotocol.io) (MCP), which is the standard way to give AI assistants access to external tools. Claude Code (or Claude Desktop) connects to it and gains a set of posting/scheduling/analytics tools backed by the Meta Graph API.

For local files, an nginx sidecar briefly serves each image at a one-shot public URL so Meta's servers can fetch it; the URL is torn down and the file is archived once the post goes through. Facebook uploads go direct — no public URL needed.

```
Your photo inbox          MCP server               Meta Graph API
  data/images/    →   stage → publish    →   Instagram / Facebook Page
                      ↓ on success
                  data/posted/YYYY-MM-DD/
```

---

## Setup

See **[SETUP.md](SETUP.md)** for the full step-by-step guide (Meta App creation, token generation, `.env` configuration, Docker Compose, reverse proxy).

Quick summary:
1. Create a Meta App (type: Business) and add Facebook Login for Business + Instagram Graph API.
2. Generate a long-lived User Access Token with the scopes below.
3. Copy `.env.example` → `.env` and fill in your token, Page ID, and IG User ID.
4. `docker compose up -d --build`
5. Register the server with your MCP client (Claude Code or Claude Desktop).

### Required token scopes

| Scope | Required for |
|---|---|
| `pages_show_list` | Everything — used to derive the Page Access Token |
| `pages_read_engagement` | Analytics |
| `pages_manage_posts` | FB Page publishing |
| `instagram_basic` | IG account info |
| `instagram_content_publish` | IG publishing |
| `publish_to_groups` | FB Group cross-posting (optional) |

---

## Running

### Docker Compose (recommended)

```bash
cp .env.example .env
# edit .env

docker compose up -d --build
```

Two containers start:
- `instagram-mcp` — MCP server on `MCP_PORT` (default 3224)
- `instagram-mcp-files` — nginx sidecar on `FILE_SERVER_PORT` (default 3223) for image staging

### Plain Python

```bash
cp .env.example .env
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python server.py
```

---

## Connecting an MCP client

### Claude Code

```bash
claude mcp add instagram -- docker run --rm -i \
  --env-file /opt/social/.env \
  instagram-mcp:latest
```

Or in `.mcp.json`:

```json
{
  "mcpServers": {
    "instagram": {
      "command": "docker",
      "args": ["run", "--rm", "-i", "--env-file", "/opt/social/.env", "instagram-mcp:latest"]
    }
  }
}
```

### Claude Desktop

Settings → Developer → Custom Connectors → Add:

```
http://<host>:3224/mcp
```

---

## MCP transports

| `MCP_TRANSPORT` | Use when | Client URL |
|---|---|---|
| `streamable-http` (default) | Persistent container, Claude Desktop, LAN clients | `http://<host>:3224/mcp` |
| `sse` | Same, legacy transport | `http://<host>:3224/sse` |
| `stdio` | Claude Code spawns container per call | stdio (no network) |

No built-in auth — restrict at the network layer or put a TLS-terminating proxy with a bearer-token check in front if you expose beyond the LAN.

---

## File layout

```
data/
├── images/          # drop photos and videos here before posting
├── public/          # transient: nginx serves staged files here
├── posted/          # archived after successful publish, by date
│   └── YYYY-MM-DD/
└── scheduler/       # APScheduler SQLite jobstore
```

---

## Tool reference

### Posting

| Tool | What it does |
|---|---|
| `post_instagram_photo(image_url, caption, alt_text)` | IG feed photo from a public URL |
| `post_facebook_photo(image_url, caption)` | FB Page photo from a public URL |
| `post_instagram_local_photo(filename, caption, alt_text, also_story)` | IG feed photo from inbox; stages, posts, archives |
| `post_facebook_local_photo(filename, caption, also_story, group_ids)` | FB Page photo from inbox; direct multipart upload |
| `post_local_photo_dual(filename, caption, alt_text, also_story_ig, also_story_fb)` | Both platforms in one job |
| `post_instagram_local_carousel(filenames, caption)` | IG carousel (2–10 photos) |
| `post_facebook_local_carousel(filenames, caption)` | FB multi-photo post (2–10 photos) |
| `post_local_carousel_dual(filenames, caption)` | Carousel to both platforms |
| `post_instagram_reel(video_url, caption, share_to_feed)` | IG Reel from a public URL |
| `post_instagram_local_reel(filename, caption, share_to_feed)` | IG Reel from inbox |
| `post_facebook_video(video_url, caption)` | FB Page video from a public URL |
| `post_facebook_local_video(filename, caption)` | FB Page video from inbox |
| `post_facebook_local_reel(filename, description)` | FB Page Reel from inbox |
| `post_local_reel_dual(filename, caption, share_to_feed)` | Reel to both platforms |

### Scheduling

| Tool | What it does |
|---|---|
| `schedule_instagram_local_photo(filename, when, caption, also_story)` | Queue an IG photo post |
| `schedule_facebook_local_photo(filename, when, caption, also_story)` | Queue a FB photo post |
| `schedule_instagram_local_carousel(filenames, when, caption)` | Queue an IG carousel |
| `schedule_facebook_local_carousel(filenames, when, caption)` | Queue a FB carousel |
| `schedule_instagram_local_reel(filename, when, caption, share_to_feed)` | Queue an IG Reel |
| `schedule_facebook_local_video(filename, when, caption)` | Queue a FB video post |
| `schedule_facebook_local_reel(filename, when, description)` | Queue a FB Reel |
| `list_scheduled_posts()` | Show pending jobs |
| `cancel_scheduled_post(job_id)` | Remove a pending job |

`when` accepts `in 2h`, `in 1d`, or an ISO timestamp (Europe/Berlin if naive).

### Inbox

| Tool | What it does |
|---|---|
| `list_pending_images()` | List postable photos in `data/images/` |
| `list_pending_videos()` | List postable videos in `data/images/` |
| `show_image(filename, max_side)` | Render an inbox image inline so Claude can see it |

### Analytics

| Tool | What it does |
|---|---|
| `get_instagram_account_insights(metric, period, since_days)` | Account-level IG metrics (reach, engagement, followers) |
| `get_instagram_post_insights(media_id, metric)` | Per-post IG metrics |
| `get_facebook_page_insights(metric, period, since_days)` | Page-level FB metrics |
| `get_facebook_post_insights(post_id, metric)` | Per-post FB metrics |
| `top_performing_posts(platform, limit, top_n)` | Rank recent posts by engagement |
| `list_recent_instagram_posts(limit)` | Raw IG media list |
| `list_recent_facebook_posts(limit)` | Raw FB Page post list |

### Autopilot

| Tool | What it does |
|---|---|
| `autopilot_plan(days_ahead, max_posts, slots_per_day, …)` | Analyse inbox, dedup, cluster into carousels, propose a schedule |
| `autopilot_commit(plan)` | Validate and queue the filled-in plan |

### Utility

| Tool | What it does |
|---|---|
| `check_token_validity()` | Inspect token scopes, type, and expiry |

---

## Meta API notes

- **Graph API version:** v25.0 (latest stable as of 2026-02-18). Set via `GRAPH_API_VERSION` in `.env`.
- **IG rate limit:** 50 posts per 24-hour rolling window. The server checks before each publish and refuses if the quota is full.
- **Container polling:** IG requires a two-step create → publish flow with a status poll in between. Photos time out after 90 s; Reels after 600 s (both configurable in `.env`).
- **Token lifetime:** Long-lived user tokens last ~60 days. Use `check_token_validity` to monitor, and re-exchange before expiry. See SETUP.md for the renewal curl command.
- **Errors:** Meta errors are surfaced verbatim — `code`, `error_subcode`, `message`, `fbtrace_id`, and the user-facing message — rather than being swallowed.

---

## Reverse proxy (Caddy)

The nginx sidecar speaks plain HTTP. If Caddy runs on a remote VPS and reaches this host over Tailscale/Headscale:

```caddy
social.example.com {
    reverse_proxy <homelab-tailscale-name>:3223
}
```

Set `PUBLIC_BASE_URL=https://social.example.com` in `.env`.
