# Atlantic land outlines

`atlantic-land.json` contains Natural Earth 1:110m land polygons, public domain.
The source URL is pinned to a repository commit; the SHA-256 of the downloaded
GeoJSON and the terms URL are recorded in the JSON asset.

Processing: retain each Polygon whose outer-ring bounding box intersects
100°W–20°E, 0–70°N; round coordinates to four decimals. Preserve full exterior
and interior rings. The runtime converts them with the same plate-carree
projection as track points. The North America/western Atlantic display clips
that broader asset to 100–45°W, 5–60°N, retaining the Gulf, Caribbean and eastern
Canada while hiding remote eastern-Atlantic systems. Display counts use the
same region; source snapshots keep the broader Atlantic data. This is narrower
than the NHC full-Atlantic overview, not a copied official NHC domain or an
impact mask. No online tile service, geometry library, or authenticated access
is required at runtime.

This small-scale outline includes major islands, not every small island.
The geography is a display aid, not a navigation chart or storm-impact mask.
