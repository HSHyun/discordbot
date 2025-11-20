"""본문과 이미지 자산, 댓글을 수집하는 도우미."""

from __future__ import annotations

import io
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, List, Tuple
from urllib.parse import parse_qs, urljoin, urlparse

import requests
from PIL import Image
from bs4 import BeautifulSoup

MIME_EXTENSION = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
}

IMAGE_FORMAT_EXTENSION = {
    "JPEG": ".jpg",
    "JPG": ".jpg",
    "PNG": ".png",
    "GIF": ".gif",
    "BMP": ".bmp",
    "TIFF": ".tiff",
    "WEBP": ".webp",
}

ALLOWED_URL_SUFFIXES = {
    ".jpg",
    ".jpeg",
    ".png",
    ".gif",
    ".bmp",
    ".webp",
    ".tif",
    ".tiff",
}

VIDEO_URL_SUFFIXES = {
    ".mp4",
    ".webm",
    ".mkv",
    ".mov",
    ".avi",
}


def fetch_post_body(
    url: str, headers: dict[str, str]
) -> Tuple[str, List[str], List[dict]]:
    """상세 페이지에서 본문 텍스트, 이미지 URL, 댓글 정보를 추출한다."""
    response = requests.get(url, headers=headers, timeout=30)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")
    container = soup.select_one("div.write_div")

    text = ""
    if container is not None:
        text = container.get_text("\n", strip=True)

    image_urls: List[str] = []
    seen = set()
    if container is not None:
        for img in container.select("img"):
            candidates = [
                img.get(attr)
                for attr in ("data-origin", "data-original", "data-src", "src")
            ]
            for candidate in candidates:
                if not candidate or "gallview_loading" in candidate:
                    continue
                full_url = urljoin(url, candidate)
                if full_url.startswith("//"):
                    full_url = "https:" + full_url
                if full_url not in seen:
                    image_urls.append(full_url)
                    seen.add(full_url)
                    break

    comments = _fetch_dcinside_comments(url)
    return text, image_urls, comments


def guess_extension(
    url: str, content_type: str | None, content: bytes | None = None
) -> str:
    """다운로드한 이미지의 확장자를 추론한다."""
    ctype = (content_type or "").split(";", 1)[0].strip().lower()
    if ctype:
        ext = MIME_EXTENSION.get(ctype)
        if ext:
            return ext
    if content is not None:
        try:
            with Image.open(io.BytesIO(content)) as img:
                detected = IMAGE_FORMAT_EXTENSION.get((img.format or "").upper())
                if detected:
                    return detected
        except Exception:
            pass
    parsed = urlparse(url)
    suffix = Path(parsed.path).suffix.lower()
    if suffix in ALLOWED_URL_SUFFIXES:
        return suffix
    return ".bin"


def contains_video_url(urls: Iterable[str]) -> bool:
    """URL 목록 중 비디오로 추정되는 항목이 있는지 확인한다."""
    for url in urls:
        if not url:
            continue
        suffix = Path(urlparse(url).path).suffix.lower()
        if suffix in VIDEO_URL_SUFFIXES:
            return True
    return False


def download_images(
    image_urls: Iterable[str],
    external_id: str,
    referer: str,
    asset_root: Path,
    headers: dict[str, str],
) -> List[dict]:
    """이미지를 내려받아 로컬에 저장하고 메타데이터를 반환한다."""
    assets: List[dict] = []
    target_dir = asset_root / external_id
    target_dir.mkdir(parents=True, exist_ok=True)

    for index, img_url in enumerate(image_urls, start=1):
        try:
            merged_headers = dict(headers)
            merged_headers.setdefault("Referer", referer)
            response = requests.get(img_url, headers=merged_headers, timeout=30)
            response.raise_for_status()
        except requests.RequestException as err:
            print(f"Failed to download image {img_url}: {err}")
            continue

        extension = guess_extension(
            img_url, response.headers.get("Content-Type"), response.content
        )
        filename = f"image_{index}{extension}"
        local_path = target_dir / filename
        local_path.write_bytes(response.content)

        assets.append(
            {
                "asset_type": "image",
                "url": img_url,
                "local_path": str(local_path),
                "metadata": {
                    "order": index,
                    "content_type": response.headers.get("Content-Type"),
                    "size_bytes": len(response.content),
                },
            }
        )
    return assets


_KST = timezone(timedelta(hours=9))
_NON_DIGIT_RE = re.compile(r"[^0-9]")
_MOBILE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Linux; Android 13; Pixel 6) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Mobile Safari/537.36"
    )
}


def _parse_dcinside_datetime(value: str | None) -> str | None:
    if not value:
        return None
    cleaned = value.strip()
    if not cleaned:
        return None
    cleaned = cleaned.replace("년", ".").replace("월", ".").replace("일", "")
    cleaned = cleaned.replace(":", " ")
    parts = _NON_DIGIT_RE.sub(" ", cleaned).split()
    if len(parts) < 5:
        return None
    try:
        dt = datetime(
            int(parts[0]),
            int(parts[1]),
            int(parts[2]),
            int(parts[3]),
            int(parts[4]),
        )
    except ValueError:
        return None
    return dt.replace(tzinfo=_KST).astimezone(timezone.utc).isoformat()


def _fetch_dcinside_comments(post_url: str) -> List[dict]:
    parsed = urlparse(post_url)
    query = parse_qs(parsed.query)
    board_id = query.get("id", [None])[0]
    post_no = query.get("no", [None])[0]
    if not board_id or not post_no:
        return []

    mobile_url = f"https://m.dcinside.com/board/{board_id}/{post_no}"
    headers = dict(_MOBILE_HEADERS)
    headers.setdefault("Referer", post_url)

    try:
        response = requests.get(
            mobile_url,
            headers=headers,
            timeout=30,
            allow_redirects=False,
        )
    except requests.RequestException:
        return []

    if response.status_code != 200:
        return []

    soup = BeautifulSoup(response.text, "html.parser")
    comment_nodes = soup.select("ul.all-comment-lst > li")
    comments: List[dict] = []
    for node in comment_nodes:
        classes = node.get("class") or []
        if any(cls for cls in classes if cls.startswith("comment_write")):
            continue

        external_id = (
            node.get("data-no")
            or node.get("data-cno")
            or node.get("no")
            or node.get("data-no2")
        )
        if not external_id:
            node_id = node.get("id") or ""
            if node_id.startswith("comment_cnt_"):
                external_id = node_id.split("_")[-1]
        if not external_id:
            continue

        author = "unknown"
        author_node = node.select_one("a.nick")
        if author_node:
            ip_node = author_node.find(class_="ip")
            if ip_node:
                ip_node.extract()
            author_text = author_node.get_text(strip=True)
            if author_text:
                author = author_text

        content = ""
        content_node = node.select_one("p.txt") or node.select_one(".txt")
        if content_node:
            content = content_node.get_text(" ", strip=True)

        created_at = None
        date_node = node.select_one("span.date")
        if date_node:
            created_at = _parse_dcinside_datetime(date_node.get_text(strip=True))

        parent_external = node.get("data-parent") or node.get("parent")
        if parent_external in {"0", "", None}:
            parent_external = None

        is_deleted = False
        if classes and any("del" in cls for cls in classes):
            is_deleted = True
        if "삭제" in content:
            is_deleted = True

        metadata = {
            "depth": 1 if parent_external else 0,
        }
        order = node.get("ch")
        if order:
            metadata["order"] = order
        data_type = node.get("data-type")
        if data_type:
            metadata["data_type"] = data_type
        m_no = node.get("m_no") or node.get("data-m_no")
        if m_no:
            metadata["m_no"] = m_no

        comments.append(
            {
                "external_id": str(external_id),
                "author": author,
                "content": content,
                "created_at": created_at,
                "is_deleted": is_deleted,
                "metadata": metadata,
                "parent_external_id": str(parent_external) if parent_external else None,
            }
        )
    return comments
