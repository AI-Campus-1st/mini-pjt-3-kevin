# 인터페이스 명세

- 범위: 내부 모듈(`rag/`)의 입출력, 평가 하네스 호출 방식
- 관련 문서: [masterplan.md](masterplan.md) §4 아키텍처, [data_spec.md](data_spec.md) 저장 형식

| 절 | 대상 | 상태 |
|---|---|---|
| §1 | 외부 API (네이버 검색·법령 Open API·열린국회정보) | 미작성 (수집기 코드 `collector/collect_*.py`가 현재 기준) |
| §2 | `rag/retriever.py` | 확정 (Day 3) |
| §3 | `eval/run_eval.py` | 확정 (Day 3, retrieval-only 모드) |
| §4 | `rag/chain.py` `build_chain(config)` | Day 4 오전 작성 예정 |

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

## 4. `rag/chain.py`

- Day 4 오전 작성 예정
