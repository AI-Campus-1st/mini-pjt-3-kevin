"""
관련도 판정
- 뉴스: R4 규칙 (EDA 확정) = 동의어 제목 포함 또는 (본문 2회 이상 and 1000자당 1회 이상)
- 보도자료: 제목 포함 또는 본문 2회 이상 (다주제 보도자료의 부수 언급 배제)
- 수동 자료·법령·의안: 선별된 1차 자료이므로 전부 포함
- 블로그·카페: 제목·요약문에 동의어가 있고 요약문 30자 이상인 글만 포함
"""
import re

import config

KEYS = re.compile(config.NEWS_KEYS)


def key_count(text: str) -> int:
    return len(KEYS.findall(text or ''))


def is_relevant(doc_type: str, collect_method: str, title: str, body: str) -> tuple[bool, str]:
    """(포함 여부, 적용 규칙명)"""
    in_title = bool(KEYS.search(title or ''))
    n = key_count(body)
    if doc_type == 'news':
        density = n / max(len(body) / 1000, 0.3)
        ok = in_title or (n >= config.RELEVANCE_MIN_COUNT and density >= config.RELEVANCE_MIN_DENSITY_PER_1K)
        return ok, 'R4'
    if doc_type == 'press_release':
        return in_title or n >= config.PRESS_RELEASE_MIN_COUNT, 'PR2'
    if doc_type in ('blog', 'cafe'):
        # 검색 API는 검색어 일부만 일치해도 반환 → 제목·요약문에 동의어가 있는 글만 포함
        return len(body) >= 30 and (in_title or n >= 1), 'snippet_kw'
    return True, 'primary'
