import pytest

from app.catalog import MODELS, normalize_request, public_models


def test_requested_models_and_observed_capabilities():
    assert len(MODELS) == 17
    mini = normalize_request({"model": "sd-2-0-mini", "prompt": "sunrise", "resolution": "480p"})
    assert (mini["upstream_model"], mini["duration"], mini["resolution"]) == ("seedance-2-0-mini", 4, "480p")
    assert MODELS["sd-2-5"].limits == {"images": 30, "videos": 10, "audios": 10}
    assert MODELS["vidu-q4-540p"].limits == {"images": 15, "videos": 0, "audios": 3}
    models = {row["id"]: row for row in public_models()}
    assert models["minimax-h3-768p"]["meta"]["actual_default_resolution"] == "720p"
    assert models["sd-2-5-480p-preview"]["meta"]["inferred_alias"] is True
    assert "no separate preview" in models["sd-2-5-480p-preview"]["meta"]["alias_note"]


def test_full_seedance_and_vidu_standard_seedance_content():
    def request(model, counts):
        return {"model": model, "content": [{"type": "text", "text": "参考图片1与音频1"}] + [
            {"type": kind + "_url", kind + "_url": {"url": f"https://media.example.test/{kind}/{i}"}}
            for kind, count in counts.items() for i in range(count)]}
    mini = normalize_request(request("sd-2-0-mini", {"image": 9, "audio": 3, "video": 3}))
    assert [len(mini[k]) for k in ("images", "audios", "videos")] == [9, 3, 3]
    assert mini["mode"] == "reference_material"
    vidu = normalize_request(request("vidu-q4-540p", {"image": 15, "audio": 3}))
    assert [len(vidu[k]) for k in ("images", "audios", "videos")] == [15, 3, 0]
    assert vidu["duration"] == 3


@pytest.mark.parametrize("body,match", [
    ({"model": "sd-2-0-mini", "resolution": "1080p"}, "resolution"),
    ({"model": "sd-2-0-1080p", "resolution": "720p"}, "actual 1080p"),
    ({"model": "vidu-q4-540p", "video_urls": ["https://media.example.test/v.mp4"]}, "at most 0 videos"),
    ({"model": "vidu-q4-540p", "image_urls": ["https://media.example.test/i.png"] * 16}, "at most 15 images"),
    ({"model": "kling-4.0", "audio_urls": ["https://media.example.test/a.mp3"]}, "at most 0 audios"),
    ({"model": "kling-4.0", "ratio": "4:3"}, "aspect ratio"),
    ({"model": "sd-2-5", "duration": 31}, "4–30"),
    ({"model": "minimax-h3-768p", "image_urls": ["i"] * 9, "audio_urls": ["a"] * 3, "video_urls": ["v"]}, "12 total"),
])
def test_reject_unsupported_combinations_before_upload(body, match):
    with pytest.raises(ValueError, match=match):
        normalize_request({"prompt": "sunrise", **body})
