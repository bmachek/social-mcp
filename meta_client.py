"""Async client for the subset of the Meta Graph API we need.

Verified against developers.facebook.com for Graph API v25.0 (Feb 2026):
- Instagram content publishing two-step flow:
  POST /{ig-user-id}/media        -> returns container id
  GET  /{container-id}?fields=status_code -> poll until FINISHED
  POST /{ig-user-id}/media_publish?creation_id=...
- Facebook Page photo: POST /{page-id}/photos
- Token introspection: GET /debug_token?input_token=...
- Publishing quota: GET /{ig-user-id}/content_publishing_limit

Token model
-----------
The configured access_token is a long-lived USER token. For every IG and FB
Page publish call, the client derives a PAGE access token from it via
GET /me/accounts (cached for the client's lifetime) and uses that page token
to call the publish endpoints. This is the correct token for
/{page-id}/photos, /{page-id}/feed, /{page-id}/photo_stories and the IG
/{ig-user-id}/media + media_publish edges. Group posting (publish_to_groups)
still uses the user token, since groups are not owned by the page.

Scopes required on the underlying user token (set them at OAuth time in the
Graph API Explorer / Login flow — they cannot be requested from inside the
API call):
  - pages_show_list           (needed to derive the page token at all)
  - pages_read_engagement
  - pages_manage_posts        (FB page feed/photo/story publish)
  - instagram_basic
  - instagram_content_publish (IG feed/carousel/story publish)
  - publish_to_groups         (only if posting to groups)
"""

from __future__ import annotations

import asyncio
import mimetypes
import pathlib
from dataclasses import dataclass
from typing import Any

import httpx


GRAPH_HOST = "https://graph.facebook.com"

CONTAINER_STATUS_FINISHED = "FINISHED"
CONTAINER_STATUS_TERMINAL_ERROR = {"ERROR", "EXPIRED"}


class MetaAPIError(RuntimeError):
    """Raised when the Graph API returns an error payload.

    Surfaces the structured fields Meta returns (code, subcode, message,
    fbtrace_id) so callers can report them verbatim instead of guessing.
    """

    def __init__(self, payload: dict[str, Any], http_status: int | None = None):
        err = payload.get("error", {}) if isinstance(payload, dict) else {}
        self.code = err.get("code")
        self.subcode = err.get("error_subcode")
        self.type = err.get("type")
        self.message = err.get("message") or str(payload)
        self.fbtrace_id = err.get("fbtrace_id")
        self.user_title = err.get("error_user_title")
        self.user_msg = err.get("error_user_msg")
        self.http_status = http_status
        super().__init__(self._fmt())

    def _fmt(self) -> str:
        parts = [f"Meta API error: {self.message}"]
        if self.code is not None:
            parts.append(f"code={self.code}")
        if self.subcode is not None:
            parts.append(f"subcode={self.subcode}")
        if self.type:
            parts.append(f"type={self.type}")
        if self.http_status is not None:
            parts.append(f"http={self.http_status}")
        if self.fbtrace_id:
            parts.append(f"fbtrace_id={self.fbtrace_id}")
        if self.user_msg:
            parts.append(f"user_msg={self.user_msg}")
        return " | ".join(parts)

    def to_dict(self) -> dict[str, Any]:
        return {
            "message": self.message,
            "code": self.code,
            "subcode": self.subcode,
            "type": self.type,
            "http_status": self.http_status,
            "fbtrace_id": self.fbtrace_id,
            "error_user_title": self.user_title,
            "error_user_msg": self.user_msg,
        }


@dataclass
class ContainerStatus:
    status_code: str
    raw: dict[str, Any]


class MetaClient:
    def __init__(
        self,
        access_token: str,
        ig_user_id: str,
        fb_page_id: str,
        graph_version: str = "v25.0",
        app_id: str | None = None,
        app_secret: str | None = None,
        timeout: float = 30.0,
        page_access_token: str | None = None,
    ):
        if not access_token:
            raise ValueError("access_token is required")
        if not graph_version.startswith("v"):
            graph_version = f"v{graph_version}"
        self.access_token = access_token
        self.ig_user_id = ig_user_id
        self.fb_page_id = fb_page_id
        self.graph_version = graph_version
        self.app_id = app_id
        self.app_secret = app_secret
        self.base = f"{GRAPH_HOST}/{graph_version}"
        self._client = httpx.AsyncClient(timeout=timeout)
        self._page_access_token: str | None = page_access_token

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "MetaClient":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        token: str | None = None,
    ) -> dict[str, Any]:
        url = f"{self.base}/{path.lstrip('/')}"
        params = {**(params or {})}
        params.setdefault("access_token", token or self.access_token)
        resp = await self._client.request(method, url, params=params, data=data)
        try:
            payload = resp.json()
        except ValueError:
            payload = {"error": {"message": resp.text or "non-JSON response"}}
        if resp.status_code >= 400 or (isinstance(payload, dict) and "error" in payload):
            raise MetaAPIError(payload, http_status=resp.status_code)
        return payload

    async def get_page_access_token(self) -> str:
        """Derive (and cache) the Page Access Token for fb_page_id.

        Walks GET /me/accounts (paginated) until it finds the configured page
        and returns its `access_token`. Caches in-memory for the lifetime of
        this client.

        Required scope on the user token: pages_show_list. Page tokens derived
        from a long-lived (60d) user token do not themselves expire.
        """
        if self._page_access_token:
            return self._page_access_token
        params: dict[str, Any] = {"fields": "id,name,access_token", "limit": 100}
        after: str | None = None
        while True:
            p = dict(params)
            if after:
                p["after"] = after
            payload = await self._request("GET", "me/accounts", params=p)
            for acc in payload.get("data") or []:
                if str(acc.get("id")) == str(self.fb_page_id):
                    tok = acc.get("access_token")
                    if not tok:
                        raise MetaAPIError({"error": {"message": (
                            f"Page {self.fb_page_id} found in /me/accounts but no "
                            "access_token returned — user token is missing pages_show_list."
                        )}})
                    self._page_access_token = tok
                    return tok
            cursors = (payload.get("paging") or {}).get("cursors") or {}
            after = cursors.get("after")
            if not after or not (payload.get("data") or []):
                break
        raise MetaAPIError({"error": {"message": (
            f"Page id {self.fb_page_id} not found in /me/accounts. Confirm the "
            "configured user is an admin of that page and the token has "
            "pages_show_list."
        )}})

    async def validate_image_url(self, image_url: str) -> dict[str, Any]:
        """Confirm the URL is publicly reachable and looks like an image.

        Instagram requires the image to be downloadable by Meta's servers.
        We HEAD first, fall back to a 1-byte range GET if HEAD is rejected.
        """
        try:
            r = await self._client.head(image_url, follow_redirects=True)
            if r.status_code == 405 or r.status_code >= 400:
                r = await self._client.get(
                    image_url,
                    follow_redirects=True,
                    headers={"Range": "bytes=0-0"},
                )
        except httpx.HTTPError as e:
            raise RuntimeError(f"image_url not reachable: {e}") from e
        if r.status_code >= 400:
            raise RuntimeError(
                f"image_url returned HTTP {r.status_code}; Meta servers will not be able to fetch it"
            )
        ctype = r.headers.get("content-type", "")
        if ctype and not ctype.startswith("image/"):
            raise RuntimeError(
                f"image_url content-type is {ctype!r}; expected image/* for Instagram publishing"
            )
        return {
            "status": r.status_code,
            "content_type": ctype or "unknown",
            "content_length": r.headers.get("content-length"),
            "final_url": str(r.url),
        }

    async def create_ig_container(
        self,
        image_url: str,
        caption: str | None = None,
        alt_text: str | None = None,
        media_type: str | None = None,
    ) -> str:
        data: dict[str, Any] = {"image_url": image_url}
        if caption:
            data["caption"] = caption
        if alt_text:
            data["alt_text"] = alt_text
        if media_type:
            data["media_type"] = media_type
        page_token = await self.get_page_access_token()
        payload = await self._request(
            "POST", f"{self.ig_user_id}/media", data=data, token=page_token,
        )
        cid = payload.get("id")
        if not cid:
            raise MetaAPIError({"error": {"message": f"unexpected response: {payload}"}})
        return str(cid)

    async def create_ig_story_container(self, image_url: str) -> str:
        """IG Story: same /media endpoint with media_type=STORIES, no caption."""
        return await self.create_ig_container(image_url=image_url, media_type="STORIES")

    async def create_ig_carousel_item(self, image_url: str) -> str:
        """Per-child container for a carousel slide (is_carousel_item=true)."""
        data: dict[str, Any] = {"image_url": image_url, "is_carousel_item": "true"}
        page_token = await self.get_page_access_token()
        payload = await self._request(
            "POST", f"{self.ig_user_id}/media", data=data, token=page_token,
        )
        cid = payload.get("id")
        if not cid:
            raise MetaAPIError({"error": {"message": f"unexpected response: {payload}"}})
        return str(cid)

    async def create_ig_carousel_container(
        self,
        children_ids: list[str],
        caption: str | None = None,
    ) -> str:
        """Parent container that ties carousel children together for publish."""
        data: dict[str, Any] = {
            "media_type": "CAROUSEL",
            "children": ",".join(children_ids),
        }
        if caption:
            data["caption"] = caption
        page_token = await self.get_page_access_token()
        payload = await self._request(
            "POST", f"{self.ig_user_id}/media", data=data, token=page_token,
        )
        cid = payload.get("id")
        if not cid:
            raise MetaAPIError({"error": {"message": f"unexpected response: {payload}"}})
        return str(cid)

    async def get_container_status(self, container_id: str) -> ContainerStatus:
        page_token = await self.get_page_access_token()
        payload = await self._request(
            "GET", container_id, params={"fields": "status_code,status"},
            token=page_token,
        )
        return ContainerStatus(status_code=payload.get("status_code", ""), raw=payload)

    async def wait_for_container(
        self,
        container_id: str,
        timeout_seconds: float = 90.0,
        poll_interval: float = 3.0,
    ) -> ContainerStatus:
        loop = asyncio.get_event_loop()
        deadline = loop.time() + timeout_seconds
        last: ContainerStatus | None = None
        while loop.time() < deadline:
            last = await self.get_container_status(container_id)
            if last.status_code == CONTAINER_STATUS_FINISHED:
                return last
            if last.status_code in CONTAINER_STATUS_TERMINAL_ERROR:
                raise MetaAPIError(
                    {
                        "error": {
                            "message": f"container terminal status: {last.status_code}",
                            "code": "container_status",
                        }
                    }
                )
            await asyncio.sleep(poll_interval)
        raise TimeoutError(
            f"container {container_id} not FINISHED after {timeout_seconds}s "
            f"(last status: {last.status_code if last else 'unknown'})"
        )

    async def publish_ig_container(self, creation_id: str) -> dict[str, Any]:
        page_token = await self.get_page_access_token()
        return await self._request(
            "POST",
            f"{self.ig_user_id}/media_publish",
            data={"creation_id": creation_id},
            token=page_token,
        )

    async def get_publishing_limit(self) -> dict[str, Any]:
        page_token = await self.get_page_access_token()
        return await self._request(
            "GET",
            f"{self.ig_user_id}/content_publishing_limit",
            params={"fields": "quota_usage,config"},
            token=page_token,
        )

    async def post_fb_page_photo(
        self,
        image_url: str,
        caption: str | None = None,
        published: bool = True,
    ) -> dict[str, Any]:
        data: dict[str, Any] = {"url": image_url, "published": str(published).lower()}
        if caption:
            # `message` is the canonical field for post text on /photos posts.
            data["message"] = caption
        page_token = await self.get_page_access_token()
        return await self._request(
            "POST", f"{self.fb_page_id}/photos", data=data, token=page_token,
        )

    async def post_fb_page_photo_file(
        self,
        file_path: pathlib.Path,
        caption: str | None = None,
        published: bool = True,
    ) -> dict[str, Any]:
        """Upload a local file directly to /{page-id}/photos as multipart `source`.

        Bypasses the public-URL requirement that `post_fb_page_photo` has.
        Only works for Facebook — Instagram's /media endpoint has no equivalent.
        """
        page_token = await self.get_page_access_token()
        url = f"{self.base}/{self.fb_page_id}/photos"
        data: dict[str, Any] = {
            "access_token": page_token,
            "published": str(published).lower(),
        }
        if caption:
            data["message"] = caption
        mime = mimetypes.guess_type(file_path.name)[0] or "image/jpeg"
        content = file_path.read_bytes()
        files = {"source": (file_path.name, content, mime)}
        resp = await self._client.post(url, data=data, files=files)
        try:
            payload = resp.json()
        except ValueError:
            payload = {"error": {"message": resp.text or "non-JSON response"}}
        if resp.status_code >= 400 or (isinstance(payload, dict) and "error" in payload):
            raise MetaAPIError(payload, http_status=resp.status_code)
        return payload

    async def post_fb_page_multi_photo_feed(
        self,
        photo_ids: list[str],
        message: str | None = None,
    ) -> dict[str, Any]:
        """Create a Page feed post that attaches multiple previously-uploaded photos.

        Upload each photo first via post_fb_page_photo_file(published=False) to
        get its `id`, then call this to publish a single feed post with all
        photos attached.
        """
        if not photo_ids:
            raise ValueError("photo_ids must not be empty")
        data: dict[str, Any] = {}
        if message:
            data["message"] = message
        for i, pid in enumerate(photo_ids):
            data[f"attached_media[{i}]"] = '{"media_fbid":"' + str(pid) + '"}'
        page_token = await self.get_page_access_token()
        return await self._request(
            "POST", f"{self.fb_page_id}/feed", data=data, token=page_token,
        )

    async def list_ig_recent_media(
        self,
        limit: int = 25,
        after: str | None = None,
    ) -> dict[str, Any]:
        """List recent IG media on the configured account.

        Returns the raw Graph response so the caller can keep the `paging.cursors`
        for follow-up calls.
        """
        params: dict[str, Any] = {
            "fields": "id,caption,media_type,media_url,permalink,thumbnail_url,timestamp",
            "limit": max(1, min(int(limit), 100)),
        }
        if after:
            params["after"] = after
        page_token = await self.get_page_access_token()
        return await self._request(
            "GET", f"{self.ig_user_id}/media", params=params, token=page_token,
        )

    async def list_fb_page_recent_posts(
        self,
        limit: int = 25,
        after: str | None = None,
    ) -> dict[str, Any]:
        """List recent Page posts including the attached photo (if any).

        `full_picture` is the high-res rendering Meta serves for the post.
        """
        params: dict[str, Any] = {
            "fields": "id,message,created_time,permalink_url,full_picture,attachments{media,type,title}",
            "limit": max(1, min(int(limit), 100)),
        }
        if after:
            params["after"] = after
        page_token = await self.get_page_access_token()
        return await self._request(
            "GET", f"{self.fb_page_id}/posts", params=params, token=page_token,
        )

    async def post_fb_page_story_from_photo(self, photo_id: str) -> dict[str, Any]:
        """Promote a previously-uploaded unpublished Page photo into a Page Story.

        Workflow: upload to /{page-id}/photos with published=false to obtain
        photo_id, then POST /{page-id}/photo_stories?photo_id=... to publish
        it as a 24h Story. Requires pages_manage_posts scope on the page token.
        """
        page_token = await self.get_page_access_token()
        return await self._request(
            "POST",
            f"{self.fb_page_id}/photo_stories",
            data={"photo_id": photo_id},
            token=page_token,
        )

    async def post_fb_group_photo_file(
        self,
        group_id: str,
        file_path: pathlib.Path,
        caption: str | None = None,
    ) -> dict[str, Any]:
        """Upload a local file to a Facebook Group as a photo post.

        POST /{group-id}/photos with multipart `source`. Needs publish_to_groups
        scope, the user must be an admin/member of the group, and the Meta App
        must be approved for publish_to_groups. Surfaces structured errors so
        the caller can decide whether to abort or continue with the next group.
        """
        url = f"{self.base}/{group_id}/photos"
        data: dict[str, Any] = {"access_token": self.access_token}
        if caption:
            data["message"] = caption
        mime = mimetypes.guess_type(file_path.name)[0] or "image/jpeg"
        content = file_path.read_bytes()
        files = {"source": (file_path.name, content, mime)}
        resp = await self._client.post(url, data=data, files=files)
        try:
            payload = resp.json()
        except ValueError:
            payload = {"error": {"message": resp.text or "non-JSON response"}}
        if resp.status_code >= 400 or (isinstance(payload, dict) and "error" in payload):
            raise MetaAPIError(payload, http_status=resp.status_code)
        return payload

    async def debug_token(self, token_to_check: str | None = None) -> dict[str, Any]:
        """Introspect a token via /debug_token.

        Uses an app access token (`{app_id}|{app_secret}`) if app credentials
        are configured; otherwise falls back to the same token for
        authentication (the token can introspect itself in many cases).
        """
        target = token_to_check or self.access_token
        if self.app_id and self.app_secret:
            auth_token = f"{self.app_id}|{self.app_secret}"
        else:
            auth_token = self.access_token
        url = f"{self.base}/debug_token"
        params = {"input_token": target, "access_token": auth_token}
        resp = await self._client.get(url, params=params)
        try:
            payload = resp.json()
        except ValueError:
            payload = {"error": {"message": resp.text or "non-JSON response"}}
        if resp.status_code >= 400 or (isinstance(payload, dict) and "error" in payload):
            raise MetaAPIError(payload, http_status=resp.status_code)
        return payload
