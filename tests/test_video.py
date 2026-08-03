"""Tests for narrated video export (scene images + audio -> one MP4).

Unit tests monkeypatch subprocess.run entirely — no real ffmpeg needed, and no
dependency on imageio-ffmpeg being resolvable in whatever environment runs these.
One real-binary smoke test runs for real when imageio-ffmpeg IS available (skipped
otherwise), matching this project's "verify by actually running" discipline
without making that a hard requirement for the rest of the suite.
"""

from __future__ import annotations

import subprocess

import pytest

from polyglo.models import LocalizedScene, QAStatus, Scene
from polyglo.video import VideoComposeError, compose_story_video

REAL_PNG = b"\x89PNG\r\n\x1a\n" + b"fake but has the right magic bytes"
REAL_WAV = b"RIFF" + b"fake but has the right magic bytes for a wav"
REAL_MP3 = b"ID3" + b"fake but has the right magic bytes for an mp3"
SIMULATED_IMAGE = b"simulated-image|model|a prompt"
SIMULATED_AUDIO = b"simulated-audio|model|locale|text"


def make_scene(ordinal: int) -> Scene:
    return Scene("s1", ordinal, f"text {ordinal}", f"a scene showing {ordinal}",
                 image_sha256=f"img{ordinal}")


def make_localized(ordinal: int, status: QAStatus = QAStatus.PASS) -> LocalizedScene:
    return LocalizedScene("s1", ordinal, "es-ES", f"texto {ordinal}",
                          audio_sha256=f"aud{ordinal}", qa_status=status)


class FakeCompletedProcess:
    def __init__(self, returncode: int = 0, stderr: str = "", stdout: str = ""):
        self.returncode = returncode
        self.stderr = stderr
        self.stdout = stdout


def fake_subprocess_run_factory(returncode: int = 0, fail_on_call: int | None = None):
    """Returns a fake subprocess.run that records every call and writes a
    placeholder file at the output path argument (the last positional arg),
    so compose_story_video's final read_bytes() has something to read."""
    calls: list[list[str]] = []

    def fake_run(args, **kwargs):
        # The one-off `-filters` capability probe (does this ffmpeg build have
        # drawtext?) is not an encode. Keep it out of `calls` so every count
        # assertion below stays about actual encoding work.
        if "-filters" in args:
            return FakeCompletedProcess(returncode=0, stderr="", stdout=" drawtext ")
        calls.append(args)
        call_index = len(calls)
        rc = returncode if fail_on_call is None or call_index != fail_on_call else 1
        if rc == 0:
            output_path = args[-1]
            with open(output_path, "wb") as f:
                f.write(b"fake mp4 bytes")
        return FakeCompletedProcess(returncode=rc, stderr="simulated ffmpeg failure")

    fake_run.calls = calls
    return fake_run


@pytest.fixture(autouse=True)
def fake_ffmpeg_exe(monkeypatch):
    monkeypatch.setattr("polyglo.video._ffmpeg_exe", lambda: "ffmpeg")


def test_composes_a_real_looking_video_from_qualifying_scenes(monkeypatch):
    fake_run = fake_subprocess_run_factory()
    monkeypatch.setattr(subprocess, "run", fake_run)

    scenes = [make_scene(0), make_scene(1)]
    localized = [make_localized(0), make_localized(1)]
    images = {0: REAL_PNG, 1: REAL_PNG}
    audio = {0: REAL_WAV, 1: REAL_MP3}

    result = compose_story_video(scenes, localized, images, audio)

    assert result == b"fake mp4 bytes"
    # 2 per-scene segment calls + 1 concat call
    assert len(fake_run.calls) == 3


def test_segment_command_includes_expected_ffmpeg_flags(monkeypatch):
    fake_run = fake_subprocess_run_factory()
    monkeypatch.setattr(subprocess, "run", fake_run)

    compose_story_video([make_scene(0)], [make_localized(0)],
                        {0: REAL_PNG}, {0: REAL_WAV})

    segment_call = fake_run.calls[0]
    for flag in ("-loop", "1", "-pix_fmt", "yuv420p", "-shortest"):
        assert flag in segment_call


def test_concat_call_uses_c_copy_and_safe_0(monkeypatch):
    fake_run = fake_subprocess_run_factory()
    monkeypatch.setattr(subprocess, "run", fake_run)

    compose_story_video([make_scene(0), make_scene(1)],
                        [make_localized(0), make_localized(1)],
                        {0: REAL_PNG, 1: REAL_PNG}, {0: REAL_WAV, 1: REAL_WAV})

    concat_call = fake_run.calls[-1]
    assert "-f" in concat_call and "concat" in concat_call
    assert "-safe" in concat_call and "0" in concat_call
    assert "-c" in concat_call and "copy" in concat_call


def test_scene_with_no_image_is_skipped(monkeypatch):
    fake_run = fake_subprocess_run_factory()
    monkeypatch.setattr(subprocess, "run", fake_run)

    scenes = [make_scene(0), make_scene(1)]
    localized = [make_localized(0), make_localized(1)]
    images = {1: REAL_PNG}  # scene 0 has no image at all
    audio = {0: REAL_WAV, 1: REAL_WAV}

    compose_story_video(scenes, localized, images, audio)

    # only scene 1 qualifies -> 1 segment call + 1 concat call
    assert len(fake_run.calls) == 2


def test_scene_with_no_audio_is_skipped(monkeypatch):
    fake_run = fake_subprocess_run_factory()
    monkeypatch.setattr(subprocess, "run", fake_run)

    compose_story_video([make_scene(0), make_scene(1)],
                        [make_localized(0), make_localized(1)],
                        {0: REAL_PNG, 1: REAL_PNG}, {0: REAL_WAV})

    assert len(fake_run.calls) == 2  # scene 1 (no audio) skipped


def test_quarantined_scene_is_skipped(monkeypatch):
    fake_run = fake_subprocess_run_factory()
    monkeypatch.setattr(subprocess, "run", fake_run)

    compose_story_video(
        [make_scene(0), make_scene(1)],
        [make_localized(0), make_localized(1, status=QAStatus.QUARANTINED)],
        {0: REAL_PNG, 1: REAL_PNG}, {0: REAL_WAV, 1: REAL_WAV},
    )
    assert len(fake_run.calls) == 2  # scene 1 (quarantined) skipped


def test_simulated_placeholder_bytes_are_not_treated_as_real_media(monkeypatch):
    """The regression this project would actually hit first, given it runs
    zero-credential by default: simulated marker bytes must never become a fake
    'video' — this is the same reasoning web.py's own _sniff() already applies."""
    fake_run = fake_subprocess_run_factory()
    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(VideoComposeError, match="no scene has both"):
        compose_story_video([make_scene(0)], [make_localized(0)],
                            {0: SIMULATED_IMAGE}, {0: SIMULATED_AUDIO})
    assert fake_run.calls == []


def test_no_qualifying_scenes_raises_before_any_ffmpeg_call(monkeypatch):
    fake_run = fake_subprocess_run_factory()
    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(VideoComposeError, match="no scene has both"):
        compose_story_video([], [], {}, {})
    assert fake_run.calls == []


def test_ffmpeg_failure_raises_video_compose_error_with_stderr(monkeypatch):
    fake_run = fake_subprocess_run_factory(fail_on_call=1)
    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(VideoComposeError, match="simulated ffmpeg failure"):
        compose_story_video([make_scene(0)], [make_localized(0)],
                            {0: REAL_PNG}, {0: REAL_WAV})


def test_scenes_are_composed_in_ordinal_order_regardless_of_input_order(monkeypatch):
    fake_run = fake_subprocess_run_factory()
    monkeypatch.setattr(subprocess, "run", fake_run)

    compose_story_video(
        [make_scene(2), make_scene(0), make_scene(1)],
        [make_localized(2), make_localized(0), make_localized(1)],
        {0: REAL_PNG, 1: REAL_PNG, 2: REAL_PNG},
        {0: REAL_WAV, 1: REAL_WAV, 2: REAL_WAV},
    )
    segment_calls = fake_run.calls[:-1]
    ordinals_seen = [
        int(str(c[c.index("-i") + 1]).rsplit("seg_", 1)[1].split(".")[0])
        for c in segment_calls
    ]
    assert ordinals_seen == [0, 1, 2]


# ---------------------------------------------------------------------------
# Optional real-binary smoke test
# ---------------------------------------------------------------------------

def _real_ffmpeg_available() -> bool:
    try:
        import imageio_ffmpeg
        imageio_ffmpeg.get_ffmpeg_exe()
        return True
    except Exception:
        return False


@pytest.mark.skipif(not _real_ffmpeg_available(), reason="imageio-ffmpeg not resolvable in this environment")
def test_real_ffmpeg_produces_a_genuinely_playable_mp4(monkeypatch, tmp_path):
    """No monkeypatching of subprocess here — a real ffmpeg binary composes a real,
    tiny (1x1 image + near-silent audio) video and we check the actual MP4 magic."""
    from io import BytesIO

    from PIL import Image

    from polyglo.audio_utils import pcm_to_wav

    monkeypatch.undo()  # remove the autouse _ffmpeg_exe patch for this one real test

    buf = BytesIO()
    Image.new("RGB", (4, 4), color=(10, 20, 30)).save(buf, format="PNG")
    real_image = buf.getvalue()
    real_wav = pcm_to_wav(b"\x00\x00" * 4800)  # 0.2s of silence at 24kHz mono

    result = compose_story_video(
        [make_scene(0)], [make_localized(0)], {0: real_image}, {0: real_wav},
    )

    assert len(result) > 100
    assert b"ftyp" in result[:64]


# ---------------------------------------------------------------------------
# Concurrency limit — real production incident (2026-08-02): this route had no
# resource ceiling at all when it shipped, and the app's Render instance hit its
# memory limit and was force-restarted shortly after. Each real ffmpeg encode
# holds every scene's image+audio bytes in memory plus libx264's own encoding
# buffers; bounding concurrent encodes is the direct fix.
# ---------------------------------------------------------------------------


def test_second_concurrent_composition_raises_video_busy_error(monkeypatch):
    """Simulates two overlapping requests without needing real threads: the first
    call holds the semaphore open (via a fake subprocess.run that blocks until
    released), the second call must fail fast with VideoBusyError rather than
    queue or block."""
    import threading

    first_call_started = threading.Event()
    release_first_call = threading.Event()

    def slow_run(args, **kwargs):
        first_call_started.set()
        release_first_call.wait(timeout=5)
        output_path = args[-1]
        with open(output_path, "wb") as f:
            f.write(b"fake mp4 bytes")
        return FakeCompletedProcess(returncode=0)

    monkeypatch.setattr(subprocess, "run", slow_run)

    result_holder = {}
    def _run_first():
        result_holder["first"] = compose_story_video(
            [make_scene(0)], [make_localized(0)], {0: REAL_PNG}, {0: REAL_WAV},
        )

    t = threading.Thread(target=_run_first)
    t.start()
    assert first_call_started.wait(timeout=5)  # first call has the semaphore now

    with pytest.raises(VideoComposeError, match="already being composed"):
        compose_story_video([make_scene(0)], [make_localized(0)], {0: REAL_PNG}, {0: REAL_WAV})

    release_first_call.set()
    t.join(timeout=5)
    assert result_holder["first"] == b"fake mp4 bytes"  # first call still succeeded


def test_semaphore_is_released_after_a_composition_so_the_next_one_can_run(monkeypatch):
    fake_run = fake_subprocess_run_factory()
    monkeypatch.setattr(subprocess, "run", fake_run)

    # Two sequential (not concurrent) calls must both succeed — the semaphore
    # must be released after the first completes, not held forever.
    compose_story_video([make_scene(0)], [make_localized(0)], {0: REAL_PNG}, {0: REAL_WAV})
    compose_story_video([make_scene(0)], [make_localized(0)], {0: REAL_PNG}, {0: REAL_WAV})
    assert len(fake_run.calls) == 4  # 2 segment + 2 concat calls across both runs


def test_semaphore_is_released_even_when_ffmpeg_fails(monkeypatch):
    fake_run = fake_subprocess_run_factory(returncode=1)
    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(VideoComposeError):
        compose_story_video([make_scene(0)], [make_localized(0)], {0: REAL_PNG}, {0: REAL_WAV})

    # A second call must still be able to acquire the slot — a failed composition
    # must not leak the semaphore forever.
    fake_run_2 = fake_subprocess_run_factory(returncode=1)
    monkeypatch.setattr(subprocess, "run", fake_run_2)
    with pytest.raises(VideoComposeError):
        compose_story_video([make_scene(0)], [make_localized(0)], {0: REAL_PNG}, {0: REAL_WAV})
