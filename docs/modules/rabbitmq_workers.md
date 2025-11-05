# RabbitMQ 기반 요약 워커

## 개요
- `dcinside_worker.py`, `reddit_worker.py`는 RabbitMQ 큐에 적재된 `item_id`를 받아 상세 수집과 Codex 요약을 수행한다.
- Cloud Run 등 HTTP 기반 환경에서 동작하도록 설계되어 있으며, 요청 1회당 큐에서 최대 1건을 소비한다.

## 실행 흐름
1. 큐에 `{ "item_id": 123 }` 형태의 JSON 메시지가 들어간다.
2. 외부 트리거가 워커 HTTP 엔드포인트에 `POST /` 요청을 보낸다.
3. 워커는 큐에서 메시지를 하나 꺼내 DB에서 해당 아이템을 조회한다.
4. 상세 페이지/Reddit API를 호출해 본문·댓글·이미지를 확보하고, Codex 요약을 생성한다.
5. `item_summary`에 요약을 저장하고, 원문 텍스트/댓글/에셋을 갱신한다.
6. 작업 결과를 JSON으로 응답한다. 큐가 비어 있으면 `204 No Content`를 반환한다.

## 환경 변수
- `RABBITMQ_URL`: AMQP URL (기본 `amqp://guest:guest@localhost:5672/%2F`).
- `DCINSIDE_QUEUE` / `REDDIT_QUEUE`: 큐 이름. 기본 `dcinside_items`, `reddit_items`.
- `PORT`: HTTP 리스닝 포트 (Cloud Run은 자동 제공).
- DB 접속 (`DB_NAME`, `DB_USER`, `DB_PASSWORD`, `DB_HOST`, `DB_PORT`).
- Codex 설정 (`CODEX_MODEL`, `CODEX_TIMEOUT`, `CODEX_MAX_TEXT`, `CODEX_DEBUG`).

## 배포 팁
- Cloud Run에 배포할 경우 `concurrency=1`, `max-instances`를 서비스별로 조정하면 메시지 단위 병렬 처리가 용이하다.
- 메시지 발행 측에서 큐에 push한 뒤 동일한 수만큼 HTTP 요청을 보내면 자동 확장 시나리오를 구현할 수 있다.
- 재시도 불가 메시지는 `MessageHandlingError(requeue=False)`로 표기돼 큐에서 제거되고, DB에 오류 원인이 기록된다.
