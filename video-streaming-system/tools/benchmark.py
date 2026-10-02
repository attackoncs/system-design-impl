"""Actual generated video, durable DAG, encrypted HLS and loopback HTTP measurements."""
import argparse
import json
import os
from pathlib import Path
import platform
import tempfile
from threading import Thread
import time
from urllib.parse import urlencode
from urllib.request import urlopen

from video_streaming import FFmpegMedia, VideoService, Worker
from video_streaming.http import server


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--duration", type=float, default=3)
    parser.add_argument("--http-requests", type=int, default=200)
    parser.add_argument("--output")
    args = parser.parse_args()
    if not 1 <= args.duration <= 30 or not 1 <= args.http_requests <= 100000:
        parser.error("duration 1..30 seconds and positive bounded HTTP count required")
    runtime = Path(__file__).resolve().parents[1] / ".runtime"
    runtime.mkdir(exist_ok=True)
    media = FFmpegMedia()
    with tempfile.TemporaryDirectory(dir=runtime) as directory:
        root = Path(directory).resolve()
        root.relative_to(runtime.resolve())
        source = root / "fixture.mp4"
        media.run(["-f", "lavfi", "-i", "testsrc2=size=320x180:rate=30", "-f", "lavfi", "-i",
                   "sine=frequency=440:sample_rate=44100", "-t", str(args.duration), "-c:v", "libx264",
                   "-threads", "2", "-pix_fmt", "yuv420p", "-c:a", "aac", str(source)])
        service = VideoService(root / "metadata.sqlite3", root / "objects", profiles=(144, 240))
        http = thread = None
        try:
            token = service.issue("benchmark-owner")
            payload = source.read_bytes()
            upload = service.create(token, "Generated test video", len(payload), part_size=65536)
            before = time.perf_counter()
            for i, grant in enumerate(upload["parts"]):
                service.put_part(grant, payload[i * 65536:(i + 1) * 65536])
            service.finish(token, upload["video_id"])
            upload_ms = (time.perf_counter() - before) * 1000
            worker = Worker(service, media)
            timings = {}
            before = time.perf_counter()
            while True:
                job = service.claim("benchmark", 120)
                if job is None:
                    break
                started = time.perf_counter()
                output = worker.execute(job)
                if not service.complete(job, output):
                    raise RuntimeError("lease lost during fixture processing")
                timings[job["name"]] = round((time.perf_counter() - started) * 1000, 3)
            service.handle_completions()
            pipeline_ms = (time.perf_counter() - before) * 1000
            grant = service.playback(token, upload["video_id"])
            http = server(service)
            thread = Thread(target=http.serve_forever, daemon=True)
            thread.start()
            url = f"http://127.0.0.1:{http.server_address[1]}"
            for quality in (144, 240):
                playlist = url + "/media?" + urlencode({"grant": grant, "resource": f"{quality}/index.m3u8"})
                media.run(["-protocol_whitelist", "file,http,tcp,crypto", "-allowed_extensions", "ALL",
                           "-i", playlist, "-f", "null", "-"])
            segment = url + "/media?" + urlencode({"grant": grant, "resource": "144/segment_00000.ts"})
            samples = []
            for _ in range(args.http_requests):
                started = time.perf_counter()
                with urlopen(segment, timeout=5) as response:
                    assert response.read()
                samples.append((time.perf_counter() - started) * 1000)
            samples.sort()
            assets = service.outputs(upload["video_id"])["publish"]
            report = {"duration_seconds": args.duration, "source_bytes": len(payload),
                      "renditions": [144, 240], "upload_parts": len(upload["parts"]),
                      "upload_and_finalize_ms": round(upload_ms, 3), "pipeline_ms": round(pipeline_ms, 3),
                      "task_ms": timings, "output_bytes": sum(service.objects.file(key).stat().st_size for key in set(assets.values())),
                      "encrypted_http_decode_verified": True, "http_requests": args.http_requests,
                      "http_latency": {f"p{p}_ms": round(samples[int((len(samples) - 1) * p / 100)], 3) for p in (50, 95, 99)},
                      "environment": {"platform": platform.platform(), "python": platform.python_version(),
                                      "logical_cpus": os.cpu_count()},
                      "scope": "one local worker; sequential loopback HTTP; no cloud CDN or production scale claim"}
            print(json.dumps(report, indent=2))
            if args.output:
                target = Path(args.output)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        finally:
            if http:
                http.shutdown()
                http.server_close()
                thread.join(timeout=5)
            service.close()


if __name__ == "__main__":
    main()
