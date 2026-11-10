"""
수집 계층 공통 저장 유틸
- doc_id 생성 (data_spec §2-4), manifest 기록 (§3-2), 수집 로그 기록
- 수집 계층은 원문 DB·벡터 DB에 쓰지 않음 (계층 분리)
"""
import hashlib
import json
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

import config

KST = timezone(timedelta(hours=9))


def now_kst() -> str:
    """수집 시각 (ISO 8601, KST 오프셋 포함)"""
    return datetime.now(KST).isoformat(timespec='seconds')


def mtime_kst(path: Path) -> str:
    """파일 수정 시각을 KST ISO 문자열로 변환"""
    return datetime.fromtimestamp(path.stat().st_mtime, KST).isoformat(timespec='seconds')


def date_part(published_at: str) -> str:
    """doc_id 날짜부: YYYY-MM-DD → YYYYMMDD, YYYY-MM → YYYYMM00, 미상 → 00000000"""
    digits = re.sub(r'\D', '', published_at or '')
    if len(digits) >= 8:
        return digits[:8]
    if len(digits) == 6:
        return digits + '00'
    return '00000000'


def make_doc_id(source: str, published_at: str, canonical_key: str) -> str:
    h = hashlib.sha1(canonical_key.encode('utf-8')).hexdigest()[:8]
    return f'{source}_{date_part(published_at)}_{h}'


def file_sha1(path: Path) -> str:
    h = hashlib.sha1()
    with open(path, 'rb') as f:
        for block in iter(lambda: f.read(1 << 20), b''):
            h.update(block)
    return h.hexdigest()


def rel_path(path: Path) -> str:
    """저장소 루트 기준 상대 경로 (슬래시 구분)"""
    return Path(path).resolve().relative_to(config.ROOT).as_posix()


class Manifest:
    """data/manifest.jsonl 추가 기록기. 이미 등록된 doc_id는 다시 기록하지 않음"""

    def __init__(self, path: Path = config.MANIFEST_PATH):
        self.path = path
        self.entries: dict[str, dict] = {}
        if path.exists():
            with open(path, encoding='utf-8') as f:
                for line in f:
                    if line.strip():
                        e = json.loads(line)
                        self.entries[e['doc_id']] = e

    def __contains__(self, doc_id: str) -> bool:
        return doc_id in self.entries

    def missing_raw(self, source: str) -> list[dict]:
        """등록되어 있으나 원본 파일이 없는 항목
        - manifest는 git 추적, data/raw/는 git 제외이므로 새 환경에서는 등록 문서의 원본이 없음 → 수집기가 원본만 복원"""
        return [e for e in self.entries.values()
                if e['source'] == source and not (config.ROOT / e['raw_path']).exists()]

    def find_key(self, canonical_key: str) -> dict | None:
        """canonical_key가 같은 등록 항목 (doc_id 날짜부가 달라져도 같은 문서로 취급해야 하는 소스용)"""
        return next((e for e in self.entries.values() if e['canonical_key'] == canonical_key), None)

    def add(self, entry: dict) -> bool:
        required = ['doc_id', 'source', 'doc_type', 'canonical_key', 'title', 'publisher',
                    'published_at', 'collected_at', 'collect_method', 'raw_path']
        missing = [k for k in required if k not in entry]
        if missing:
            raise ValueError(f'manifest 필수 필드 누락: {missing}')
        if entry['doc_id'] in self.entries:
            return False
        entry.setdefault('url', '')
        entry.setdefault('citation', '')
        entry.setdefault('page_range', '')
        entry.setdefault('attachments', [])
        entry.setdefault('license', '')
        entry.setdefault('note', '')
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, 'a', encoding='utf-8') as f:
            f.write(json.dumps(entry, ensure_ascii=False) + '\n')
        self.entries[entry['doc_id']] = entry
        return True


def log_collect(source: str, url: str, status: str, http_status: int = 0, message: str = '') -> None:
    """수집 로그 (data/raw/collect_log.jsonl). 인덱서가 docs.db collect_log 테이블로 적재"""
    config.COLLECT_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    rec = {'source': source, 'url': url, 'status': status, 'http_status': http_status,
           'collected_at': now_kst(), 'message': message}
    with open(config.COLLECT_LOG_PATH, 'a', encoding='utf-8') as f:
        f.write(json.dumps(rec, ensure_ascii=False) + '\n')


class PoliteSession:
    """요청 간 지연을 강제하는 HTTP 세션 (병렬 요청 없음)"""

    def __init__(self, delay: float = config.REQUEST_DELAY_SEC):
        self.delay = delay
        self.session = requests.Session()
        self.session.headers['User-Agent'] = config.USER_AGENT
        self._last = 0.0

    def get(self, url: str, **kwargs) -> requests.Response:
        wait = self.delay - (time.time() - self._last)
        if wait > 0:
            time.sleep(wait)
        kwargs.setdefault('timeout', 30)
        try:
            return self.session.get(url, **kwargs)
        finally:
            self._last = time.time()
