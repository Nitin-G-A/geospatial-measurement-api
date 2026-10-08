"""Generate samples/sample_shapefile.zip (two plots in EPSG:4326).

Usage:  python scripts/make_sample_shapefile.py
"""
import io
import zipfile
from pathlib import Path

import shapefile
from pyproj import CRS
from shapely.geometry import Polygon
from shapely.geometry.polygon import orient

PLOTS = {
    "Plot A": [(77.5946, 12.9716), (77.5996, 12.9716), (77.5996, 12.9766), (77.5946, 12.9766)],
    "Plot B": [(77.6010, 12.9716), (77.6050, 12.9716), (77.6050, 12.9750), (77.6010, 12.9750)],
}


def main() -> None:
    shp, shx, dbf = io.BytesIO(), io.BytesIO(), io.BytesIO()
    writer = shapefile.Writer(shp=shp, shx=shx, dbf=dbf, shapeType=shapefile.POLYGON)
    writer.field("name", "C", 40)
    writer.field("owner", "C", 40)
    for name, ring in PLOTS.items():
        # Shapefile outer rings must be clockwise.
        writer.poly([list(orient(Polygon(ring), sign=-1.0).exterior.coords)])
        writer.record(name, "Survey Dept")
    writer.close()

    out = Path(__file__).resolve().parent.parent / "samples" / "sample_shapefile.zip"
    out.parent.mkdir(exist_ok=True)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("plots.shp", shp.getvalue())
        archive.writestr("plots.shx", shx.getvalue())
        archive.writestr("plots.dbf", dbf.getvalue())
        archive.writestr("plots.prj", CRS.from_epsg(4326).to_wkt("WKT1_ESRI"))
    print(f"Wrote {out}")


if __name__ == "__main__":
    main()
