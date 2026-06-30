"""Shared S3 PMTiles helpers for Streamlit indicator viewers."""

from __future__ import annotations

import datetime as dt
import http.server
import re
import socket
import threading
from http import HTTPStatus
from typing import Any
from urllib.parse import urlparse

import boto3
import folium
import matplotlib.colors as mcolors
import matplotlib.pyplot as plt
import numpy as np
from botocore import UNSIGNED
from botocore.config import Config
from folium.elements import JSCSSMixin
from folium.map import Layer
from jinja2 import Template
from pmtiles.reader import Reader

ENDPOINT_URL = "https://minio.dive.edito.eu"
S3_BUCKET = "oidc-jacobb"
S3_PREFIX = "Hereon/IndicatorAssesment"
DEFAULT_RUN_DATE = dt.date(2026, 6, 4)
DEFAULT_DOMAIN_BOUNDS = (53.04, 5.12, 55.63, 10.40)
MIN_BOUNDS_SPAN_DEG = 0.5

# Erosion risk (R1) — categorical colors matching geotiff_app_s3.py.
# Data exists only for ratio >= 0.25 (below is NaN / transparent), so the
# visible scheme has three segments:
#   0.25–0.50 yellow, 0.50–0.75 orange, 0.75–1.0 red.
# Values < 0.25 (no data) are conceptually green but never drawn.
EROSION_R1_CMAP = "erosion_risk"
EROSION_R1_UNDER_COLOR = "#008000"  # green, < 0.25 (masked / no data)
EROSION_R1_BOUNDS = (0.25, 0.5, 0.75, 1.0)
EROSION_R1_SEGMENT_COLORS = ("#ffff80", "#ffa500", "#ff0000")  # yellow, orange, red
# Text ticks shown on the erosion-risk colorbar instead of numbers.
EROSION_R1_TICKS = ((0.25, "low"), (0.5, "increased"), (0.75, "high"))
# Assessment critical-level picker (maps label → numeric threshold).
EROSION_R1_CRITICAL_LEVELS: dict[str, float] = {
    "Low": 0.25,
    "Increased": 0.5,
    "High": 0.75,
}
EROSION_R1_DEFAULT_CRITICAL_LEVEL = "Increased"
EROSION_R1_BAND_LABELS = ("low", "increased", "high")


def erosion_r1_band_for_value(value: float) -> tuple[str, str]:
    """Return (band label, hex colour) for an erosion ratio."""
    if value < EROSION_R1_BOUNDS[1]:
        return EROSION_R1_BAND_LABELS[0], EROSION_R1_SEGMENT_COLORS[0]
    if value < EROSION_R1_BOUNDS[2]:
        return EROSION_R1_BAND_LABELS[1], EROSION_R1_SEGMENT_COLORS[1]
    return EROSION_R1_BAND_LABELS[2], EROSION_R1_SEGMENT_COLORS[2]


def erosion_r1_histogram_bands() -> list[dict[str, float | str]]:
    """Background bands for R1 histogram (yellow / orange / red)."""
    return [
        {
            "x0": EROSION_R1_BOUNDS[0],
            "x1": EROSION_R1_BOUNDS[1],
            "color": EROSION_R1_SEGMENT_COLORS[0],
        },
        {
            "x0": EROSION_R1_BOUNDS[1],
            "x1": EROSION_R1_BOUNDS[2],
            "color": EROSION_R1_SEGMENT_COLORS[1],
        },
        {
            "x0": EROSION_R1_BOUNDS[2],
            "x1": EROSION_R1_BOUNDS[3],
            "color": EROSION_R1_SEGMENT_COLORS[2],
        },
    ]

CMAP_OPTIONS = (
    EROSION_R1_CMAP,
    "viridis",
    "plasma",
    "cividis",
    "RdYlBu_r",
    "coolwarm",
    "YlOrRd",
    "Greens",
)


def default_cmap_index(cmap_name: str) -> int:
    try:
        return CMAP_OPTIONS.index(cmap_name)
    except ValueError:
        return 0


def build_fill_color_expression(
    value_attribute: str,
    *,
    vmin: float,
    vmax: float,
    cmap_name: str,
) -> list[Any]:
    """MapLibre fill-color expression (linear or categorical step)."""
    if cmap_name == EROSION_R1_CMAP:
        # Underflow (< 0.25) green, then switch into each segment at its lower bound.
        stops: list[Any] = ["step", ["get", value_attribute], EROSION_R1_UNDER_COLOR]
        for boundary, color in zip(
            EROSION_R1_BOUNDS[:-1], EROSION_R1_SEGMENT_COLORS, strict=True
        ):
            stops.extend([float(boundary), color])
        return stops

    cmap = plt.colormaps.get_cmap(cmap_name)
    stops = ["interpolate", ["linear"], ["get", value_attribute]]
    for t in np.linspace(0.0, 1.0, 8):
        value = vmin + t * (vmax - vmin)
        stops.extend([float(value), mcolors.to_hex(cmap(t))])
    return stops


def _add_erosion_risk_legend(m: folium.Map, caption: str) -> None:
    """Segmented erosion-risk colorbar with text ticks (low / increased / high)."""
    lo, hi = EROSION_R1_BOUNDS[0], EROSION_R1_BOUNDS[-1]
    span = hi - lo

    segments = ""
    for left, right, color in zip(
        EROSION_R1_BOUNDS[:-1], EROSION_R1_BOUNDS[1:], EROSION_R1_SEGMENT_COLORS, strict=True
    ):
        width_pct = (right - left) / span * 100.0
        segments += (
            f'<div style="width:{width_pct:.4f}%; background:{color}; height:14px;"></div>'
        )

    ticks = ""
    for value, label in EROSION_R1_TICKS:
        pos_pct = (value - lo) / span * 100.0
        ticks += (
            f'<div style="position:absolute; left:{pos_pct:.4f}%; transform:translateX(-50%); '
            'text-align:center; font-size:11px; color:#111;">'
            '<div style="height:6px; border-left:1px solid #111; margin:0 auto; width:0;"></div>'
            f"{label}</div>"
        )

    html = f"""
    <div style="
      position: fixed; top: 12px; right: 12px; z-index: 9999;
      background: rgba(255,255,255,0.92); border: 1px solid rgba(0,0,0,0.2);
      border-radius: 6px; padding: 8px 12px 22px 12px; width: 220px;
      font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Arial, sans-serif;
      box-shadow: 0 2px 10px rgba(0,0,0,0.15);
    ">
      <div style="font-size:12px; font-weight:600; color:#111; margin-bottom:6px;">{caption}</div>
      <div style="display:flex; width:100%; border:1px solid rgba(0,0,0,0.25);">{segments}</div>
      <div style="position:relative; width:100%; height:18px; margin-top:2px;">{ticks}</div>
    </div>
    """
    m.get_root().html.add_child(folium.Element(html))


def _add_continuous_legend(
    m: folium.Map,
    caption: str,
    cmap_name: str,
    vmin: float,
    vmax: float,
) -> None:
    """Continuous colorbar inside a semi-transparent white box (matches R1 legend)."""
    cmap = plt.colormaps.get_cmap(cmap_name)
    stops = ", ".join(
        f"{mcolors.to_hex(cmap(t))} {t * 100:.2f}%" for t in np.linspace(0.0, 1.0, 12)
    )
    gradient = f"linear-gradient(to right, {stops})"

    span = vmax - vmin
    ticks = ""
    for t in (0.0, 0.25, 0.5, 0.75, 1.0):
        value = vmin + t * span
        pos_pct = t * 100.0
        ticks += (
            f'<div style="position:absolute; left:{pos_pct:.4f}%; transform:translateX(-50%); '
            'text-align:center; font-size:11px; color:#111;">'
            '<div style="height:6px; border-left:1px solid #111; margin:0 auto; width:0;"></div>'
            f"{value:.2f}</div>"
        )

    html = f"""
    <div style="
      position: fixed; top: 12px; right: 12px; z-index: 9999;
      background: rgba(255,255,255,0.92); border: 1px solid rgba(0,0,0,0.2);
      border-radius: 6px; padding: 8px 12px 22px 12px; width: 220px;
      font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Arial, sans-serif;
      box-shadow: 0 2px 10px rgba(0,0,0,0.15);
    ">
      <div style="font-size:12px; font-weight:600; color:#111; margin-bottom:6px;">{caption}</div>
      <div style="width:100%; height:14px; border:1px solid rgba(0,0,0,0.25); background:{gradient};"></div>
      <div style="position:relative; width:100%; height:18px; margin-top:2px;">{ticks}</div>
    </div>
    """
    m.get_root().html.add_child(folium.Element(html))


def add_map_legend(
    m: folium.Map,
    *,
    caption: str,
    cmap_name: str,
    vmin: float,
    vmax: float,
) -> None:
    """Add a Folium colorbar (categorical step for R1, continuous otherwise)."""
    if cmap_name == EROSION_R1_CMAP:
        _add_erosion_risk_legend(m, caption)
        return

    _add_continuous_legend(m, caption, cmap_name, float(vmin), float(vmax))


class SchismPMTilesLayer(JSCSSMixin, Layer):
    _template = Template(
        """
        {% macro script(this, kwargs) -%}
        if (!window.__schismPmtilesProtocol) {
            const protocol = new pmtiles.Protocol();
            maplibregl.addProtocol("pmtiles", protocol.tile);
            window.__schismPmtilesProtocol = protocol;
        }
        {{ this._parent.get_name() }}.createPane("schism_pmtiles_{{ this.get_name() }}");
        const pane = {{ this._parent.get_name() }}.getPane("schism_pmtiles_{{ this.get_name() }}");
        pane.style.zIndex = 450;
        pane.style.pointerEvents = {{ this.pointer_events|tojson }};
        // Assign to a variable named get_name(): newer folium (>=0.16) auto-emits
        // `{{ this.get_name() }}.addTo(map)` via Layer.render/ElementAddToElement.
        // Without this var that statement references an undefined identifier and
        // throws, aborting the rest of the map script (e.g. the Draw control).
        var {{ this.get_name() }} = L.maplibreGL({
            pane: "schism_pmtiles_{{ this.get_name() }}",
            style: {{ this.style|tojson }},
            interactive: {{ this.interactive|tojson }},
        });
        {{ this.get_name() }}.addTo({{ this._parent.get_name() }});
        {%- endmacro %}
        """
    )
    default_css = [
        ("maplibre_css", "https://unpkg.com/maplibre-gl@4.7.1/dist/maplibre-gl.css"),
    ]
    default_js = [
        ("pmtiles", "https://unpkg.com/pmtiles@3.2.1/dist/pmtiles.js"),
        ("maplibre-lib", "https://unpkg.com/maplibre-gl@4.7.1/dist/maplibre-gl.js"),
        (
            "maplibre-leaflet",
            "https://unpkg.com/@maplibre/maplibre-gl-leaflet@0.0.22/leaflet-maplibre-gl.js",
        ),
    ]

    def __init__(
        self,
        style: dict[str, Any],
        layer_name: str = "SCHISM indicator",
        *,
        interactive: bool = True,
        pointer_events: str = "auto",
        **kwargs,
    ):
        super().__init__(name=layer_name, **kwargs)
        self.style = style
        self.interactive = interactive
        self.pointer_events = pointer_events
        # folium / streamlit-folium derive the layer's JavaScript variable name
        # from ``_name`` (as ``f"{_name.lower()}_{id}"``). A human label such as
        # "SSH q95" would inject a space into that identifier, producing invalid
        # JS like ``ssh q95_1.addTo(map)`` which throws a SyntaxError and aborts
        # the whole map script (blank map on newer folium). Keep the readable
        # display name (passed via ``name=``) but use a sanitized identifier here.
        safe_name = re.sub(r"\W+", "_", layer_name).strip("_") or "schism_layer"
        self._name = safe_name


def _s3_client():
    return boto3.client(
        "s3",
        endpoint_url=ENDPOINT_URL,
        config=Config(signature_version=UNSIGNED),
    )


def parse_s3_uri(s3_uri: str) -> tuple[str, str]:
    parsed = urlparse(s3_uri)
    if parsed.scheme != "s3" or not parsed.netloc:
        raise ValueError("S3 URI must look like s3://bucket/key")
    return parsed.netloc, parsed.path.lstrip("/")


def public_s3_url(bucket: str, key: str) -> str:
    """Browser-reachable HTTPS URL for a public MinIO/S3 object.

    The PMTiles JS protocol fetches this directly from the user's browser via
    HTTP Range requests, so it must be a public, internet-reachable URL (the
    local 127.0.0.1 proxy only works when the browser and server share a
    machine, i.e. local development — not on Streamlit Cloud).
    """
    return f"{ENDPOINT_URL.rstrip('/')}/{bucket}/{key.lstrip('/')}"


def run_date_folder(run_date: dt.date) -> str:
    return run_date.strftime("%Y%m%d")


def effective_map_bounds(
    bounds: tuple[float, float, float, float],
    layer_cfg: dict[str, Any],
) -> tuple[float, float, float, float]:
    south, west, north, east = bounds
    span_lon = east - west
    span_lat = north - south
    if layer_cfg.get("use_domain_bounds") or span_lon < MIN_BOUNDS_SPAN_DEG or span_lat < MIN_BOUNDS_SPAN_DEG:
        ds, dw, dn, de = DEFAULT_DOMAIN_BOUNDS
        return (ds, dw, dn, de)
    return bounds


def indicator_s3_uri(run_date: dt.date, filename: str) -> str:
    folder = run_date_folder(run_date)
    return f"s3://{S3_BUCKET}/{S3_PREFIX}/{folder}/{filename}"


def s3_get_bytes(bucket: str, key: str, offset: int, length: int) -> bytes:
    client = _s3_client()
    end = offset + length - 1
    resp = client.get_object(Bucket=bucket, Key=key, Range=f"bytes={offset}-{end}")
    return resp["Body"].read()


def s3_object_size(bucket: str, key: str) -> int:
    client = _s3_client()
    head = client.head_object(Bucket=bucket, Key=key)
    return int(head["ContentLength"])


def fetch_s3_object_bytes(bucket: str, key: str) -> bytes:
    return _s3_client().get_object(Bucket=bucket, Key=key)["Body"].read()


def load_pmtiles_info_from_s3(
    bucket: str, key: str, *, value_attribute: str, attribute_fallbacks: tuple[str, ...] = ()
) -> dict[str, Any]:
    def get_bytes(offset: int, length: int) -> bytes:
        return s3_get_bytes(bucket, key, offset, length)

    reader = Reader(get_bytes)
    header = reader.header()
    metadata = reader.metadata()

    bounds = metadata.get("antimeridian_adjusted_bounds")
    if isinstance(bounds, str):
        west, south, east, north = (float(v) for v in bounds.split(","))
    else:
        west = header["min_lon_e7"] / 1e7
        south = header["min_lat_e7"] / 1e7
        east = header["max_lon_e7"] / 1e7
        north = header["max_lat_e7"] / 1e7

    layer_id = key.rsplit("/", 1)[-1].removesuffix(".pmtiles")
    vector_layers = metadata.get("vector_layers") or []
    if vector_layers:
        layer_id = vector_layers[0].get("id", layer_id)

    vmin, vmax = 0.0, 1.0
    tilestats = metadata.get("tilestats", {}).get("layers") or []
    resolved_attr = value_attribute
    if tilestats:
        attrs = tilestats[0].get("attributes") or []
        found = False
        for name in (value_attribute, *attribute_fallbacks):
            for attr in attrs:
                if attr.get("attribute") == name:
                    vmin = float(attr.get("min", vmin))
                    vmax = float(attr.get("max", vmax))
                    resolved_attr = name
                    found = True
                    break
            if found:
                break
        if not found and attrs:
            vmin = float(attrs[0].get("min", vmin))
            vmax = float(attrs[0].get("max", vmax))
            resolved_attr = str(attrs[0].get("attribute", value_attribute))

    return {
        "layer_id": layer_id,
        "value_attribute": resolved_attr,
        "bounds": (south, west, north, east),
        "min_zoom": int(header["min_zoom"]),
        "max_zoom": int(header["max_zoom"]),
        "value_min": vmin,
        "value_max": vmax,
        "pmtiles_version": int(header["version"]),
    }


def _pick_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class _S3PMTilesProxyHandler(http.server.BaseHTTPRequestHandler):
    bucket: str = ""
    key: str = ""

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _send_cors(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, HEAD, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Range")
        self.send_header("Accept-Ranges", "bytes")

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self._send_cors()
        self.end_headers()

    def do_GET(self) -> None:
        try:
            file_size = s3_object_size(self.bucket, self.key)
        except Exception:
            self.send_error(HTTPStatus.NOT_FOUND, "S3 object not found")
            return

        range_header = self.headers.get("Range")
        if range_header:
            match = re.match(r"bytes=(\d+)-(\d*)", range_header)
            if not match:
                self.send_error(HTTPStatus.BAD_REQUEST, "Invalid Range header")
                return
            start = int(match.group(1))
            end = int(match.group(2)) if match.group(2) else file_size - 1
            end = min(end, file_size - 1)
            if start > end or start >= file_size:
                self.send_error(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                return
            data = s3_get_bytes(self.bucket, self.key, start, end - start + 1)
            self.send_response(HTTPStatus.PARTIAL_CONTENT)
            self.send_header("Content-Type", "application/vnd.pmtiles")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Content-Range", f"bytes {start}-{end}/{file_size}")
            self._send_cors()
            self.end_headers()
            self.wfile.write(data)
            return

        data = s3_get_bytes(self.bucket, self.key, 0, file_size)
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/vnd.pmtiles")
        self.send_header("Content-Length", str(len(data)))
        self._send_cors()
        self.end_headers()
        self.wfile.write(data)


def start_pmtiles_proxy_url(bucket: str, key: str) -> str:
    port = _pick_free_port()
    handler = type(
        "Handler",
        (_S3PMTilesProxyHandler,),
        {"bucket": bucket, "key": key},
    )
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", port), handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    filename = key.rsplit("/", 1)[-1]
    return f"http://127.0.0.1:{port}/{filename}"


def build_maplibre_style(
    *,
    pmtiles_url: str,
    source_layer: str,
    value_attribute: str,
    vmin: float,
    vmax: float,
    opacity: float,
    cmap_name: str,
    tile_max_zoom: int,
) -> dict[str, Any]:
    color_expr = build_fill_color_expression(
        value_attribute,
        vmin=vmin,
        vmax=vmax,
        cmap_name=cmap_name,
    )

    layer = {
        "id": "schism-indicator-fill",
        "source": "schism_indicator",
        "source-layer": source_layer,
        "type": "fill",
        "minzoom": 0,
        "maxzoom": tile_max_zoom + 1,
        "paint": {
            "fill-opacity": opacity,
            "fill-color": color_expr,
            "fill-outline-color": "rgba(0,0,0,0)",
            "fill-antialias": True,
        },
    }
    return {
        "version": 8,
        "sources": {
            "schism_indicator": {
                "type": "vector",
                "url": f"pmtiles://{pmtiles_url}",
                "maxzoom": tile_max_zoom,
            }
        },
        "layers": [layer],
    }
