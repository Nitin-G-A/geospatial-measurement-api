# Geospatial File Measurement API

A FastAPI backend that accepts a **KML** file or a **zipped Shapefile**, extracts every feature
(ID, geometry type, geometry, CRS, attributes) and returns **area** (polygons) and **length**
(lines), calculated in an appropriate **projected CRS** — never directly on latitude/longitude degrees.

## Contents

- [Setup](#setup)
- [API](#api)
- [Architecture](#architecture)
- [Design decisions](#design-decisions)
- [Testing](#testing)
- [Learnings](#learnings)
- [Future scope](#future-scope)

---

## Setup

Requires **Python 3.10+**. No GDAL needed: everything installs from pre-built wheels.

```bash
git clone <this-repo-url>
cd geospatial-measurement-api

python -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt  # use requirements.txt for runtime only

uvicorn app.main:app --reload
```

- API: http://127.0.0.1:8000
- Interactive docs (Swagger UI): http://127.0.0.1:8000/docs
- A SQLite database is created automatically at `./data/app.db`.

### Run with Docker

```bash
docker build -t geo-measure .
docker run -p 8000:8000 geo-measure
```

### Configuration (environment variables)

| Variable | Default | Meaning |
|---|---|---|
| `DATABASE_URL` | `sqlite:///./data/app.db` | Any SQLAlchemy URL (e.g. PostgreSQL) |
| `MAX_UPLOAD_MB` | `50` | Maximum upload size |
| `MAX_UNCOMPRESSED_MB` | `200` | Maximum total size of a ZIP's contents (zip-bomb guard) |
| `MAX_FEATURES` | `100000` | Maximum features per file |

### Sample data

- `samples/sample.kml` — a polygon, a line and a point near Bengaluru.
- `python scripts/make_sample_shapefile.py` — writes `samples/sample_shapefile.zip`.

---

## API

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/api/files/` | Upload and process a `.kml` or `.zip` (Shapefile) |
| `GET` | `/api/files/{id}/` | File information |
| `GET` | `/api/files/{id}/measurements/` | Per-feature measurements |
| `GET` | `/api/files/` | List uploaded files (extra) |
| `GET` | `/health` | Liveness check |

### `POST /api/files/`

Multipart form upload, field name `file`.

```bash
curl -F "file=@samples/sample.kml" http://127.0.0.1:8000/api/files/
```

`201 Created`

```json
{
  "id": "3f6c1e0b9a7d4c2e8b5a1d0f7e6c4b2a",
  "filename": "sample.kml",
  "file_type": "kml",
  "feature_count": 3,
  "crs": "EPSG:4326",
  "status": "COMPLETED",
  "error": null,
  "warnings": [],
  "created_at": "2026-10-07T06:40:12.123456"
}
```

| Status | When |
|---|---|
| `201` | File processed (`status: COMPLETED`) |
| `400` | Missing filename or empty file |
| `413` | Larger than `MAX_UPLOAD_MB` |
| `415` | Not a `.kml` or `.zip` |
| `422` | File received but unreadable (corrupt ZIP, malformed XML, no `.shp`, …). The record is kept with `status: FAILED` and an `error` message, and the same body is returned so the `id` can be looked up later |

### `GET /api/files/{id}/`

```bash
curl http://127.0.0.1:8000/api/files/3f6c1e0b9a7d4c2e8b5a1d0f7e6c4b2a/
```

Returns the same object as the upload response. `404` for an unknown id.

### `GET /api/files/{id}/measurements/`

Query parameters: `limit` (default 100, max 1000), `offset` (default 0),
`include_geometry` (default `false`; adds GeoJSON in the file's original CRS).

```bash
curl "http://127.0.0.1:8000/api/files/3f6c1e0b9a7d4c2e8b5a1d0f7e6c4b2a/measurements/"
```

```json
{
  "file_id": "3f6c1e0b9a7d4c2e8b5a1d0f7e6c4b2a",
  "filename": "sample.kml",
  "crs": "EPSG:4326",
  "feature_count": 3,
  "limit": 100,
  "offset": 0,
  "units": { "area": "square_meters", "length": "meters" },
  "summary": {
    "features_by_status": { "MEASURED": 2, "NOT_APPLICABLE": 1 },
    "total_area_m2": 300073.5,
    "total_length_m": 765.5
  },
  "features": [
    {
      "index": 0,
      "geometry_type": "Polygon",
      "crs": "EPSG:4326",
      "properties": { "name": "Plot A", "description": "Rectangular survey plot", "owner": "Survey Dept", "folder": "Plots" },
      "measurement": {
        "status": "MEASURED",
        "area_m2": 300073.5,
        "length_m": null,
        "projected_crs": "LAEA(lat_0=12.97, lon_0=77.6, WGS84)",
        "note": null
      },
      "geometry": null
    },
    {
      "index": 1,
      "geometry_type": "LineString",
      "crs": "EPSG:4326",
      "properties": { "name": "Boundary path", "folder": "Paths" },
      "measurement": {
        "status": "MEASURED",
        "area_m2": null,
        "length_m": 765.5,
        "projected_crs": "EPSG:32643",
        "note": null
      },
      "geometry": null
    },
    {
      "index": 2,
      "geometry_type": "Point",
      "crs": "EPSG:4326",
      "properties": { "name": "Survey marker" },
      "measurement": {
        "status": "NOT_APPLICABLE",
        "area_m2": null,
        "length_m": null,
        "projected_crs": null,
        "note": "No measurement is defined for points"
      },
      "geometry": null
    }
  ]
}
```

> Numbers above are illustrative (hand-computed on the WGS 84 ellipsoid); your run may differ in the
> last decimals.

`measurement.status` values:

| Status | Meaning |
|---|---|
| `MEASURED` | `area_m2` (polygons) or `length_m` (lines) is populated |
| `NOT_APPLICABLE` | Points — nothing to measure |
| `UNSUPPORTED` | Geometry type not measurable (e.g. mixed geometry collections), missing/empty geometry, or coordinates that cannot be valid for the declared CRS. See `note` |
| `ERROR` | The feature's geometry could not be read, or measuring failed. See `note` |

One bad feature never fails the whole file. `409` is returned if the file's status is not `COMPLETED`.

---

## Architecture

### Application structure

```
app/
  main.py              FastAPI app, router registration, table creation on startup
  config.py            Env-driven settings and limits
  database.py          SQLAlchemy engine / session dependency
  models.py            UploadedFile (1) ── (n) Feature
  schemas.py           Pydantic response models
  routers/files.py     HTTP layer: validation, status codes, pagination
  services/
    readers.py         KML reader + zipped-Shapefile reader -> ParsedFile
    crs.py             CRS labelling, transformer cache, projected-CRS selection
    measurements.py    Measurer: area/length per geometry, status handling
    ingest.py          Orchestrates detect -> parse -> measure -> persist
tests/                 Unit tests (measurements) + end-to-end API tests
```

The router only deals with HTTP; all geospatial logic lives in `services/` and is testable without a server.

### File-processing flow

1. `POST /api/files/` validates filename, size and emptiness, then reads the upload into memory.
2. `ingest_file` picks the reader from the extension (`.kml` → KML, `.zip` → Shapefile).
3. The reader returns a `ParsedFile`: a CRS plus a list of `ParsedFeature`s (index, shapely geometry,
   properties, optional per-feature error).
   - **KML**: parsed with `defusedxml` (blocks entity-expansion attacks), namespace-agnostic. Folders,
     `ExtendedData` and `SimpleData` become properties. `MultiGeometry` becomes a Multi* geometry (or a
     `GeometryCollection` when mixed). Altitude is dropped.
   - **Shapefile**: the ZIP is read in memory (nothing is extracted to disk, so no zip-slip). Exactly one
     `.shp` is required; `.shx`, `.dbf`, `.prj` and `.cpg` (encoding) are used when present.
4. Each feature is measured (below) and stored together with its GeoJSON geometry, CRS, properties and
   measurement. Results are computed **once at upload**, so `GET .../measurements/` is a cheap read.
5. The file record becomes `COMPLETED` (or `FAILED` with an error message if the content is unreadable).

### Measurement calculation flow

```
geometry + source CRS
   │
   ├─ Point / MultiPoint ............................. NOT_APPLICABLE
   ├─ other types / empty / missing .................. UNSUPPORTED
   │
   ├─ source CRS is projected ........................ measure in place, convert linear unit to metres
   │
   └─ source CRS is geographic (e.g. EPSG:4326)
        ├─ sanity-check coordinate ranges
        ├─ centroid -> WGS 84 lon/lat
        ├─ polygons   -> reproject to local Lambert Azimuthal Equal-Area -> .area
        └─ lines      -> reproject to the UTM zone of the centroid     -> .length
```

Supported: `Polygon`, `MultiPolygon` (area); `LineString`, `LinearRing`, `MultiLineString` (length).

### CRS handling

- **KML** is always WGS 84 (EPSG:4326) by specification.
- **Shapefile** CRS comes from the `.prj` (WKT, including Esri flavour) via `pyproj`. If it is missing or
  unparsable, EPSG:4326 is assumed and a **warning** is added to the file record.
- Labels use the authority code when one can be identified (`EPSG:4326`), otherwise the CRS name.
- Transformations use `always_xy=True`, so axis-order differences between CRS definitions cannot swap
  coordinates, and transformers are cached.
- Data declared geographic whose coordinates are outside lon ±180 / lat ±90 (typically projected data
  with no `.prj`) is reported as `UNSUPPORTED` instead of producing nonsense numbers.

---

## Design decisions

| Decision | Why | Alternatives considered |
|---|---|---|
| **FastAPI** | Typed request/response models, automatic OpenAPI docs, low boilerplate for a small service | Django + DRF: heavier than needed here |
| **Pure-Python readers** (`pyshp`, `defusedxml`, `shapely`, `pyproj`) | Installs from wheels everywhere; no GDAL/system packages; easy Docker image | `geopandas`/`fiona`/`pyogrio`: broader format support but GDAL makes installs and images heavier. Easy to swap in later behind `readers.py` |
| **Equal-area projection for area, UTM for length** | LAEA is equal-area by construction, so area stays correct anywhere on Earth with no zone edges. UTM keeps distance error tiny and is a familiar, EPSG-coded choice | One UTM zone for everything: simpler, but area error grows for large or zone-straddling features. Geodesic calculation (`pyproj.Geod`): very accurate but the brief asks for a projected CRS (I use `Geod` in tests as an independent cross-check) |
| **Per-feature projection** | A file can span several UTM zones; choosing per feature avoids one wrong global choice | One CRS per file: cheaper, wrong for wide-area files |
| **Measure at upload, store results** | Reads are fast; measurements are reproducible and don't need the raw file | Compute on request: avoids storage but repeats work |
| **Synchronous processing** | Simple and deterministic for the file sizes targeted; the sync endpoint runs in FastAPI's thread pool so the event loop is not blocked | Background tasks / Celery queue: better for very large files (see future scope) |
| **Per-feature failure isolation** | One bad feature should not discard a 10,000-feature survey; each has a status and note | Fail the whole file on first error |
| **`FAILED` files are kept and return `422`** | Client gets an immediate error and an `id` it can inspect later | Return only an error and store nothing |
| **SQLite via SQLAlchemy** | Zero setup; switching to PostgreSQL is a `DATABASE_URL` change | PostGIS: powerful spatial queries, unnecessary for this scope |
| **Security limits** | Size cap, zip-bomb guard, feature cap, no disk extraction, XML entity expansion disabled | — |
| **Multi-geometries supported** | `MultiPolygon`/`MultiLineString` are common in real data and trivial to measure | Treat as unsupported |

### Known limitations

- Geometries crossing the antimeridian (±180°) may be assigned a misleading centroid/projection.
- Polygon edges are treated as straight lines in lon/lat before projection; for very large polygons
  (hundreds of km) densifying edges first would be more faithful.
- KMZ, GeoJSON, GeoPackage and 3D measurements are not supported; altitude is ignored.
- Invalid polygons (e.g. self-intersecting) are still measured but flagged in `note`.

---

## Testing

```bash
pip install -r requirements-dev.txt
pytest -v
```

- `tests/test_measurements.py` — CRS selection (UTM zones, poles), area/length accuracy against
  `pyproj.Geod` geodesic results, holes, multipolygons, unit conversion (US survey feet), unsupported and
  invalid geometries.
- `tests/test_api.py` — upload → info → measurements for KML and Shapefile (geographic and projected),
  attributes, pagination, and error paths (415/400/413/422/404/409, malformed XML, entity-expansion
  attack, bad ZIPs).

---

## Learnings

- **Degrees are not metres.** The same 0.01° square covers very different ground at the equator and near
  the poles, so measuring before projecting is simply wrong, not just imprecise.
- **Projection choice depends on the question.** Area wants an equal-area projection; distance wants a
  conformal one with small scale error. No single CRS is ideal for both.
- **Axis order is a real trap.** EPSG:4326 is officially lat/lon while files store lon/lat;
  `always_xy=True` removes the ambiguity.
- **Shapefiles are really a bundle** (`.shp/.shx/.dbf/.prj/.cpg`), and a missing `.prj` is common, so the
  API must degrade gracefully and say what it assumed.
- **Untrusted uploads need defensive handling**: zip bombs, XML entity expansion, giant files and
  malformed geometry all need explicit guards.
- **Failure isolation matters more than strictness** for batch geospatial data: report per-feature
  problems instead of rejecting the file.

## Future scope

- **Async processing** (Celery/RQ or a task queue) with `PROCESSING` status polling and webhooks for very large files.
- **More formats**: KMZ, GeoJSON, GeoPackage, File Geodatabase (via `pyogrio`/GDAL as an optional backend).
- **PostGIS storage** for spatial queries (features within a bounding box, intersections, dissolve).
- **Geodesic cross-check / densification** of long edges for continental-scale polygons.
- **Antimeridian and polar handling** for geometries spanning ±180°.
- **User-supplied CRS override** for shapefiles without `.prj`, and optional output units (hectares, acres, km).
- **Auth, rate limiting, object storage** for original uploads, and delete/reprocess endpoints.
- **Alembic migrations**, structured logging, metrics and CI (lint, tests) on every push.
