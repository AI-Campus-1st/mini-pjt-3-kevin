# 인터페이스 명세

- 범위: 내부 모듈(`rag/`)의 입출력, 평가 하네스 호출 방식, RAG 체인의 인용·출처·거절 형식
- 관련 문서: [masterplan.md](masterplan.md) §4 아키텍처, [data_spec.md](data_spec.md) 저장 형식

| 절 | 대상 | 상태 |
|---|---|---|
| §1 | 외부 API (네이버 검색·법령 Open API·열린국회정보) | 미작성 (수집기 코드 `collector/collect_*.py`가 현재 기준) |
| §2 | `rag/retriever.py` | 확정 (Day 3) |
| §3 | `eval/run_eval.py` | 확정 (Day 3, retrieval-only 모드) |
| §4 | `rag/chain.py` `build_chain(cfg)`, `rag/docstore.py` | 확정 (Day 4) |

---

## 1. 외부 API

- 미작성

---

## 2. `rag/retriever.py`

### 2-1. 계층 규칙

- Chroma 읽기 전용 조회와 쿼리 임베딩만 수행 (문서 임베딩·컬렉션 쓰기 금지)
- 쿼리 임베딩 모델은 컬렉션 메타데이터 `embedding_model`을 따름 (data_spec §5-4)
- 의존: `config`, `chromadb`, `openai`. `indexer/` import 금지
- 챗봇과 평가가 같은 `Retriever` 사용

### 2-2. `Retriever`

```python
Retriever(collection: str | None = None, top_k: int = config.TOP_K)
```

| 인자 | 설명 |
|---|---|
| `collection` | Chroma 컬렉션명. 생략 시 `kb_A_te3s` (`config.collection_name('A', True)`) |
| `top_k` | `search`의 기본 반환 청크 수 |

| 속성 | 설명 |
|---|---|
| `collection_name` | 사용 컬렉션명 |
| `info` | 컬렉션 메타데이터 사본 (`embedding_model`, `chunk_config`, `title_prefix`, `built_at`, `n_docs`, `n_chunks`) |
| `embedding_model` | 쿼리 임베딩에 쓰는 모델 |

| 메서드 | 입력 | 출력 | 설명 |
|---|---|---|---|
| `search(query, k=None, filters=None, query_embedding=None)` | 질문 문자열, 반환 수, 필터, 미리 계산한 임베딩 | `list[RetrievedChunk]` (점수 내림차순, rank 1부터) | 청크 단위 결과. 같은 문서의 청크가 여러 개 포함될 수 있음 |
| `embed_queries(queries)` | `list[str]` | `list[list[float]]` | 쿼리 일괄 임베딩. 같은 모델·질문은 프로세스 안에서 재사용 |

### 2-3. `filters`

| 키 | 타입 | Chroma 조건 |
|---|---|---|
| `year_from` | int | `published_year >= year_from` |
| `year_to` | int | `published_year <= year_to` |
| `doc_types` | list[str] | `doc_type in doc_types` (data_spec §2-3 코드) |

- 여러 키는 AND 결합, 빈 dict·None은 필터 없음
- 발행일 미상 문서는 `published_year = 0` → 연도 필터 지정 시 제외됨

### 2-4. `RetrievedChunk`

| 필드 | 타입 | 설명 |
|---|---|---|
| `rank` | int | 1부터 시작 |
| `chunk_id` | str | data_spec §5-1 |
| `doc_id` | str | |
| `score` | float | 코사인 유사도 (`1 - cosine distance`), 높을수록 유사 |
| `text` | str | 임베딩 입력 텍스트 (제목 prefix 포함 여부는 컬렉션에 따름) |
| `meta` | dict | data_spec §5-3 청크 메타데이터 전체 |

### 2-5. `dedup_docs(chunks)`

- 입력 순서를 유지하며 doc_id별 첫 청크(최고 점수)만 남기고 rank를 1부터 다시 부여
- 평가의 문서 단위 순위 산정에 사용

---

## 3. `eval/run_eval.py`

```
python -m eval.run_eval --tag <태그> --collection <컬렉션> --retrieval-only [--top-k 5] [--changed-variable "<설명>"] [--eval-set eval/eval_set.jsonl]
```

| 지표 | 정의 |
|---|---|
| `hit@1`·`hit@3`·`hit@5` | 상위 50개 청크를 문서 단위로 중복 제거한 순위에서 정답 문서가 k위 이내 |
| `mrr` | 같은 문서 순위의 역순위 평균, 10위 밖은 0 (MRR@10) |
| `ctx_hit` | 생성 단계에 들어가는 상위 `top_k` 청크 안에 정답 문서 청크 존재 |
| `ctx_n_docs_mean` | 상위 `top_k` 청크의 고유 문서 수 평균 |
| `ctx_doc_type_share` | 상위 `top_k` 청크의 문서 종류 비중 (전 문항 합산) |
| `top1_score` | 1위 청크 점수, 정답(hit@1)·오답·답변 불가 문항별 분포 |

- 답변 불가 문항은 적중 지표 평균에서 제외하고 `top1_score`만 집계
- 정답 판정은 data_spec §6 규칙
- 결과 형식은 data_spec §7 (`per_question.jsonl`에 `retrieved_docs`, `ctx_doc_types`, `ctx_n_docs`, `ctx_hit`, `source_need` 필드 추가)

---

## 4. `rag/chain.py`, `rag/prompts.py`, `rag/docstore.py`

### 4-1. 처리 흐름

```
질문 (+ 대화 이력) ─▶ ① 질의 재작성 (이력 있을 때만) ─▶ ② 검색 (필터, top_k 청크) ─▶ ③ 임계값 판정 (설정 시)
   ─▶ ④ 컨텍스트 조립 (문서 단위 번호) ─▶ ⑤ 생성 (스트리밍) ─▶ ⑥ 인용 [n] 추출·출처 매핑·거절 판정
```

- ①·⑤는 LangChain LCEL 체인 (`ChatPromptTemplate | ChatOpenAI | StrOutputParser`)
- ②는 `rag.retriever.Retriever` (평가와 동일 검색기)
- 검색 결과가 0건이거나 임계값 미달이면 ⑤를 호출하지 않고 안내·거절 응답 반환

### 4-2. `build_chain(cfg=None) -> RagChain`

| 키 | 기본값 (`config`) | 설명 |
|---|---|---|
| `collection` | `DEFAULT_COLLECTION` (`kb_A_te3s`) | 검색 컬렉션 |
| `top_k` | `TOP_K` (5) | 생성에 전달하는 청크 수 |
| `score_threshold` | `SCORE_THRESHOLD` (None) | 이 값 미만 청크 제외, 남는 청크가 없으면 검색 실패 처리. None이면 미적용 |
| `prompt_version` | `PROMPT_VERSION` (`v1`) | `rag.prompts.SYSTEM_PROMPTS` 키 |
| `llm_model` | `LLM_MODEL` | 생성·재작성 모델 |
| `reasoning_effort` | `LLM_REASONING_EFFORT` (None) | None이면 모델 기본값 |
| `rewrite` | True | 대화 이력이 있을 때 질의 재작성 수행 여부 |
| `history_turns` | `HISTORY_TURNS` (3) | 생성 단계에 전달하는 이전 대화 턴 수 |

- 챗봇과 평가가 같은 함수 호출. 평가는 실험 설정만 `cfg`로 덮어씀

### 4-3. `RagChain` 메서드

| 메서드 | 입력 | 출력 | 용도 |
|---|---|---|---|
| `prepare(question, history=None, filters=None)` | 질문, 이전 대화 `[{"role": "user"/"assistant", "content"}]`, 필터 (§2-3) | `Prepared` | ①~④. UI가 "검색 중" 단계 표시에 사용 |
| `stream(prep)` | `Prepared` | 문자열 토큰 generator | ⑤. `st.write_stream`에 그대로 전달. 생성 생략 상태면 안내문 1회 반환 |
| `finalize(prep, text)` | `Prepared`, 생성 전문 | `RagAnswer` | ⑥ |
| `invoke(question, history=None, filters=None)` | 위와 같음 | `RagAnswer` | 비스트리밍 일괄 실행 (평가용) |

### 4-4. `Prepared`

| 필드 | 설명 |
|---|---|
| `question` | 원 질문 |
| `query` | 검색에 사용한 질의 (재작성 결과 또는 원 질문) |
| `rewritten` | 재작성 여부 |
| `chunks` | 검색 청크 `list[RetrievedChunk]` (임계값 적용 후) |
| `sources` | 컨텍스트 문서 목록 (§4-6) |
| `status` | `ok` / `no_result` (검색 0건) / `below_threshold` (임계값 미달) |
| `latency_ms` | `{"rewrite", "retrieve"}` |

### 4-5. `RagAnswer`

| 필드 | 타입 | 설명 |
|---|---|---|
| `question`, `query`, `rewritten`, `status` | | `Prepared`와 동일 |
| `answer` | str | 생성 답변 전문 (생성 생략 시 안내·거절 문구) |
| `sources` | list[dict] | 컨텍스트 문서 전체 (§4-6), `cited` 표시 포함 |
| `citations` | list[dict] | 답변 속 인용 `[{"n", "doc_id"}]` (등장 순, 중복 제거) |
| `invalid_citations` | list[int] | 컨텍스트에 없는 번호 |
| `refused` | bool | 거절 판정 (§4-7) |
| `partial_refusal` | bool | 거절 문구와 인용이 함께 있는 경우 (일부 항목만 확인 불가) |
| `latency_ms` | dict | `{"rewrite", "retrieve", "generate", "total"}` |
| `config` | dict | 사용한 `cfg` 사본 |

### 4-6. 출처 번호와 `sources` 항목

- 번호 단위는 **문서**: 상위 `top_k` 청크를 검색 순위대로 보면서 처음 등장한 문서에 1부터 번호 부여, 같은 문서의 청크는 같은 번호 아래 순서대로 묶음
- 컨텍스트 형식

```
[n] 문서종류: {한글 표기} (신뢰등급 {tier}: {등급 설명}) | 발행처: {publisher} | 발행일: {published_at}
제목: {title}
{청크 본문 (제목 prefix 제외), 같은 문서의 청크는 "..."로 구분}
```

| 키 | 설명 |
|---|---|
| `n` | 출처 번호 |
| `doc_id`, `doc_type`, `doc_type_label`, `source_tier`, `title`, `publisher`, `published_at`, `url`, `citation` | 청크 메타데이터 (data_spec §5-3) |
| `score` | 문서 내 최고 청크 유사도 |
| `chunks` | `[{"chunk_id", "score", "text", "section", "page"}]` |
| `cited` | 답변에서 인용되었는지 |

### 4-7. 거절

- 거절 문구: `제공된 자료에서 확인할 수 없습니다` (`rag.prompts.REFUSAL_PHRASE`, 가이드 4-6)
- 프롬프트 지시: 질문 핵심의 근거가 없으면 첫 문장을 거절 문구로 시작, 확인할 수 없다는 서술에는 인용 번호를 붙이지 않음
- 판정 정규식 `rag.prompts.REFUSAL_RE`: 모델의 변형 표현 포함 (예: "제공된 자료에서는 ... 확인할 수 없습니다", "자료만으로는 ... 확인할 수 없습니다")
- `refused` = 답변 첫 문장이 거절 정규식과 일치, 또는 생성 생략 상태(`no_result`, `below_threshold`)
- `partial_refusal` = 첫 문장은 거절이 아니나 이후 서술에 거절 표현이 있음 (일부 항목만 확인 불가)
- 인용 수와 무관하게 판정 (거절하면서 "해당 문서에 내용 없음"을 인용으로 설명하는 경우 대응). 자동 판정은 Day 5 채점에서 수기 확인

### 4-8. `rag/docstore.py`

| 함수 | 출력 | 설명 |
|---|---|---|
| `get_document(doc_id)` | dict 또는 None | `store/docs.db` 읽기 전용 연결로 원문 전체(`body`)와 서지 정보 조회. UI 원문 보기용 |
| `kb_overview()` | dict | 지식베이스 포함 문서의 연도 범위·문서 종류 목록 (UI 필터 구성용) |
