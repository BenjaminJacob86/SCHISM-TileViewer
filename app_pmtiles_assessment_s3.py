"""
SCHISM indicator viewer: PMTiles map + NetCDF polygon assessments (S3).

  PMTiles  → fast triangle-mesh map (MapLibre on Folium)
  NetCDF   → exact polygon statistics, threshold maps, PDF (native mesh)

Run: streamlit run app_pmtiles_assessment_s3.py
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any

import altair as alt
import folium
import numpy as np
import pandas as pd
import streamlit as st
from folium.plugins import Draw
from streamlit_folium import st_folium
from assessment_report import add_legend_box, build_assessment_pdf_bytes
from pmtiles_s3_common import (
    CMAP_OPTIONS,
    DEFAULT_RUN_DATE,
    ENDPOINT_URL,
    EROSION_R1_CMAP,
    EROSION_R1_BAND_LABELS,
    EROSION_R1_CRITICAL_LEVELS,
    EROSION_R1_DEFAULT_CRITICAL_LEVEL,
    EROSION_R1_SEGMENT_COLORS,
    S3_PREFIX,
    SchismPMTilesLayer,
    add_map_legend,
    build_maplibre_style,
    default_cmap_index,
    effective_map_bounds,
    erosion_r1_band_for_value,
    erosion_r1_histogram_bands,
    fetch_s3_object_bytes,
    indicator_s3_uri,
    load_pmtiles_info_from_s3,
    parse_s3_uri,
    run_date_folder,
    s3_object_size,
    start_pmtiles_proxy_url,
)
from schism_mesh_assessment import (
    THRESHOLD_ABOVE_FACE,
    THRESHOLD_BELOW_FACE,
    polygon_assessment,
)
from schism_mesh_index import MESH_INDEX_FILENAME, build_mesh_index_bytes_from_nc, mesh_index_from_bytes

INDICATOR_LAYERS: dict[str, dict[str, Any]] = {
    "SSH q95": {
        "file": "q95_ssh_tris.pmtiles",
        "nc_file": "q95_ssh.nc",
        "nc_variable": "q95_ssh",
        "attribute": "q95_ssh",
        "caption": "SSH q95 (m)",
        "unit": "m",
        "cmap": "plasma",
        "critical_default": 1.0,
    },
    "Significant wave height q95": {
        "file": "q95_Hs_tris.pmtiles",
        "nc_file": "q95_Hs.nc",
        "nc_variable": "q95_Hs",
        "attribute": "q95_Hs",
        "caption": "Hs q95 (m)",
        "unit": "m",
        "cmap": "plasma",
        "critical_default": 1.5,
    },
    "Bed stress q95": {
        "file": "q95_tau_tris.pmtiles",
        "nc_file": "q95_tau.nc",
        "nc_variable": "q95_tau",
        "attribute": "q95_tau",
        "caption": "Bed stress q95 (N/m²)",
        "unit": "Pa",
        "cmap": "plasma",
        "critical_default": 0.5,
    },
    "Erosion risk ratio (R1)": {
        "file": "R1_tris.pmtiles",
        "nc_file": "R1.nc",
        "nc_variable": "R1",
        "attribute": "erosion_R1",
        "attribute_fallbacks": ("R1",),
        "caption": "Erosion risk ratio R1",
        "unit": "-",
        "cmap": EROSION_R1_CMAP,
        "critical_default": 0.5,
        "value_min_default": 0.25,
        "value_max_default": 1.0,
        "use_domain_bounds": True,
    },
    "Vegetation (nveg)": {
        "file": "nveg_tris.pmtiles",
        "nc_file": "nveg.nc",
        "nc_variable": "nveg",
        "attribute": "nveg",
        "caption": "Vegetation cover (nveg)",
        "unit": "-",
        "cmap": "Greens",
        "critical_default": 0.5,
    },
}


class SchismPMTilesLayerAssessment(SchismPMTilesLayer):
    """PMTiles overlay that does not block Folium Draw interactions."""

    def __init__(self, style: dict[str, Any], layer_name: str = "SCHISM indicator", **kwargs):
        super().__init__(
            style,
            layer_name=layer_name,
            interactive=False,
            pointer_events="none",
            **kwargs,
        )


def _indicator_nc_s3_uri(run_date: dt.date, nc_file: str) -> str:
    return indicator_s3_uri(run_date, nc_file)


@st.cache_data(show_spinner="Loading indicator NetCDF…", ttl=3600)
def _load_indicator_nc_cached(bucket: str, key: str) -> bytes:
    return fetch_s3_object_bytes(bucket, key)


@st.cache_data(show_spinner="Loading mesh spatial index…", ttl=3600)
def _load_mesh_index_bbox_from_s3(index_bucket: str, index_key: str) -> np.ndarray | None:
    try:
        raw = fetch_s3_object_bytes(index_bucket, index_key)
    except Exception:
        return None
    return mesh_index_from_bytes(raw)


@st.cache_data(show_spinner="Building mesh spatial index…", ttl=3600)
def _build_mesh_index_bbox_from_nc(nc_bucket: str, nc_key: str) -> np.ndarray:
    nc_bytes = fetch_s3_object_bytes(nc_bucket, nc_key)
    return mesh_index_from_bytes(build_mesh_index_bytes_from_nc(nc_bytes))


def _mesh_index_bbox_for_assessment(
    nc_bucket: str,
    nc_key: str,
    index_bucket: str,
    index_key: str,
) -> np.ndarray | None:
    bbox = _load_mesh_index_bbox_from_s3(index_bucket, index_key)
    if bbox is not None:
        return bbox
    try:
        return _build_mesh_index_bbox_from_nc(nc_bucket, nc_key)
    except Exception:
        return None


@st.cache_data(show_spinner="Computing area assessment…", ttl=600)
def _cached_polygon_assessment(
    nc_bucket: str,
    nc_key: str,
    variable: str,
    geom_json: str,
    critical_value: float,
    index_bucket: str,
    index_key: str,
) -> dict[str, Any]:
    nc_bytes = _load_indicator_nc_cached(nc_bucket, nc_key)
    mesh_bbox = _mesh_index_bbox_for_assessment(nc_bucket, nc_key, index_bucket, index_key)
    geom = json.loads(geom_json)
    return polygon_assessment(
        nc_bytes,
        geom,
        variable=variable,
        critical_value=critical_value,
        mesh_index_bbox=mesh_bbox,
        render_threshold=True,
    )


@st.cache_resource(show_spinner="Starting S3 PMTiles proxy…")
def _cached_pmtiles_proxy_url(bucket: str, key: str) -> str:
    return start_pmtiles_proxy_url(bucket, key)


PANEL_BLUE = "#1e3a8a"
FOCCUS_LOGO = Path(__file__).resolve().parent / "FOCCUS_Logo_clean RGB_whiteBG.png"
if not FOCCUS_LOGO.is_file():
    FOCCUS_LOGO = Path(__file__).resolve().parent / "FOCCUS_Logo_clean RGB.png"
# Set when the project README is published on GitHub (option 1: external doc link).
GITHUB_README_URL = ""
DEMO_TAGLINE = (
    "Interactive GCOAST-GB erosion indicators and polygon-based area assessment "
    "for the German Bight."
)


def run_dashboard(*, configure_page: bool = True) -> None:
    if configure_page:
        st.set_page_config(
            page_title="SCHISM indicators + assessment (S3)",
            layout="wide",
            initial_sidebar_state="collapsed",
        )
    st.markdown(
        f"""
        <style>
          .block-container {{padding-top: 0.75rem; padding-bottom: 0.5rem;}}
          /* Blue frame for panels tagged with the .blue-panel sentinel */
          div[data-testid="stVerticalBlockBorderWrapper"]:has(.blue-panel) {{
            border-color: {PANEL_BLUE} !important;
            border-width: 1.5px !important;
          }}
        </style>
        """,
        unsafe_allow_html=True,
    )
    hdr_logo, hdr_text, hdr_link = st.columns([1.1, 5.5, 1.4], vertical_alignment="center")
    with hdr_logo:
        if FOCCUS_LOGO.is_file():
            st.image(str(FOCCUS_LOGO), width=120)
    with hdr_text:
        st.markdown(
            "**FOCCUS Demonstrator — Management and protection of the coastal area, German Bight**"
        )
        st.caption(DEMO_TAGLINE)
    with hdr_link:
        if GITHUB_README_URL:
            st.link_button("About / documentation", GITHUB_README_URL, use_container_width=True)
        else:
            st.caption("Documentation link (GitHub) — set `GITHUB_README_URL` in the app.")

    indicator_names = list(INDICATOR_LAYERS)

    Debug = False
    with st.container(border=True):
        tb_run, tb_ind, tb_crit = st.columns([1, 1, 1], vertical_alignment="bottom")
        with tb_run:
            run_date = st.date_input("Run date", value=DEFAULT_RUN_DATE)
        run_folder = run_date_folder(run_date)

        with tb_ind:
            indicator_label = st.selectbox("Indicator", options=indicator_names, index=0)
        layer_cfg = INDICATOR_LAYERS[indicator_label]

        pmtiles_uri = indicator_s3_uri(run_date, layer_cfg["file"])
        nc_uri = _indicator_nc_s3_uri(run_date, layer_cfg["nc_file"])

        is_erosion_r1 = layer_cfg.get("cmap") == EROSION_R1_CMAP
        with tb_crit:
            if is_erosion_r1:
                level_options = list(EROSION_R1_CRITICAL_LEVELS)
                default_level = EROSION_R1_DEFAULT_CRITICAL_LEVEL
                default_idx = (
                    level_options.index(default_level)
                    if default_level in level_options
                    else 0
                )
                critical_level = st.selectbox(
                    "Critical level",
                    options=level_options,
                    index=default_idx,
                    help=(
                        "Erosion risk exceedance threshold: Low (>0.25), "
                        "Increased (>0.5), High (>0.75)."
                    ),
                )
                critical_value = float(EROSION_R1_CRITICAL_LEVELS[critical_level])
            else:
                critical_level = None
                critical_value = st.number_input(
                    "Critical value",
                    value=float(layer_cfg.get("critical_default", 0.0)),
                    step=0.05,
                    help="Used for exceedance donut chart and threshold map (from NetCDF).",
                )

        if Debug:
            st.caption(f"S3 folder: `{S3_PREFIX}/{run_folder}/`")
            st.text_input("PMTiles URI", value=pmtiles_uri, disabled=True)
            st.text_input("NetCDF URI", value=nc_uri, disabled=True)

    try:
        p_bucket, p_key = parse_s3_uri(pmtiles_uri)
        if s3_object_size(p_bucket, p_key) < 64 * 1024:
            st.warning(f"PMTiles file looks empty ({pmtiles_uri}). Map may not render.")
        info = load_pmtiles_info_from_s3(
            p_bucket,
            p_key,
            value_attribute=layer_cfg["attribute"],
            attribute_fallbacks=tuple(layer_cfg.get("attribute_fallbacks", ())),
        )
        pmtiles_url = _cached_pmtiles_proxy_url(p_bucket, p_key)

        n_bucket, n_key = parse_s3_uri(nc_uri)
        _load_indicator_nc_cached(n_bucket, n_key)  # warm cache for polygon assessment
        index_uri = indicator_s3_uri(run_date, MESH_INDEX_FILENAME)
        idx_bucket, idx_key = parse_s3_uri(index_uri)
        _mesh_index_bbox_for_assessment(n_bucket, n_key, idx_bucket, idx_key)  # warm spatial index
    except Exception as exc:
        st.error(f"Failed to load data from S3: {exc}")
        st.stop()

    south, west, north, east = effective_map_bounds(info["bounds"], layer_cfg)
    center_lat = (south + north) / 2.0
    center_lon = (west + east) / 2.0
    value_attr = info["value_attribute"]
    value_caption = layer_cfg["caption"]
    nc_variable = layer_cfg["nc_variable"]

    default_cmap = layer_cfg["cmap"]

    data_vmin = float(info["value_min"])
    data_vmax = float(info["value_max"])
    if data_vmin >= data_vmax:
        data_vmin = float(layer_cfg.get("value_min_default", data_vmin))
        data_vmax = float(layer_cfg.get("value_max_default", max(data_vmax, data_vmin + 1.0)))

    scale_floor = float(layer_cfg.get("value_min_default", data_vmin))
    scale_ceil = float(layer_cfg.get("value_max_default", data_vmax))
    slider_min = min(data_vmin, scale_floor)
    slider_max = max(data_vmax, scale_ceil)
    if slider_min >= slider_max:
        slider_min, slider_max = 0.0, 1.0

    span = slider_max - slider_min
    step = max(span / 200.0, 1e-4)
    default_lo = max(slider_min, min(data_vmin, slider_max - step))
    default_hi = min(slider_max, max(data_vmax, slider_min + step))
    if default_lo >= default_hi:
        default_lo, default_hi = slider_min, slider_max

    tile_options = {
        "OpenStreetMap": "OpenStreetMap",
        "CartoDB positron": "CartoDB positron",
        "CartoDB dark_matter": "CartoDB dark_matter",
    }

    data_max_zoom = int(info["max_zoom"])
    data_min_zoom = int(info["min_zoom"])
    # Tiles only render at zoom >= data_min_zoom. Sparse layers (e.g. R1) are often
    # built starting at a high zoom, so fitting the whole domain (~z7) shows nothing.
    domain_view = data_min_zoom <= 7
    zoom_start = min(data_max_zoom, 7) if domain_view else data_min_zoom

    unit = layer_cfg.get("unit", "")
    var_label = f"{indicator_label} ({unit})" if unit else indicator_label


    def _empty_stats() -> dict[str, Any]:
        """Zeroed stats so the dashboard panels render before a polygon is drawn."""
        if is_erosion_r1:
            edges = np.linspace(0.25, 1.0, 9)
        else:
            lo = float(min(vmin, vmax))
            hi = float(max(vmin, vmax))
            if hi <= lo:
                hi = lo + 1.0
            edges = np.linspace(lo, hi, 9)
        return {
            "count": 0,
            "above_count": 0,
            "below_or_equal_count": 0,
            "area_km2": 0.0,
            "area_above_km2": 0.0,
            "area_below_or_equal_km2": 0.0,
            "mean": 0.0,
            "min": 0.0,
            "max": 0.0,
            "hist_counts": np.zeros(len(edges) - 1),
            "hist_area": np.zeros(len(edges) - 1),
            "hist_edges": edges,
        }


    MAP_HEIGHT = 340
    THR_HEIGHT = 300
    TOP_PANEL_H = MAP_HEIGHT + 70

    cmap_key = f"cmap_{indicator_label}"
    range_key = f"range_{indicator_label}"

    if "selected_geom" not in st.session_state:
        st.session_state.selected_geom = None


    @st.fragment
    def indicator_map_panel():
        """Map + style controls in an isolated fragment.

        Changing colormap / range / opacity / basemap reruns only this fragment,
        leaving the statistics, distribution and threshold panels untouched.
        Drawing a new polygon updates the shared selection and triggers a full rerun.
        """
        with st.container(height=TOP_PANEL_H, border=True):
            hc1, hc2, hc3 = st.columns([3, 2, 3], vertical_alignment="center")
            hc1.markdown("**Indicator map**")
            with hc2:
                if st.session_state.selected_geom is not None and st.button(
                    "Clear", use_container_width=True
                ):
                    st.session_state.selected_geom = None
                    st.rerun()
            with hc3.popover("Style", use_container_width=True):
                f_cmap = st.selectbox(
                    "Colormap",
                    options=list(CMAP_OPTIONS),
                    index=default_cmap_index(default_cmap),
                    key=cmap_key,
                )
                f_basemap = st.selectbox(
                    "Basemap",
                    options=["OpenStreetMap", "CartoDB positron", "CartoDB dark_matter"],
                    index=1,
                    key="basemap_choice",
                )
                f_opacity = st.slider(
                    "Mesh overlay opacity", 0.1, 1.0, 0.85, 0.05, key="mesh_opacity"
                )
                f_vmin, f_vmax = st.slider(
                    "Color scale range",
                    min_value=float(slider_min),
                    max_value=float(slider_max),
                    value=(float(default_lo), float(default_hi)),
                    step=float(step),
                    key=range_key,
                    help="Set min and max of the colorbar / continuous map scale.",
                )

            fm = folium.Map(
                location=[center_lat, center_lon],
                zoom_start=zoom_start,
                max_zoom=data_max_zoom + 1,
                tiles=tile_options[f_basemap],
                control_scale=True,
            )
            fstyle = build_maplibre_style(
                pmtiles_url=pmtiles_url,
                source_layer=info["layer_id"],
                value_attribute=value_attr,
                vmin=float(f_vmin),
                vmax=float(f_vmax),
                opacity=float(f_opacity),
                cmap_name=f_cmap,
                tile_max_zoom=data_max_zoom,
            )
            SchismPMTilesLayerAssessment(fstyle, layer_name=indicator_label).add_to(fm)
            Draw(
                export=False,
                position="topleft",
                draw_options={
                    "polyline": False,
                    "circle": False,
                    "circlemarker": False,
                    "marker": False,
                    "polygon": True,
                    "rectangle": True,
                },
                edit_options={"edit": True, "remove": True},
            ).add_to(fm)
            add_map_legend(
                fm,
                caption=value_caption,
                cmap_name=f_cmap,
                vmin=float(f_vmin),
                vmax=float(f_vmax),
            )
            existing = st.session_state.selected_geom
            if existing is not None:
                folium.GeoJson(
                    {"type": "Feature", "geometry": existing, "properties": {}},
                    style_function=lambda _: {
                        "color": PANEL_BLUE,
                        "weight": 2,
                        "fillOpacity": 0.05,
                    },
                    name="selection",
                ).add_to(fm)
            if domain_view:
                folium.FitBounds([[south, west], [north, east]]).add_to(fm)
            map_state = st_folium(
                fm, use_container_width=True, height=MAP_HEIGHT, key="indicator_map"
            )
            if not domain_view:
                st.caption(
                    f"Tiles only exist at zoom {data_min_zoom}–{data_max_zoom}; zoom/pan to explore."
                )

        new_geom = None
        if isinstance(map_state, dict):
            last = map_state.get("last_active_drawing")
            if isinstance(last, dict) and "geometry" in last:
                new_geom = last["geometry"]
        # Only adopt an actual new drawing; ignore None (map remount on style change)
        # so the selection — and the other panels — survive style tweaks.
        if new_geom is not None and json.dumps(new_geom) != json.dumps(
            st.session_state.selected_geom
        ):
            st.session_state.selected_geom = new_geom
            st.rerun()


    map_col, stats_col = st.columns([5, 4], gap="medium", vertical_alignment="top")

    # --- Panel 1: indicator map (top-left) -------------------------------------
    with map_col:
        indicator_map_panel()

    geom = st.session_state.selected_geom
    vmin, vmax = st.session_state.get(range_key, (default_lo, default_hi))

    assessment = None
    real_stats = None
    if geom is not None:
        try:
            assessment = _cached_polygon_assessment(
                n_bucket,
                n_key,
                nc_variable,
                json.dumps(geom),
                float(critical_value),
                idx_bucket,
                idx_key,
            )
            real_stats = assessment.get("stats")
        except Exception as exc:
            with stats_col:
                st.error(f"Failed to compute polygon stats: {exc}")

    has_data = bool(real_stats) and real_stats.get("count", 0) > 0
    stats = real_stats if has_data else _empty_stats()

    counts = stats["hist_counts"]
    edges = stats["hist_edges"]
    hist_area = stats.get("hist_area", np.zeros(len(counts)))
    centers = (edges[:-1] + edges[1:]) / 2.0
    hist_df = (
        pd.DataFrame({"value": centers, "count": counts, "area_km2": hist_area})
        .groupby("value", as_index=True)[["count", "area_km2"]]
        .sum()
        .sort_index()
    )

    total = int(stats["count"])
    above = int(stats["above_count"])
    below = int(stats["below_or_equal_count"])
    area_above = float(stats.get("area_above_km2", 0.0))
    area_below = float(stats.get("area_below_or_equal_km2", 0.0))

    # --- Panel 2: area statistics + donut (top-right) --------------------------
    with stats_col:
        with st.container(height=TOP_PANEL_H, border=True):
            st.markdown('<span class="blue-panel"></span>', unsafe_allow_html=True)
            head_l, head_r = st.columns([3, 2], vertical_alignment="center")
            head_l.markdown("**Area statistics** (NetCDF mesh)")
            if geom is None:
                head_r.caption("Awaiting selection")
            elif not has_data:
                head_r.caption("No triangles in polygon")
            else:
                head_r.caption(f"{total:,} triangles")

            metric_col, donut_col = st.columns([2, 3], vertical_alignment="center")
            with metric_col:
                st.metric("Area km²", f"{stats.get('area_km2', 0.0):.2f}")
                st.metric("Mean", f"{stats['mean']:.4g}")
                st.metric("Min", f"{stats['min']:.4g}")
                st.metric("Max", f"{stats['max']:.4g}")

            with donut_col:
                donut_df = pd.DataFrame(
                    {"category": ["≤ critical", "> critical"], "count": [below, above]}
                )
                donut_df["pct"] = donut_df["count"] / max(total, 1) * 100.0
                donut_df["area_km2"] = [area_below, area_above]
                if total == 0:
                    placeholder_df = pd.DataFrame(
                        {"category": ["No selection"], "count": [1], "pct": [0.0], "area_km2": [0.0]}
                    )
                    donut = (
                        alt.Chart(placeholder_df)
                        .mark_arc(innerRadius=45, outerRadius=85)
                        .encode(
                            theta=alt.Theta("count:Q", stack=True),
                            color=alt.value("#e2e8f0"),
                            tooltip=["category:N"],
                        )
                        .properties(title="Area %", height=250)
                    )
                    donut_text = (
                        alt.Chart(pd.DataFrame({"label": ["0%"]}))
                        .mark_text(size=15, color="#718096")
                        .encode(text="label:N")
                    )
                else:
                    donut = (
                        alt.Chart(donut_df)
                        .mark_arc(innerRadius=45, outerRadius=85)
                        .encode(
                            theta=alt.Theta("count:Q", stack=True),
                            color=alt.Color(
                                "category:N",
                                scale=alt.Scale(
                                    domain=["≤ critical", "> critical"],
                                    range=["#2b6cb0", "#e53e3e"],
                                ),
                                legend=alt.Legend(orient="bottom", title=None),
                            ),
                            tooltip=[
                                "category:N",
                                "count:Q",
                                alt.Tooltip("pct:Q", format=".1f"),
                                alt.Tooltip("area_km2:Q", format=".2f", title="area (km²)"),
                            ],
                        )
                        .properties(title="Area %", height=250)
                    )
                    donut_text = (
                        alt.Chart(donut_df)
                        .mark_text(radius=105, size=12)
                        .encode(
                            theta=alt.Theta("count:Q", stack=True),
                            text=alt.Text("pct:Q", format=".1f"),
                            color=alt.value("black"),
                        )
                    )
                st.altair_chart(donut + donut_text, use_container_width=True)

    # --- Panel 3: threshold exceedance map (bottom-left) -----------------------
    with map_col:
        with st.container(height=THR_HEIGHT + 75, border=True):
            st.markdown('<span class="blue-panel"></span>', unsafe_allow_html=True)
            st.markdown("**Target area threshold exceedance map**")
            thr_rgba = assessment.get("threshold_rgba") if assessment else None
            thr_bounds = assessment.get("threshold_bounds") if assessment else None
            if not has_data or thr_rgba is None or thr_bounds is None:
                st.caption(
                    "Draw a polygon to render the rasterized mesh: "
                    "below threshold = light blue (blue edges), above = red."
                )
                st.info("No threshold map yet — select an area on the indicator map.")
            else:
                try:
                    tsouth, twest, tnorth, teast = thr_bounds
                    tcenter = [(tsouth + tnorth) / 2, (twest + teast) / 2]
                    tm = folium.Map(
                        location=tcenter, zoom_start=8, tiles="CartoDB positron", control_scale=True
                    )
                    folium.raster_layers.ImageOverlay(
                        image=thr_rgba,
                        bounds=[[tsouth, twest], [tnorth, teast]],
                        opacity=1.0,
                        interactive=False,
                    ).add_to(tm)
                    folium.GeoJson(
                        {"type": "Feature", "geometry": geom, "properties": {}},
                        style_function=lambda _: {
                            "color": PANEL_BLUE,
                            "weight": 2,
                            "fillOpacity": 0,
                        },
                    ).add_to(tm)
                    add_legend_box(
                        tm,
                        title="Threshold (mesh)",
                        items=[
                            (THRESHOLD_ABOVE_FACE, f"≥ {critical_value:g}"),
                            (THRESHOLD_BELOW_FACE, f"< {critical_value:g}"),
                        ],
                    )
                    folium.FitBounds([[tsouth, twest], [tnorth, teast]]).add_to(tm)
                    st_folium(tm, use_container_width=True, height=THR_HEIGHT, key="threshold_map")
                except Exception as exc:
                    st.error(f"Failed to render threshold map: {exc}")

    # --- Panel 4: value distribution / bar plot (bottom-right) -----------------
    with stats_col:
        with st.container(height=THR_HEIGHT + 75, border=True):
            st.markdown('<span class="blue-panel"></span>', unsafe_allow_html=True)
            st.markdown("**Value distribution** — area (km²) vs. threshold")
            hist_plot_df = hist_df.reset_index()
            if is_erosion_r1:
                bands = erosion_r1_band_for_value
                hist_plot_df[["risk_band", "bar_color"]] = hist_plot_df["value"].apply(
                    lambda v: pd.Series(bands(float(v)))
                )
                bg_df = pd.DataFrame(erosion_r1_histogram_bands())
                background = (
                    alt.Chart(bg_df)
                    .mark_rect(opacity=0.22)
                    .encode(
                        x=alt.X("x0:Q", scale=alt.Scale(domain=[0.25, 1.0])),
                        x2="x1:Q",
                        color=alt.Color("color:N", scale=None, legend=None),
                    )
                )
                bars = (
                    alt.Chart(hist_plot_df)
                    .mark_bar()
                    .encode(
                        x=alt.X(
                            "value:Q",
                            title=var_label,
                            scale=alt.Scale(domain=[0.25, 1.0]),
                        ),
                        y=alt.Y("area_km2:Q", title="Area (km²)"),
                        color=alt.Color(
                            "risk_band:N",
                            scale=alt.Scale(
                                domain=list(EROSION_R1_BAND_LABELS),
                                range=list(EROSION_R1_SEGMENT_COLORS),
                            ),
                            legend=alt.Legend(title="Risk band", orient="top"),
                        ),
                        tooltip=[
                            "value:Q",
                            alt.Tooltip("area_km2:Q", format=".2f", title="area (km²)"),
                            "count:Q",
                            "risk_band:N",
                        ],
                    )
                )
                rule_label = critical_level or f"{critical_value:g}"
            else:
                background = None
                bars = (
                    alt.Chart(hist_plot_df)
                    .mark_bar()
                    .encode(
                        x=alt.X("value:Q", title=var_label),
                        y=alt.Y("area_km2:Q", title="Area (km²)"),
                        tooltip=[
                            "value:Q",
                            alt.Tooltip("area_km2:Q", format=".2f", title="area (km²)"),
                            "count:Q",
                        ],
                    )
                )
                rule_label = "critical value"

            rule = (
                alt.Chart(pd.DataFrame({"value": [critical_value]}))
                .mark_rule(color="#e53e3e", strokeWidth=2)
                .encode(x="value:Q")
            )
            label = (
                alt.Chart(pd.DataFrame({"value": [critical_value], "label": [rule_label]}))
                .mark_text(color="#e53e3e", angle=90, dy=-5, dx=5, align="left", fontSize=15, fontWeight="bold")
                .encode(x="value:Q", text="label:N")
            )
            hist_chart = bars + rule + label
            if background is not None:
                hist_chart = background + hist_chart
            st.altair_chart(
                hist_chart.properties(height=THR_HEIGHT - 40).interactive(),
                use_container_width=True,
            )

    # Download assessment (PDF) — temporarily disabled while testing the top-bar layout.
    # if has_data:
    #     placeholder_rgba = np.zeros((2, 2, 4), dtype=np.uint8)
    #     pdf_bytes = build_assessment_pdf_bytes(
    #         variable=var_label,
    #         critical_value=float(critical_value),
    #         stats=stats,
    #         hist_df=hist_df,
    #         raster_rgba=placeholder_rgba,
    #         raster_bounds=(south, west, north, east),
    #         geom=geom,
    #     )
    #     st.download_button(
    #         label="Download assessment (PDF)",
    #         data=pdf_bytes,
    #         file_name=f"assessment_{run_folder}_{nc_variable}.pdf",
    #         mime="application/pdf",
    #         use_container_width=True,
    #     )


if __name__ == "__main__":
    run_dashboard()
