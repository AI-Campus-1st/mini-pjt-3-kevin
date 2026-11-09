"""
평가 하네스 (masterplan §6-2, data_spec §6·§7, api_spec §3)
- 챗봇과 같은 rag.chain.build_chain(cfg)로 검색·생성 수행 (가이드 7장 "평가 하네스와 챗봇이 동일한 체인 호출")
- 문서 단위 지표: 체인의 검색 후보(fetch_k 청크)를 doc_id 기준으로 중복 제거한 순위로 Hit@1/3/5, MRR@10 산출
- 컨텍스트 지표: 체인이 생성 단계에 넣는 청크(Prepared.chunks) 안에 정답 문서가 있는지 (ctx_hit), 그 안의 고유 문서 수
- 정답 판정: gold_doc_ids 또는 그 dup_cluster_id와 일치, 또는 문서 제목+본문이 gold_patterns 정규식 전부와 일치
- retrieval-only 모드: LLM 호출 없음 (검색 실험용)
- full 모드: 문항당 generations회 생성, 거절 여부는 다수결, 다수결과 판정이 같은 첫 생성을 대표 답변으로 채점 시트에 기록
- 결과: eval/results/{YYYYMMDD-HHMM}_{tag}/ 에 config.json, per_question.jsonl, summary.json (+ full 모드 grade_sheet.csv)

사용 예:
    python -m eval.run_eval --tag baseline --retrieval-only --hybrid 0 --tier-bonus none   # E0 검색 설정
    python -m eval.run_eval --tag hybrid_tier_b04 --retrieval-only                         # config 기본값 (채택 설정)
    python -m eval.run_eval --tag prompt_v2 --prompt-version v2 --changed-variable "프롬프트 v2"
- 지정하지 않은 옵션은 config 기본값(챗봇과 동일)
"""
import argparse
import copy
import csv
import hashlib
import json
import re
import sqlite3
import threading
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

import config
from rag.chain import Prepared, RagChain, build_chain
from rag.retriever import dedup_docs

KST = timezone(timedelta(hours=9))
EVAL_DIR = Path(__file__).resolve().parent
RESULTS_DIR = EVAL_DIR / 'results'
MRR_CUTOFF = 10
KEEP_CHUNKS = 20      # per_question.jsonl에 남기는 청크 수
GEN_RETRIES = 3       # 생성 API 오류 시 재시도 횟수

GRADE_COLUMNS = ['id', 'type', 'q', 'answer_key', 'required_points', 'hit_rank', 'ctx_hit', 'refused_votes',
                 'refused_auto', 'points_auto', 'cited_n', 'cited_gold_n', 'invalid_citations', 'layer_hint',
                 'sources', 'answer', 'accuracy', 'citation_correct', 'refusal_correct', 'failure_layer', 'memo']


def load_set(path: Path) -> list[dict]:
    return [json.loads(l) for l in open(path, encoding='utf-8') if l.strip()]


def pct(values: list[float], q: float) -> float | None:
    return round(float(np.percentile(values, q)), 1) if values else None


def mean(values: list) -> float | None:
    return round(float(np.mean(values)), 4) if values else None


class GoldJudge:
    """문항별 정답 문서 판정 (data_spec §6 정답 판정 규칙)"""

    def __init__(self):
        self.con = sqlite3.connect(config.DOCS_DB_PATH, check_same_thread=False)
        self.cluster = dict(self.con.execute('SELECT doc_id, dup_cluster_id FROM documents'))
        self._text = {}
        self._lock = threading.Lock()   # full 모드 생성 스레드에서 공유

    def text(self, doc_id: str) -> str:
        with self._lock:
            if doc_id not in self._text:
                row = self.con.execute('SELECT title, body FROM documents WHERE doc_id = ?', (doc_id,)).fetchone()
                self._text[doc_id] = (row[0] + '\n' + row[1]) if row else ''
            return self._text[doc_id]

    def matcher(self, item: dict):
        golds = set(item.get('gold_doc_ids') or [])
        golds |= {self.cluster[g] for g in list(golds) if self.cluster.get(g)}
        regs = [re.compile(p) for p in item.get('gold_patterns') or []]

        def is_gold(doc_id: str, cluster_id: str = '') -> bool:
            cluster_id = cluster_id or self.cluster.get(doc_id) or ''
            if doc_id in golds or (cluster_id and cluster_id in golds):
                return True
            return bool(regs) and all(r.search(self.text(doc_id)) for r in regs)
        return is_gold


def retrieval_record(item: dict, prep: Prepared, judge: GoldJudge) -> dict:
    """체인 검색 결과(Prepared)로 검색 지표 산출"""
    cands, ctx = prep.candidates, prep.chunks
    docs = dedup_docs(cands)
    rec = {'id': item['id'], 'type': item['type'], 'source_need': item.get('source_need', ''),
           'query': prep.query, 'status': prep.status,
           'retrieved': [{'rank': c.rank, 'chunk_id': c.chunk_id, 'doc_id': c.doc_id, 'score': c.score, **c.extra}
                         for c in cands[:KEEP_CHUNKS]],
           'retrieved_docs': [{'rank': d.rank, 'doc_id': d.doc_id, 'doc_type': d.meta.get('doc_type', ''),
                               'score': d.score} for d in docs[:MRR_CUTOFF]],
           'top1_score': cands[0].score if cands else None,
           'ctx_doc_types': [c.meta.get('doc_type', '') for c in ctx],
           'ctx_n_docs': len({c.doc_id for c in ctx}),
           'latency_ms': {'retrieve': prep.latency_ms.get('retrieve')}}
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
        'ctx_hit': int(any(is_gold(c.doc_id, c.meta.get('dup_cluster_id', '')) for c in ctx)),
    })
    return rec


def generate_once(chain: RagChain, prep: Prepared, item: dict, judge: GoldJudge) -> dict:
    """1회 생성 (Prepared 사본 사용, 인용 표시·지연시간이 생성 간에 섞이지 않도록)"""
    for attempt in range(GEN_RETRIES):
        try:
            p = copy.deepcopy(prep)
            ans = chain.finalize(p, ''.join(chain.stream(p)))
            break
        except Exception as e:  # API 일시 오류 대응
            if attempt == GEN_RETRIES - 1:
                raise
            print(f'  [재시도] {item["id"]} {type(e).__name__}: {e}')
            time.sleep(5 * (attempt + 1))
    is_gold = judge.matcher(item) if item['type'] != 'unanswerable' else (lambda *_: False)
    points = item.get('required_points') or []
    return {'answer': ans.answer, 'citations': ans.citations, 'invalid_citations': ans.invalid_citations,
            'refused': ans.refused, 'partial_refusal': ans.partial_refusal,
            'cited_gold': [c['doc_id'] for c in ans.citations if is_gold(c['doc_id'])],
            'points_found': [pt for pt in points if re.search(pt, ans.answer)],
            'sources': [{k: s[k] for k in ('n', 'doc_id', 'doc_type_label', 'title', 'published_at', 'cited')}
                        for s in p.sources],
            'latency_ms': ans.latency_ms}


def merge_generations(rec: dict, gens: list[dict], item: dict) -> None:
    """거절 다수결, 대표 답변(다수결과 판정이 같은 첫 생성) 선정"""
    votes = sum(g['refused'] for g in gens)
    refused = votes * 2 > len(gens)
    rep = next(i for i, g in enumerate(gens) if g['refused'] == refused)
    r = gens[rep]
    rec.update({'answer': r['answer'], 'citations': r['citations'], 'invalid_citations': r['invalid_citations'],
                'refused': refused, 'refused_votes': votes, 'partial_refusal': r['partial_refusal'],
                'cited_gold': r['cited_gold'], 'points_found': r['points_found'],
                'n_points': len(item.get('required_points') or []), 'sources': r['sources'],
                'rep_gen': rep, 'n_generations': len(gens),
                'generations': [{k: g[k] for k in ('answer', 'citations', 'invalid_citations', 'refused',
                                                   'partial_refusal', 'cited_gold', 'points_found', 'latency_ms')}
                                for g in gens]})
    rec['latency_ms'] = {'retrieve': rec['latency_ms']['retrieve'],
                         'generate': r['latency_ms'].get('generate'), 'total': r['latency_ms'].get('total')}


def agg(rows: list[dict]) -> dict:
    ans = [r for r in rows if r['type'] != 'unanswerable']
    out = {'n': len(ans)}
    for k in ['hit@1', 'hit@3', 'hit@5', 'ctx_hit']:
        out[k] = mean([r[k] for r in ans])
    out['mrr'] = mean([r['rr'] for r in ans])
    return out


def agg_answers(rows: list[dict]) -> dict:
    """full 모드 자동 지표 (정확도·출처 정확성은 직접 채점 결과로 별도 집계)"""
    ans = [r for r in rows if r['type'] != 'unanswerable']
    una = [r for r in rows if r['type'] == 'unanswerable']
    answered = [r for r in ans if not r['refused']]
    out = {
        'refusal_acc': mean([int(r['refused']) for r in una]),
        'false_refusal_rate': mean([int(r['refused']) for r in ans]),
        'partial_refusal_rate': mean([int(r['partial_refusal']) for r in ans]),
        'refusal_unanimous': mean([int(r['refused_votes'] in (0, r['n_generations'])) for r in rows]),
        'cited_gold_rate': mean([int(bool(r['cited_gold'])) for r in answered]),
        'points_auto': mean([len(r['points_found']) / r['n_points'] for r in answered if r['n_points']]),
        'citations_mean': mean([len(r['citations']) for r in answered]),
        'invalid_citation_rate': mean([int(bool(r['invalid_citations'])) for r in rows]),
        'n_answerable': len(ans), 'n_unanswerable': len(una), 'n_answered': len(answered),
    }
    return out


def summarize(rows: list[dict], top_k: int, full: bool) -> dict:
    s = {'overall': agg(rows), 'by_type': {}, 'by_source_need': {}}
    for t in sorted({r['type'] for r in rows} - {'unanswerable'}):
        s['by_type'][t] = agg([r for r in rows if r['type'] == t])
    for n in sorted({r['source_need'] for r in rows if r['type'] != 'unanswerable'}):
        s['by_source_need'][n] = agg([r for r in rows if r['source_need'] == n and r['type'] != 'unanswerable'])
    ctx_types = Counter(t for r in rows for t in r['ctx_doc_types'])
    total = sum(ctx_types.values())
    s['ctx_doc_type_share'] = {k: round(v / total, 4) for k, v in ctx_types.most_common()} if total else {}
    s['ctx_n_docs_mean'] = mean([r['ctx_n_docs'] for r in rows])
    s['status'] = dict(Counter(r['status'] for r in rows))
    scores = defaultdict(list)
    for r in rows:
        if r['top1_score'] is None:
            continue
        key = 'unanswerable' if r['type'] == 'unanswerable' else ('hit' if r['hit@1'] else 'miss')
        scores[key].append(r['top1_score'])
    s['top1_score'] = {k: {'n': len(v), 'mean': round(float(np.mean(v)), 4), 'min': round(min(v), 4),
                           'max': round(max(v), 4)} for k, v in scores.items()}
    lat = [r['latency_ms']['retrieve'] for r in rows]
    s['latency_retrieve_p50'], s['latency_retrieve_p95'] = pct(lat, 50), pct(lat, 95)
    if full:
        s['answer'] = agg_answers(rows)
        s['answer_by_type'] = {t: agg_answers([r for r in rows if r['type'] == t])
                               for t in sorted({r['type'] for r in rows})}
        gen = [r['latency_ms']['generate'] for r in rows if r['latency_ms'].get('generate') is not None]
        tot = [r['latency_ms']['total'] for r in rows if r['latency_ms'].get('total') is not None]
        s['latency_generate_p50'], s['latency_generate_p95'] = pct(gen, 50), pct(gen, 95)
        s['latency_p50'], s['latency_p95'] = pct(tot, 50), pct(tot, 95)
    s['n_questions'] = len(rows)
    s['top_k'] = top_k
    return s


def layer_hint(r: dict) -> str:
    """실패 계층 분류 참고값 (자동 추정, 최종 판정은 채점자)"""
    if r['type'] == 'unanswerable':
        return 'generation 후보 (답변 불가 문항에 답변)' if not r['refused'] else ''
    if not r['ctx_hit']:
        return 'retrieval 후보 (정답 문서가 컨텍스트에 없음)'
    if r['refused']:
        return 'generation 후보 (정답 문서가 컨텍스트에 있으나 거절)'
    return ''


def write_grade_sheet(rows: list[dict], items: dict, path: Path) -> None:
    """직접 채점 시트 (data_spec §7-4, UTF-8 BOM 쉼표 구분)"""
    with open(path, 'w', encoding='utf-8-sig', newline='') as f:
        w = csv.DictWriter(f, fieldnames=GRADE_COLUMNS)
        w.writeheader()
        for r in rows:
            it = items[r['id']]
            src = '\n'.join(f'[{s["n"]}] {s["doc_type_label"]} | {s["title"]} | {s["published_at"]} | {s["doc_id"]}'
                            f'{" (인용)" if s["cited"] else ""}' for s in r['sources'])
            w.writerow({
                'id': r['id'], 'type': r['type'], 'q': it['q'], 'answer_key': it['answer_key'],
                'required_points': ' / '.join(it.get('required_points') or []),
                'hit_rank': r['hit_rank'] if r['hit_rank'] is not None else '',
                'ctx_hit': '' if r['ctx_hit'] is None else r['ctx_hit'],
                'refused_votes': f'{r["refused_votes"]}/{r["n_generations"]}',
                'refused_auto': 'Y' if r['refused'] else 'N',
                'points_auto': f'{len(r["points_found"])}/{r["n_points"]}' if r['n_points'] else '',
                'cited_n': len(r['citations']), 'cited_gold_n': len(r['cited_gold']),
                'invalid_citations': ','.join(map(str, r['invalid_citations'])),
                'layer_hint': layer_hint(r), 'sources': src, 'answer': r['answer'],
                'accuracy': '', 'citation_correct': '', 'refusal_correct': '', 'failure_layer': '', 'memo': ''})


def main():
    p = argparse.ArgumentParser(description='평가 하네스')
    p.add_argument('--tag', required=True)
    p.add_argument('--collection', default=config.DEFAULT_COLLECTION)
    p.add_argument('--changed-variable', default='')
    p.add_argument('--top-k', type=int, default=None)
    p.add_argument('--threshold', default=None, help='유사도 임계값 (none이면 미적용)')
    p.add_argument('--prompt-version', default=None)
    p.add_argument('--eval-set', default=str(EVAL_DIR / 'eval_set.jsonl'))
    p.add_argument('--retrieval-only', action='store_true')
    p.add_argument('--generations', type=int, default=config.EVAL_GENERATIONS)
    p.add_argument('--workers', type=int, default=4, help='생성 동시 호출 수')
    p.add_argument('--hybrid', type=int, choices=[0, 1], default=None, help='벡터 + BM25 RRF 결합 검색 (E5)')
    p.add_argument('--tier-bonus', default=None, help='신뢰등급별 가산점, 예: 1=0.04,2=0.04 (none이면 미적용)')
    p.add_argument('--primary-quota', type=int, default=None, help='상위 5개 문서 중 1차 자료 최소 수')
    p.add_argument('--doc-chunk-cap', type=int, default=None, help='컨텍스트 문서당 최대 청크 수')
    p.add_argument('--ids', default='', help='쉼표 구분 문항 id (일부 문항만 실행, 점검용)')
    a = p.parse_args()
    full = not a.retrieval_only

    items = load_set(Path(a.eval_set))
    if a.ids:
        keep = set(a.ids.split(','))
        items = [i for i in items if i['id'] in keep]
    # 지정한 옵션만 cfg로 덮어쓰고 나머지는 챗봇과 같은 config 기본값 사용
    over = {'collection': a.collection, 'fetch_k': config.FETCH_K}
    if a.top_k is not None:
        over['top_k'] = a.top_k
    if a.threshold is not None:
        over['score_threshold'] = None if a.threshold.lower() == 'none' else float(a.threshold)
    if a.prompt_version is not None:
        over['prompt_version'] = a.prompt_version
    if a.hybrid is not None:
        over['hybrid'] = bool(a.hybrid)
    if a.tier_bonus is not None:
        over['tier_bonus'] = (None if a.tier_bonus.lower() == 'none' else
                              {int(k): float(v) for k, v in (x.split('=') for x in a.tier_bonus.split(','))})
    if a.primary_quota is not None:
        over['primary_quota'] = a.primary_quota
    if a.doc_chunk_cap is not None:
        over['doc_chunk_cap'] = a.doc_chunk_cap
    chain = build_chain(over)
    c = chain.cfg
    judge = GoldJudge()

    t0 = time.perf_counter()
    chain.retriever.embed_queries([i['q'] for i in items])   # 일괄 임베딩 후 prepare에서 캐시 재사용
    emb_ms = (time.perf_counter() - t0) * 1000
    preps = [chain.prepare(i['q'], i.get('history'), i.get('filters')) for i in items]
    rows = [retrieval_record(i, pr, judge) for i, pr in zip(items, preps)]

    if full:
        tasks = [(qi, g) for qi in range(len(items)) for g in range(a.generations)]
        results: dict[tuple[int, int], dict] = {}
        t0 = time.perf_counter()

        def run(task):
            qi, g = task
            results[task] = generate_once(chain, preps[qi], items[qi], judge)
            done = len(results)
            if done % 10 == 0 or done == len(tasks):
                print(f'  생성 {done}/{len(tasks)} ({time.perf_counter() - t0:.0f}s)', flush=True)

        with ThreadPoolExecutor(max_workers=a.workers) as ex:
            list(ex.map(run, tasks))
        for qi, (item, rec) in enumerate(zip(items, rows)):
            merge_generations(rec, [results[(qi, g)] for g in range(a.generations)], item)

    summary = summarize(rows, c['top_k'], full)
    summary['query_embed_ms_per_q'] = round(emb_ms / len(items), 1)

    run_at = datetime.now(KST)
    out_dir = RESULTS_DIR / f'{run_at:%Y%m%d-%H%M}_{a.tag}'
    out_dir.mkdir(parents=True, exist_ok=True)
    info = chain.retriever.info
    cfg = {'tag': a.tag, 'changed_variable': a.changed_variable, 'collection': a.collection,
           'embedding_model': info.get('embedding_model'), 'chunk_config': info.get('chunk_config'),
           'title_prefix': info.get('title_prefix'), 'n_docs': info.get('n_docs'), 'n_chunks': info.get('n_chunks'),
           'top_k': c['top_k'], 'fetch_k': c['fetch_k'], 'mrr_cutoff': MRR_CUTOFF,
           'score_threshold': c['score_threshold'], 'hybrid': c['hybrid'], 'tier_bonus': c['tier_bonus'],
           'primary_quota': c['primary_quota'], 'doc_chunk_cap': c['doc_chunk_cap'],
           'prompt_version': c['prompt_version'] if full else None,
           'llm_model': chain.cfg['llm_model'] if full else None,
           'temperature': config.LLM_TEMPERATURE if full else None,
           'reasoning_effort': chain.cfg['reasoning_effort'] if full else None,
           'generations': a.generations if full else None,
           'eval_set': Path(a.eval_set).name, 'ids': a.ids or None,
           'eval_set_sha1': hashlib.sha1(Path(a.eval_set).read_bytes()).hexdigest(),
           'mode': 'full' if full else 'retrieval_only', 'run_at': run_at.isoformat(timespec='seconds')}
    (out_dir / 'config.json').write_text(json.dumps(cfg, ensure_ascii=False, indent=1), encoding='utf-8')
    with open(out_dir / 'per_question.jsonl', 'w', encoding='utf-8') as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + '\n')
    (out_dir / 'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding='utf-8')
    if full:
        write_grade_sheet(rows, {i['id']: i for i in items}, out_dir / 'grade_sheet.csv')

    o = summary['overall']
    if o['n']:
        print(f'[{a.tag}] {a.collection} hybrid={c["hybrid"]} tier_bonus={c["tier_bonus"]} | Hit@1 {o["hit@1"]:.3f} Hit@3 {o["hit@3"]:.3f} Hit@5 {o["hit@5"]:.3f} '
              f'MRR {o["mrr"]:.3f} ctx_hit@{c["top_k"]} {o["ctx_hit"]:.3f} (n={o["n"]})')
    for t, v in summary['by_type'].items():
        print(f'  {t:<10} n={v["n"]:<3} Hit@3 {v["hit@3"]:.3f} MRR {v["mrr"]:.3f}')
    print(f'  컨텍스트 문서 종류 비중: {summary["ctx_doc_type_share"]}')
    if full:
        print(f'  답변 자동 지표: {summary["answer"]}')
        print(f'  지연시간 p50/p95: 생성 {summary["latency_generate_p50"]}/{summary["latency_generate_p95"]} ms, '
              f'전체 {summary["latency_p50"]}/{summary["latency_p95"]} ms')
    print(f'  저장: {out_dir.relative_to(config.ROOT)}')


if __name__ == '__main__':
    main()
