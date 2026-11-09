"""
EDA 심화 통계 (리포트 2-1-1 근거)
- 대상: store/docs.db의 뉴스 문서 전체(제공 CSV 적재분 36,887건)와 그중 지식베이스 포함분(included = 1)
- 분석 1 시계열: 월별 건수의 변동계수·자기상관·강건 z 피크, 연도별 일평균과 Kruskal-Wallis 검정, 요일 분포, 보도자료 발행 월과의 상관
- 분석 2 키워드 언급: 본문 언급 횟수·밀도 분포, R4 기준 영역별 비중, 연도별 관련도 통과율과 카이제곱 검정
- 분석 3 중복 구조: near-dup 클러스터 크기, MinHash 유사도, 재배포 시차, 통신사 여부와 중복률의 관계
- 실행: 프로젝트 루트에서 `python notebooks/eda_stats.py`
- 산출: data/interim/eda_stats.json, report/images/05~08_eda_*.png
"""
import json
import re
import sqlite3
import sys
from pathlib import Path

import matplotlib

matplotlib.use('Agg')
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from matplotlib.ticker import FuncFormatter, PercentFormatter
from scipy import stats

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import config  # noqa: E402

IMG_DIR = ROOT / 'report' / 'images'
OUT_JSON = config.INTERIM_DIR / 'eda_stats.json'

KEYS = re.compile(config.NEWS_KEYS)
WIRE = {'뉴시스', '뉴스1', '연합뉴스'}
ELECTION = re.compile(r'후보|공약|선거|출마')
# 관련도 판정을 통과하여 중복 처리 대상이 된 문서의 exclude_reason
DEDUP_SCOPE = {'', 'exact_dup', 'near_dup', 'too_short'}
FULL_MONTHS = ('2021-11', '2026-09')  # 수집 시작·종료 월(10/16 기준)은 부분 월이므로 제외

# 차트 색상: dataviz 참조 팔레트(라이트 모드) 슬롯 1~2와 중립 잉크
C1, C2 = '#2a78d6', '#eb6834'
INK, INK2, MUTED = '#0b0b0b', '#52514e', '#898781'
GRID, AXIS, SURFACE = '#e1e0d9', '#c3c2b7', '#fcfcfb'
SEQ = ['#cde2fb', '#9ec5f4', '#6da7ec', '#3987e5', '#256abf', '#184f95', '#0d366b']

plt.rcParams.update({
    'font.family': 'Malgun Gothic',
    'axes.unicode_minus': False,
    'figure.facecolor': SURFACE,
    'axes.facecolor': SURFACE,
    'savefig.facecolor': SURFACE,
    'axes.edgecolor': AXIS,
    'axes.labelcolor': INK2,
    'axes.titlecolor': INK,
    'axes.titlesize': 12,
    'axes.titleweight': 'bold',
    'axes.titlelocation': 'left',
    'axes.labelsize': 10,
    'axes.spines.top': False,
    'axes.spines.right': False,
    'axes.grid': True,
    'grid.color': GRID,
    'grid.linewidth': 0.6,
    'axes.axisbelow': True,
    'xtick.color': MUTED,
    'ytick.color': MUTED,
    'xtick.labelcolor': INK2,
    'ytick.labelcolor': INK2,
    'legend.frameon': False,
    'legend.labelcolor': INK2,
    'font.size': 10,
})

thousands = FuncFormatter(lambda v, _: f'{v:,.0f}')


def load() -> pd.DataFrame:
    con = sqlite3.connect(config.DOCS_DB_PATH)
    df = pd.read_sql(
        "select doc_id, title, body, body_len, publisher, published_ts, included, "
        "exclude_reason, dup_cluster_id from documents where doc_type = 'news'", con)
    pr = pd.read_sql(
        "select published_at from documents where doc_type = 'press_release' and included = 1", con)
    con.close()
    df['ts'] = pd.to_datetime(df['published_ts'])
    df['year'] = df['ts'].dt.year
    df['month'] = df['ts'].dt.to_period('M')
    df['title'] = df['title'].fillna('')
    df['body'] = df['body'].fillna('')
    df['n_kw'] = df['body'].map(lambda b: len(KEYS.findall(b)))
    df['in_title'] = df['title'].str.contains(KEYS)
    # relevance.is_relevant와 같은 밀도 정의 (짧은 본문은 300자로 하한)
    df['density'] = df['n_kw'] / np.maximum(df['body'].str.len() / 1000, 0.3)
    df['wire'] = df['publisher'].isin(WIRE)
    pr['month'] = pd.to_datetime(pr['published_at']).dt.to_period('M')
    return df, pr


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * np.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return c - h, c + h


def bootstrap_mean_ci(x: np.ndarray, n_boot: int = 5000, seed: int = 0) -> tuple[float, float]:
    rng = np.random.default_rng(seed)
    means = rng.choice(x, size=(n_boot, len(x)), replace=True).mean(axis=1)
    return float(np.percentile(means, 2.5)), float(np.percentile(means, 97.5))


# ---------------------------------------------------------------- 분석 1 시계열
def time_series(df: pd.DataFrame, pr: pd.DataFrame) -> dict:
    lo, hi = FULL_MONTHS
    raw_m = df.groupby('month').size().loc[lo:hi]
    kb_m = df[df['included'] == 1].groupby('month').size().reindex(raw_m.index, fill_value=0)

    # 로그 건수의 중앙값·MAD 기반 강건 z (modified z-score)
    lg = np.log(raw_m)
    mad = np.median(np.abs(lg - np.median(lg)))
    rz = 0.6745 * (lg - np.median(lg)) / mad
    x = raw_m.values - raw_m.values.mean()
    acf = {k: float(np.corrcoef(x[:-k], x[k:])[0, 1]) for k in (1, 2, 3, 6, 12)}

    # 연도별 일평균: 2021·2026은 부분 연도이므로 연간 합계 대신 일평균으로 비교
    daily = df.set_index('ts').resample('D').size().to_frame('n')
    daily['year'] = daily.index.year
    by_year = {}
    for y, g in daily.groupby('year'):
        ci = bootstrap_mean_ci(g['n'].values)
        by_year[int(y)] = {'days': int(len(g)), 'mean': float(g['n'].mean()),
                           'median': float(g['n'].median()), 'ci95': ci}
    kw = stats.kruskal(*[g['n'].values for _, g in daily.groupby('year')])

    wd = df['ts'].dt.day_of_week.value_counts().sort_index()
    wd_chi = stats.chisquare(wd.values)

    pm = pr.groupby('month').size().reindex(raw_m.index, fill_value=0)
    sp = stats.spearmanr(raw_m.values, pm.values)
    mw = stats.mannwhitneyu(raw_m[pm > 0], raw_m[pm == 0])

    res = {
        'months': len(raw_m),
        'monthly_mean': float(raw_m.mean()), 'monthly_std': float(raw_m.std()),
        'monthly_cv': float(raw_m.std() / raw_m.mean()),
        'monthly_min': [str(raw_m.idxmin()), int(raw_m.min())],
        'monthly_max': [str(raw_m.idxmax()), int(raw_m.max())],
        'kb_monthly_cv': float(kb_m.std() / kb_m.mean()),
        'raw_kb_pearson': float(np.corrcoef(raw_m, kb_m)[0, 1]),
        'acf': acf,
        'robust_z_top': {str(k): round(float(v), 2) for k, v in rz.sort_values(ascending=False).head(8).items()},
        'robust_z_bottom': {str(k): round(float(v), 2) for k, v in rz.sort_values().head(5).items()},
        'peaks_z_ge_1': [str(k) for k, v in rz.items() if v >= 1.0],
        'daily_by_year': by_year,
        'kruskal_daily_by_year': {'H': float(kw.statistic), 'p': float(kw.pvalue)},
        'weekday_counts': {int(k): int(v) for k, v in wd.items()},
        'weekday_chisq': {'chi2': float(wd_chi.statistic), 'p': float(wd_chi.pvalue)},
        'press_release_months': int((pm > 0).sum()),
        'spearman_news_vs_pr': {'rho': float(sp.statistic), 'p': float(sp.pvalue)},
        'mannwhitney_pr_month': {'U': float(mw.statistic), 'p': float(mw.pvalue),
                                 'median_with_pr': float(raw_m[pm > 0].median()),
                                 'median_without_pr': float(raw_m[pm == 0].median())},
    }
    plot_monthly(raw_m, kb_m, rz)
    return res


def plot_monthly(raw_m: pd.Series, kb_m: pd.Series, rz: pd.Series) -> None:
    fig, ax = plt.subplots(figsize=(11, 4.4))
    xs = raw_m.index.to_timestamp()
    ax.plot(xs, raw_m.values, color=C1, lw=2, label='원본 뉴스')
    ax.plot(xs, kb_m.values, color=C2, lw=2, label='지식베이스 포함')
    ax.axhline(raw_m.median(), color=AXIS, lw=1)
    ax.text(xs[30], raw_m.median() + 25, f'원본 월 중앙값 {raw_m.median():,.0f}', color=MUTED, fontsize=9,
            ha='center', va='bottom')

    labels = {
        '2021-11': '2022 예산 공방', '2022-12': '국비 삭감 논란', '2025-01': '법 재발의',
        '2025-06': '소비쿠폰 논의', '2025-07': '추경·지급 개시', '2026-03': '추경 논의',
    }
    # 라벨 위치 (x 오프셋, y 오프셋, 정렬): 2021-11은 왼쪽 끝, 2025-07은 2025-06 라벨과 겹치지 않도록 오른쪽 배치
    place = {'2021-11': (-4, 9, 'left'), '2025-07': (10, -4, 'left')}
    for m, txt in labels.items():
        p = pd.Period(m, 'M')
        x, y = p.to_timestamp(), raw_m.loc[p]
        dx, dy, ha = place.get(m, (0, 9, 'center'))
        ax.scatter([x], [y], s=36, color=C1, edgecolor=SURFACE, linewidth=1.5, zorder=3)
        ax.annotate(f'{m}  {txt}\nz={rz.loc[p]:.1f}', (x, y), xytext=(dx, dy), textcoords='offset points',
                    ha=ha, va='bottom' if dy > 0 else 'top', fontsize=8.5, color=INK2)
    ax.set_ylim(0, raw_m.max() * 1.25)
    ax.yaxis.set_major_formatter(thousands)
    ax.set_ylabel('월별 기사 수')
    ax.set_title('월별 기사 수 추이와 피크 월 (2021-11 ~ 2026-09, 강건 z ≥ 1.0 표시)')
    ax.legend(loc='upper left', ncol=2)
    fig.tight_layout()
    fig.savefig(IMG_DIR / '05_eda_monthly.png', dpi=150)
    plt.close(fig)


# ---------------------------------------------------------------- 분석 2 키워드 언급
def keyword(df: pd.DataFrame) -> dict:
    n = len(df)
    no_title = ~df['in_title']
    cond_n = df['n_kw'] >= config.RELEVANCE_MIN_COUNT
    cond_d = df['density'] >= config.RELEVANCE_MIN_DENSITY_PER_1K
    regions = {
        'title': int(df['in_title'].sum()),
        'body_pass': int((no_title & cond_n & cond_d).sum()),
        'n_ge2_low_density': int((no_title & cond_n & ~cond_d).sum()),
        'n_eq1': int((no_title & (df['n_kw'] == 1)).sum()),
        'n_eq0': int((no_title & (df['n_kw'] == 0)).sum()),
    }
    sp = stats.spearmanr(df['body_len'], df['n_kw'])

    # 연도별 관련도 통과율 (R4 판정 = exclude_reason이 irrelevant·empty_body가 아닌 문서)
    df = df.assign(rel=~df['exclude_reason'].isin(['irrelevant', 'empty_body']))
    tab = pd.crosstab(df['year'], df['rel'])
    chi = stats.chi2_contingency(tab.values)
    rel_by_year = {}
    for y, row in tab.iterrows():
        k, t = int(row[True]), int(row.sum())
        rel_by_year[int(y)] = {'n': t, 'rate': k / t, 'ci95': wilson(k, t)}
    kb_by_year = (df.groupby('year')['included'].mean()).round(4).to_dict()

    # 2026년 통과율 하락 점검: 선거 관련 제목 비중과 그 문서의 통과율
    df['elec'] = df['title'].str.contains(ELECTION)
    elec = {}
    for y, g in df.groupby('year'):
        e = g[g['elec']]
        ne = g[~g['elec']]
        elec[int(y)] = {'share': float(g['elec'].mean()), 'rel_rate_elec': float(e['rel'].mean()),
                        'rel_rate_non_elec': float(ne['rel'].mean())}
    term_share = {t: df.groupby('year')['body'].apply(lambda s: float(s.str.contains(t).mean())).to_dict()
                  for t in ['지역화폐', '지역사랑상품권', '지역상품권']}
    n_by_year = df.groupby('year')['n_kw'].agg(['mean', 'median']).round(3)

    res = {
        'n_docs': n,
        'kw_count_dist': {int(k): int(v) for k, v in df['n_kw'].clip(upper=10).value_counts().sort_index().items()},
        'share_n0': float((df['n_kw'] == 0).mean()), 'share_n1': float((df['n_kw'] == 1).mean()),
        'share_n_ge2': float((df['n_kw'] >= 2).mean()),
        'r4_regions': regions,
        'spearman_len_vs_count': {'rho': float(sp.statistic), 'p': float(sp.pvalue)},
        'density_median_included': float(df.loc[df['included'] == 1, 'density'].median()),
        'relevance_by_year': rel_by_year,
        'relevance_chisq': {'chi2': float(chi[0]), 'p': float(chi[1]), 'dof': int(chi[2])},
        'kb_rate_by_year': {int(k): float(v) for k, v in kb_by_year.items()},
        'election_title': elec,
        'term_share_by_year': {t: {int(k): round(v, 4) for k, v in d.items()} for t, d in term_share.items()},
        'kw_count_by_year': {int(y): r.to_dict() for y, r in n_by_year.iterrows()},
    }
    plot_keyword(df, res)
    return res


def plot_keyword(df: pd.DataFrame, res: dict) -> None:
    fig, (a1, a2) = plt.subplots(1, 2, figsize=(12, 4.6), gridspec_kw={'width_ratios': [1, 1.15]})

    # (a) 언급 횟수 분포: 관련도 통과·탈락 누적
    cnt = df['n_kw'].clip(upper=10)
    tab = pd.crosstab(cnt, df['rel']).reindex(range(0, 11), fill_value=0)
    xs = np.arange(11)
    a1.bar(xs, tab[True], width=0.72, color=C1, label='관련도 통과 (R4)')
    a1.bar(xs, tab[False], width=0.72, bottom=tab[True], color=C2, label='관련도 탈락',
           edgecolor=SURFACE, linewidth=1.5)
    a1.set_xticks(xs, [str(i) for i in range(10)] + ['10+'])
    a1.yaxis.set_major_formatter(thousands)
    a1.set_xlabel('본문 내 키워드(동의어) 언급 횟수')
    a1.set_ylabel('기사 수')
    a1.set_title('(a) 키워드 언급 횟수 분포')
    tot1 = int(tab.loc[1].sum())
    a1.annotate(f'1회 {tot1:,}건 ({res["share_n1"]:.1%})\n통과분은 제목 포함 기사', (1, tot1),
                xytext=(14, -4), textcoords='offset points', va='top', fontsize=9, color=INK2)
    a1.legend(loc='upper right')
    a1.grid(axis='x', visible=False)

    # (b) 제목 미포함 기사의 본문 길이 × 언급 횟수, R4 통과 영역
    d = df[~df['in_title'] & (df['n_kw'] > 0) & (df['body_len'] >= 100)]
    from matplotlib.colors import ListedColormap, LogNorm
    hb = a2.hexbin(d['body_len'], d['n_kw'].clip(upper=30), xscale='log', gridsize=(42, 30),
                   cmap=ListedColormap(SEQ), norm=LogNorm(), mincnt=1, linewidths=0.2, edgecolors=SURFACE)
    lx = np.logspace(2, np.log10(d['body_len'].max()), 200)
    border = np.maximum(2, lx / 1000)
    a2.plot(lx, border, color=INK, lw=1.4, label='R4 경계: n = max(2, 길이/1000)')
    a2.fill_between(lx, border, 30.5, color=C1, alpha=0.06, lw=0)
    a2.text(160, 24, 'R4 통과 영역\n(2회 이상 and 1000자당 1회 이상)', fontsize=9, color=INK2)
    a2.legend(loc='upper left', bbox_to_anchor=(0.0, 0.80))
    a2.set_ylim(0.5, 30.5)
    a2.set_xlabel('본문 길이 (자, 로그 축)')
    a2.set_ylabel('언급 횟수 (30 이상은 30)')
    a2.set_title('(b) 제목 미포함 기사의 길이·언급 횟수와 R4 경계')
    a2.xaxis.set_major_formatter(thousands)
    cb = fig.colorbar(hb, ax=a2, pad=0.015)
    cb.set_label('기사 수 (로그)', color=INK2)
    cb.outline.set_visible(False)
    cb.ax.tick_params(colors=MUTED, labelcolor=INK2)
    fig.tight_layout()
    fig.savefig(IMG_DIR / '06_eda_keyword.png', dpi=150)
    plt.close(fig)

    # (c) 연도별 관련도 통과율과 선거 관련 제목 비중 (서로 다른 지표이므로 별도 패널)
    fig, (b1, b2) = plt.subplots(1, 2, figsize=(11, 3.8))
    years = sorted(res['relevance_by_year'])
    rates = [res['relevance_by_year'][y]['rate'] for y in years]
    lo = [res['relevance_by_year'][y]['rate'] - res['relevance_by_year'][y]['ci95'][0] for y in years]
    hi = [res['relevance_by_year'][y]['ci95'][1] - res['relevance_by_year'][y]['rate'] for y in years]
    b1.errorbar(years, rates, yerr=[lo, hi], color=C1, lw=2, marker='o', ms=7, mec=SURFACE, mew=1.5,
                ecolor=MUTED, elinewidth=1, capsize=3)
    for y, r in zip(years, rates):
        b1.annotate(f'{r:.1%}', (y, r), xytext=(9, 0), textcoords='offset points', ha='left', va='center',
                    fontsize=9, color=INK2)
    b1.set_ylim(0.40, 0.60)
    b1.yaxis.set_major_formatter(PercentFormatter(1, decimals=0))
    b1.set_title('(a) 연도별 관련도(R4) 통과율, 95% Wilson 구간')
    b1.set_xticks(years)
    b1.grid(axis='x', visible=False)

    sh = [res['election_title'][y]['share'] for y in years]
    b2.bar(years, sh, width=0.6, color=C1)
    for y, s in zip(years, sh):
        b2.annotate(f'{s:.1%}', (y, s), xytext=(0, 4), textcoords='offset points', ha='center', fontsize=9, color=INK2)
    b2.yaxis.set_major_formatter(PercentFormatter(1, decimals=0))
    b2.set_ylim(0, max(sh) * 1.25)
    b2.set_title('(b) 제목에 선거 관련어(후보·공약·선거·출마)가 있는 기사 비중')
    b2.set_xticks(years)
    b2.grid(axis='x', visible=False)
    fig.tight_layout()
    fig.savefig(IMG_DIR / '07_eda_relevance_year.png', dpi=150)
    plt.close(fig)


# ---------------------------------------------------------------- 분석 3 중복 구조
def duplicates(df: pd.DataFrame) -> dict:
    con = sqlite3.connect(config.DOCS_DB_PATH)
    log = pd.read_sql(
        "select l.method, l.similarity from dedup_log l join documents d on d.doc_id = l.doc_id "
        "where d.doc_type = 'news'", con)
    con.close()

    scope = df[df['exclude_reason'].isin(DEDUP_SCOPE)].copy()
    size = scope.groupby('dup_cluster_id').size()
    scope['csize'] = scope['dup_cluster_id'].map(size)
    multi = scope[scope['csize'] > 1].copy()
    g = multi.groupby('dup_cluster_id')
    first = g['ts'].transform('min')
    lag_h = (multi['ts'] - first).dt.total_seconds() / 3600
    lag_h = lag_h[multi['ts'] != first]  # 클러스터 최초 기사 제외
    span_h = g['ts'].agg(lambda s: (s.max() - s.min()).total_seconds() / 3600)
    npub = g['publisher'].nunique()

    scope['is_dup'] = scope['exclude_reason'].isin(['exact_dup', 'near_dup'])
    tab = pd.crosstab(scope['wire'], scope['is_dup'])
    chi = stats.chi2_contingency(tab.values)
    sim = log.loc[log['method'] == 'minhash', 'similarity']

    reps = scope[scope['doc_id'] == scope['dup_cluster_id']]
    top = size.sort_values(ascending=False).head(5)
    titles = df.set_index('doc_id')['title']

    res = {
        'scope_docs': int(len(scope)),
        'clusters': int(len(size)),
        'cluster_size_dist': {int(k): int(v) for k, v in size.value_counts().sort_index().items()},
        'multi_clusters': int((size > 1).sum()),
        'docs_in_multi': int(size[size > 1].sum()),
        'removed': {k: int(v) for k, v in log['method'].value_counts().items()},
        'minhash_similarity': {q: float(sim.quantile(q)) for q in (0.0, 0.25, 0.5, 0.75, 1.0)},
        'minhash_sim_share_lt_0_8': float((sim < 0.8).mean()),
        'lag_hours': {q: float(lag_h.quantile(q)) for q in (0.25, 0.5, 0.75, 0.9, 0.95)},
        'lag_share_le_1h': float((lag_h <= 1).mean()), 'lag_share_le_24h': float((lag_h <= 24).mean()),
        'lag_gt_7d': int((lag_h > 168).sum()),
        'span_median_h': float(span_h.median()),
        'publishers_per_cluster_median': float(npub.median()),
        'single_publisher_cluster_share': float((npub == 1).mean()),
        'cluster_with_wire_share': float(g['wire'].any().mean()),
        'rep_wire_share_multi': float(reps.loc[reps['csize'] > 1, 'wire'].mean()),
        'wire_share_scope': float(scope['wire'].mean()),
        'dup_rate_wire': float(tab.loc[True, True] / tab.loc[True].sum()),
        'dup_rate_non_wire': float(tab.loc[False, True] / tab.loc[False].sum()),
        'wire_dup_chisq': {'chi2': float(chi[0]), 'p': float(chi[1])},
        'top_clusters': [{'size': int(n), 'title': titles[c]} for c, n in top.items()],
    }
    plot_dup(size, sim, lag_h, res)
    return res


def plot_dup(size: pd.Series, sim: pd.Series, lag_h: pd.Series, res: dict) -> None:
    fig, (a1, a2, a3) = plt.subplots(1, 3, figsize=(13.5, 4.2))

    dist = size[size > 1].value_counts().sort_index()
    a1.bar(dist.index, dist.values, width=0.7, color=C1)
    a1.set_yscale('log')
    a1.yaxis.set_major_formatter(thousands)
    for k, v in dist.items():
        a1.annotate(f'{v:,}', (k, v), xytext=(0, 3), textcoords='offset points', ha='center', fontsize=8.5, color=INK2)
    a1.set_xticks(dist.index)
    a1.set_xlabel('클러스터 크기 (기사 수)')
    a1.set_ylabel('클러스터 수 (로그 축)')
    a1.set_title(f'(a) 중복 클러스터 크기 ({res["multi_clusters"]:,}개)')
    a1.grid(axis='x', visible=False)

    # num_perm = 64이므로 추정 Jaccard는 1/64 단위 이산값 → 구간 경계를 (k ± 0.5)/64에 맞춤
    bins = (np.arange(44, 65) + 0.5) / config.MINHASH_NUM_PERM
    hv, _, _ = a2.hist(sim, bins=bins, color=C1, edgecolor=SURFACE, linewidth=1.5)
    top = hv.max() * 1.22
    a2.set_ylim(0, top)
    a2.axvline(0.7, color=INK, lw=1.2)
    a2.text(0.705, top * 0.96, '임계값 0.7', fontsize=9, color=INK2, va='top')
    a2.axvline(sim.median(), color=MUTED, lw=1)
    a2.text(sim.median() + 0.005, top * 0.96, f'중앙값 {sim.median():.2f}', fontsize=9, color=INK2, va='top')
    a2.set_xlabel('MinHash Jaccard 유사도 (1/64 단위)')
    a2.set_ylabel('제거 기사 수')
    a2.set_title(f'(b) near-dup 제거 기사의 유사도 ({len(sim):,}건)')
    a2.grid(axis='x', visible=False)

    xs = np.sort(lag_h.clip(lower=1 / 60).values)
    ys = np.arange(1, len(xs) + 1) / len(xs)
    a3.step(xs, ys, where='post', color=C1, lw=2)
    a3.set_xscale('log')
    a3.set_xlim(1 / 60, xs.max() * 1.2)
    for h, lab, ty in ((1, '1시간', 0.12),(24, '24시간', 0.72), (168, '7일', 0.56)):
        share = (lag_h <= h).mean()
        a3.axvline(h, color=AXIS, lw=1)
        a3.text(h * 1.15, ty, f'{lab} 이내\n{share:.1%}', fontsize=9, color=INK2, va='center')
    a3.yaxis.set_major_formatter(PercentFormatter(1, decimals=0))
    a3.xaxis.set_major_formatter(FuncFormatter(lambda v, _: f'{v:g}'))
    a3.set_xlabel('클러스터 최초 기사 대비 시차 (시간, 로그 축)')
    a3.set_ylabel('누적 비율')
    a3.set_title(f'(c) 재게재 시차 누적분포 (중앙값 {lag_h.median():.1f}시간)')
    fig.tight_layout()
    fig.savefig(IMG_DIR / '08_eda_duplicates.png', dpi=150)
    plt.close(fig)


def main() -> None:
    IMG_DIR.mkdir(parents=True, exist_ok=True)
    df, pr = load()
    df['rel'] = ~df['exclude_reason'].isin(['irrelevant', 'empty_body'])
    out = {
        'n_news': int(len(df)),
        'n_kb': int(df['included'].sum()),
        'time_series': time_series(df, pr),
        'keyword': keyword(df),
        'duplicates': duplicates(df),
    }
    OUT_JSON.write_text(json.dumps(out, ensure_ascii=False, indent=2, default=str), encoding='utf-8')
    print(json.dumps(out, ensure_ascii=False, indent=2, default=str))


if __name__ == '__main__':
    main()
