# gemini_summary.py 요약

## 역할
- Google Gemini Generative Language API를 호출해 텍스트/이미지를 한국어 요약으로 변환한다.
- 모델 우선순위 리스트(예: `gemini-2.5-flash` → `gemini-2.5-flash-lite`)를 순회하며 한도 초과 시 자동 폴백한다.
- 요약 실패·타임아웃·HTTP 오류를 `SummaryError`로 감싸 상위 레이어에 전달하고, 마지막으로 시도한 모델명을 함께 기록한다.

## 핵심 구성 요소
- **`SummaryError`**: Gemini 요청 실패를 표현하는 예외. `last_model` 속성으로 마지막 시도 모델을 알 수 있다.
- **`GeminiConfig`**: API 키, 모델 우선순위, 타임아웃, 텍스트 길이, 디버그/쿨다운 옵션을 담는 데이터클래스.
- **`summarise_with_gemini`**: 입력 텍스트와 이미지 경로를 받아 (요약문, 사용 모델) 튜플을 반환한다. 이미지 파일은 base64로 인라인 전송한다.
- **쿨다운 캐시**: 429/`RESOURCE_EXHAUSTED` 오류가 발생한 모델을 일정 시간 제외해 반복 실패를 방지한다.

## 외부 의존성
- `requests`: Gemini REST API 호출.
- `base64`, `mimetypes`: 로컬 이미지 파일을 인라인 데이터 파트로 변환.
- 표준 라이브러리(`json`, `time`, `textwrap`)로 프롬프트 구성 및 응답 파싱.

## 운영 시 유의 사항
- `.env`에 `GEMINI_API_KEY`가 반드시 존재해야 하며, `GEMINI_MODEL_PRIORITIES`로 우선순위를 제어한다.
- `config.max_text_length`를 초과하는 입력은 잘라낸 후 말줄임표를 붙여 전송한다.
- `config.debug`를 활성화하면 요청 페이로드와 응답 요약이 stderr로 출력되므로 민감 정보 노출에 주의한다.

## 확장 아이디어
- 모델별 사용량을 DB/Redis에 기록해 여러 워커가 동일한 쿼터 정보를 공유하도록 확장.
- `generation_config`를 소스별 설정으로 분리해 길이/톤 조절.
- 이미지가 많은 게시물을 위해 첨부 개수 제한(`image_limit`)을 동적으로 조정하거나 썸네일만 전달.
