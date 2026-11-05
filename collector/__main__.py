"""
수집 계층 실행 진입점

사용 예:
    python -m collector all
    python -m collector provided korea_kr
"""
import argparse
import json
import time

from collector import collect_assembly, collect_law, collect_naver, crawl_korea_kr, load_provided, register_manual

TASKS = {
    'provided': load_provided.run,
    'manual': register_manual.run,
    'law': collect_law.run,
    'assembly': collect_assembly.run,
    'naver': collect_naver.run,
    'korea_kr': crawl_korea_kr.run,
}


def main():
    parser = argparse.ArgumentParser(description='수집 계층 배치 실행')
    parser.add_argument('tasks', nargs='+', choices=['all', *TASKS])
    args = parser.parse_args()
    names = list(TASKS) if 'all' in args.tasks else args.tasks
    for name in names:
        t0 = time.time()
        print(f'[collector] {name} 시작', flush=True)
        result = TASKS[name]()
        print(f'[collector] {name} 완료 ({time.time() - t0:.1f}s): {json.dumps(result, ensure_ascii=False)}', flush=True)


if __name__ == '__main__':
    main()
