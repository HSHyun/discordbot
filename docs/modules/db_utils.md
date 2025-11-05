# db_utils.py 요약

## 역할
- `source`, `item`, `item_asset`, `item_summary`, `comment` 테이블과 인덱스를 생성해 데이터베이스 상태를 보장한다.
- 소스 설정(`SourceConfig`)을 기준으로 레코드를 조회하거나 새로 삽입한다.
- 게시물 메타데이터를 구성해 `item` 테이블에 upsert하고, 첨부 자산 목록을 교체한다.
- 요약 결과를 `item_summary` 테이블에 기록하고, 원문/이미지 정보를 `item.metadata`에 병합한다.

## 핵심 구성 요소
- **`SourceConfig`**: 소스 식별 코드, 크롤 주기, 파서 이름, 메타데이터를 담는 데이터클래스.
- **`ensure_tables`**: 테이블/인덱스 생성 및 `is_active` 기본값 정비.
- **`get_or_create_source`**: 소스 코드로 레코드를 조회하거나 없으면 새로 추가하고 생성 여부 반환.
- **`upsert_items`**: 게시물 정보를 upsert하고 새로 삽입되었는지 여부를 함께 돌려준다.
- **`replace_item_assets`**: 첨부된 이미지를 전부 교체하고 `item_asset` 행을 삽입.
- **`update_item_with_summary`**: 요약 텍스트를 `item_summary`에 저장하고, 원문/이미지 개수/오류 상태를 `item.metadata`에 반영.
- **`seed_sources_from_file`**: JSON 파일을 읽어 기본 소스 구성을 `source` 테이블에 주입.
- **`delete_item`**: 비활성 대상이나 비디오 게시물 등 필요 시 `item`과 연결된 에셋을 삭제.

## 외부 의존성
- `psycopg2-binary`: 데이터베이스 커넥션/커서, `Json`, `RealDictCursor` 사용.
- `crawl_dcinside.Post`: upsert 시 게시물 데이터를 읽기 위한 타입.

## 운영 시 유의 사항
- 함수 호출 후 커밋을 수행하므로, 상위 레이어에서 명시적 트랜잭션을 관리할 필요가 없다.
- `replace_item_assets`는 기존 첨부 파일을 모두 삭제하므로, 스냅샷 보관이 필요한 경우 별도 백업 전략이 필요하다.
- `delete_item`은 `item_asset`이 `ON DELETE CASCADE`로 연결되어 있어 후속 정리가 자동으로 이뤄진다.

## 확장 아이디어
- `SourceConfig`를 다른 모듈에서도 공유해 다중 소스를 등록할 수 있도록 설정 레지스트리를 구축.
- upsert 시 중복 판별 로직을 커스터마이즈할 수 있도록 전략 패턴을 도입.
- 에셋 메타데이터에 썸네일 경로나 해시값을 추가해 중복 파일 저장을 줄인다.
