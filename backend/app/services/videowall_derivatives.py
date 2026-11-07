"""Private, local-file-only VW1 media preparation. No database or publication writes."""
from dataclasses import dataclass
from fractions import Fraction
import hashlib
import json
from pathlib import Path
import subprocess
import time
from typing import Callable

from app.services.videowall_integrity import MAX_GROUP_CANVAS_PIXELS, qualified_panel_geometry


PROFILE = "code302_h264_cfr_v1"
Heartbeat = Callable[[], bool]
_VIDEO_INPUT_FORMATS = "mov,matroska,webm,avi,asf,mpeg,mpegts,flv,ogg"
_IMAGE_INPUT_FORMATS = "png_pipe,jpeg_pipe,webp_pipe,image2"


class DerivativeError(ValueError):
    def __init__(self, code: str, message: str):
        self.code = code
        self.safe_message = message
        super().__init__(message)


@dataclass(frozen=True)
class MediaProbe:
    width: int
    height: int
    codec_name: str
    pixel_format: str
    has_audio: bool
    fps_num: int | None
    fps_den: int | None
    frame_count: int | None
    duration_ms: int
    start_time: Fraction
    keyframe_indices: tuple[int, ...]
    keyframe_pts_ms: tuple[int, ...]
    container_duration: Fraction


@dataclass(frozen=True)
class PreparedSlice:
    position_index: int
    path: Path
    width: int
    height: int
    mime_type: str
    size_bytes: int
    sha256: str
    duration_ms: int
    fps_num: int | None
    fps_den: int | None
    frame_count: int | None
    has_audio: bool
    keyframe_pts_ms: tuple[int, ...]


@dataclass(frozen=True)
class PreparedMedia:
    kind: str
    duration_ms: int
    source_sha256: str
    slices: tuple[PreparedSlice, ...]
    fps_num: int | None
    fps_den: int | None
    frame_count: int | None


def _tick(heartbeat):
    if heartbeat is not None and heartbeat() is False:
        raise DerivativeError("LEASE_LOST", "Publication processing lease was lost")


def _run(argv, *, heartbeat=None, timeout_seconds=3600) -> bytes:
    _tick(heartbeat)
    started = time.monotonic()
    try:
        process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, shell=False)
    except OSError:
        raise DerivativeError("MEDIA_TOOL_UNAVAILABLE", "FFmpeg and FFprobe must be installed") from None
    try:
        while True:
            try:
                output, _ = process.communicate(timeout=min(5, timeout_seconds))
                break
            except subprocess.TimeoutExpired:
                _tick(heartbeat)
                if time.monotonic() - started >= timeout_seconds:
                    raise DerivativeError("MEDIA_PROCESS_TIMEOUT", "Media processing exceeded its time limit")
        _tick(heartbeat)
        if process.returncode:
            raise DerivativeError("MEDIA_PROCESS_FAILED", "Media could not be decoded or encoded")
        if len(output) > 64 * 1024 * 1024:
            raise DerivativeError("MEDIA_PROBE_LIMIT", "Media inspection exceeded its output limit")
        return output
    finally:
        if process.poll() is None:
            process.kill()  # Only the child process created by this invocation.
            process.communicate()


def _fraction(value, default=None):
    try:
        return Fraction(str(value).replace(":", "/"))
    except (ValueError, ZeroDivisionError):
        return default


def _ms(value: Fraction) -> int:
    return (2 * value.numerator * 1000 + value.denominator) // (2 * value.denominator)


def _local_file(path) -> Path:
    if not isinstance(path, Path):
        raise DerivativeError("LOCAL_MEDIA_REQUIRED", "A validated local media path is required")
    try:
        resolved = path.resolve(strict=True)
        if not resolved.is_file():
            raise OSError()
        if any(character in str(resolved) for character in ("%", "*", "?", "[", "]")):
            raise DerivativeError("MASTER_PATH_UNSUPPORTED", "Master path must not contain media sequence patterns")
        return resolved
    except OSError:
        raise DerivativeError("MASTER_FILE_MISSING", "The master media file is unavailable") from None


def file_sha256(path: Path, *, heartbeat=None) -> str:
    digest = hashlib.sha256()
    last = time.monotonic()
    with _local_file(path).open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
            if time.monotonic() - last >= 5:
                _tick(heartbeat)
                last = time.monotonic()
    _tick(heartbeat)
    return digest.hexdigest()


def probe_media(path: Path, *, content_type: str, heartbeat=None, ffprobe_bin="ffprobe") -> MediaProbe:
    path = _local_file(path)
    if content_type not in {"image", "video"}:
        raise DerivativeError("UNSUPPORTED_MASTER_TYPE", "VW1 supports image and video masters only")
    entries = ("stream=index,codec_type,codec_name,width,height,pix_fmt,avg_frame_rate,r_frame_rate,"
               "time_base,sample_aspect_ratio,bits_per_raw_sample,color_transfer:stream_tags=rotate:"
               "stream_side_data=rotation:packet=stream_index,pts,duration,flags:format=duration")
    try:
        data = json.loads(_run([ffprobe_bin, "-v", "error", "-protocol_whitelist", "file,pipe",
            "-format_whitelist", _IMAGE_INPUT_FORMATS if content_type == "image" else _VIDEO_INPUT_FORMATS,
            "-show_streams", "-show_format",
            "-show_packets", "-show_entries", entries, "-of", "json", str(path)], heartbeat=heartbeat))
        streams = data["streams"]
        videos = [stream for stream in streams if stream.get("codec_type") == "video"]
        if len(videos) != 1:
            raise ValueError()
        video = videos[0]
        width, height = int(video["width"]), int(video["height"])
        if width <= 0 or height <= 0:
            raise ValueError()
    except DerivativeError:
        raise
    except (ValueError, KeyError, TypeError):
        raise DerivativeError("INVALID_MEDIA_PROBE", "Master must contain one readable image or video stream") from None
    rotation = [video.get("tags", {}).get("rotate", 0)]
    rotation += [item.get("rotation", 0) for item in video.get("side_data_list", [])]
    if any(_fraction(value, Fraction(1)) != 0 for value in rotation):
        raise DerivativeError("MASTER_ROTATION_UNSUPPORTED", "Master video rotation must be zero")
    sar = _fraction(video.get("sample_aspect_ratio"), Fraction(1))
    if sar != 1:
        raise DerivativeError("MASTER_SAR_UNSUPPORTED", "Master pixels must be square")
    pixel = str(video.get("pix_fmt", ""))
    bits = int(video.get("bits_per_raw_sample") or 0)
    if (bits > 8 or any(token in pixel for token in ("p10", "p12", "p14", "p16", "gray16", "rgb48", "rgba64"))
            or video.get("color_transfer") in {"smpte2084", "arib-std-b67"}):
        raise DerivativeError("MASTER_HDR_UNSUPPORTED", "VW1 requires qualified 8-bit SDR media")
    audio = any(stream.get("codec_type") == "audio" for stream in streams)
    container_duration = _fraction(data.get("format", {}).get("duration"), Fraction(0))
    if content_type == "image":
        if video.get("codec_name") not in {"png", "mjpeg", "webp"}:
            raise DerivativeError("UNSUPPORTED_IMAGE_FORMAT", "Image master must be PNG, JPEG or WebP")
        return MediaProbe(width, height, video["codec_name"], pixel, audio, None, None,
                          None, 0, Fraction(0), (), (), container_duration)
    fps = _fraction(video.get("avg_frame_rate"))
    nominal = _fraction(video.get("r_frame_rate"))
    if fps is None or fps <= 0 or nominal != fps:
        raise DerivativeError("MASTER_VFR_UNSUPPORTED", "VW1 requires a verified constant frame rate master")
    timebase = _fraction(video.get("time_base"))
    packets = [packet for packet in data.get("packets", [])
               if packet.get("stream_index") == video["index"]]
    try:
        pts = sorted((int(packet["pts"]) * timebase, "K" in packet.get("flags", "")) for packet in packets)
        if not pts or timebase is None or timebase <= 0 or len({value for value, _ in pts}) != len(pts):
            raise ValueError()
    except (ValueError, KeyError, TypeError):
        raise DerivativeError("MASTER_TIMESTAMPS_UNSUPPORTED", "Video must have complete, unique presentation timestamps") from None
    start = pts[0][0]
    tolerance = timebase
    if any(abs((value - start) - Fraction(index, 1) / fps) > tolerance
           for index, (value, _) in enumerate(pts)):
        raise DerivativeError("MASTER_VFR_UNSUPPORTED", "VW1 requires a verified constant frame rate master")
    for packet in packets:
        duration_ticks = packet.get("duration")
        if duration_ticks is not None and abs(int(duration_ticks) * timebase - 1 / fps) > tolerance:
            raise DerivativeError("MASTER_VFR_UNSUPPORTED", "Video frame durations must be constant")
    duration = Fraction(len(pts), 1) / fps
    key_indices = tuple(index for index, (_, keyframe) in enumerate(pts) if keyframe)
    return MediaProbe(width, height, video["codec_name"], pixel, audio,
                      fps.numerator, fps.denominator, len(pts), _ms(duration), start,
                      key_indices, tuple(_ms(pts[index][0] - start) for index in key_indices),
                      container_duration)


def prepare_node_slices(master_path: Path, output_dir: Path, *, canvas_width: int,
                        canvas_height: int, content_type: str, image_duration_ms: int | None = None,
                        rows: int = 1, columns: int = 3,
                        heartbeat=None, ffmpeg_bin="ffmpeg", ffprobe_bin="ffprobe") -> PreparedMedia:
    """Decode one master into row-major crop branches; verify every generated file.

    Ownership must be resolved afresh by the publication authority before this
    pure processor is called. Its output directory must be private, outside the
    public media root. Publication is a separate all-or-nothing database action.
    """
    if (type(canvas_width) is int and type(canvas_height) is int
            and canvas_width > 0 and canvas_height > 0
            and canvas_width * canvas_height > MAX_GROUP_CANVAS_PIXELS):
        raise DerivativeError("WALL_PROCESSING_CAPACITY_EXCEEDED",
                              "V1 processing capacity is at most 24883200 canvas pixels")
    panel = qualified_panel_geometry(canvas_width, canvas_height, rows, columns)
    if panel is None:
        raise DerivativeError("UNSUPPORTED_WALL_GEOMETRY",
                              "VW1 requires an exact rectangular canvas of Full HD or 4K panels")
    master_path = _local_file(master_path)
    fingerprint = master_path.stat()
    identity = (fingerprint.st_dev, fingerprint.st_ino, fingerprint.st_size, fingerprint.st_mtime_ns, fingerprint.st_ctime_ns)
    source = probe_media(master_path, content_type=content_type, heartbeat=heartbeat, ffprobe_bin=ffprobe_bin)
    if (source.width, source.height) != (canvas_width, canvas_height):
        raise DerivativeError("MASTER_DIMENSIONS_MISMATCH",
                              f"Master must be {canvas_width}x{canvas_height}; actual {source.width}x{source.height}")
    if content_type == "image" and (type(image_duration_ms) is not int or image_duration_ms <= 0):
        raise DerivativeError("INVALID_IMAGE_DURATION", "Image timeline duration must be positive integer milliseconds")
    source_hash = file_sha256(master_path, heartbeat=heartbeat)
    output_dir = Path(output_dir)
    try:
        output_dir.mkdir(parents=True, exist_ok=False)
    except OSError:
        raise DerivativeError("PRIVATE_OUTPUT_UNAVAILABLE", "A new private output directory is required") from None
    panel_width, panel_height = panel
    count = rows * columns
    extension = ".png" if content_type == "image" else ".mp4"
    paths = tuple(output_dir / ("position-" + str(position) + extension)
                  for position in range(1, count + 1))
    branches = []
    for index in range(count):
        row, column = divmod(index, columns)
        source_label = f"s{index}" if count > 1 else "0:v:0"
        prefix = "" if count > 1 else "setpts=PTS-STARTPTS,"
        branches.append(f"[{source_label}]{prefix}crop={panel_width}:{panel_height}:"
                        f"{column * panel_width}:{row * panel_height},setsar=1[v{index}]")
    graph = (("[0:v:0]setpts=PTS-STARTPTS,split=" + str(count)
              + "".join(f"[s{index}]" for index in range(count)) + ";")
             if count > 1 else "") + ";".join(branches)
    argv = [ffmpeg_bin, "-hide_banner", "-loglevel", "error", "-nostdin", "-n", "-threads", "1",
            "-protocol_whitelist", "file,pipe", "-format_whitelist",
            _IMAGE_INPUT_FORMATS if content_type == "image" else _VIDEO_INPUT_FORMATS,
            "-noautorotate", "-i", str(master_path), "-filter_complex_threads", "1", "-filter_complex", graph]
    if content_type == "video":
        fps = Fraction(source.fps_num, source.fps_den)
        gop = max(1, _ms(Fraction(2) * fps) // 1000)
        exact_duration = Fraction(source.frame_count, 1) / fps
        duration_text = f"{float(exact_duration):.9f}"
    for index, path in enumerate(paths):
        argv += ["-map", f"[v{index}]"]
        if content_type == "image":
            argv += ["-an", "-frames:v", "1", "-c:v", "png", "-pix_fmt", "rgba", "-threads", "1", "-update", "1"]
        else:
            if index == 0 and source.has_audio:
                argv += ["-map", "0:a:0", "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
                         "-af", "atrim=duration=" + duration_text + ",asetpts=PTS-STARTPTS"]
            else:
                argv += ["-an"]
            argv += ["-c:v", "libx264", "-threads", "2", "-preset", "fast", "-crf", "18",
                     "-profile:v", "high", "-pix_fmt", "yuv420p", "-r", str(fps), "-fps_mode", "cfr",
                     "-g", str(gop), "-keyint_min", str(gop), "-sc_threshold", "0",
                     "-x264-params", f"open-gop=0:keyint={gop}:min-keyint={gop}:scenecut=0",
                     "-force_key_frames", f"expr:gte(n,n_forced*{gop})",
                     "-video_track_timescale", str(source.fps_num * 512),
                     "-t", duration_text, "-movflags", "+faststart"]
        argv.append(str(path))
    _run(argv, heartbeat=heartbeat)
    prepared = []
    video_probes = []
    for position, path in enumerate(paths, 1):
        probe = probe_media(path, content_type=content_type, heartbeat=heartbeat, ffprobe_bin=ffprobe_bin)
        if (probe.width, probe.height) != (panel_width, panel_height):
            raise DerivativeError("SLICE_DIMENSIONS_MISMATCH", "Derived media dimensions do not match the panel")
        if content_type == "video":
            if (probe.codec_name != "h264" or probe.pixel_format != "yuv420p"
                    or (probe.fps_num, probe.fps_den, probe.frame_count, probe.duration_ms) !=
                       (source.fps_num, source.fps_den, source.frame_count, source.duration_ms)
                    or probe.start_time != 0
                    or probe.has_audio != (source.has_audio and position == 1)
                    or abs(probe.container_duration - exact_duration) > Fraction(2, 1000)):
                raise DerivativeError("SLICE_TIMELINE_MISMATCH", "Derived video does not preserve the shared timeline or audio policy")
            video_probes.append(probe)
        prepared.append(PreparedSlice(position, path, probe.width, probe.height,
            "image/png" if content_type == "image" else "video/mp4", path.stat().st_size,
            file_sha256(path, heartbeat=heartbeat), image_duration_ms if content_type == "image" else probe.duration_ms,
            probe.fps_num, probe.fps_den, probe.frame_count, probe.has_audio, probe.keyframe_pts_ms))
    if video_probes and any(probe.keyframe_indices != video_probes[0].keyframe_indices for probe in video_probes[1:]):
        raise DerivativeError("SLICE_KEYFRAMES_MISMATCH", "Derived video keyframes are not aligned")
    final = master_path.stat()
    if identity != (final.st_dev, final.st_ino, final.st_size, final.st_mtime_ns, final.st_ctime_ns):
        raise DerivativeError("MASTER_CHANGED", "Master changed during preparation")
    if source_hash != file_sha256(master_path, heartbeat=heartbeat):
        raise DerivativeError("MASTER_CHANGED", "Master changed during preparation")
    return PreparedMedia(content_type, image_duration_ms if content_type == "image" else source.duration_ms,
                         source_hash, tuple(prepared), source.fps_num, source.fps_den, source.frame_count)
