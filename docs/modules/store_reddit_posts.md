# store_reddit_posts.py 요약

## 역할
- `.env`를 로드해 DB 접속 정보, 서브레딧 목록, 수집 제한(`REDDIT_MAX_POSTS`, `REDDIT_MIN_POST_AGE_HOURS`), RabbitMQ 큐 이름(`REDDIT_QUEUE`) 등을 초기화한다.
- Reddit `/r/<subreddit>/new` 피드를 호출해 최신 게시물 목록을 확보하고, 비디오 게시물은 즉시 건너뛴다.
- `upsert_items()`로 `item` 테이블을 갱신하고, 새로 삽입된 `item_id`를 RabbitMQ 큐에 발행해 워커가 상세 수집·요약을 수행하도록 트리거한다.

## 핵심 흐름
1. `fetch_posts_for_subreddit()`가 `limit=REDDIT_MAX_POSTS`(기본 50)과 `max_age_hours=REDDIT_MIN_POST_AGE_HOURS`를 인자로 넘겨 Reddit API에서 게시물을 가져온다.
2. 비디오 게시물(`post.is_video` 또는 `contains_video_url`)은 큐에 올리지 않도록 제외한다.
3. DB 테이블을 보장하고 소스 구성을 조회/생성한다. 비활성 소스(`is_active=FALSE`)는 건너뛴다.
4. `upsert_items()` 호출 결과 중 새로 삽입된 `item_id`를 모아 `REDDIT_QUEUE` 큐에 `{"item_id": ...}` 형식의 메시지로 발행한다.
5. Reddit 워커(`reddit_worker.py`)가 큐를 소비하면서 본문/댓글/이미지 수집과 Gemini 요약을 수행한다.

## 외부 의존성
- `psycopg2-binary`: PostgreSQL 연결.
- `pika`: RabbitMQ 퍼블리셔.
- 내부 모듈 `crawl_reddit`, `db_utils`, `content_fetcher`.

## 운영 시 유의 사항
- `REDDIT_MAX_POSTS`와 `REDDIT_MIN_POST_AGE_HOURS`로 테스트/운영 상황에 맞게 수집 범위를 조정할 수 있다.
- `RABBITMQ_URL`과 `REDDIT_QUEUE` 값을 `.env`에 설정해야 워커로 메시지를 전달할 수 있으며, 스크립트 실행 시 큐가 없으면 자동으로 선언된다.
- 큐 발행은 새로 삽입된 게시물에 대해서만 수행하므로, 이미 처리된 게시물은 중복으로 워커에 전달되지 않는다.

## 확장 아이디어
- 서브레딧 목록을 환경 변수나 외부 설정 파일로 분리해 동적으로 관리.
- 메시지에 우선순위나 재처리 플래그를 추가해 워커가 처리 전략을 바꿀 수 있도록 확장.
- 댓글/이미지 수집 대상 서브레딧을 라우팅 키별로 분리해 워커를 스케일 아웃.
