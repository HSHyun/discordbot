# Gemini 요약 전환 메모

## 배경
- 전체 파이프라인이 Codex CLI를 서브프로세스로 실행하는 `codex_summary.py`에 의존하고 있음.
- Codex 호출은 CLI 설치·인증이 필요하고, 모델을 바꿀 때마다 프롬프트/구성을 재작성해야 함.
- Google Gemini API 키를 확보했으므로 HTTP 기반 요약 모듈로 교체할 수 있음.
- 개인 프로젝트라 요청량이 크지 않지만, 무료/저가 티어 한도 때문에 모델 우선순위 제어가 필요함.

## 목표
1. Codex 관련 코드/문서를 모두 제거하고 Gemini API 전용 모듈을 도입한다.
2. 초기에는 `gemini-2.5-flash`를 기본 모델로 사용하되, 한도 초과 시 `gemini-2.5-flash-lite`로 자동 폴백한다.
3. 폴백 로직은 향후 저사양 모델을 추가할 수 있도록 리스트 기반 우선순위 구조로 설계한다.

## 요구사항
- `.env`에 존재하는 `GEMINI_API_KEY`를 활용하고, 필요 시 `GEMINI_MODEL_PRIORITIES`(예: `gemini-2.5-flash,gemini-2.5-flash-lite`)를 추가한다.
- 기존 `CodexConfig` → `SummarizerConfig` 등 중립 이름으로 변경하고, 모델 우선순위·API 키·타임아웃·텍스트 길이 등을 포함한다.
- 텍스트/이미지를 입력받아 Gemini Generative Language API `generateContent` 엔드포인트로 요청하는 `gemini_summary.py`(가칭)를 새로 작성한다.
- API 응답의 `429`/quota exceeded 오류를 감지해 다음 모델로 재시도하고, 모든 모델이 실패하면 가장 최근 오류로 `SummaryError`를 발생시킨다.
- 쿨다운 전략: 한도 초과 모델은 일정 시간(예: 10분) 동안 우선순위에서 제외하고, 시간이 지나면 자동 복귀시키는 간단한 메모리 구조를 도입한다.

## 구현 아이디어
- 모델 우선순위 매니저
  - 환경 변수나 설정 파일에서 `model_priorities` 리스트를 읽는다.
  - 각 모델에 대해 `last_failure`/`cooldown_until` 값을 저장한다.
  - 요약 호출 시 활성 모델 목록을 순회하며 첫 성공 시 즉시 반환, 실패 시 오류 유형에 따라 상태 업데이트.
- 이미지 처리
  - 기존 `download_images` 결과(`local_path`)를 읽어 base64 인코딩 후 `inline_data`로 전달한다.
  - 이미지가 많을 경우 최대 첨부 개수/용량을 설정하고 초과분은 무시하거나 별도 메시지에 기재한다.
- 프롬프트 구조
  - Codex에서 쓰던 system/user 메시지를 Gemini 포맷(`contents`, `parts`)으로 변환한다.
  - 출력은 3문장 내 한국어, 불릿 금지 등 기존 지침을 그대로 유지한다.
- 예외 처리
  - HTTP 오류, 타임아웃, 유효하지 않은 응답을 모두 `SummaryError`로 감싼다.
  - 디버그 모드에서는 요청/응답 일부를 stderr로 출력해 추적한다.

## 해야 할 작업
1. `codex_summary.py`를 대체할 `gemini_summary.py` 작성 및 테스트.
2. `reddit_worker.py`, `dcinside_worker.py`, `store_*` 스크립트의 설정/호출부를 새로운 요약 모듈로 교체.
3. 환경 변수 이름과 기본값(`CODEX_*`)을 `GEMINI_*`로 변경하고 문서화.
4. 테스트 스크립트/노트(`docs/modules/test_*`, `docs/notes/*`)에서 Codex 언급을 Gemini로 교체.
5. README나 `docs/overview.md`에 모델 우선순위, 폴백 정책, 한도 관리 방법을 설명.
6. 필요 시 DB에 저장된 과거 `model_name` 값을 새 형식으로 업데이트하는 마이그레이션 작성.

## 향후 확장
- 사용량을 DB나 Redis에 저장해 여러 워커 프로세스가 같은 한도 정보를 공유하도록 확장.
- Discord 알림에 모델 전환 이벤트를 포함해 비용/품질 상태를 한눈에 확인.
- 모델별 요약 품질 비교 로그를 쌓아 향후 최적 우선순위를 조정.
