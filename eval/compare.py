"""
실험 결과 비교표 (태그별 최신 실행 기준)

사용 예:
    python -m eval.compare baseline chunk_B no_title
"""
import json
import sys
from pathlib import Path

RESULTS_DIR = Path(__file__).resolve().parent / 'results'


def latest(tag: str, mode: str | None = None) -> Path | None:
    """태그가 정확히 일치하는 최신 실행 (폴더명 {YYYYMMDD-HHMM}_{tag}, mode 지정 시 config.json의 mode까지 일치)"""
    runs = []
    for p in RESULTS_DIR.glob(f'*_{tag}'):
        if p.name.split('_', 1)[1] != tag or not (p / 'summary.json').exists():
            continue
        if mode and json.loads((p / 'config.json').read_text(encoding='utf-8')).get('mode') != mode:
            continue
        runs.append(p)
    return sorted(runs)[-1] if runs else None


def main(tags: list[str]):
    rows = []
    for t in tags:
        d = latest(t, 'retrieval_only') or latest(t)
        if not d:
            print(f'결과 없음: {t}')
            continue
        cfg = json.loads((d / 'config.json').read_text(encoding='utf-8'))
        s = json.loads((d / 'summary.json').read_text(encoding='utf-8'))
        rows.append((t, cfg, s))
    if not rows:
        return
    print('| 태그 | 컬렉션 | Hit@1 | Hit@3 | Hit@5 | MRR@10 | ctx_hit@k | 컨텍스트 고유 문서 | 실행 |')
    print('|---|---|---:|---:|---:|---:|---:|---:|---|')
    for t, cfg, s in rows:
        o = s['overall']
        print(f'| {t} | {cfg["collection"]} | {o["hit@1"]:.3f} | {o["hit@3"]:.3f} | {o["hit@5"]:.3f} | {o["mrr"]:.3f} '
              f'| {o["ctx_hit"]:.3f} | {s["ctx_n_docs_mean"]:.2f} | {cfg["run_at"][:16]} |')
    types = list(rows[0][2]['by_type'])
    print('\n유형별 Hit@3')
    print('| 태그 | ' + ' | '.join(types) + ' |')
    print('|---|' + '---:|' * len(types))
    for t, _, s in rows:
        print(f'| {t} | ' + ' | '.join(f'{s["by_type"][x]["hit@3"]:.3f}' for x in types) + ' |')
    needs = list(rows[0][2]['by_source_need'])
    print('\n근거 문서 종류(source_need)별 Hit@3')
    print('| 태그 | ' + ' | '.join(f'{x} (n={rows[0][2]["by_source_need"][x]["n"]})' for x in needs) + ' |')
    print('|---|' + '---:|' * len(needs))
    for t, _, s in rows:
        print(f'| {t} | ' + ' | '.join(f'{s["by_source_need"][x]["hit@3"]:.3f}' for x in needs) + ' |')


if __name__ == '__main__':
    main(sys.argv[1:] or ['baseline', 'chunk_B', 'no_title'])
