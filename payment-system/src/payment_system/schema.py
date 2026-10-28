"""Payment tables and immutable balanced ledger constraints."""

SCHEMA = """
        CREATE TABLE IF NOT EXISTS wallets(merchant TEXT PRIMARY KEY,available INTEGER NOT NULL CHECK(available>=0),held INTEGER NOT NULL CHECK(held>=0));
        CREATE TABLE IF NOT EXISTS operations(id TEXT PRIMARY KEY,merchant TEXT,kind TEXT,order_id TEXT,parent TEXT,
            amount INTEGER NOT NULL CHECK(amount>0),currency TEXT NOT NULL CHECK(currency='USD'),destination TEXT,
            request_key TEXT,hash TEXT,status TEXT,provider_id TEXT,revision INTEGER,checkout TEXT,created REAL,
            UNIQUE(merchant,request_key),UNIQUE(merchant,kind,order_id),FOREIGN KEY(merchant) REFERENCES wallets(merchant));
        CREATE TABLE IF NOT EXISTS ledger(id TEXT PRIMARY KEY,operation TEXT,kind TEXT,provider_id TEXT,posted INTEGER DEFAULT 0,created REAL,
            FOREIGN KEY(operation) REFERENCES operations(id));
        CREATE TABLE IF NOT EXISTS lines(tx TEXT,line INTEGER,account TEXT,debit INTEGER NOT NULL,credit INTEGER NOT NULL,
            CHECK((debit>0 AND credit=0) OR (credit>0 AND debit=0)),PRIMARY KEY(tx,line),FOREIGN KEY(tx) REFERENCES ledger(id));
        CREATE TRIGGER IF NOT EXISTS ledger_start_unposted BEFORE INSERT ON ledger WHEN NEW.posted!=0 BEGIN SELECT RAISE(ABORT,'ledger must start unposted'); END;
        CREATE TRIGGER IF NOT EXISTS finalize_balanced BEFORE UPDATE OF posted ON ledger WHEN NEW.posted=1 AND OLD.posted=0
        BEGIN SELECT CASE WHEN (SELECT count(*) FROM lines WHERE tx=OLD.id)<2 OR
            (SELECT sum(debit)-sum(credit) FROM lines WHERE tx=OLD.id)!=0 THEN RAISE(ABORT,'unbalanced ledger') END; END;
        CREATE TRIGGER IF NOT EXISTS ledger_no_update BEFORE UPDATE ON ledger WHEN OLD.posted=1 BEGIN SELECT RAISE(ABORT,'immutable ledger'); END;
        CREATE TRIGGER IF NOT EXISTS ledger_no_delete BEFORE DELETE ON ledger WHEN OLD.posted=1 BEGIN SELECT RAISE(ABORT,'immutable ledger'); END;
        CREATE TRIGGER IF NOT EXISTS lines_no_insert BEFORE INSERT ON lines WHEN (SELECT posted FROM ledger WHERE id=NEW.tx)=1 BEGIN SELECT RAISE(ABORT,'immutable lines'); END;
        CREATE TRIGGER IF NOT EXISTS lines_no_update BEFORE UPDATE ON lines WHEN (SELECT posted FROM ledger WHERE id=OLD.tx)=1 OR (SELECT posted FROM ledger WHERE id=NEW.tx)=1 BEGIN SELECT RAISE(ABORT,'immutable lines'); END;
        CREATE TRIGGER IF NOT EXISTS lines_no_delete BEFORE DELETE ON lines WHEN (SELECT posted FROM ledger WHERE id=OLD.tx)=1 BEGIN SELECT RAISE(ABORT,'immutable lines'); END;
        CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY,kind TEXT,payload TEXT,status TEXT,attempts INTEGER,failures INTEGER,due REAL,lease REAL,token TEXT,error TEXT);
        CREATE TABLE IF NOT EXISTS executions(seq INTEGER PRIMARY KEY AUTOINCREMENT,job TEXT,action TEXT,detail TEXT,created REAL);
        CREATE TABLE IF NOT EXISTS inbox(id TEXT PRIMARY KEY,hash TEXT,result TEXT);
        CREATE TABLE IF NOT EXISTS reviews(id TEXT PRIMARY KEY,operation TEXT,reason TEXT,detail TEXT,state TEXT,created REAL);
        CREATE TABLE IF NOT EXISTS reconciliations(id TEXT PRIMARY KEY,status TEXT,result TEXT,created REAL);
        
CREATE UNIQUE INDEX IF NOT EXISTS operations_provider_identity ON operations(provider_id) WHERE provider_id IS NOT NULL;
"""
