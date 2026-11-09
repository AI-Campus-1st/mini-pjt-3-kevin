"""
챗봇 진입점 (조회 전용, rag/만 사용)

실행:
    streamlit run app/main.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import streamlit as st  # noqa: E402

from app import chat, dashboard  # noqa: E402

st.set_page_config(page_title='지역화폐 정책 자료 챗봇', layout='wide')

tab_chat, tab_dash = st.tabs(['챗봇', '평가 대시보드'])
with tab_chat:
    chat.render()
with tab_dash:
    dashboard.render()
