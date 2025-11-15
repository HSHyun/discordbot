# Reddit 소스 추가 설계 초안

## 목표
- `/r/OpenAI`, `/r/singularity`, `/r/ClaudeAI`의 `/new` 피드에서 최신 게시물을 주기적으로 수집한다.
- 게시물 메타데이터(제목, URL, 작성자, 게시 시간, 업보트, 댓글, 댓글 수 등)를 DB에 저장한다.
- Gemini를 통해 각 게시물의 이미지를 포함해 요약하여 공유한다.
- Gemini를 통해 의미 있는 새 소식이 있는지 정리하고 Discord로 배포할 준비를 한다.

## 요구 사항
- Reddit HTML을 파싱하거나 공식 API를 활용해 게시물 목록과 세부 정보를 확보한다.
- 기존 DCInside 파이프라인과 동일한 DB 스키마(`source`, `item`, `item_asset`)를 재사용한다.
- 만약 DCInside와 동일한 DB가 아닌 메타데이터는 맨 마지막 속성인 metadata에 추가한다.
- `source` 테이블에 서브레딧별로 세 개의 행을 추가하며, `metadata`에 subreddit 이름, 타깃 URL, 추가 옵션을 저장한다.
- 요약은 기본적으로 게시물 본문과 댓글을 대상으로 수행하며, 이미지가 있을 경우 다운로드한다.

## 수집 대상 항목
- `external_id`: Reddit 게시물 ID 
- `url`: 원문 URL (`https://www.reddit.com/...`)
- `title`: 게시물 제목
- `author`: 작성자
- `content`: 게시글 본문 (없을 시 공백)
- `published_at`: 게시글 작성 시간
- `first_seen_at`: 크롤링 시행한 시간
-  `metadata`: `item` 테이블에 공통 컬럼 이외의 reddit용 정보
     - `score`: 업보트 수
     - `num_comments`: 댓글 수
     - `permalink`: /r/.../ 형태의 Reddit 내부 링크
     - `is_self`: 텍스트 게시물 여부
     - `flair`: 게시글에 붙은 플레어


## 흐름 개요
1. `crawl_reddit.py` (신규): Reddit 피드를 호출해 게시물 리스트를 반환.
2. `store_reddit_posts.py` (신규 또는 공용 파이프라인 확장):
   - `SourceConfig`를 통해 `source` 레코드 생성 여부 확인
   - `upsert_items` 호출로 `item` 테이블에 저장
   - 본문/이미지 수집 후 Gemini 요약 실행
3. Discord 봇 또는 차후 알림 로직에서 Reddit 게시물을 포함한 요약을 전달.

## 기술 고려사항
- Reddit은 비로그인 접속에도 HTML을 제공하나, 빈도가 높을 경우 API 키 사용을 권장.
- 공식 API를 사용할 경우 `requests` 대신 `praw` 같은 라이브러리를 도입할지 검토.
- 이미지 다운로드: Reddit의 썸네일/미디어 URL을 파싱해야 하며, 갤러리 포맷에 유의.
- 시간대 변환: `published_at`를 `datetime`으로 변환해 `item.published_at`에 저장.

## DB / 메타데이터 설계
- `source.metadata` 예시:
  ```json
  {
    "num_comments": 3,
    "permalink": "/r/OpenAI/news",
    "is_self": True,
    "flair" : "Question"
  }
  ```

- `item.metadata`에 공통된 컬럼들을 제외한 `Reddit`용 추가 데이터들은 모두 `source.metadata`에 저장.
- 요약이나 게시글의 특성을 파악하는데 다른 정보가 있으면 추가 가능 

## 작업 단계 제안
1. Reddit HTML 구조 분석 후 크롤러 초안 작성.
2. `docs/modules/`에 Reddit 관련 문서 추가.
3. Gemini 연동을 염두에 두고 `crawl_reddit.py`, `store_reddit_posts.py` 스캐폴드 생성을 요청.
4. `SourceConfig`를 3개 추가하고 `is_active=FALSE` 상태로 기본 레코드 생성.
5. 테스트 실행 후 `overview.md`와 문서 갱신.


## 추가 정보
1. `gemini_summary.py` (구 Codex) 모듈을 재사용하여 요약을 진행하면 좋겠음
2. `Reddit`의 `url`만 추가하면 해당 `url`에서도 가능하면 좋겠음
3. 먼저 만든 `dcinside`의 흐름을 가져와서 진행하면 좋겠음
4. 만들면서 테스트 하는건 좋은데 코드 내에서 실행하는 Gemini LLM같은 경우는 이미지와 함께 요약해달라고 하면 하나하나 요약하는데 시간이 걸리므로 timeout을 적절하게 설정해서 테스트 하고 설정하면 좋겠음
5. 최초 실행시 현재까지 있는 모든 게시물을 다 가져오는건 불가능하니까 최대 5시간 전까지 올라왔던 게시물만 일단은 크롤링하는 걸로 만들고, 이후에는 계속 서버를 돌리면서 차곡차곡 쌓을 예정.
6. 비디오 게시물은 저장과 요약 모두 건너뛰기.

## 상태 메모
- 2024-XX-XX: `crawl_reddit.py`, `store_reddit_posts.py` 구현 완료. `source` 레코드는 기본적으로 `is_active=FALSE`로 생성되므로 활성화 필요. 5시간 제한과 비디오 제외 로직 적용.
