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
