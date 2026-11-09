"""
원문 DB 읽기 전용 조회 (docs/api_spec.md §4-8)
- store/docs.db를 읽기 전용(URI mode=ro)으로 연결
- 챗봇 원문 보기와 필터 구성에 사용
"""
import sqlite3
from functools import lru_cache

import config


def _connect() -> sqlite3.Connection:
    con = sqlite3.connect(f'file:{config.DOCS_DB_PATH.as_posix()}?mode=ro', uri=True, check_same_thread=False)
    con.row_factory = sqlite3.Row
    return con


def get_document(doc_id: str) -> dict | None:
    con = _connect()
    row = con.execute('SELECT doc_id, doc_type, source_tier, title, publisher, url, citation, page_range, '
                      'published_at, body, body_len FROM documents WHERE doc_id = ?', (doc_id,)).fetchone()
    con.close()
    return dict(row) if row else None


@lru_cache(maxsize=1)
def kb_overview() -> dict:
    con = _connect()
    years = con.execute('SELECT MIN(published_year), MAX(published_year) FROM documents '
                        'WHERE included = 1 AND published_year > 0').fetchone()
    types = [r[0] for r in con.execute('SELECT doc_type, COUNT(*) c FROM documents WHERE included = 1 '
                                       'GROUP BY doc_type ORDER BY c DESC')]
    con.close()
    return {'year_min': int(years[0]), 'year_max': int(years[1]), 'doc_types': types,
            'doc_type_labels': dict(config.DOC_TYPE_LABEL), 'doc_type_tier': dict(config.DOC_TYPE_TIER)}


@lru_cache(maxsize=1)
def kb_distribution(collection: str = config.DEFAULT_COLLECTION) -> dict:
    """지식베이스 구성 (평가 대시보드용): 문서 종류별 문서·청크 수, 발행 연도별 문서 수"""
    con = _connect()
    # chunks.doc_id에는 색인이 없으므로 컬렉션 색인으로 문서별 청크 수를 먼저 집계한 뒤 결합
    by_type = [dict(r) for r in con.execute(
        'SELECT d.doc_type, COUNT(*) n_docs, SUM(COALESCE(c.n, 0)) n_chunks FROM documents d '
        'LEFT JOIN (SELECT doc_id, COUNT(*) n FROM chunks WHERE collection = ? GROUP BY doc_id) c '
        'ON c.doc_id = d.doc_id WHERE d.included = 1 GROUP BY d.doc_type ORDER BY n_docs DESC', (collection,))]
    by_year = [dict(r) for r in con.execute(
        'SELECT published_year year, COUNT(*) n_docs FROM documents WHERE included = 1 AND published_year > 0 '
        'GROUP BY published_year ORDER BY published_year')]
    con.close()
    for r in by_type:
        r['label'] = config.DOC_TYPE_LABEL.get(r['doc_type'], r['doc_type'])
        r['tier'] = config.DOC_TYPE_TIER.get(r['doc_type'], 0)
    return {'collection': collection, 'by_type': by_type, 'by_year': by_year}
