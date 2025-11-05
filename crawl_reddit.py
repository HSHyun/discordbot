#!/usr/bin/env python3
"""Reddit 서브레딧 `/new` 피드를 조회해 게시물 목록을 제공합니다."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable, Iterator, List
from html import unescape
import os
import time
from threading import Lock
from pathlib import Path
from urllib.parse import urlparse

import requests
from requests.auth import HTTPBasicAuth

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

REDDIT_BASE = "https://www.reddit.com"
REDDIT_API_BASE = "https://oauth.reddit.com"
REDDIT_TOKEN_URL = "https://www.reddit.com/api/v1/access_token"
API_TIMEOUT_SECONDS = 30
# 댓글 수집은 기본적으로 제한하지 않는다.
MAX_COMMENT_DEPTH: int | None = None
MAX_COMMENTS_PER_POST: int | None = None


_TOKEN_LOCK = Lock()
_TOKEN_CACHE: dict[str, float | str | None] = {
    "access_token": None,
    "expires_at": 0.0,
}
_CACHED_CREDENTIALS: dict[str, str] | None = None
_API_SESSION = requests.Session()
_ENV_LOADED = False


@dataclass(slots=True)
class RedditPost:
    """Reddit 게시물 정보를 담는 데이터 구조."""

    subreddit: str
    external_id: str
    title: str
    url: str
    author: str
    created_utc: datetime
    score: int
    num_comments: int
    selftext: str
    permalink: str
    is_self: bool
    flair: str | None
    thumbnail: str | None
    media_urls: List[str]
    metadata: dict

    @property
    def is_video(self) -> bool:
        return bool(self.metadata.get("is_video"))

    @property
    def number(self) -> str:
        return self.external_id

    @property
    def subject(self) -> str:
        return self.subreddit

    @property
    def comments(self) -> str:
        return str(self.num_comments)

    @property
    def writer(self) -> str:
        return self.author or "unknown"

    @property
    def date_display(self) -> str:
        return self.created_utc.astimezone().strftime("%Y-%m-%d %H:%M")

    @property
    def date_iso(self) -> str:
        return self.created_utc.replace(tzinfo=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    @property
    def views(self) -> str:
        return str(self.score)

    @property
    def recommends(self) -> str:
        return str(self.score)


def _getenv_casefold(key: str) -> str | None:
    _ensure_env_loaded()
    target = key.casefold()
    for env_key, value in os.environ.items():
        if env_key.casefold() == target:
            return value.strip()
    return None


def _resolve_user_agent(override: str | None) -> str:
    env_agent = _getenv_casefold("REDDIT_USER_AGENT")
    if env_agent:
        return env_agent
    if override:
        return override
    return DEFAULT_USER_AGENT


def _load_credentials() -> dict[str, str]:
    global _CACHED_CREDENTIALS
    if _CACHED_CREDENTIALS is not None:
        return _CACHED_CREDENTIALS

    client_id = _getenv_casefold("REDDIT_CLIENT_ID")
    client_secret = _getenv_casefold("REDDIT_CLIENT_SECRET")
    username = _getenv_casefold("REDDIT_USERNAME")
    password = _getenv_casefold("REDDIT_PASSWORD")

    if not all([client_id, client_secret, username, password]):
        missing = [
            name
            for name, value in (
                ("REDDIT_CLIENT_ID", client_id),
                ("REDDIT_CLIENT_SECRET", client_secret),
                ("REDDIT_USERNAME", username),
                ("REDDIT_PASSWORD", password),
            )
            if not value
        ]
        raise RuntimeError(
            "필수 Reddit OAuth 환경 변수가 설정되지 않았습니다: " + ", ".join(missing)
        )

    _CACHED_CREDENTIALS = {
        "client_id": client_id,
        "client_secret": client_secret,
        "username": username,
        "password": password,
    }
    return _CACHED_CREDENTIALS


def _ensure_env_loaded(path: Path = Path(".env")) -> None:
    global _ENV_LOADED
    if _ENV_LOADED:
        return
    if not path.exists():
        _ENV_LOADED = True
        return
    try:
        with path.open("r", encoding="utf-8") as env_file:
            for raw_line in env_file:
                line = raw_line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                if not key or key in os.environ:
                    continue
                cleaned = value.strip()
                if (
                    len(cleaned) >= 2
                    and cleaned[0] == cleaned[-1]
                    and cleaned[0] in {'"', "'"}
                ):
                    cleaned = cleaned[1:-1]
                os.environ[key] = cleaned
    except OSError:
        pass
    _ENV_LOADED = True


def _request_new_token(user_agent: str) -> tuple[str, float]:
    creds = _load_credentials()
    auth = HTTPBasicAuth(creds["client_id"], creds["client_secret"])
    data = {
        "grant_type": "password",
        "username": creds["username"],
        "password": creds["password"],
    }
    headers = {
        "User-Agent": user_agent,
    }
    response = _API_SESSION.post(
        REDDIT_TOKEN_URL,
        auth=auth,
        data=data,
        headers=headers,
        timeout=API_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    payload = response.json()
    token = payload.get("access_token")
    if not token:
        raise RuntimeError("Reddit OAuth 토큰 응답에 access_token이 없습니다.")
    expires_in = float(payload.get("expires_in") or 3600)
    return token, time.time() + max(expires_in - 60.0, 0.0)


def _invalidate_token() -> None:
    with _TOKEN_LOCK:
        _TOKEN_CACHE["access_token"] = None
        _TOKEN_CACHE["expires_at"] = 0.0


def _get_access_token(user_agent: str) -> str:
    now = time.time()
    with _TOKEN_LOCK:
        token = _TOKEN_CACHE.get("access_token")
        expires_at = float(_TOKEN_CACHE.get("expires_at") or 0.0)
        if token and now < expires_at:
            return str(token)

        new_token, new_expiry = _request_new_token(user_agent)
        _TOKEN_CACHE["access_token"] = new_token
        _TOKEN_CACHE["expires_at"] = new_expiry
        return new_token


def _api_request(
    method: str,
    path: str,
    *,
    params: dict | None = None,
    user_agent: str,
) -> requests.Response:
    url = f"{REDDIT_API_BASE}{path}"
    attempt_params = dict(params or {})
    attempt_params.setdefault("raw_json", 1)

    for attempt in range(2):
        token = _get_access_token(user_agent)
        headers = {
            "Authorization": f"bearer {token}",
            "User-Agent": user_agent,
            "Accept": "application/json",
        }
        response = _API_SESSION.request(
            method,
            url,
            params=attempt_params,
            headers=headers,
            timeout=API_TIMEOUT_SECONDS,
        )
        if response.status_code != 401:
            response.raise_for_status()
            return response
        _invalidate_token()

    response.raise_for_status()
    return response


def _format_comment_timestamp(timestamp: float | int | None) -> str | None:
    if timestamp is None:
        return None
    try:
        dt = datetime.fromtimestamp(float(timestamp), tz=timezone.utc)
    except (TypeError, ValueError, OSError):
        return None
    return dt.isoformat()


def _parse_comment_listing(
    listing: dict,
    *,
    accumulator: List[dict],
    max_comments: int | None,
    depth: int = 0,
) -> None:
    if MAX_COMMENT_DEPTH is not None and depth >= MAX_COMMENT_DEPTH:
        return

    children = listing.get("children") or []
    for child in children:
        if max_comments is not None and len(accumulator) >= max_comments:
            return
        if not isinstance(child, dict):
            continue
        if child.get("kind") != "t1":
            continue
        data = child.get("data") or {}
        accumulator.append(
            {
                "id": data.get("id"),
                "name": data.get("name"),
                "author": data.get("author") or "unknown",
                "body": data.get("body") or "",
                "score": data.get("score"),
                "created_utc": _format_comment_timestamp(data.get("created_utc")),
                "permalink": data.get("permalink"),
                "depth": depth,
                "parent_id": data.get("parent_id"),
                "is_deleted": bool(data.get("body_deleted"))
                or (str(data.get("body") or "").strip().lower() in {"[deleted]", "[removed]"}),
                "metadata": {
                    "score": data.get("score"),
                    "ups": data.get("ups"),
                    "downs": data.get("downs"),
                    "permalink": data.get("permalink"),
                    "distinguished": data.get("distinguished"),
                    "stickied": data.get("stickied"),
                    "collapsed": data.get("collapsed"),
                },
            }
        )
        replies = data.get("replies")
        if replies and isinstance(replies, dict):
            reply_listing = replies.get("data") or {}
            _parse_comment_listing(
                reply_listing,
                accumulator=accumulator,
                max_comments=max_comments,
                depth=depth + 1,
            )


def _fetch_post_comments(
    permalink: str,
    *,
    user_agent: str,
    max_comments: int | None = MAX_COMMENTS_PER_POST,
) -> List[dict]:
    if not permalink:
        return []
    params = {"sort": "confidence"}
    if max_comments is not None:
        params["limit"] = max_comments
    if MAX_COMMENT_DEPTH is not None:
        params["depth"] = MAX_COMMENT_DEPTH
    response = _api_request(
        "GET",
        f"{permalink}.json",
        params=params,
        user_agent=user_agent,
    )
    payload = response.json()
    if not isinstance(payload, list) or len(payload) < 2:
        return []
    comment_listing = payload[1].get("data") if isinstance(payload[1], dict) else None
    if not isinstance(comment_listing, dict):
        return []

    return _extract_comments_from_listing(comment_listing, max_comments=max_comments)


def _extract_comments_from_listing(
    listing_data: dict, *, max_comments: int | None
) -> List[dict]:
    collected: List[dict] = []
    _parse_comment_listing(
        listing_data,
        accumulator=collected,
        max_comments=max_comments,
        depth=0,
    )
    return collected


def _to_datetime(timestamp: float | int | None) -> datetime:
    if not timestamp:
        return datetime.now(timezone.utc)
    return datetime.fromtimestamp(float(timestamp), tz=timezone.utc)


def _clean_url(url: str | None) -> str | None:
    if not url:
        return None
    return unescape(url)


def _extract_media_urls(node: dict) -> List[str]:
    media_urls: List[str] = []
    preview = node.get("preview") or {}
    for image in preview.get("images", []):
        source = image.get("source") or {}
        url = _clean_url(source.get("url"))
        if url:
            media_urls.append(url)
        for res in image.get("resolutions", []):
            url = _clean_url(res.get("url"))
            if url and url not in media_urls:
                media_urls.append(url)
    gallery_data = node.get("gallery_data") or {}
    media_metadata = node.get("media_metadata") or {}
    for item in gallery_data.get("items", []):
        media_id = item.get("media_id")
        if not media_id:
            continue
        meta = media_metadata.get(media_id) or {}
        if meta.get("status") != "valid":
            continue
        variants = meta.get("p") or []
        for variant in variants:
            url = _clean_url(variant.get("u"))
            if url and url not in media_urls:
                media_urls.append(url)
        source = meta.get("s") or {}
        url = _clean_url(source.get("u"))
        if url and url not in media_urls:
            media_urls.append(url)
    if not media_urls:
        thumb = _clean_url(node.get("url_overridden_by_dest"))
        if thumb:
            media_urls.append(thumb)
    return media_urls


def _build_reddit_post(
    node: dict,
    *,
    subreddit: str,
    created: datetime,
    resolved_user_agent: str,
    fetched_at: datetime,
    comments: List[dict] | None = None,
) -> RedditPost | None:
    post_id = node.get("id")
    if not post_id:
        return None
    reddit_id = node.get("name") or post_id
    media_urls = _extract_media_urls(node)

    metadata = {
        "score": int(node.get("score") or 0),
        "num_comments": int(node.get("num_comments") or 0),
        "is_self": bool(node.get("is_self")),
        "permalink": node.get("permalink"),
        "upvote_ratio": node.get("upvote_ratio"),
        "over_18": node.get("over_18"),
        "spoiler": node.get("spoiler"),
        "post_hint": node.get("post_hint"),
        "domain": node.get("domain"),
        "url_overridden_by_dest": node.get("url_overridden_by_dest"),
        "is_video": bool(node.get("is_video")),
        "media_only": bool(node.get("media_only")),
        "fetched_at": fetched_at.isoformat(),
    }

    permalink = node.get("permalink") or ""
    expected_comments = metadata["num_comments"]
    if MAX_COMMENTS_PER_POST is None:
        comment_cap: int | None = None
    else:
        comment_cap = (
            min(expected_comments, MAX_COMMENTS_PER_POST)
            if expected_comments
            else MAX_COMMENTS_PER_POST
        )
    if comments is None:
        try:
            comments = _fetch_post_comments(
                permalink,
                user_agent=resolved_user_agent,
                max_comments=comment_cap,
            )
        except requests.HTTPError:
            comments = []
    else:
        comments = comments[:comment_cap]
    metadata["comments"] = comments

    return RedditPost(
        subreddit=subreddit,
        external_id=reddit_id,
        title=node.get("title") or "(untitled)",
        url=f"{REDDIT_BASE}{permalink}" if permalink else node.get("url") or "",
        author=node.get("author") or "unknown",
        created_utc=created,
        score=int(node.get("score") or 0),
        num_comments=int(node.get("num_comments") or 0),
        selftext=node.get("selftext") or "",
        permalink=permalink,
        is_self=bool(node.get("is_self")),
        flair=node.get("link_flair_text") or node.get("author_flair_text"),
        thumbnail=_clean_url(node.get("thumbnail")),
        media_urls=media_urls,
        metadata={k: v for k, v in metadata.items() if v is not None},
    )


def fetch_reddit_posts(
    subreddit: str,
    limit: int = 50,
    user_agent: str = DEFAULT_USER_AGENT,
    max_age_hours: float | None = None,
) -> List[RedditPost]:
    """지정한 서브레딧에서 최신 게시물을 가져온다."""
    resolved_user_agent = _resolve_user_agent(user_agent)
    response = _api_request(
        "GET",
        f"/r/{subreddit}/new",
        params={"limit": limit},
        user_agent=resolved_user_agent,
    )
    payload = response.json()

    posts: List[RedditPost] = []
    cutoff = None
    if max_age_hours is not None:
        cutoff = datetime.now(timezone.utc).timestamp() - (max_age_hours * 3600)

    fetched_at = datetime.now(timezone.utc)

    for child in payload.get("data", {}).get("children", []):
        node = child.get("data") or {}
        if cutoff is not None and (node.get("created_utc") or 0) < cutoff:
            continue
        created = _to_datetime(node.get("created_utc"))
        post = _build_reddit_post(
            node,
            subreddit=subreddit,
            created=created,
            resolved_user_agent=resolved_user_agent,
            fetched_at=fetched_at,
        )
        if post is not None:
            posts.append(post)

    return posts


def fetch_reddit_post_by_url(
    url: str,
    *,
    user_agent: str = DEFAULT_USER_AGENT,
) -> RedditPost | None:
    """단일 Reddit 게시물을 URL로 조회한다."""

    if not url:
        raise ValueError("게시물 URL이 필요합니다.")

    resolved_user_agent = _resolve_user_agent(user_agent)

    parsed = urlparse(url)
    path = parsed.path or ""
    if not path:
        raise ValueError("유효한 Reddit 게시물 URL이 아닙니다.")

    if not path.startswith("/"):
        path = "/" + path

    path = path.rstrip("/")
    if not path:
        raise ValueError("유효한 Reddit 게시물 경로를 확인할 수 없습니다.")

    params = {"sort": "confidence"}
    if MAX_COMMENTS_PER_POST is not None:
        params["limit"] = MAX_COMMENTS_PER_POST
    if MAX_COMMENT_DEPTH is not None:
        params["depth"] = MAX_COMMENT_DEPTH
    response = _api_request(
        "GET",
        f"{path}.json",
        params=params,
        user_agent=resolved_user_agent,
    )

    payload = response.json()
    if not isinstance(payload, list) or not payload:
        return None

    post_listing = payload[0] if len(payload) >= 1 else None
    if not isinstance(post_listing, dict):
        return None

    children = post_listing.get("data", {}).get("children", [])
    if not children:
        return None
    first_child = children[0]
    if not isinstance(first_child, dict):
        return None
    node = first_child.get("data") or {}
    if not node:
        return None

    comment_listing = payload[1].get("data") if len(payload) > 1 and isinstance(payload[1], dict) else None
    comments: List[dict] | None = None
    if isinstance(comment_listing, dict):
        comments = _extract_comments_from_listing(
            comment_listing,
            max_comments=MAX_COMMENTS_PER_POST,
        )

    subreddit = node.get("subreddit") or ""
    if not subreddit:
        segments = [segment for segment in path.split("/") if segment]
        try:
            index = segments.index("r")
            if index + 1 < len(segments):
                subreddit = segments[index + 1]
        except ValueError:
            pass
    subreddit = subreddit or ""
    fetched_at = datetime.now(timezone.utc)
    created = _to_datetime(node.get("created_utc"))

    return _build_reddit_post(
        node,
        subreddit=subreddit or "",
        created=created,
        resolved_user_agent=resolved_user_agent,
        fetched_at=fetched_at,
        comments=comments,
    )


def fetch_multiple(
    subreddits: Iterable[str],
    limit: int = 50,
    user_agent: str = DEFAULT_USER_AGENT,
) -> Iterator[RedditPost]:
    """여러 서브레딧에서 게시물을 순차적으로 가져온다."""
    for subreddit in subreddits:
        for post in fetch_reddit_posts(subreddit, limit=limit, user_agent=user_agent):
            yield post


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Fetch Reddit /new posts")
    parser.add_argument("subreddit", help="대상 서브레딧")
    parser.add_argument("--limit", type=int, default=10)
    args = parser.parse_args()

    for post in fetch_reddit_posts(args.subreddit, limit=args.limit):
        print(f"[{post.subreddit}] {post.title} (score={post.score}, comments={post.num_comments})")
        print(f"    URL: {post.url}")
        if post.media_urls:
            print(f"    Media: {post.media_urls[0]}")
