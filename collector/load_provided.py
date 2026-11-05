"""
제공 뉴스 적재
- 지역화폐 키워드 CSV를 읽어 doc_id를 부여하고 data/raw/news/provided_news.jsonl로 저장
- 선별(관련도·중복)은 인덱서 단계에서 수행. 이 단계는 형식 정규화만 담당
- 제공 뉴스는 manifest에 기록하지 않음 (manifest는 직접 수집 문서 전용, data_spec §1)
"""
import json
import re
import unicodedata

import pandas as pd

import config
from collector.storage import log_collect, make_doc_id, mtime_kst

OUT_PATH = config.RAW_DIR / 'news' / 'provided_news.jsonl'


def find_provided_csv():
    # 파일명이 NFD로 저장된 경우가 있어 NFC 정규화 후 매칭
    for p in config.PROVIDED_DIR.iterdir():
        if unicodedata.normalize('NFC', p.name).startswith(config.PROVIDED_PREFIX):
            return p
    raise FileNotFoundError(f'{config.PROVIDED_PREFIX}* 파일 없음: {config.PROVIDED_DIR}')


def naver_canonical(url: str) -> str:
    """네이버 뉴스 URL 3종 형식을 oid/aid 기준 키로 정규화"""
    m = re.search(r'/article/(?:comment/)?(\d+)/(\d+)', url) or re.search(r'oid=(\d+).*?aid=(\d+)', url)
    return f'naver:{m.group(1)}/{m.group(2)}' if m else f'url:{url}'


def run() -> dict:
    path = find_provided_csv()
    collected_at = mtime_kst(path)
    df = pd.read_csv(path, encoding='utf-8-sig', dtype=str).fillna('')
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    seen = {}
    n_written = n_dup_id = 0
    with open(OUT_PATH, 'w', encoding='utf-8') as f:
        for row in df.itertuples(index=False):
            ts = row.발행일
            published_at = ts[:10]
            key = naver_canonical(row.원문URL)
            doc_id = make_doc_id('news', published_at, key)
            if doc_id in seen:
                # 동일 기사 중복 행: 첫 행만 유지
                if seen[doc_id] != key:
                    raise RuntimeError(f'doc_id 해시 충돌: {doc_id}')
                n_dup_id += 1
                continue
            seen[doc_id] = key
            rec = {
                'doc_id': doc_id, 'source': 'news', 'doc_type': 'news',
                'canonical_key': key, 'url': row.원문URL, 'title': row.제목,
                'publisher': row.매체명, 'published_at': published_at, 'published_ts': ts[:19],
                'collected_at': collected_at, 'collect_method': 'provided',
                'keywords': row.수집키워드, 'body': row.본문전문,
            }
            f.write(json.dumps(rec, ensure_ascii=False) + '\n')
            n_written += 1
    log_collect('news', str(path.name), 'ok', 0, f'rows={len(df)} written={n_written} dup_id={n_dup_id}')
    return {'rows': len(df), 'written': n_written, 'dup_id_skipped': n_dup_id, 'out': str(OUT_PATH)}
