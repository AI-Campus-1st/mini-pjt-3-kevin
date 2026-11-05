"""
평가셋 검증
- gold_doc_ids가 원문 DB에 존재하고 지식베이스에 포함되었는지 확인 (제외 문서는 클러스터 대표로 대체 가능한지 표시)
- required_points(정규식)가 gold 문서 본문 중 하나 이상에 등장하는지 확인 → answer_key의 근거 검증
- gold_patterns 문항은 일치 문서 수 확인
- 답변 불가 문항은 required_points 대신 note 확인용으로 표시만 함

사용 예:
    python -m eval.validate_set eval/eval_set_draft.jsonl
"""
import json
import re
import sqlite3
import sys

import config


def main(path: str):
    con = sqlite3.connect(config.DOCS_DB_PATH)
    docs = {r[0]: r for r in con.execute('SELECT doc_id, included, dup_cluster_id, title, body FROM documents')}
    included_texts = None
    problems, n = 0, 0
    for line in open(path, encoding='utf-8'):
        if not line.strip():
            continue
        item = json.loads(line)
        n += 1
        msgs = []
        golds = item.get('gold_doc_ids', [])
        texts = []
        for g in golds:
            if g not in docs:
                msgs.append(f'gold 없음: {g}')
                continue
            _, inc, cluster, title, body = docs[g]
            if not inc:
                rep = cluster if cluster != g else ''
                msgs.append(f'gold 제외 문서: {g} (대표 {rep or "없음"})')
            texts.append(title + '\n' + body)
        pats = item.get('gold_patterns', [])
        if pats:
            if included_texts is None:
                included_texts = [r[3] + '\n' + r[4] for r in docs.values() if r[1]]
            regs = [re.compile(p) for p in pats]
            hits = sum(1 for t in included_texts if all(r.search(t) for r in regs))
            if hits == 0:
                msgs.append('gold_patterns 일치 문서 0건')
            pat_note = f' | patterns 일치 {hits}건'
        else:
            pat_note = ''
        if item['type'] != 'unanswerable':
            if not golds and not pats:
                msgs.append('정답 근거(gold_doc_ids/gold_patterns) 없음')
            for p in item.get('required_points', []):
                if texts and not any(re.search(p, t) for t in texts):
                    msgs.append(f'required_point 미발견: {p}')
        status = 'OK ' if not msgs else 'CHK'
        problems += bool(msgs)
        print(f'{status} {item["id"]} [{item["type"]}] gold {len(golds)}건{pat_note} | {item["q"][:50]}')
        for m in msgs:
            print(f'      - {m}')
    print(f'\n총 {n}문항, 확인 필요 {problems}문항')


if __name__ == '__main__':
    main(sys.argv[1] if len(sys.argv) > 1 else 'eval/eval_set.jsonl')
