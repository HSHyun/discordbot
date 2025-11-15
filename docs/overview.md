# 프로젝트 개요

이 프로젝트는 여러 커뮤니티/뉴스 사이트에서 새 소식을 주기적으로 수집하고, 요약한 뒤 디스코드 채널에 공유하기 위한 자동화 파이프라인을 구축하는 것을 목표로 한다. 현재는 DCInside 특이점 추천 게시판을 단일 소스로 사용하며, 구조를 확장해 다양한 사이트로 범위를 넓히는 것을 염두에 두고 있다.

## 현재 저장소 구성
- `crawl_dcinside.py`: DCInside 추천 게시판 목록을 수집하는 크롤러.
- `store_dcinside_posts.py`: 파이프라인 엔트리 포인트. 게시물을 upsert한 뒤 RabbitMQ에 `item_id`를 발행한다.
- `db_utils.py`: 테이블 보장, 소스 레코드 관리, 아이템 upsert/에셋 갱신 등 데이터베이스 관련 로직.
- `content_fetcher.py`: 워커가 게시물 본문/이미지를 다시 수집할 때 사용하는 헬퍼.
- `gemini_summary.py`: Gemini API를 호출해 요약을 생성하고 결과를 정리.
- `main.py`: 요약된 게시물을 불러와 디스코드 봇으로 전송.
- `data/`: 게시물 이미지 등 에셋이 저장되는 디렉터리.
- `docs/`: 프로젝트 문서.
- `config/sources.json`: 기본으로 등록할 소스 정보를 담은 시드 파일.
- `requirements.txt`: 파이썬 의존성 정의 (BeautifulSoup4, psycopg2-binary, requests, Pillow, discord.py).

## 상위 목표
- 각 소스별 크롤러를 주기적으로 실행해 신규 게시물을 데이터베이스에 누적한다.
- 게시물 본문과 이미지를 기반으로 LLM 요약을 생성한다.
- 일정 주기(예: 매시간/매일)마다 중요한 업데이트를 선별해 디스코드에 전송한다.
- 소스 테이블에서 크롤링 간격, 활성화 여부, 메타데이터를 관리해 운영 편의성을 높인다.

## 시스템 구성 요소
- **크롤러(`crawl_dcinside.py`, `crawl_reddit.py`)**: 대상 사이트 API/HTML을 파싱해 게시물 리스트를 수집한다.
- **파이프라인 엔트리(`store_dcinside_posts.py`, `store_reddit_posts.py`)**: 소스 설정을 확인하고 게시물을 upsert한 뒤 RabbitMQ에 작업을 발행한다.
- **데이터베이스 유틸(`db_utils.py`)**: 테이블 생성/정비, 소스 레코드 관리, 게시물 upsert, 에셋 교체, 요약 결과 저장.
- **콘텐츠 수집(`content_fetcher.py`)**: 워커가 상세 페이지 텍스트/이미지를 추출하고 저장할 때 사용한다.
- **요약 모듈(`gemini_summary.py`)**: Gemini API와 통신해 한국어 요약을 생성.
- **Discord 봇(`main.py`)**: DB에서 요약된 콘텐츠를 읽어 임베드 형태로 디스코드 채널에 게시한다.
- **PostgreSQL DB**: 소스 설정, 게시물, 에셋 정보를 저장한다.
- **RabbitMQ**: `item_id` 작업을 큐잉해 워커가 비동기적으로 처리할 수 있도록 한다.
- **LLM 서비스(Gemini API)**: 워커가 본문과 이미지를 요약할 때 사용한다.

## 데이터 흐름 요약
1. 크론 잡이나 워크플로 스케줄러가 각 소스별 수집 스크립트를 실행한다.
2. 스크립트는 `source` 테이블을 확인해 비활성 소스는 건너뛰고, 활성 소스만 크롤링한다.
3. 신규 게시물은 `item` 테이블에 upsert되고, `item_id`가 RabbitMQ 큐에 발행된다.
4. 워커(`reddit_worker.py`, `dcinside_worker.py`)가 큐 메시지를 소비해 상세 본문/댓글/이미지를 수집하고 Gemini API로 요약을 생성한다.
5. 요약 결과는 `item_summary` 테이블에 저장되고, Discord 봇이 최신 요약 N개를 읽어 채널에 게시한다.
6. 향후 자동 알림 로직(예: “특정 키워드 포함”, “추천 수 급증”)으로 확장해 중요한 소식을 별도 채널로 보낼 수 있다.
7. 비디오로 판별된 게시물은 워커 단계에서 제외된다.

## 데이터베이스 구조 개요
- `source`
  - `code`, `name`, `url_pattern`, `parser`, `fetch_interval_minutes`, `is_active`, `metadata`
  - 수집 스크립트는 존재하지 않는 경우 기본 행을 생성하되 `is_active=FALSE`로 두고, 운영자가 활성화해야만 실제 크롤링 수행
- `item`
  - `source_id`, `external_id`, `url`, `title`, `author`, `content`, `published_at`, `metadata`
  - 원문 텍스트/요약 보조 정보는 `metadata`에 보존하며, 요약 결과는 별도 `item_summary`에서 관리
- `item_summary`
  - `item_id`, `model_name`, `summary_text`, `created_at`, `meta`
  - 게시물별·모델별 요약을 다중으로 저장하고, `meta`에 입력 길이/이미지 개수 등 부가 정보를 기록
- `item_asset`
  - 게시물 이미지 등 첨부 자원을 저장하며, `metadata`에 순서/크기 정보를 포함

## 스케줄링 및 운영 포인트
- **크롤링 주기**: `fetch_interval_minutes` 컬럼을 활용하거나 별도 스케줄러에서 소스별 주기를 설정한다.
- **활성화 제어**: `is_active`를 `TRUE`로 전환해야 해당 소스가 수집된다.
- **에러 로깅**: `metadata.summary_error` 등에 실패 원인을 적재해 재시도 정책을 수립한다.
- **확장 전략**: 새로운 사이트를 추가할 때는
  1. 크롤러 모듈 추가 (`crawl_xxx.py` 등)
  2. `store_xxx_posts.py`와 비슷한 수집 스크립트 작성
  3. `source` 레코드 등록 후 `is_active=TRUE` 설정
- **테스트 모드 제약**: `REDDIT_MIN_POST_AGE_HOURS`, `DCINSIDE_MIN_POST_AGE_HOURS`, `*_MAX_POSTS` 환경 변수를 통해 수집 범위를 줄여 시험 실행을 할 수 있다.

## 향후 작업 체크리스트
- [x] Reddit 소스 크롤러 설계 및 구현
- [ ] 추가 소스(RSS, 뉴스레터 등) 크롤러 설계 및 구현
- [ ] 소스별 요약 전략 개선 (핵심 문구 추출, 태그 분류 등)
- [ ] Discord 알림 로직 고도화 (중요도 필터, 알림 주기 조정)
- [ ] 문서 기반 워크플로 확립 (ADR 작성, 다이어그램 추가, Gemini와 연동된 자동 요약 스크립트)
- [ ] 배포/스케줄링 환경 구축 (예: cron, GitHub Actions, Airflow 등)

이 문서를 Obsidian Vault나 버전 관리(Git)와 연결해 지속적으로 업데이트하면, 문서 기반으로 기능을 설계하고 Gemini에게 구현 골격을 맡기는 워크플로를 안정적으로 운영할 수 있다.
