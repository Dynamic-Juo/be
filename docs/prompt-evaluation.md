# 프롬프트 경계와 평가

이 문서는 2026-09-13 `be` main `38bd16263afbf79daffd67878277c3af0a4a5d51`의 프롬프트·LLM·검색 경계를 정적 감사한 기록이다. 프롬프트 버전은 `2026-09-11.1`이며 정의는 [prompts.py](../deepcheck/prompts.py)에 있다. 제품 판정 기준은 `docs` main `8d07ff36e127012a4cb67adeea7ef45292bd85c0` 중 유일하게 `Accepted`인 [근거 정책][D-EVID]이다. PRD·MVP·AI 파이프라인 문서는 `Draft`이고 [docs PR #7][PR7]은 열려 있는 리뷰이므로 각각 목표 기록과 의견으로 분리한다.

이번 감사에서는 실제 LLM·검색 API·YouTube를 호출하지 않았다. 네트워크 연결을 차단하는 fixture 아래 전체 로컬 모의 테스트 `329 passed, 2 warnings`를 확인했다. 이는 파서와 고정 fixture의 계약 회귀만 증명하며 현재 프롬프트의 실모델 품질, 실제 검색 품질, 프롬프트 인젝션 방어율을 증명하지 않는다. 파이프라인 전체 판정은 [최신 기획 대비 소스 감사](source-audit.md)를 함께 읽는다.

## 결론

현재 코드는 입력을 비신뢰 데이터로 표시하고, 전체 JSON 응답·중복 키·`NaN`·열거형·인용 문자열·원문 scope·provenance·독립 계보를 후단에서 확인한다. 하지만 다음 세 항목 때문에 사용자에게 원문 기반 검증이 완성됐다고 표시할 수 없다.

1. 등록된 실제 검색 제공자는 모두 검색 발췌나 팩트체크 메타데이터만 만들며 `original + provenance_verified=true` 근거를 만들지 못한다. 따라서 실제 기본 경로의 `supported`·`refuted`는 구조적으로 도달할 수 없다.
2. 원문 fixture를 수동 주입하면 서버는 “6.0%”와 “6.0% 초과”처럼 의미가 다른 조합도 LLM 제안대로 긍정 승인할 수 있다. 인용 문자열의 존재와 비어 있지 않은 `cite_reason`만 확인할 뿐 entailment는 검증하지 않는다.
3. 주장 추출은 전문에 문자열이 있는지를 볼 뿐 완전성·독립 주장 단위·문맥 인접성·실제 반복 횟수·모든 위치를 보증하지 않는다.

원문 수집기를 연결하기 전에 2번의 거짓 양성 경로를 막는 것이 P0다. 원문 수집만 먼저 붙이면 현재 잠복한 의미 검증 결함이 사용자 판정에 활성화된다.

## 두 LLM 호출의 데이터 계약

| 작업 | 모델에 보내는 입력 | 모델 출력 | 서버가 결정적으로 확인하는 것 | 확인하지 못하는 것 |
| --- | --- | --- | --- | --- |
| 주장 추출 | `transcript`, `max_claims` | `claims[].text`, `context`, `repeat` | 응답 전체가 JSON object인지, 중복 키·비정상 상수 여부, 배열·필드 타입, `text`와 `context`가 전문 어딘가에 존재하는지, 양의 정수 repeat, 규칙 중복 여부 | 제목 기반 우선순위, segment 연계, 독립 주장 여부, 문맥 인접성, 실제 repeat, 전체 recall |
| 관계 판정 | 주장·문맥·영상 제목/게시일, 번호가 붙은 근거의 제목·URL·발행처·날짜·유형·scope·provenance·independence·rating·전달 content | verdict·reason·insufficient reason·citations | verdict/사유 allowlist, 인용 번호, 인용 문자열이 해당 content에 존재하는지, 양성 판정의 original/provenance와 1차 또는 독립 계보 2개 조건 | 인용이 주장 전체를 함의하는지, `cite_reason`의 의미 관련성, 일반적인 시점·정정·출처 충돌, 메타데이터의 실제 계보 |

추출 입력에는 영상 제목·게시일·segments가 없다. 제목과 게시일은 추출 뒤 카드에 붙으므로 Draft PRD의 “제목 연관성은 순서에만 반영” 목표를 현재 LLM 호출이 실행할 수 없다. 긴 전문은 chunk·coverage marker 없이 한 번에 보내고 출력은 4,096 token으로 제한하므로 `max_claims=0`도 “모두 추출” 보증이 아니다.

프롬프트는 발언과 자료 안의 명령을 데이터로만 다루고 주체·대상·시점·부정·조건·수치 단위를 보존하라고 요구한다. 게시일을 사건일로 단정하지 않고 다른 기간 자료에는 `time_mismatch`를 우선하라고도 한다. 이 지시는 필요한 모델 행동이지만 서버가 모두 결정적으로 재계산하는 불변식은 아니다.

## 결정적 검증의 실제 한계

### 의미가 다른 양성 판정

프롬프트는 정확히 `6.0%`인 원문과 `6.0%를 넘었다`는 주장을 같다고 판단하지 말라고 명시한다. 그러나 `tests/test_llm_verdict.py`의 `_verified_primary()` 원문은 “상승률은 6.0%로 집계됐다”이고, 양성 테스트의 주장은 “상승률은 6.0%를 넘었습니다”인데도 `SUPPORTED`를 기대한다. Fake LLM이 원문 문장을 인용하면 서버의 문자열·scope·provenance gate를 모두 통과한다.

`_apply_citations()`는 `cite_reason`도 비어 있지 않은지만 확인한다. 주장과 무관한 이유가 들어와도 현재 서버 계약상 통과한다. 즉 인용 검증은 “그 문자열이 제공한 자료 안에 있다”만 보장하며, 인용이 주장의 주체·비교 연산자·수치·단위·시점·부정까지 지지하거나 반박한다는 보장은 없다.

완료 조건은 다음과 같다.

- 수치의 정확·초과·이상·이하, 단위, 부정, 조건, 시점을 구조화해 주장과 인용을 결정적으로 비교한다.
- `cite_reason`이 인용과 주장 사이의 검증 가능한 관계를 설명하는지 검사하거나, 서버가 구조화된 차이를 직접 생성한다.
- 반대 의미 쌍과 부분 일치 쌍을 포함한 골든 테스트에서 거짓 양성 0건을 확인한다.
- 현재 모순된 양성 fixture를 수정하고, 실제 의미 validator를 통과해야만 `supported/refuted`가 나오도록 한다.

### 주장 완전성·위치·중복

기본 설정은 `claim_extractor=rule`, `llm_provider=off`다. 규칙 추출기는 문장 길이·단정형과 숫자·연도·수량·영문 고유명사 중심 점수를 사용하므로 무수치 검증 가능 사실을 빠뜨릴 수 있다. LLM 추출도 다음 한계를 가진다.

- `"10%"` 같은 전문의 부분 문자열을 독립 주장으로 승인할 수 있다.
- `repeat=99`가 실제 반복 횟수와 맞는지 확인하지 않는다.
- `context`가 주장 근처인지, 대명사를 실제로 해소하는지 확인하지 않는다.
- 유한 상한을 채우면 뒤 항목 검증 전에 멈춰 이후 환각·잘못된 타입을 발견하지 못한다.
- 의미 중복은 핵심어 70% 겹침으로 판단해 “늘렸다/줄였다”, “인상/인하”, “채용/해고” 같은 반대 주장을 합칠 수 있다.
- 위치 연결은 주장 앞 12자가 segment에 있으면 `exact`로 두고 첫 매치만 남긴다. 모든 언급 위치가 아니다.

Draft R-03을 완료하려면 고정 전문마다 필수 주장 목록과 모든 위치를 정답으로 만들고 recall, 잘못된 병합, 위치 정확도를 측정해야 한다. 누락 여부를 알 수 없으면 결과에도 coverage 불확실성을 표시한다.

## 검색 자료와 양성 판정의 도달 가능성

`WikipediaProvider`, `GDELTProvider`, `FactCheckProvider`, `NaverSearchProvider`는 현재 `content_scope=search_excerpt`, `provenance_verified=false`인 자료를 만든다. 양성 판정은 모든 인용이 `original`이고 provenance가 검증됐으며, 검증된 1차 출처 하나 또는 서로 다른 `independence_group` 두 개 이상일 때만 허용된다. 테스트의 양성 사례는 `_verified_primary()`가 이 값을 수동으로 만든 fixture이며 실제 provider가 만든 결과가 아니다.

이 보수적 gate 자체는 [Accepted 근거 정책][D-EVID]과 맞는다. 미구현인 것은 원문을 안전하게 가져와 다음 대응을 확인하는 collector와 provenance validator다.

- 검색 URL·원문 URL·최종 redirect·본문의 대응
- 발행 주체와 도메인, 게시·수정·정정 시점
- 1차 자료 여부와 재전송 기사들의 공통 원문 계보
- 주장 관련 본문 구간과 사용자에게 보여줄 정확한 원문 링크
- 서로 충돌하거나 일부만 확인되는 자료의 결정적 처리

[NAVER 뉴스 검색][O-NAVER]의 `originallink`·`description`, [Google Fact Check `claims.search`][O-FACTCHECK]의 claim review 메타데이터, [MediaWiki search][O-MEDIAWIKI]의 snippet은 탐색 단서다. 그 응답만으로 원문 본문과 출처 계보를 검증했다고 간주하면 안 된다. 향후 URL을 직접 가져올 때는 redirect마다 scheme·host·A/AAAA·public IP와 egress를 검사하는 [SSRF 경계][O-SSRF]가 별도로 필요하다.

검색 관련성은 현재 상위 핵심어 하나만 제목이나 snippet에 있어도 통과할 수 있다. provider 사이 URL·본문 중복 제거가 없고 `evidence_per_claim`은 전체 상한이 아니라 provider별 요청값이다. FactCheck `nextPageToken`도 사용하지 않는다. Wikipedia 검색 결과의 `timestamp`는 문서 발행일이 아닌 마지막 편집 시각인데 `published_at`으로 전달되므로 시점 판정에 오해가 생길 수 있다.

## 실패·시간·병렬 경계

| 상황 | 현재 카드 결과 | 감사 판단 |
| --- | --- | --- |
| 정상 빈 추출 | `no_claims` | 정상과 실패 구분됨 |
| provider 미설정·전부 실패 | `failed + weak_source` | 검색 0건과 구분됨 |
| 정상 검색 0건 | `done + no_source` | 정상 종료로 표현됨 |
| 처리된 LLM 불가·허용 밖 verdict | `done + not_direct` | 모델 장애가 정상 부족처럼 접혀 원인 구분 부족 |
| 양성 근거 gate 실패 | `done + weak_source` | 보수적 강등 |
| claim 시작 전 전체 deadline 경과 | `timed_out`, reason 빈 문자열 | 사용자 설명 부족, 실행 중 호출은 중단하지 못함 |
| claim worker 예상 밖 예외 | `failed`, claim reason/error 없음 | 운영 진단과 사용자 고정 오류 코드 부족 |

주장 카드는 `ThreadPoolExecutor`로 기본 3개까지 동시에 처리하지만 환경값에 3 상한 clamp는 없다. 한 주장 안의 provider는 설정 순서대로 호출한다. `DEEPCHECK_EVIDENCE_BUDGET_SEC=30`은 production orchestration이 아니라 별도 helper에만 쓰인다. 전체 600초 deadline도 claim 시작 전에만 확인하며 이미 시작한 검색·LLM 호출을 중단하지 않는다.

LLM HTTP 경로는 TLS 검증, 성공 응답 2 MiB·오류 4 KiB 읽기 상한, 외부 오류의 정규화를 갖춘다. JSON parser는 응답 전체 또는 전체 fenced block만 받고 중복 키와 비정상 상수를 거부한다. 다만 완전한 JSON Schema와 알 수 없는 필드 거부는 없고, provider의 `finish_reason=length`를 확인하지 않아 우연히 파싱 가능한 잘린 JSON을 승인할 수 있다. 응답 `content` 타입이 문자열이 아니면 일부 예외가 `LLMUnavailable`로 정규화되지 않는다.

## 프롬프트 인젝션 경계

구현된 방어는 다음과 같다.

- system prompt에서 transcript, title, rating, content를 비신뢰 데이터로 선언한다.
- user 입력을 JSON 필드로 직렬화하고 모델에 tool·파일·네트워크 권한을 주지 않는다.
- 전체 JSON 파싱, 출력 allowlist, 인용 번호·문자열, 양성 provenance gate를 후단에서 적용한다.
- 고정 사례 11개 중 추출 인젝션 1개와 근거 인젝션 1개가 있다.

남은 공격면은 원격 자료 속 간접 지시가 의미 판정과 `cite_reason`에 영향을 주는 경우, 긴 입력·다중 인코딩·분할 지시, 정상 문구를 흉내 낸 adversarial content, 반복 공격 탐지다. [OWASP LLM01:2025][O-PROMPT]도 system 지시만으로 완전한 방어가 되지 않으므로 비신뢰 콘텐츠 분리, 최소 권한, 결정적 출력 검증, 모니터링과 반복 공격 테스트를 함께 요구한다. 원문 collector를 붙이기 전에 remote content 정규화·크기 제한·instruction-like 구간 표시와 의미 검증 골든셋을 준비한다.

## 외부 제공자·모델 버전 점검

- 기본 LLM 모델은 `deepseek-chat`이다. [DeepSeek 공식 변경 기록][O-DEEPSEEK-UPDATES]은 이 legacy 이름을 2026-07-24 중단한다고 기록하며 2026-09-10 기준 V4.1 Flash에는 `deepseek-flash`를 안내한다. 현재 날짜에는 기본 모델 이름을 유효하다고 전제할 수 없다. 실제 API는 호출하지 않았으므로 동작 여부는 `미검증`이며, 지원 모델로 교체하고 고정 사례를 다시 평가해야 한다.
- `response_format=json_object`와 JSON 지시는 [DeepSeek JSON mode][O-DEEPSEEK-JSON]의 기본 사용 조건과 맞지만, transport 형식 준수는 의미 정확성을 보장하지 않는다.
- NAVER API HUB 뉴스 경로·헤더와 `description`을 검색 발췌로 다루는 구현은 현행 문서와 맞는다. 다만 코드·테스트의 “2026-07-31 기존 API 종료” 설명은 부정확하다. [공식 공지][O-NAVER-NOTICE]상 그날은 신규 신청 종료이고 기존 발급 키 지원 종료는 2027-06-30이다.

모델·API 이름, 종료일, quota는 시간에 따라 변하므로 배포 전 공식 문서를 다시 확인하고 버전·확인일·응답 계약을 회귀 기록에 고정한다.

## 평가 기록을 읽는 법

현재 프롬프트 `2026-09-11.1`의 실모델 평가는 없다. 저장된 [v2](evaluations/2026-09-10-prompt-v2.json)와 [v3](evaluations/2026-09-10-prompt-v3.json)는 2026-09-10 당시 프롬프트와 기반 HEAD `8a11b46` 위 dirty worktree에서 실행한 과거 기록이다.

| 기록 | 결과 | 말할 수 있는 것 | 말할 수 없는 것 |
| --- | --- | --- | --- |
| v2 `2026-09-10.2` | 10/11 | 고정 합성 사례 한 번의 출력 | 현재 prompt·모델 품질 |
| v3 `2026-09-10.3` | 11/11 | 시점 사유 지시 변경 뒤 같은 11건의 출력 | 실제 영상 정확도, M-08 70%, 일반 인젝션 방어 |
| 현재 main mock suite | 329 passed, 경고 2 | 코드 계약의 로컬 회귀 | 외부 provider·실모델·부하·hard timeout |

고정 사례는 주장 없음·문맥·부정·추출 인젝션 4건과 원문 기반 일치·불일치·검색 발췌 한계·시점 불일치·부분 확인·충돌·판정 인젝션 7건이다. 양성 원문은 테스트 작성자가 수동 제공한 가상 자료다. 실제 검색 코드가 원문을 수집했다는 의미가 아니다. 11개 중 2개 인젝션 사례로 일반 방어 성능을 주장하지 않는다.

목록 모드는 외부 호출 없이 사례와 `network_calls=0`을 확인한다.

```bash
.venv/bin/python scripts/evaluate_prompts.py
```

`--live`는 유료 외부 호출이며 이번 감사에서는 실행하지 않았다. 향후 승인된 평가에서는 모델 이름·프롬프트 hash·Git SHA·dirty 상태·사례 버전·비용·latency·원시 출력을 고정하고, 기대값을 모델 출력에 맞춰 수정하지 않는다. 학습·튜닝에 쓰지 않은 holdout과 실제 한국어 영상 정답 세트를 별도로 둔다.

## 공개 전 완료 게이트

### P0

1. 수치·단위·비교·부정·조건·시점의 의미 일치를 서버에서 검증하고 현재 `6.0%` 모순 fixture를 거짓 양성 방지 테스트로 바꾼다.
2. SSRF·크기·redirect 경계를 포함한 원문 collector와 출처·본문·계보 validator를 구현한다.
3. 실제 provider 결과로 양성·음성·부족·충돌·정정 사례를 끝까지 통과시키고 사용자가 원문을 재확인할 수 있게 한다.

### P1

1. 주장 전체 recall, 반대 의미 중복, 모든 발언 위치, 문맥 인접성, 실제 repeat를 정답 세트로 평가한다.
2. LLM/provider 실패·timeout·정상 부족을 서로 다른 안정된 코드와 사용자 설명으로 보존한다.
3. `finish_reason`, 응답 content 타입, JSON Schema, unknown field, evidence 중복·pagination·전체 예산을 검증한다.
4. 지원 중인 모델 이름으로 교체하고 현재 프롬프트의 holdout·adversarial 실모델 평가를 승인된 비용 범위에서 다시 수행한다.

### P2

1. 원격 문서 인젝션, 난독화·다중 인코딩·분할 지시·긴 입력을 포함한 반복 corpus와 모니터링을 운영한다.
2. provider별 날짜 의미와 source type을 명시하고 발행처·URL·본문 correspondence를 지속 점검한다.

[D-EVID]: https://github.com/Dynamic-Juo/docs/blob/8d07ff36e127012a4cb67adeea7ef45292bd85c0/design/evidence-policy.md#L37-L50
[PR7]: https://github.com/Dynamic-Juo/docs/pull/7
[O-NAVER]: https://api.ncloud-docs.com/docs/naver-api-hub-search-news
[O-NAVER-NOTICE]: https://developers.naver.com/notice/article/32530
[O-FACTCHECK]: https://developers.google.com/fact-check/tools/api/reference/rest/v1alpha1/claims/search
[O-MEDIAWIKI]: https://www.mediawiki.org/wiki/API:Search
[O-DEEPSEEK-UPDATES]: https://api-docs.deepseek.com/updates/
[O-DEEPSEEK-JSON]: https://api-docs.deepseek.com/guides/json_mode/
[O-PROMPT]: https://genai.owasp.org/llmrisk/llm01-prompt-injection/
[O-SSRF]: https://cheatsheetseries.owasp.org/cheatsheets/Server_Side_Request_Forgery_Prevention_Cheat_Sheet.html
