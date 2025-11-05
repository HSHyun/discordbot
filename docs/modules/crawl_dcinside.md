# crawl_dcinside.py 요약

## 역할
- DCInside 특이점 추천 게시판 목록 페이지를 요청해 게시물 정보를 파싱한다.
- 추출한 데이터를 `Post` 데이터클래스로 래핑해 상위 로직(`store_dcinside_posts.py`)에서 재사용할 수 있도록 공급한다.
- 단독 실행 시 표 형식으로 게시물 정보를 출력해 크롤러 동작을 점검할 수 있다.

## 핵심 흐름
1. `fetch_posts()`가 `urllib.request.urlopen`으로 목록 HTML을 가져온다.
2. BeautifulSoup으로 DOM을 파싱하고, 게시물 행(`tr.ub-content.us-post`)을 순회한다.
3. 번호, 제목, 작성자, 댓글 수, 날짜, 조회 수 등을 추출해 `Post` 인스턴스로 변환한다.
4. 호출 측은 제너레이터를 통해 필요한 만큼의 게시물을 받아 처리한다.

## 외부 의존성
- `BeautifulSoup4`: HTML 파싱.
- `urllib.request`: HTTP 요청.

## 확장 아이디어
- 다국어/다른 게시판 대응을 위해 헤더나 URL을 파라미터화.
- 비정상 응답 시 재시도 정책과 로깅 추가.
