# store_reddit_posts.py 요약

## 역할
- Reddit 서브레딧 게시물을 수집해 PostgreSQL에 저장하고 Codex 요약을 생성한다.
- `db_utils`, `content_fetcher`, `codex_summary` 모듈과 협업해 전체 파이프라인을 orchestration 한다.
- 서브레딧별로 분리된 이미지 디렉터리를 유지하며 요약 결과를 `item` 테이블에 반영한다.
- 댓글을 `comment` 테이블에 저장하고, 상위 댓글을 Codex 입력에 포함한다.
- 비디오 게시물 또는 비디오 URL이 감지된 게시물은 upsert 대상에서 제외한다.

## 핵심 흐름
1. 환경 변수를 로드해 DB/Codex 설정을 초기화하고, `seed_sources_from_file()`로 기본 소스 구성을 등록한다.
2. `build_source_config()`가 서브레딧별 `SourceConfig`를 생성한다.
3. `fetch_reddit_posts()`로 최신 게시물을 가져오되 최대 5시간 이내 게시물만 취득하고 `upsert_items()`로 DB에 저장한다.
4. `process_jobs()`에서 텍스트를 구성하고 이미지(있을 경우)를 `data/reddit/{subreddit}/` 하위에 다운로드하며, 댓글을 `replace_item_comments()`로 갱신한다.
5. Codex 요약 입력에 댓글 주요 내용(최대 5개)을 덧붙여 요약 품질을 높인다.
6. Codex 요약을 생성해 `item_summary` 테이블에 기록(`update_item_with_summary`)하고, 원문·이미지 정보를 `item.metadata`에 남긴다.
7. 게시물이 비디오로 판단될 경우 upsert 단계에서 제외한다.

## 외부 의존성
- `psycopg2-binary`: PostgreSQL 연결.
- 내부 모듈 `crawl_reddit`, `db_utils`, `content_fetcher`, `codex_summary`.

## 운영 시 유의 사항
- `SourceConfig` 생성 시 `fetch_interval_minutes=60`으로 기본 주기를 설정한다.
- 새로 등록된 소스는 `is_active=FALSE` 상태이므로 DB에서 활성화해야 크롤링이 진행된다.
- Reddit API 429 대비를 위해 User-Agent 헤더를 반드시 포함한다.
- 댓글은 최대 50개까지 저장하며, 트리 구조는 `parent_id` 참조로 복원 가능하다.
- 이미지가 없는 게시물은 요약 텍스트만으로 처리하거나, Codex가 비어 있는 본문에 대해 오류를 던질 경우 원문 텍스트 일부를 저장한다.
- 비디오 전용 게시물은 현재 파이프라인에서 제외된다.

## 확장 아이디어
- 서브레딧 목록을 환경 변수나 설정 파일로 외부화.
- 업보트/댓글 수 기준으로 중요도를 평가해 Discord 알림 우선순위를 조정.
- Reddit 전용 요약 프롬프트를 적용하거나, AI 모델을 다르게 선택할 수 있도록 설정 분리.
