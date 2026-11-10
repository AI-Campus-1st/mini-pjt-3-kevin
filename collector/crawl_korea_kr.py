"""
정책브리핑(korea.kr) 보도자료 수집
- robots.txt: User-agent * Allow / (2026-11-05 확인, docs/source_access.md)
- 상세 페이지 본문은 스크립트로 렌더링되어 HTML에 텍스트가 없음 → 첨부 PDF(없으면 HWPX)를 원문으로 저장
- 이용 조건: 본문 텍스트 공공누리 제1유형(출처표시), 사진·이미지는 수집하지 않음
- 첨부가 없어 HTML만 저장된 보도자료는 인덱서가 본문을 추출할 수 없어 parse_error로 제외
- manifest에 등록되어 있으나 원본 파일이 없는 보도자료는 newsId로 원본만 다시 받아 복원 (manifest 행 추가 없음)
"""
import re
from urllib.parse import quote

from bs4 import BeautifulSoup

import config
from collector.storage import Manifest, PoliteSession, log_collect, make_doc_id, now_kst, rel_path

BASE = 'https://www.korea.kr'
LIST_URL = BASE + '/briefing/pressReleaseList.do'
VIEW_URL = BASE + '/briefing/pressReleaseView.do?newsId={news_id}'
OUT_DIR = config.RAW_DIR / 'korea_kr'


def list_items(sess: PoliteSession, keyword: str, end_date: str) -> list[dict]:
    """검색 결과 목록 전 페이지 순회"""
    items, page = [], 1
    while True:
        url = (f'{LIST_URL}?srchWord={quote(keyword)}&startDate={config.COLLECT_START_DATE}'
               f'&endDate={end_date}&period=direct&pageIndex={page}')
        r = sess.get(url)
        if r.status_code != 200:
            log_collect('korea_kr', url, 'error', r.status_code, 'list page')
            break
        soup = BeautifulSoup(r.text, 'lxml')
        links = soup.select('a[href*="pressReleaseView.do"]')
        if not links:
            break
        for a in links:
            m = re.search(r'newsId=(\d+)', a.get('href', ''))
            if not m:
                continue
            src = [s.get_text(strip=True) for s in a.select('span.source > span')]
            items.append({
                'news_id': m.group(1),
                'title': a.select_one('strong').get_text(strip=True) if a.select_one('strong') else '',
                'published_at': src[0] if src else '',
                'publisher': src[1] if len(src) > 1 else '',
                'keyword': keyword,
            })
        page += 1
    return items


def parse_view(html: str) -> dict:
    soup = BeautifulSoup(html, 'lxml')
    h1 = soup.find('h1')
    info = soup.select_one('div.info')
    info_text = info.get_text(' ', strip=True) if info else ''
    m = re.search(r'(\d{4})\.(\d{2})\.(\d{2})', info_text)
    attachments, seen = [], set()
    for a in soup.select('div.filedown a[href*="download.do"]'):
        href = a.get('href', '')
        name = a.get_text(' ', strip=True)
        if href in seen or name in ('내려받기', '바로보기', ''):
            continue
        seen.add(href)
        attachments.append({'name': name, 'url': BASE + href})
    return {
        'title': h1.get_text(strip=True) if h1 else '',
        'published_at': f'{m.group(1)}-{m.group(2)}-{m.group(3)}' if m else '',
        'attachments': attachments,
    }


def pick_attachment(attachments: list[dict]):
    """원문 텍스트 추출용 첨부 선택: PDF 우선, 없으면 HWPX"""
    for ext in ('.pdf', '.hwpx'):
        for att in attachments:
            if att['name'].lower().endswith(ext):
                return att, ext
    return None, None


def fetch_doc(sess: PoliteSession, news_id: str, doc_id: str) -> dict | None:
    """상세 페이지 HTML과 첨부 원본(PDF 우선, 없으면 HWPX) 저장. 상세 페이지 오류 시 None
    - raw_path: 첨부를 받았으면 첨부 파일, 아니면 HTML"""
    view_url = VIEW_URL.format(news_id=news_id)
    r = sess.get(view_url)
    if r.status_code != 200:
        log_collect('korea_kr', view_url, 'error', r.status_code, 'view page')
        return None
    html_path = OUT_DIR / f'{doc_id}.html'
    html_path.write_text(r.text, encoding='utf-8')
    view = parse_view(r.text)
    att, ext = pick_attachment(view['attachments'])
    att_records, raw_path = [], html_path
    if att:
        fr = sess.get(att['url'])
        if fr.status_code == 200 and fr.content:
            att_path = OUT_DIR / f'{doc_id}__att1{ext}'
            att_path.write_bytes(fr.content)
            raw_path = att_path
            att_records.append({'name': att['name'], 'url': att['url'], 'path': rel_path(att_path),
                                'mime': 'application/pdf' if ext == '.pdf' else 'application/hwp+zip'})
        else:
            log_collect('korea_kr', att['url'], 'error', fr.status_code, 'attachment')
    return {'url': view_url, 'http_status': r.status_code, 'view': view, 'raw_path': raw_path,
            'attachments': att_records, 'has_attachment': bool(att)}


def run(end_date: str | None = None) -> dict:
    end_date = end_date or now_kst()[:10]
    sess = PoliteSession()
    manifest = Manifest()
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    found = {}
    for kw in config.KOREA_KR_KEYWORDS:
        for it in list_items(sess, kw, end_date):
            found.setdefault(it['news_id'], it)
    stats = {'listed': len(found), 'added': 0, 'skipped_exists': 0, 'no_attachment': 0, 'errors': 0,
             'restored': 0, 'restore_mismatch': 0}

    for news_id, it in found.items():
        key = f'korea_kr:{news_id}'
        doc_id = make_doc_id('korea_kr', it['published_at'], key)
        if doc_id in manifest:
            stats['skipped_exists'] += 1
            continue
        got = fetch_doc(sess, news_id, doc_id)
        if got is None:
            stats['errors'] += 1
            continue
        if not got['has_attachment']:
            stats['no_attachment'] += 1
        view = got['view']
        manifest.add({
            'doc_id': doc_id, 'source': 'korea_kr', 'doc_type': 'press_release',
            'url': got['url'], 'canonical_key': key,
            'title': view['title'] or it['title'], 'publisher': it['publisher'],
            'published_at': view['published_at'] or it['published_at'],
            'collected_at': now_kst(), 'collect_method': 'crawl',
            'raw_path': rel_path(got['raw_path']), 'attachments': got['attachments'],
            'license': '공공누리 제1유형 (본문 텍스트)', 'http_status': got['http_status'],
            'note': f'검색어: {it["keyword"]}',
        })
        log_collect('korea_kr', got['url'], 'ok', got['http_status'])
        stats['added'] += 1

    # 등록 문서의 원본 복원 (새 환경: manifest만 있고 data/raw/가 없는 상태). manifest 행은 추가하지 않음
    for e in manifest.missing_raw('korea_kr'):
        got = fetch_doc(sess, e['canonical_key'].split(':', 1)[1], e['doc_id'])
        if got is None:
            stats['errors'] += 1
            continue
        same = rel_path(got['raw_path']) == e['raw_path']
        log_collect('korea_kr', got['url'], 'ok' if same else 'error', got['http_status'],
                    'raw restored' if same else f'raw restored as {rel_path(got["raw_path"])}, manifest {e["raw_path"]}')
        stats['restored' if same else 'restore_mismatch'] += 1
    return stats
