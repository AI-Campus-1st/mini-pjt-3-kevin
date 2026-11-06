"""
평가 하네스 (masterplan §6-2, data_spec §6·§7)
- retrieval-only 모드: LLM 호출 없이 검색 지표만 산출 (청킹·임베딩 실험용)
- 문서 단위 지표: 검색 청크를 doc_id 기준으로 중복 제거한 순위로 Hit@1/3/5, MRR@10 산출
- 컨텍스트 지표: 실제 생성 단계에 들어가는 상위 top_k 청크 안에 정답 문서가 있는지 (ctx_hit), 그 안의 고유 문서 수
- 정답 판정: gold_doc_ids 또는 그 dup_cluster_id와 일치, 또는 문서 제목+본문이 gold_patterns 정규식 전부와 일치
- 결과: eval/results/{YYYYMMDD-HHMM}_{tag}/ 에 config.json, per_question.jsonl, summary.json

사용 예:
    python -m eval.run_eval --tag baseline --collection kb_A_te3s --retrieval-only
    python -m eval.run_eval --tag chunk_B --collection kb_B_te3s --retrieval-only --changed-variable "청킹 B"
"""
import argparse
import hashlib
import json
import re
import sqlite3
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

import config
from rag.retriever import Retriever, dedup_docs

KST = timezone(timedelta(hours=9))
EVAL_DIR = Path(__file__).resolve().parent
RESULTS_DIR = EVAL_DIR / 'results'
FETCH_K = 50          # 문서 단위 순위 산정을 위해 가져오는 청크 수
MRR_CUTOFF = 10
KEEP_CHUNKS = 20      # per_question.jsonl에 남기는 청크 수


def load_set(path: Path) -> list[dict]:
    return [json.loads(l) for l in open(path, encoding='utf-8') if l.strip()]


class GoldJudge:
    """문항별 정답 문서 판정 (data_spec §6 정답 판정 규칙)"""

    def __init__(self):
        self.con = sqlite3.connect(config.DOCS_DB_PATH)
        self.cluster = dict(self.con.execute('SELECT doc_id, dup_cluster_id FROM documents'))
        self._text = {}

    def text(self, doc_id: str) -> str:
        if doc_id not in self._text:
            row = self.con.execute('SELECT title, body FROM documents WHERE doc_id = ?', (doc_id,)).fetchone()
            self._text[doc_id] = (row[0] + '\n' + row[1]) if row else ''
        return self._text[doc_id]

    def matcher(self, item: dict):
        golds = set(item.get('gold_doc_ids') or [])
        golds |= {self.cluster[g] for g in list(golds) if self.cluster.get(g)}
        regs = [re.compile(p) for p in item.get('gold_patterns') or []]

        def is_gold(doc_id: str, cluster_id: str = '') -> bool:
            if doc_id in golds or (cluster_id and cluster_id in golds):
                return True
            return bool(regs) and all(r.search(self.text(doc_id)) for r in regs)
        return is_gold


def eval_question(item: dict, retriever: Retriever, judge: GoldJudge, emb: list[float], top_k: int) -> dict:
    t0 = time.perf_counter()
    chunks = retriever.search(item['q'], k=FETCH_K, filters=item.get('filters'), query_embedding=emb)
    ms = round((time.perf_counter() - t0) * 1000, 1)
    docs = dedup_docs(chunks)
    rec = {'id': item['id'], 'type': item['type'], 'source_need': item.get('source_need', ''),
           'retrieved': [{'rank': c.rank, 'chunk_id': c.chunk_id, 'doc_id': c.doc_id, 'score': c.score}
                         for c in chunks[:KEEP_CHUNKS]],
           'retrieved_docs': [{'rank': d.rank, 'doc_id': d.doc_id, 'doc_type': d.meta.get('doc_type', ''),
                               'score': d.score} for d in docs[:MRR_CUTOFF]],
           'top1_score': chunks[0].score if chunks else None,
           'ctx_doc_types': [c.meta.get('doc_type', '') for c in chunks[:top_k]],
           'ctx_n_docs': len({c.doc_id for c in chunks[:top_k]}),
           'latency_ms': {'retrieve': ms}}
    if item['type'] == 'unanswerable':
        rec.update({'hit_rank': None, 'hit@1': None, 'hit@3': None, 'hit@5': None, 'rr': None, 'ctx_hit': None})
        return rec
    is_gold = judge.matcher(item)
    hit_rank = next((d.rank for d in docs if is_gold(d.doc_id, d.meta.get('dup_cluster_id', ''))), None)
    rec.update({
        'hit_rank': hit_rank,
        'hit@1': int(hit_rank is not None and hit_rank <= 1),
        'hit@3': int(hit_rank is not None and hit_rank <= 3),
        'hit@5': int(hit_rank is not None and hit_rank <= 5),
        'rr': round(1 / hit_rank, 4) if hit_rank and hit_rank <= MRR_CUTOFF else 0.0,
        'ctx_hit': int(any(is_gold(c.doc_id, c.meta.get('dup_cluster_id', '')) for c in chunks[:top_k])),
    })
    return rec


def agg(rows: list[dict]) -> dict:
    ans = [r for r in rows if r['type'] != 'unanswerable']
    out = {'n': len(ans)}
    for k in ['hit@1', 'hit@3', 'hit@5', 'ctx_hit']:
        out[k] = round(float(np.mean([r[k] for r in ans])), 4) if ans else None
    out['mrr'] = round(float(np.mean([r['rr'] for r in ans])), 4) if ans else None
    return out


def summarize(rows: list[dict], top_k: int) -> dict:
    s = {'overall': agg(rows), 'by_type': {}, 'by_source_need': {}}
    for t in sorted({r['type'] for r in rows} - {'unanswerable'}):
        s['by_type'][t] = agg([r for r in rows if r['type'] == t])
    for n in sorted({r['source_need'] for r in rows if r['type'] != 'unanswerable'}):
        s['by_source_need'][n] = agg([r for r in rows if r['source_need'] == n and r['type'] != 'unanswerable'])
    ctx_types = Counter(t for r in rows for t in r['ctx_doc_types'])
    total = sum(ctx_types.values())
    s['ctx_doc_type_share'] = {k: round(v / total, 4) for k, v in ctx_types.most_common()}
    s['ctx_n_docs_mean'] = round(float(np.mean([r['ctx_n_docs'] for r in rows])), 2)
    scores = defaultdict(list)
    for r in rows:
        key = 'unanswerable' if r['type'] == 'unanswerable' else ('hit' if r['hit@1'] else 'miss')
        scores[key].append(r['top1_score'])
    s['top1_score'] = {k: {'n': len(v), 'mean': round(float(np.mean(v)), 4), 'min': round(min(v), 4),
                           'max': round(max(v), 4)} for k, v in scores.items()}
    lat = [r['latency_ms']['retrieve'] for r in rows]
    s['latency_retrieve_p50'] = round(float(np.percentile(lat, 50)), 1)
    s['latency_retrieve_p95'] = round(float(np.percentile(lat, 95)), 1)
    s['n_questions'] = len(rows)
    s['top_k'] = top_k
    return s


def main():
    p = argparse.ArgumentParser(description='평가 하네스')
    p.add_argument('--tag', required=True)
    p.add_argument('--collection', required=True)
    p.add_argument('--changed-variable', default='')
    p.add_argument('--top-k', type=int, default=config.TOP_K)
    p.add_argument('--eval-set', default=str(EVAL_DIR / 'eval_set.jsonl'))
    p.add_argument('--retrieval-only', action='store_true')
    a = p.parse_args()
    if not a.retrieval_only:
        raise SystemExit('full 모드(생성 포함)는 rag/chain.py 구현(Day 4) 이후 지원')

    items = load_set(Path(a.eval_set))
    retriever = Retriever(a.collection, top_k=a.top_k)
    judge = GoldJudge()
    t0 = time.perf_counter()
    embs = retriever.embed_queries([i['q'] for i in items])
    emb_ms = (time.perf_counter() - t0) * 1000
    rows = [eval_question(i, retriever, judge, e, a.top_k) for i, e in zip(items, embs)]
    summary = summarize(rows, a.top_k)
    summary['query_embed_ms_per_q'] = round(emb_ms / len(items), 1)

    run_at = datetime.now(KST)
    out_dir = RESULTS_DIR / f'{run_at:%Y%m%d-%H%M}_{a.tag}'
    out_dir.mkdir(parents=True, exist_ok=True)
    info = retriever.info
    cfg = {'tag': a.tag, 'changed_variable': a.changed_variable, 'collection': a.collection,
           'embedding_model': info.get('embedding_model'), 'chunk_config': info.get('chunk_config'),
           'title_prefix': info.get('title_prefix'), 'top_k': a.top_k, 'fetch_k': FETCH_K, 'mrr_cutoff': MRR_CUTOFF,
           'score_threshold': None, 'prompt_version': None, 'llm_model': None, 'temperature': None,
           'eval_set': Path(a.eval_set).name,
           'eval_set_sha1': hashlib.sha1(Path(a.eval_set).read_bytes()).hexdigest(),
           'mode': 'retrieval_only', 'run_at': run_at.isoformat(timespec='seconds')}
    (out_dir / 'config.json').write_text(json.dumps(cfg, ensure_ascii=False, indent=1), encoding='utf-8')
    with open(out_dir / 'per_question.jsonl', 'w', encoding='utf-8') as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + '\n')
    (out_dir / 'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding='utf-8')

    o = summary['overall']
    print(f'[{a.tag}] {a.collection} | Hit@1 {o["hit@1"]:.3f} Hit@3 {o["hit@3"]:.3f} Hit@5 {o["hit@5"]:.3f} '
          f'MRR {o["mrr"]:.3f} ctx_hit@{a.top_k} {o["ctx_hit"]:.3f} (n={o["n"]})')
    for t, v in summary['by_type'].items():
        print(f'  {t:<10} n={v["n"]:<3} Hit@3 {v["hit@3"]:.3f} MRR {v["mrr"]:.3f}')
    print(f'  컨텍스트 문서 종류 비중: {summary["ctx_doc_type_share"]}')
    print(f'  저장: {out_dir.relative_to(config.ROOT)}')


if __name__ == '__main__':
    main()
