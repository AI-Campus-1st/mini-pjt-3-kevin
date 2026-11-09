"""
BM25 희소 검색 (하이브리드 검색 확장 E5, masterplan §6-3)
- 형태소 분석: kiwipiepy, 내용어(체언·수사·외국어·한자·숫자·어근·용언 어간)만 색인어로 사용
- 문서 쪽 가중치는 인덱서(indexer/bm25.py)가 미리 계산해 store/bm25/{collection}/ 에 저장, 여기서는 읽기 전용 조회만 수행
- 질의 점수 = 질의 색인어 열(column) 가중치 합 (Okapi BM25)
- 인덱서와 검색이 같은 tokenize()를 사용 (색인어 규칙 불일치 방지)
- 도메인 핵심 용어는 사용자 사전(config.BM25_USER_WORDS)으로 한 단어 처리
"""
import json
import threading

import numpy as np
from scipy import sparse

import config

KEEP_TAGS = {'NNG', 'NNP', 'NNB', 'NR', 'SL', 'SH', 'SN', 'XR', 'VV', 'VA'}
_kiwi = None
_kiwi_lock = threading.Lock()


def _get_kiwi():
    global _kiwi
    with _kiwi_lock:
        if _kiwi is None:
            from kiwipiepy import Kiwi
            _kiwi = Kiwi()
            for w in config.BM25_USER_WORDS:
                _kiwi.add_user_word(w, 'NNP')
        return _kiwi


def _terms(tokens) -> list[str]:
    return [t.form.lower() if t.tag == 'SL' else t.form for t in tokens if t.tag in KEEP_TAGS]


def tokenize(text: str) -> list[str]:
    kiwi = _get_kiwi()
    with _kiwi_lock:
        return _terms(kiwi.tokenize(text))


def tokenize_many(texts: list[str]) -> list[list[str]]:
    """일괄 형태소 분석 (인덱스 구축용)"""
    kiwi = _get_kiwi()
    return [_terms(toks) for toks in kiwi.tokenize(texts)]


def index_dir(collection: str):
    return config.STORE_DIR / 'bm25' / collection


class BM25Index:
    def __init__(self, collection: str):
        d = index_dir(collection)
        if not (d / 'weights.npz').exists():
            raise FileNotFoundError(f'BM25 인덱스 없음: {d} (python -m indexer bm25 --collection {collection} 실행 필요)')
        self.weights = sparse.load_npz(d / 'weights.npz').tocsc()   # 청크 × 어휘
        self.vocab = {t: i for i, t in enumerate(json.loads((d / 'vocab.json').read_text(encoding='utf-8')))}
        self.chunk_ids = json.loads((d / 'chunk_ids.json').read_text(encoding='utf-8'))
        self.meta = json.loads((d / 'meta.json').read_text(encoding='utf-8'))

    def search(self, query: str, k: int) -> list[tuple[str, float]]:
        cols = [self.vocab[t] for t in tokenize(query) if t in self.vocab]
        if not cols:
            return []
        scores = np.asarray(self.weights[:, cols].sum(axis=1)).ravel()
        k = min(k, int((scores > 0).sum()))
        if k <= 0:
            return []
        top = np.argpartition(-scores, k - 1)[:k]
        top = top[np.argsort(-scores[top])]
        return [(self.chunk_ids[i], round(float(scores[i]), 4)) for i in top]
