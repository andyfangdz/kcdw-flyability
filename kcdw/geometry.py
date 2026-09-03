from __future__ import annotations


def point_in_polygon(lon: float, lat: float, polygon: list[list[float]]) -> bool:
    """Ray casting with boundary treated as inside; coordinates are [lon, lat]."""
    if len(polygon) < 3:
        return False
    inside = False
    j = len(polygon) - 1
    for i, (xi, yi) in enumerate(polygon):
        xj, yj = polygon[j]
        cross = (lon - xi) * (yj - yi) - (lat - yi) * (xj - xi)
        if abs(cross) < 1e-10 and min(xi, xj) - 1e-10 <= lon <= max(xi, xj) + 1e-10 and min(yi, yj) - 1e-10 <= lat <= max(yi, yj) + 1e-10:
            return True
        if (yi > lat) != (yj > lat):
            x_at_lat = (xj - xi) * (lat - yi) / (yj - yi) + xi
            if lon < x_at_lat:
                inside = not inside
        j = i
    return inside


def geometry_contains(geometry: dict | None, lon: float, lat: float) -> bool:
    if not geometry:
        return False
    coordinates = geometry.get("coordinates", [])
    if geometry.get("type") == "Polygon":
        return bool(coordinates and point_in_polygon(lon, lat, coordinates[0]))
    if geometry.get("type") == "MultiPolygon":
        return any(poly and point_in_polygon(lon, lat, poly[0]) for poly in coordinates)
    return False
