"""Fixed-template actual FFmpeg media adapter, including encrypted HLS."""
import os
import shutil
import subprocess

from .service import PROFILES


class InvalidMedia(ValueError):
    pass


def executable():
    path = shutil.which("ffmpeg")
    if path:
        return path
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except ImportError:
        raise RuntimeError("Install the media extra or provide FFmpeg on PATH") from None


class FFmpegMedia:
    timeout = 60

    def __init__(self, binary=None, timeout=60):
        if timeout <= 0:
            raise ValueError("invalid media timeout")
        self.binary = binary or executable()
        self.timeout = timeout

    @staticmethod
    def input(source):
        # Source containers must not fetch URLs or interpret arbitrary playlist paths.
        return ["-protocol_whitelist", "file,pipe,crypto", "-format_whitelist",
                "mov,matroska,webm,avi,mpegts,mpeg,ogg,flv,asf", "-i", str(source)]

    def run(self, arguments, fatal=False):
        options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
        result = subprocess.run([self.binary, "-hide_banner", "-nostdin", "-loglevel", "error", "-y"] + arguments,
                                capture_output=True, timeout=self.timeout, **options)
        if result.returncode:
            if fatal:
                raise InvalidMedia("source video cannot be decoded")
            raise RuntimeError("FFmpeg processing failed")

    def inspect(self, source, folder):
        self.run(self.input(source) + ["-map", "0:v:0", "-frames:v", "1", "-f", "null", "-"], fatal=True)
        return {"adapter": "ffmpeg"}

    def thumbnail(self, source, folder):
        path = folder / "thumbnail.png"
        self.run(self.input(source) + ["-map", "0:v:0", "-frames:v", "1", "-vf", "scale=320:-2", str(path)])
        return path

    def encode(self, source, folder, profile, key):
        width, height, bitrate = PROFILES[profile]
        key_path = folder / "encryption.key"
        key_path.write_bytes(key)
        key_info = folder / "key-info.txt"
        key_info.write_text("../key.bin\n" + key_path.resolve().as_posix() + "\n", encoding="utf-8")
        path = folder / "index.m3u8"
        self.run(self.input(source) + ["-map", "0:v:0", "-map", "0:a:0?",
                  "-vf", f"scale={width}:{height}:force_original_aspect_ratio=decrease,pad={width}:{height}:(ow-iw)/2:(oh-ih)/2",
                  "-c:v", "libx264", "-preset", "ultrafast", "-threads", "2", "-pix_fmt", "yuv420p",
                  "-b:v", str(bitrate), "-r", "30", "-g", "60", "-sc_threshold", "0",
                  "-force_key_frames", "expr:gte(t,n_forced*2)", "-c:a", "aac", "-b:a", "96000",
                  "-f", "hls", "-hls_time", "2", "-hls_playlist_type", "vod",
                  "-hls_flags", "independent_segments", "-hls_key_info_file", str(key_info),
                  "-hls_segment_filename", str(folder / "segment_%05d.ts"), str(path)])
        return [path] + sorted(folder.glob("segment_*.ts"))
