"""Upload pipeline: detect type -> parse -> measure -> persist."""
from __future__ import annotations

import json
import logging

import shapely
from sqlalchemy.orm import Session

from app.config import settings
from app.models import (
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_PROCESSING,
    Feature,
    UploadedFile,
    new_id,
)
from app.services.crs import crs_label
from app.services.measurements import STATUS_ERROR, Measurement, Measurer
from app.services.readers import (
    FileProcessingError,
    ParsedFile,
    read_kml,
    read_shapefile_zip,
)

logger = logging.getLogger(__name__)


class UnsupportedFileTypeError(Exception):
    pass


def detect_file_type(filename: str) -> str:
    lowered = filename.lower()
    if lowered.endswith(".kml"):
        return "kml"
    if lowered.endswith(".zip"):
        return "shapefile"
    raise UnsupportedFileTypeError(
        "Unsupported file type. Upload a .kml file or a .zip containing a Shapefile."
    )


def parse_file(file_type: str, data: bytes) -> ParsedFile:
    if file_type == "kml":
        return read_kml(data, settings.max_features)
    return read_shapefile_zip(data, settings.max_uncompressed_bytes, settings.max_features)


def _build_feature_rows(file_id: str, parsed: ParsedFile) -> list[Feature]:
    measurer = Measurer(parsed.crs)
    label = crs_label(parsed.crs)
    rows: list[Feature] = []

    for feature in parsed.features:
        if feature.error:
            measurement = Measurement(STATUS_ERROR, note=feature.error)
        else:
            measurement = measurer.measure(feature.geometry)

        geometry = feature.geometry
        rows.append(
            Feature(
                file_id=file_id,
                feature_index=feature.index,
                geometry_type=geometry.geom_type if geometry is not None else None,
                geometry=json.loads(shapely.to_geojson(geometry)) if geometry is not None else None,
                crs=label,
                properties=feature.properties,
                measure_status=measurement.status,
                area_m2=measurement.area_m2,
                length_m=measurement.length_m,
                projected_crs=measurement.projected_crs,
                note=measurement.note,
            )
        )
    return rows


def ingest_file(db: Session, filename: str, data: bytes) -> UploadedFile:
    """Process an upload and persist the result.

    Raises UnsupportedFileTypeError for unknown extensions. Any problem with the
    file *content* is recorded on the returned record as status FAILED.
    """
    file_type = detect_file_type(filename)
    # The id is assigned up front so feature rows can reference it before any write.
    record = UploadedFile(
        id=new_id(), filename=filename, file_type=file_type, status=STATUS_PROCESSING
    )

    try:
        parsed = parse_file(file_type, data)
        rows = _build_feature_rows(record.id, parsed)
    except FileProcessingError as exc:
        record.status = STATUS_FAILED
        record.error = str(exc)
        rows = []
    except Exception:
        logger.exception("Unexpected error while processing %s", filename)
        record.status = STATUS_FAILED
        record.error = "Unexpected error while processing the file"
        rows = []
    else:
        record.status = STATUS_COMPLETED
        record.crs = crs_label(parsed.crs)
        record.crs_wkt = parsed.crs.to_wkt()
        record.feature_count = len(rows)
        record.warnings = parsed.warnings

    db.add(record)
    db.add_all(rows)
    db.commit()
    return record
