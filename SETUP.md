# Setup guide

This guide walks you through everything you need to do once before you can start posting. It takes about 20–30 minutes.

## Prerequisites

- A **Facebook Page** you admin (Business or Creator type).
- An **Instagram Professional account** (Business or Creator) linked to that Page.
  If not linked yet: Instagram app → Profile → Edit profile → Page → choose your Page.
- A server or machine that can run Docker Compose.
- A publicly reachable HTTPS URL for the image-staging sidecar (needed for IG posts from local files). Caddy is the assumed reverse proxy; adapt if you use something else.

---

## Step 1 — Create a Meta App

1. Go to <https://developers.facebook.com/apps/> and click **Create App**.
2. Choose **Other** → **Business** as the app type.
3. Give it a name (e.g. `social-mcp`) and click **Create**.
4. On the app dashboard, click **Add Product** and add both:
   - **Facebook Login for Business**
   - **Instagram Graph API**

---

## Step 2 — Note your App ID and App Secret

In the app dashboard: **Settings → Basic**.

Copy **App ID** and **App Secret** — you'll need them in `.env` as `META_APP_ID` and `META_APP_SECRET`.

---

## Step 3 — Generate a long-lived User Access Token

### 3a. Get a short-lived token in Graph API Explorer

1. Open <https://developers.facebook.com/tools/explorer/>.
2. Select your app from the **Meta App** drop-down (top right).
3. Click **Generate Access Token**.
4. In the permissions dialog, enable **all of these**:

   | Permission | Why |
   |---|---|
   | `pages_show_list` | Lets the server derive a Page Access Token — without this every call fails |
   | `pages_read_engagement` | Required by insights endpoints |
   | `pages_manage_posts` | FB Page publishing (photos, videos, stories, reels) |
   | `instagram_basic` | Read IG account info |
   | `instagram_content_publish` | Publish to IG feed, carousel, reels |
   | `publish_to_groups` | Only if you want cross-posting to FB Groups |

5. Click **Generate Token** and accept the permission dialogs.
6. Copy the short-lived token shown in the top field.

### 3b. Exchange it for a long-lived (~60 day) token

```bash
export META_APP_ID="your-app-id"
export META_APP_SECRET="your-app-secret"
export SHORT_LIVED_TOKEN="the-token-from-explorer"

curl -G "https://graph.facebook.com/v25.0/oauth/access_token" \
  -d grant_type=fb_exchange_token \
  -d client_id="$META_APP_ID" \
  -d client_secret="$META_APP_SECRET" \
  -d fb_exchange_token="$SHORT_LIVED_TOKEN"
```

The response contains `access_token` — that's your `META_ACCESS_TOKEN`.

---

## Step 4 — Discover your Page ID and Instagram User ID

```bash
export LONG_LIVED_TOKEN="the-long-lived-token-from-step-3b"

curl -G "https://graph.facebook.com/v25.0/me/accounts" \
  -d fields="id,name,instagram_business_account" \
  -d access_token="$LONG_LIVED_TOKEN"
```

The response looks like:

```json
{
  "data": [{
    "id": "111122223333",
    "name": "My Page",
    "instagram_business_account": {
      "id": "444455556666"
    }
  }]
}
```

- `id` (on the Page object) → `FB_PAGE_ID`
- `instagram_business_account.id` → `IG_USER_ID`

---

## Step 5 — Configure `.env`

```bash
cp .env.example .env
$EDITOR .env
```

Fill in at minimum:

```
META_ACCESS_TOKEN=   # long-lived token from step 3b
IG_USER_ID=          # from step 4
FB_PAGE_ID=          # from step 4
META_APP_ID=         # from step 2
META_APP_SECRET=     # from step 2
PUBLIC_BASE_URL=     # e.g. https://social.example.com  (see step 6)
```

---

## Step 6 — Set up the public image URL (for IG local-file posts)

Instagram fetches images from a public HTTPS URL. The docker-compose stack includes an nginx sidecar (`instagram-mcp-files`) that serves staged files on a local port. You need to put a reverse proxy in front of it.

**Caddy example** — add to your Caddyfile:

```caddy
social.example.com {
    reverse_proxy <homelab-host>:3223
}
```

Replace `<homelab-host>` with the hostname or IP visible from your Caddy instance (use a Tailscale/Headscale hostname if Caddy is on a remote VPS).

Set `PUBLIC_BASE_URL=https://social.example.com` in `.env`.

> Facebook direct-upload tools (`post_facebook_local_photo`, `post_facebook_local_video`, `post_facebook_local_reel`) **don't** need this URL — they upload the bytes directly. Only Instagram and dual-platform tools need it.

---

## Step 7 — Create the data directory structure

```bash
mkdir -p data/images data/public data/posted data/scheduler
```

Drop photos/videos into `data/images/` when you want to post them.

---

## Step 8 — Build and start the containers

```bash
docker compose up -d --build
```

This starts:
- `instagram-mcp` — the MCP server on port `3224` (HTTP/streamable-http by default)
- `instagram-mcp-files` — the nginx sidecar on port `3223`

Verify both are up:

```bash
docker compose ps
docker compose logs instagram-mcp
```

---

## Step 9 — Verify the token

Use the MCP tool or run a quick curl against the `/debug_token` endpoint:

```bash
curl -G "https://graph.facebook.com/v25.0/debug_token" \
  -d input_token="$META_ACCESS_TOKEN" \
  -d access_token="${META_APP_ID}|${META_APP_SECRET}"
```

Or, once connected to the MCP server, call `check_token_validity()` — it returns scopes, expiry, and whether the Page token is derivable.

---

## Step 10 — Connect an MCP client

### Claude Code (CLI)

```bash
claude mcp add instagram -- docker run --rm -i \
  --env-file /opt/social/.env \
  instagram-mcp:latest
```

Or add to your project's `.mcp.json`:

```json
{
  "mcpServers": {
    "instagram": {
      "command": "docker",
      "args": [
        "run", "--rm", "-i",
        "--env-file", "/opt/social/.env",
        "instagram-mcp:latest"
      ]
    }
  }
}
```

### Claude Desktop (GUI)

Settings → Developer → Custom Connectors → Add:

```
http://<homelab-host>:3224/mcp
```

---

## Token renewal (every ~60 days)

Long-lived user tokens expire after roughly 60 days. Re-run step 3b with a fresh short-lived token (from the Graph API Explorer) and update `META_ACCESS_TOKEN` in `.env`, then restart:

```bash
docker compose restart instagram-mcp
```

Set a calendar reminder ~55 days after each renewal. You can check how much time is left at any point with `check_token_validity()`.

---

## App Review (for non-developer users)

While your Meta App is in **Development mode**, only users who are admins, developers, or testers of the app can use it. For personal use this is fine — you're already an admin.

If you ever want to publish on behalf of other users, you'll need to submit the relevant permissions (`instagram_content_publish`, `pages_manage_posts`) for App Review and switch the app to **Live** mode (Settings → Basic → App Mode).

---

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| Every call fails with permission error | `pages_show_list` missing from token — regenerate with that scope |
| IG publish returns `(#10)` | IG account not linked to the FB Page — fix in Instagram app settings |
| `PUBLIC_BASE_URL` staging fails | Nginx sidecar not running, or Caddy not proxying to port 3223 |
| Token expired | Renew via step 3b and restart the container |
| Container polling times out for Reels | Increase `IG_REEL_CONTAINER_TIMEOUT_SECONDS` in `.env` (default 600 s) |
