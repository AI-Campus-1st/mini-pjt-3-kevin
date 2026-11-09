"""
검색기 (docs/api_spec.md §2)
- Chroma 컬렉션 읽기 전용 조회 + 쿼리 임베딩만 수행 (문서 임베딩·쓰기 금지, masterplan §4-1)
- 쿼리 임베딩 모델은 컬렉션 메타데이터의 embedding_model을 따름 (data_spec §5-4, 문서·쿼리 모델 불일치 방지)
- 점수는 코사인 유사도 (1 - cosine distance)
- 챗봇(rag/chain.py)과 평가(eval/run_eval.py)가 같은 Retriever를 사용
- 하이브리드 검색(E5): 벡터 상위 k + BM25 상위 k를 RRF(Reciprocal Rank Fusion)로 결합, 점수는 코사인 유사도 유지
"""
from dataclasses import dataclass, field, replace

import chromadb
import numpy as np
from openai import OpenAI

import config

_query_cache: dict[tuple[str, str], list[float]] = {}


@dataclass
class RetrievedChunk:
    rank: int
    chunk_id: str
    doc_id: str
    score: float
    text: str
    meta: dict = field(default_factory=dict)
    extra: dict = field(default_factory=dict)   # 결합 검색 정보 (rrf, vec_rank, bm25_rank)


def build_where(filters: dict | None) -> dict | None:
    """filters {'year_from', 'year_to', 'doc_types'} → Chroma where 조건"""
    if not filters:
        return None
    conds = []
    if filters.get('year_from'):
        conds.append({'published_year': {'$gte': int(filters['year_from'])}})
    if filters.get('year_to'):
        conds.append({'published_year': {'$lte': int(filters['year_to'])}})
    if filters.get('doc_types'):
        conds.append({'doc_type': {'$in': list(filters['doc_types'])}})
    if not conds:
        return None
    return conds[0] if len(conds) == 1 else {'$and': conds}


def match_filters(meta: dict, filters: dict | None) -> bool:
    """build_where와 같은 조건을 메타데이터에 직접 적용 (BM25 결과 필터링용)"""
    if not filters:
        return True
    year = int(meta.get('published_year') or 0)
    if filters.get('year_from') and year < int(filters['year_from']):
        return False
    if filters.get('year_to') and year > int(filters['year_to']):
        return False
    if filters.get('doc_types') and meta.get('doc_type') not in set(filters['doc_types']):
        return False
    return True


class Retriever:
    def __init__(self, collection: str | None = None, top_k: int = config.TOP_K):
        self.collection_name = collection or config.collection_name('A', True)
        self.top_k = top_k
        db = chromadb.PersistentClient(path=str(config.CHROMA_DIR))
        self.coll = db.get_collection(self.collection_name, embedding_function=None)
        self.info = dict(self.coll.metadata or {})
        self.embedding_model = self.info.get('embedding_model', config.EMBEDDING_MODEL)
        self._client = OpenAI()
        self._bm25 = None

    @property
    def bm25(self):
        if self._bm25 is None:
            from rag.bm25 import BM25Index
            self._bm25 = BM25Index(self.collection_name)
        return self._bm25

    def embed_queries(self, queries: list[str]) -> list[list[float]]:
        """쿼리 임베딩 (같은 모델·질문은 프로세스 안에서 재사용)"""
        todo = [q for q in dict.fromkeys(queries) if (self.embedding_model, q) not in _query_cache]
        for i in range(0, len(todo), 500):
            part = todo[i:i + 500]
            resp = self._client.embeddings.create(model=self.embedding_model, input=part)
            for q, d in zip(part, sorted(resp.data, key=lambda d: d.index)):
                _query_cache[(self.embedding_model, q)] = d.embedding
        return [_query_cache[(self.embedding_model, q)] for q in queries]

    def search(self, query: str, k: int | None = None, filters: dict | None = None,
               query_embedding: list[float] | None = None) -> list[RetrievedChunk]:
        """유사도 상위 k개 청크 (청크 단위, 문서 중복 허용)"""
        k = k or self.top_k
        emb = query_embedding or self.embed_queries([query])[0]
        res = self.coll.query(query_embeddings=[emb], n_results=k, where=build_where(filters),
                              include=['documents', 'metadatas', 'distances'])
        out = []
        for i, (cid, text, meta, dist) in enumerate(zip(res['ids'][0], res['documents'][0], res['metadatas'][0],
                                                        res['distances'][0]), start=1):
            out.append(RetrievedChunk(rank=i, chunk_id=cid, doc_id=meta['doc_id'], score=round(1 - dist, 6),
                                      text=text, meta=meta))
        return out

    def search_hybrid(self, query: str, k: int | None = None, filters: dict | None = None,
                      query_embedding: list[float] | None = None, rrf_k: int = config.RRF_K,
                      vec_key=None) -> list[RetrievedChunk]:
        """벡터 상위 k + BM25 상위 k를 RRF로 결합한 상위 k개 청크
        - vec_key: 벡터 결과 재정렬 기준 함수 (높을수록 앞, 예: 신뢰등급 가산점), 결합 전에 적용"""
        k = k or self.top_k
        emb = query_embedding or self.embed_queries([query])[0]
        vec = self.search(query, k=k, filters=filters, query_embedding=emb)
        if vec_key:
            vec = [replace(c, rank=i) for i, c in enumerate(sorted(vec, key=lambda c: -vec_key(c)), start=1)]
        hits = self.bm25.search(query, k * 4 if filters else k)
        ids = [cid for cid, _ in hits]
        got = self.coll.get(ids=ids, include=['documents', 'metadatas', 'embeddings']) if ids else {'ids': []}
        by_id = {cid: (doc, meta, e) for cid, doc, meta, e in
                 zip(got['ids'], got.get('documents') or [], got.get('metadatas') or [], got.get('embeddings', []))}
        q = np.asarray(emb, dtype=np.float32)
        bm_rank: dict[str, int] = {}
        for cid in ids:
            if cid in by_id and match_filters(by_id[cid][1], filters) and len(bm_rank) < k:
                bm_rank[cid] = len(bm_rank) + 1
        pool = {c.chunk_id: c for c in vec}
        for cid in bm_rank:
            if cid not in pool:
                doc, meta, e = by_id[cid]
                e = np.asarray(e, dtype=np.float32)
                cos = float(q @ e / (np.linalg.norm(q) * np.linalg.norm(e)))
                pool[cid] = RetrievedChunk(rank=0, chunk_id=cid, doc_id=meta['doc_id'], score=round(cos, 6),
                                           text=doc, meta=meta)
        vec_rank = {c.chunk_id: c.rank for c in vec}
        fused = []
        for cid, c in pool.items():
            rrf = sum(1 / (rrf_k + r[cid]) for r in (vec_rank, bm_rank) if cid in r)
            fused.append((rrf, c.score, cid))
        fused.sort(key=lambda x: (-x[0], -x[1]))
        out = []
        for i, (rrf, _, cid) in enumerate(fused[:k], start=1):
            c = pool[cid]
            out.append(RetrievedChunk(rank=i, chunk_id=cid, doc_id=c.doc_id, score=c.score, text=c.text, meta=c.meta,
                                      extra={'rrf': round(rrf, 6), 'vec_rank': vec_rank.get(cid),
                                             'bm25_rank': bm_rank.get(cid)}))
        return out


def dedup_docs(chunks: list[RetrievedChunk]) -> list[RetrievedChunk]:
    """문서 단위 중복 제거 (문서별 최고 점수 청크만 남기고 순위 재부여)"""
    seen, out = set(), []
    for c in chunks:
        if c.doc_id in seen:
            continue
        seen.add(c.doc_id)
        out.append(RetrievedChunk(rank=len(out) + 1, chunk_id=c.chunk_id, doc_id=c.doc_id, score=c.score,
                                  text=c.text, meta=c.meta, extra=c.extra))
    return out
