"""Read-only diagnostic for the configured META_ACCESS_TOKEN / FB_PAGE_ID.

Run inside the same env as the MCP server (it consumes /opt/social/.env via
python-dotenv) so it sees the exact same token and IDs:

    docker exec instagram-mcp python /app/diagnose.py
or  python /opt/social/diagnose.py   (if your venv is active)

Performs only GETs against the Graph API:
  - /debug_token            (token introspection)
  - /me/accounts            (paginated walk of all pages the user admins)
  - /{page-id}              (IG link lookup)

Nothing is posted.
"""

from __future__ import annotations

import asyncio
import os
import sys

from dotenv import load_dotenv

from meta_client import MetaAPIError, MetaClient


load_dotenv()


REQUIRED_SCOPES = [
    "pages_show_list",
    "pages_manage_posts",
    "pages_read_engagement",
    "instagram_basic",
    "instagram_content_publish",
]


def _env(name: str) -> str:
    v = os.getenv(name, "")
    if not v:
        print(f"!! env var {name} is empty — diagnostic cannot proceed")
        sys.exit(2)
    return v


def _flatten_scopes(debug_data: dict) -> list[str]:
    """Token scopes can live in two places: top-level `scopes` (list of str)
    or `granular_scopes` (list of {scope, target_ids}). Merge both into a flat
    set so the presence check works regardless of the token format Meta
    returned."""
    scopes: set[str] = set()
    raw = debug_data.get("scopes") or []
    if isinstance(raw, list):
        scopes.update(str(s) for s in raw)
    elif isinstance(raw, str):
        scopes.update(raw.split(","))
    for g in debug_data.get("granular_scopes") or []:
        if isinstance(g, dict) and g.get("scope"):
            scopes.add(str(g["scope"]))
    return sorted(scopes)


async def main() -> int:
    access_token = _env("META_ACCESS_TOKEN")
    ig_user_id = _env("IG_USER_ID")
    fb_page_id = _env("FB_PAGE_ID")
    graph_version = os.getenv("GRAPH_API_VERSION", "v25.0")
    app_id = os.getenv("META_APP_ID")
    app_secret = os.getenv("META_APP_SECRET")

    print("=" * 72)
    print("Configuration (from /opt/social/.env)")
    print("=" * 72)
    print(f"  GRAPH_API_VERSION = {graph_version}")
    print(f"  FB_PAGE_ID        = {fb_page_id}    (env var: FB_PAGE_ID)")
    print(f"  IG_USER_ID        = {ig_user_id}    (env var: IG_USER_ID)")
    print(f"  META_APP_ID set:    {bool(app_id)}")
    print(f"  META_APP_SECRET set: {bool(app_secret)}")
    print()

    async with MetaClient(
        access_token=access_token,
        ig_user_id=ig_user_id,
        fb_page_id=fb_page_id,
        graph_version=graph_version,
        app_id=app_id,
        app_secret=app_secret,
    ) as client:

        # ---- 1) /debug_token on the user token ---------------------------
        print("=" * 72)
        print("1) GET /debug_token on META_ACCESS_TOKEN")
        print("=" * 72)
        try:
            info = await client.debug_token()
        except MetaAPIError as e:
            print(f"!! debug_token failed: {e}")
            return 1
        data = info.get("data") or {}
        scopes = _flatten_scopes(data)
        print(f"  is_valid     : {data.get('is_valid')}")
        print(f"  type         : {data.get('type')}    "
              f"(expected: USER for this MCP server's flow)")
        print(f"  app_id       : {data.get('app_id')}")
        print(f"  application  : {data.get('application')}")
        print(f"  user_id      : {data.get('user_id')}")
        print(f"  profile_id   : {data.get('profile_id')}")
        print(f"  expires_at   : {data.get('expires_at')}")
        print(f"  data_access  : {data.get('data_access_expires_at')}")
        print(f"  scopes ({len(scopes)}):")
        for s in scopes:
            print(f"    - {s}")
        print()
        print("  Required-scope check:")
        missing: list[str] = []
        for s in REQUIRED_SCOPES:
            ok = s in scopes
            print(f"    {'OK ' if ok else 'MISS'}  {s}")
            if not ok:
                missing.append(s)
        print()

        token_type = (data.get("type") or "").upper()
        if token_type != "USER":
            print(f"!! token type is {token_type!r}, not USER — /me/accounts will not"
                  " return pages the way this server expects.")
        if missing:
            print(f"!! missing scopes: {', '.join(missing)}")
        print()

        # ---- 2) /me/accounts (paginated full walk) -----------------------
        print("=" * 72)
        print("2) GET /me/accounts — all pages this user administers")
        print("=" * 72)
        page_dump: list[dict] = []
        try:
            params = {"fields": "id,name,access_token,tasks", "limit": 100}
            after: str | None = None
            page_no = 0
            while True:
                page_no += 1
                p = dict(params)
                if after:
                    p["after"] = after
                payload = await client._request("GET", "me/accounts", params=p)
                items = payload.get("data") or []
                print(f"  page {page_no}: {len(items)} entries")
                for acc in items:
                    page_dump.append(acc)
                cursors = (payload.get("paging") or {}).get("cursors") or {}
                nxt = (payload.get("paging") or {}).get("next")
                after = cursors.get("after") if nxt else None
                if not after or not items:
                    break
        except MetaAPIError as e:
            print(f"!! /me/accounts failed: {e}")
            return 1

        print(f"  total pages returned: {len(page_dump)}")
        for acc in page_dump:
            has_tok = bool(acc.get("access_token"))
            print(f"    - id={acc.get('id')!r:>22}  "
                  f"name={acc.get('name')!r:<40}  "
                  f"has_access_token={has_tok}  tasks={acc.get('tasks')}")
        print()

        # ---- 3) Match configured FB_PAGE_ID ------------------------------
        print("=" * 72)
        print("3) Match configured FB_PAGE_ID against /me/accounts result")
        print("=" * 72)
        configured = str(fb_page_id)
        ids_returned = [str(a.get("id")) for a in page_dump]
        match = next((a for a in page_dump if str(a.get("id")) == configured), None)

        case_a = match is not None
        case_b = len(page_dump) == 0
        case_c = (not case_a) and (not case_b)

        if case_a:
            print(f"  CASE (a): configured FB_PAGE_ID={configured} IS in /me/accounts.")
            print(f"            page name        : {match.get('name')!r}")
            print(f"            has access_token : {bool(match.get('access_token'))}")
            print(f"            tasks            : {match.get('tasks')}")
            print("            -> User IS admin and the page is reachable.")
            print("            If the server still says 'Page not found', it is a")
            print("            code-side bug (string/int compare, pagination not")
            print("            being walked, etc). Should not happen with the")
            print("            current meta_client.get_page_access_token().")
        elif case_b:
            print("  CASE (b): /me/accounts returned ZERO pages.")
            print("            Likely causes:")
            print("            - token lacks `pages_show_list`")
            print("            - token is not a USER token (was Page or App token)")
            print("            - the underlying Facebook user does not administer")
            print("              any page at all")
        else:
            print(f"  CASE (c): configured FB_PAGE_ID={configured} is NOT in the list.")
            print( "            User administers other page(s) instead — check that")
            print( "            the FB_PAGE_ID env var matches one of:")
            for pid in ids_returned:
                print(f"              {pid}")
            print( "            Fix: set FB_PAGE_ID in /opt/social/.env to the")
            print( "            correct id, then recreate the container.")
        print()

        # ---- 4) IG link on the configured (or, if missing, returned) page
        print("=" * 72)
        print("4) Instagram-Business-Account link on the page")
        print("=" * 72)
        check_page_id = configured if case_a else (ids_returned[0] if ids_returned else None)
        if not check_page_id:
            print("  skipped — no page id available to query.")
        else:
            try:
                ig_link = await client._request(
                    "GET",
                    check_page_id,
                    params={"fields": "id,name,instagram_business_account"},
                )
                ig = ig_link.get("instagram_business_account") or {}
                print(f"  page id  : {ig_link.get('id')}")
                print(f"  page name: {ig_link.get('name')!r}")
                if ig.get("id"):
                    print(f"  IG linked Business Account id: {ig.get('id')}")
                    print(f"  IG_USER_ID in .env           : {ig_user_id}")
                    if str(ig.get("id")) != str(ig_user_id):
                        print(f"  !! MISMATCH — POST /{ig_user_id}/media will fail."
                              " Update IG_USER_ID in .env.")
                    else:
                        print("  IG_USER_ID matches the linked account.")
                else:
                    print("  !! no instagram_business_account linked to this page.")
                    print("     Link the IG Professional account to the FB Page in")
                    print("     Page Settings -> Linked Accounts -> Instagram.")
            except MetaAPIError as e:
                print(f"  !! page lookup failed: {e}")
        print()

        # ---- 5) Verdict + next steps -------------------------------------
        print("=" * 72)
        print("VERDICT")
        print("=" * 72)
        if case_a and not missing:
            print("  Configuration looks correct. If posting still fails, the")
            print("  remaining failure mode is App Review / scope GRANT vs scope")
            print("  declared. See manual steps below.")
        elif case_a and missing:
            print("  Page is reachable but the user token is missing scopes:")
            print(f"    {', '.join(missing)}")
            print("  Regenerate the token with these scopes added.")
        elif case_b:
            print("  /me/accounts is empty. Token cannot enumerate any page.")
            if "pages_show_list" in missing:
                print("  Root cause: `pages_show_list` not granted.")
            print("  Regenerate the token with pages_show_list (and the rest).")
        else:
            print("  FB_PAGE_ID in .env does not match any page this user admins.")
            print("  Fix FB_PAGE_ID before anything else can succeed.")
        print()

        print("Next steps — CODE side (in this repo):")
        if case_a:
            print("  - none required for the page-lookup path; current code already")
            print("    paginates /me/accounts and matches by string id.")
        else:
            print("  - none required for the page-lookup path; current code is")
            print("    already paginating /me/accounts and matching by string id.")
        if case_c:
            print(f"  - Update FB_PAGE_ID in /opt/social/.env (currently {configured}).")
        print("  - After any .env change:")
        print("      docker compose -f /opt/social/docker-compose.yml up -d --force-recreate instagram-mcp")
        print()

        print("Next steps — MANUAL side (Meta App Dashboard / Graph Explorer):")
        if missing:
            print(f"  - Generate a new user token granting: {', '.join(missing)}")
            print("    (Graph API Explorer -> select app -> Get User Access Token ->")
            print("    tick all required scopes -> Generate.)")
            print("  - Exchange the short-lived token for a ~60-day long-lived token")
            print("    via /oauth/access_token (see README) before pasting into .env.")
        print("  - Confirm the Meta App has `instagram_content_publish` and")
        print("    `pages_manage_posts` available — for non-admins these require")
        print("    App Review. During development a token from an admin/developer/")
        print("    tester user bypasses review.")
        print("  - Confirm the FB Page has the IG Professional Account linked")
        print("    (Page Settings -> Linked Accounts -> Instagram).")
        print("  - If the app is in Development Mode, either keep it there and use")
        print("    the admin token, or switch to Live mode (Settings -> Basic).")
        print()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
