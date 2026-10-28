"""Classified road tiles with explicit cross-tile and cross-level entrances."""

import json

from .geo import cell, tile
from .store import Error


def _limit(value, name):
    if type(value) is not int or not 1 <= value <= 100000:
        raise ValueError(f"{name} must be an integer in 1..100000")
    return value


def _nodes(db, z, limits, classes, maximum):
    query = (
        "SELECT n.* FROM nodes n WHERE n.tx BETWEEN ? AND ? AND n.ty BETWEEN ? AND ?"
    )
    parameters = list(limits)
    if z != 16:
        marks = ",".join("?" for _ in classes)
        query += (
            f" AND (EXISTS(SELECT 1 FROM edges e WHERE e.source=n.id AND e.road_class IN ({marks}))"
            f" OR EXISTS(SELECT 1 FROM edges e WHERE e.target=n.id AND e.road_class IN ({marks})))"
        )
        parameters.extend((*classes, *classes))
    result = [
        dict(row)
        for row in db.execute(
            query + " ORDER BY n.id LIMIT ?", (*parameters, maximum + 1)
        )
    ]
    if len(result) > maximum:
        raise Error(413, "routing tile node budget exceeded")
    return result


def _edges(db, z, limits, classes, maximum):
    marks = ",".join("?" for _ in classes)
    result = []
    for row in db.execute(
        "SELECT e.*,n.lat AS source_lat,n.lon AS source_lon,t.lat AS target_lat,t.lon AS target_lon "
        "FROM edges e JOIN nodes n ON n.id=e.source JOIN nodes t ON t.id=e.target "
        f"WHERE n.tx BETWEEN ? AND ? AND n.ty BETWEEN ? AND ? AND e.road_class IN ({marks}) "
        "ORDER BY e.source,e.id LIMIT ?",
        (*limits, *classes, maximum + 1),
    ):
        if len(result) >= maximum:
            raise Error(413, "routing tile edge budget exceeded")
        edge = {
            key: row[key]
            for key in ("id", "source", "target", "length", "toll", "road_class")
        }
        edge["speeds"] = json.loads(row["speeds"])
        edge["source_tile"] = [z, *cell(row["source_lat"], row["source_lon"], z)]
        edge["target_tile"] = [z, *cell(row["target_lat"], row["target_lon"], z)]
        result.append(edge)
    return result


def _portals(db, nodes, z, x, y, maximum):
    levels = {node["id"]: {16} for node in nodes}
    incident = set()
    # Incoming roads matter too: an intersection may have only a local outgoing
    # road in this tile, while a highway from a neighboring tile enters it.
    for start in range(0, len(nodes), 256):
        identifiers = [node["id"] for node in nodes[start : start + 256]]
        placeholders = ",".join("?" for _ in identifiers)
        for row in db.execute(
            f"SELECT id,source,target,road_class FROM edges WHERE (source IN ({placeholders}) OR target IN ({placeholders})) AND road_class!='local' LIMIT ?",
            (*identifiers, *identifiers, maximum + 1),
        ):
            incident.add(row["id"])
            if len(incident) > maximum:
                raise Error(413, "routing tile entrance budget exceeded")
            available = (12, 8) if row["road_class"] == "highway" else (12,)
            for identifier in (row["source"], row["target"]):
                if identifier in levels:
                    levels[identifier].update(available)
    return [
        {
            "node": node["id"],
            "from": [z, x, y],
            "to": [level, *cell(node["lat"], node["lon"], level)],
        }
        for node in nodes
        for level in sorted(levels[node["id"]])
        if level != z
    ]


def _relatives(nodes, z, x, y):
    next_zoom = {8: 12, 12: 16}.get(z)
    children = (
        sorted(
            {(next_zoom, *cell(node["lat"], node["lon"], next_zoom)) for node in nodes}
        )
        if next_zoom is not None
        else []
    )
    parent_zoom = {16: 12, 12: 8}.get(z)
    parent = [parent_zoom, x // 16, y // 16] if parent_zoom is not None else None
    return parent, [list(child) for child in children]


def routing_tile(store, graph, x, y, z=16, *, max_nodes=5000, max_edges=5000):
    """Load road-class views; exact route search deliberately stays at level 16.

    Levels 16, 12 and 8 contain all roads, arterials/highways and highways,
    respectively. Shared node IDs give topology-preserving cross-level entrances.
    These views do not claim an unproven hierarchical shortcut or speedup.
    """
    if type(z) is not int or z not in (8, 12, 16):
        raise ValueError("routing zoom must be 8,12,16")
    _, x, y = tile(z, x, y)
    max_nodes, max_edges = (
        _limit(max_nodes, "max_nodes"),
        _limit(max_edges, "max_edges"),
    )
    classes = {
        16: ("local", "arterial", "highway"),
        12: ("arterial", "highway"),
        8: ("highway",),
    }[z]
    scale = 2 ** (16 - z)
    limits = (x * scale, (x + 1) * scale - 1, y * scale, (y + 1) * scale - 1)
    with store.transaction() as db:
        if store.ready(db)["graph"] != graph:
            raise Error(410, "graph version no longer available")
        nodes = _nodes(db, z, limits, classes, max_nodes)
        edges = _edges(db, z, limits, classes, max_edges)
        portals = _portals(db, nodes, z, x, y, max_edges)
        parent, children = _relatives(nodes, z, x, y)
        return {
            "graph": graph,
            "tile": [z, x, y],
            "nodes": nodes,
            "edges": edges,
            "road_classes": list(classes),
            "parent": parent,
            "children": children,
            "portals": portals,
            "routing_strategy": "classified views; exact path search remains at level 16",
        }
