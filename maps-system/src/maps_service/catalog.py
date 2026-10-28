"""Validate and encode a complete graph before taking the SQLite writer."""

import hashlib
import uuid

from .geo import boxes, cell, coordinates, distance
from .models import Error, MODES, dumps, finite, identifier, positive

ROAD_CLASSES = {"local", "arterial", "highway"}


def prepare_graph(graph):
    if not isinstance(graph, dict) or set(graph) - {
        "nodes",
        "edges",
        "addresses",
        "license",
    }:
        raise ValueError("invalid graph document")
    license = graph.get("license", "unspecified")
    if not isinstance(license, str) or not 1 <= len(license) <= 2048:
        raise ValueError("invalid dataset license")
    nodes, edges = graph.get("nodes"), graph.get("edges")
    if not isinstance(nodes, list) or not 1 <= len(nodes) <= 100000:
        raise ValueError("graph requires 1..100000 nodes")
    if not isinstance(edges, list) or len(edges) > 500000:
        raise ValueError("invalid edge count")
    points, node_rows = {}, []
    for node in nodes:
        if not isinstance(node, dict) or set(node) != {"id", "latitude", "longitude"}:
            raise ValueError("invalid graph node")
        name = identifier(node["id"])
        if name in points:
            raise ValueError("duplicate node ID")
        lat, lon = coordinates(node["latitude"], node["longitude"])
        points[name] = (lat, lon)
        tx, ty = cell(lat, lon)
        node_rows.append((name, lat, lon, tx, ty))
    edge_rows, spatial_rows, edge_names = [], [], set()
    document_edges = []
    for edge in edges:
        if not isinstance(edge, dict) or set(edge) - {
            "id",
            "source",
            "target",
            "length_m",
            "speeds",
            "toll",
            "road_class",
        }:
            raise ValueError("invalid graph edge")
        name = identifier(edge.get("id"))
        if name in edge_names:
            raise ValueError("duplicate edge ID")
        edge_names.add(name)
        source, target = edge.get("source"), edge.get("target")
        identifier(source)
        identifier(target)
        if source == target or source not in points or target not in points:
            raise ValueError("invalid road endpoints")
        length = positive(
            edge["length_m"]
            if "length_m" in edge
            else distance(points[source], points[target])
        )
        speeds = edge.get("speeds")
        if not isinstance(speeds, dict) or not speeds or set(speeds) - MODES:
            raise ValueError("invalid travel modes")
        speeds = {mode: positive(speed, 100) for mode, speed in speeds.items()}
        toll, road_class = edge.get("toll", False), edge.get("road_class", "local")
        if (
            type(toll) is not bool
            or not isinstance(road_class, str)
            or road_class not in ROAD_CLASSES
        ):
            raise ValueError("invalid toll/road class")
        edge_rows.append(
            (name, source, target, length, dumps(speeds), int(toll), road_class)
        )
        for west, east, south, north in boxes(points[source], points[target]):
            spatial_rows.append((name, west, east, south, north))
        item = {
            "id": name,
            "source": source,
            "target": target,
            "length_m": length,
            "speeds": speeds,
            "toll": toll,
        }
        # Keep old all-local catalog hashes stable during schema migration.
        if road_class != "local":
            item["road_class"] = road_class
        document_edges.append(item)
    addresses = graph.get("addresses", {})
    if not isinstance(addresses, dict) or len(addresses) > 100000:
        raise ValueError("invalid fixture addresses")
    address_rows = {}
    for name, node in addresses.items():
        if not isinstance(name, str) or not name.strip() or len(name) > 500:
            raise ValueError("invalid fixture address")
        identifier(node)
        normalized = name.strip().casefold()
        if node not in points or normalized in address_rows:
            raise ValueError("invalid/duplicate fixture address")
        address_rows[normalized] = node
    document = {
        "nodes": [
            {"id": name, "latitude": lat, "longitude": lon}
            for name, lat, lon, _tx, _ty in sorted(node_rows)
        ],
        "edges": sorted(document_edges, key=lambda item: item["id"]),
        "addresses": dict(sorted(address_rows.items())),
        "license": license,
    }
    return {
        "nodes": node_rows,
        "edges": edge_rows,
        "spatial": spatial_rows,
        "addresses": list(address_rows.items()),
        "license": license,
        "hash": hashlib.sha256(dumps(document).encode()).hexdigest(),
        "edge_ids": edge_names,
    }


def install_graph(db, prepared):
    for table in (
        "spatial",
        "edge_boxes",
        "edges",
        "nodes",
        "addresses",
        "traffic",
        "samples",
    ):
        db.execute("DELETE FROM " + table)
    db.executemany("INSERT INTO nodes VALUES(?,?,?,?,?)", prepared["nodes"])
    db.executemany(
        "INSERT INTO edges(id,source,target,length,speeds,toll,road_class) VALUES(?,?,?,?,?,?,?)",
        prepared["edges"],
    )
    db.executemany("INSERT INTO addresses VALUES(?,?)", prepared["addresses"])
    boxes_rows, spatial_rows = [], []
    for number, (edge, west, east, south, north) in enumerate(prepared["spatial"], 1):
        boxes_rows.append((number, edge))
        spatial_rows.append((number, west, east, south, north))
    db.executemany("INSERT INTO edge_boxes VALUES(?,?)", boxes_rows)
    db.executemany("INSERT INTO spatial VALUES(?,?,?,?,?)", spatial_rows)


def prepare_snapshot(snapshot):
    if not isinstance(snapshot, dict) or set(snapshot) != {"meta", "graph", "traffic"}:
        raise ValueError("invalid snapshot shape")
    if not isinstance(snapshot["meta"], dict):
        raise ValueError("invalid snapshot metadata")
    incoming = dict(snapshot["meta"])
    if set(incoming) != {
        "graph",
        "lineage",
        "revision",
        "traffic_version",
        "ready",
        "license",
        "graph_hash",
    }:
        raise ValueError("invalid snapshot metadata")
    for field in ("lineage", "graph"):
        if (
            not isinstance(incoming[field], str)
            or str(uuid.UUID(incoming[field])) != incoming[field]
        ):
            raise ValueError("invalid catalog UUID")
    for field in ("revision", "traffic_version"):
        if type(incoming[field]) is not int or not 0 <= incoming[field] < 2**63:
            raise ValueError("invalid catalog revision")
    if incoming["ready"] is not True:
        raise ValueError("snapshot must contain a ready graph")
    prepared = prepare_graph(snapshot["graph"])
    if (
        incoming["graph_hash"] != prepared["hash"]
        or incoming["license"] != prepared["license"]
    ):
        raise Error(409, "catalog checksum or metadata mismatch")
    records = snapshot["traffic"]
    if not isinstance(records, list) or len(records) > 500000:
        raise ValueError("invalid snapshot traffic")
    traffic, seen = [], set()
    for item in records:
        if not isinstance(item, dict) or set(item) != {
            "edge",
            "speed",
            "closed",
            "expires",
        }:
            raise ValueError("invalid snapshot traffic record")
        record = dict(item)
        edge = identifier(record["edge"])
        if edge in seen or edge not in prepared["edge_ids"]:
            raise ValueError("duplicate/missing traffic edge")
        seen.add(edge)
        if type(record["closed"]) not in (int, bool) or record["closed"] not in (0, 1):
            raise ValueError("invalid closed flag")
        record["closed"] = int(record["closed"])
        if not finite(record["expires"]) or record["expires"] < 0:
            raise ValueError("invalid traffic expiry")
        if record["speed"] is not None:
            record["speed"] = positive(record["speed"], 100)
        if not record["closed"] and record["speed"] is None:
            raise ValueError("traffic speed required unless closed")
        traffic.append(record)
    return incoming, prepared, sorted(traffic, key=lambda record: record["edge"])
