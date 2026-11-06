"""
적재 검증 (가이드 4-5: 문서 수, 청크 수, 빈 청크 여부)
- 원문 DB: 포함 문서 분포, 컬렉션별 청크 길이 분포
- 벡터 DB: Chroma 적재 수와 chunks 테이블 일치 여부, 컬렉션 메타데이터, 저장 벡터 자기 검색(top-1이 자기 자신인지)
- 결과는 data/interim/verify_{collection}.json으로 저장
"""
import json
import random
import sqlite3

import pandas as pd

import config

SELF_CHECK_N = 30


def check_chroma(name: str, chunk_ids: list[str]) -> dict:
    """Chroma 컬렉션 적재 상태 (임베딩 전이면 exists=False)"""
    from indexer.embed import chroma_client
    if not config.CHROMA_DIR.exists():
        return {'exists': False}
    db = chroma_client()
    if name not in [c.name for c in db.list_collections()]:
        return {'exists': False}
    coll = db.get_collection(name, embedding_function=None)
    stored = set(coll.get(include=[])['ids'])
    expected = set(chunk_ids)
    sample = random.Random(0).sample(sorted(stored), min(SELF_CHECK_N, len(stored)))
    got = coll.get(ids=sample, include=['embeddings'])
    res = coll.query(query_embeddings=got['embeddings'], n_results=1, include=['distances'])
    self_top1 = sum(1 for cid, ids in zip(got['ids'], res['ids']) if ids and ids[0] == cid)
    return {
        'exists': True,
        'count': coll.count(),
        'missing_in_chroma': len(expected - stored),
        'extra_in_chroma': len(stored - expected),
        'metadata': coll.metadata,
        'space': coll.configuration.get('hnsw', {}).get('space'),
        'self_top1': f'{self_top1}/{len(sample)}',
    }


def run(collection: str | None = None) -> dict:
    con = sqlite3.connect(config.DOCS_DB_PATH)
    docs = pd.read_sql('SELECT doc_id, doc_type, source_tier, body_len, published_year, included, exclude_reason FROM documents', con)
    inc = docs[docs.included == 1]
    out = {
        'documents_total': int(len(docs)),
        'documents_included': int(len(inc)),
        'included_by_type': inc.doc_type.value_counts().to_dict(),
        'included_by_tier': {int(k): int(v) for k, v in inc.source_tier.value_counts().sort_index().items()},
        'included_by_year': {int(k): int(v) for k, v in inc.published_year.value_counts().sort_index().items()},
        'excluded_by_reason': docs[docs.included == 0].exclude_reason.value_counts().to_dict(),
    }
    colls = [collection] if collection else [r[0] for r in con.execute('SELECT DISTINCT collection FROM chunks')]
    for c in colls:
        ch = pd.read_sql('SELECT c.chunk_id, c.doc_id, c.char_len, d.doc_type FROM chunks c JOIN documents d USING(doc_id) WHERE c.collection = ?',
                         con, params=(c,))
        q = ch.char_len.quantile([.05, .5, .95]).astype(int).to_dict() if len(ch) else {}
        out[c] = {
            'chunks': int(len(ch)),
            'docs_with_chunks': int(ch.doc_id.nunique()),
            'docs_without_chunks': int(len(set(inc.doc_id) - set(ch.doc_id))),
            'empty_or_tiny_lt50': int((ch.char_len < 50).sum()),
            'over_1500': int((ch.char_len > 1500).sum()),
            'char_len_p5_p50_p95': [q.get(.05), q.get(.5), q.get(.95)],
            'chunks_by_type': ch.doc_type.value_counts().to_dict(),
            'chroma': check_chroma(c, ch.chunk_id.tolist()),
        }
        (config.INTERIM_DIR / f'verify_{c}.json').write_text(json.dumps(out[c], ensure_ascii=False, indent=1), encoding='utf-8')
    con.close()
    return out
