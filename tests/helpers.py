"""Builders for in-memory test uploads."""
import io
import zipfile

import shapefile
from pyproj import CRS
from shapely.geometry import Polygon
from shapely.geometry.polygon import orient

KML_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2"><Document>{body}</Document></kml>"""


def kml(*placemarks: str) -> bytes:
    return KML_TEMPLATE.format(body="".join(placemarks)).encode()


def kml_polygon(name: str, ring: str, inner: str | None = None) -> str:
    inner_xml = (
        f"<innerBoundaryIs><LinearRing><coordinates>{inner}</coordinates></LinearRing></innerBoundaryIs>"
        if inner
        else ""
    )
    return (
        f"<Placemark><name>{name}</name><Polygon><outerBoundaryIs><LinearRing>"
        f"<coordinates>{ring}</coordinates></LinearRing></outerBoundaryIs>{inner_xml}"
        "</Polygon></Placemark>"
    )


def kml_line(name: str, coords: str) -> str:
    return (
        f"<Placemark><name>{name}</name><LineString>"
        f"<coordinates>{coords}</coordinates></LineString></Placemark>"
    )


def kml_point(name: str, coords: str) -> str:
    return f"<Placemark><name>{name}</name><Point><coordinates>{coords}</coordinates></Point></Placemark>"


def _build_zip(shape_type, add_shapes, crs: CRS | None, include_prj: bool = True) -> bytes:
    shp, shx, dbf = io.BytesIO(), io.BytesIO(), io.BytesIO()
    writer = shapefile.Writer(shp=shp, shx=shx, dbf=dbf, shapeType=shape_type)
    writer.field("name", "C", 40)
    add_shapes(writer)
    writer.close()

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("data.shp", shp.getvalue())
        archive.writestr("data.shx", shx.getvalue())
        archive.writestr("data.dbf", dbf.getvalue())
        if crs is not None and include_prj:
            archive.writestr("data.prj", crs.to_wkt("WKT1_ESRI"))
    return buffer.getvalue()


def zip_polygons(rings: list[list[tuple[float, float]]], crs: CRS | None, **kwargs) -> bytes:
    def add(writer):
        for i, ring in enumerate(rings):
            clockwise = list(orient(Polygon(ring), sign=-1.0).exterior.coords)  # shapefile outer rings are CW
            writer.poly([clockwise])
            writer.record(f"poly-{i}")

    return _build_zip(shapefile.POLYGON, add, crs, **kwargs)


def zip_lines(lines: list[list[tuple[float, float]]], crs: CRS | None, **kwargs) -> bytes:
    def add(writer):
        for i, line in enumerate(lines):
            writer.line([line])
            writer.record(f"line-{i}")

    return _build_zip(shapefile.POLYLINE, add, crs, **kwargs)


def zip_points(points: list[tuple[float, float]], crs: CRS | None, **kwargs) -> bytes:
    def add(writer):
        for i, (x, y) in enumerate(points):
            writer.point(x, y)
            writer.record(f"pt-{i}")

    return _build_zip(shapefile.POINT, add, crs, **kwargs)
