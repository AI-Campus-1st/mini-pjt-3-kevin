"""
BM25 인덱스 구축 (하이브리드 검색 확장 E5)
- 입력: docs.db chunks 테이블의 컬렉션별 청크 텍스트 (Chroma 문서와 동일한 임베딩 입력 텍스트, 제목 prefix 포함)
- 색인어: rag.bm25.tokenize_many (검색 시 질의와 같은 규칙)
- 가중치: Okapi BM25, w(t, d) = idf(t) * tf * (k1 + 1) / (tf + k1 * (1 - b + b * dl / avgdl)), idf = ln(1 + (N - df + 0.5) / (df + 0.5))
- 출력: store/bm25/{collection}/ weights.npz (청크 × 어휘 희소 행렬), vocab.json, chunk_ids.json, meta.json
"""
import json
import math
import sqlite3
import time
from collections import Counter
from datetime import datetime, timedelta, timezone

import numpy as np
from scipy import sparse

import config
from rag.bm25 import index_dir, tokenize_many

K1 = 1.5
B = 0.75
BATCH = 2000


def run(collection: str) -> dict:
    con = sqlite3.connect(config.DOCS_DB_PATH)
    rows = con.execute('SELECT chunk_id, text FROM chunks WHERE collection = ? ORDER BY chunk_id',
                       (collection,)).fetchall()
    if not rows:
        raise SystemExit(f'chunks 테이블에 {collection} 청크 없음')
    t0 = time.time()
    docs = []
    for i in range(0, len(rows), BATCH):
        docs.extend(tokenize_many([r[1] for r in rows[i:i + BATCH]]))
        print(f'  형태소 분석 {min(i + BATCH, len(rows))}/{len(rows)} ({time.time() - t0:.0f}s)', flush=True)

    vocab: dict[str, int] = {}
    df = Counter()
    tfs = []
    for terms in docs:
        tf = Counter(terms)
        tfs.append(tf)
        df.update(tf.keys())
        for t in tf:
            vocab.setdefault(t, len(vocab))
    n = len(docs)
    lens = np.array([len(d) for d in docs], dtype=np.float64)
    avgdl = float(lens.mean())
    idf = {t: math.log(1 + (n - c + 0.5) / (c + 0.5)) for t, c in df.items()}

    data, ri, ci = [], [], []
    for r, tf in enumerate(tfs):
        norm = K1 * (1 - B + B * lens[r] / avgdl)
        for t, f in tf.items():
            data.append(idf[t] * f * (K1 + 1) / (f + norm))
            ri.append(r)
            ci.append(vocab[t])
    w = sparse.csr_matrix((np.array(data, dtype=np.float32), (ri, ci)), shape=(n, len(vocab)))

    d = index_dir(collection)
    d.mkdir(parents=True, exist_ok=True)
    sparse.save_npz(d / 'weights.npz', w)
    terms = [None] * len(vocab)
    for t, i in vocab.items():
        terms[i] = t
    (d / 'vocab.json').write_text(json.dumps(terms, ensure_ascii=False), encoding='utf-8')
    (d / 'chunk_ids.json').write_text(json.dumps([r[0] for r in rows]), encoding='utf-8')
    meta = {'collection': collection, 'n_chunks': n, 'vocab_size': len(vocab), 'avgdl': round(avgdl, 2),
            'k1': K1, 'b': B, 'nnz': int(w.nnz), 'user_words': config.BM25_USER_WORDS,
            'built_at': datetime.now(timezone(timedelta(hours=9))).isoformat(timespec='seconds')}
    (d / 'meta.json').write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding='utf-8')
    return meta
