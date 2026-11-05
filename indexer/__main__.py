"""
인덱싱 계층 실행 진입점

사용 예:
    python -m indexer docs                      # 원문 DB 구축 (파싱·정제·관련도·중복)
    python -m indexer chunk --preset A          # 청킹 설정 A (제목 prefix 포함)
    python -m indexer chunk --preset A --no-title
    python -m indexer verify
"""
import argparse
import json
import time

from indexer import build_docs, chunk, verify


def main():
    p = argparse.ArgumentParser(description='인덱싱 계층 배치 실행')
    sub = p.add_subparsers(dest='cmd', required=True)
    sub.add_parser('docs')
    c = sub.add_parser('chunk')
    c.add_argument('--preset', required=True, choices=['A', 'B'])
    c.add_argument('--no-title', action='store_true')
    v = sub.add_parser('verify')
    v.add_argument('--collection')
    args = p.parse_args()

    t0 = time.time()
    if args.cmd == 'docs':
        result = build_docs.run()
    elif args.cmd == 'chunk':
        result = chunk.run(args.preset, title_prefix=not args.no_title)
    else:
        result = verify.run(args.collection)
    print(json.dumps(result, ensure_ascii=False, indent=1))
    print(f'[indexer] {args.cmd} 완료 ({time.time() - t0:.1f}s)')


if __name__ == '__main__':
    main()
