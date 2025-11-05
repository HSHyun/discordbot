# store_dcinside_posts.py 요약

## 역할
- `.env`를 통해 환경 값을 불러오고 소스별 기본 설정을 정의한다.
- `db_utils`, `content_fetcher`, `codex_summary` 모듈을 orchestration 해 전체 파이프라인을 수행한다.
- 허용된 말머리만 필터링한 뒤 DB 상태와 소스 활성 여부를 점검하고 작업을 실행한다.

- **환경 변수 로더**: `.env` 파일을 읽어 DB 접속 정보와 Codex 설정을 초기화.
- **소스 설정 상수**: `SourceConfig` 인스턴스로 코드/이름/패턴/메타데이터를 정의.
- **`process_details`**: 콘텐츠·댓글 수집, 에셋 저장, Codex 요약 호출을 담당 모듈과 협업해 수행하며 비디오 링크가 발견되면 해당 아이템을 삭제하고 건너뜀. 생성된 요약은 `item_summary`에 저장한다.
- 실행 초기에 `seed_sources_from_file()`로 기본 소스 구성을 등록하고, 필요 시 관리자 승인을 통해 활성화한다.
- **`main`**: 크롤링 대상 필터링, 테이블 보장, 소스 활성 확인 후 세부 작업 호출.

## 외부 의존성
- `psycopg2-binary`: PostgreSQL 연결.
- `requests`: 상세 페이지 요청 시 예외 처리용 타입 사용.
- `Codex CLI`: 요약 생성(외부 명령 실행 필요).
- 내부 모듈 `db_utils`, `content_fetcher`, `codex_summary`.

## 운영 시 유의 사항
- `source.is_active`가 `TRUE`인 경우에만 크롤링과 요약이 진행된다.
- 이미지 다운로드 경로는 `data/assets/{external_id}`로 고정되어 있으므로 저장 공간을 주기적으로 관리해야 한다.
- 모바일 페이지에서 댓글을 함께 수집하므로 구조 변경 시 파서 업데이트가 필요하다.
- Codex 요약 실패 시 원문 텍스트 일부만 저장되며, 오류 메시지는 `metadata.summary_error`에 기록된다.
- 비디오 URL이 포함된 게시물은 요약과 저장을 생략하고 `item` 레코드를 삭제한다.

## 확장 아이디어
- SourceConfig를 외부 YAML/JSON에서 불러오도록 확장해 멀티 소스 대응.
- `process_details`를 비동기 실행(쓰레드/async)으로 전환해 처리 속도 개선.
- CodexConfig를 소스 메타데이터에 포함시켜 소스별 다른 프롬프트를 허용.
