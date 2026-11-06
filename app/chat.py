"""
챗 탭 (가이드 4-6 답변 화면, 4-7 챗봇 UI 요건)
- 스트리밍 출력, 검색 진행 상태 표시, 멀티턴(session_state), 기간·문서 종류 필터, 빈 결과 안내
- 답변 아래 번호 출처 목록(제목·발행처·발행일·링크·문서 종류 배지), 근거 청크와 유사도, 원문 전체 보기
"""
import streamlit as st

from rag.chain import build_chain
from rag.docstore import get_document, kb_overview

TIER_COLOR = {1: 'blue', 2: 'green', 3: 'orange', 4: 'gray'}
TIER_NAME = {1: '공식 원문', 2: '기관 분석', 3: '언론', 4: '개인 글'}
EXAMPLES = [
    '지역사랑상품권 부정유통 시 과태료 상한과 근거 조항은?',
    '지역화폐의 지역경제 효과에 대한 찬반 근거는?',
    '연도별 지역사랑상품권 국비 예산은 어떻게 변했나?',
]


def md_safe(text: str) -> str:
    """화면 표시용 이스케이프: '~'(취소선으로 해석되어 '1~8월'이 깨짐), '$'(수식으로 해석)"""
    return text.replace('~', r'\~').replace('$', r'\$')


def stream_safe(gen):
    for tok in gen:
        yield md_safe(tok)


@st.cache_resource(show_spinner='검색기와 모델 준비 중...')
def get_chain():
    return build_chain()


def sidebar(ov: dict) -> dict:
    labels = ov['doc_type_labels']
    with st.sidebar:
        st.header('검색 필터')
        y0, y1 = st.slider('발행 연도', ov['year_min'], ov['year_max'], (ov['year_min'], ov['year_max']))
        types = st.multiselect('문서 종류 (비우면 전체)', ov['doc_types'],
                               format_func=lambda t: f'{labels.get(t, t)} (등급 {ov["doc_type_tier"].get(t, "-")})')
        st.caption('연도 범위를 좁히면 발행일 미상 문서는 제외')
        if st.button('대화 초기화', use_container_width=True):
            st.session_state.messages = []
            st.rerun()
        cfg = get_chain().cfg
        with st.expander('검색·생성 설정'):
            st.markdown(f'- 컬렉션: `{cfg["collection"]}`\n- 생성 모델: `{cfg["llm_model"]}`\n'
                        f'- 상위 청크 수: {cfg["top_k"]}\n- 유사도 임계값: {cfg["score_threshold"]}\n'
                        f'- 프롬프트: {cfg["prompt_version"]}')
        st.caption('답변은 근거 문서로 연결하기 위한 요약. 인용 전 원문 확인 필요')
    filters = {}
    if (y0, y1) != (ov['year_min'], ov['year_max']):
        filters.update({'year_from': y0, 'year_to': y1})
    if types:
        filters['doc_types'] = types
    return filters


def source_line(s: dict) -> str:
    tier = s['source_tier']
    badge = f':{TIER_COLOR.get(tier, "gray")}-badge[{s["doc_type_label"]} · {TIER_NAME.get(tier, "")}]'
    meta = ', '.join(x for x in [s['publisher'], s['published_at']] if x)
    link = f' · [원문 링크]({s["url"]})' if s['url'] else (f' · {s["citation"]}' if s['citation'] else '')
    return f'**[{s["n"]}]** {badge} {md_safe(s["title"])} ({meta}){md_safe(link) if not s["url"] else link}'


def source_detail(s: dict, key: str):
    with st.expander(f'[{s["n"]}] 근거 청크 {len(s["chunks"])}개 · 최고 유사도 {s["score"]:.3f}'):
        for c in s['chunks']:
            loc = ' · '.join(x for x in [c['section'], f'{c["page"]}쪽' if c['page'] else ''] if x)
            st.caption(f'`{c["chunk_id"]}` 유사도 {c["score"]:.3f}' + (f' · {loc}' if loc else ''))
            st.text(c['text'])
        if st.toggle('원문 전체 보기', key=f'{key}_full_{s["n"]}'):
            doc = get_document(s['doc_id'])
            if doc:
                pages = f' (포함 쪽: {doc["page_range"]})' if doc['page_range'] else ''
                st.caption(f'{doc["title"]} · {doc["body_len"]:,}자{pages}')
                with st.container(height=400):
                    st.text(doc['body'])


def render_result(res: dict, key: str):
    if res['status'] == 'no_result':
        return
    lat = res['latency_ms']
    info = [f'검색 질의: "{res["query"]}"' if res['rewritten'] else '',
            f'검색 {lat.get("retrieve", 0) / 1000:.1f}s · 생성 {lat.get("generate", 0) / 1000:.1f}s'
            + (f' · 질의 재작성 {lat["rewrite"] / 1000:.1f}s' if res['rewritten'] else '')]
    st.caption(' | '.join(x for x in info if x))
    if res['invalid_citations']:
        st.warning(f'자료에 없는 인용 번호: {res["invalid_citations"]}')
    cited = [s for s in res['sources'] if s['cited']]
    other = [s for s in res['sources'] if not s['cited']]
    if cited:
        st.markdown('**출처**')
        for s in cited:
            st.markdown(source_line(s))
            source_detail(s, key)
    if other:
        with st.expander(f'검색되었으나 인용되지 않은 문서 {len(other)}건'):
            for s in other:
                st.markdown(source_line(s))
                source_detail(s, key)


def answer(chain, question: str, history: list[dict], filters: dict, key: str) -> dict:
    with st.status('관련 문서 검색 중...', expanded=False) as status:
        prep = chain.prepare(question, history, filters)
        if prep.rewritten:
            st.write(f'질의 재작성: {prep.query}')
        st.write(f'검색 문서 {len(prep.sources)}건')
        status.update(label=f'문서 {len(prep.sources)}건 검색 완료 · 답변 생성 중...', state='running')
    parts = []

    def collect():
        for tok in chain.stream(prep):
            parts.append(tok)
            yield tok
    st.write_stream(stream_safe(collect()))
    res = chain.finalize(prep, ''.join(parts)).to_dict()
    done = {'ok': '답변 완료', 'no_result': '검색 결과 없음', 'below_threshold': '관련 문서 부족'}[res['status']]
    status.update(label=f'{done} · 문서 {len(prep.sources)}건 검색, {len(res["citations"])}건 인용',
                  state='complete')
    render_result(res, key)
    return res


def render():
    ov = kb_overview()
    filters = sidebar(ov)
    if 'messages' not in st.session_state:
        st.session_state.messages = []
    msgs = st.session_state.messages

    st.subheader('지역화폐(지역사랑상품권) 정책 자료 챗봇')
    st.caption('법령·보도자료·국회 자료·연구보고서·뉴스·블로그에서 근거를 찾아 번호로 연결. '
               '신뢰 등급: 1 공식 원문 / 2 기관 분석 / 3 언론 / 4 개인 글')
    # 대화 영역을 입력창보다 먼저 배치하고, 입력값을 받은 뒤 채움 (탭 안의 chat_input은 호출 위치에 표시됨)
    box = st.container()
    question = st.chat_input('질문을 입력하세요') or st.session_state.pop('pending', None)
    with box:
        for i, m in enumerate(msgs):
            with st.chat_message(m['role']):
                st.markdown(md_safe(m['content']))
                if m['role'] == 'assistant' and m.get('result'):
                    render_result(m['result'], key=f'm{i}')
        if not msgs and not question:
            st.caption('예시 질문')
            cols = st.columns(len(EXAMPLES))
            for c, ex in zip(cols, EXAMPLES):
                if c.button(ex, use_container_width=True):
                    st.session_state.pending = ex
                    st.rerun()
    if not question:
        return
    history = [{'role': m['role'], 'content': m['content']} for m in msgs]
    with box:
        with st.chat_message('user'):
            st.markdown(md_safe(question))
        with st.chat_message('assistant'):
            res = answer(get_chain(), question, history, filters, key=f'm{len(msgs) + 1}')
    msgs.append({'role': 'user', 'content': question})
    msgs.append({'role': 'assistant', 'content': res['answer'], 'result': res})
