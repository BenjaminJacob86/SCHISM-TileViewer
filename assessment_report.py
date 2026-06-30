"""Shared assessment helpers (legend, PDF) — no Streamlit side effects on import."""

from __future__ import annotations

import html as _html
import io
from typing import Optional

import folium
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from matplotlib.backends.backend_pdf import PdfPages


def add_legend_box(m: folium.Map, *, title: str, items: list[tuple[str, str]]) -> None:
    safe_title = _html.escape(title)
    rows = "\n".join(
        f"""
        <div style="display:flex; align-items:center; gap:8px; margin:4px 0;">
          <span style="width:14px; height:14px; border:1px solid rgba(0,0,0,0.25); background:{color}; display:inline-block;"></span>
          <span style="font-size:12px; color:#111;">{_html.escape(label)}</span>
        </div>
        """.strip()
        for color, label in items
    )

    html = f"""
    <div style="
      position: fixed;
      top: 22px;
      right: 22px;
      z-index: 9999;
      background: rgba(255,255,255,0.92);
      border: 1px solid rgba(0,0,0,0.2);
      border-radius: 8px;
      padding: 10px 12px;
      box-shadow: 0 4px 16px rgba(0,0,0,0.15);
      max-width: 260px;
      color: #111;
      font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Arial, sans-serif;
    ">
      <div style="font-weight:600; font-size:13px; margin-bottom:6px;">{safe_title}</div>
      {rows}
    </div>
    """
    m.get_root().html.add_child(folium.Element(html))


def build_assessment_pdf_bytes(
    *,
    variable: str,
    critical_value: float,
    stats: dict,
    hist_df: pd.DataFrame,
    raster_rgba: np.ndarray,
    raster_bounds: tuple[float, float, float, float],
    threshold_rgba: Optional[np.ndarray] = None,
    threshold_bounds: Optional[tuple[float, float, float, float]] = None,
    geom: Optional[dict] = None,
) -> bytes:
    def _plot_geom(ax, g: dict) -> None:
        if not g or "type" not in g:
            return
        gtype = g["type"]
        coords = g.get("coordinates")
        if not coords:
            return

        def _plot_ring(ring):
            xs = [pt[0] for pt in ring]
            ys = [pt[1] for pt in ring]
            ax.plot(xs, ys, color="black", linewidth=1.2, alpha=0.8)

        if gtype == "Polygon":
            for ring in coords:
                _plot_ring(ring)
        elif gtype == "MultiPolygon":
            for poly in coords:
                for ring in poly:
                    _plot_ring(ring)

    buf = io.BytesIO()
    with PdfPages(buf) as pdf:
        fig1 = plt.figure(figsize=(8.27, 11.69))
        fig1.suptitle("Assessment", fontsize=16, y=0.98)

        ax_text = fig1.add_axes([0.08, 0.76, 0.84, 0.18])
        ax_text.axis("off")
        ax_text.text(0.0, 1.0, f"Variable: {variable}", fontsize=12, va="top")
        ax_text.text(0.0, 0.78, f"Critical value: {critical_value:g}", fontsize=12, va="top")

        if stats.get("count", 0) > 0:
            ax_text.text(0.0, 0.52, f"Count: {stats['count']}", fontsize=11, va="top")
            ax_text.text(0.0, 0.36, f"Mean: {stats['mean']:.6g}", fontsize=11, va="top")
            ax_text.text(0.0, 0.20, f"Min:  {stats['min']:.6g}", fontsize=11, va="top")
            ax_text.text(0.0, 0.04, f"Max:  {stats['max']:.6g}", fontsize=11, va="top")

        above = int(stats.get("above_count", 0))
        below = int(stats.get("below_or_equal_count", 0))
        total = max(int(stats.get("count", 0)), 1)
        sizes = [below, above]
        labels = ["≤ critical", "> critical"]
        colors = ["#2b6cb0", "#e53e3e"]

        ax_donut = fig1.add_axes([0.20, 0.40, 0.60, 0.32])
        wedges, _ = ax_donut.pie(
            sizes,
            colors=colors,
            startangle=90,
            wedgeprops=dict(width=0.35, edgecolor="white"),
        )
        ax_donut.set_aspect("equal")
        ax_donut.set_title("Area % above/below critical", fontsize=12)
        pct_below = below / total * 100.0
        pct_above = above / total * 100.0
        ax_donut.legend(
            wedges,
            [f"{labels[0]}: {pct_below:.1f}% ({below})", f"{labels[1]}: {pct_above:.1f}% ({above})"],
            loc="lower center",
            bbox_to_anchor=(0.5, -0.15),
            fontsize=10,
            frameon=False,
        )

        pdf.savefig(fig1)
        plt.close(fig1)

        fig2, ax2 = plt.subplots(figsize=(11.69, 8.27))
        ax2.set_title("Histogram inside polygon", fontsize=14)
        ax2.set_xlabel(variable)
        ax2.set_ylabel("Count")

        if not hist_df.empty:
            x = hist_df.index.to_numpy(dtype=float)
            y = hist_df["count"].to_numpy(dtype=float)
            ax2.bar(x, y, width=0.018, color="#4a5568")
            ax2.axvline(float(critical_value), color="red", linewidth=2, label="critical value")
            ax2.legend(loc="upper right")
        else:
            ax2.text(0.5, 0.5, "No histogram data", ha="center", va="center")

        pdf.savefig(fig2)
        plt.close(fig2)

        south, west, north, east = raster_bounds
        fig3, ax3 = plt.subplots(figsize=(11.69, 8.27))
        ax3.set_title(f"Map preview ({variable})", fontsize=14)
        ax3.imshow(
            raster_rgba,
            extent=[west, east, south, north],
            origin="upper",
            interpolation="nearest",
        )
        _plot_geom(ax3, geom)
        ax3.set_xlabel("Longitude")
        ax3.set_ylabel("Latitude")
        pdf.savefig(fig3)
        plt.close(fig3)

        if threshold_rgba is not None and threshold_bounds is not None:
            tsouth, twest, tnorth, teast = threshold_bounds
            fig4, ax4 = plt.subplots(figsize=(11.69, 8.27))
            ax4.set_title("Threshold map (≥ critical value in red)", fontsize=14)
            ax4.imshow(
                threshold_rgba,
                extent=[twest, teast, tsouth, tnorth],
                origin="upper",
                interpolation="nearest",
            )
            _plot_geom(ax4, geom)
            ax4.set_xlabel("Longitude")
            ax4.set_ylabel("Latitude")
            pdf.savefig(fig4)
            plt.close(fig4)

    buf.seek(0)
    return buf.read()
