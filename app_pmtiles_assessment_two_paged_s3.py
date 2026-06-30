"""FOCCUS demonstrator — two-page Streamlit app (About + Dashboard).

Run:
  streamlit run app_pmtiles_assessment_two_paged_s3.py

Page 1 (About): ESC1GB.md documentation with embedded PDF (DF122.pdf).
Page 2 (Dashboard): interactive indicator map and polygon area assessment.
"""

from __future__ import annotations

import streamlit as st

from foccus_about_page import render_about_page

st.set_page_config(
    page_title="FOCCUS German Bight demonstrator",
    layout="wide",
    initial_sidebar_state="collapsed",
)

about = st.Page(render_about_page, title="About", icon="📖", default=True)
dashboard = st.Page("foccus_dashboard_page.py", title="Dashboard", icon="🗺️")

pg = st.navigation([about, dashboard])
pg.run()
