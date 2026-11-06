"""
프로젝트 단일 설정원
- 경로, 수집 키워드, 전처리 기준, 청킹 프리셋, 모델명을 한곳에서 관리
- 수치의 근거: docs/eda_findings.md, docs/masterplan.md
"""
import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / '.env')

# 경로
DATA_DIR = ROOT / 'data'
PROVIDED_DIR = DATA_DIR / 'news_csv_5y'
PROVIDED_PREFIX = '05_공공정책_키워드_지역화폐'
RAW_DIR = DATA_DIR / 'raw'
REPORTS_DIR = RAW_DIR / 'reports'
INTERIM_DIR = DATA_DIR / 'interim'
MANIFEST_PATH = DATA_DIR / 'manifest.jsonl'
MANUAL_SOURCES_PATH = DATA_DIR / 'manual_sources.csv'
COLLECT_LOG_PATH = RAW_DIR / 'collect_log.jsonl'
STORE_DIR = ROOT / 'store'
DOCS_DB_PATH = STORE_DIR / 'docs.db'
CHROMA_DIR = STORE_DIR / 'chroma'

# 수집 공통
USER_AGENT = 'Mozilla/5.0 (compatible; mini-project-rag-collector; education)'
REQUEST_DELAY_SEC = 1.5
COLLECT_START_DATE = '2021-10-16'

# 정책브리핑 보도자료 검색어
KOREA_KR_KEYWORDS = ['지역사랑상품권', '지역화폐']

# 법령 Open API 검색어
LAW_QUERY = '지역사랑상품권'

# 열린국회정보 (국회의원 발의법률안)
ASSEMBLY_SERVICE = 'nzmimeepazxkubdpn'
ASSEMBLY_AGES = [21, 22]
ASSEMBLY_BILL_NAME = '지역사랑상품권'

# 네이버 API HUB 검색 (블로그·카페 요약문)
NAVER_ENDPOINT = 'https://naverapihub.apigw.ntruss.com/search/v1/{kind}'
NAVER_KINDS = ['blog', 'cafearticle']
NAVER_QUERIES = [
    '지역화폐 사용처',
    '지역화폐 불편',
    '지역화폐 가맹점',
    '지역화폐 캐시백',
    '지역사랑상품권 사용처',
    '지역사랑상품권 후기',
    '지역사랑상품권 할인 구매',
    '지역사랑상품권 부정유통',
    '경기지역화폐 사용',
    '동백전 캐시백',
]
NAVER_DISPLAY = 100
NAVER_MAX_START = 1000
NAVER_DAILY_LIMIT = 25000
# 지식베이스 포함 상한: 검색어별 정확도순 상위 N건
# 카페는 200~500건 목표(masterplan §5-2)에 맞춘 50건, 블로그는 블로그:카페 = 80:20 목표에 맞춘 220건
SNIPPET_TOP_PER_QUERY = {'blog': 220, 'cafe': 50}

# 전처리 기준 (EDA 확정값)
NEWS_KEYS = r'지역화폐|지역사랑상품권|지역상품권'
RELEVANCE_MIN_COUNT = 2
RELEVANCE_MIN_DENSITY_PER_1K = 1.0
PRIMARY_MIN_COUNT = 1
NEARDUP_THRESHOLD = 0.7
MINHASH_NUM_PERM = 64
MINHASH_SHINGLE = 5
MIN_BODY_LEN = 200
# 지식베이스에서 제외하는 문서 종류 (수집은 유지, 인덱싱 단계에서 exclude_reason = type_excluded)
# cafe: 네이버 카페 검색 API가 게시일을 제공하지 않아 발행일 기록 요건(가이드 7장) 미충족 (Day 4 사용자 결정)
KB_EXCLUDE_TYPES = {'cafe'}

# 문서 종류 → 신뢰도 등급 (data_spec §2-3)
DOC_TYPE_TIER = {
    'press_release': 1, 'law': 1, 'bill': 1, 'budget': 1, 'local_notice': 1, 'guideline': 1,
    'review_report': 2, 'minutes': 2, 'veto': 2, 'report': 2,
    'news': 3,
    'blog': 4, 'cafe': 4,
}
DOC_TYPE_LABEL = {
    'press_release': '보도자료', 'law': '법령', 'bill': '의안', 'budget': '예산자료',
    'local_notice': '지자체공고', 'guideline': '업무지침', 'review_report': '검토보고서', 'minutes': '회의록',
    'veto': '재의요구서', 'report': '연구보고서', 'news': '뉴스', 'blog': '블로그', 'cafe': '카페글',
}

# 청킹 프리셋 (masterplan §5-4)
CHUNK_PRESETS = {
    'A': {'mode': 'fixed', 'size': 500, 'overlap': 100},
    'B': {'mode': 'structured', 'news_whole_max': 1200, 'size': 800, 'overlap': 150},
}

# 임베딩·생성
EMBEDDING_MODEL = os.getenv('EMBEDDING_MODEL', 'text-embedding-3-small')
EMBEDDING_ALIAS = {'text-embedding-3-small': 'te3s', 'text-embedding-3-large': 'te3l'}
LLM_MODEL = os.getenv('LLM_MODEL', 'gpt-6-luna')
# gpt-6-luna는 temperature 기본값(1)만 지원 → None이면 호출 시 인자 생략
LLM_TEMPERATURE = None
# 추론 강도: None이면 모델 기본값 사용 (Day 4 결정), 질의 재작성은 속도 우선 low
LLM_REASONING_EFFORT = None
REWRITE_REASONING_EFFORT = 'low'
TOP_K = 5
# 유사도 임계값: None이면 미적용 (E0 baseline 정의, E3에서 값 결정)
SCORE_THRESHOLD = None
PROMPT_VERSION = 'v1'
# 생성 단계에 전달하는 이전 대화 턴 수, 이전 답변 최대 길이
HISTORY_TURNS = 3
HISTORY_ANSWER_MAX_CHARS = 800
# 답변 품질 평가 시 문항당 생성 횟수 (temperature 고정 불가 대응, 다수결 판정)
EVAL_GENERATIONS = 3


def collection_name(chunk_config: str, title_prefix: bool = True, model: str = EMBEDDING_MODEL) -> str:
    """Chroma 컬렉션명 규칙 (data_spec §5-1)"""
    name = f'kb_{chunk_config}_{EMBEDDING_ALIAS.get(model, "emb")}'
    return name if title_prefix else f'{name}_notitle'


DEFAULT_COLLECTION = collection_name('A', True)
