# instagram-mcp

A small self-hosted [MCP](https://modelcontextprotocol.io) server that lets
Claude Code (or any other MCP client) publish photos to your **Instagram
Professional account** and the **linked Facebook Page** through the Meta
Graph API.

Conversational use looks like:

> "Post `https://cdn.example.com/sunset.jpg` to Instagram with the caption
> *'Evening over the Alps.'*"

## Endpoints used

Verified against `developers.facebook.com` (Graph API **v25.0**, latest stable
as of 2026-02-18):

| Step | Endpoint |
| ---- | -------- |
| Create IG media container | `POST /{ig-user-id}/media` (`image_url`, `caption`, optional `alt_text`) |
| Poll container status     | `GET /{container-id}?fields=status_code` — wait for `FINISHED` |
| Publish IG container      | `POST /{ig-user-id}/media_publish?creation_id=…` |
| Check publishing quota    | `GET /{ig-user-id}/content_publishing_limit` (50 posts / 24 h) |
| Post FB Page photo        | `POST /{page-id}/photos` (`url`, `message`, `published`) |
| Introspect token          | `GET /debug_token?input_token=…` |

## Tools exposed over MCP

| Tool | Purpose |
| ---- | ------- |
| `post_instagram_photo(image_url, caption, alt_text="")` | Runs the full IG two-step flow, polls until `FINISHED`, publishes. Validates the URL is reachable and `image/*` first. |
| `post_facebook_photo(image_url, caption, published=True)` | Single POST to the Page's `/photos` edge. |
| `post_instagram_local_photo(filename, caption, alt_text="")` | Like `post_instagram_photo` but takes a filename from `data/images/`; the file is briefly served via the nginx sidecar so Meta can fetch it. Moves to `data/posted/YYYY-MM-DD/` on success. |
| `post_facebook_local_photo(filename, caption, published=True)` | Uploads a file from `data/images/` directly via multipart to the Page (no public URL needed). |
| `list_pending_images()` | Lists candidates in `data/images/` so the model knows which filenames are postable. |
| `check_token_validity()` | Calls `/debug_token`, returns scopes, type, expiry (Unix + ISO), valid flag, and the configured Graph API version. |

Errors from Meta are surfaced verbatim — `code`, `error_subcode`, `message`,
`fbtrace_id`, and the user-facing message if present — instead of being
swallowed.

## Required scopes

You're using the classic Facebook-Login → linked-Page → IG path (the right
choice when the IG account is paired with a Page you own). Request these on
the user access token when generating it in the Graph API Explorer:

- `pages_show_list` — needed so the server can derive a Page Access Token
  from `/me/accounts`. Without it, **every** posting call fails.
- `pages_read_engagement`
- `pages_manage_posts` — required for all FB Page publishing
  (`post_facebook_photo`, page photo upload, page feed, page stories).
- `instagram_basic`
- `instagram_content_publish` — required for every IG publish call
  (feed photo, carousel, story).
- `publish_to_groups` — only if you use `FB_GROUP_IDS` / cross-post to groups.

> The 2025 scope rename to `instagram_business_*` applies to the *Instagram
> API with Instagram Login* flow, not this Facebook-Login flow.

### Token model used by the server

`META_ACCESS_TOKEN` is the long-lived **User** token. On the first publish
call the server transparently fetches the **Page Access Token** for
`FB_PAGE_ID` via `GET /me/accounts` (needs `pages_show_list`), caches it
in-memory for the lifetime of the request, and uses it for:

- every IG endpoint (`/{ig-user-id}/media`, `media_publish`,
  `content_publishing_limit`, list)
- every FB Page endpoint (`/{page-id}/photos`, `/feed`, `/photo_stories`,
  list)

Group posts (`/{group-id}/photos`) still use the user token because
`publish_to_groups` is a user-level scope.

`check_token_validity` now introspects **both** tokens and tells you whether
the page token is derivable at all — use it first when debugging permission
errors.

### Meta App Dashboard / OAuth setup checklist

A code-side fix only routes the right token to the right endpoint. The
following must be done manually in the Meta App Dashboard / when generating
the token; the server cannot do it for you:

1. **App type** — Meta App must be type **Business** with the
   *Instagram Graph API* and *Facebook Login for Business* products added.
2. **IG ↔ Page link** — the IG Professional account must be linked to the
   FB Page in the Page's Linked Accounts settings. Without that link IG
   publishing returns `(#10)`.
3. **App Review** — `instagram_content_publish`, `pages_manage_posts` and
   `publish_to_groups` require Meta App Review for non-admins. During
   development you can use any admin/developer/tester user of the app
   without review.
4. **App mode** — to publish on behalf of users who are *not* admins/devs/
   testers, switch the app from *Development* to *Live* (Settings → Basic).
   While in Development mode, the configured `META_ACCESS_TOKEN` must
   belong to such a privileged user.
5. **Token generation** — in Graph API Explorer pick your app, select the
   user, request the scopes listed above, then exchange the short-lived
   token for a long-lived one (curl below). Long-lived user tokens last
   ~60 days; the derived page token does not expire as long as the parent
   user token is valid.

## One-time Meta setup

1. Create a Meta App at <https://developers.facebook.com/apps/> of type
   **Business**.
2. Add the **Facebook Login for Business** and **Instagram Graph API**
   products to the app.
3. In your Facebook Page settings, link it to the Instagram Professional
   account you want to publish to.
4. Use the Graph API Explorer (or your own OAuth flow) to obtain a short-lived
   user access token granting the scopes listed above.
5. Exchange it for a long-lived (~60 day) token:

   ```bash
   curl -G "https://graph.facebook.com/v25.0/oauth/access_token" \
     -d grant_type=fb_exchange_token \
     -d client_id="$META_APP_ID" \
     -d client_secret="$META_APP_SECRET" \
     -d fb_exchange_token="$SHORT_LIVED_TOKEN"
   ```

6. Discover your IDs:

   ```bash
   # Facebook Page id + linked Instagram Business Account id
   curl -G "https://graph.facebook.com/v25.0/me/accounts" \
     -d fields="id,name,instagram_business_account" \
     -d access_token="$LONG_LIVED_TOKEN"
   ```

7. Put the values into `.env` (copy `.env.example`).

## Running

### Directly with Python

```bash
cp .env.example .env
# edit .env

python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python server.py
```

The server speaks MCP over stdio — it's meant to be launched by an MCP
client, not run standalone for long.

### With Docker (preferred on the homelab)

```bash
cp .env.example .env
# edit .env

docker compose build
# the image is now available as instagram-mcp:latest
# Claude Code will invoke `docker run` directly (see below) — you don't
# usually `docker compose up` this service.
```

## Transports

The server supports three MCP transports, picked by `MCP_TRANSPORT` in `.env`:

| Mode | When | URL / how the client reaches it |
| --- | --- | --- |
| `stdio` | Local Claude Code spawns the container per call | JSON-RPC over the process' stdin/stdout — no network |
| `streamable-http` (default) | Persistent container, GUI clients like Claude Desktop | `http://<host>:${MCP_PORT}/mcp` |
| `sse` | Same as above, legacy transport | `http://<host>:${MCP_PORT}/sse` |

The persistent HTTP service runs on `0.0.0.0:${MCP_PORT}` (default 3224) so any
LAN client can reach it. There is **no built-in auth** — restrict access at the
network layer or put Caddy with a bearer-token check in front if you expose it
beyond the LAN.

### Adding via the Claude Desktop GUI

Settings → Developer → Custom Connectors → *Add custom connector* → paste:

```
http://<homelab-lan-ip-or-name>:3224/mcp
```

Some Claude Desktop versions reject plain `http://` URLs and require HTTPS.
If yours does, terminate TLS with Caddy in front and use that URL instead.

## Registering with Claude Code

Drop one of these into your project's `.mcp.json` (or `~/.claude.json`
globally):

**Plain Python:**

```json
{
  "mcpServers": {
    "instagram": {
      "command": "python",
      "args": ["/opt/social/server.py"],
      "env": {}
    }
  }
}
```

**Docker:**

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

Or register from the CLI:

```bash
claude mcp add instagram -- docker run --rm -i --env-file /opt/social/.env instagram-mcp:latest
```

Then ask Claude Code something like:

> "Use the instagram tool to post `https://…/foo.jpg` with caption `Hello`."

## Local-folder workflow

For posting without first uploading the image anywhere external:

```
data/
├── images/                 # drop files here
├── public/                 # transient: nginx serves <token>/<filename>
└── posted/YYYY-MM-DD/      # archive after successful publish
```

1. Copy a JPEG/PNG into `/opt/social/data/images/`.
2. Ask Claude: *"List my pending images, then post `sunset.jpg` to Instagram
   with the caption 'Evening over the Alps.'"*
3. The MCP server hardlinks the file into `data/public/<random-token>/sunset.jpg`,
   nginx serves it at `https://social.example.com/<token>/sunset.jpg`, Meta
   fetches it, the token directory is removed, the original is moved to
   `data/posted/YYYY-MM-DD/sunset.jpg`.

**Facebook** doesn't need the public URL — `post_facebook_local_photo`
uploads the bytes directly via multipart `source`.

### Reverse-proxy snippet (Caddy)

Caddy runs on a remote VPS and reaches this host over Headscale, so the nginx
sidecar binds on `0.0.0.0:${FILE_SERVER_PORT}` (default 3223). Add to the
remote Caddyfile and reload — replace `<homelab-tailscale-name>` with the
host's name on your tailnet:

```caddy
social.example.com {
    reverse_proxy <homelab-tailscale-name>:3223
}
```

The sidecar speaks plain HTTP; Caddy handles the public TLS cert.

### Bringing the stack up

```bash
docker compose -f /opt/social/docker-compose.yml up -d --build instagram-mcp-files
# the MCP container itself is invoked by Claude Code per call (stdio), not
# kept running — only the nginx sidecar runs continuously.
```

## Operational notes

- **URL reachability** — Meta fetches the image server-side, so `image_url`
  must be publicly accessible HTTPS and serve `Content-Type: image/*`. The
  server HEAD-checks the URL before each publish and refuses if it can't be
  reached.
- **Rate limit** — Before each IG publish, the server reads
  `/content_publishing_limit` and refuses if `quota_usage >= 50`. The quota
  is per 24 h rolling window, per IG account.
- **Container polling** — Polls every `IG_CONTAINER_POLL_SECONDS` (default
  3 s) up to `IG_CONTAINER_TIMEOUT_SECONDS` (default 90 s). If the container
  transitions to `ERROR` or `EXPIRED` the tool returns a structured error.
- **Token lifetime** — Long-lived user tokens last ~60 days. Use
  `check_token_validity` to monitor; refresh by re-exchanging through
  `oauth/access_token` before expiry. Page access tokens derived from a
  long-lived user token do not expire as long as the user token stays valid.
- **Logs** go to stderr; stdout is reserved for MCP JSON-RPC framing.
