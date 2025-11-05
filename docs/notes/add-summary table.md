# 배경
현재 게시물을 크롤링 후 codex를 이용해서 요약문을 생성한 뒤 저장하는건 해당 게시물 열에 저장하게 됨
이러면 요약한 정보에 대해서 분류나 필터링을 할 때 불편함이 있으므로 이를 새로운 테이블을 만들어서 따로 관리를 하고자함. 또한, 원문으로부터 별도의 프로세서가 생성한 2차 가공 데이터이므로 다른 테이블에 저장하여 관리하는게 더 맞는거 같음.

# 문제
1. 현재 게시물의 요약 정보는 다른 테이블에 별도 저장하지 않고 해당 열에 저장됨
2. 이런 경우 요약정보를 가지고 또 다른 가공 데이터를 만들기에 힘듬

# 요구사항
1. 크롤링 파이프라인은 기존과 동일하게 게시물/댓글을 수집하되, Codex 등으로 생성한 요약문을 새로운 테이블에 분리 저장한다.
2. 새로운 테이블은 `item_summary`로 하고, 스키마/제약은 아래와 같이 정의한다.
	- `id` BIGSERIAL PRIMARY KEY — 요약 레코드 식별자
	- `item_id` BIGINT NOT NULL REFERENCES item(id) ON DELETE CASCADE — 어떤 게시물의 요약인지
	- `model_name` TEXT NOT NULL — 사용한 요약 모델명(예: `gpt-5-codex`, `gmini`)
	- `summary_text` TEXT NOT NULL — 본문·댓글을 기반으로 생성된 요약문
	- `created_at` TIMESTAMPTZ NOT NULL DEFAULT NOW() — 요약 생성 시각
	- `meta` JSONB NOT NULL DEFAULT '{}'::jsonb — 입력 길이, 댓글 반영 여부, 오류 메시지 등 추가 메타데이터 저장
	- 제약/인덱스: `INDEX (item_id)`, 필요 시 `INDEX (created_at)`
3. `meta` 컬럼 활용 예시: `{ "input_chars": 3500, "comments_used": true, "error": null }`
4. 요약 생성 실패나 재시도 관리 정책(상태 플래그, 실패 기록 등)을 `item_summary`의 `meta` 또는 별도 로그로 관리한다.
5. 동일 게시물에 대해 여러 요약 모델을 저장할 수 있도록 `model_name` 조합에 대한 고유 제약은 두지 않는다.
