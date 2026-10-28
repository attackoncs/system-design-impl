"""Idempotent shard schema and scheduler indexes."""


def initialize(store):
    store.initialize("""
        CREATE TABLE IF NOT EXISTS metadata(id INTEGER PRIMARY KEY CHECK(id=1),shard INTEGER,shards INTEGER,revision INTEGER);
        CREATE TABLE IF NOT EXISTS hotels(id TEXT PRIMARY KEY,name TEXT,address TEXT,active INTEGER NOT NULL CHECK(active IN(0,1)));
        CREATE TABLE IF NOT EXISTS rooms(hotel TEXT,id TEXT,name TEXT,description TEXT,active INTEGER NOT NULL CHECK(active IN(0,1)),
            PRIMARY KEY(hotel,id),FOREIGN KEY(hotel) REFERENCES hotels(id));
        CREATE TABLE IF NOT EXISTS inventory(hotel TEXT,room TEXT,day TEXT,capacity INTEGER NOT NULL CHECK(capacity>=0),
            overbook INTEGER NOT NULL CHECK(overbook BETWEEN 0 AND 10),price INTEGER NOT NULL CHECK(price>=0),
            reserved INTEGER NOT NULL DEFAULT 0 CHECK(reserved>=0 AND reserved<=capacity+(capacity*overbook)/100),
            version INTEGER NOT NULL DEFAULT 0,PRIMARY KEY(hotel,room,day),FOREIGN KEY(hotel,room) REFERENCES rooms(hotel,id));
        CREATE TABLE IF NOT EXISTS bookings(id TEXT PRIMARY KEY,hotel TEXT,room TEXT,user TEXT,request_key TEXT,hash TEXT,
            start TEXT,end TEXT,quantity INTEGER,amount INTEGER,currency TEXT,status TEXT,payment TEXT,
            held INTEGER NOT NULL CHECK(held IN(0,1)),expires REAL,created REAL,method TEXT,
            UNIQUE(hotel,user,request_key),FOREIGN KEY(hotel,room) REFERENCES rooms(hotel,id));
        CREATE INDEX IF NOT EXISTS user_history ON bookings(user,hotel,created);
        CREATE TABLE IF NOT EXISTS nights(booking TEXT,day TEXT,price INTEGER,PRIMARY KEY(booking,day),
            FOREIGN KEY(booking) REFERENCES bookings(id));
        CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY,booking TEXT,action TEXT,status TEXT,
            attempts INTEGER,due REAL,lease REAL,token TEXT,error TEXT,FOREIGN KEY(booking) REFERENCES bookings(id));
        CREATE TABLE IF NOT EXISTS events(seq INTEGER PRIMARY KEY AUTOINCREMENT,booking TEXT,kind TEXT,payload TEXT,created REAL);
        """)
    with store.transaction(True) as db:
        db.execute(
            "CREATE INDEX IF NOT EXISTS pending_expiry ON bookings(status,expires,id)"
        )
        db.execute("CREATE INDEX IF NOT EXISTS ready_jobs ON jobs(status,due,id)")
        db.execute("CREATE INDEX IF NOT EXISTS leased_jobs ON jobs(status,lease,id)")
        db.execute(
            "CREATE INDEX IF NOT EXISTS history_cursor ON bookings(hotel,user,id)"
        )
