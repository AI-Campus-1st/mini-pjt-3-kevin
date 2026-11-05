"""
텍스트 정제
- 뉴스 꼬리말 보일러플레이트 절단 (EDA §2: "기사 섹션 분류 안내" 19.8%, 제보·구독 안내)
- PDF 반복 머리글·꼬리말·쪽번호 제거
- 띄어쓰기가 소실된 PDF 텍스트(재의요구서·체계자구 검토보고 등)의 띄어쓰기 복원
"""
import re
from collections import Counter

NEWS_TAIL_PATTERNS = [
    r'기사를 더 보고 싶다면\?.*$',
    r'[A-Za-z가-힣 ]{0,15}는 여러분의 제보를 기다리고 있습니다.*$',
    r'제보하기\s+전화.*$',
    r"'제보' 검색.*$",
    r'[가-힣 ]{0,20}거리를 알려주세요\..*$',
    r'네이버,\s*유튜브에서 뉴스를 구독해주세요.*$',
]
NEWS_INLINE_PATTERNS = [
    r'ⓒ[^\n]{0,60}(무단\s*전재|재배포\s*금지)[^\n]{0,40}',
    r'Copyright\s*ⓒ?[^\n]{0,80}',
    r'<?저작권자[^\n]{0,80}>?',
    r'무단\s*전재\s*(및|·)?\s*재배포\s*금지',
]

_kiwi = None

# 띄어쓰기 복원 시 쪼개지지 않아야 하는 도메인 복합명사 (예: '지역 사랑 상품권'으로 분리되는 문제 방지)
DOMAIN_TERMS = [
    '지역사랑상품권', '지역화폐', '지역상품권', '온누리상품권', '행정안전부', '기획재정부', '지방자치단체',
    '지방재정법', '국가재정법', '보조금관리법', '재의요구서', '재의요구', '예산편성권', '예산요구서',
    '인구감소지역', '행정안전위원회', '법제사법위원회', '전문위원', '판매대행점', '환전대행가맹점',
    '개별가맹점', '자치사무', '국가사무', '권력분립', '지방소멸', '지역균형발전', '일부개정법률안',
]


def normalize_ws(text: str) -> str:
    text = text.replace(' ', ' ').replace('\r', '\n')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r'\n\s*\n+', '\n\n', text)
    return text.strip()


def clean_news(body: str) -> str:
    text = body
    for p in NEWS_TAIL_PATTERNS:
        text = re.sub(p, '', text, flags=re.S)
    for p in NEWS_INLINE_PATTERNS:
        text = re.sub(p, '', text)
    return normalize_ws(text)


def space_ratio(text: str) -> float:
    hangul = len(re.findall(r'[가-힣]', text))
    return text.count(' ') / hangul if hangul else 1.0


def restore_spacing(text: str) -> str:
    """한글 대비 공백 비율이 매우 낮은 텍스트만 kiwipiepy로 띄어쓰기 복원"""
    global _kiwi
    if space_ratio(text) >= 0.08:
        return text
    if _kiwi is None:
        from kiwipiepy import Kiwi
        _kiwi = Kiwi()
        for term in DOMAIN_TERMS:
            _kiwi.add_user_word(term, 'NNP', 10)
    return '\n'.join(_kiwi.space(line) if line.strip() else line for line in text.split('\n'))


PAGE_NO_LINE = re.compile(r'\s*[-–]?\s*\d{1,3}\s*[-–]?\s*')
EDGE = 3  # 머리글·꼬리말 판정 대상: 각 쪽의 처음·마지막 3줄


def strip_repeated_lines(pages: list[str], min_ratio: float = 0.3) -> list[str]:
    """쪽 가장자리(처음·마지막 3줄)에서 여러 쪽에 반복되는 짧은 줄과 쪽번호 줄 제거
    - 표 안의 반복 수치가 지워지지 않도록 본문 중간 줄은 대상에서 제외"""
    norm = lambda l: re.sub(r'\d+', '#', l.strip())
    split = [[l for l in p.split('\n')] for p in pages]
    counts = Counter()
    for lines in split:
        edge = {norm(l) for l in lines[:EDGE] + lines[-EDGE:] if 0 < len(l.strip()) < 60}
        counts.update(edge)
    repeated = {k for k, c in counts.items() if len(pages) >= 3 and c / len(pages) >= min_ratio}
    out = []
    for lines in split:
        n = len(lines)
        keep = []
        for i, l in enumerate(lines):
            at_edge = i < EDGE or i >= n - EDGE
            if at_edge and (norm(l) in repeated or PAGE_NO_LINE.fullmatch(l)):
                continue
            keep.append(l)
        out.append('\n'.join(keep))
    return out


def clean_sections(sections: list[dict], is_pdf: bool) -> list[dict]:
    if is_pdf:
        texts = strip_repeated_lines([s['text'] for s in sections])
        sections = [{**s, 'text': t} for s, t in zip(sections, texts)]
    out = []
    for s in sections:
        t = normalize_ws(restore_spacing(normalize_ws(s['text'])))
        if t:
            out.append({**s, 'text': t})
    return out
