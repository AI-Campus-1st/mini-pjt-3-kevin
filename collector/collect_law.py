"""
법령 원문 수집 (국가법령정보 공동활용 Open API)
- 현행 법률·시행령·시행규칙 본문 JSON 저장
- 응답의 상세 링크에 OC 값이 포함되므로 manifest에는 OC 없는 공개 URL만 기록
"""
import json
import os

import config
from collector.storage import Manifest, PoliteSession, log_collect, make_doc_id, now_kst, rel_path

SEARCH_URL = 'https://www.law.go.kr/DRF/lawSearch.do'
SERVICE_URL = 'https://www.law.go.kr/DRF/lawService.do'
PUBLIC_URL = 'https://www.law.go.kr/법령/{name}'
OUT_DIR = config.RAW_DIR / 'law'


def fmt_date(yyyymmdd: str) -> str:
    return f'{yyyymmdd[:4]}-{yyyymmdd[4:6]}-{yyyymmdd[6:8]}' if len(yyyymmdd or '') == 8 else ''


def run() -> dict:
    oc = os.getenv('LAW_OPEN_API_OC', '')
    if not oc:
        raise RuntimeError('.env에 LAW_OPEN_API_OC 없음')
    sess = PoliteSession()
    manifest = Manifest()
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    r = sess.get(SEARCH_URL, params={'OC': oc, 'target': 'law', 'type': 'JSON', 'query': config.LAW_QUERY})
    laws = r.json()['LawSearch'].get('law', [])
    laws = laws if isinstance(laws, list) else [laws]
    stats = {'listed': len(laws), 'added': 0, 'skipped_exists': 0}
    for law in laws:
        mst = law['법령일련번호']
        name = law['법령명한글']
        key = f'law:{mst}'
        published_at = fmt_date(law.get('공포일자', ''))
        doc_id = make_doc_id('law', published_at, key)
        if doc_id in manifest:
            stats['skipped_exists'] += 1
            continue
        body = sess.get(SERVICE_URL, params={'OC': oc, 'target': 'law', 'type': 'JSON', 'MST': mst})
        path = OUT_DIR / f'{doc_id}.json'
        path.write_text(json.dumps(body.json(), ensure_ascii=False, indent=1), encoding='utf-8')
        manifest.add({
            'doc_id': doc_id, 'source': 'law', 'doc_type': 'law',
            'url': PUBLIC_URL.format(name=name.replace(' ', '')), 'canonical_key': key,
            'title': name, 'publisher': '법제처 국가법령정보센터',
            'published_at': published_at, 'collected_at': now_kst(), 'collect_method': 'api',
            'raw_path': rel_path(path), 'http_status': body.status_code,
            'note': f'{law.get("법령구분명", "")}, 공포 {published_at}, 시행 {fmt_date(law.get("시행일자", ""))}',
        })
        log_collect('law', f'MST={mst}', 'ok', body.status_code, name)
        stats['added'] += 1
    return stats
