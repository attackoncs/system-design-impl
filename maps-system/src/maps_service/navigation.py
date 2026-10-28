import heapq
import math

from .geo import bearing, coordinates
from .store import Error, MODES


def encode_polyline(geometry):
    """Google encoded-polyline precision 5 from [longitude, latitude] points."""
    previous = [0, 0]
    output = []
    for longitude, latitude in geometry:
        latitude, longitude = coordinates(latitude, longitude)
        for index, value in enumerate((latitude, longitude)):
            scaled = value * 100000
            current = (
                math.floor(scaled + 0.5) if scaled >= 0 else math.ceil(scaled - 0.5)
            )
            delta = current - previous[index]
            previous[index] = current
            encoded = ~(delta << 1) if delta < 0 else delta << 1
            while encoded >= 0x20:
                output.append(chr((0x20 | (encoded & 0x1F)) + 63))
                encoded >>= 5
            output.append(chr(encoded + 63))
    return "".join(output)


def _instructions(edges, nodes, mode, traffic):
    steps = []
    previous_heading = None
    compass = (
        "north",
        "northeast",
        "east",
        "southeast",
        "south",
        "southwest",
        "west",
        "northwest",
    )
    for index, edge in enumerate(edges):
        source, target = nodes[edge["source"]], nodes[edge["target"]]
        start, end = (source["lat"], source["lon"]), (target["lat"], target["lon"])
        heading = bearing(start, end)
        if index == 0:
            maneuver = "depart"
            instruction = (
                f"Head {compass[int((heading + 22.5) % 360 // 45)]} along road {edge['id']}."
                if heading is not None
                else f"Take road {edge['id']}."
            )
        else:
            turn = (
                (heading - previous_heading + 180) % 360 - 180
                if heading is not None and previous_heading is not None
                else 0
            )
            if abs(turn) >= 150:
                maneuver, action = "u-turn", "Make a U-turn onto"
            elif turn >= 25:
                maneuver, action = "turn-right", "Turn right onto"
            elif turn <= -25:
                maneuver, action = "turn-left", "Turn left onto"
            else:
                maneuver, action = "continue", "Continue along"
            instruction = f"{action} road {edge['id']}."
        state = traffic.get(edge["id"])
        speed = (
            state["speed"]
            if state and state["speed"] is not None
            else edge["speeds"][mode]
        )
        steps.append(
            {
                "edge": edge["id"],
                "maneuver": maneuver,
                "instruction": instruction,
                "start_location": {"latitude": start[0], "longitude": start[1]},
                "end_location": {"latitude": end[0], "longitude": end[1]},
                "distance_m": edge["length"],
                "duration_seconds": edge["length"] / speed,
            }
        )
        previous_heading = heading
    return steps


def route(
    store,
    origin,
    destination,
    mode="driving",
    avoid_tolls=False,
    alternatives=1,
    max_nodes=50000,
):
    if (
        not isinstance(mode, str)
        or mode not in MODES
        or type(avoid_tolls) is not bool
        or type(alternatives) is not int
        or not 1 <= alternatives <= 3
        or type(max_nodes) is not int
        or not 1 <= max_nodes <= 100000
    ):
        raise ValueError("invalid routing options")
    with store.transaction() as db:
        meta = store.ready(db)
        source = store.resolve(db, origin)
        target = store.resolve(db, destination)
        nodes = {source["id"]: source, target["id"]: target}
        traffic = (
            {
                row["edge"]: dict(row)
                for row in db.execute(
                    "SELECT * FROM traffic WHERE expires>?", (store.clock(),)
                )
            }
            if mode == "driving"
            else {}
        )
        loaded = set()
        visited_total = 0

        def shortest(banned=frozenset()):
            nonlocal visited_total
            frontier = [(0.0, source["id"])]
            costs = {source["id"]: 0.0}
            parents = {}
            visited = set()
            while frontier:
                cost, node_id = heapq.heappop(frontier)
                if node_id in visited:
                    continue
                visited.add(node_id)
                visited_total += 1
                if visited_total > max_nodes:
                    raise Error(413, "route search budget exceeded")
                if node_id == target["id"]:
                    path_nodes = [node_id]
                    edges = []
                    while node_id != source["id"]:
                        previous, edge = parents[node_id]
                        edges.append(edge)
                        path_nodes.append(previous)
                        node_id = previous
                    edges.reverse()
                    path_nodes.reverse()
                    return {
                        "nodes": path_nodes,
                        "edges": [e["id"] for e in edges],
                        "_edges": edges,
                        "distance_m": sum(e["length"] for e in edges),
                        "eta_seconds": cost,
                    }
                node = node_record(node_id)
                for edge in store.outgoing(db, node, meta["graph"], loaded):
                    if (
                        edge["id"] in banned
                        or mode not in edge["speeds"]
                        or avoid_tolls
                        and edge["toll"]
                    ):
                        continue
                    state = traffic.get(edge["id"])
                    if state and state["closed"]:
                        continue
                    speed = (
                        state["speed"]
                        if state and state["speed"] is not None
                        else edge["speeds"][mode]
                    )
                    value = cost + edge["length"] / speed
                    if value < costs.get(edge["target"], float("inf")):
                        costs[edge["target"]] = value
                        parents[edge["target"]] = (node_id, edge)
                        heapq.heappush(frontier, (value, edge["target"]))
            return None

        def node_record(node_id):
            if node_id not in nodes:
                row = db.execute(
                    "SELECT * FROM nodes WHERE id=?", (node_id,)
                ).fetchone()
                if row is None:
                    raise Error(503, "routing tile references a missing node")
                nodes[node_id] = dict(row)
            return nodes[node_id]

        best = shortest()
        if best is None:
            raise Error(404, "destination unreachable for selected mode/options")
        candidates = {tuple(best["edges"]): best}
        if alternatives > 1:
            for edge in best["edges"][:64]:
                candidate = shortest(frozenset([edge]))
                if candidate:
                    candidates[tuple(candidate["edges"])] = candidate
        ranked = sorted(
            candidates.values(), key=lambda row: (row["eta_seconds"], row["edges"])
        )[:alternatives]
        for candidate in ranked:
            candidate["geometry"] = [
                [node_record(node_id)["lon"], node_record(node_id)["lat"]]
                for node_id in candidate["nodes"]
            ]
            candidate["polyline"] = {
                "points": encode_polyline(candidate["geometry"]),
                "precision": 5,
            }
            candidate["steps"] = _instructions(
                candidate.pop("_edges"), nodes, mode, traffic
            )
            candidate["arrival_instruction"] = "Arrive at destination."
        return {
            "graph": meta["graph"],
            "revision": meta["revision"],
            "traffic_version": meta["traffic_version"],
            "mode": mode,
            "routes": ranked,
            "routing_tiles_loaded": len(loaded),
            "nodes_visited": visited_total,
            "candidate_method": "bounded single-edge avoidance",
            "routing_strategy": "exact fine-level Dijkstra",
            "routing_levels_used": [16],
        }
