"""CRS helpers: labelling, transformer caching and projected-CRS selection.

Strategy for geographic (lon/lat) data
--------------------------------------
Degrees are not a unit of length or area, so geometries are reprojected to a
metre-based CRS chosen *per feature* before measuring:

* Areas   -> a Lambert Azimuthal Equal-Area (LAEA) projection centred on the
             feature. Equal-area by construction, so the result is accurate
             wherever on Earth the feature is, with no zone edges.
* Lengths -> the UTM zone (WGS 84) containing the feature's centroid, or UPS
             near the poles. UTM keeps scale error below ~0.04 % in-zone.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from functools import lru_cache

from pyproj import CRS, Transformer
from shapely.geometry.base import BaseGeometry

WGS84_EPSG = "EPSG:4326"
_EPS = 1e-9


@dataclass(frozen=True)
class ProjectionChoice:
    definition: str  # anything pyproj.CRS.from_user_input accepts
    label: str  # human-readable name returned by the API


def crs_label(crs: CRS) -> str:
    """'EPSG:4326' when the CRS maps to an authority code, else its name."""
    try:
        authority = crs.to_authority(min_confidence=70)
    except Exception:  # pragma: no cover - defensive, pyproj internals
        authority = None
    if authority:
        return f"{authority[0]}:{authority[1]}"
    return crs.name or "UNKNOWN"


def meters_per_unit(crs: CRS) -> float:
    """Linear unit of a projected CRS expressed in metres (e.g. US ft -> 0.3048006)."""
    try:
        factor = crs.axis_info[0].unit_conversion_factor
        return float(factor) if factor else 1.0
    except Exception:  # pragma: no cover - defensive
        return 1.0


@lru_cache(maxsize=1024)
def get_transformer(src_wkt: str, dst_definition: str) -> Transformer:
    """Cached transformer; always_xy so inputs are (lon, lat) / (easting, northing)."""
    return Transformer.from_crs(
        CRS.from_wkt(src_wkt), CRS.from_user_input(dst_definition), always_xy=True
    )


def geographic_range_problem(geom: BaseGeometry) -> str | None:
    """Detects data that claims to be lon/lat but clearly is not (e.g. missing .prj)."""
    minx, miny, maxx, maxy = geom.bounds
    if (
        minx < -180 - _EPS
        or maxx > 180 + _EPS
        or miny < -90 - _EPS
        or maxy > 90 + _EPS
    ):
        return (
            "Coordinates fall outside the valid longitude/latitude range; "
            "the file's CRS is probably missing or wrong"
        )
    return None


def representative_lonlat(geom: BaseGeometry, src_wkt: str) -> tuple[float, float]:
    """Centroid of the geometry as WGS 84 (lon, lat), longitude normalised to [-180, 180)."""
    centroid = geom.centroid
    transformer = get_transformer(src_wkt, WGS84_EPSG)
    lon, lat = transformer.transform(centroid.x, centroid.y)
    if not (math.isfinite(lon) and math.isfinite(lat)):
        raise ValueError("could not determine a representative location for the geometry")
    lon = ((lon + 180.0) % 360.0) - 180.0
    lat = max(-90.0, min(90.0, lat))
    return lon, lat


def utm_epsg(lon: float, lat: float) -> int:
    """EPSG code of the WGS 84 UTM zone for a location (UPS beyond 84N / 80S)."""
    if lat >= 84.0:
        return 32661  # WGS 84 / UPS North
    if lat <= -80.0:
        return 32761  # WGS 84 / UPS South
    zone = int(math.floor((lon + 180.0) / 6.0)) + 1
    zone = min(max(zone, 1), 60)
    return (32600 if lat >= 0 else 32700) + zone


def utm_projection(lon: float, lat: float) -> ProjectionChoice:
    code = utm_epsg(lon, lat)
    return ProjectionChoice(definition=f"EPSG:{code}", label=f"EPSG:{code}")


def equal_area_projection(lon: float, lat: float) -> ProjectionChoice:
    # Rounding keeps the number of distinct transformers (and cache misses) small;
    # the centre only affects shape distortion, never the equal-area property.
    lon0, lat0 = round(lon, 2), round(lat, 2)
    definition = (
        f"+proj=laea +lat_0={lat0} +lon_0={lon0} +x_0=0 +y_0=0 "
        "+datum=WGS84 +units=m +no_defs"
    )
    return ProjectionChoice(
        definition=definition,
        label=f"LAEA(lat_0={lat0}, lon_0={lon0}, WGS84)",
    )
