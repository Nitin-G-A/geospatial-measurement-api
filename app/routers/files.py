"""/api/files endpoints."""
import os

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from fastapi.responses import JSONResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.models import STATUS_COMPLETED, STATUS_FAILED, Feature, UploadedFile
from app.schemas import (
    FeatureMeasurementOut,
    FileList,
    FileOut,
    MeasurementOut,
    MeasurementsResponse,
    MeasurementSummary,
)
from app.services.ingest import UnsupportedFileTypeError, ingest_file

router = APIRouter(prefix="/api/files", tags=["files"])


def _get_file_or_404(db: Session, file_id: str) -> UploadedFile:
    record = db.get(UploadedFile, file_id)
    if record is None:
        raise HTTPException(status_code=404, detail="File not found")
    return record


def _round(value: float | None) -> float | None:
    return round(value, 4) if value is not None else None


@router.post(
    "/",
    response_model=FileOut,
    status_code=201,
    summary="Upload a KML or zipped Shapefile",
    responses={
        400: {"description": "Missing filename or empty file"},
        413: {"description": "File exceeds the upload size limit"},
        415: {"description": "Unsupported file type"},
        422: {
            "model": FileOut,
            "description": "File was received but its content could not be processed (status FAILED)",
        },
    },
)
def upload_file(file: UploadFile = File(...), db: Session = Depends(get_db)):
    filename = os.path.basename((file.filename or "").replace("\\", "/")).strip()[:255]
    if not filename:
        raise HTTPException(status_code=400, detail="Uploaded file must have a filename")

    data = file.file.read(settings.max_upload_bytes + 1)
    if len(data) > settings.max_upload_bytes:
        raise HTTPException(
            status_code=413,
            detail=f"File is larger than the {settings.max_upload_bytes // (1024 * 1024)} MB limit",
        )
    if not data:
        raise HTTPException(status_code=400, detail="Uploaded file is empty")

    try:
        record = ingest_file(db, filename, data)
    except UnsupportedFileTypeError as exc:
        raise HTTPException(status_code=415, detail=str(exc)) from exc

    out = FileOut.model_validate(record)
    if record.status == STATUS_FAILED:
        # The record is kept so the failure can be inspected with GET /api/files/{id}/.
        return JSONResponse(status_code=422, content=out.model_dump(mode="json"))
    return out


@router.get("/", response_model=FileList, summary="List uploaded files")
def list_files(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    total = db.scalar(select(func.count()).select_from(UploadedFile)) or 0
    items = db.scalars(
        select(UploadedFile)
        .order_by(UploadedFile.created_at.desc(), UploadedFile.id)
        .offset(offset)
        .limit(limit)
    ).all()
    return FileList(
        total=total,
        limit=limit,
        offset=offset,
        items=[FileOut.model_validate(i) for i in items],
    )


@router.get("/{file_id}/", response_model=FileOut, summary="File information")
def get_file(file_id: str, db: Session = Depends(get_db)):
    return _get_file_or_404(db, file_id)


@router.get(
    "/{file_id}/measurements/",
    response_model=MeasurementsResponse,
    summary="Per-feature measurements",
    responses={409: {"description": "File processing did not complete"}},
)
def get_measurements(
    file_id: str,
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    include_geometry: bool = Query(False, description="Include GeoJSON geometry per feature"),
    db: Session = Depends(get_db),
):
    record = _get_file_or_404(db, file_id)
    if record.status != STATUS_COMPLETED:
        detail = f"File status is {record.status}"
        if record.error:
            detail += f": {record.error}"
        raise HTTPException(status_code=409, detail=detail)

    # Totals cover the whole file, independent of pagination.
    status_rows = db.execute(
        select(
            Feature.measure_status,
            func.count(),
            func.coalesce(func.sum(Feature.area_m2), 0.0),
            func.coalesce(func.sum(Feature.length_m), 0.0),
        )
        .where(Feature.file_id == file_id)
        .group_by(Feature.measure_status)
    ).all()

    summary = MeasurementSummary(
        features_by_status={status: count for status, count, _, _ in status_rows},
        total_area_m2=round(sum(row[2] for row in status_rows), 4),
        total_length_m=round(sum(row[3] for row in status_rows), 4),
    )

    features = db.scalars(
        select(Feature)
        .where(Feature.file_id == file_id)
        .order_by(Feature.feature_index)
        .offset(offset)
        .limit(limit)
    ).all()

    return MeasurementsResponse(
        file_id=record.id,
        filename=record.filename,
        crs=record.crs,
        feature_count=record.feature_count,
        limit=limit,
        offset=offset,
        units={"area": "square_meters", "length": "meters"},
        summary=summary,
        features=[
            FeatureMeasurementOut(
                index=f.feature_index,
                geometry_type=f.geometry_type,
                crs=f.crs,
                properties=f.properties or {},
                measurement=MeasurementOut(
                    status=f.measure_status,
                    area_m2=_round(f.area_m2),
                    length_m=_round(f.length_m),
                    projected_crs=f.projected_crs,
                    note=f.note,
                ),
                geometry=f.geometry if include_geometry else None,
            )
            for f in features
        ],
    )
