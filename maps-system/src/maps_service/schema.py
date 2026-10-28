"""Idempotent public catalog and private telemetry schema."""

SCHEMA = """
CREATE TABLE IF NOT EXISTS graph_versions(graph TEXT PRIMARY KEY,hash TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS config(key TEXT PRIMARY KEY,value TEXT);
CREATE TABLE IF NOT EXISTS nodes(id TEXT PRIMARY KEY,lat REAL,lon REAL,tx INTEGER,ty INTEGER);
CREATE INDEX IF NOT EXISTS nodes_tile ON nodes(tx,ty);
CREATE INDEX IF NOT EXISTS nodes_lat_lon ON nodes(lat,lon);
CREATE TABLE IF NOT EXISTS edges(id TEXT PRIMARY KEY,source TEXT,target TEXT,length REAL,speeds TEXT,toll INTEGER,road_class TEXT NOT NULL DEFAULT 'local');
CREATE INDEX IF NOT EXISTS edges_source ON edges(source);
CREATE INDEX IF NOT EXISTS edges_target ON edges(target);
CREATE TABLE IF NOT EXISTS addresses(name TEXT PRIMARY KEY,node TEXT);
CREATE TABLE IF NOT EXISTS edge_boxes(id INTEGER PRIMARY KEY,edge TEXT);
CREATE VIRTUAL TABLE IF NOT EXISTS spatial USING rtree(id,west,east,south,north);
CREATE TABLE IF NOT EXISTS traffic(edge TEXT PRIMARY KEY,speed REAL,closed INTEGER,expires REAL);
CREATE TABLE IF NOT EXISTS users(id TEXT PRIMARY KEY,token TEXT UNIQUE,admin INTEGER);
CREATE TABLE IF NOT EXISTS events(user TEXT,event TEXT,hash TEXT,result TEXT,received REAL,PRIMARY KEY(user,event));
CREATE TABLE IF NOT EXISTS positions(user TEXT PRIMARY KEY,seq INTEGER,timestamp REAL,body TEXT,received REAL);
CREATE TABLE IF NOT EXISTS samples(user TEXT,event TEXT,edge TEXT,speed REAL,received REAL,PRIMARY KEY(user,event));
CREATE INDEX IF NOT EXISTS samples_time ON samples(received);
CREATE INDEX IF NOT EXISTS events_received ON events(received);
CREATE INDEX IF NOT EXISTS events_user_received ON events(user,received);
CREATE INDEX IF NOT EXISTS positions_received ON positions(received);

"""
