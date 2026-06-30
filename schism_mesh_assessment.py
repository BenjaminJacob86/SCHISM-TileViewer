"""Polygon statistics on SCHISM indicator NetCDF (unstructured triangle mesh)."""

from __future__ import annotations

import io
from dataclasses import dataclass
from typing import Any

import matplotlib.pyplot as plt
import numpy as np
import xarray as xr
from matplotlib.collections import PolyCollection
from shapely.geometry import Polygon, shape
from shapely.prepared import prep

from schism_mesh_index import candidate_face_indices

NODE_DIM = "nSCHISM_hgrid_node"
EARTH_RADIUS_KM = 6371.0088

# Threshold map colours (below: light blue fill + blue outline; above: red fill).
THRESHOLD_BELOW_FACE = "#bee3f8"
THRESHOLD_BELOW_EDGE = "#2b6cb0"
THRESHOLD_ABOVE_FACE = "#e53e3e"
THRESHOLD_ABOVE_EDGE = "#c53030"


@dataclass(frozen=True)
class MeshContext:
    lon: np.ndarray
    lat: np.ndarray
    face_nodes: np.ndarray
    values: np.ndarray


def _looks_like_wgs84(lon: np.ndarray, lat: np.ndarray) -> bool:
    """True only if coordinates fall within plausible WGS84 lon/lat ranges.

    Projected SCHISM_hgrid_node_x/y (e.g. metres) have a wide span but values far
    outside +/-180 / +/-90, so a span-only check is not enough.
    """
    finite_lon = lon[np.isfinite(lon)]
    finite_lat = lat[np.isfinite(lat)]
    if finite_lon.size == 0 or finite_lat.size == 0:
        return False
    lon_span = float(finite_lon.max() - finite_lon.min())
    lat_span = float(finite_lat.max() - finite_lat.min())
    if lon_span <= 1e-4 or lat_span <= 1e-4:
        return False
    return (
        finite_lon.min() >= -180.0
        and finite_lon.max() <= 180.0
        and finite_lat.min() >= -90.0
        and finite_lat.max() <= 90.0
    )


def mesh_lon_lat_from_dataset(ds: xr.Dataset) -> tuple[np.ndarray, np.ndarray]:
    if "node_lon" in ds.coords and "node_lat" in ds.coords:
        return (
            np.asarray(ds["node_lon"].values, dtype=np.float64),
            np.asarray(ds["node_lat"].values, dtype=np.float64),
        )
    lon_name, lat_name = "SCHISM_hgrid_node_x", "SCHISM_hgrid_node_y"
    if lon_name not in ds.coords or lat_name not in ds.coords:
        raise KeyError(
            "NetCDF needs node_lon/node_lat (WGS84) or SCHISM_hgrid_node_x/y coordinates."
        )
    lon = np.asarray(ds[lon_name].values, dtype=np.float64)
    lat = np.asarray(ds[lat_name].values, dtype=np.float64)
    if not _looks_like_wgs84(lon, lat):
        raise ValueError(
            "This indicator NetCDF has no WGS84 node_lon/node_lat coordinates; its "
            "SCHISM_hgrid_node_x/y look projected "
            f"(x range [{float(np.nanmin(lon)):.1f}, {float(np.nanmax(lon)):.1f}], "
            f"y range [{float(np.nanmin(lat)):.1f}, {float(np.nanmax(lat)):.1f}]). "
            "Re-export the indicator with the updated pipeline so node_lon/node_lat "
            "(degrees) are embedded, then re-upload the .nc to S3."
        )
    return lon, lat


def _face_nodes_zero_based(ds: xr.Dataset) -> np.ndarray:
    fn = np.asarray(ds["SCHISM_hgrid_face_nodes"].values)
    if fn.ndim != 2:
        raise ValueError(f"SCHISM_hgrid_face_nodes must be 2-D, got {fn.shape}")
    tri = np.round(fn[:, :3]).astype(np.int64)
    if tri.min() >= 1:
        tri = tri - 1
    return tri


def _face_values(node_values: np.ndarray, face_nodes: np.ndarray) -> np.ndarray:
    verts = node_values[face_nodes]
    return np.nanmean(verts, axis=1)


def _triangle_area_km2(lon: np.ndarray, lat: np.ndarray) -> float:
    lat_c = float(np.mean(lat))
    m_per_deg_lon = 111_320.0 * np.cos(np.deg2rad(lat_c))
    m_per_deg_lat = 111_320.0
    x = lon * m_per_deg_lon
    y = lat * m_per_deg_lat
    area_m2 = 0.5 * abs(
        np.dot(x, np.roll(y, -1)) - np.dot(y, np.roll(x, -1))
    )
    return float(area_m2) / 1_000_000.0


def _face_indices_for_polygon(
    face_nodes: np.ndarray,
    values: np.ndarray,
    lon: np.ndarray,
    lat: np.ndarray,
    user_poly,
    mesh_bbox: np.ndarray | None,
) -> np.ndarray:
    minx, miny, maxx, maxy = user_poly.bounds
    if mesh_bbox is not None and len(mesh_bbox) == len(face_nodes):
        candidates = candidate_face_indices(mesh_bbox, (minx, miny, maxx, maxy))
    else:
        candidates = np.arange(len(face_nodes), dtype=np.int64)

    prepared = prep(user_poly)
    selected: list[int] = []
    for i in candidates:
        val = values[i]
        if not np.isfinite(val):
            continue
        fn = face_nodes[i]
        tri_lon = lon[fn]
        tri_lat = lat[fn]
        if (
            float(tri_lon.max()) < minx
            or float(tri_lon.min()) > maxx
            or float(tri_lat.max()) < miny
            or float(tri_lat.min()) > maxy
        ):
            continue
        tri = Polygon(zip(tri_lon, tri_lat, strict=True))
        if tri.is_empty or not tri.is_valid or not prepared.intersects(tri):
            continue
        selected.append(int(i))
    return np.asarray(selected, dtype=np.int64)


def load_mesh_context_from_nc_bytes(nc_bytes: bytes, variable: str) -> MeshContext:
    with xr.open_dataset(io.BytesIO(nc_bytes)) as ds:
        if variable not in ds:
            raise KeyError(f"Variable {variable!r} not in {list(ds.data_vars)}")
        da = ds[variable]
        if NODE_DIM not in da.dims:
            raise ValueError(f"{variable} must be on {NODE_DIM}, got {da.dims}")
        node_values = np.asarray(da.values, dtype=np.float64)
        lon, lat = mesh_lon_lat_from_dataset(ds)
        face_nodes = _face_nodes_zero_based(ds)
        values = _face_values(node_values, face_nodes)
    return MeshContext(lon=lon, lat=lat, face_nodes=face_nodes, values=values)


def _stats_from_selection(
    mesh: MeshContext,
    selected_idx: np.ndarray,
    *,
    critical_value: float,
    n_hist_bins: int,
) -> dict[str, Any]:
    selected_vals: list[float] = []
    selected_areas: list[float] = []

    for i in selected_idx:
        fn = mesh.face_nodes[i]
        val = float(mesh.values[i])
        tri_lon = mesh.lon[fn]
        tri_lat = mesh.lat[fn]
        area_km2 = _triangle_area_km2(tri_lon, tri_lat)
        if area_km2 <= 0:
            continue
        selected_vals.append(val)
        selected_areas.append(area_km2)

    if not selected_vals:
        return {"count": 0}

    vals = np.asarray(selected_vals, dtype=np.float64)
    areas = np.asarray(selected_areas, dtype=np.float64)
    total_area = float(areas.sum())
    weighted_mean = float(np.sum(vals * areas) / total_area)

    above = vals > float(critical_value)
    area_above = float(areas[above].sum())
    area_below = total_area - area_above

    counts, edges = np.histogram(vals, bins=n_hist_bins)
    hist_area, _ = np.histogram(vals, bins=edges, weights=areas)

    return {
        "count": int(vals.size),
        "mean": weighted_mean,
        "min": float(vals.min()),
        "max": float(vals.max()),
        "area_km2": total_area,
        "area_above_km2": area_above,
        "area_below_or_equal_km2": area_below,
        "above_count": int(np.count_nonzero(above)),
        "below_or_equal_count": int(np.count_nonzero(~above)),
        "hist_counts": counts,
        "hist_area": hist_area,
        "hist_edges": edges,
    }


def render_threshold_rgba(
    mesh: MeshContext,
    selected_idx: np.ndarray,
    *,
    critical_value: float,
    bounds: tuple[float, float, float, float],
    max_size: int = 1024,
    pad_frac: float = 0.03,
) -> tuple[np.ndarray, tuple[float, float, float, float]]:
    """
    Rasterize selected triangles to RGBA (fast Folium ImageOverlay).

    Below threshold: light blue fill, blue edges.
    Above threshold: red fill.
    """
    minx, miny, maxx, maxy = bounds
    pad_x = (maxx - minx) * pad_frac
    pad_y = (maxy - miny) * pad_frac
    minx -= pad_x
    maxx += pad_x
    miny -= pad_y
    maxy += pad_y

    span_x = max(maxx - minx, 1e-6)
    span_y = max(maxy - miny, 1e-6)
    aspect = span_x / span_y
    if aspect >= 1.0:
        width_px = max_size
        height_px = max(1, int(max_size / aspect))
    else:
        height_px = max_size
        width_px = max(1, int(max_size * aspect))

    polys_below: list[np.ndarray] = []
    polys_above: list[np.ndarray] = []
    crit = float(critical_value)
    for i in selected_idx:
        fn = mesh.face_nodes[i]
        poly = np.column_stack((mesh.lon[fn], mesh.lat[fn]))
        if float(mesh.values[i]) >= crit:
            polys_above.append(poly)
        else:
            polys_below.append(poly)

    dpi = 100.0
    fig = plt.figure(figsize=(width_px / dpi, height_px / dpi), dpi=dpi)
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_xlim(minx, maxx)
    ax.set_ylim(miny, maxy)
    ax.axis("off")
    fig.patch.set_alpha(0.0)
    ax.patch.set_alpha(0.0)

    if polys_below:
        ax.add_collection(
            PolyCollection(
                polys_below,
                facecolors=THRESHOLD_BELOW_FACE,
                edgecolors=THRESHOLD_BELOW_EDGE,
                linewidths=0.45,
                alpha=0.88,
                zorder=1,
            )
        )
    if polys_above:
        ax.add_collection(
            PolyCollection(
                polys_above,
                facecolors=THRESHOLD_ABOVE_FACE,
                edgecolors=THRESHOLD_ABOVE_EDGE,
                linewidths=0.35,
                alpha=0.92,
                zorder=2,
            )
        )

    fig.canvas.draw()
    rgba = np.asarray(fig.canvas.buffer_rgba(), dtype=np.uint8).copy()
    plt.close(fig)
    return rgba, (miny, minx, maxy, maxx)


def polygon_assessment(
    nc_bytes: bytes,
    geom: dict[str, Any],
    *,
    variable: str,
    critical_value: float,
    n_hist_bins: int = 40,
    mesh_index_bbox: np.ndarray | None = None,
    render_threshold: bool = True,
    threshold_max_size: int = 1024,
) -> dict[str, Any]:
    """
    One NetCDF read + one polygon face selection for stats and threshold map.
    """
    user_poly = shape(geom)
    if user_poly.is_empty:
        return {"stats": {"count": 0}}

    mesh = load_mesh_context_from_nc_bytes(nc_bytes, variable)
    selected_idx = _face_indices_for_polygon(
        mesh.face_nodes,
        mesh.values,
        mesh.lon,
        mesh.lat,
        user_poly,
        mesh_index_bbox,
    )
    stats = _stats_from_selection(
        mesh, selected_idx, critical_value=critical_value, n_hist_bins=n_hist_bins
    )
    result: dict[str, Any] = {"stats": stats, "selected_count": int(selected_idx.size)}

    if render_threshold and selected_idx.size and stats.get("count", 0) > 0:
        rgba, thr_bounds = render_threshold_rgba(
            mesh,
            selected_idx,
            critical_value=critical_value,
            bounds=user_poly.bounds,
            max_size=threshold_max_size,
        )
        result["threshold_rgba"] = rgba
        result["threshold_bounds"] = thr_bounds

    return result


def polygon_stats_from_nc_bytes(
    nc_bytes: bytes,
    geom: dict[str, Any],
    *,
    variable: str,
    critical_value: float,
    n_hist_bins: int = 40,
    mesh_index_bbox: np.ndarray | None = None,
) -> dict[str, Any]:
    return polygon_assessment(
        nc_bytes,
        geom,
        variable=variable,
        critical_value=critical_value,
        n_hist_bins=n_hist_bins,
        mesh_index_bbox=mesh_index_bbox,
        render_threshold=False,
    )["stats"]


def threshold_features_inside_polygon(
    nc_bytes: bytes,
    geom: dict[str, Any],
    *,
    variable: str,
    critical_value: float,
    mesh_index_bbox: np.ndarray | None = None,
) -> dict[str, Any]:
    """Legacy GeoJSON threshold output (slow for large selections)."""
    from shapely.geometry import mapping

    user_poly = shape(geom)
    if user_poly.is_empty:
        return {"type": "FeatureCollection", "features": []}

    mesh = load_mesh_context_from_nc_bytes(nc_bytes, variable)
    selected_idx = _face_indices_for_polygon(
        mesh.face_nodes,
        mesh.values,
        mesh.lon,
        mesh.lat,
        user_poly,
        mesh_index_bbox,
    )
    features: list[dict[str, Any]] = []
    crit = float(critical_value)
    for i in selected_idx:
        fn = mesh.face_nodes[i]
        val = float(mesh.values[i])
        tri_lon = mesh.lon[fn]
        tri_lat = mesh.lat[fn]
        tri = Polygon(zip(tri_lon, tri_lat, strict=True))
        above = val >= crit
        features.append(
            {
                "type": "Feature",
                "properties": {"value": val, "above": above},
                "geometry": mapping(tri),
            }
        )
    return {"type": "FeatureCollection", "features": features}
