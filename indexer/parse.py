"""
원본 → 텍스트 파싱
- 반환 형식: sections 리스트 [{'section': 구획명, 'page': 쪽 번호(없으면 0), 'text': 본문}]
- 청킹 B 설정은 이 구획 단위를 우선 경계로 사용
"""
import html
import json
import re
import zipfile
from pathlib import Path

import pymupdf

import config


def parse_page_range(spec: str, n_pages: int) -> list[int]:
    """'22,112-118' → [22, 112, ..., 118] (1부터 시작). 빈 값이면 전체 쪽"""
    if not spec:
        return list(range(1, n_pages + 1))
    pages = []
    for part in spec.split(','):
        part = part.strip()
        if '-' in part:
            a, b = part.split('-')
            pages += list(range(int(a), int(b) + 1))
        elif part:
            pages.append(int(part))
    return [p for p in pages if 1 <= p <= n_pages]


def pdf_sections(path: Path, page_range: str = '') -> list[dict]:
    doc = pymupdf.open(path)
    out = []
    for p in parse_page_range(page_range, doc.page_count):
        out.append({'section': '', 'page': p, 'text': doc[p - 1].get_text()})
    return out


def hwpx_sections(path: Path) -> list[dict]:
    """HWPX(zip+XML)에서 본문 문단 텍스트 추출"""
    out = []
    with zipfile.ZipFile(path) as z:
        names = sorted(n for n in z.namelist() if re.match(r'Contents/section\d+\.xml', n))
        for n in names:
            xml = z.read(n).decode('utf-8', errors='ignore')
            paras = re.findall(r'<hp:p[ >].*?</hp:p>', xml, flags=re.S)
            lines = [''.join(re.findall(r'<hp:t[^>]*>(.*?)</hp:t>', p, flags=re.S)) for p in paras]
            out.append({'section': '', 'page': 0, 'text': '\n'.join(l for l in lines if l.strip())})
    return out


def _law_unit_text(unit: dict) -> str:
    lines = [unit.get('조문내용', '')]
    hangs = unit.get('항', [])
    hangs = hangs if isinstance(hangs, list) else [hangs]
    for h in hangs:
        if isinstance(h, dict):
            if h.get('항내용'):
                lines.append(h['항내용'] if isinstance(h['항내용'], str) else ' '.join(map(str, h['항내용'])))
            hos = h.get('호', [])
            hos = hos if isinstance(hos, list) else [hos]
            for ho in hos:
                if isinstance(ho, dict):
                    lines.append(ho.get('호내용', ''))
                    moks = ho.get('목', [])
                    moks = moks if isinstance(moks, list) else [moks]
                    for mok in moks:
                        if isinstance(mok, dict):
                            c = mok.get('목내용', '')
                            lines.append(c if isinstance(c, str) else ' '.join(map(str, c)))
    return '\n'.join(str(l).strip() for l in lines if l)


def law_sections(path: Path) -> list[dict]:
    """법령 본문 JSON → 조문 단위 구획 + 제개정이유"""
    law = json.loads(path.read_text(encoding='utf-8'))['법령']
    units = law.get('조문', {}).get('조문단위', [])
    units = units if isinstance(units, list) else [units]
    out = []
    for u in units:
        if u.get('조문여부') != '조문':
            continue
        title = u.get('조문제목', '')
        out.append({'section': f'제{u.get("조문번호")}조({title})' if title else f'제{u.get("조문번호")}조',
                    'page': 0, 'text': _law_unit_text(u)})
    reason = law.get('제개정이유', {}).get('제개정이유내용')
    if reason:
        text = reason if isinstance(reason, str) else '\n'.join(
            ' '.join(map(str, r)) if isinstance(r, list) else str(r) for r in reason)
        out.append({'section': '제개정이유', 'page': 0, 'text': text})
    return out


def assembly_sections(path: Path) -> list[dict]:
    """의안 목록 JSON → 의안별 한 줄 요약 (발의일 순)"""
    rows = json.loads(path.read_text(encoding='utf-8'))
    rows = sorted(rows, key=lambda r: r.get('PROPOSE_DT') or '')
    lines = []
    for r in rows:
        result = r.get('PROC_RESULT') or '계류(처리 결과 없음)'
        lines.append(f'- 의안번호 {r.get("BILL_NO")} | 제안일 {r.get("PROPOSE_DT")} | 대표발의 {r.get("RST_PROPOSER") or r.get("PROPOSER")}'
                     f' ({r.get("PROPOSER")}) | 소관위 {r.get("COMMITTEE") or "-"} | 처리결과 {result}'
                     f'{" (" + r["PROC_DT"] + ")" if r.get("PROC_DT") else ""}')
    head = f'{rows[0].get("BILL_NAME") if rows else ""} 발의 현황 (총 {len(rows)}건)'
    return [{'section': '', 'page': 0, 'text': head + '\n' + '\n'.join(lines)}]


_naver_cache: dict = {}


def _naver_items(raw_path: Path) -> dict:
    if raw_path not in _naver_cache:
        items = json.loads(raw_path.read_text(encoding='utf-8'))
        _naver_cache[raw_path] = {it.get('link'): {**it, '_rank': i + 1} for i, it in enumerate(items)}
    return _naver_cache[raw_path]


def naver_rank(raw_path: Path, url: str) -> int:
    """검색 결과 내 순위 (정확도순, 1부터). 없으면 매우 큰 값"""
    return _naver_items(raw_path).get(url, {}).get('_rank', 10 ** 9)


def naver_sections(raw_path: Path, url: str) -> list[dict]:
    """검색 API 응답 JSON에서 해당 링크의 요약문 추출"""
    it = _naver_items(raw_path).get(url, {})
    desc = html.unescape(re.sub(r'<[^>]+>', '', it.get('description', '')))
    return [{'section': '', 'page': 0, 'text': desc}]


def parse_entry(entry: dict) -> list[dict]:
    """manifest 항목 → sections"""
    path = config.ROOT / entry['raw_path']
    suffix = path.suffix.lower()
    if entry['source'] in ('blog', 'cafe'):
        return naver_sections(path, entry['url'])
    if entry['source'] == 'law':
        return law_sections(path)
    if entry['source'] == 'assembly' and suffix == '.json':
        return assembly_sections(path)
    if suffix == '.pdf':
        return pdf_sections(path, entry.get('page_range', ''))
    if suffix == '.hwpx':
        return hwpx_sections(path)
    raise ValueError(f'지원하지 않는 원본 형식: {entry["raw_path"]}')
