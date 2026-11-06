# 데이터 명세

- 범위: 수집 원본 → 원문 DB(`store/docs.db`) → 벡터 DB(`store/chroma/`) → 평가셋·실험 결과까지 모든 저장 형식
- 상태: **확정 (Day 2)**. 수집기·인덱서 구현 기준. **§2 공통 규칙과 §5 청크 메타데이터는 이후 변경 금지** (변경 시 전체 재인덱싱 + 평가셋 gold_doc_ids 재검증)
- 관련 문서: [masterplan.md](masterplan.md) §4 아키텍처, [source_access.md](source_access.md) 수집 소스, [api_spec.md](api_spec.md) 외부 API·모듈 인터페이스

---

## 1. 데이터 흐름과 저장 위치

```
data/news_csv_5y/ (제공, 읽기 전용)
        │ collector/load_provided.py
        ▼
data/raw/news/provided_news.jsonl   data/raw/{source}/ + data/manifest.jsonl + data/raw/collect_log.jsonl
                                     ◀── collector/crawl_*·collect_*·register_manual.py
        │ python -m indexer docs (parse → clean → relevance → dedup)
        ▼
store/docs.db  (원문 전체 + 구획 + 처리 로그 + 청크 텍스트)
        │ python -m indexer chunk → embed
        ▼
store/chroma/  (청크 + 임베딩 + 메타데이터)   store/bm25/ (하이브리드 검색 확장 시)
        │ rag/ (조회 전용)
        ▼
app/ (챗봇)            eval/ (평가셋, 실험 결과)
```

| 경로 | 내용 | git 추적 | 재생성 방법 |
|---|---|---|---|
| `data/news_csv_5y/` | 제공 뉴스 CSV | 제외 | 교육 과정 배포본 |
| `data/raw/` | 직접 수집 원본 (HTML·PDF·HWP·JSON) | 제외 | 수집기 재실행, 수동 자료는 manifest의 URL로 재다운로드 |
| `data/manifest.jsonl` | 직접 수집 문서의 출처·수집 일시 목록 | **추적** | 수집기가 추가 기록 |
| `data/manual_sources.csv` | 수동 다운로드 자료의 서지 정보 (§3-3) | **추적** | 수작업 |
| `data/interim/` | EDA 요약 등 중간 산출물 | 제외 | 노트북·인덱서 재실행 |
| `store/docs.db` | 원문 DB | 제외 | `python -m indexer` |
| `store/chroma/` | 벡터 DB | 제외 | `python -m indexer` |
| `eval/eval_set.jsonl` | 평가셋 | **추적** | 수작업 |
| `eval/results/` | 실험별 결과 | **추적** | `python -m eval.run_eval` |

- `manifest.jsonl`을 추적하는 이유: 원본 파일은 저장소에 없어도 모든 문서의 출처 URL·수집 일시를 저장소에서 확인 가능하도록 하기 위함

---

## 2. 공통 규칙 (Day 2 오전 확정 후 변경 금지)

### 2-1. 인코딩·시각

| 항목 | 규칙 | 예 |
|---|---|---|
| 텍스트 인코딩 | UTF-8 (BOM 없음). 제공 CSV는 UTF-8 BOM으로 읽음 | |
| `published_at` | 발행일, `YYYY-MM-DD` | `2025-08-04` |
| `published_ts` | 발행 시각 원본이 있으면 `YYYY-MM-DDTHH:MM:SS` (KST, 시간대 표기 없음) | `2025-08-04T14:12:00` |
| `collected_at` | 수집 시각, ISO 8601 KST 오프셋 포함 | `2026-11-05T15:30:12+09:00` |
| 발행 월만 확인 | `published_at` `YYYY-MM` 허용 (수동 자료), doc_id 날짜부 `YYYYMM00` | `2020-12` → `20201200` |
| 발행일 미상 | `published_at` 빈 문자열, doc_id 날짜부 `00000000` | |

- 제공 뉴스의 `발행일`은 시간대 표기 없는 현지 시각(KST로 간주)
- 제공 뉴스의 `collected_at`: 원본 수집 시각이 없으므로 제공 CSV 파일의 수정 시각을 기록하고 `collect_method = provided`로 구분

### 2-2. 소스 코드 (`source`)

| 코드 | 출처 | 기본 doc_type |
|---|---|---|
| `news` | 제공 뉴스 (네이버 뉴스 URL) | news |
| `korea_kr` | 대한민국 정책브리핑 | press_release |
| `mois` | 행정안전부 누리집 | press_release, guideline |
| `law` | 국가법령정보센터 | law |
| `assembly` | 열린국회정보·의안정보시스템 | bill, review_report |
| `nabo` | 국회예산정책처 | budget, report |
| `kipf` | 한국조세재정연구원 | report |
| `krila` | 한국지방행정연구원 | report |
| `ggri` | 경기연구원 | report |
| `nkis` | 국가정책연구포털 | report |
| `nars` | 국회입법조사처 | report |
| `moef` | 기획재정부·열린재정 | budget |
| `localri` | 시도 연구원 (경기연구원 제외) | report |
| `localgov` | 지자체 누리집 | local_notice |
| `blog` | 네이버 검색 API (블로그 요약문) | blog |
| `cafe` | 네이버 검색 API (카페 게시글 요약문) | cafe (지식베이스 제외, §4-1 `type_excluded`) |

- 소스 추가 시 이 표에 먼저 등록

### 2-3. 문서 종류 (`doc_type`)와 신뢰도 등급 (`source_tier`)

| doc_type | 한글 표기 | source_tier | 설명 |
|---|---|---:|---|
| `press_release` | 보도자료 | 1 | 부처 보도자료 원문 |
| `law` | 법령 | 1 | 법률·시행령·시행규칙 조문 |
| `bill` | 의안 | 1 | 법률안 원문, 제안 이유 |
| `budget` | 예산자료 | 1 | 예산안·결산 관련 공식 자료 |
| `local_notice` | 지자체공고 | 1 | 지자체 사업 공고·안내 |
| `guideline` | 업무지침 | 1 | 부처 업무지침·운영지침 (제도 운영 기준 원문) |
| `review_report` | 검토보고서 | 2 | 국회 상임위 전문위원 검토보고서 (법안에 대한 분석 의견) |
| `minutes` | 회의록 | 2 | 국회 회의록 (공식 기록이나 내용 대부분이 의원 발언·토론) |
| `veto` | 재의요구서 | 2 | 대통령 재의요구서 (공식 문서이나 내용은 거부 사유라는 정부 의견) |
| `report` | 연구보고서 | 2 | 국책·지자체 연구기관 보고서 |
| `news` | 뉴스 | 3 | 언론 보도 |
| `blog` | 블로그 | 4 | 이용자 작성 글 (요약문). 양산형 블로그는 지식베이스에서 제외 (`blog_excluded`) |
| `cafe` | 카페글 | 4 | 커뮤니티 게시글 (요약문). 게시일 미제공으로 지식베이스에서 제외 |

- 등급 의미: 1 정부·국회 공식 원문 (제도·사실의 근거) / 2 공식 기관의 분석 의견 / 3 언론 보도 / 4 개인 의견
- 검토보고서는 국회 공식 문서이나 법안의 사실 내용이 아닌 전문위원의 분석 의견이므로 연구보고서와 같은 등급 2로 분류
- 같은 기준으로 회의록(의원 발언)과 재의요구서(정부 의견)도 등급 2. 등급 1은 제도·사실 자체를 담은 원문(법령 조문, 의안 원문, 보도자료, 예산 원문, 지자체 공고)으로 한정
- 프롬프트의 출처 신뢰도 구분과 UI 배지에 그대로 사용

### 2-4. 문서 ID (`doc_id`)

```
doc_id = {source}_{yyyymmdd}_{sha1(canonical_key)[:8]}
```

| source | canonical_key | 근거 |
|---|---|---|
| `news` | `naver:{oid}/{aid}` | 제공 URL이 3가지 형식(`mnews/article/{oid}/{aid}` 32,187건, `main/read.naver?oid=&aid=` 2,621건, `article/{oid}/{aid}` 2,039건)으로 혼재 → oid·aid로 정규화. 정규화 후 중복 1건(원 URL 중복과 동일) |
| `korea_kr` | `korea_kr:{newsId}` | 상세 URL의 페이지 파라미터와 무관하게 고정 |
| 그 외 크롤링 | 쿼리·프래그먼트를 정렬·정규화한 URL | |
| 수동 다운로드 | 다운로드 페이지 URL, 없으면 `file:{sha1(파일 바이트)}` | |

- 예: `news_20250212_3f9a1c2b`, `korea_kr_20250804_9b0e77d1`
- 해시 충돌 확인: 적재 시 동일 doc_id·다른 canonical_key이면 오류 처리
- doc_id는 평가셋 정답(`gold_doc_ids`)의 기준이므로 재인덱싱·임계값 변경에도 불변이어야 함

---

## 3. 수집 원본 (`data/raw/`, `data/manifest.jsonl`)

### 3-1. 파일 배치

```
data/raw/{source}/{doc_id}.{html|json|pdf|hwp|txt}
data/raw/{source}/{doc_id}__att{n}.{pdf|hwp}      # 첨부파일
data/raw/reports/                                  # 수동 다운로드 PDF (서지 정보는 data/manual_sources.csv)
```

- 제공 뉴스는 `data/raw/`에 복사하지 않고 CSV에서 직접 적재

### 3-2. `manifest.jsonl` (1행 = 1문서, 추가 기록 방식)

| 필드 | 타입 | 필수 | 설명 |
|---|---|---|---|
| `doc_id` | str | O | §2-4 규칙 |
| `source` | str | O | §2-2 코드 |
| `doc_type` | str | O | §2-3 |
| `url` | str | △ | 원문 접근 URL (사용자에게 링크로 제시). 수동 자료는 빈 문자열 허용 |
| `citation` | str | △ | 참고문헌 형식 출처. 수동 자료는 필수 (url 대신 사용자에게 제시) |
| `page_range` | str | | PDF 중 지식베이스에 포함할 쪽 (예: `22,112-118`). 빈 값이면 전체 |
| `canonical_key` | str | O | doc_id 생성 키 |
| `title` | str | O | |
| `publisher` | str | O | 발행 기관 (예: 행정안전부, 한국조세재정연구원) |
| `published_at` | str | O | `YYYY-MM-DD` 또는 빈 문자열 |
| `collected_at` | str | O | §2-1 |
| `collect_method` | str | O | `crawl` / `api` / `manual` |
| `raw_path` | str | O | 본문 원본 경로 (저장소 루트 기준 상대 경로) |
| `attachments` | list | | `[{"name", "url", "path", "mime"}]` |
| `license` | str | | 이용 조건 (예: `공공누리 제1유형`) |
| `http_status` | int | | 크롤링 시 응답 코드 |
| `note` | str | | 대체 수집 사유 등 |

```json
{"doc_id": "korea_kr_20250804_9b0e77d1", "source": "korea_kr", "doc_type": "press_release", "url": "https://www.korea.kr/briefing/pressReleaseView.do?newsId=...", "canonical_key": "korea_kr:...", "title": "...", "publisher": "행정안전부", "published_at": "2025-08-04", "collected_at": "2026-11-05T15:30:12+09:00", "collect_method": "crawl", "raw_path": "data/raw/korea_kr/korea_kr_20250804_9b0e77d1.html", "attachments": [{"name": "....pdf", "url": "...", "path": "data/raw/korea_kr/korea_kr_20250804_9b0e77d1__att1.pdf", "mime": "application/pdf"}], "license": "공공누리 제1유형", "http_status": 200, "note": ""}
```

- 동일 doc_id 재수집 시 manifest에 새 행을 추가하지 않고 `collect_log`에만 기록

### 3-3. 수동 자료 서지 (`data/manual_sources.csv`)

- 수동 다운로드 PDF는 세부 URL 대신 참고문헌 형식의 출처(`citation`)로 관리 (사용자 결정)
- 수집기가 이 파일을 읽어 `data/raw/reports/`의 PDF를 manifest에 등록 (`collect_method = manual`, `url` 빈 문자열)

| 컬럼 | 필수 | 설명 |
|---|---|---|
| `file` | O | `data/raw/reports/` 안의 파일명 |
| `source` | O | §2-2 코드 |
| `doc_type` | O | §2-3 |
| `title` | O | 자료 제목 |
| `authors` | | 저자, 복수는 `;` 구분 |
| `publisher` | O | 발행 기관 |
| `series` | | 총서·학술지 권호 등 |
| `published_at` | O | `YYYY-MM-DD` 또는 `YYYY-MM` |
| `citation` | O | 참고문헌 형식 출처 (UI 출처 목록에 그대로 표시) |
| `include_pages` | | 포함할 PDF 쪽 (1부터 시작, 예: `22,112-118`). 빈 값이면 전체 |
| `downloaded_at` | O | 다운로드 시각 (파일 수정 시각), `collected_at`으로 사용 |
| `note` | | 용도, 처리 시 주의 사항 |

- doc_id의 canonical_key: `file:{sha1(파일 바이트)}` (§2-4)

---

## 4. 원문 DB (`store/docs.db`, SQLite)

### 4-1. `documents` — 후보 문서 전체 (지식베이스 포함 여부는 `included`로 구분)

| 컬럼 | 타입 | 설명 |
|---|---|---|
| `doc_id` | TEXT PK | §2-4 |
| `source` | TEXT | §2-2 |
| `doc_type` | TEXT | §2-3 |
| `source_tier` | INTEGER | §2-3 |
| `title` | TEXT | |
| `publisher` | TEXT | 뉴스는 매체명, 그 외 발행 기관 |
| `url` | TEXT | 수동 자료는 빈 문자열 |
| `citation` | TEXT | 참고문헌 형식 출처 (수동 자료 필수, 그 외 빈 문자열) |
| `page_range` | TEXT | 포함한 PDF 쪽 범위 |
| `published_at` | TEXT | `YYYY-MM-DD` 또는 `YYYY-MM` |
| `published_year` | INTEGER | 기간 필터용 |
| `published_ts` | TEXT | 시각 정보가 있는 경우 |
| `collected_at` | TEXT | |
| `collect_method` | TEXT | `provided` / `crawl` / `api` / `manual` |
| `keywords` | TEXT | 제공 뉴스의 수집키워드 |
| `body` | TEXT | 정제 후 본문 전체 (원문 보기에 사용) |
| `body_len` | INTEGER | 정제 후 길이 |
| `raw_len` | INTEGER | 정제 전 길이 |
| `relevance_rule` | TEXT | 적용 규칙명 (예: `R4`), 1차 자료는 `primary` |
| `dup_cluster_id` | TEXT | near-dup 클러스터 ID (대표 문서의 doc_id) |
| `is_representative` | INTEGER | 클러스터 대표 여부 (0/1) |
| `included` | INTEGER | 지식베이스 포함 여부 (0/1) |
| `exclude_reason` | TEXT | `irrelevant` / `exact_dup` / `near_dup` / `too_short` / `empty_body` / `rank_cap` (검색어별 상위 N건 초과, 블로그 220건·카페 50건) / `parse_error` / `type_excluded` (`config.KB_EXCLUDE_TYPES`에 지정된 문서 종류, 현재 cafe) / `blog_excluded` (`config.KB_EXCLUDE_BLOGS`에 지정된 양산형 블로그, eda_findings §7-5) |
| `raw_path` | TEXT | 직접 수집 문서의 원본 경로 |
| `extra` | TEXT | JSON 문자열 (소스별 부가 정보: 의안번호, 법령 시행일 등) |

- 인덱스: `(included, doc_type)`, `(published_year)`, `(dup_cluster_id)`
- 제외 문서도 보존하는 이유: 처리 전후 건수와 제외 사유를 리포트에 제시하고, 평가셋 정답 문서가 제외된 경우 대표 문서로 추적하기 위함

### 4-2. `dedup_log`

| 컬럼 | 타입 | 설명 |
|---|---|---|
| `doc_id` | TEXT | 제외된 문서 |
| `representative_id` | TEXT | 남긴 대표 문서 |
| `method` | TEXT | `url` / `body_hash` / `minhash` |
| `similarity` | REAL | MinHash 추정 Jaccard (해당 시) |
| `threshold` | REAL | 적용 임계값 |

### 4-3. `collect_log`

| 컬럼 | 타입 | 설명 |
|---|---|---|
| `id` | INTEGER PK | |
| `source` | TEXT | |
| `url` | TEXT | |
| `status` | TEXT | `ok` / `skip` / `error` |
| `http_status` | INTEGER | |
| `collected_at` | TEXT | |
| `message` | TEXT | 오류 내용 등 |

- 수집 계층은 원문 DB에 쓰지 않으므로 수집기는 `data/raw/collect_log.jsonl`에 기록하고, 인덱서가 이 테이블로 적재

### 4-4. `sections` — 지식베이스 포함 문서의 구획 (청킹 입력)

| 컬럼 | 타입 | 설명 |
|---|---|---|
| `doc_id` | TEXT | |
| `idx` | INTEGER | 구획 순번 |
| `section` | TEXT | 법령 조문(`제20조(과태료)`), 제개정이유 등. 없으면 빈 문자열 |
| `page` | INTEGER | PDF 원본 쪽 번호. 없으면 0 |
| `text` | TEXT | 정제 후 구획 본문 |

- `documents.body`는 구획 본문을 빈 줄로 이어 붙인 값

### 4-5. `chunks` — 청크 텍스트 (임베딩 입력)

| 컬럼 | 타입 | 설명 |
|---|---|---|
| `chunk_id` | TEXT | §5-1 (`collection`과 함께 기본키) |
| `collection` | TEXT | Chroma 컬렉션명 (청킹 설정별) |
| `doc_id` | TEXT | |
| `chunk_idx` | INTEGER | |
| `chunk_total` | INTEGER | |
| `section` | TEXT | 청크 시작 위치의 구획명 |
| `page` | INTEGER | 청크 시작 위치의 PDF 쪽 |
| `text` | TEXT | 임베딩 입력 텍스트 (prefix 포함) |
| `char_len` | INTEGER | |

- `python -m indexer chunk`가 생성하고 임베딩 단계가 읽어 Chroma에 적재
- 용도: 청크 길이 분포 검증, BM25 인덱스 생성(하이브리드 검색 확장), 디버깅

---

## 5. 청크와 벡터 DB (`store/chroma/`) — Day 2 오전 확정 후 변경 금지

### 5-1. 식별자와 컬렉션

| 항목 | 규칙 | 예 |
|---|---|---|
| 컬렉션명 | `kb_{청킹설정}_{임베딩약칭}` (영문·숫자·`_`만, 3자 이상) | `kb_A_te3s`, `kb_B_te3s`, `kb_A_te3s_notitle` |
| `chunk_id` | `{doc_id}#c{chunk_idx:03d}` | `news_20250212_3f9a1c2b#c000` |
| 거리 함수 | cosine (`hnsw:space = cosine`) | |

### 5-2. 임베딩 입력 텍스트

```
[{doc_type 한글}] {title} ({publisher}, {published_at})
{chunk 본문}
```

- 예: `[보도자료] 지역사랑상품권 국비 지원 확대 (행정안전부, 2025-08-04)`
- 제목 prefix 제거 실험(E2) 컬렉션은 본문만 사용

### 5-3. 청크 메타데이터

| 키 | 타입 | 용도 |
|---|---|---|
| `doc_id` | str | 원문 조회, 평가 |
| `chunk_idx` | int | 원문 위치 |
| `chunk_total` | int | 원문 위치 |
| `section` | str | 보고서 헤딩·법령 조문 번호 (없으면 빈 문자열) |
| `doc_type` | str | 필터, 배지 |
| `source_tier` | int | 신뢰도 구분 |
| `source` | str | |
| `title` | str | 출처 표기 |
| `publisher` | str | 출처 표기 |
| `url` | str | 원문 링크 (수동 자료는 빈 문자열) |
| `citation` | str | 참고문헌 형식 출처 (url이 없을 때 표시) |
| `page` | int | PDF 원본 쪽 번호 (청크 시작 쪽, 해당 없으면 0) |
| `published_at` | str | 출처 표기 |
| `published_year` | int | 기간 필터 (`$gte`·`$lte`) |
| `dup_cluster_id` | str | 평가 시 정답 판정 |
| `char_len` | int | 길이 분포 확인 |

- Chroma 메타데이터는 str·int·float·bool만 허용 → 결측은 빈 문자열 또는 0으로 저장, None 금지

### 5-4. 컬렉션 메타데이터

```json
{"hnsw:space": "cosine", "embedding_model": "text-embedding-3-small", "chunk_config": "A", "title_prefix": true, "built_at": "2026-11-07T10:20:00+09:00", "n_docs": 0, "n_chunks": 0}
```

- 챗봇·평가는 이 값으로 쿼리 임베딩 모델을 선택하여 문서·쿼리 모델 불일치를 방지

---

## 6. 평가셋 (`eval/eval_set.jsonl`)

| 필드 | 타입 | 필수 | 설명 |
|---|---|---|---|
| `id` | str | O | `q001` 형식 |
| `q` | str | O | 질문 |
| `type` | str | O | `fact` / `compare` / `temporal` / `synthesis` / `unanswerable` / `multiturn` |
| `gold_doc_ids` | list[str] | △ | 정답 근거 문서. 답변 불가는 빈 리스트 |
| `gold_patterns` | list[str] | △ | 정규식 목록. 모두 일치하는 지식베이스 문서를 정답으로 인정 (같은 사실을 다룬 뉴스가 수백 건인 문항용). `gold_doc_ids`와 둘 중 하나 이상 필수 (답변 불가 제외) |
| `answer_key` | str | O | 모범 답안 요지 |
| `required_points` | list[str] | | 채점 시 반드시 포함되어야 하는 사실 (정규식). `eval/validate_set.py`가 gold 문서 본문에 존재하는지 검증 |
| `source_need` | str | | 근거가 있어야 하는 문서 종류 (`press_release`, `law`, `report`, `news` 등) |
| `filters` | object | | 질문에 함께 적용할 필터 (`year_from`, `year_to`, `doc_types`) |
| `history` | list | | multiturn 문항의 이전 대화 `[{"role", "content"}]` |
| `note` | str | | 출제 근거, 답변 불가 확인 방법 등 |

```json
{"id": "q001", "q": "지역사랑상품권 부정유통 적발 시 과태료 상한과 근거 조항은?", "type": "fact", "gold_doc_ids": ["law_..."], "answer_key": "...", "required_points": ["과태료 상한 금액", "근거 조항 번호"], "source_need": "law", "note": ""}
{"id": "q055", "q": "해외 지역통화 사례와 비교하면 국내 지역화폐의 차이는?", "type": "unanswerable", "gold_doc_ids": [], "answer_key": "지식베이스에 근거 없음, 거절이 정답", "note": "전체 검색으로 근거 부재 확인"}
```

**정답 판정 규칙 (Hit@k)**
- 검색 결과 청크를 doc_id 단위로 중복 제거한 후 순위 산정
- 검색된 doc_id가 `gold_doc_ids`에 있거나, gold 문서와 같은 `dup_cluster_id`이면 적중
- `gold_patterns`가 있으면 검색된 문서의 제목+본문이 모든 정규식과 일치할 때도 적중
- gold 문서가 dedup으로 제외된 경우 해당 클러스터 대표 문서를 정답으로 간주
- 초안은 `eval/eval_set_draft.jsonl`, 검토 시트는 `eval/eval_review.csv`(Excel 저장 후 CP949 탭 구분, 열 이름으로 읽음), 사용자 검토 후 확정본을 `eval/eval_set.jsonl`로 저장

---

## 7. 실험 결과 (`eval/results/{YYYYMMDD-HHMM}_{tag}/`)

### 7-1. `config.json`

| 키 | 설명 |
|---|---|
| `tag` | 실험 태그 (`baseline`, `chunk_B`, `no_title`, `threshold`, `prompt_v2`, `hybrid`) |
| `changed_variable` | 기준 대비 변경한 단일 변수 |
| `collection` | 사용 컬렉션 |
| `embedding_model`, `chunk_config`, `title_prefix` | 컬렉션 메타데이터 사본 |
| `top_k`, `score_threshold` | 검색 설정 |
| `prompt_version`, `llm_model`, `temperature` | 생성 설정 |
| `eval_set_sha1` | 평가셋 파일 해시 (평가셋 변경 여부 추적) |
| `mode` | `retrieval_only` / `full` |
| `run_at` | 실행 시각 |

### 7-2. `per_question.jsonl`

| 키 | 설명 |
|---|---|
| `id`, `type` | 평가셋 문항 |
| `retrieved` | `[{"rank", "chunk_id", "doc_id", "score"}]` |
| `hit_rank` | 첫 적중 순위 (없으면 null) |
| `hit@1`, `hit@3`, `hit@5`, `rr` | 문항별 지표 |
| `top1_score` | 유사도 분포 분석용 |
| `answer` | 생성 답변 (`full` 모드) |
| `citations` | 답변 속 인용 번호와 doc_id 대응 `[{"n", "doc_id"}]` |
| `refused` | 거절 여부 |
| `latency_ms` | `{"retrieve", "generate", "total"}` |

### 7-3. `summary.json`

- 전체 및 `type`별 `hit@1`·`hit@3`·`hit@5`·`mrr`, `refusal_acc`, `false_refusal_rate`, `latency_p50`·`latency_p95`, 문항 수

### 7-4. `grade_sheet.csv` (직접 채점)

| 컬럼 | 값 |
|---|---|
| `id`, `type`, `q`, `answer_key`, `answer` | 자동 기입 |
| `accuracy` | 1 / 0.5 / 0 |
| `citation_correct` | Y / N / partial |
| `refusal_correct` | Y / N / NA |
| `failure_layer` | `data` / `preprocess` / `indexing` / `retrieval` / `generation` / 빈칸 |
| `memo` | 판정 근거 |

---

## 8. 중간 산출물 (`data/interim/`)

| 파일 | 생성 주체 | 내용 |
|---|---|---|
| `eda_summary.json` | `notebooks/eda.ipynb` | 구성·길이·관련도·중복·퍼널 수치 |
| `funnel.json` | 인덱서 | 실제 적재 기준 단계별 잔존 건수 (리포트 인용) |

---

## 9. 변경 관리

- §2 공통 규칙·§5 청크 메타데이터는 Day 2 오전 확정 이후 변경 시 영향 범위: 전체 재인덱싱 + 평가셋 `gold_doc_ids` 재검증
- 그 외 항목은 필드 추가만 허용 (기존 필드 의미 변경·삭제 금지)
