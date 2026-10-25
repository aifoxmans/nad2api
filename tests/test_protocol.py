import hashlib
import json
from unittest.mock import Mock

import pytest
import requests

from app.browser import BrowserRejected
from app.catalog import normalize_request
from app.client import GRAPH, NadClient, NadError, build_submit_body, build_video_param, extract_result_urls
from app.media import CHUNK_THRESHOLD, upload_media_bytes, validate_material_durations


def client():
    return NadClient({"id": 1, "proxy_url": "http://127.0.0.1:9080", "cookie": "session=example", "dfp": "test-device"})


def test_submit_body_binds_all_materials_and_preserves_standard_options():
    normalized = normalize_request({"model": "sd-2-0-mini", "prompt": "图片1 音频1 视频1", "resolution": "480p"})
    assets = {kind: [{"url": f"https://media.example.test/{kind}", "duration": 3, "size": 12}] for kind in ("images", "videos", "audios")}
    param = build_video_param(normalized, assets)
    body = build_submit_body(param, assets, mode="reference_material")
    assert param["image"] == [assets[k][0]["url"] for k in ("images", "videos", "audios")]
    assert param["duration"] == "4" and param["resolution"] == "480p"
    assert body["input"]["userParams"]["taskType"] == "MULTI_REF_IMG2VIDEO"
    assert len(body["input"]["attachments"]) == 3
    assert body["input"]["content"] == param["description"]


def test_generation_network_failure_is_never_retried():
    api = client()
    api.session.request = Mock(side_effect=requests.Timeout("test"))
    with pytest.raises(NadError, match="outcome unavailable"):
        api.request("POST", GRAPH + "/ainvoke", {})
    assert api.session.request.call_count == 1


def test_http_rejection_is_distinct_from_unknown_submission():
    api = client()
    response = Mock(ok=True, status_code=200)
    response.json.return_value = {"code": "E90002", "msg": "insufficient points"}
    api.session.request = Mock(return_value=response)
    with pytest.raises(BrowserRejected, match="insufficient points"):
        api.request("POST", GRAPH + "/ainvoke", {})
    assert api.session.request.call_count == 1


@pytest.mark.parametrize("http,body", [(500, {"code": "E50000", "msg": "error"}),
                                      (408, {"code": "E40800", "msg": "timeout"}),
                                      (200, {"message": "incomplete"}),
                                      (400, {"code": "A00000", "task_id": 100})])
def test_ambiguous_generation_response_never_becomes_confirmed_rejection(http, body):
    api = client()
    response = Mock(ok=http < 400, status_code=http)
    response.json.return_value = body
    api.session.request = Mock(return_value=response)
    with pytest.raises(NadError):
        api.request("POST", GRAPH + "/ainvoke", {})
    assert api.session.request.call_count == 1


def test_non_json_generation_error_remains_unknown():
    api = client()
    response = Mock(ok=False, status_code=400)
    response.json.side_effect = ValueError("not json")
    api.session.request = Mock(return_value=response)
    with pytest.raises(NadError):
        api.request("POST", GRAPH + "/ainvoke", {})
    assert api.session.request.call_count == 1


def test_submit_uses_independent_conversation_and_exact_response_binding():
    api = client()
    calls = []
    def request(method, path, body=None, **kwargs):
        calls.append((method, path, body, kwargs))
        if path.endswith("/ainvoke"):
            return {"code": "A00000", "thread_id": kwargs["params"]["thread_id"], "task_id": 100}
        return {"code": "A00000", "data": {}}
    api.request = request
    n = normalize_request({"model": "sd-2-0-mini", "prompt": "sunrise"})
    receipt = api.submit(n, {"images": [], "videos": [], "audios": []})
    assert receipt["run_id"] == "100"
    assert calls[0][1] == "/apis/nadouai/session/create"
    assert calls[1][3]["params"] == {"command": 2, "stream": "true", "thread_id": receipt["thread_id"]}
    assert calls[1][2]["input"]["userParams"]["taskType"] == "TEXT2VIDEO"
    assert receipt["credits_consumed"] is None


def test_poll_only_returns_completed_output_video_urls():
    api = client()
    ui = {"asset": {"videos": [{"status": "completed", "url": "https://result.example.test/video.mp4"}]},
          "input": {"videos": [{"url": "https://input.example.test/reference.mp4"}]}}
    api.request = Mock(return_value={"data": {"files": {"ui_state.json": [{"content": json.dumps(ui)}]}}})
    result = api.poll("100", "s_example")
    assert result["status"] == "succeeded"
    assert result["result_urls"] == ["https://result.example.test/video.mp4"]
    assert extract_result_urls({"asset": {"videos": [{"status": "running", "url": "https://input.example.test/reference.mp4"}]}}) == []


def test_small_upload_preserves_metadata_duration_units():
    api = Mock()
    api.request.return_value = {"data": {"id": 7, "url": "https://material.example.test/audio.mp3",
        "metadata": {"duration": 8000, "size": 12}}}
    uploaded = upload_media_bytes(b"test", "audio/mpeg", "audio.mp3", "audios", client=api)
    assert uploaded["duration"] == 8
    assert uploaded["asset_id"] == "7"
    assert api.request.call_args.kwargs["files"]["file"] == ("audio.mp3", b"test", "audio/mpeg")


def test_chunk_upload_does_not_leak_account_auth_to_upload_host(monkeypatch):
    api = Mock()
    api.session.proxies = {"https": "http://127.0.0.1:9080"}
    data = b"x" * CHUNK_THRESHOLD
    digest = hashlib.md5(data, usedforsecurity=False).hexdigest()
    api.request.side_effect = [
        {"data": {"uploadUrl": "https://intl-upload.iqiyi.com/upload?signed=example", "fileId": "file-example"}},
        {"data": {"staticwebPath": "https://material.example.test/upload.png"}},
        {"data": {"url": "https://material.example.test/upload.png", "metadata": {"size": len(data)}}},
    ]
    upload = Mock()
    upload.proxies = {}
    upload.post.return_value.json.return_value = {"file_id": "file-example", "range_md5": digest, "file_range_accepted": len(data)}
    monkeypatch.setattr("app.media.requests.Session", lambda: upload)
    result = upload_media_bytes(data, "image/png", "reference.png", "images", client=api)
    assert result["url"] == "https://material.example.test/upload.png"
    assert upload.trust_env is False
    assert "headers" not in upload.post.call_args.kwargs
    assert "cookies" not in upload.post.call_args.kwargs
    assert upload.post.call_args.kwargs["data"]["range"] == f"0-{len(data)-1}"


def test_vidu_rejects_audio_duration_before_generation():
    n = normalize_request({"model": "vidu-q4-540p", "prompt": "sunrise", "image_urls": ["https://media.example.test/i.png"]})
    with pytest.raises(BrowserRejected, match="3–12"):
        validate_material_durations(n, {"images": [], "videos": [], "audios": [{"duration": 2}]})


def test_reward_needing_captcha_is_never_bypassed():
    api = client()
    api.request = Mock(return_value={"data": {"status": 1, "quotaExhausted": False}})
    assert api.daily_checkin()["manual_required"] is True
    assert api.request.call_count == 1
