"""
수동 다운로드 자료 등록
- data/manual_sources.csv의 서지 정보로 data/raw/reports/의 PDF를 manifest에 등록 (data_spec §3-3)
- canonical_key: file:{sha1(파일 바이트)}
"""
import pandas as pd

import config
from collector.storage import Manifest, file_sha1, log_collect, make_doc_id, rel_path


def run() -> dict:
    df = pd.read_csv(config.MANUAL_SOURCES_PATH, dtype=str, keep_default_na=False)
    manifest = Manifest()
    stats = {'rows': len(df), 'added': 0, 'skipped_exists': 0, 'missing_file': 0}
    for row in df.itertuples(index=False):
        path = config.REPORTS_DIR / row.file
        if not path.exists():
            log_collect(row.source, row.file, 'error', 0, 'file not found')
            stats['missing_file'] += 1
            continue
        key = f'file:{file_sha1(path)}'
        doc_id = make_doc_id(row.source, row.published_at, key)
        if doc_id in manifest:
            stats['skipped_exists'] += 1
            continue
        manifest.add({
            'doc_id': doc_id, 'source': row.source, 'doc_type': row.doc_type,
            'url': '', 'canonical_key': key, 'title': row.title, 'publisher': row.publisher,
            'published_at': row.published_at, 'collected_at': row.downloaded_at,
            'collect_method': 'manual', 'raw_path': rel_path(path),
            'citation': row.citation, 'page_range': row.include_pages,
            'note': row.note, 'authors': row.authors, 'series': row.series,
        })
        log_collect(row.source, row.file, 'ok', 0, 'manual register')
        stats['added'] += 1
    return stats
