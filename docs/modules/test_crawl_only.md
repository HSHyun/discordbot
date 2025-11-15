# test_crawl_only.py 요약

## 역할
- 활성화(`is_active=TRUE`) 상태의 소스에 대해서만 게시글과 댓글을 수집하고, 요약 없이 `item`·`comment` 테이블을 업데이트하는 크롤링 검증 스크립트.
- 실제 파이프라인이 사용하는 크롤러/DB 유틸 함수를 그대로 호출해 동작 여부를 점검한다.

## 핵심 흐름
1. `.env`에서 DB 접속 정보를 읽고 `ensure_tables()`로 스키마를 보장한다.
2. `source` 테이블에서 플랫폼 및 소스 코드 필터를 반영해 활성화된 행을 조회한다(이전에 `seed_sources_from_file`로 기본 소스를 등록).
3. Reddit 소스는 `fetch_posts_for_subreddit()`으로 최신 게시물을 가져오고, 비디오 게시물을 제외한 뒤 `upsert_items()`와 `_normalise_reddit_comments()`로 댓글을 저장한다.
4. DCInside 소스는 `fetch_posts()`로 목록을 가져온 뒤 허용된 말머리만 유지하고, `fetch_post_body()`로 본문·댓글을 수집해 `replace_item_comments()`로 저장한다.
5. 각 소스별로 처리 건수와 신규 삽입 수, 댓글 수를 콘솔에 로그로 남긴다.

## 주요 CLI 옵션
- `--platform {reddit|dcinside|all}`: 대상 플랫폼 필터.
- `--source CODE`: 특정 `source.code` 하나만 테스트.

## 운영 시 유의 사항
- 요약(Stage) 없이 게시물과 댓글만 저장하므로 테스트를 반복해도 Gemini 비용이 발생하지 않는다.
- Reddit 테스트를 위해서는 OAuth 자격 증명이 필요하며, DCInside는 비디오 게시물을 건너뛴다.
- 실행 결과는 실제 DB에 반영되므로 테스트 후 정리가 필요할 수 있다.

## 확장 아이디어
- 처리 결과를 표 형태로 출력하거나 CSV 로그로 저장.
- 에러 발생 시 재시도·스킵 전략을 옵션으로 제공.
- 댓글/게시물 수가 기준 이하일 때 경고를 출력하는 검증 로직 추가.
