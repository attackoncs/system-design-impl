"""Local-only FFmpeg operations with bounded diagnostics and owned subprocesses."""

import contextlib
import math
import os
import pathlib
import shutil
import subprocess
import tempfile
import threading
import time

from video_streaming.pipeline import PROFILES

try:
    import imageio_ffmpeg
except ImportError:
    imageio_ffmpeg = None


class InvalidMedia(ValueError):
    """The source cannot be decoded and should not be retried."""


class MediaCancelled(RuntimeError):
    """The owning task lost its lease and stopped its media subprocess."""


def executable():
    """Return an available FFmpeg executable or raise RuntimeError."""
    path = shutil.which("ffmpeg")
    if path:
        return path
    if imageio_ffmpeg is None:
        raise RuntimeError("Install the media extra or provide FFmpeg on PATH")
    return imageio_ffmpeg.get_ffmpeg_exe()


def _duration(value):
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ValueError("invalid media timeout")
    try:
        finite = math.isfinite(value)
    except OverflowError:
        finite = False
    if not finite or value <= 0:
        raise ValueError("invalid media timeout")
    return value


class FFmpegMedia:
    """Run fixed templates, with one bounded command per concurrent task."""

    timeout = 60

    def __init__(self, binary=None, timeout=60):
        self.timeout = _duration(timeout)
        self.binary = executable() if binary is None else os.fspath(binary)
        if not self.binary:
            raise ValueError("invalid FFmpeg executable")
        self._local = threading.local()

    @contextlib.contextmanager
    def operation(self, cancelled):
        """Bind a task's lease-loss signal to this thread's subprocesses."""
        previous = getattr(self._local, "cancelled", None)
        self._local.cancelled = cancelled
        try:
            yield
        finally:
            self._local.cancelled = previous

    @staticmethod
    def input(source):
        """Allow local source containers or a worker-created concat manifest."""
        source = pathlib.Path(source)
        arguments = ["-protocol_whitelist", "file,pipe,crypto"]
        if source.suffix == ".ffconcat":
            arguments += ["-f", "concat", "-safe", "0"]
        else:
            arguments += [
                "-format_whitelist",
                "mov,matroska,webm,avi,mpegts,mpeg,ogg,flv,asf",
            ]
        return arguments + ["-i", str(source)]

    def _wait(self, process, cancelled, deadline, command):
        while process.poll() is None:
            if cancelled is not None and cancelled.is_set():
                raise MediaCancelled("media task lost its lease")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(command, self.timeout)
            try:
                process.wait(timeout=min(0.1, remaining))
            except subprocess.TimeoutExpired:
                pass

    def run(self, arguments, fatal=False):
        """Run a command; timeout/cancellation always kills and joins its child.

        FFmpeg diagnostics go to a temporary file, not an unbounded Python
        byte buffer. Only a bounded tail is retained in an exception.
        """
        command = [
            self.binary,
            "-hide_banner",
            "-nostdin",
            "-loglevel",
            "error",
            "-nostats",
            "-y",
            *arguments,
        ]
        options = (
            {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
        )
        cancelled = getattr(self._local, "cancelled", None)
        if cancelled is not None and cancelled.is_set():
            raise MediaCancelled("media task lost its lease")
        with tempfile.TemporaryFile() as diagnostics:
            process = subprocess.Popen(
                command, stdout=subprocess.DEVNULL, stderr=diagnostics, **options
            )
            try:
                self._wait(process, cancelled, time.monotonic() + self.timeout, command)
                if process.returncode:
                    diagnostics.seek(0, os.SEEK_END)
                    diagnostics.seek(max(0, diagnostics.tell() - 1024))
                    detail = (
                        diagnostics.read().decode("utf-8", errors="replace").strip()
                    )
                    error = InvalidMedia if fatal else RuntimeError
                    raise error(
                        (
                            "source video cannot be decoded"
                            if fatal
                            else "FFmpeg processing failed"
                        )
                        + (": " + detail if detail else "")
                    )
            finally:
                if process.poll() is None:
                    process.kill()
                process.wait()

    def inspect(self, source, folder):
        """Decode the full video/audio, rejecting malformed later frames too."""
        del folder
        self.run(
            [
                "-xerror",
                *self.input(source),
                "-map",
                "0:v:0",
                "-map",
                "0:a:0?",
                "-f",
                "null",
                "-",
            ],
            fatal=True,
        )
        return {"adapter": "ffmpeg", "validated": "all-frames"}

    def preprocess(self, source, folder):
        """Persist stream-copy chunks cut at existing source keyframes.

        Two seconds is a target. Long source GOPs yield longer chunks; this
        operation does not re-encode merely to invent new keyframe boundaries.
        """
        self.run(
            self.input(source)
            + [
                "-map",
                "0:v:0",
                "-map",
                "0:a:0?",
                "-c",
                "copy",
                "-f",
                "segment",
                "-segment_format",
                "matroska",
                "-segment_time",
                "2",
                "-reset_timestamps",
                "1",
                str(folder / "gop_%05d.mkv"),
            ]
        )
        parts = sorted(folder.glob("gop_*.mkv"))
        if not parts:
            raise InvalidMedia("source produced no GOP chunks")
        return parts

    def thumbnail(self, source, folder):
        """Create thumbnail.png in folder and return its path."""
        path = folder / "thumbnail.png"
        self.run(
            self.input(source)
            + ["-map", "0:v:0", "-frames:v", "1", "-vf", "scale=320:-2", str(path)]
        )
        return path

    def encode(self, source, folder, profile, key):
        """Encode a supported rendition into encrypted, keyframe-aligned HLS."""
        if type(profile) is not int or profile not in PROFILES:
            raise ValueError("invalid rendition profile")
        if not isinstance(key, bytes) or len(key) != 16:
            raise ValueError("AES-128 key must contain sixteen bytes")
        width, height, bitrate = PROFILES[profile]
        key_path = folder / "encryption.key"
        key_path.write_bytes(key)
        key_info = folder / "key-info.txt"
        key_info.write_text(
            "../key.bin\n" + key_path.resolve().as_posix() + "\n", encoding="utf-8"
        )
        path = folder / "index.m3u8"
        self.run(
            self.input(source)
            + [
                "-map",
                "0:v:0",
                "-map",
                "0:a:0?",
                "-vf",
                f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
                f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2",
                "-c:v",
                "libx264",
                "-preset",
                "ultrafast",
                "-threads",
                "2",
                "-pix_fmt",
                "yuv420p",
                "-b:v",
                str(bitrate),
                "-r",
                "30",
                "-g",
                "60",
                "-sc_threshold",
                "0",
                "-force_key_frames",
                "expr:gte(t,n_forced*2)",
                "-c:a",
                "aac",
                "-b:a",
                "96000",
                "-f",
                "hls",
                "-hls_time",
                "2",
                "-hls_playlist_type",
                "vod",
                "-hls_flags",
                "independent_segments",
                "-hls_key_info_file",
                str(key_info),
                "-hls_segment_filename",
                str(folder / "segment_%05d.ts"),
                str(path),
            ]
        )
        return [path] + sorted(folder.glob("segment_*.ts"))
