#!/usr/bin/env python3
"""DCInside 특이점 갤러리의 추천 게시물을 가져와 출력합니다."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterator
from urllib.parse import urljoin
from urllib.request import Request, urlopen

from bs4 import BeautifulSoup

TARGET_URL = "https://gall.dcinside.com/mgallery/board/lists/?id=thesingularity&exception_mode=recommend"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}


@dataclass(slots=True)
class Post:
    external_id: str
    number: str
    subject: str
    title: str
    url: str
    comments: str
    writer: str
    date_display: str
    date_iso: str
    views: str
    recommends: str


def extract_subject(cell) -> str:
    if cell is None:
        return ""
    inner = cell.select_one(".subject_inner")
    if inner:
        return inner.get_text(strip=True)
    return cell.get_text(strip=True)


def fetch_posts() -> Iterator[Post]:
    """추천 게시판 목록에서 게시물을 순차적으로 반환합니다."""
    req = Request(TARGET_URL, headers=HEADERS)
    with urlopen(req) as resp:  # nosec: B310 - 신뢰된 출처에서만 호출함
        html = resp.read()

    soup = BeautifulSoup(html, "html.parser")
    for row in soup.select("tr.ub-content.us-post"):
        external_id = row.get("data-no", "")
        number = row.select_one("td.gall_num")
        subject_cell = row.select_one("td.gall_subject")
        subject = extract_subject(subject_cell)
        title_cell = row.select_one("td.gall_tit")
        link_tag = title_cell.select_one("a") if title_cell else None
        writer = row.select_one("td.gall_writer")
        date_cell = row.select_one("td.gall_date")
        views = row.select_one("td.gall_count")
        recommends = row.select_one("td.gall_recommend")
        comments_tag = title_cell.select_one("span.reply_num") if title_cell else None

        yield Post(
            external_id=external_id,
            number=number.get_text(strip=True) if number else "",
            subject=subject,
            title=link_tag.get_text(" ", strip=True) if link_tag else "",
            url=urljoin(TARGET_URL, link_tag["href"])
            if link_tag and link_tag.has_attr("href")
            else "",
            comments=comments_tag.get_text(strip=True) if comments_tag else "",
            writer=writer.get_text(" ", strip=True) if writer else "",
            date_display=date_cell.get_text(strip=True) if date_cell else "",
            date_iso=date_cell.get("title", "") if date_cell else "",
            views=views.get_text(strip=True) if views else "",
            recommends=recommends.get_text(strip=True) if recommends else "",
        )


def main() -> None:
    for post in fetch_posts():
        headline = f"{post.number:<7} | {post.title}"
        print(headline)
        print(f"    Subject   : {post.subject}")
        if post.comments:
            print(f"    Comments  : {post.comments}")
        print(f"    Writer    : {post.writer}")
        print(f"    Date      : {post.date_display}")
        if post.date_iso:
            print(f"    Date (ISO): {post.date_iso}")
        print(f"    Views     : {post.views}")
        print(f"    Recommends: {post.recommends}")
        print(f"    URL       : {post.url}\n")


if __name__ == "__main__":
    main()
