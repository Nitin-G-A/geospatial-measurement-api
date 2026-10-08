"""Readers turning an uploaded KML / zipped Shapefile into plain Python objects.

Both readers are pure Python on top of pyshp, defusedxml and shapely, so no
GDAL installation is required.
"""
from __future__ import annotations

import codecs
import io
import math
import re
import zipfile
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import PurePosixPath
from xml.etree.ElementTree import Element, ParseError

import shapefile  # pyshp
import shapely
from defusedxml import ElementTree as SafeET
from defusedxml.common import DefusedXmlException
from pyproj import CRS
from shapely.geometry import (
    GeometryCollection,
    LinearRing,
    LineString,
    MultiLineString,
    MultiPoint,
    MultiPolygon,
    Point,
    Polygon,
    shape,
)
from shapely.geometry.base import BaseGeometry


class FileProcessingError(Exception):
    """The upload could not be turned into features (bad archive, bad XML, ...)."""


@dataclass
class ParsedFeature:
    index: int
    geometry: BaseGeometry | None
    properties: dict
    error: str | None = None  # set when this one feature could not be read


@dataclass
class ParsedFile:
    file_type: str
    crs: CRS
    features: list[ParsedFeature]
    warnings: list[str] = field(default_factory=list)


def _json_safe(value):
    """Attribute values from DBF files can be Decimal/date/bytes; make them JSON-friendly."""
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, date):  # also covers datetime
        return value.isoformat()
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return str(value)


# =========================================================================== KML

_KML_GEOMETRY_TAGS = {"Point", "LineString", "LinearRing", "Polygon", "MultiGeometry"}
_KML_UNSUPPORTED_TAGS = {"Model", "Track", "MultiTrack"}
_COMMA_SPACES = re.compile(r"\s*,\s*")


def _local(tag) -> str:
    """Tag name without XML namespace (KML 2.0/2.1/2.2 and gx: all work)."""
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _children(el: Element, name: str) -> list[Element]:
    return [c for c in el if _local(c.tag) == name]


def _child(el: Element, name: str) -> Element | None:
    for c in el:
        if _local(c.tag) == name:
            return c
    return None


def _text(el: Element | None) -> str:
    return (el.text or "").strip() if el is not None else ""


def _parse_coordinate_text(text: str) -> list[tuple[float, float]]:
    coords: list[tuple[float, float]] = []
    # Tolerate "lon, lat, alt" written with spaces after the commas.
    for token in _COMMA_SPACES.sub(",", text.strip()).split():
        parts = token.split(",")
        if len(parts) < 2:
            raise ValueError(f"invalid coordinate tuple {token!r}")
        x, y = float(parts[0]), float(parts[1])
        if not (math.isfinite(x) and math.isfinite(y)):
            raise ValueError(f"non-finite coordinate {token!r}")
        coords.append((x, y))  # altitude is intentionally ignored
    return coords


def _kml_coordinates(el: Element) -> list[tuple[float, float]]:
    node = _child(el, "coordinates")
    return _parse_coordinate_text(node.text or "") if node is not None else []


def _ring_coordinates(boundary: Element) -> list[tuple[float, float]]:
    ring = _child(boundary, "LinearRing")
    if ring is None:
        raise ValueError("polygon boundary has no LinearRing")
    return _kml_coordinates(ring)


def _kml_geometry(el: Element) -> BaseGeometry:
    tag = _local(el.tag)

    if tag == "Point":
        coords = _kml_coordinates(el)
        if not coords:
            raise ValueError("Point has no coordinates")
        return Point(coords[0])

    if tag == "LineString":
        coords = _kml_coordinates(el)
        if len(coords) < 2:
            raise ValueError("LineString needs at least 2 coordinates")
        return LineString(coords)

    if tag == "LinearRing":
        coords = _kml_coordinates(el)
        if len(coords) < 3:
            raise ValueError("LinearRing needs at least 3 coordinates")
        return LinearRing(coords)

    if tag == "Polygon":
        outer = _child(el, "outerBoundaryIs")
        if outer is None:
            raise ValueError("Polygon has no outerBoundaryIs")
        shell = _ring_coordinates(outer)
        holes = [_ring_coordinates(b) for b in _children(el, "innerBoundaryIs")]
        return Polygon(shell, holes)

    if tag == "MultiGeometry":
        parts = [
            _kml_geometry(c)
            for c in el
            if _local(c.tag) in _KML_GEOMETRY_TAGS | _KML_UNSUPPORTED_TAGS
        ]
        if not parts:
            raise ValueError("MultiGeometry is empty")
        types = {p.geom_type for p in parts}
        if types == {"Point"}:
            return MultiPoint(parts)
        if types <= {"LineString", "LinearRing"}:
            return MultiLineString([LineString(p.coords) for p in parts])
        if types == {"Polygon"}:
            return MultiPolygon(parts)
        return GeometryCollection(parts)  # mixed content: reported as unsupported later

    raise ValueError(f"unsupported KML geometry element <{tag}>")


def _kml_properties(placemark: Element, folder_path: list[str]) -> dict:
    props: dict = {}
    name = _text(_child(placemark, "name"))
    if name:
        props["name"] = name
    description = _text(_child(placemark, "description"))
    if description:
        props["description"] = description

    extended = _child(placemark, "ExtendedData")
    if extended is not None:
        for node in extended.iter():
            local = _local(node.tag)
            key = node.get("name")
            if not key:
                continue
            if local == "Data":
                value_el = next((c for c in node if _local(c.tag).lower() == "value"), None)
                value = _text(value_el)
            elif local == "SimpleData":
                value = _text(node)
            else:
                continue
            props[f"data_{key}" if key in props else key] = value

    if folder_path:
        props["folder"] = " / ".join(folder_path)
    return props


def _iter_placemarks(root: Element):
    """Yield (placemark, folder_path) in document order, without recursion."""
    stack = [(iter(root), [])]
    while stack:
        iterator, path = stack[-1]
        for child in iterator:
            name = _local(child.tag)
            if name == "Placemark":
                yield child, path
            elif name in ("Document", "Folder"):
                label = _text(_child(child, "name"))
                stack.append((iter(child), path + [label] if label else path))
                break
        else:
            stack.pop()


def read_kml(data: bytes, max_features: int) -> ParsedFile:
    try:
        root = SafeET.fromstring(data)
    except (ParseError, DefusedXmlException) as exc:
        raise FileProcessingError(f"Invalid KML/XML: {exc}") from exc

    if _local(root.tag) == "Placemark":
        placemarks = iter([(root, [])])
    else:
        placemarks = _iter_placemarks(root)

    features: list[ParsedFeature] = []
    for index, (placemark, path) in enumerate(placemarks):
        if index >= max_features:
            raise FileProcessingError(f"File contains more than {max_features} features")

        geometry: BaseGeometry | None = None
        error: str | None = None
        geometry_el = next(
            (
                c
                for c in placemark
                if _local(c.tag) in _KML_GEOMETRY_TAGS | _KML_UNSUPPORTED_TAGS
            ),
            None,
        )
        if geometry_el is not None:
            try:
                geometry = shapely.force_2d(_kml_geometry(geometry_el))
            except Exception as exc:
                error = f"Could not read geometry: {exc}"

        features.append(
            ParsedFeature(
                index=index,
                geometry=geometry,
                properties=_kml_properties(placemark, path),
                error=error,
            )
        )

    if not features:
        raise FileProcessingError("No Placemark features found in KML file")

    # KML coordinates are always WGS 84 longitude/latitude by specification.
    return ParsedFile("kml", CRS.from_epsg(4326), features)


# ===================================================================== Shapefile


def _is_junk(name: str) -> bool:
    path = PurePosixPath(name)
    return "__MACOSX" in path.parts or path.name.startswith(".")


def _shapefile_crs(prj: bytes | None, warnings: list[str]) -> CRS:
    if prj:
        try:
            return CRS.from_wkt(prj.decode("utf-8", "replace").strip("\x00 \r\n\t"))
        except Exception:
            warnings.append(".prj file could not be parsed; assuming EPSG:4326 (WGS 84)")
    else:
        warnings.append("No .prj file found; assuming EPSG:4326 (WGS 84)")
    return CRS.from_epsg(4326)


def _shapefile_encoding(cpg: bytes | None) -> str:
    if cpg:
        name = cpg.decode("ascii", "ignore").strip()
        candidates = [name, f"cp{name}"] if name.isdigit() else [name]
        for candidate in candidates:
            try:
                codecs.lookup(candidate)
                return candidate
            except LookupError:
                continue
    return "utf-8"


def _iter_shapes(reader: shapefile.Reader, has_dbf: bool):
    if has_dbf:
        for shape_record in reader.iterShapeRecords():
            yield shape_record.shape, shape_record.record
    else:
        for shp in reader.iterShapes():
            yield shp, None


def read_shapefile_zip(data: bytes, max_uncompressed: int, max_features: int) -> ParsedFile:
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise FileProcessingError("Upload is not a valid ZIP archive") from exc

    with archive:
        infos = [i for i in archive.infolist() if not i.is_dir() and not _is_junk(i.filename)]
        if sum(i.file_size for i in infos) > max_uncompressed:
            raise FileProcessingError("ZIP archive is too large once uncompressed")

        # Members are only ever read into memory, never extracted to disk,
        # so path traversal ("zip slip") is not possible.
        lookup = {i.filename.lower(): i for i in infos}
        shp_names = sorted(n for n in lookup if n.endswith(".shp"))
        if not shp_names:
            raise FileProcessingError("ZIP archive does not contain a .shp file")
        if len(shp_names) > 1:
            raise FileProcessingError(
                "ZIP archive contains multiple shapefiles "
                f"({', '.join(shp_names)}); upload one shapefile per ZIP"
            )

        base = shp_names[0][: -len(".shp")]

        def read_part(ext: str) -> bytes | None:
            info = lookup.get(base + ext)
            return archive.read(info) if info else None

        shp_bytes = read_part(".shp")
        shx_bytes = read_part(".shx")
        dbf_bytes = read_part(".dbf")
        prj_bytes = read_part(".prj")
        cpg_bytes = read_part(".cpg")

    warnings: list[str] = []
    crs = _shapefile_crs(prj_bytes, warnings)
    if not dbf_bytes:
        warnings.append("No .dbf file found; features have no attributes")

    try:
        reader = shapefile.Reader(
            shp=io.BytesIO(shp_bytes),
            shx=io.BytesIO(shx_bytes) if shx_bytes else None,
            dbf=io.BytesIO(dbf_bytes) if dbf_bytes else None,
            encoding=_shapefile_encoding(cpg_bytes),
            encodingErrors="replace",
        )
    except Exception as exc:
        raise FileProcessingError(f"Could not open shapefile: {exc}") from exc

    field_names = (
        [f[0] for f in reader.fields if f[0] != "DeletionFlag"] if dbf_bytes else []
    )

    features: list[ParsedFeature] = []
    try:
        for index, (shp, record) in enumerate(_iter_shapes(reader, bool(dbf_bytes))):
            if index >= max_features:
                raise FileProcessingError(f"File contains more than {max_features} features")

            geometry: BaseGeometry | None = None
            error: str | None = None
            if shp.shapeType != shapefile.NULL:
                try:
                    geometry = shapely.force_2d(shape(shp.__geo_interface__))
                except Exception as exc:
                    error = f"Could not read geometry: {exc}"

            properties = (
                {name: _json_safe(value) for name, value in zip(field_names, list(record))}
                if record is not None
                else {}
            )
            features.append(ParsedFeature(index, geometry, properties, error))
    except FileProcessingError:
        raise
    except Exception as exc:
        raise FileProcessingError(f"Failed while reading shapefile: {exc}") from exc

    if not features:
        raise FileProcessingError("Shapefile contains no features")

    return ParsedFile("shapefile", crs, features, warnings)
