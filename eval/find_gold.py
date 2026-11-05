"""
평가셋 정답 문서 탐색 도구 (원문 DB 정규식 검색)
- 평가 문항 작성 시 정답 근거 문서(gold_doc_ids)를 찾고, 답변 불가 문항의 근거 부재를 확인하는 용도
- 벡터 검색을 쓰지 않으므로 검색기 성능과 독립적인 정답 판정 가능

사용 예:
    python -m eval.find_gold "부정유통" "과태료" --types law veto
    python -m eval.find_gold "해외|일본|독일" "지역통화" --all
"""
import argparse
import re
import sqlite3

import config


def search(patterns: list[str], types: list[str] | None = None, include_excluded: bool = False, limit: int = 20):
    con = sqlite3.connect(config.DOCS_DB_PATH)
    sql = 'SELECT doc_id, doc_type, source_tier, title, publisher, published_at, body, included FROM documents'
    cond, params = [], []
    if not include_excluded:
        cond.append('included = 1')
    if types:
        cond.append(f'doc_type IN ({",".join("?" * len(types))})')
        params += types
    if cond:
        sql += ' WHERE ' + ' AND '.join(cond)
    regs = [re.compile(p) for p in patterns]
    hits = []
    for row in con.execute(sql, params):
        text = row[3] + '\n' + row[6]
        if all(r.search(text) for r in regs):
            m = regs[-1].search(text)
            s = max(0, m.start() - 80)
            hits.append({'doc_id': row[0], 'doc_type': row[1], 'tier': row[2], 'title': row[3], 'publisher': row[4],
                         'published_at': row[5], 'included': row[7],
                         'snippet': re.sub(r'\s+', ' ', text[s:m.end() + 120])})
    con.close()
    hits.sort(key=lambda h: (h['tier'], h['published_at']))
    return hits[:limit] if limit else hits, len(hits)


def main():
    p = argparse.ArgumentParser()
    p.add_argument('patterns', nargs='+', help='모두 일치해야 하는 정규식들 (제목+본문 대상)')
    p.add_argument('--types', nargs='*')
    p.add_argument('--all', action='store_true', help='지식베이스 제외 문서까지 검색')
    p.add_argument('--limit', type=int, default=20)
    a = p.parse_args()
    hits, total = search(a.patterns, a.types, a.all, a.limit)
    print(f'일치 문서 {total}건 (표시 {len(hits)}건)')
    for h in hits:
        print(f'- {h["doc_id"]} [{h["doc_type"]}/T{h["tier"]}] {h["published_at"]} {h["publisher"]} | {h["title"][:60]}')
        print(f'    ...{h["snippet"]}...')


if __name__ == '__main__':
    main()
