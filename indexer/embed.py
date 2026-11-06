"""
청크 임베딩·Chroma 적재 (data_spec §5)
- docs.db chunks 테이블의 컬렉션별 청크를 OpenAI 임베딩으로 변환하여 store/chroma/에 적재
- 재실행 시 이미 적재된 chunk_id는 건너뜀 (중단 후 재개 가능)
- 분당 토큰 한도(TPM) 안에서 호출하도록 최근 60초 사용량 기준으로 대기, 오류 시 지수 백오프 재시도
- 호출별 토큰 사용량은 data/interim/embed_log.jsonl에 기록 (비용·호출량 추적)
"""
import json
import sqlite3
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone

import chromadb
import tiktoken
from openai import APIConnectionError, APIStatusError, OpenAI, RateLimitError

import config

KST = timezone(timedelta(hours=9))
TPM_BUDGET = 900_000          # 계정 한도 1,000,000 TPM의 90%
BATCH_MAX_TOKENS = 100_000    # 요청당 상한 300,000 토큰보다 작게 잡아 호출 간격을 고르게 유지
BATCH_MAX_INPUTS = 1000
WORKERS = 3
MAX_RETRY = 6
LOG_PATH = config.INTERIM_DIR / 'embed_log.jsonl'

META_SQL = '''
SELECT c.chunk_id, c.doc_id, c.chunk_idx, c.chunk_total, c.section, c.page, c.text, c.char_len,
       d.doc_type, d.source_tier, d.source, d.title, d.publisher, d.url, d.citation,
       d.published_at, d.published_year, d.dup_cluster_id
FROM chunks c JOIN documents d USING(doc_id)
WHERE c.collection = ?
ORDER BY c.chunk_id
'''
STR_KEYS = ['doc_id', 'section', 'doc_type', 'source', 'title', 'publisher', 'url', 'citation', 'published_at',
            'dup_cluster_id']
INT_KEYS = ['chunk_idx', 'chunk_total', 'source_tier', 'page', 'published_year', 'char_len']


def now_kst() -> str:
    return datetime.now(KST).isoformat(timespec='seconds')


def chroma_client() -> chromadb.ClientAPI:
    config.CHROMA_DIR.mkdir(parents=True, exist_ok=True)
    return chromadb.PersistentClient(path=str(config.CHROMA_DIR))


def to_meta(row: sqlite3.Row) -> dict:
    """Chroma 메타데이터 (data_spec §5-3), None은 빈 문자열·0으로 대체"""
    m = {k: (row[k] or '') for k in STR_KEYS}
    m.update({k: int(row[k] or 0) for k in INT_KEYS})
    return m


class TokenPacer:
    """최근 60초 토큰 사용량이 예산을 넘지 않도록 호출 전 대기 (스레드 공유)"""

    def __init__(self, budget: int):
        self.budget = budget
        self.events = deque()
        self.lock = threading.Lock()

    def acquire(self, tokens: int):
        while True:
            with self.lock:
                now = time.time()
                while self.events and now - self.events[0][0] > 60:
                    self.events.popleft()
                used = sum(t for _, t in self.events)
                if used + tokens <= self.budget:
                    self.events.append((now, tokens))
                    return
                wait = 60 - (now - self.events[0][0]) + 0.5
            time.sleep(max(wait, 0.5))


def make_batches(rows: list, enc) -> list[list]:
    batches, cur, cur_tok = [], [], 0
    for r in rows:
        n = len(enc.encode(r['text']))
        if cur and (cur_tok + n > BATCH_MAX_TOKENS or len(cur) >= BATCH_MAX_INPUTS):
            batches.append((cur, cur_tok))
            cur, cur_tok = [], 0
        cur.append(r)
        cur_tok += n
    if cur:
        batches.append((cur, cur_tok))
    return batches


def embed_batch(client: OpenAI, model: str, pacer: TokenPacer, rows: list, est_tokens: int) -> tuple[list, int]:
    texts = [r['text'] for r in rows]
    for attempt in range(MAX_RETRY):
        pacer.acquire(est_tokens)
        try:
            resp = client.embeddings.create(model=model, input=texts)
            return [d.embedding for d in sorted(resp.data, key=lambda d: d.index)], resp.usage.total_tokens
        except (RateLimitError, APIConnectionError) as e:
            err = e
        except APIStatusError as e:
            if e.status_code < 500:
                raise
            err = e
        wait = 2 ** attempt * 5
        print(f'  재시도 {attempt + 1}/{MAX_RETRY} ({type(err).__name__}), {wait}s 대기', flush=True)
        time.sleep(wait)
    raise RuntimeError(f'임베딩 실패: {err}')


def run(preset: str, title_prefix: bool = True, model: str = config.EMBEDDING_MODEL) -> dict:
    coll_name = config.collection_name(preset, title_prefix, model)
    con = sqlite3.connect(config.DOCS_DB_PATH)
    con.row_factory = sqlite3.Row
    rows = con.execute(META_SQL, (coll_name,)).fetchall()
    con.close()
    if not rows:
        raise SystemExit(f'chunks 테이블에 {coll_name} 없음 → python -m indexer chunk 먼저 실행')

    db = chroma_client()
    coll = db.get_or_create_collection(
        coll_name, embedding_function=None,
        metadata={'hnsw:space': 'cosine', 'embedding_model': model, 'chunk_config': preset,
                  'title_prefix': title_prefix, 'built_at': now_kst(),
                  'n_docs': len({r['doc_id'] for r in rows}), 'n_chunks': len(rows)})
    done = set(coll.get(include=[])['ids'])
    todo = [r for r in rows if r['chunk_id'] not in done]
    enc = tiktoken.get_encoding('cl100k_base')
    batches = make_batches(todo, enc)
    total_est = sum(t for _, t in batches)
    print(f'[embed] {coll_name}: 전체 {len(rows)}, 적재됨 {len(done)}, 대상 {len(todo)} '
          f'({len(batches)}배치, 추정 {total_est:,} 토큰)', flush=True)

    client = OpenAI(max_retries=0, timeout=120)
    pacer = TokenPacer(TPM_BUDGET)
    used_tokens, added, t0 = 0, 0, time.time()
    config.INTERIM_DIR.mkdir(parents=True, exist_ok=True)
    with ThreadPoolExecutor(WORKERS) as pool, open(LOG_PATH, 'a', encoding='utf-8') as log:
        futs = {pool.submit(embed_batch, client, model, pacer, b, t): b for b, t in batches}
        for f in as_completed(futs):
            b = futs[f]
            vecs, tok = f.result()
            coll.add(ids=[r['chunk_id'] for r in b], embeddings=vecs,
                     documents=[r['text'] for r in b], metadatas=[to_meta(r) for r in b])
            used_tokens += tok
            added += len(b)
            log.write(json.dumps({'at': now_kst(), 'collection': coll_name, 'model': model, 'inputs': len(b),
                                  'tokens': tok}, ensure_ascii=False) + '\n')
            log.flush()
            el = time.time() - t0
            print(f'  {added}/{len(todo)} 청크, {used_tokens:,} 토큰, {el:.0f}s', flush=True)

    return {'collection': coll_name, 'chunks_total': len(rows), 'added': added, 'already': len(done),
            'chroma_count': coll.count(), 'tokens': used_tokens, 'elapsed_sec': round(time.time() - t0, 1)}
