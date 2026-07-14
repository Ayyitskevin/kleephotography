import pytest

from app import video

pytestmark = pytest.mark.unit


@pytest.mark.unit
def test_rendition_args_center_crop():
    args = video.rendition_args("in.mov", "out.mp4", 1080, 1920, 23)
    assert args[0] == "ffmpeg" and args[-1] == "out.mp4"
    vf = args[args.index("-vf") + 1]
    # fill the target frame from any source aspect, then center-crop
    assert vf == "scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920"
    # same delivery profile as the web transcode
    assert "yuv420p" in args and "+faststart" in args and "libx264" in args


@pytest.mark.unit
def test_rendition_presets_match_migration_vocabulary():
    # preset keys are migration 066's CHECK vocabulary — keep them in sync
    assert set(video.RENDITION_PRESETS) == {"9x16", "1x1"}
    assert video.RENDITION_PRESETS["9x16"] == (1080, 1920)
    assert video.RENDITION_PRESETS["1x1"] == (1080, 1080)
