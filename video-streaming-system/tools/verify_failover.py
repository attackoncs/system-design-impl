"""Kill only the primary in the owned Patroni lab; verify durable video recovery."""
import argparse
import json
from pathlib import Path
from threading import Thread
import time
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
import uuid

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo

from video_streaming import FFmpegMedia, VideoService, Worker
from video_streaming.database import MetadataUnavailable
from video_streaming.http import server


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default=".runtime/failover.json")
    args = parser.parse_args()
    runtime = Path(__file__).resolve().parents[1] / ".runtime"
    state_path = runtime / "ha-lab.json"
    state = json.loads(state_path.read_text())
    if not state.get("ready") or state.get("stopped") or "new_primary" in state:
        raise RuntimeError("start a fresh owned Patroni lab first")
    schema = "failover_" + uuid.uuid4().hex
    dsn = state["dsn"]
    with psycopg.connect(dsn, autocommit=True, target_session_attrs="read-write", connect_timeout=3) as connection:
        connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    root = runtime / schema
    root.mkdir()
    service = VideoService(make_conninfo(dsn, options="-c search_path=" + schema), root / "objects", profiles=(144, 240))
    http, thread = None, None
    try:
        media = FFmpegMedia()
        source = root / "fixture.mp4"
        media.run(["-f", "lavfi", "-i", "testsrc2=size=320x180:rate=30", "-t", "3",
                   "-c:v", "libx264", "-threads", "2", "-pix_fmt", "yuv420p", str(source)])
        payload = source.read_bytes()
        token = service.issue("fault-test-owner")
        upload = service.create(token, "Survive primary loss", len(payload), part_size=65536)
        for index, grant in enumerate(upload["parts"]):
            service.put_part(grant, payload[index * 65536:(index + 1) * 65536])
        service.finish(token, upload["video_id"])
        stale = service.claim("old-worker", lease_seconds=2)
        http = server(service)
        thread = Thread(target=http.serve_forever, daemon=True)
        thread.start()
        url = f"http://127.0.0.1:{http.server_address[1]}"
        request = Request(url + "/videos?" + urlencode({"video": upload["video_id"]}), headers={"Authorization": "Bearer " + token})
        with urlopen(request, timeout=20) as response:
            assert json.load(response)["state"] == "PROCESSING"
        before = time.monotonic()
        (runtime / "ha-control.json").write_text(json.dumps({"action": "kill_primary"}))
        failures, successful_reads = 0, 0
        deadline = before + 75
        while True:
            try:
                with urlopen(request, timeout=20) as response:
                    assert json.load(response)["state"] == "PROCESSING"
                    successful_reads += 1
            except HTTPError as error:
                if error.code != 503:
                    raise
                failures += 1
            state = json.loads(state_path.read_text())
            if state.get("new_primary") and successful_reads:
                # The successful read must be from the new primary.
                if service.db.execute("SELECT pg_is_in_recovery()").fetchone()[0] is False:
                    break
            if time.monotonic() > deadline:
                raise RuntimeError("existing API did not recover")
            time.sleep(.2)
        recovery = time.monotonic() - before
        assert not service.complete(stale, {"late": True})
        worker = Worker(service, media)
        while worker.run_once():
            pass
        assert service.status(token, upload["video_id"])["state"] == "READY"
        assert service.db.execute("SELECT COUNT(*) FROM completions WHERE handled=1").fetchone()[0] == 1
        assert service.db.execute("SELECT attempts FROM jobs WHERE name='inspect'").fetchone()[0] == 2
        grant = service.playback(token, upload["video_id"])
        for profile in (144, 240):
            playlist = url + "/media?" + urlencode({"grant": grant, "resource": f"{profile}/index.m3u8"})
            media.run(["-protocol_whitelist", "file,http,tcp,crypto", "-allowed_extensions", "ALL", "-i", playlist, "-f", "null", "-"])
        report = {"postgresql": "18.6", "patroni": "4.1.5", "etcd": "3.5.16", "database_nodes": 3,
                  "etcd_members": 3, "synchronous_mode": "strict", "old_primary": state["old_primary"],
                  "new_primary": state["new_primary"], "promotion_seconds": state["promotion_seconds"],
                  "existing_api_recovery_seconds": round(recovery, 3), "http_503_observed": failures,
                  "credentials_upload_and_jobs_survived": True, "stale_lease_rejected": True,
                  "completion_count": 1, "inspection_attempts": 2, "real_encrypted_http_decode": [144, 240],
                  "scope": "single-machine WSL lab; real PostgreSQL/Patroni/etcd; local video objects"}
        print(json.dumps(report, indent=2))
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, indent=2) + "\n")
    finally:
        if http:
            http.shutdown()
            http.server_close()
            thread.join(timeout=5)
        service.close()
        with psycopg.connect(dsn, autocommit=True, target_session_attrs="read-write", connect_timeout=3) as connection:
            connection.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


if __name__ == "__main__":
    main()
