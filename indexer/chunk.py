"""
청킹 (masterplan §5-4)
- 설정 A: 모든 문서 500자 / overlap 100 고정 분할
- 설정 B: 문서 종류별 구조 분할
  - 뉴스: 1,200자 이하는 문서 단위, 초과 시 800/150
  - 법령: 조문 단위 (짧은 조문은 800자 이내로 병합)
  - 보도자료·보고서류: 헤딩·단락 기호 경계로 블록 분할 후 800자 이내 병합, 초과 블록은 800/150
  - 블로그·카페: 요약문 단위
- 임베딩 입력 텍스트 = 제목 prefix + 청크 본문 (data_spec §5-2), 제목 prefix 제거 실험은 title_prefix=False
- 결과는 docs.db chunks 테이블에 컬렉션명 기준으로 저장 (임베딩은 embed 단계)
"""
import bisect
import re
import sqlite3

from langchain_text_splitters import RecursiveCharacterTextSplitter

import config

SEPARATORS = ['\n\n', '\n', '. ', '다. ', ' ', '']
HEADING = re.compile(
    r'^\s*(?:[ⅠⅡⅢⅣⅤⅥⅦⅧⅨⅩ]+\s*\.|\d{1,2}\s*\.\s|\d{1,2}-\d{1,2}\.\s|[가-하]\.\s|□|■|【|\[\s*[가-힣 ]{2,20}\s*\])')
STRUCTURED_TYPES = {'press_release', 'report', 'review_report', 'minutes', 'veto', 'budget', 'bill', 'local_notice'}


def prefix(doc: dict) -> str:
    label = config.DOC_TYPE_LABEL.get(doc['doc_type'], doc['doc_type'])
    return f'[{label}] {doc["title"]} ({doc["publisher"]}, {doc["published_at"]})\n'


def splitter(size: int, overlap: int) -> RecursiveCharacterTextSplitter:
    return RecursiveCharacterTextSplitter(chunk_size=size, chunk_overlap=overlap, separators=SEPARATORS,
                                          keep_separator=True, add_start_index=True)


def join_sections(sections: list[dict]) -> tuple[str, list[int]]:
    """구획을 이어 붙인 본문과 각 구획 시작 오프셋"""
    parts, starts, pos = [], [], 0
    for s in sections:
        starts.append(pos)
        parts.append(s['text'])
        pos += len(s['text']) + 2
    return '\n\n'.join(parts), starts


def locate(starts: list[int], sections: list[dict], offset: int) -> tuple[str, int]:
    i = max(0, bisect.bisect_right(starts, offset) - 1)
    return sections[i]['section'], sections[i]['page']


def split_fixed(body: str, size: int, overlap: int) -> list[tuple[str, int]]:
    docs = splitter(size, overlap).create_documents([body])
    return [(d.page_content, d.metadata['start_index']) for d in docs]


def merge_blocks(blocks: list[tuple[str, int]], limit: int) -> list[tuple[str, int]]:
    """(텍스트, 시작 오프셋) 블록을 limit 이내로 순차 병합, 초과 블록은 재분할"""
    out, buf, buf_start = [], '', 0
    for text, start in blocks:
        if len(text) > limit:
            if buf:
                out.append((buf, buf_start))
                buf = ''
            out += [(t, start + s) for t, s in split_fixed(text, limit, 150)]
            continue
        if buf and len(buf) + 1 + len(text) > limit:
            out.append((buf, buf_start))
            buf = ''
        if not buf:
            buf, buf_start = text, start
        else:
            buf = buf + '\n' + text
    if buf:
        out.append((buf, buf_start))
    return out


def heading_blocks(body: str) -> list[tuple[str, int]]:
    blocks, cur, cur_start, pos = [], [], 0, 0
    for line in body.split('\n'):
        if HEADING.match(line) and cur:
            blocks.append(('\n'.join(cur), cur_start))
            cur, cur_start = [], pos
        cur.append(line)
        pos += len(line) + 1
    if cur:
        blocks.append(('\n'.join(cur), cur_start))
    return [(t.strip(), s) for t, s in blocks if t.strip()]


def chunk_doc(doc: dict, sections: list[dict], preset: str) -> list[dict]:
    body, starts = join_sections(sections)
    cfg = config.CHUNK_PRESETS[preset]
    t = doc['doc_type']
    if cfg['mode'] == 'fixed':
        pieces = split_fixed(body, cfg['size'], cfg['overlap']) if len(body) > cfg['size'] else [(body, 0)]
    elif t == 'news':
        pieces = [(body, 0)] if len(body) <= cfg['news_whole_max'] else split_fixed(body, cfg['size'], cfg['overlap'])
    elif t in ('blog', 'cafe'):
        pieces = [(body, 0)]
    elif t == 'law':
        pieces = merge_blocks([(s['text'], st) for s, st in zip(sections, starts)], cfg['size'])
    elif t in STRUCTURED_TYPES:
        pieces = merge_blocks(heading_blocks(body), cfg['size'])
    else:
        pieces = split_fixed(body, cfg['size'], cfg['overlap'])
    out = []
    for text, start in pieces:
        text = text.strip()
        if not text:
            continue
        section, page = locate(starts, sections, start)
        out.append({'text': text, 'section': section, 'page': page})
    return out


def run(preset: str, title_prefix: bool = True) -> dict:
    coll = config.collection_name(preset, title_prefix)
    con = sqlite3.connect(config.DOCS_DB_PATH)
    con.row_factory = sqlite3.Row
    docs = con.execute('SELECT doc_id, doc_type, title, publisher, published_at FROM documents WHERE included = 1').fetchall()
    secs = {}
    for r in con.execute('SELECT doc_id, idx, section, page, text FROM sections ORDER BY doc_id, idx'):
        secs.setdefault(r['doc_id'], []).append({'section': r['section'], 'page': r['page'], 'text': r['text']})
    con.execute('DELETE FROM chunks WHERE collection = ?', (coll,))
    rows = []
    for d in docs:
        d = dict(d)
        pieces = chunk_doc(d, secs.get(d['doc_id'], []), preset)
        head = prefix(d) if title_prefix else ''
        for i, p in enumerate(pieces):
            text = head + p['text']
            rows.append((f'{d["doc_id"]}#c{i:03d}', coll, d['doc_id'], i, len(pieces), p['section'], p['page'],
                         text, len(text)))
    con.executemany('INSERT INTO chunks VALUES (?,?,?,?,?,?,?,?,?)', rows)
    con.commit()
    con.close()
    return {'collection': coll, 'docs': len(docs), 'chunks': len(rows)}
