import heapq
import json

from .store import Error,MODES


def route(store,origin,destination,mode="driving",avoid_tolls=False,alternatives=1,max_nodes=50000):
    if mode not in MODES or type(avoid_tolls) is not bool or type(alternatives) is not int or not 1<=alternatives<=3 or type(max_nodes) is not int or not 1<=max_nodes<=100000:
        raise ValueError("invalid routing options")
    with store.transaction() as db:
        meta=store.ready(db)
        source=store.resolve(db,origin)
        target=store.resolve(db,destination)
        traffic={row["edge"]:dict(row) for row in db.execute("SELECT * FROM traffic WHERE expires>?",(store.clock(),))} if mode=="driving" else {}
        loaded=set()
        visited_total=0
        def shortest(banned=frozenset()):
            nonlocal visited_total
            frontier=[(0.0,source["id"])]
            costs={source["id"]:0.0}
            parents={}
            visited=set()
            while frontier:
                cost,node_id=heapq.heappop(frontier)
                if node_id in visited:
                    continue
                visited.add(node_id)
                visited_total+=1
                if visited_total>max_nodes:
                    raise Error(413,"route search budget exceeded")
                if node_id==target["id"]:
                    nodes=[node_id]
                    edges=[]
                    while node_id!=source["id"]:
                        previous,edge=parents[node_id]
                        edges.append(edge)
                        nodes.append(previous)
                        node_id=previous
                    edges.reverse()
                    nodes.reverse()
                    return {"nodes":nodes,"edges":[e["id"] for e in edges],"distance_m":sum(e["length"] for e in edges),"eta_seconds":cost}
                node=dict(db.execute("SELECT * FROM nodes WHERE id=?",(node_id,)).fetchone())
                for edge in store.outgoing(db,node,meta["graph"],loaded):
                    if edge["id"] in banned or mode not in edge["speeds"] or avoid_tolls and edge["toll"]:
                        continue
                    state=traffic.get(edge["id"])
                    if state and state["closed"]:
                        continue
                    speed=state["speed"] if state and state["speed"] is not None else edge["speeds"][mode]
                    value=cost+edge["length"]/speed
                    if value<costs.get(edge["target"],float("inf")):
                        costs[edge["target"]]=value
                        parents[edge["target"]]=(node_id,edge)
                        heapq.heappush(frontier,(value,edge["target"]))
            return None
        best=shortest()
        if best is None:
            raise Error(404,"destination unreachable for selected mode/options")
        candidates={tuple(best["edges"]):best}
        if alternatives>1:
            for edge in best["edges"][:64]:
                candidate=shortest(frozenset([edge]))
                if candidate:
                    candidates[tuple(candidate["edges"])]=candidate
        ranked=sorted(candidates.values(),key=lambda row:(row["eta_seconds"],row["edges"]))[:alternatives]
        for candidate in ranked:
            candidate["geometry"]=[[row["lon"],row["lat"]] for node_id in candidate["nodes"] for row in [db.execute("SELECT lat,lon FROM nodes WHERE id=?",(node_id,)).fetchone()]]
        return {"graph":meta["graph"],"revision":meta["revision"],"traffic_version":meta["traffic_version"],"mode":mode,"routes":ranked,"routing_tiles_loaded":len(loaded),"nodes_visited":visited_total,"candidate_method":"bounded single-edge avoidance"}
