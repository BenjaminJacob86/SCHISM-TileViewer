"""Precomputed triangle bounding boxes for fast mesh polygon queries."""

from __future__ import annotations

import io
from pathlib import Path

import numpy as np
import xarray as xr

MESH_INDEX_FILENAME = "mesh_spatial_index.npz"
MESH_INDEX_VERSION = 1
NODE_DIM = "nSCHISM_hgrid_node"


def face_nodes_zero_based_array(face_nodes: np.ndarray) -> np.ndarray:
    tri = np.round(face_nodes[:, :3]).astype(np.int64)
    if tri.size and tri.min() >= 1:
        tri = tri - 1
    return tri


def build_triangle_bbox(
    lon: np.ndarray, lat: np.ndarray, face_nodes: np.ndarray
) -> np.ndarray:
    """Return (n_faces, 4) array: minx, miny, maxx, maxy in degrees."""
    tri = face_nodes_zero_based_array(face_nodes)
    tri_lon = lon[tri]
    tri_lat = lat[tri]
    return np.column_stack(
        (
            tri_lon.min(axis=1),
            tri_lat.min(axis=1),
            tri_lon.max(axis=1),
            tri_lat.max(axis=1),
        )
    ).astype(np.float64)


def build_mesh_index_from_grid(grid: dict[str, np.ndarray]) -> np.ndarray:
    if "node_lon" not in grid or "node_lat" not in grid:
        raise KeyError("grid must include node_lon and node_lat for mesh index export")
    return build_triangle_bbox(grid["node_lon"], grid["node_lat"], grid["face_nodes"])


def mesh_index_to_bytes(bbox: np.ndarray) -> bytes:
    buf = io.BytesIO()
    np.savez_compressed(
        buf,
        bbox=bbox.astype(np.float32),
        version=np.int32(MESH_INDEX_VERSION),
    )
    return buf.getvalue()


def mesh_index_from_bytes(data: bytes) -> np.ndarray:
    with np.load(io.BytesIO(data)) as archive:
        return np.asarray(archive["bbox"], dtype=np.float64)


def build_mesh_index_bytes_from_nc(nc_bytes: bytes) -> bytes:
    with xr.open_dataset(io.BytesIO(nc_bytes)) as ds:
        if "node_lon" not in ds.coords or "node_lat" not in ds.coords:
            raise KeyError(
                "NetCDF needs node_lon/node_lat to build mesh_spatial_index.npz"
            )
        lon = np.asarray(ds["node_lon"].values, dtype=np.float64)
        lat = np.asarray(ds["node_lat"].values, dtype=np.float64)
        face_nodes = np.asarray(ds["SCHISM_hgrid_face_nodes"].values)
    bbox = build_triangle_bbox(lon, lat, face_nodes)
    return mesh_index_to_bytes(bbox)


def write_mesh_spatial_index(path: Path, grid: dict[str, np.ndarray]) -> Path:
    bbox = build_mesh_index_from_grid(grid)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as fh:
        fh.write(mesh_index_to_bytes(bbox))
    print(
        f"Wrote mesh spatial index: {path} "
        f"({bbox.shape[0]:,} triangles, {path.stat().st_size / 1_048_576:.1f} MiB)"
    )
    return path


def candidate_face_indices(
    bbox: np.ndarray,
    bounds: tuple[float, float, float, float],
) -> np.ndarray:
    """Face indices whose axis-aligned bounds intersect the query box (minx, miny, maxx, maxy)."""
    minx, miny, maxx, maxy = bounds
    hits = (
        (bbox[:, 2] >= minx)
        & (bbox[:, 0] <= maxx)
        & (bbox[:, 3] >= miny)
        & (bbox[:, 1] <= maxy)
    )
    return np.nonzero(hits)[0]
