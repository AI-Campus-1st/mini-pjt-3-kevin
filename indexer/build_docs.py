"""
원문 DB(store/docs.db) 구축
- 입력: data/raw/news/provided_news.jsonl (제공 뉴스), data/manifest.jsonl (직접 수집), data/raw/collect_log.jsonl
- 처리 순서: 파싱 → 정제 → 관련도 → 완전 중복 → near-dup → 길이 기준 (EDA 퍼널과 동일 순서)
- 제외 문서도 included=0과 제외 사유로 보존 (data_spec §4-1)
- 매 실행 시 docs.db를 새로 생성 (재현성)
"""
import json
import re
import sqlite3
from collections import Counter, defaultdict

import config
from indexer import clean, dedup, parse, relevance

SCHEMA = '''
CREATE TABLE documents (
    doc_id TEXT PRIMARY KEY, source TEXT, doc_type TEXT, source_tier INTEGER,
    title TEXT, publisher TEXT, url TEXT, citation TEXT, page_range TEXT,
    published_at TEXT, published_year INTEGER, published_ts TEXT,
    collected_at TEXT, collect_method TEXT, keywords TEXT,
    body TEXT, body_len INTEGER, raw_len INTEGER,
    relevance_rule TEXT, dup_cluster_id TEXT, is_representative INTEGER,
    included INTEGER, exclude_reason TEXT, raw_path TEXT, extra TEXT
);
CREATE INDEX idx_doc_incl ON documents(included, doc_type);
CREATE INDEX idx_doc_year ON documents(published_year);
CREATE INDEX idx_doc_cluster ON documents(dup_cluster_id);
CREATE TABLE sections (
    doc_id TEXT, idx INTEGER, section TEXT, page INTEGER, text TEXT,
    PRIMARY KEY (doc_id, idx)
);
CREATE TABLE dedup_log (
    doc_id TEXT, representative_id TEXT, method TEXT, similarity REAL, threshold REAL
);
CREATE TABLE collect_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT, url TEXT, status TEXT,
    http_status INTEGER, collected_at TEXT, message TEXT
);
CREATE TABLE chunks (
    chunk_id TEXT, collection TEXT, doc_id TEXT, chunk_idx INTEGER, chunk_total INTEGER,
    section TEXT, page INTEGER, text TEXT, char_len INTEGER,
    PRIMARY KEY (collection, chunk_id)
);
CREATE INDEX idx_chunks_coll ON chunks(collection);
'''

NEWS_PATH = config.RAW_DIR / 'news' / 'provided_news.jsonl'
URL_UNIQUE_TYPES = {'news', 'press_release', 'blog', 'cafe'}


def blog_id(url: str) -> str:
    """네이버 블로그 URL에서 블로그 ID 추출 (blog.naver.com/{id}/{글번호})"""
    m = re.search(r'blog\.naver\.com/([^/?#]+)', url or '')
    return m.group(1) if m else ''


def read_jsonl(path):
    with open(path, encoding='utf-8') as f:
        return [json.loads(l) for l in f if l.strip()]


def load_candidates() -> list[dict]:
    docs = []
    for r in read_jsonl(NEWS_PATH):
        raw = r.pop('body')
        body = clean.clean_news(raw)
        docs.append({**r, 'citation': '', 'page_range': '', 'raw_path': 'data/raw/news/provided_news.jsonl',
                     'raw_len': len(raw), 'sections': [{'section': '', 'page': 0, 'text': body}], 'body': body,
                     'extra': {}})
    for e in read_jsonl(config.MANIFEST_PATH):
        try:
            secs = parse.parse_entry(e)
            is_pdf = e['raw_path'].lower().endswith('.pdf')
            raw_len = sum(len(s['text']) for s in secs)
            secs = clean.clean_sections(secs, is_pdf)
            err = ''
        except Exception as ex:  # 파싱 실패 문서도 사유와 함께 기록
            secs, raw_len, err = [], 0, f'{type(ex).__name__}: {ex}'
        body = '\n\n'.join(s['text'] for s in secs)
        extra = {k: e[k] for k in ('authors', 'series', 'note', 'license', 'attachments') if e.get(k)}
        if err:
            extra['parse_error'] = err
        if e['source'] in ('blog', 'cafe'):
            extra['search_rank'] = parse.naver_rank(config.ROOT / e['raw_path'], e['url'])
        docs.append({
            'doc_id': e['doc_id'], 'source': e['source'], 'doc_type': e['doc_type'],
            'canonical_key': e['canonical_key'], 'url': e.get('url', ''), 'title': e['title'],
            'publisher': e['publisher'], 'published_at': e['published_at'], 'published_ts': '',
            'collected_at': e['collected_at'], 'collect_method': e['collect_method'], 'keywords': '',
            'citation': e.get('citation', ''), 'page_range': e.get('page_range', ''),
            'raw_path': e['raw_path'], 'raw_len': raw_len, 'sections': secs, 'body': body, 'extra': extra,
        })
    return docs


def run() -> dict:
    docs = load_candidates()
    by_id = {d['doc_id']: d for d in docs}
    funnel = defaultdict(Counter)

    for d in docs:
        t = d['doc_type']
        funnel[t]['0_candidates'] += 1
        d.update(included=0, exclude_reason='', dup_cluster_id=d['doc_id'], is_representative=1)
        if t in config.KB_EXCLUDE_TYPES:
            d['exclude_reason'] = 'type_excluded'
            continue
        if t == 'blog' and blog_id(d['url']) in config.KB_EXCLUDE_BLOGS:
            d['exclude_reason'] = 'blog_excluded'
            continue
        if d['extra'].get('parse_error'):
            d['exclude_reason'] = 'parse_error'
            continue
        if not d['body']:
            d['exclude_reason'] = 'empty_body'
            continue
        ok, rule = relevance.is_relevant(t, d['collect_method'], d['title'], d['body'])
        d['relevance_rule'] = rule
        if not ok:
            d['exclude_reason'] = 'irrelevant'
            continue
        if d['extra'].get('search_rank', 0) > config.SNIPPET_TOP_PER_QUERY.get(t, 10 ** 9):
            d['exclude_reason'] = 'rank_cap'
            continue
        funnel[t]['1_relevant'] += 1

    # doc_type 그룹별 중복 처리
    logs = []
    groups = defaultdict(list)
    for d in docs:
        if not d['exclude_reason']:
            groups[d['doc_type']].append(d)
    for t, group in groups.items():
        # URL 일치 판정은 URL이 문서 고유 식별자인 종류에만 적용 (의안 목록 등은 공통 포털 URL 사용)
        url_unique = t in URL_UNIQUE_TYPES
        cluster_of, glogs = dedup.dedup_group(
            [{'doc_id': d['doc_id'], 'url': d['url'] if url_unique else '', 'body': d['body']} for d in group])
        logs += glogs
        for d in group:
            rep = cluster_of[d['doc_id']]
            d['dup_cluster_id'] = rep
            d['is_representative'] = int(rep == d['doc_id'])
        for lg in glogs:
            by_id[lg['doc_id']]['exclude_reason'] = 'exact_dup' if lg['method'] in ('url', 'body_hash') else 'near_dup'
        funnel[t]['2_after_exact'] = sum(1 for d in group if by_id[d['doc_id']]['exclude_reason'] != 'exact_dup')
        funnel[t]['3_after_neardup'] = sum(1 for d in group if not by_id[d['doc_id']]['exclude_reason'])

    for d in docs:
        if d['exclude_reason']:
            continue
        if d['doc_type'] == 'news' and len(d['body']) < config.MIN_BODY_LEN:
            d['exclude_reason'] = 'too_short'
            continue
        d['included'] = 1
        funnel[d['doc_type']]['4_included'] += 1

    config.STORE_DIR.mkdir(parents=True, exist_ok=True)
    if config.DOCS_DB_PATH.exists():
        config.DOCS_DB_PATH.unlink()
    con = sqlite3.connect(config.DOCS_DB_PATH)
    con.executescript(SCHEMA)
    rows, sec_rows = [], []
    for d in docs:
        year = int(d['published_at'][:4]) if d['published_at'][:4].isdigit() else 0
        rows.append((
            d['doc_id'], d['source'], d['doc_type'], config.DOC_TYPE_TIER[d['doc_type']],
            d['title'], d['publisher'], d['url'], d['citation'], d['page_range'],
            d['published_at'], year, d.get('published_ts', ''), d['collected_at'], d['collect_method'],
            d.get('keywords', ''), d['body'], len(d['body']), d['raw_len'], d.get('relevance_rule', ''),
            d['dup_cluster_id'], d['is_representative'], d['included'], d['exclude_reason'],
            d['raw_path'], json.dumps(d['extra'], ensure_ascii=False),
        ))
        if d['included']:
            sec_rows += [(d['doc_id'], i, s['section'], s['page'], s['text']) for i, s in enumerate(d['sections'])]
    con.executemany(f'INSERT INTO documents VALUES ({",".join("?" * 25)})', rows)
    con.executemany('INSERT INTO sections VALUES (?,?,?,?,?)', sec_rows)
    con.executemany('INSERT INTO dedup_log VALUES (?,?,?,?,?)',
                    [(l['doc_id'], l['representative_id'], l['method'], l['similarity'], l['threshold']) for l in logs])
    if config.COLLECT_LOG_PATH.exists():
        con.executemany('INSERT INTO collect_log (source, url, status, http_status, collected_at, message) VALUES (?,?,?,?,?,?)',
                        [(r['source'], r['url'], r['status'], r['http_status'], r['collected_at'], r['message'])
                         for r in read_jsonl(config.COLLECT_LOG_PATH)])
    con.commit()
    con.close()

    summary = {t: dict(sorted(c.items())) for t, c in sorted(funnel.items())}
    summary['_total_included'] = sum(1 for d in docs if d['included'])
    summary['_exclude_reasons'] = dict(Counter(d['exclude_reason'] for d in docs if d['exclude_reason']))
    config.INTERIM_DIR.mkdir(parents=True, exist_ok=True)
    (config.INTERIM_DIR / 'funnel.json').write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding='utf-8')
    return summary
