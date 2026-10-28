from .geo import cell, clipped_segments, png, tile, unproject
from .store import Error, dumps
from .routing_tiles import routing_tile as routing_tile


def tile_urls(store, latitude, longitude, z, format="png"):
    """Return version-pinned viewport seed URLs with wrapped adjacent cells."""
    if format not in ("geojson", "png"):
        raise ValueError("unsupported tile format")
    x, y = cell(latitude, longitude, z)
    n = 2**z
    cells = sorted(
        {
            ((x + dx) % n, max(0, min(n - 1, y + dy)))
            for dx in (-1, 0, 1)
            for dy in (-1, 0, 1)
        }
    )
    with store.transaction() as db:
        graph = store.ready(db)["graph"]
        return {
            "graph": graph,
            "zoom": z,
            "center": [x, y],
            "tiles": [
                {
                    "z": z,
                    "x": xx,
                    "y": yy,
                    "url": f"/v1/tiles/{graph}/{z}/{xx}/{yy}.{format}",
                }
                for xx, yy in cells
            ],
        }


def _budget(value, name):
    if type(value) is not int or not 1 <= value <= 100000:
        raise ValueError(f"{name} must be an integer in 1..100000")
    return value


def render_tile(
    store, graph, z, x, y, format="geojson", *, max_edges=5000, max_segments=10000
):
    z, x, y = tile(z, x, y)
    max_edges, max_segments = (
        _budget(max_edges, "max_edges"),
        _budget(max_segments, "max_segments"),
    )
    if format not in ("geojson", "png"):
        raise ValueError("unsupported tile format")
    with store.transaction() as db:
        meta = store.ready(db)
        if graph != meta["graph"]:
            raise Error(410, "graph version no longer available at origin")

        def load():
            archive = getattr(store, "tile_archive", None)
            if archive is not None:
                artifact = archive.get(graph, meta["graph_hash"], z, x, y, format)
                if artifact is not None:
                    return artifact
            n = 2**z
            north, west = unproject(x / n, y / n)
            south, east = unproject((x + 1) / n, (y + 1) / n)
            if y == 0:
                north = 90
            if y == n - 1:
                south = -90
            features = []
            segments = []
            candidates = store.spatial_edges(
                db, west, east, south, north, limit=max_edges + 1
            )
            if len(candidates) > max_edges:
                raise Error(
                    413, "tile render edge budget exceeded; choose a higher zoom"
                )
            for edge in candidates:
                a, b = (edge["alat"], edge["alon"]), (edge["blat"], edge["blon"])
                for index, (start, end) in enumerate(clipped_segments(a, b, z, x, y)):
                    segments.append((start, end))
                    if len(segments) > max_segments:
                        raise Error(413, "tile render segment budget exceeded")
                    if format == "png":
                        continue
                    coordinates = []
                    for xx, yy in (start, end):
                        lat, lon = unproject((x + xx) / n, (y + yy) / n)
                        coordinates.append([lon, lat])
                    features.append(
                        {
                            "type": "Feature",
                            "id": edge["id"] + ":" + str(index),
                            "properties": {
                                "edge": edge["id"],
                                "toll": bool(edge["toll"]),
                            },
                            "geometry": {
                                "type": "LineString",
                                "coordinates": coordinates,
                            },
                        }
                    )
            if format == "png":
                return png(segments), "image/png"
            return dumps(
                {
                    "type": "FeatureCollection",
                    "features": features,
                    "graph": graph,
                    "tile": [z, x, y],
                }
            ).encode(), "application/geo+json"

        return store.tiles.get((graph, z, x, y, format), load)
