from .geo import clipped_segments,png,tile,unproject
from .store import Error,dumps


def render_tile(store,graph,z,x,y,format="geojson"):
    z,x,y=tile(z,x,y)
    if format not in ("geojson","png"):
        raise ValueError("unsupported tile format")
    with store.transaction() as db:
        meta=store.ready(db)
        if graph!=meta["graph"]:
            raise Error(410,"graph version no longer available at origin")
        def load():
            n=2**z
            north,west=unproject(x/n,y/n)
            south,east=unproject((x+1)/n,(y+1)/n)
            if y==0:
                north=90
            if y==n-1:
                south=-90
            features=[]
            segments=[]
            for edge in store.spatial_edges(db,west,east,south,north):
                a,b=(edge["alat"],edge["alon"]),(edge["blat"],edge["blon"])
                for index,(start,end) in enumerate(clipped_segments(a,b,z,x,y)):
                    segments.append((start,end))
                    coordinates=[]
                    for xx,yy in (start,end):
                        lat,lon=unproject((x+xx)/n,(y+yy)/n)
                        coordinates.append([lon,lat])
                    features.append({"type":"Feature","id":edge["id"]+":"+str(index),"properties":{"edge":edge["id"],"toll":bool(edge["toll"])},"geometry":{"type":"LineString","coordinates":coordinates}})
            if format=="png":
                return png(segments),"image/png"
            return dumps({"type":"FeatureCollection","features":features,"graph":graph,"tile":[z,x,y]}).encode(),"application/geo+json"
        return store.tiles.get((graph,z,x,y,format),load)


def routing_tile(store,graph,x,y):
    _,x,y=tile(16,x,y)
    with store.transaction() as db:
        if store.ready(db)["graph"]!=graph:
            raise Error(410,"graph version no longer available")
        nodes=[dict(r) for r in db.execute("SELECT * FROM nodes WHERE tx=? AND ty=? ORDER BY id",(x,y))]
        edges=[]
        for node in nodes:
            edges.extend(store.outgoing(db,node,graph,set()))
        return {"graph":graph,"tile":[16,x,y],"nodes":nodes,"edges":edges}
