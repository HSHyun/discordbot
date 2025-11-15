# store_dcinside_posts.py 요약

## 역할
- `.env`를 읽어 DB 접속 정보, 게시물 제한(`DCINSIDE_MAX_POSTS`), 최소 경과 시간(`DCINSIDE_MIN_POST_AGE_HOURS`), RabbitMQ 큐 이름(`DCINSIDE_QUEUE`) 등을 초기화한다.
- DCInside 추천 게시판 목록을 크롤링해 허용된 말머리/시간 조건을 통과한 게시물만 선별한다.
- `upsert_items()`로 `item` 테이블을 갱신한 뒤, 새로 삽입된 `item_id`를 RabbitMQ 큐에 발행해 후속 워커가 상세 수집·요약을 수행하도록 트리거한다.

## 주요 동작
1. `fetch_posts()` 결과에서 허용된 말머리만 남기고, `DCINSIDE_MIN_POST_AGE_HOURS`가 설정돼 있으면 해당 시간 이상 지난 게시물만 유지한다.
2. `DCINSIDE_MAX_POSTS`가 양수라면 테스트 편의를 위해 그 수만큼만 잘라서 처리한다.
3. DB 테이블을 보장(`ensure_tables`)하고 소스 구성을 조회/생성(`get_or_create_source`), 비활성 소스는 건너뛴다.
4. `upsert_items()`로 게시물을 저장하고, `inserted=True`였던 `item_id` 목록을 `DCINSIDE_QUEUE` 큐에 JSON 메시지(`{"item_id": ...}`)로 발행한다.
5. 실제 본문·이미지·댓글 수집과 Gemini 요약은 `dcinside_worker.py`가 큐를 소비하면서 수행한다.

## 외부 의존성
- `psycopg2-binary`: PostgreSQL 연결.
- `pika`: RabbitMQ 퍼블리싱.
- 내부 모듈 `crawl_dcinside`, `db_utils`.

## 운영 시 유의 사항
- `source.is_active`가 `TRUE`인 경우에만 큐 발행이 이루어진다.
- `RABBITMQ_URL`과 `DCINSIDE_QUEUE`를 `.env`에 정의해야 큐로 메시지를 보낼 수 있으며, 스크립트 실행 시 해당 큐가 없으면 자동으로 선언된다.
- `DCINSIDE_MIN_POST_AGE_HOURS`를 활용하면 일정 시간 후에만 게시물을 워커에 넘겨, 초기 댓글 누락을 줄일 수 있다.
- 테스트할 때는 `DCINSIDE_MAX_POSTS`로 크롤링 건수를 제어할 수 있다.

## 확장 아이디어
- 다중 갤러리를 지원하도록 `SUBJECT` 필터/소스 구성을 외부 설정으로 분리.
- 게시물 필터링 기준(예: 추천 수, 댓글 수)을 환경 변수로 추가해 상황에 맞게 조정.
- 발행 실패 시를 대비해 DLQ(Dead Letter Queue)나 재시도 로직을 도입.
