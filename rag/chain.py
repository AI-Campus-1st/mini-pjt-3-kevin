"""
RAG 체인 (docs/api_spec.md §4)
- 질의 재작성(이력 있을 때) → 검색 → 임계값 판정 → 문서 단위 번호 컨텍스트 조립 → 생성(스트리밍) → 인용 매핑·거절 판정
- 재작성·생성은 LangChain LCEL 체인 (ChatPromptTemplate | ChatOpenAI | StrOutputParser)
- 챗봇(app/)과 평가(eval/run_eval.py)가 같은 build_chain(cfg) 사용
"""
import re
import time
from dataclasses import asdict, dataclass, field, replace
from typing import Iterator

from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from langchain_openai import ChatOpenAI

import config
from rag import prompts
from rag.retriever import RetrievedChunk, Retriever

CITE_RE = re.compile(r'\[(\d{1,2}(?:\s*[,，]\s*\d{1,2})*)\]')
REFUSAL_PAT = re.compile(prompts.REFUSAL_RE)
NO_RESULT_MSG = ('조건에 맞는 문서를 찾지 못했습니다. 사이드바의 기간 또는 문서 종류 필터를 넓히거나 '
                 '질문의 표현을 바꿔 다시 시도해 주세요.')
BELOW_THRESHOLD_MSG = (f'{prompts.REFUSAL_PHRASE}. 질문과 충분히 관련된 문서가 검색되지 않았습니다 '
                       '(유사도 임계값 미달). 필터를 넓히거나 질문을 구체적으로 바꿔 보세요.')


def default_cfg() -> dict:
    return {
        'collection': config.DEFAULT_COLLECTION,
        'top_k': config.TOP_K,
        'fetch_k': config.FETCH_K,
        'hybrid': config.HYBRID,
        'tier_bonus': config.TIER_BONUS,
        'primary_quota': config.PRIMARY_QUOTA,
        'doc_chunk_cap': config.DOC_CHUNK_CAP,
        'score_threshold': config.SCORE_THRESHOLD,
        'prompt_version': config.PROMPT_VERSION,
        'llm_model': config.LLM_MODEL,
        'reasoning_effort': config.LLM_REASONING_EFFORT,
        'rewrite': True,
        'history_turns': config.HISTORY_TURNS,
    }


@dataclass
class Prepared:
    question: str
    query: str
    rewritten: bool
    chunks: list[RetrievedChunk]
    sources: list[dict]
    context: str
    history_msgs: list
    status: str
    latency_ms: dict = field(default_factory=dict)
    candidates: list[RetrievedChunk] = field(default_factory=list)


@dataclass
class RagAnswer:
    question: str
    query: str
    rewritten: bool
    status: str
    answer: str
    sources: list[dict]
    citations: list[dict]
    invalid_citations: list[int]
    refused: bool
    partial_refusal: bool
    latency_ms: dict
    config: dict

    def to_dict(self) -> dict:
        return asdict(self)


def strip_prefix(text: str, title_prefix: bool) -> str:
    """제목 prefix 컬렉션이면 첫 줄([문서종류] 제목 (발행처, 날짜)) 제거 (컨텍스트 헤더에 별도 표기)"""
    if title_prefix and text.startswith('[') and '\n' in text:
        return text.split('\n', 1)[1]
    return text


def group_sources(chunks: list[RetrievedChunk], title_prefix: bool) -> list[dict]:
    """검색 순위대로 문서 단위 번호 부여, 같은 문서 청크는 묶음 (api_spec §4-6)"""
    by_doc: dict[str, dict] = {}
    for c in chunks:
        m = c.meta
        if c.doc_id not in by_doc:
            by_doc[c.doc_id] = {
                'n': len(by_doc) + 1, 'doc_id': c.doc_id, 'doc_type': m.get('doc_type', ''),
                'doc_type_label': config.DOC_TYPE_LABEL.get(m.get('doc_type', ''), m.get('doc_type', '')),
                'source_tier': int(m.get('source_tier', 0)), 'title': m.get('title', ''),
                'publisher': m.get('publisher', ''), 'published_at': m.get('published_at', ''),
                'url': m.get('url', ''), 'citation': m.get('citation', ''), 'score': c.score,
                'chunks': [], 'cited': False,
            }
        by_doc[c.doc_id]['chunks'].append({'chunk_id': c.chunk_id, 'score': c.score,
                                           'text': strip_prefix(c.text, title_prefix),
                                           'section': m.get('section', ''), 'page': int(m.get('page', 0))})
    return list(by_doc.values())


def format_context(sources: list[dict]) -> str:
    blocks = []
    for s in sources:
        tier = s['source_tier']
        head = (f'[{s["n"]}] 문서종류: {s["doc_type_label"]} (신뢰등급 {tier}: {prompts.TIER_DESC.get(tier, "")}) '
                f'| 발행처: {s["publisher"]} | 발행일: {s["published_at"] or "미상"}\n제목: {s["title"]}')
        body = '\n...\n'.join(c['text'] for c in sorted(s['chunks'], key=lambda c: c['chunk_id']))
        blocks.append(f'{head}\n{body}')
    return '\n\n'.join(blocks)


def extract_citations(text: str, sources: list[dict]) -> tuple[list[dict], list[int]]:
    valid = {s['n']: s['doc_id'] for s in sources}
    seen, cites, invalid = set(), [], []
    for m in CITE_RE.finditer(text):
        for part in re.split(r'[,，]', m.group(1)):
            n = int(part.strip())
            if n in seen:
                continue
            seen.add(n)
            if n in valid:
                cites.append({'n': n, 'doc_id': valid[n]})
            else:
                invalid.append(n)
    return cites, invalid


def tier_score(c: RetrievedChunk, bonus: dict) -> float:
    """유사도 + 신뢰등급 가산점 (tier_bonus 정렬 기준)"""
    return c.score + bonus.get(int(c.meta.get('source_tier', 0)), 0.0)


def apply_primary_quota(chunks: list[RetrievedChunk], quota: int, slots: int = 5) -> list[RetrievedChunk]:
    """문서 단위 상위 slots개 안에 1차 자료(신뢰등급 1~2) 문서가 quota개 이상 오도록 문서 순서 조정
    - 남은 자리 수가 채워야 할 1차 자료 수와 같아지면 그 자리부터는 1차 자료 문서만 배치
    - 상위 slots개 문서의 최고 순위 청크를 조정된 순서로 앞에 두고, 나머지 청크는 원래 순서 유지"""
    order: list[str] = []
    head: dict[str, RetrievedChunk] = {}
    for c in chunks:
        if c.doc_id not in head:
            head[c.doc_id] = c
            order.append(c.doc_id)
    is_primary = {d: 1 <= int(head[d].meta.get('source_tier', 0)) <= 2 for d in order}
    need = min(quota, sum(is_primary.values()))
    placed, rest = [], list(order)
    while rest and len(placed) < slots:
        remaining = slots - len(placed)
        have = sum(1 for d in placed if is_primary[d])
        d = next(x for x in rest if is_primary[x]) if need - have >= remaining else rest[0]
        placed.append(d)
        rest.remove(d)
    heads = [head[d] for d in placed]
    head_ids = {c.chunk_id for c in heads}
    return heads + [c for c in chunks if c.chunk_id not in head_ids]


def detect_refusal(text: str) -> tuple[bool, bool]:
    """(첫 문장이 거절인지, 답변 어딘가에 거절 표현이 있는지)
    - 프롬프트가 핵심 근거 부재 시 첫 문장을 거절 문구로 시작하도록 지시하므로 첫 문장으로 판정
    - 결론 둘째 문장 이후의 "일부 확인 불가" 서술은 부분 거절로만 집계
    - 문장 끝 인용 번호("...입니다.[1] 다만")가 문장 분리를 막지 않도록 인용 번호 제거 후 분리"""
    head = CITE_RE.sub('', re.split(r'\n\s*\n|\n#', text.strip(), maxsplit=1)[0])
    first = re.split(r'(?<=[.!?])\s', head, maxsplit=1)[0]
    return bool(REFUSAL_PAT.search(first)), bool(REFUSAL_PAT.search(text))


class RagChain:
    def __init__(self, cfg: dict | None = None):
        self.cfg = {**default_cfg(), **(cfg or {})}
        self.retriever = Retriever(self.cfg['collection'], top_k=self.cfg['top_k'])
        self.title_prefix = bool(self.retriever.info.get('title_prefix', True))
        if self.cfg['hybrid']:   # BM25 인덱스·형태소 분석기를 생성 시점에 적재 (첫 질문 지연 방지)
            self.retriever.bm25.search('지역사랑상품권', 1)
        llm_kwargs = {'model': self.cfg['llm_model']}
        if config.LLM_TEMPERATURE is not None:
            llm_kwargs['temperature'] = config.LLM_TEMPERATURE
        gen_llm = ChatOpenAI(**llm_kwargs, **({'reasoning_effort': self.cfg['reasoning_effort']}
                                              if self.cfg['reasoning_effort'] else {}))
        rw_llm = ChatOpenAI(**llm_kwargs, reasoning_effort=config.REWRITE_REASONING_EFFORT)
        system = prompts.SYSTEM_PROMPTS[self.cfg['prompt_version']]
        self.answer_chain = (ChatPromptTemplate.from_messages([
            ('system', system), MessagesPlaceholder('history'), ('human', prompts.HUMAN_TEMPLATE)])
            | gen_llm | StrOutputParser())
        self.rewrite_chain = (ChatPromptTemplate.from_messages([
            ('system', prompts.REWRITE_SYSTEM), ('human', prompts.REWRITE_HUMAN)])
            | rw_llm | StrOutputParser())

    def _recent(self, history: list[dict] | None) -> list[dict]:
        if not history:
            return []
        return history[-2 * self.cfg['history_turns']:]

    def _history_msgs(self, history: list[dict]) -> list:
        """생성 단계에 전달할 이전 대화 (이전 답변의 인용 번호는 이번 자료 번호와 혼동되므로 제거)"""
        msgs = []
        for h in history:
            text = h['content']
            if h['role'] == 'assistant':
                text = CITE_RE.sub('', text)[:config.HISTORY_ANSWER_MAX_CHARS]
                msgs.append(('ai', text))
            else:
                msgs.append(('human', text))
        return msgs

    def prepare(self, question: str, history: list[dict] | None = None, filters: dict | None = None) -> Prepared:
        recent = self._recent(history)
        lat = {}
        query, rewritten = question, False
        t0 = time.perf_counter()
        if recent and self.cfg['rewrite']:
            hist_text = '\n'.join(f'{"사용자" if h["role"] == "user" else "도우미"}: '
                                  f'{CITE_RE.sub("", h["content"])[:config.HISTORY_ANSWER_MAX_CHARS]}' for h in recent)
            out = self.rewrite_chain.invoke({'history': hist_text, 'question': question}).strip()
            if out:
                query, rewritten = out.splitlines()[0].strip(), out.strip() != question.strip()
        lat['rewrite'] = round((time.perf_counter() - t0) * 1000, 1)

        t0 = time.perf_counter()
        fetch_k = max(self.cfg['fetch_k'], self.cfg['top_k'])
        if self.cfg['hybrid']:
            bonus = self.cfg.get('tier_bonus')
            found = self.retriever.search_hybrid(query, k=fetch_k, filters=filters,
                                                 vec_key=(lambda c: tier_score(c, bonus)) if bonus else None)
        else:
            found = self.retriever.search(query, k=fetch_k, filters=filters)
        candidates = self.rerank(found)
        lat['retrieve'] = round((time.perf_counter() - t0) * 1000, 1)
        chunks = self.select_context(candidates)

        status = 'ok'
        thr = self.cfg['score_threshold']
        if not chunks:
            status = 'no_result'
        elif thr is not None:
            chunks = [c for c in chunks if c.score >= thr]
            if not chunks:
                status = 'below_threshold'
        sources = group_sources(chunks, self.title_prefix)
        return Prepared(question=question, query=query, rewritten=rewritten, chunks=chunks, sources=sources,
                        context=format_context(sources), history_msgs=self._history_msgs(recent),
                        status=status, latency_ms=lat, candidates=candidates)

    def rerank(self, candidates: list[RetrievedChunk]) -> list[RetrievedChunk]:
        """검색 후보 재정렬 (Day 5 검색 개선 실험, 설정이 없으면 검색 순서 유지)
        - tier_bonus: 신뢰등급별 가산점을 유사도에 더해 재정렬 (하이브리드는 검색 단계에서 벡터 순위에 적용 후 RRF 결합)
        - primary_quota: 문서 단위 상위 5위 안에 1차 자료(신뢰등급 1~2) 문서를 최소 N개 배치, 나머지는 원래 순서"""
        out = list(candidates)
        bonus = self.cfg.get('tier_bonus')
        if bonus and not self.cfg['hybrid']:
            out.sort(key=lambda c: -tier_score(c, bonus))
        quota = self.cfg.get('primary_quota')
        if quota:
            out = apply_primary_quota(out, quota)
        return [replace(c, rank=i) for i, c in enumerate(out, start=1)]

    def select_context(self, candidates: list[RetrievedChunk]) -> list[RetrievedChunk]:
        """검색 후보(fetch_k)에서 생성 단계에 넣을 top_k 청크 선택
        - 기본: 후보 순서대로 상위 top_k
        - doc_chunk_cap: 문서당 최대 청크 수 제한 (장문 문서 한 건이 컨텍스트를 독점하는 것 방지)"""
        cap = self.cfg.get('doc_chunk_cap')
        if not cap:
            return candidates[:self.cfg['top_k']]
        out, per_doc = [], {}
        for c in candidates:
            if per_doc.get(c.doc_id, 0) >= cap:
                continue
            per_doc[c.doc_id] = per_doc.get(c.doc_id, 0) + 1
            out.append(c)
            if len(out) == self.cfg['top_k']:
                break
        return out

    def stream(self, prep: Prepared) -> Iterator[str]:
        t0 = time.perf_counter()
        if prep.status == 'no_result':
            yield NO_RESULT_MSG
        elif prep.status == 'below_threshold':
            yield BELOW_THRESHOLD_MSG
        else:
            yield from self.answer_chain.stream({'history': prep.history_msgs, 'context': prep.context,
                                                 'question': prep.question})
        prep.latency_ms['generate'] = round((time.perf_counter() - t0) * 1000, 1)

    def finalize(self, prep: Prepared, text: str) -> RagAnswer:
        cites, invalid = extract_citations(text, prep.sources)
        cited = {c['n'] for c in cites}
        for s in prep.sources:
            s['cited'] = s['n'] in cited
        refused_head, has_phrase = detect_refusal(text)
        skipped = prep.status != 'ok'
        refused = skipped or refused_head
        lat = dict(prep.latency_ms)
        lat['total'] = round(sum(lat.get(k, 0) for k in ('rewrite', 'retrieve', 'generate')), 1)
        return RagAnswer(question=prep.question, query=prep.query, rewritten=prep.rewritten, status=prep.status,
                         answer=text, sources=prep.sources, citations=cites, invalid_citations=invalid,
                         refused=refused, partial_refusal=(has_phrase and not refused), latency_ms=lat,
                         config=dict(self.cfg))

    def invoke(self, question: str, history: list[dict] | None = None, filters: dict | None = None) -> RagAnswer:
        prep = self.prepare(question, history, filters)
        text = ''.join(self.stream(prep))
        return self.finalize(prep, text)


def build_chain(cfg: dict | None = None) -> RagChain:
    return RagChain(cfg)
