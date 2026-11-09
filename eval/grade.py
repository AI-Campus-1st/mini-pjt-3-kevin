"""
직접 채점 결과 집계 (data_spec §7-4)
- 입력: 실험 결과 폴더의 grade_sheet.csv (UTF-8 쉼표 원본, Excel 저장본(CP949 탭 구분) 모두 열 이름으로 읽음)
- 출력: 같은 폴더의 graded_summary.json + 콘솔 요약
- 정확도: accuracy 1 / 0.5 / 0 (정확 / 부분 / 오답), 답변 가능 문항 대상, 목표 "정확+부분 비율 ≥ 80%"
- 출처 정확성: citation_correct Y / partial / N, 인용이 있는 답변 대상, 목표 Y 비율 ≥ 90%
- 거절: refusal_correct Y / N / NA (거절 판단이 맞았는지), 답변 불가 문항 Y 비율 = 거절 정확도, 답변 가능 문항 N 비율 = 오거절률
- 실패 계층: failure_layer (data / preprocess / indexing / retrieval / generation) 문항 유형별 집계

사용 예:
    python -m eval.grade eval/results/20261107-1510_baseline
"""
import argparse
import csv
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

ACC_MAP = {'1': 1.0, '1.0': 1.0, '정확': 1.0, '0.5': 0.5, '.5': 0.5, '부분': 0.5, '0': 0.0, '0.0': 0.0, '오답': 0.0}
LAYERS = ['data', 'preprocess', 'indexing', 'retrieval', 'generation']


def read_sheet(path: Path) -> list[dict]:
    """UTF-8(BOM) 쉼표 구분 또는 Excel 저장본(CP949 탭 구분) 읽기"""
    for enc, delim in [('utf-8-sig', ','), ('utf-8-sig', '\t'), ('cp949', '\t'), ('cp949', ',')]:
        try:
            with open(path, encoding=enc, newline='') as f:
                rows = list(csv.DictReader(f, delimiter=delim))
        except UnicodeDecodeError:
            continue
        if rows and 'accuracy' in rows[0] and 'id' in rows[0]:
            return [{k.strip(): (v or '').strip() for k, v in r.items() if k} for r in rows]
    raise SystemExit(f'채점 시트를 읽지 못함: {path}')


def rate(values: list[float]) -> float | None:
    return round(sum(values) / len(values), 4) if values else None


def summarize(rows: list[dict]) -> dict:
    ans = [r for r in rows if r['type'] != 'unanswerable']
    una = [r for r in rows if r['type'] == 'unanswerable']
    acc = [ACC_MAP[r['accuracy']] for r in ans if r['accuracy'] in ACC_MAP]
    cite = [r['citation_correct'].lower() for r in rows if r['citation_correct'].lower() in ('y', 'n', 'partial')]
    una_ref = [r['refusal_correct'].upper() for r in una if r['refusal_correct'].upper() in ('Y', 'N')]
    ans_ref = [r['refusal_correct'].upper() for r in ans if r['refusal_correct'].upper() in ('Y', 'N')]
    return {
        'n_answerable': len(ans), 'n_graded': len(acc),
        'accuracy_mean': rate(acc),
        'correct_or_partial': rate([float(a >= 0.5) for a in acc]),
        'accuracy_counts': {k: sum(1 for a in acc if a == v) for k, v in [('정확', 1.0), ('부분', 0.5), ('오답', 0.0)]},
        'citation_y': rate([float(c == 'y') for c in cite]),
        'citation_y_or_partial': rate([float(c != 'n') for c in cite]),
        'citation_counts': dict(Counter(cite)),
        'refusal_acc': rate([float(c == 'Y') for c in una_ref]),
        'false_refusal_rate': rate([float(c == 'N') for c in ans_ref]),
        'n_unanswerable_graded': len(una_ref), 'n_answerable_refusal_graded': len(ans_ref),
    }


def main():
    p = argparse.ArgumentParser(description='직접 채점 결과 집계')
    p.add_argument('result_dir')
    a = p.parse_args()
    out_dir = Path(a.result_dir)
    rows = read_sheet(out_dir / 'grade_sheet.csv')
    bad = [r['id'] for r in rows if r['accuracy'] and r['accuracy'] not in ACC_MAP]
    bad_layer = [r['id'] for r in rows if r['failure_layer'] and r['failure_layer'].lower() not in LAYERS]

    s = {'overall': summarize(rows), 'by_type': {}}
    for t in sorted({r['type'] for r in rows}):
        s['by_type'][t] = summarize([r for r in rows if r['type'] == t])
    layers = defaultdict(Counter)
    for r in rows:
        if r['failure_layer'].lower() in LAYERS:
            layers[r['type']][r['failure_layer'].lower()] += 1
    s['failure_layer'] = {'total': dict(sum(layers.values(), Counter())),
                          'by_type': {t: dict(c) for t, c in sorted(layers.items())}}
    s['invalid_values'] = {'accuracy': bad, 'failure_layer': bad_layer}
    (out_dir / 'graded_summary.json').write_text(json.dumps(s, ensure_ascii=False, indent=1), encoding='utf-8')

    sys.stdout.reconfigure(encoding='utf-8')
    o = s['overall']
    print(f'채점 {o["n_graded"]}/{o["n_answerable"]} | 정확도 평균 {o["accuracy_mean"]} | 정확+부분 {o["correct_or_partial"]} '
          f'{o["accuracy_counts"]}')
    print(f'출처 정확성 Y {o["citation_y"]} (Y+partial {o["citation_y_or_partial"]}) {o["citation_counts"]}')
    print(f'거절 정확도 {o["refusal_acc"]} | 오거절률 {o["false_refusal_rate"]}')
    for t, v in s['by_type'].items():
        print(f'  {t:<12} 정확+부분 {v["correct_or_partial"]} 평균 {v["accuracy_mean"]} 출처 Y {v["citation_y"]}')
    print(f'실패 계층: {s["failure_layer"]["total"]}')
    if bad or bad_layer:
        print(f'허용되지 않은 값: {s["invalid_values"]}')
    print(f'저장: {out_dir / "graded_summary.json"}')


if __name__ == '__main__':
    main()
