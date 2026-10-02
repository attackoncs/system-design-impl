"""S3 HTTP and optional real PostgreSQL tests, with isolated local caches/schemas."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import socket
import select
import subprocess
import sys
import tempfile
import time
import uuid
from urllib.request import urlopen
from urllib.parse import urlencode
from threading import Event, Thread

import pytest

from video_streaming import FFmpegMedia, S3Objects, VideoService, Worker
from video_streaming.database import MetadataUnavailable
from video_streaming.http import server
from test_video import FakeMedia, uploaded, make_fixture


@pytest.fixture(scope="module")
def s3_endpoint():
    configured = os.environ.get("VIDEO_TEST_S3_ENDPOINT")
    if configured:
        yield configured
        return
    pytest.importorskip("moto")
    pytest.importorskip("boto3")
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
    child = subprocess.Popen([sys.executable, "-m", "moto.server", "-H", "127.0.0.1", "-p", str(port)],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **options)
    endpoint = f"http://127.0.0.1:{port}"
    try:
        deadline = time.monotonic() + 15
        while True:
            try:
                with urlopen(endpoint, timeout=.5):
                    break
            except OSError:
                if child.poll() is not None or time.monotonic() > deadline:
                    raise AssertionError("S3 protocol emulator failed to start")
                time.sleep(.1)
        yield endpoint
    finally:
        child.terminate()
        child.wait(timeout=10)


@pytest.fixture
def stores(tmp_path, s3_endpoint):
    import boto3
    from botocore.config import Config
    client = boto3.client("s3", endpoint_url=s3_endpoint, region_name="us-east-1",
                          aws_access_key_id=os.environ.get("VIDEO_TEST_S3_ACCESS_KEY", "test-only"),
                          aws_secret_access_key=os.environ.get("VIDEO_TEST_S3_SECRET_KEY", "test-only"),
                          config=Config(connect_timeout=3, read_timeout=10, retries={"max_attempts": 1},
                                        s3={"addressing_style": "path"}))
    bucket = "video-" + uuid.uuid4().hex
    client.create_bucket(Bucket=bucket)
    yield [S3Objects(tmp_path / name / "objects", bucket, client=client) for name in ("api", "worker", "reader")]
    for page in client.get_paginator("list_objects_v2").paginate(Bucket=bucket):
        for item in page.get("Contents", []):
            client.delete_object(Bucket=bucket, Key=item["Key"])
    client.delete_bucket(Bucket=bucket)


def test_independent_caches_share_remote_upload_and_publication(stores, tmp_path):
    api = VideoService(tmp_path / "db.sqlite3", stores[0], profiles=(144,))
    worker = VideoService(tmp_path / "db.sqlite3", stores[1], profiles=(144,))
    reader = VideoService(tmp_path / "db.sqlite3", stores[2])
    try:
        token, upload = uploaded(api)
        assert not list(stores[1].root.iterdir())
        runner = Worker(worker, FakeMedia())
        while runner.run_once():
            pass
        grant = reader.playback(token, upload["video_id"])
        assert b"144/index.m3u8" in reader.asset(grant, "master.m3u8").read_bytes()
        assert stores[0].root != stores[1].root != stores[2].root
        key = reader.asset(grant, "key.bin").name
        assert reader.asset(grant, "key.bin").is_file()
        stores[0].client.delete_object(Bucket=stores[0].bucket, Key=stores[0].prefix + key)
        # Cached copies must not conceal authoritative-object loss.
        assert not reader.asset(grant, "key.bin").is_file()
        with pytest.raises(FileNotFoundError):
            reader.asset(grant, "key.bin").read_bytes()
    finally:
        for service in (api, worker, reader):
            service.close()


def test_remote_corruption_and_failed_write_never_confirmed(stores, monkeypatch):
    writer, reader, _ = stores
    key = writer.put(b"original")
    writer.client.put_object(Bucket=writer.bucket, Key=writer.prefix + key, Body=b"corrupt")
    with pytest.raises(OSError, match="checksum"):
        reader.file(key).read_bytes()
    assert not (reader.root / key).exists()
    monkeypatch.setattr(writer.client, "upload_file", lambda *a, **k: (_ for _ in ()).throw(ConnectionError()))
    with pytest.raises(OSError, match="upload"):
        writer.put(b"unconfirmed")


def test_remote_gc_retains_references_and_other_prefixes(stores):
    store = stores[0]
    live, orphan = store.put(b"live"), store.put(b"orphan")
    store.client.put_object(Bucket=store.bucket, Key="another/" + orphan, Body=b"other")
    assert store.sweep({live}, time.time() + 10) == 1
    assert store.file(live).is_file() and not store.file(orphan).is_file()
    assert store.client.head_object(Bucket=store.bucket, Key="another/" + orphan)


@pytest.fixture
def postgres_dsn():
    dsn = os.environ.get("VIDEO_TEST_DATABASE_DSN")
    if not dsn:
        pytest.skip("set VIDEO_TEST_DATABASE_DSN to a disposable real PostgreSQL cluster")
    psycopg = pytest.importorskip("psycopg")
    from psycopg import sql
    from psycopg.conninfo import make_conninfo
    schema = "test_" + uuid.uuid4().hex
    with psycopg.connect(dsn, autocommit=True, target_session_attrs="read-write", connect_timeout=3) as connection:
        connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    try:
        yield make_conninfo(dsn, options="-c search_path=" + schema)
    finally:
        with psycopg.connect(dsn, autocommit=True, target_session_attrs="read-write", connect_timeout=3) as connection:
            connection.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def test_postgres_cross_connection_claims_fencing_and_reconnect(postgres_dsn, tmp_path):
    a = VideoService(postgres_dsn, tmp_path / "a", profiles=(144,))
    b = VideoService(postgres_dsn, tmp_path / "a", profiles=(144,))
    try:
        token, upload = uploaded(a)
        with ThreadPoolExecutor(2) as pool:
            claims = list(pool.map(lambda service: service.claim("worker"), (a, b)))
        assert sum(job is not None for job in claims) == 1
        job = next(job for job in claims if job)
        assert a.complete(job, {"adapter": "test"})
        assert not b.complete(job, {"adapter": "late"})
        # Terminate only this test-owned session; failed operation is surfaced,
        # then the existing service reconnects and reads committed state.
        pid = a.db.connection.info.backend_pid
        b.db.execute("SELECT pg_terminate_backend(?)", (pid,))
        with pytest.raises(MetadataUnavailable):
            a.status(token, upload["video_id"])
        assert a.status(token, upload["video_id"])["state"] == "PROCESSING"
    finally:
        a.close()
        b.close()


def test_postgres_and_s3_real_media_independent_nodes(postgres_dsn, stores, tmp_path):
    api = VideoService(postgres_dsn, stores[0], profiles=(144, 240))
    reader = VideoService(postgres_dsn, stores[2])
    children, logs, http, thread = [], [], None, None
    runtime = Path(__file__).resolve().parents[1] / ".runtime"
    runtime.mkdir(exist_ok=True)
    # UUID lease paths plus deeply-nested pytest paths can exceed Windows MAX_PATH.
    work_directory = tempfile.TemporaryDirectory(prefix="vw-", dir=runtime)
    work_root = Path(work_directory.name).resolve()
    work_root.relative_to(runtime.resolve())
    try:
        media = FFmpegMedia()
        source = tmp_path / "fixture.mp4"
        make_fixture(media, source)
        token, upload = uploaded(api, source.read_bytes())
        env = os.environ.copy()
        env.update(VIDEO_DATABASE_DSN=postgres_dsn, VIDEO_S3_BUCKET=stores[0].bucket,
            VIDEO_S3_ENDPOINT=stores[0].client.meta.endpoint_url, VIDEO_S3_PREFIX=stores[0].prefix,
            AWS_ACCESS_KEY_ID=os.environ.get("VIDEO_TEST_S3_ACCESS_KEY", "test-only"),
            AWS_SECRET_ACCESS_KEY=os.environ.get("VIDEO_TEST_S3_SECRET_KEY", "test-only"),
            PYTHONPATH=str(Path(__file__).resolve().parents[1] / "src"))
        options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
        for index in range(2):
            log = (tmp_path / f"worker-{index}.log").open("wb")
            logs.append(log)
            children.append(subprocess.Popen([sys.executable, "-m", "video_streaming.cli", "--root",
                str(work_root / str(index)), "worker", "--name", f"remote-{index}"],
                env=env, stdout=log, stderr=subprocess.STDOUT, **options))
        deadline = time.monotonic() + 45
        while reader.status(token, upload["video_id"])["state"] != "READY":
            if (any(child.poll() is not None for child in children) or time.monotonic() > deadline
                    or reader.status(token, upload["video_id"])["state"] == "FAILED"):
                states = [tuple(row) for row in reader.db.execute("SELECT name,state,attempts,error FROM jobs")]
                raise AssertionError(f"independent remote workers did not publish; jobs={states}; logs={tmp_path}")
            time.sleep(.1)
        grant = reader.playback(token, upload["video_id"])
        assert b"240/index.m3u8" in reader.asset(grant, "master.m3u8").read_bytes()
        assert reader.asset(grant, "144/segment_00000.ts").stat().st_size > 0
        http = server(reader)
        thread = Thread(target=http.serve_forever, daemon=True)
        thread.start()
        for profile in (144, 240):
            playlist = f"http://127.0.0.1:{http.server_address[1]}/media?" + urlencode({"grant": grant, "resource": f"{profile}/index.m3u8"})
            media.run(["-protocol_whitelist", "file,http,tcp,crypto", "-allowed_extensions", "ALL",
                       "-i", playlist, "-f", "null", "-"])
    finally:
        if http:
            http.shutdown()
            http.server_close()
            thread.join(timeout=5)
        for child in children:
            if child.poll() is None:
                child.terminate()
                child.wait(timeout=5)
        for log in logs:
            log.close()
        work_directory.cleanup()
        api.close()
        reader.close()


def test_postgres_unknown_commit_is_not_replayed(postgres_dsn, tmp_path, monkeypatch):
    import psycopg
    service = VideoService(postgres_dsn, tmp_path / "objects")
    connection_type = type(service.db.connection)
    original = connection_type.execute
    commits = []
    def uncertain(connection, query, *args, **kwargs):
        result = original(connection, query, *args, **kwargs)
        if query == "COMMIT":
            commits.append(query)
            raise psycopg.OperationalError("simulated lost commit acknowledgement")
        return result
    try:
        monkeypatch.setattr(connection_type, "execute", uncertain)
        with pytest.raises(MetadataUnavailable, match="outcome unknown"):
            with service.db:
                service.db.execute("INSERT INTO settings VALUES('uncertain','committed')")
        assert commits == ["COMMIT"]
        # Reconciliation reads the already-committed value; no second INSERT.
        assert service.db.execute("SELECT value FROM settings WHERE key='uncertain'").fetchone()[0] == "committed"
    finally:
        service.close()


def test_postgres_rolls_back_part_on_object_failure(postgres_dsn, stores, tmp_path, monkeypatch):
    service = VideoService(postgres_dsn, stores[0])
    try:
        token = service.issue("alice")
        upload = service.create(token, "Atomic part", 4, part_size=4)
        original = stores[0].put
        monkeypatch.setattr(stores[0], "put", lambda _: (_ for _ in ()).throw(OSError("storage offline")))
        with pytest.raises(OSError):
            service.put_part(upload["parts"][0], b"data")
        assert service.status(token, upload["video_id"])["uploaded_parts"] == []
        monkeypatch.setattr(stores[0], "put", original)
        service.put_part(upload["parts"][0], b"data")
        assert service.status(token, upload["video_id"])["uploaded_parts"] == [0]
    finally:
        service.close()


def test_offline_migration_preserves_grants_jobs_and_rejects_nonempty_target(postgres_dsn, stores, tmp_path):
    from migrate_metadata import migrate
    source = VideoService(tmp_path / "source.sqlite3", tmp_path / "local", profiles=(144,))
    target = VideoService(postgres_dsn, stores[1], profiles=(144,))
    try:
        token, upload = uploaded(source)
        counts = migrate(tmp_path / "source.sqlite3", target, stores[1], tmp_path / "local")
        assert counts["jobs"] == 4
        assert target.authenticate(token) == "alice"
        assert target.verify(upload["parts"][0], "upload")["video"] == upload["video_id"]
        worker = Worker(target, FakeMedia())
        while worker.run_once():
            pass
        assert target.status(token, upload["video_id"])["state"] == "READY"
        with pytest.raises(ValueError, match="empty"):
            migrate(tmp_path / "source.sqlite3", target)
        assert target.status(token, upload["video_id"])["state"] == "READY"
    finally:
        source.close()
        target.close()


def test_postgres_blackholed_socket_has_bounded_recovery(postgres_dsn, tmp_path):
    """A TCP connection stays open but drops requests: server timeouts cannot help."""
    from psycopg.conninfo import make_conninfo
    original = VideoService(postgres_dsn, tmp_path / "objects")
    destination = (original.db.connection.info.host, original.db.connection.info.port)
    original.close()
    pause, stop = Event(), Event()
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    listener.settimeout(.1)
    connections, threads = [], []
    def relay(client):
        remote = socket.create_connection(destination, timeout=3)
        connections.append(remote)
        try:
            while not stop.is_set():
                for source in select.select([client, remote], [], [], .1)[0]:
                    value = source.recv(65536)
                    if not value:
                        return
                    if source is client and pause.is_set():
                        continue
                    (remote if source is client else client).sendall(value)
        except OSError:
            pass
        finally:
            client.close()
            remote.close()
    def accept():
        while not stop.is_set():
            try:
                client, _ = listener.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            connections.append(client)
            thread = Thread(target=relay, args=(client,), daemon=True)
            threads.append(thread)
            thread.start()
    acceptor = Thread(target=accept, daemon=True)
    acceptor.start()
    service = None
    try:
        service = VideoService(make_conninfo(postgres_dsn, host="127.0.0.1", port=str(listener.getsockname()[1]),
                                            sslmode="disable"), tmp_path / "objects")
        pause.set()
        before = time.monotonic()
        with pytest.raises(MetadataUnavailable):
            service.db.execute("SELECT 1")
        assert time.monotonic() - before < 8
        pause.clear()
        assert service.db.execute("SELECT 21").fetchone()[0] == 21
    finally:
        if service:
            service.close()
        stop.set()
        listener.close()
        for connection in connections:
            connection.close()
        acceptor.join(timeout=3)
        for thread in threads:
            thread.join(timeout=3)
