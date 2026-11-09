# 인터페이스 명세

- 범위: 외부 API 호출 방식, 내부 모듈(`rag/`)의 입출력, 평가 하네스 호출 방식, RAG 체인의 인용·출처·거절 형식
- 관련 문서: [masterplan.md](masterplan.md) §4 아키텍처, [data_spec.md](data_spec.md) 저장 형식, [source_access.md](source_access.md) 소스별 접근성 판정

| 절 | 대상 | 상태 |
|---|---|---|
| §1 | 외부 API (네이버 검색·법령 Open API·열린국회정보·OpenAI) | 확정 (Day 2 수집 기준, Day 4 문서화) |
| §2 | `rag/retriever.py` | 확정 (Day 3) |
| §3 | `eval/run_eval.py` | 확정 (Day 3, retrieval-only 모드) |
| §4 | `rag/chain.py` `build_chain(cfg)`, `rag/docstore.py` | 확정 (Day 4) |

---

## 1. 외부 API

### 1-0. 공통

| 항목 | 내용 |
|---|---|
| 인증 정보 | `.env`에만 저장 (git 제외), 키 이름은 `.env.example` |
| HTTP 세션 | `collector.storage.PoliteSession`: 요청 간 최소 지연 강제, 병렬 요청 없음, timeout 30s, User-Agent `config.USER_AGENT` |
| 기록 | 문서별 출처·수집 일시는 `data/manifest.jsonl`, 호출 결과는 `data/raw/collect_log.jsonl` (data_spec §3·§4-3) |
| 원본 저장 | API 응답 JSON 그대로 `data/raw/{source}/`에 저장 후 인덱서가 파싱 |
| 실행 | `python -m collector law`, `python -m collector assembly`, `python -m collector naver` (또는 `all`) |

| API | `.env` 키 | 수집기 | 요청 지연 | Day 2 수집 결과 |
|---|---|---|---:|---|
| 네이버 검색 (NAVER API HUB) | `NAVER_CLIENT_ID`, `NAVER_CLIENT_SECRET` | `collector/collect_naver.py` | 0.3s | 200회 호출, 블로그·카페 요약문 13,357건 (지식베이스 포함 1,046건) |
| 국가법령정보 공동활용 | `LAW_OPEN_API_OC` | `collector/collect_law.py` | 1.5s | 법률·시행령·시행규칙 3건 |
| 열린국회정보 | `ASSEMBLY_API_KEY` | `collector/collect_assembly.py` | 1.0s | 제21대 14건·제22대 26건 의안 목록 → 문서 2건 |
| OpenAI | `OPENAI_API_KEY` | `indexer/embed.py`, `rag/` | TPM 기준 조절 | 임베딩 69,636,257 토큰 (Day 3) |

### 1-1. 네이버 검색 API (블로그·카페)

| 항목 | 내용 |
|---|---|
| 엔드포인트 | `GET https://naverapihub.apigw.ntruss.com/search/v1/{blog\|cafearticle}` (`config.NAVER_ENDPOINT`) |
| 인증 헤더 | `X-NCP-APIGW-API-KEY-ID` (Client ID), `X-NCP-APIGW-API-KEY` (Client Secret). 개발자센터 방식(`openapi.naver.com`, `X-Naver-Client-Id`)은 이 키로 401 |
| 파라미터 | `query` (검색어, `config.NAVER_QUERIES` 10개), `display=100`, `start=1,101,...,901`, `sort=sim` (정확도순) |
| 호출 한도 | 무료 제공량 일 25,000건·월 775,000건. 날짜별 누적 호출 수를 `data/raw/naver/quota.json`에 기록하고 `config.NAVER_DAILY_LIMIT` 도달 시 중단 (유료 구간 진입 금지) |
| 조회 상한 | 검색어당 `start + display ≤ 1,000` → 검색어 분할로 대응 |
| 페이지 종료 | 응답 `items`가 `display`보다 적으면 해당 검색어 종료 |
| 사용 필드 (블로그) | `title`, `link`, `description`, `bloggername`, `postdate` (`YYYYMMDD`) |
| 사용 필드 (카페) | `title`, `link`, `description`, `cafename` (게시일 필드 없음 → `published_at` 빈 문자열) |
| 변환 | `title`·`description`의 HTML 태그 제거 및 엔티티 복원. 본문은 `description`(요약문)만 사용 |
| 문서 키 | `canonical_key = {blog\|cafe}:{link}`, `publisher` = 블로그명·카페명 |
| 제약 | blog.naver.com·cafe.naver.com robots.txt가 RAG 목적 봇 접근 금지 → 링크 본문 수집 금지, API 응답만 사용 |

### 1-2. 국가법령정보 공동활용 Open API

| 항목 | 내용 |
|---|---|
| 목록 조회 | `GET https://www.law.go.kr/DRF/lawSearch.do` 파라미터 `OC`, `target=law`, `type=JSON`, `query=지역사랑상품권` (`config.LAW_QUERY`) |
| 목록 사용 필드 | `LawSearch.law[]`의 `법령일련번호`(MST), `법령명한글`, `공포일자`, `시행일자`, `법령구분명`. 결과가 1건이면 리스트가 아닌 객체로 오므로 리스트로 변환 |
| 본문 조회 | `GET https://www.law.go.kr/DRF/lawService.do` 파라미터 `OC`, `target=law`, `type=JSON`, `MST={법령일련번호}` |
| 본문 사용 필드 | `법령.조문.조문단위[]` 중 `조문여부 = 조문`인 항목의 `조문번호`·`조문제목`·`조문내용`·`항[].항내용`·`항[].호[].호내용` → 조문 단위 구획, `법령.제개정이유.제개정이유내용` → 별도 구획 |
| 문서 키 | `canonical_key = law:{법령일련번호}`, `published_at` = 공포일자, 시행일자는 `note`에 기록 |
| 공개 URL | 응답의 상세 링크에 OC 값이 포함되므로 manifest에는 `https://www.law.go.kr/법령/{법령명(공백 제거)}`만 기록 |
| 호출 한도 | 별도 한도 미확인. 법령 3건 기준 목록 1회 + 본문 3회 호출 |

### 1-3. 열린국회정보 Open API (국회의원 발의법률안)

| 항목 | 내용 |
|---|---|
| 엔드포인트 | `GET https://open.assembly.go.kr/portal/openapi/nzmimeepazxkubdpn` (`config.ASSEMBLY_SERVICE`) |
| 파라미터 | `KEY`, `Type=json`, `pIndex` (1부터), `pSize=100`, `AGE` (대수, `config.ASSEMBLY_AGES` = 21, 22), `BILL_NAME=지역사랑상품권` |
| 응답 구조 | `{서비스코드: [{"head": [{"list_total_count"}, ...]}, {"row": [...]}]}`. 서비스코드 키가 리스트가 아니면(결과 없음·오류 응답) 수집 종료 |
| 페이지 종료 | 누적 행 수가 `list_total_count` 이상이면 종료 |
| 사용 필드 | `BILL_NO`, `BILL_NAME`, `PROPOSE_DT`, `PROPOSER`, `RST_PROPOSER`, `COMMITTEE`, `PROC_RESULT`, `PROC_DT` |
| 문서 구성 | 대수별 의안 목록을 문서 1건으로 저장 (의안 행은 제목이 동일하여 단독 문서로는 변별력 없음), 의안별 한 줄 요약을 발의일 순으로 나열 |
| 문서 키 | `canonical_key = assembly:bills:{AGE}:{BILL_NAME}`, `published_at` = 최신 발의일, `citation`에 API명·조회일 기록 |
| 제약 | 의안정보시스템(likms.assembly.go.kr)은 robots.txt 전면 차단 → 법안 원문·검토보고서는 수동 다운로드 (data_spec §3-3) |

### 1-4. OpenAI API

| 용도 | 호출 | 설정 | 비고 |
|---|---|---|---|
| 문서 임베딩 | `embeddings.create` | `config.EMBEDDING_MODEL` (`text-embedding-3-small`, 1,536차원) | 요청당 최대 100,000 토큰·1,000건 배치, 3개 스레드, 최근 60초 사용량을 TPM 한도(1,000,000)의 90% 이하로 유지, 오류 시 지수 백오프 6회. 호출 기록 `data/interim/embed_log.jsonl` |
| 쿼리 임베딩 | `embeddings.create` | 컬렉션 메타데이터 `embedding_model` | `rag/retriever.py`, 프로세스 내 캐시 |
| 답변 생성 | LangChain `ChatOpenAI` (스트리밍) | `config.LLM_MODEL` (`gpt-6-luna`), `reasoning_effort` 기본값 | temperature는 기본값(1)만 지원 → 인자 생략 |
| 질의 재작성 | LangChain `ChatOpenAI` | 같은 모델, `reasoning_effort=low` | 대화 이력이 있을 때만 호출 |

- 계정 한도 (2026-11-06 응답 헤더 기준): 임베딩 요청 5,000 RPM, 1,000,000 TPM

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

- 입력 순서를 유지하며 doc_id별 첫 청크(후보 순서상 최상위)만 남기고 rank를 1부터 다시 부여
- 평가의 문서 단위 순위 산정에 사용

### 2-6. `Retriever.search_hybrid(query, k, filters, query_embedding, rrf_k, vec_key)`

- 벡터 상위 k + BM25 상위 k (`rag/bm25.py`, 필터는 메타데이터에 직접 적용) → RRF 점수 `Σ 1 / (rrf_k + 순위)` 내림차순 상위 k
- `score`는 코사인 유사도 유지 (BM25로만 검색된 청크는 저장 임베딩과 질의 임베딩의 코사인), 임계값 판정에 그대로 사용
- `extra`: `{"rrf", "vec_rank", "bm25_rank"}`
- `vec_key`: 결합 전 벡터 결과 재정렬 기준 (신뢰등급 가산점)

### 2-7. `rag/bm25.py`

- 색인어: kiwipiepy 형태소 중 NNG·NNP·NNB·NR·SL·SH·SN·XR·VV·VA, 영문 소문자화
- 사용자 사전 `config.BM25_USER_WORDS`: 도메인 핵심 용어를 한 단어로 분석 (예: '지역사랑상품권법' → '지역사랑상품권' + '법')
- 인덱스: `python -m indexer bm25 --collection <컬렉션>`이 `store/bm25/{collection}/`에 저장 (`weights.npz` 청크 × 어휘 BM25 가중치, `vocab.json`, `chunk_ids.json`, `meta.json`), k1 1.5, b 0.75
- `rag/`는 인덱스 읽기만 수행. 사용자 사전·색인어 규칙 변경 시 인덱스 재구축 필요

---

## 3. `eval/run_eval.py`

```
python -m eval.run_eval --tag <태그> [--collection kb_A_te3s] [--retrieval-only] [--top-k 5] [--threshold <0~1>] [--prompt-version v1]
                        [--generations 3] [--workers 4] [--ids q001,q002] [--changed-variable "<설명>"] [--eval-set eval/eval_set.jsonl]
```

- 챗봇과 같은 `build_chain(cfg)`로 검색·생성 수행. 실험 설정(`collection`, `top_k`, `score_threshold`, `prompt_version`)만 `cfg`로 덮어씀
- 검색 지표는 `Prepared.candidates`(검색 후보 `fetch_k` 청크)와 `Prepared.chunks`(생성 단계 컨텍스트)에서 산출
- `--retrieval-only`: `prepare`만 실행 (LLM 호출 없음). 생략 시 full 모드
- full 모드: 문항당 `--generations`회 생성 (`config.EVAL_GENERATIONS`, 기본 3), `--workers` 동시 호출

| 지표 | 정의 |
|---|---|
| `hit@1`·`hit@3`·`hit@5` | 검색 후보 50개 청크를 문서 단위로 중복 제거한 순위에서 정답 문서가 k위 이내 |
| `mrr` | 같은 문서 순위의 역순위 평균, 10위 밖은 0 (MRR@10) |
| `ctx_hit` | 생성 단계에 들어가는 컨텍스트 청크(임계값 적용 후) 안에 정답 문서 청크 존재 |
| `ctx_n_docs_mean` | 컨텍스트 청크의 고유 문서 수 평균 |
| `ctx_doc_type_share` | 컨텍스트 청크의 문서 종류 비중 (전 문항 합산) |
| `top1_score` | 1위 청크 점수, 정답(hit@1)·오답·답변 불가 문항별 분포 |
| `refusal_acc` (full) | 답변 불가 문항 중 거절(다수결) 비율 |
| `false_refusal_rate` (full) | 답변 가능 문항 중 거절(다수결) 비율 |
| `partial_refusal_rate` (full) | 답변 가능 문항 중 대표 답변이 부분 거절인 비율 |
| `refusal_unanimous` (full) | 생성 간 거절 판정이 모두 같은 문항 비율 (재현성) |
| `cited_gold_rate` (full) | 거절하지 않은 답변 가능 문항 중 정답 문서를 1건 이상 인용한 비율 (출처 정확성의 자동 참고값) |
| `points_auto` (full) | 거절하지 않은 답변 중 `required_points` 정규식이 답변에 등장한 비율 평균 (정확도의 자동 참고값) |
| `invalid_citation_rate` (full) | 컨텍스트에 없는 번호를 인용한 답변 비율 |
| `latency_*` | 검색·생성·전체 p50/p95 (ms) |

- 답변 불가 문항은 적중 지표 평균에서 제외하고 `top1_score`만 집계
- 정답 판정은 data_spec §6 규칙
- 거절 다수결: 생성 중 과반이 거절이면 거절. 대표 답변 = 다수결과 판정이 같은 첫 생성 (채점 시트에 기록)
- 결과 형식은 data_spec §7
- 직접 채점 집계: `python -m eval.grade <결과 폴더>` → `graded_summary.json` (정확도, 출처 정확성, 거절 정확도·오거절률, 실패 계층)

---

## 4. `rag/chain.py`, `rag/prompts.py`, `rag/docstore.py`

### 4-1. 처리 흐름

```
질문 (+ 대화 이력) ─▶ ① 질의 재작성 (이력 있을 때만) ─▶ ② 검색 (필터, top_k 청크) ─▶ ③ 임계값 판정 (설정 시)
   ─▶ ④ 컨텍스트 조립 (문서 단위 번호) ─▶ ⑤ 생성 (스트리밍) ─▶ ⑥ 인용 [n] 추출·출처 매핑·거절 판정
```

- ①·⑤는 LangChain LCEL 체인 (`ChatPromptTemplate | ChatOpenAI | StrOutputParser`)
- ②는 `rag.retriever.Retriever` (평가와 동일 검색기). 후보 `fetch_k`개를 검색한 뒤 `select_context`로 `top_k`개 선택
- 검색 결과가 0건이거나 임계값 미달이면 ⑤를 호출하지 않고 안내·거절 응답 반환

### 4-2. `build_chain(cfg=None) -> RagChain`

| 키 | 기본값 (`config`) | 설명 |
|---|---|---|
| `collection` | `DEFAULT_COLLECTION` (`kb_A_te3s`) | 검색 컬렉션 |
| `top_k` | `TOP_K` (5) | 생성에 전달하는 청크 수 |
| `fetch_k` | `FETCH_K` (50) | 검색 후보 청크 수. 컨텍스트는 후보에서 `select_context`로 선택 (기본: 상위 `top_k`) |
| `hybrid` | `HYBRID` (True) | 벡터 상위 `fetch_k` + BM25 상위 `fetch_k`를 RRF(`RRF_K` 60)로 결합 (E5, Day 5 채택) |
| `tier_bonus` | `TIER_BONUS` (`{1: 0.04, 2: 0.04}`) | 신뢰등급별 유사도 가산점. 하이브리드면 벡터 순위에 적용한 뒤 RRF 결합 (Day 5 채택) |
| `primary_quota` | `PRIMARY_QUOTA` (None) | 문서 단위 상위 5위 안 1차 자료(등급 1~2) 최소 문서 수 (실험 후 미채택) |
| `doc_chunk_cap` | `DOC_CHUNK_CAP` (None) | 컨텍스트 문서당 최대 청크 수 (실험 후 미채택) |
| `score_threshold` | `SCORE_THRESHOLD` (None) | 이 값 미만 청크 제외, 남는 청크가 없으면 검색 실패 처리. None이면 미적용 |
| `prompt_version` | `PROMPT_VERSION` (`v2`, Day 6 채택. E0·E3은 `v1`) | `rag.prompts.SYSTEM_PROMPTS` 키 |
| `llm_model` | `LLM_MODEL` | 생성·재작성 모델 |
| `reasoning_effort` | `LLM_REASONING_EFFORT` (None) | None이면 모델 기본값 |
| `rewrite` | True | 대화 이력이 있을 때 질의 재작성 수행 여부 |
| `history_turns` | `HISTORY_TURNS` (3) | 생성 단계에 전달하는 이전 대화 턴 수 |

- 챗봇과 평가가 같은 함수 호출. 평가는 실험 설정만 `cfg`로 덮어씀
- E0 기준선 검색 재현: `hybrid=False`, `tier_bonus=None`

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
| `chunks` | 컨텍스트 청크 `list[RetrievedChunk]` (`select_context` 선택, 임계값 적용 후) |
| `candidates` | 검색 후보 청크 `fetch_k`개 (평가의 문서 단위 순위 산정용) |
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
- `refused` = 답변 첫 문장(인용 번호 제거 후 분리)이 거절 정규식과 일치, 또는 생성 생략 상태(`no_result`, `below_threshold`)
- `partial_refusal` = 첫 문장은 거절이 아니나 이후 서술에 거절 표현이 있음 (일부 항목만 확인 불가)
- 인용 수와 무관하게 판정 (거절하면서 "해당 문서에 내용 없음"을 인용으로 설명하는 경우 대응). 자동 판정은 Day 5 채점에서 수기 확인

### 4-8. `rag/docstore.py`

| 함수 | 출력 | 설명 |
|---|---|---|
| `get_document(doc_id)` | dict 또는 None | `store/docs.db` 읽기 전용 연결로 원문 전체(`body`)와 서지 정보 조회. UI 원문 보기용 |
| `kb_overview()` | dict | 지식베이스 포함 문서의 연도 범위·문서 종류 목록 (UI 필터 구성용) |
