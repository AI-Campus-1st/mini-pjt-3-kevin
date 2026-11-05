"""
적재 검증 (가이드 4-5: 문서 수, 청크 수, 빈 청크 여부)
- 결과는 data/interim/verify_{collection}.json으로 저장
"""
import json
import sqlite3

import pandas as pd

import config


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
        ch = pd.read_sql('SELECT c.doc_id, c.char_len, d.doc_type FROM chunks c JOIN documents d USING(doc_id) WHERE c.collection = ?',
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
        }
        (config.INTERIM_DIR / f'verify_{c}.json').write_text(json.dumps(out[c], ensure_ascii=False, indent=1), encoding='utf-8')
    con.close()
    return out
