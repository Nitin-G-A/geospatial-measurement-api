"""Pydantic response models."""
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class FileOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    filename: str
    file_type: str
    feature_count: int
    crs: str | None
    status: str
    error: str | None = None
    warnings: list[str] = Field(default_factory=list)
    created_at: datetime


class FileList(BaseModel):
    total: int
    limit: int
    offset: int
    items: list[FileOut]


class MeasurementOut(BaseModel):
    # MEASURED | NOT_APPLICABLE | UNSUPPORTED | ERROR
    status: str
    area_m2: float | None = None
    length_m: float | None = None
    projected_crs: str | None = None
    note: str | None = None


class FeatureMeasurementOut(BaseModel):
    index: int
    geometry_type: str | None
    crs: str | None
    properties: dict[str, Any]
    measurement: MeasurementOut
    # Only populated when ?include_geometry=true
    geometry: dict[str, Any] | None = None


class MeasurementSummary(BaseModel):
    features_by_status: dict[str, int]
    total_area_m2: float
    total_length_m: float


class MeasurementsResponse(BaseModel):
    file_id: str
    filename: str
    crs: str | None
    feature_count: int
    limit: int
    offset: int
    units: dict[str, str]
    summary: MeasurementSummary
    features: list[FeatureMeasurementOut]
