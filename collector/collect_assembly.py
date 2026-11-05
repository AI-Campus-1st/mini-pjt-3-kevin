"""
의안 목록 수집 (열린국회정보 Open API, 국회의원 발의법률안)
- 의안정보시스템은 robots.txt 전면 차단 → API만 사용
- 대수(AGE)별 법안 목록을 하나의 문서로 저장 (개별 의안 행은 제목이 동일하여 단독 문서로는 변별력 없음)
"""
import json
import os

import config
from collector.storage import Manifest, PoliteSession, log_collect, make_doc_id, now_kst, rel_path

API_URL = 'https://open.assembly.go.kr/portal/openapi/' + config.ASSEMBLY_SERVICE
OUT_DIR = config.RAW_DIR / 'assembly'


def fetch_all(sess: PoliteSession, key: str, age: int) -> list[dict]:
    rows, page = [], 1
    while True:
        r = sess.get(API_URL, params={'KEY': key, 'Type': 'json', 'pIndex': page, 'pSize': 100,
                                      'AGE': age, 'BILL_NAME': config.ASSEMBLY_BILL_NAME})
        d = r.json()
        block = d.get(config.ASSEMBLY_SERVICE)
        if not isinstance(block, list):
            break
        total = block[0]['head'][0]['list_total_count']
        rows += block[1].get('row', [])
        if len(rows) >= total:
            break
        page += 1
    return rows


def run() -> dict:
    key = os.getenv('ASSEMBLY_API_KEY', '')
    if not key:
        raise RuntimeError('.env에 ASSEMBLY_API_KEY 없음')
    sess = PoliteSession(delay=1.0)
    manifest = Manifest()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    stats = {}
    for age in config.ASSEMBLY_AGES:
        rows = fetch_all(sess, key, age)
        canonical = f'assembly:bills:{age}:{config.ASSEMBLY_BILL_NAME}'
        dates = sorted(r.get('PROPOSE_DT') or '' for r in rows)
        published_at = dates[-1] if dates else ''
        doc_id = make_doc_id('assembly', published_at, canonical)
        path = OUT_DIR / f'bills_age{age}.json'
        path.write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding='utf-8')
        stats[f'age{age}'] = len(rows)
        if doc_id in manifest or not rows:
            continue
        manifest.add({
            'doc_id': doc_id, 'source': 'assembly', 'doc_type': 'bill',
            'url': 'https://open.assembly.go.kr', 'canonical_key': canonical,
            'citation': f'열린국회정보 Open API 「국회의원 발의법률안」 (제{age}대, 법안명 "{config.ASSEMBLY_BILL_NAME}" 검색, {now_kst()[:10]} 조회).',
            'title': f'제{age}대 국회 지역사랑상품권법 개정안 발의 현황', 'publisher': '국회 (열린국회정보)',
            'published_at': published_at, 'collected_at': now_kst(), 'collect_method': 'api',
            'raw_path': rel_path(path), 'http_status': 200,
            'note': f'국회의원 발의법률안 API, BILL_NAME={config.ASSEMBLY_BILL_NAME}, {len(rows)}건',
        })
        log_collect('assembly', f'AGE={age}', 'ok', 200, f'{len(rows)} bills')
    return stats
