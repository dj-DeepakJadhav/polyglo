"""Narrated video export: a story's scene images + one locale's narration audio,
composed into a single downloadable MP4 slideshow.

Real ffmpeg via ``subprocess`` — no pure-Python muxer is trustworthy for real
audio+video, and no video-composition library was already a dependency. The
binary comes from ``imageio-ffmpeg`` (a real, per-platform static build shipped
inside its own pip wheel) rather than an added system package, so this works the
same way in local dev, CI, and the Docker image without touching the Dockerfile.

Deliberately takes already-fetched blob bytes rather than a ``BlobStore`` — this
module does no DB or storage access itself (see ``web.py``'s route for that),
which keeps it a pure, easily unit-testable function.
"""

from __future__ import annotations

import subprocess
import tempfile
import threading
from pathlib import Path

from polyglo.models import LocalizedScene, QAStatus, Scene

__all__ = ["VideoComposeError", "VideoBusyError", "compose_story_video"]

_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_JPEG_MAGIC = b"\xff\xd8\xff"
_WAV_MAGIC = b"RIFF"
_MP3_MAGICS = (b"ID3", b"\xff\xfb", b"\xff\xf3", b"\xff\xf2")

# Real production incident (2026-08-02): the app's Render instance hit its memory
# limit and was force-restarted shortly after this feature shipped, with no
# concurrency limit on it at all. Each real ffmpeg encode holds every scene's
# image+audio bytes in memory plus libx264's own encoding buffers -- on a small
# instance, two or three of those running at once is a real, plausible OOM cause,
# not a theoretical one. `_ENCODE_SLOTS` bounds how many compositions can run at
# the same time; a request that can't get a slot fails fast (VideoBusyError -> a
# real 503) rather than queuing and holding a web worker thread, which would
# just move the memory pressure from "concurrent ffmpeg processes" to
# "concurrent held requests" instead of actually fixing it.
_MAX_CONCURRENT_ENCODES = 1
_encode_slots = threading.Semaphore(_MAX_CONCURRENT_ENCODES)

# 640x640 + a fast x264 preset instead of 1024x1024 + the (unset, so "medium")
# default preset -- x264's slower presets trade real encoder-side memory and CPU
# for compression efficiency neither of which this feature needs for a short demo
# clip. Cuts per-encode memory pressure without changing the actual feature.
_VIDEO_SIZE = 640


class VideoComposeError(RuntimeError):
    pass


class VideoBusyError(VideoComposeError):
    """Raised when another video composition is already running and the
    concurrency limit would be exceeded. Callers should surface this as a real
    503/"try again shortly", not a 500 or an infinite wait."""
    pass


def _looks_like_real_image(data: bytes) -> bool:
    return data.startswith(_PNG_MAGIC) or data.startswith(_JPEG_MAGIC)


def _looks_like_real_audio(data: bytes) -> str | None:
    """Returns the real file extension ("wav"/"mp3") or None if not real media —
    this project's own simulated-narration marker bytes (``simulated-audio|...``)
    correctly return None here, same reasoning as web.py's `_sniff()`."""
    if data.startswith(_WAV_MAGIC):
        return "wav"
    if any(data.startswith(m) for m in _MP3_MAGICS):
        return "mp3"
    return None


def _ffmpeg_exe() -> str:
    try:
        import imageio_ffmpeg
    except Exception as exc:  # pragma: no cover - environment issue, not logic
        raise VideoComposeError(
            f"ffmpeg is not available ({type(exc).__name__}: {exc}) — "
            "imageio-ffmpeg should be installed as a project dependency"
        ) from exc
    return imageio_ffmpeg.get_ffmpeg_exe()


_HAS_DRAWTEXT: bool | None = None


def _has_drawtext(ffmpeg: str) -> bool:
    """Whether this ffmpeg build actually ships the ``drawtext`` filter.

    It is NOT always present. ``drawtext`` requires libfreetype at compile time, and
    the static binary ``imageio-ffmpeg`` ships is built without it — so burned-in
    subtitles work on a dev machine with a full ffmpeg and fail in production with
    ``No such filter: 'drawtext'``, taking the whole export down with them. Found the
    hard way: a real 422 from the deployed service while the identical code produced a
    correct MP4 locally.

    Probed once per process (``-filters`` is a cheap, no-input invocation) and cached,
    rather than per scene.
    """
    global _HAS_DRAWTEXT
    if _HAS_DRAWTEXT is None:
        try:
            probe = subprocess.run(
                [ffmpeg, "-hide_banner", "-filters"], capture_output=True, text=True,
            )
            _HAS_DRAWTEXT = " drawtext " in probe.stdout
        except Exception:  # pragma: no cover - probe failure means assume absent
            _HAS_DRAWTEXT = False
    return _HAS_DRAWTEXT


def _run(ffmpeg: str, args: list[str]) -> None:
    result = subprocess.run(
        [ffmpeg, *args], capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise VideoComposeError(
            f"ffmpeg exited {result.returncode}: {result.stderr[-800:]}"
        )


def _make_fallback_png() -> bytes:
    return (
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
        b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\xcf\xc0"
        b"\x00\x00\x03\x01\x01\x00\x18\xdd\x8d\xb0\x00\x00\x00\x00IEND\xaeB`\x82"
    )


def _make_fallback_wav() -> bytes:
    import math
    import struct
    from polyglo.narrate import pcm_to_wav

    samples = bytearray()
    for i in range(32000):
        val = int(500 * math.sin(2 * math.pi * 440 * i / 16000))
        samples.extend(struct.pack("<h", val))
    return pcm_to_wav(bytes(samples))


def compose_story_video(
    scenes: list[Scene],
    localized: list[LocalizedScene],
    image_bytes_by_ordinal: dict[int, bytes],
    audio_bytes_by_ordinal: dict[int, bytes],
    aspect_ratio: str = "9:16",
    allow_fallback: bool = False,
) -> bytes:
    """Returns raw MP4 bytes: a vertical 9:16 Reel or 1:1 slideshow of every qualifying
    scene's image and audio clip.
    """
    localized_by_ordinal = {ls.ordinal: ls for ls in localized}
    included: list[tuple[Scene, LocalizedScene, str]] = []  # (scene, ls, audio_ext)

    for scene in sorted(scenes, key=lambda s: s.ordinal):
        ls = localized_by_ordinal.get(scene.ordinal)
        if ls is None or ls.qa_status is QAStatus.QUARANTINED:
            continue
        image = image_bytes_by_ordinal.get(scene.ordinal)
        audio = audio_bytes_by_ordinal.get(scene.ordinal)

        if not image or not _looks_like_real_image(image):
            if not allow_fallback:
                continue
            image = _make_fallback_png()
            image_bytes_by_ordinal[scene.ordinal] = image

        audio_ext = _looks_like_real_audio(audio) if audio else None
        if audio_ext is None:
            if not allow_fallback:
                continue
            audio = _make_fallback_wav()
            audio_bytes_by_ordinal[scene.ordinal] = audio
            audio_ext = "wav"

        included.append((scene, ls, audio_ext))

    if not included:
        raise VideoComposeError(
            "no scene has both a real image and real narration audio for this "
            "locale yet — nothing to compose into a video"
        )

    if not _encode_slots.acquire(blocking=False):
        raise VideoBusyError(
            "a video is already being composed — this app allows one at a time "
            "to protect its memory budget. Try again in a few seconds."
        )
    try:
        ffmpeg = _ffmpeg_exe()

        if aspect_ratio == "9:16":
            w, h = 540, 960
        else:
            w, h = 640, 640

        with tempfile.TemporaryDirectory(prefix="polyglo-video-") as tmp:
            tmp_dir = Path(tmp)
            segment_paths: list[Path] = []

            for scene, ls, audio_ext in included:
                img_path = tmp_dir / f"seg_{scene.ordinal}.png"
                img_path.write_bytes(image_bytes_by_ordinal[scene.ordinal])
                audio_path = tmp_dir / f"seg_{scene.ordinal}.{audio_ext}"
                audio_path.write_bytes(audio_bytes_by_ordinal[scene.ordinal])
                segment_path = tmp_dir / f"seg_{scene.ordinal}.mp4"

                # drawtext crashes on non-ASCII (umlauts, accents, Devanagari, CJK, etc.)
                # and on several special chars. Safest approach: keep only printable ASCII,
                # then escape the ffmpeg drawtext special characters.
                safe_text = "".join(
                    c if (32 <= ord(c) < 127) else " "
                    for c in ls.text
                )
                for ch in ("\\", "'", ":", "[", "]", "=", ",", "{", "}"):
                    safe_text = safe_text.replace(ch, " ")
                safe_text = " ".join(safe_text.split())  # collapse whitespace
                sub_text = safe_text[:120]  # cap length to avoid oversized subtitle boxes

                font_size = 22 if aspect_ratio == "9:16" else 20
                y_offset = "h-90" if aspect_ratio == "9:16" else "h-70"
                # Subtitles are a nice-to-have; a video with no caption still carries the
                # narration and the art. If this ffmpeg build has no drawtext, drop the
                # filter rather than failing the export.
                subtitle_filter = (
                    f",drawtext=text='{sub_text}':x=(w-text_w)/2:y={y_offset}:"
                    f"fontcolor=white:fontsize={font_size}:box=1:boxcolor=black@0.65:boxborderw=8"
                ) if _has_drawtext(ffmpeg) else ""

                zoom_pan = (
                    f"zoompan=z='min(zoom+0.0015,1.2)':d=125:x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s={w}x{h},"
                    f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
                    f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color=black"
                    f"{subtitle_filter}"
                )

                _run(ffmpeg, [
                    "-y", "-loop", "1", "-i", str(img_path), "-i", str(audio_path),
                    "-filter_complex",
                    f"[0:v]{zoom_pan}[v1]; "
                    "[1:a]volume=1.2[voice]; "
                    "aevalsrc='0.012*sin(2*PI*261.63*t)+0.012*sin(2*PI*329.63*t)+0.008*sin(2*PI*392.00*t)':s=24000[bg]; "
                    "[voice][bg]amix=inputs=2:duration=first:dropout_transition=2[aout]",
                    "-c:v", "libx264", "-preset", "ultrafast",
                    "-pix_fmt", "yuv420p",
                    "-map", "[v1]", "-map", "[aout]",
                    "-c:a", "aac", "-b:a", "128k",
                    "-shortest", "-movflags", "+faststart",
                    str(segment_path),
                ])
                segment_paths.append(segment_path)

            concat_path = tmp_dir / "concat.txt"
            concat_path.write_text(
                "\n".join(f"file '{p.as_posix()}'" for p in segment_paths), encoding="utf-8",
            )

            output_path = tmp_dir / "story.mp4"
            _run(ffmpeg, [
                "-y", "-f", "concat", "-safe", "0", "-i", str(concat_path),
                "-c", "copy", str(output_path),
            ])

            return output_path.read_bytes()
    finally:
        _encode_slots.release()
