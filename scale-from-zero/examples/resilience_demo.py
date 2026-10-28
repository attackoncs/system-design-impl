"""Online Web-pool changes, fenced cache fills and explicit edge fallback."""

import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from lab import Lab
from scale_from_zero.assets import fetch_asset

with Lab("balanced", web_nodes=3) as lab:
    lab.login()
    lab.update()
    lab.caught_up()
    previous = lab.call("/v1/profile?min_version=1")
    ticket = lab.call(
        "/internal/get",
        {"key": lab.user, "min_version": 0},
        node="cache",
        auth="internal",
    )
    lab.update(1, "second")
    stale = {key: previous[key] for key in ("user", "version", "profile")}
    fill = lab.call(
        "/internal/put",
        {"key": lab.user, "value": stale, "generation": ticket["generation"]},
        node="cache",
        auth="internal",
    )
    current = lab.call("/v1/profile")
    lab.call("/admin/backends", {"nodes": ["web0"]}, node="lb", auth="admin")
    reduced = lab.call("/v1/profile")["web_node"]
    lab.call(
        "/admin/backends", {"nodes": ["web0", "web1", "web2"]}, node="lb", auth="admin"
    )
    expanded = sorted({lab.call("/v1/profile")["web_node"] for _ in range(6)})
    edge = fetch_asset(lab.config, "app-v1.txt")
    lab.call("/admin/invalidate", {"asset": "app-v1.txt"}, node="edge", auth="admin")
    invalidated = lab.call("/admin/status", node="edge", auth="admin")["entries"]
    lab.stop("edge")
    origin = fetch_asset(lab.config, "app-v1.txt")
    lab.drained()
    result = {
        "stage": "balanced",
        "configured_processes": 9,
        "stale_fill_accepted": fill["stored"],
        "current_version": current["version"],
        "reduced_node": reduced,
        "expanded_nodes": expanded,
        "edge_source": edge["asset_source"],
        "edge_entries_after_invalidate": invalidated,
        "fallback_source": origin["asset_source"],
        "consumer": lab.call("/admin/status", node="worker", auth="admin"),
    }
    assert not fill["stored"] and current["version"] == 2
    assert reduced == "web0" and expanded == ["web0", "web1", "web2"]
    assert invalidated == 0 and origin["asset_source"] == "origin_fallback"
    assert result["consumer"]["unique_events"] == 2
    print(json.dumps(result, indent=2))
