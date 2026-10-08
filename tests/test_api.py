"""End-to-end tests through the HTTP API."""
import io
import zipfile

import pytest
from pyproj import CRS, Geod
from shapely.geometry import LineString, Polygon

from tests.helpers import (
    kml,
    kml_line,
    kml_point,
    kml_polygon,
    zip_lines,
    zip_points,
    zip_polygons,
)

GEOD = Geod(ellps="WGS84")
WGS84 = CRS.from_epsg(4326)
UTM43 = CRS.from_epsg(32643)

PLOT_RING = "77.59,12.97,0 77.60,12.97,0 77.60,12.98,0 77.59,12.98,0 77.59,12.97,0"
PLOT = Polygon([(77.59, 12.97), (77.60, 12.97), (77.60, 12.98), (77.59, 12.98)])
LINE_COORDS = "77.59,12.97,0 77.62,13.00,0 77.65,13.02,0"
LINE = LineString([(77.59, 12.97), (77.62, 13.0), (77.65, 13.02)])


def upload(client, name: str, content: bytes):
    return client.post("/api/files/", files={"file": (name, content, "application/octet-stream")})


def measurements(client, file_id: str, **params):
    response = client.get(f"/api/files/{file_id}/measurements/", params=params)
    assert response.status_code == 200, response.text
    return response.json()


# ----------------------------------------------------------------------- KML


def test_kml_upload_file_info_and_measurements(client):
    content = kml(
        kml_polygon("Plot A", PLOT_RING),
        kml_line("Path", LINE_COORDS),
        kml_point("Marker", "77.595,12.975,0"),
    )
    response = upload(client, "survey.kml", content)

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["filename"] == "survey.kml"
    assert body["feature_count"] == 3
    assert body["crs"] == "EPSG:4326"
    assert body["status"] == "COMPLETED"

    info = client.get(f"/api/files/{body['id']}/")
    assert info.status_code == 200
    assert info.json()["id"] == body["id"]

    data = measurements(client, body["id"])
    polygon, line, point = data["features"]

    assert polygon["index"] == 0
    assert polygon["geometry_type"] == "Polygon"
    assert polygon["crs"] == "EPSG:4326"
    assert polygon["properties"]["name"] == "Plot A"
    assert polygon["measurement"]["status"] == "MEASURED"
    assert polygon["measurement"]["area_m2"] == pytest.approx(
        abs(GEOD.geometry_area_perimeter(PLOT)[0]), rel=5e-3
    )

    assert line["geometry_type"] == "LineString"
    assert line["measurement"]["length_m"] == pytest.approx(GEOD.geometry_length(LINE), rel=5e-3)
    assert line["measurement"]["projected_crs"] == "EPSG:32643"

    assert point["geometry_type"] == "Point"
    assert point["measurement"]["status"] == "NOT_APPLICABLE"

    assert data["summary"]["features_by_status"] == {
        "MEASURED": 2,
        "NOT_APPLICABLE": 1,
    }
    assert data["summary"]["total_area_m2"] == pytest.approx(polygon["measurement"]["area_m2"])
    assert data["units"] == {"area": "square_meters", "length": "meters"}


def test_kml_extended_data_and_folders_become_properties(client):
    placemark = (
        "<Placemark><name>P</name><description>desc</description>"
        '<ExtendedData><Data name="owner"><value>Alice</value></Data></ExtendedData>'
        f"<Point><coordinates>77.5,12.9</coordinates></Point></Placemark>"
    )
    content = (
        '<kml xmlns="http://www.opengis.net/kml/2.2"><Document><Folder><name>Parcels</name>'
        f"{placemark}</Folder></Document></kml>"
    ).encode()
    file_id = upload(client, "a.kml", content).json()["id"]

    properties = measurements(client, file_id)["features"][0]["properties"]
    assert properties == {
        "name": "P",
        "description": "desc",
        "owner": "Alice",
        "folder": "Parcels",
    }


def test_kml_without_namespace_and_with_spaced_coordinates(client):
    content = (
        "<kml><Document><Placemark><name>x</name><Polygon><outerBoundaryIs><LinearRing>"
        "<coordinates>77.59, 12.97, 0 77.60, 12.97, 0 77.60, 12.98, 0 77.59, 12.97, 0</coordinates>"
        "</LinearRing></outerBoundaryIs></Polygon></Placemark></Document></kml>"
    ).encode()
    response = upload(client, "plain.kml", content)
    assert response.status_code == 201, response.text
    feature = measurements(client, response.json()["id"])["features"][0]
    assert feature["measurement"]["status"] == "MEASURED"


def test_kml_polygon_with_hole(client):
    inner = "77.593,12.973 77.597,12.973 77.597,12.977 77.593,12.977 77.593,12.973"
    file_id = upload(client, "hole.kml", kml(kml_polygon("H", PLOT_RING, inner))).json()["id"]

    area = measurements(client, file_id)["features"][0]["measurement"]["area_m2"]
    hole = Polygon([(77.593, 12.973), (77.597, 12.973), (77.597, 12.977), (77.593, 12.977)])
    expected = abs(GEOD.geometry_area_perimeter(PLOT)[0]) - abs(GEOD.geometry_area_perimeter(hole)[0])
    assert area == pytest.approx(expected, rel=5e-3)


def test_kml_multigeometry_polygons_are_supported(client):
    placemark = (
        "<Placemark><name>multi</name><MultiGeometry>"
        f"<Polygon><outerBoundaryIs><LinearRing><coordinates>{PLOT_RING}</coordinates></LinearRing></outerBoundaryIs></Polygon>"
        "<Polygon><outerBoundaryIs><LinearRing><coordinates>"
        "77.70,12.97 77.71,12.97 77.71,12.98 77.70,12.98 77.70,12.97"
        "</coordinates></LinearRing></outerBoundaryIs></Polygon>"
        "</MultiGeometry></Placemark>"
    )
    file_id = upload(client, "m.kml", kml(placemark)).json()["id"]
    feature = measurements(client, file_id)["features"][0]
    assert feature["geometry_type"] == "MultiPolygon"
    assert feature["measurement"]["status"] == "MEASURED"


def test_kml_mixed_multigeometry_is_unsupported_but_does_not_fail_the_file(client):
    placemark = (
        "<Placemark><name>mixed</name><MultiGeometry>"
        "<Point><coordinates>77.5,12.9</coordinates></Point>"
        "<LineString><coordinates>77.5,12.9 77.6,13.0</coordinates></LineString>"
        "</MultiGeometry></Placemark>"
    )
    response = upload(client, "mixed.kml", kml(placemark, kml_point("ok", "77.5,12.9")))
    assert response.status_code == 201
    features = measurements(client, response.json()["id"])["features"]
    assert features[0]["geometry_type"] == "GeometryCollection"
    assert features[0]["measurement"]["status"] == "UNSUPPORTED"
    assert features[1]["measurement"]["status"] == "NOT_APPLICABLE"


def test_bad_feature_does_not_fail_the_file(client):
    broken = "<Placemark><name>bad</name><LineString><coordinates>77.5,12.9</coordinates></LineString></Placemark>"
    nogeom = "<Placemark><name>none</name></Placemark>"
    response = upload(client, "partial.kml", kml(broken, nogeom, kml_point("ok", "77.5,12.9")))

    assert response.status_code == 201
    features = measurements(client, response.json()["id"])["features"]
    assert features[0]["measurement"]["status"] == "ERROR"
    assert features[0]["geometry"] is None
    assert features[1]["measurement"]["status"] == "UNSUPPORTED"
    assert features[1]["geometry_type"] is None
    assert features[2]["measurement"]["status"] == "NOT_APPLICABLE"


def test_include_geometry_returns_geojson(client):
    file_id = upload(client, "g.kml", kml(kml_point("p", "77.5,12.9,0"))).json()["id"]

    without = measurements(client, file_id)["features"][0]
    with_geometry = measurements(client, file_id, include_geometry="true")["features"][0]

    assert without["geometry"] is None
    assert with_geometry["geometry"] == {"type": "Point", "coordinates": [77.5, 12.9]}


def test_measurements_pagination_and_whole_file_summary(client):
    placemarks = [kml_polygon(f"p{i}", PLOT_RING) for i in range(5)]
    file_id = upload(client, "many.kml", kml(*placemarks)).json()["id"]

    page = measurements(client, file_id, limit=2, offset=3)

    assert [f["index"] for f in page["features"]] == [3, 4]
    assert page["summary"]["features_by_status"] == {"MEASURED": 5}
    assert page["summary"]["total_area_m2"] > 4 * page["features"][0]["measurement"]["area_m2"]


# ----------------------------------------------------------------- Shapefile


def test_shapefile_in_geographic_crs(client):
    rings = [[(77.59, 12.97), (77.60, 12.97), (77.60, 12.98), (77.59, 12.98)]]
    response = upload(client, "plots.zip", zip_polygons(rings, WGS84))

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["crs"] == "EPSG:4326"
    assert body["warnings"] == []

    feature = measurements(client, body["id"])["features"][0]
    assert feature["geometry_type"] == "Polygon"
    assert feature["properties"] == {"name": "poly-0"}
    assert feature["measurement"]["area_m2"] == pytest.approx(
        abs(GEOD.geometry_area_perimeter(PLOT)[0]), rel=5e-3
    )


def test_shapefile_in_projected_crs_is_measured_directly(client):
    ring = [(500000, 1400000), (500100, 1400000), (500100, 1400100), (500000, 1400100)]
    response = upload(client, "utm.zip", zip_polygons([ring], UTM43))

    assert response.status_code == 201, response.text
    assert response.json()["crs"] == "EPSG:32643"
    measurement = measurements(client, response.json()["id"])["features"][0]["measurement"]
    assert measurement["area_m2"] == pytest.approx(10_000.0, rel=1e-6)
    assert measurement["projected_crs"] == "EPSG:32643"


def test_shapefile_lines_and_points(client):
    lines = upload(client, "lines.zip", zip_lines([[(0, 0), (300, 400)]], UTM43))
    line_feature = measurements(client, lines.json()["id"])["features"][0]
    assert line_feature["measurement"]["length_m"] == pytest.approx(500.0, rel=1e-6)

    points = upload(client, "points.zip", zip_points([(500000, 1400000)], UTM43))
    point_feature = measurements(client, points.json()["id"])["features"][0]
    assert point_feature["geometry_type"] == "Point"
    assert point_feature["measurement"]["status"] == "NOT_APPLICABLE"


def test_shapefile_without_prj_assumes_wgs84_and_warns(client):
    rings = [[(77.59, 12.97), (77.60, 12.97), (77.60, 12.98), (77.59, 12.98)]]
    response = upload(client, "noprj.zip", zip_polygons(rings, WGS84, include_prj=False))

    assert response.status_code == 201
    body = response.json()
    assert body["crs"] == "EPSG:4326"
    assert any(".prj" in w for w in body["warnings"])


def test_shapefile_without_prj_but_projected_coordinates_is_flagged_per_feature(client):
    ring = [(500000, 1400000), (500100, 1400000), (500100, 1400100), (500000, 1400100)]
    response = upload(client, "wrong.zip", zip_polygons([ring], UTM43, include_prj=False))

    assert response.status_code == 201
    measurement = measurements(client, response.json()["id"])["features"][0]["measurement"]
    assert measurement["status"] == "UNSUPPORTED"
    assert measurement["area_m2"] is None


# ------------------------------------------------------------ error handling


def test_unsupported_extension_returns_415(client):
    response = upload(client, "data.geojson", b"{}")
    assert response.status_code == 415


def test_empty_file_returns_400(client):
    assert upload(client, "empty.kml", b"").status_code == 400


def test_oversized_upload_returns_413(client, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "max_upload_bytes", 10)
    assert upload(client, "big.kml", b"x" * 11).status_code == 413


def test_malformed_kml_is_recorded_as_failed(client):
    response = upload(client, "broken.kml", b"<kml><Document>")

    assert response.status_code == 422
    body = response.json()
    assert body["status"] == "FAILED"
    assert "Invalid KML" in body["error"]

    info = client.get(f"/api/files/{body['id']}/").json()
    assert info["status"] == "FAILED"
    assert client.get(f"/api/files/{body['id']}/measurements/").status_code == 409


def test_kml_with_entity_expansion_is_rejected(client):
    bomb = (
        b'<?xml version="1.0"?><!DOCTYPE kml [<!ENTITY a "aaaa"><!ENTITY b "&a;&a;&a;&a;">]>'
        b"<kml><Document><Placemark><name>&b;</name></Placemark></Document></kml>"
    )
    response = upload(client, "bomb.kml", bomb)
    assert response.status_code == 422
    assert response.json()["status"] == "FAILED"


def test_kml_without_placemarks_fails(client):
    response = upload(client, "none.kml", kml())
    assert response.status_code == 422
    assert "No Placemark" in response.json()["error"]


def test_zip_that_is_not_a_zip_fails(client):
    response = upload(client, "fake.zip", b"definitely not a zip")
    assert response.status_code == 422
    assert "not a valid ZIP" in response.json()["error"]


def test_zip_without_shp_fails(client):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("readme.txt", "hello")
    response = upload(client, "empty.zip", buffer.getvalue())
    assert response.status_code == 422
    assert ".shp" in response.json()["error"]


def test_zip_with_two_shapefiles_fails(client):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("a.shp", b"")
        archive.writestr("b.shp", b"")
    response = upload(client, "two.zip", buffer.getvalue())
    assert response.status_code == 422
    assert "multiple shapefiles" in response.json()["error"]


def test_unknown_file_id_returns_404(client):
    assert client.get("/api/files/nope/").status_code == 404
    assert client.get("/api/files/nope/measurements/").status_code == 404


def test_list_files(client):
    upload(client, "one.kml", kml(kml_point("a", "77.5,12.9")))
    upload(client, "two.kml", kml(kml_point("b", "77.5,12.9")))

    body = client.get("/api/files/").json()
    assert body["total"] == 2
    assert {item["filename"] for item in body["items"]} == {"one.kml", "two.kml"}


def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}
