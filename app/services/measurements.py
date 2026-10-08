"""Area / length calculation with correct CRS handling."""
from __future__ import annotations

import math
from dataclasses import dataclass

from pyproj import CRS
from shapely.geometry.base import BaseGeometry
from shapely.ops import transform
from shapely.validation import explain_validity

from app.services import crs as crs_utils

STATUS_MEASURED = "MEASURED"
STATUS_NOT_APPLICABLE = "NOT_APPLICABLE"  # e.g. points
STATUS_UNSUPPORTED = "UNSUPPORTED"  # e.g. geometry collections, missing geometry
STATUS_ERROR = "ERROR"  # unreadable geometry or a failure while measuring

AREA_TYPES = {"Polygon", "MultiPolygon"}
LINE_TYPES = {"LineString", "LinearRing", "MultiLineString"}
POINT_TYPES = {"Point", "MultiPoint"}


@dataclass
class Measurement:
    status: str
    area_m2: float | None = None
    length_m: float | None = None
    projected_crs: str | None = None
    note: str | None = None


class Measurer:
    """Measures geometries that are expressed in one source CRS."""

    def __init__(self, crs: CRS):
        self.crs = crs
        self._wkt = crs.to_wkt()
        self._unit_factor = crs_utils.meters_per_unit(crs) if crs.is_projected else 1.0

    def measure(self, geom: BaseGeometry | None) -> Measurement:
        if geom is None:
            return Measurement(STATUS_UNSUPPORTED, note="Feature has no geometry")
        if geom.is_empty:
            return Measurement(STATUS_UNSUPPORTED, note="Geometry is empty")

        gtype = geom.geom_type
        if gtype in POINT_TYPES:
            return Measurement(STATUS_NOT_APPLICABLE, note="No measurement is defined for points")
        if gtype not in AREA_TYPES and gtype not in LINE_TYPES:
            return Measurement(
                STATUS_UNSUPPORTED,
                note=f"Measurement is not supported for {gtype} geometries",
            )

        try:
            return self._measure_supported(geom, gtype in AREA_TYPES)
        except Exception as exc:  # never let one bad feature break the whole file
            return Measurement(STATUS_ERROR, note=f"Measurement failed: {exc}")

    # ------------------------------------------------------------------ internals

    def _measure_supported(self, geom: BaseGeometry, wants_area: bool) -> Measurement:
        if self.crs.is_projected:
            # Already metric (or at least planar): measure in place, convert units.
            projected = geom
            label = crs_utils.crs_label(self.crs)
            factor = self._unit_factor
        elif self.crs.is_geographic:
            problem = crs_utils.geographic_range_problem(geom)
            if problem:
                return Measurement(STATUS_UNSUPPORTED, note=problem)
            lon, lat = crs_utils.representative_lonlat(geom, self._wkt)
            choice = (
                crs_utils.equal_area_projection(lon, lat)
                if wants_area
                else crs_utils.utm_projection(lon, lat)
            )
            transformer = crs_utils.get_transformer(self._wkt, choice.definition)
            projected = transform(transformer.transform, geom)
            label = choice.label
            factor = 1.0
        else:
            return Measurement(
                STATUS_UNSUPPORTED, note="CRS is neither geographic nor projected"
            )

        if wants_area:
            value = projected.area * factor**2
            _require_finite(value)
            note = None
            if not geom.is_valid:
                note = (
                    f"Geometry is invalid ({explain_validity(geom)}); "
                    "area may be unreliable"
                )
            return Measurement(STATUS_MEASURED, area_m2=value, projected_crs=label, note=note)

        value = projected.length * factor
        _require_finite(value)
        return Measurement(STATUS_MEASURED, length_m=value, projected_crs=label)


def _require_finite(value: float) -> None:
    if not math.isfinite(value):
        raise ValueError("projection produced non-finite coordinates")
