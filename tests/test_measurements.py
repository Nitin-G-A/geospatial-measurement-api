"""Unit tests for CRS selection and measurement logic (no HTTP involved)."""
import pytest
from pyproj import CRS, Geod
from shapely.geometry import GeometryCollection, LineString, MultiPolygon, Point, Polygon

from app.services.crs import equal_area_projection, utm_epsg
from app.services.measurements import (
    STATUS_ERROR,
    STATUS_MEASURED,
    STATUS_NOT_APPLICABLE,
    STATUS_UNSUPPORTED,
    Measurer,
)

GEOD = Geod(ellps="WGS84")
WGS84 = CRS.from_epsg(4326)

BANGALORE_PLOT = Polygon([(77.59, 12.97), (77.60, 12.97), (77.60, 12.98), (77.59, 12.98)])


def geodesic_area(poly: Polygon) -> float:
    return abs(GEOD.geometry_area_perimeter(poly)[0])


@pytest.mark.parametrize(
    "lon, lat, expected",
    [
        (77.59, 12.97, 32643),  # Bangalore, zone 43N
        (151.2, -33.9, 32756),  # Sydney, zone 56S
        (-180.0, 0.0, 32601),  # western edge -> zone 1
        (179.999, 10.0, 32660),  # eastern edge -> zone 60
        (0.0, 89.0, 32661),  # UPS north
        (0.0, -85.0, 32761),  # UPS south
    ],
)
def test_utm_zone_selection(lon, lat, expected):
    assert utm_epsg(lon, lat) == expected


def test_polygon_area_is_in_square_meters_not_degrees():
    result = Measurer(WGS84).measure(BANGALORE_PLOT)

    assert result.status == STATUS_MEASURED
    assert result.area_m2 == pytest.approx(geodesic_area(BANGALORE_PLOT), rel=5e-3)
    # A naive calculation on degrees would give ~1e-4.
    assert result.area_m2 > 1e5
    assert result.projected_crs.startswith("LAEA")


def test_area_is_accurate_far_from_the_equator():
    poly = Polygon([(24.0, 68.0), (24.1, 68.0), (24.1, 68.05), (24.0, 68.05)])
    result = Measurer(WGS84).measure(poly)
    assert result.area_m2 == pytest.approx(geodesic_area(poly), rel=5e-3)


def test_linestring_length_matches_geodesic_distance():
    line = LineString([(77.59, 12.97), (77.62, 13.0), (77.65, 13.02)])
    result = Measurer(WGS84).measure(line)

    assert result.status == STATUS_MEASURED
    assert result.length_m == pytest.approx(GEOD.geometry_length(line), rel=5e-3)
    assert result.projected_crs == "EPSG:32643"


def test_polygon_with_hole_subtracts_the_hole():
    hole = [(77.593, 12.973), (77.597, 12.973), (77.597, 12.977), (77.593, 12.977)]
    with_hole = Polygon(BANGALORE_PLOT.exterior.coords, [hole])
    result = Measurer(WGS84).measure(with_hole)
    expected = geodesic_area(BANGALORE_PLOT) - geodesic_area(Polygon(hole))
    assert result.area_m2 == pytest.approx(expected, rel=5e-3)


def test_multipolygon_area_is_summed():
    other = Polygon([(77.70, 12.97), (77.71, 12.97), (77.71, 12.98), (77.70, 12.98)])
    result = Measurer(WGS84).measure(MultiPolygon([BANGALORE_PLOT, other]))
    expected = geodesic_area(BANGALORE_PLOT) + geodesic_area(other)
    assert result.area_m2 == pytest.approx(expected, rel=5e-3)


def test_point_needs_no_measurement():
    result = Measurer(WGS84).measure(Point(77.59, 12.97))
    assert result.status == STATUS_NOT_APPLICABLE
    assert result.area_m2 is None and result.length_m is None


def test_unsupported_geometry_is_reported_not_raised():
    collection = GeometryCollection([Point(0, 0), LineString([(0, 0), (1, 1)])])
    result = Measurer(WGS84).measure(collection)
    assert result.status == STATUS_UNSUPPORTED
    assert "GeometryCollection" in result.note


def test_missing_and_empty_geometries_are_unsupported():
    measurer = Measurer(WGS84)
    assert measurer.measure(None).status == STATUS_UNSUPPORTED
    assert measurer.measure(Polygon()).status == STATUS_UNSUPPORTED


def test_projected_crs_uses_native_units_and_converts_feet():
    # NAD83 / New York Long Island (ftUS): a 100 ft x 100 ft square.
    crs = CRS.from_epsg(2263)
    square = Polygon([(0, 0), (100, 0), (100, 100), (0, 100)])
    result = Measurer(crs).measure(square)

    foot = 1200 / 3937  # US survey foot in metres
    assert result.status == STATUS_MEASURED
    assert result.area_m2 == pytest.approx(100 * 100 * foot**2, rel=1e-6)
    assert Measurer(crs).measure(LineString([(0, 0), (300, 400)])).length_m == pytest.approx(
        500 * foot, rel=1e-6
    )


def test_projected_metric_crs_is_measured_in_place():
    crs = CRS.from_epsg(32643)
    square = Polygon([(500000, 1400000), (500100, 1400000), (500100, 1400100), (500000, 1400100)])
    result = Measurer(crs).measure(square)
    assert result.area_m2 == pytest.approx(10_000.0)
    assert result.projected_crs == "EPSG:32643"


def test_out_of_range_coordinates_in_geographic_crs_are_flagged():
    # Looks like UTM metres but is declared as lon/lat (e.g. a missing .prj).
    bogus = Polygon([(500000, 1400000), (500100, 1400000), (500100, 1400100)])
    result = Measurer(WGS84).measure(bogus)
    assert result.status == STATUS_UNSUPPORTED
    assert "CRS" in result.note


def test_invalid_polygon_is_measured_with_a_warning_note():
    bowtie = Polygon([(77.59, 12.97), (77.60, 12.98), (77.60, 12.97), (77.59, 12.98)])
    result = Measurer(WGS84).measure(bowtie)
    assert result.status in (STATUS_MEASURED, STATUS_ERROR)
    if result.status == STATUS_MEASURED:
        assert "invalid" in result.note.lower()


def test_equal_area_projection_label_contains_centre():
    choice = equal_area_projection(77.5912, 12.9734)
    assert "lat_0=12.97" in choice.definition
    assert "lon_0=77.59" in choice.definition
