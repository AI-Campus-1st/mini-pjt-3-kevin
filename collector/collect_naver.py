"""
블로그·카페 요약문 수집 (NAVER API HUB 검색 API)
- blog.naver.com, cafe.naver.com robots.txt가 RAG 목적 봇 접근을 금지 → 본문 미수집, API 응답(제목·요약문·링크)만 사용
- 무료 제공량(일 25,000건)을 상한으로 두고, 날짜별 누적 호출 수가 상한에 도달하면 중단
"""
import html
import json
import os
import re

import config
from collector.storage import Manifest, PoliteSession, log_collect, make_doc_id, now_kst, rel_path

OUT_DIR = config.RAW_DIR / 'naver'
QUOTA_PATH = OUT_DIR / 'quota.json'


def strip_tags(s: str) -> str:
    return html.unescape(re.sub(r'<[^>]+>', '', s or '')).strip()


class Quota:
    def __init__(self):
        self.data = json.loads(QUOTA_PATH.read_text(encoding='utf-8')) if QUOTA_PATH.exists() else {}
        self.today = now_kst()[:10]

    def used(self) -> int:
        return self.data.get(self.today, 0)

    def spend(self) -> None:
        self.data[self.today] = self.used() + 1
        QUOTA_PATH.write_text(json.dumps(self.data, ensure_ascii=False, indent=1), encoding='utf-8')


def run() -> dict:
    cid, secret = os.getenv('NAVER_CLIENT_ID', ''), os.getenv('NAVER_CLIENT_SECRET', '')
    if not (cid and secret):
        raise RuntimeError('.env에 NAVER_CLIENT_ID / NAVER_CLIENT_SECRET 없음')
    headers = {'X-NCP-APIGW-API-KEY-ID': cid, 'X-NCP-APIGW-API-KEY': secret}
    sess = PoliteSession(delay=0.3)
    manifest = Manifest()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    quota = Quota()
    stats = {'calls': 0, 'items': 0, 'added': 0, 'dup_link': 0}

    for kind in config.NAVER_KINDS:
        source = 'blog' if kind == 'blog' else 'cafe'
        kind_dir = OUT_DIR / kind
        kind_dir.mkdir(exist_ok=True)
        for q in config.NAVER_QUERIES:
            items = []
            for start in range(1, config.NAVER_MAX_START + 1, config.NAVER_DISPLAY):
                if quota.used() >= config.NAVER_DAILY_LIMIT:
                    log_collect(source, q, 'skip', 0, 'daily quota reached')
                    return stats
                r = sess.get(config.NAVER_ENDPOINT.format(kind=kind), headers=headers,
                             params={'query': q, 'display': config.NAVER_DISPLAY, 'start': start, 'sort': 'sim'})
                quota.spend()
                stats['calls'] += 1
                if r.status_code != 200:
                    log_collect(source, f'{kind}:{q}:{start}', 'error', r.status_code, r.text[:200])
                    break
                batch = r.json().get('items', [])
                items += batch
                if len(batch) < config.NAVER_DISPLAY:
                    break
            safe_q = re.sub(r'\s+', '_', q)
            (kind_dir / f'{safe_q}.json').write_text(json.dumps(items, ensure_ascii=False, indent=1), encoding='utf-8')
            stats['items'] += len(items)
            for it in items:
                link = it.get('link', '')
                postdate = it.get('postdate', '')
                published_at = f'{postdate[:4]}-{postdate[4:6]}-{postdate[6:8]}' if len(postdate) == 8 else ''
                doc_id = make_doc_id(source, published_at, f'{source}:{link}')
                if doc_id in manifest:
                    stats['dup_link'] += 1
                    continue
                manifest.add({
                    'doc_id': doc_id, 'source': source, 'doc_type': source, 'url': link,
                    'canonical_key': f'{source}:{link}', 'title': strip_tags(it.get('title')),
                    'publisher': it.get('bloggername') or it.get('cafename') or '',
                    'published_at': published_at, 'collected_at': now_kst(), 'collect_method': 'api',
                    'raw_path': rel_path(kind_dir / f'{safe_q}.json'), 'http_status': 200,
                    'note': f'검색어: {q}. 요약문만 수집',
                })
                stats['added'] += 1
            log_collect(source, f'{kind}:{q}', 'ok', 200, f'{len(items)} items')
    return stats
