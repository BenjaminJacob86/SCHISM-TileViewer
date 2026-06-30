"""FOCCUS demonstrator — About page (ESC1GB documentation + embedded assets)."""

from __future__ import annotations

import base64
import re
from pathlib import Path

import streamlit as st
import streamlit.components.v1 as components

APP_DIR = Path(__file__).resolve().parent
ESC1GB_MD = APP_DIR / "ESC1GB.md"
IFRAME_RE = re.compile(r"<iframe\b.*?</iframe>", re.IGNORECASE | re.DOTALL)
IFRAME_SRC_RE = re.compile(r'src="([^"]+)"', re.IGNORECASE)
INSERT_DIRECTIVE_RE = re.compile(r'streamlit_insert_pdf="([^"]+)"', re.IGNORECASE)
LOGO_HEADER_RE = re.compile(
    r'<div style="display: flex; justify-content: space-between; align-items: center;">\s*'
    r'<img\s+src="([^"]+)"[^>]*>\s*'
    r'<img\s+src="([^"]+)"[^>]*>\s*'
    r"</div>",
    re.IGNORECASE | re.DOTALL,
)
LOCAL_IMG_RE = re.compile(
    r'<img\s+src="([^"]+)"([^>]*)>',
    re.IGNORECASE,
)
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
IMAGE_MIME = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
}


def _image_data_uri(path: Path) -> str:
    mime = IMAGE_MIME.get(path.suffix.lower(), "image/png")
    b64 = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{b64}"


def _embed_pdf(path: Path, *, height: int = 720) -> None:
    if not path.is_file():
        st.warning(f"PDF not found: `{path.name}`")
        return
    pdf_b64 = base64.b64encode(path.read_bytes()).decode("ascii")
    components.html(
        f"""
        <iframe
            src="data:application/pdf;base64,{pdf_b64}"
            width="100%"
            height="{height}px"
            style="border: none;"
        ></iframe>
        """,
        height=height + 12,
    )


def _normalize_legacy_iframes(text: str) -> str:
    """Convert <iframe src="file.pdf"> blocks to streamlit_insert_pdf directives."""

    def _repl(match: re.Match[str]) -> str:
        src_match = IFRAME_SRC_RE.search(match.group(0))
        if src_match:
            return f'streamlit_insert_pdf="{src_match.group(1).strip()}"'
        return ""

    return IFRAME_RE.sub(_repl, text)


def _insert_asset(filename: str) -> None:
    """Insert a local PDF or image at the directive position in the markdown."""
    path = APP_DIR / filename.strip()
    if not path.is_file():
        st.warning(f"Asset not found: `{filename.strip()}`")
        return

    suffix = path.suffix.lower()
    if suffix == ".pdf":
        _embed_pdf(path)
    elif suffix in IMAGE_SUFFIXES:
        st.image(str(path), use_container_width=True)
    else:
        st.warning(f"Unsupported asset type for `{path.name}` (use PDF or image).")


def _render_logo_header(foccus_src: str, hereon_src: str) -> None:
    """FOCCUS top-left, Hereon top-right (matches ESC1GB.md header)."""
    left, right = st.columns([1, 1], vertical_alignment="center")
    foccus_path = APP_DIR / foccus_src.strip()
    with left:
        if foccus_path.is_file():
            st.image(str(foccus_path), width=160)
        else:
            st.warning(f"FOCCUS logo not found: `{foccus_src.strip()}`")
    with right:
        hereon = hereon_src.strip()
        st.markdown(
            f'<div style="text-align: right;"><img src="{hereon}" width="220" alt="Hereon"></div>',
            unsafe_allow_html=True,
        )


def _render_markdown_block(text: str) -> None:
    """Render markdown, swapping only *local-file* <img> tags for st.image.

    Remote images (http/https) are left untouched inside the surrounding
    markdown so wrapping HTML stays intact.
    """
    pos = 0
    for match in LOCAL_IMG_RE.finditer(text):
        src = match.group(1).strip()
        local = APP_DIR / src
        if not local.is_file():
            continue
        before = text[pos : match.start()]
        if before.strip():
            st.markdown(before, unsafe_allow_html=True)
        attrs = match.group(2)
        pct_match = re.search(r'width="(\d+)\s*%"', attrs)
        px_match = re.search(r'width="(\d+)\s*(?:px)?"', attrs)
        if pct_match:
            css_width = f"{min(int(pct_match.group(1)), 100)}%"
        elif px_match:
            css_width = f"{px_match.group(1)}px"
        else:
            css_width = "100%"
        st.markdown(
            f'<img src="{_image_data_uri(local)}" '
            f'style="width: {css_width}; height: auto; display: block;" '
            f'alt="{local.name}">',
            unsafe_allow_html=True,
        )
        pos = match.end()
    tail = text[pos:]
    if tail.strip():
        st.markdown(tail, unsafe_allow_html=True)


def _render_markdown_with_directives(text: str) -> None:
    """Render markdown and honour streamlit_insert_pdf=\"...\" at each position."""
    pos = 0
    found = False
    for match in INSERT_DIRECTIVE_RE.finditer(text):
        found = True
        before = text[pos : match.start()]
        if before.strip():
            _render_markdown_block(before)
        _insert_asset(match.group(1))
        pos = match.end()

    tail = text[pos:]
    if tail.strip():
        _render_markdown_block(tail)
    elif not found and text.strip():
        _render_markdown_block(text)


def render_about_page() -> None:
    if not ESC1GB_MD.is_file():
        st.error(f"Documentation file not found: `{ESC1GB_MD.name}`")
        return

    md_body = _normalize_legacy_iframes(ESC1GB_MD.read_text(encoding="utf-8"))

    logo_match = LOGO_HEADER_RE.search(md_body)
    if logo_match:
        _render_logo_header(logo_match.group(1), logo_match.group(2))
        md_body = LOGO_HEADER_RE.sub("", md_body, count=1).lstrip()

    _render_markdown_with_directives(md_body)
