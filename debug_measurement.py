from shapely.geometry import Polygon
from app.services.measurements import Measurer
from app import config

WGS84 = config.WGS84
poly = Polygon([(77.59, 12.97), (77.60, 12.97), (77.60, 12.98), (77.59, 12.98)])
result = Measurer(WGS84).measure(poly)
print(result)
print(result.note)
