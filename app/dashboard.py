"""
평가 대시보드 탭 (masterplan §7, 가이드 평가 결과 시각화)
- 입력: eval/results/{YYYYMMDD-HHMM}_{tag}/ 의 config.json, summary.json, per_question.jsonl, graded_summary.json (파일 읽기만)
- 지식베이스 구성은 rag.docstore (읽기 전용) 경유
- 구성: 핵심 수치 → 검색 실험 비교 → 유형별 Hit@3 → 답변 지표(자동·직접 채점) → 유사도 분포와 임계값 → 지식베이스 구성 → 응답시간
- 색: 검증된 범주형 팔레트 앞 3개 슬롯만 사용 (차트당 계열 3개 이하), 수치·라벨은 텍스트 색 유지
"""
import json
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from rag.docstore import kb_distribution

RESULTS_DIR = Path(__file__).resolve().parents[1] / 'eval' / 'results'
SERIES = ['#2a78d6', '#eb6834', '#1baf7a']   # 범주형 슬롯 1~3 (blue, orange, aqua)
TARGET_HIT3 = 0.75
TYPE_LABEL = {'fact': '단순 사실', 'compare': '비교', 'temporal': '시점', 'synthesis': '종합·판단',
              'unanswerable': '답변 불가'}

# 검색 실험 (retrieval-only, 문서 단위 지표), 표시 순서 = 실험 순서
RETRIEVAL_RUNS = [
    ('baseline', 'E0 기준선'),
    ('chunk_B', 'E1 청킹 B'),
    ('no_title', 'E2 제목 prefix 제거'),
    ('quota2', '1차 자료 할당 2'),
    ('quota3', '1차 자료 할당 3'),
    ('cap2', '문서당 청크 2'),
    ('tier_b02', '등급 가산점 0.02'),
    ('tier_b04', '등급 가산점 0.04'),
    ('tier_b06', '등급 가산점 0.06'),
    ('hybrid', 'E5 하이브리드'),
    ('hybrid_tier_b02', '하이브리드+가산점 0.02'),
    ('hybrid_tier_b04', '하이브리드+가산점 0.04 (채택)'),
    ('hybrid_tier_b06', '하이브리드+가산점 0.06'),
]
# 답변 생성 실험 (full 모드)
FULL_RUNS = [
    ('baseline', 'E0 (검색 기준선, 프롬프트 v1)'),
    ('hybrid_tier_b04', '채택 검색 (프롬프트 v1)'),
    ('prompt_v2', 'E4 채택 검색 + 프롬프트 v2'),
]


def latest(tag: str, mode: str) -> Path | None:
    runs = []
    for p in RESULTS_DIR.glob(f'*_{tag}'):
        if p.name.split('_', 1)[1] != tag or not (p / 'summary.json').exists():
            continue
        if json.loads((p / 'config.json').read_text(encoding='utf-8')).get('mode') == mode:
            runs.append(p)
    return sorted(runs)[-1] if runs else None


@st.cache_data(ttl=60)
def load_run(path: str) -> dict:
    p = Path(path)
    out = {'name': p.name,
           'config': json.loads((p / 'config.json').read_text(encoding='utf-8')),
           'summary': json.loads((p / 'summary.json').read_text(encoding='utf-8')),
           'rows': [json.loads(l) for l in open(p / 'per_question.jsonl', encoding='utf-8') if l.strip()]}
    g = p / 'graded_summary.json'
    out['graded'] = json.loads(g.read_text(encoding='utf-8')) if g.exists() else None
    return out


def runs(spec: list[tuple[str, str]], mode: str) -> list[tuple[str, dict]]:
    out = []
    for tag, label in spec:
        d = latest(tag, mode)
        if d:
            out.append((label, load_run(str(d))))
    return out


def style(fig: go.Figure, height: int = 360, **kw) -> go.Figure:
    fig.update_layout(height=height, margin=dict(l=8, r=8, t=36, b=8), bargap=0.25, bargroupgap=0.08,
                      legend=dict(orientation='h', yanchor='bottom', y=1.02, x=0), hovermode='closest', **kw)
    fig.update_traces(marker_line_width=0, selector=dict(type='bar'))
    fig.update_xaxes(showgrid=False)
    fig.update_yaxes(gridcolor='rgba(128,128,128,0.18)', zeroline=False)
    return fig


def section_headline(ret: list, full: list):
    st.subheader('핵심 수치')
    base = next((r for l, r in ret if r['config']['tag'] == 'baseline'), None)
    best = next((r for l, r in ret if r['config']['tag'] == 'hybrid_tier_b04'), None)
    c = st.columns(4)
    if base and best:
        b3, a3 = base['summary']['overall']['hit@3'], best['summary']['overall']['hit@3']
        c[0].metric('문서 단위 Hit@3 (목표 0.75)', f'{a3:.3f}', f'{a3 - b3:+.3f} (E0 {b3:.3f})')
        bm, am = base['summary']['overall']['mrr'], best['summary']['overall']['mrr']
        c[1].metric('MRR@10', f'{am:.3f}', f'{am - bm:+.3f} (E0 {bm:.3f})')
    if full:
        f0, fl = full[0][1]['summary']['answer'], full[-1][1]['summary']['answer']
        c[2].metric('오거절률 (자동, 다수결)', f'{fl["false_refusal_rate"]:.1%}',
                    f'{(fl["false_refusal_rate"] - f0["false_refusal_rate"]) * 100:+.1f}%p (E0 {f0["false_refusal_rate"]:.1%})',
                    delta_color='inverse')
        c[3].metric('거절 정확도 (자동, 다수결)', f'{fl["refusal_acc"]:.1%}', f'E0 {f0["refusal_acc"]:.1%}',
                    delta_color='off')
    st.caption('검색 지표: 답변 가능 52문항, 상위 50청크를 문서 단위로 중복 제거한 순위. '
               '답변 지표: 문항당 3회 생성, 거절 여부 다수결. 정확도·출처 정확성은 직접 채점 결과로 아래에 표시')


def section_retrieval(ret: list):
    st.subheader('검색 실험 비교')
    df = pd.DataFrame([{'실험': l, 'Hit@1': r['summary']['overall']['hit@1'], 'Hit@3': r['summary']['overall']['hit@3'],
                        'Hit@5': r['summary']['overall']['hit@5'], 'MRR@10': r['summary']['overall']['mrr'],
                        'ctx_hit@5': r['summary']['overall']['ctx_hit'], '변경 변수': r['config'].get('changed_variable', ''),
                        '실행': r['name']} for l, r in ret])
    fig = go.Figure()
    for i, m in enumerate(['Hit@1', 'Hit@3', 'MRR@10']):
        fig.add_bar(name=m, y=df['실험'], x=df[m], orientation='h', marker_color=SERIES[i],
                    hovertemplate='%{y}<br>' + m + ' %{x:.3f}<extra></extra>')
    fig.add_vline(x=TARGET_HIT3, line_dash='dash', line_width=1, line_color='gray',
                  annotation_text='목표 Hit@3 0.75', annotation_position='top')
    style(fig, height=520, barmode='group', xaxis=dict(range=[0, 1], title=None),
          yaxis=dict(autorange='reversed', title=None))
    st.plotly_chart(fig, use_container_width=True, theme='streamlit')
    with st.expander('표로 보기'):
        st.dataframe(df, hide_index=True, use_container_width=True,
                     column_config={k: st.column_config.NumberColumn(format='%.3f')
                                    for k in ['Hit@1', 'Hit@3', 'Hit@5', 'MRR@10', 'ctx_hit@5']})


def section_by_type(ret: list):
    st.subheader('질문 유형별 Hit@3')
    pick = [(l, r) for l, r in ret if r['config']['tag'] in ('baseline', 'hybrid', 'hybrid_tier_b04')]
    types = list(pick[0][1]['summary']['by_type']) if pick else []
    fig = go.Figure()
    for i, (l, r) in enumerate(pick):
        bt = r['summary']['by_type']
        fig.add_bar(name=l, x=[f'{TYPE_LABEL.get(t, t)} (n={bt[t]["n"]})' for t in types],
                    y=[bt[t]['hit@3'] for t in types], marker_color=SERIES[i],
                    hovertemplate='%{x}<br>' + l + ' Hit@3 %{y:.3f}<extra></extra>')
    style(fig, barmode='group', yaxis=dict(range=[0, 1], title='Hit@3'))
    st.plotly_chart(fig, use_container_width=True, theme='streamlit')
    st.caption('하이브리드 검색은 단순 사실·비교 문항에서 크게 오르고, 시점 문항은 기준선보다 낮음 (실패 분석 대상)')


def section_answers(full: list):
    st.subheader('답변 지표')
    if not full:
        st.info('full 모드 결과 없음')
        return
    metrics = [('refusal_acc', '거절 정확도'), ('false_refusal_rate', '오거절률'), ('partial_refusal_rate', '부분 거절률'),
               ('cited_gold_rate', '정답 문서 인용률'), ('points_auto', '필수 사실 포함률')]
    fig = go.Figure()
    for i, (l, r) in enumerate(full):
        a = r['summary']['answer']
        fig.add_bar(name=l, x=[m[1] for m in metrics], y=[a.get(m[0]) for m in metrics], marker_color=SERIES[i],
                    hovertemplate='%{x}<br>' + l + ' %{y:.1%}<extra></extra>')
    style(fig, barmode='group', yaxis=dict(range=[0, 1], tickformat='.0%', title=None))
    st.plotly_chart(fig, use_container_width=True, theme='streamlit')
    st.caption('자동 지표: 거절은 거절 문구 판정의 3회 다수결. 정답 문서 인용률·필수 사실 포함률은 거절하지 않은 답변 대상의 자동 참고값')

    graded = [(l, r) for l, r in full if r['graded'] and r['graded']['overall']['n_graded']]
    st.markdown('**직접 채점 결과**')
    if not graded:
        st.info('채점 완료된 결과 없음: 결과 폴더의 grade_sheet.csv 채점 후 `python -m eval.grade <결과 폴더>` 실행')
        return
    types = [t for t in TYPE_LABEL if t != 'unanswerable']
    fig = go.Figure()
    for i, (l, r) in enumerate(graded):
        bt = r['graded']['by_type']
        fig.add_bar(name=l, x=[TYPE_LABEL[t] for t in types] + ['전체'],
                    y=[bt.get(t, {}).get('correct_or_partial') for t in types] + [r['graded']['overall']['correct_or_partial']],
                    marker_color=SERIES[i], hovertemplate='%{x}<br>' + l + ' 정확+부분 %{y:.1%}<extra></extra>')
    fig.add_hline(y=0.8, line_dash='dash', line_width=1, line_color='gray', annotation_text='목표 80%')
    style(fig, barmode='group', yaxis=dict(range=[0, 1], tickformat='.0%', title='정확+부분 비율'))
    st.plotly_chart(fig, use_container_width=True, theme='streamlit')
    rows = []
    for l, r in graded:
        o = r['graded']['overall']
        rows.append({'실험': l, '채점 문항': o['n_graded'], '정확도 평균': o['accuracy_mean'],
                     '정확+부분': o['correct_or_partial'], '출처 정확성 (Y)': o['citation_y'],
                     '거절 정확도': o['refusal_acc'], '오거절률': o['false_refusal_rate'],
                     '실패 계층': ', '.join(f'{k} {v}' for k, v in r['graded']['failure_layer']['total'].items())})
    st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)


def section_scores(ret: list):
    st.subheader('유사도 분포와 임계값 (E3)')
    opts = {l: r for l, r in ret if r['config']['tag'] in ('baseline', 'hybrid_tier_b04')}
    if not opts:
        return
    c1, c2 = st.columns([2, 1])
    label = c1.selectbox('검색 설정', list(opts), index=len(opts) - 1)
    thr = c2.slider('임계값', 0.30, 0.70, 0.50, 0.01)
    r = opts[label]
    pts = []
    for q in r['rows']:
        mx = max(x['score'] for x in q['retrieved'][:r['config'].get('top_k', 5)])
        grp = '답변 불가' if q['type'] == 'unanswerable' else ('정답 문서 컨텍스트 포함' if q['ctx_hit'] else '정답 문서 미포함')
        pts.append({'id': q['id'], 'group': grp, 'score': mx})
    df = pd.DataFrame(pts)
    groups = ['정답 문서 컨텍스트 포함', '정답 문서 미포함', '답변 불가']
    fig = go.Figure()
    for i, g in enumerate(groups):
        d = df[df['group'] == g]
        fig.add_box(name=f'{g} (n={len(d)})', x=d['score'], marker_color=SERIES[i], boxpoints='all', jitter=0.4,
                    pointpos=0, marker_size=8, line_width=2, fillcolor='rgba(0,0,0,0)', customdata=d['id'],
                    hovertemplate='%{customdata}<br>최고 유사도 %{x:.3f}<extra></extra>', orientation='h')
    fig.add_vline(x=thr, line_dash='dash', line_width=1, line_color='gray', annotation_text=f'임계값 {thr:.2f}')
    style(fig, height=320, showlegend=False, xaxis=dict(title='컨텍스트 최고 유사도 (코사인)'))
    st.plotly_chart(fig, use_container_width=True, theme='streamlit')
    blocked = {g: int((df[df['group'] == g]['score'] < thr).sum()) for g in groups}
    st.markdown(f'임계값 {thr:.2f} 적용 시 검색 실패 처리: 답변 불가 {blocked["답변 불가"]}/{(df["group"] == "답변 불가").sum()}, '
                f'답변 가능 {blocked["정답 문서 컨텍스트 포함"] + blocked["정답 문서 미포함"]}/'
                f'{(df["group"] != "답변 불가").sum()} (그중 정답 문서가 컨텍스트에 있던 문항 {blocked["정답 문서 컨텍스트 포함"]})')
    st.caption('세 집단의 유사도 범위가 겹쳐 임계값만으로 답변 불가 문항을 분리하기 어려움 → 임계값 미채택, 프롬프트 거절 유지')


def section_kb():
    st.subheader('지식베이스 구성')
    kb = kb_distribution()
    df = pd.DataFrame(kb['by_type'])
    c1, c2 = st.columns(2)
    fig = go.Figure()
    fig.add_bar(name='문서', y=df['label'], x=df['n_docs'], orientation='h', marker_color=SERIES[0],
                hovertemplate='%{y}<br>문서 %{x:,}건<extra></extra>')
    fig.add_bar(name='청크', y=df['label'], x=df['n_chunks'], orientation='h', marker_color=SERIES[1],
                hovertemplate='%{y}<br>청크 %{x:,}개<extra></extra>')
    style(fig, height=380, barmode='group', xaxis=dict(type='log', title='건수 (로그 축)'),
          yaxis=dict(autorange='reversed', title=None))
    c1.plotly_chart(fig, use_container_width=True, theme='streamlit')
    dy = pd.DataFrame(kb['by_year'])
    fig = go.Figure()
    fig.add_bar(x=dy['year'].astype(str), y=dy['n_docs'], marker_color=SERIES[0],
                hovertemplate='%{x}년<br>%{y:,}건<extra></extra>')
    style(fig, height=380, showlegend=False, yaxis=dict(title='문서 수'), title=dict(text='발행 연도별 문서 수', x=0))
    c2.plotly_chart(fig, use_container_width=True, theme='streamlit')
    tot_d, tot_c = int(df['n_docs'].sum()), int(df['n_chunks'].sum())
    prim = df[df['tier'] <= 2]
    st.caption(f'문서 {tot_d:,}건, 청크 {tot_c:,}개 ({kb["collection"]}). 1차 자료(신뢰등급 1~2) 문서 {int(prim["n_docs"].sum())}건, '
               f'청크 비중 {prim["n_chunks"].sum() / tot_c:.1%}')


def section_latency(ret: list, full: list):
    st.subheader('응답시간')
    c1, c2 = st.columns(2)
    fig = go.Figure()
    short = {'baseline': 'E0 벡터', 'hybrid_tier_b04': '채택 하이브리드'}
    for i, (l, r) in enumerate([(l, r) for l, r in ret if r['config']['tag'] in short]):
        fig.add_box(name=short[r['config']['tag']], y=[q['latency_ms']['retrieve'] for q in r['rows']], marker_color=SERIES[i],
                    boxpoints='all', jitter=0.4, pointpos=0, marker_size=8, line_width=2,
                    hovertemplate='%{y:.0f} ms<extra></extra>')
    style(fig, showlegend=False, yaxis=dict(title='검색 (ms, 로그 축)', type='log'),
          title=dict(text='검색 단계 (E0 vs 하이브리드)', x=0))
    c1.plotly_chart(fig, use_container_width=True, theme='streamlit')
    fig = go.Figure()
    short = {'baseline': 'E0', 'hybrid_tier_b04': '채택 검색 v1', 'prompt_v2': '프롬프트 v2'}
    for i, (l, r) in enumerate(full):
        fig.add_box(name=short.get(r['config']['tag'], l), y=[q['latency_ms'].get('total') for q in r['rows']], marker_color=SERIES[i],
                    boxpoints='all', jitter=0.4, pointpos=0, marker_size=8, line_width=2,
                    hovertemplate='%{y:,.0f} ms<extra></extra>')
    style(fig, showlegend=False, yaxis=dict(title='전체 (ms)'), title=dict(text='검색+생성 전체 (대표 답변)', x=0))
    c2.plotly_chart(fig, use_container_width=True, theme='streamlit')
    st.caption('검색 단계의 400ms 안팎 1건은 프로세스 첫 질의의 벡터 인덱스 적재 시간 (두 설정 공통). '
               '검색 단계 차이(중앙값 약 7ms → 18ms)는 생성 시간(중앙값 약 4초)에 비해 무시할 수준')


def render():
    ret = runs(RETRIEVAL_RUNS, 'retrieval_only')
    full = runs(FULL_RUNS, 'full')
    if not ret:
        st.info('평가 결과 없음: python -m eval.run_eval 실행 후 표시')
        return
    section_headline(ret, full)
    section_retrieval(ret)
    section_by_type(ret)
    section_answers(full)
    section_scores(ret)
    section_kb()
    section_latency(ret, full)
