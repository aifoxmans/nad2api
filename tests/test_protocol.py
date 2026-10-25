import hashlib
import json
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from unittest.mock import Mock

import pytest
import requests

from app.browser import BrowserRejected
from app.catalog import normalize_request
from app.client import GRAPH, NadClient, NadError, build_submit_body, build_video_param, extract_result_urls
from app.media import CHUNK_THRESHOLD, upload_media_bytes, validate_material_durations


def test_material_failure_identifies_type_and_index_without_source_url(monkeypatch):
    from app.media import MediaError
    api = client()
    calls = []
    def prepare(source, kind, account_id):
        calls.append((kind, account_id))
        if len(calls) == 2:
            raise MediaError("reference download failed: headers ReadTimeout bytes=0")
        return {"url": "https://cdn.example.test/one.png"}
    monkeypatch.setattr(api, "_upload_input", prepare)
    n = normalize_request({"prompt": "lake", "image_urls": ["https://private.example.test/one.png?token=one", "https://private.example.test/two.png?token=two"]})
    with pytest.raises(MediaError, match=r"images\[2\]: reference download failed") as raised:
        api.upload_inputs(n)
    assert "token=" not in str(raised.value) and "https://" not in str(raised.value)
from app.store import Store


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


def test_inline_sources_are_bound_to_uploaded_assets_in_full_upstream_body():
    image, video = "https://source.example.test/image1.jpg", "https://source.example.test/video2.mp4?sig=abc%2Fdef%3D&x="
    caller = {"model": "sd-2-0-fast", "duration": 15, "resolution": "720p", "aspect_ratio": "9:16",
              "image_urls": [image], "prompt": f"参考视频：({video})\n再次使用 {video}\n{image}"}
    original = json.loads(json.dumps(caller))
    normalized = normalize_request(caller)
    assets = {"images": [{"url": "https://uploaded.example.test/image.jpg", "width": 1080, "height": 1440}],
              "videos": [{"url": "https://uploaded.example.test/video.mp4", "duration": 4}], "audios": []}
    param = build_video_param(normalized, assets)
    body = build_submit_body(param, assets, mode=normalized["mode"])
    assert caller == original
    assert param["image"] == [assets["images"][0]["url"], assets["videos"][0]["url"]]
    assert [item["url"] for item in body["input"]["attachments"]] == param["image"]
    assert param["description"].count('data-type="reference-tag"') == 3
    assert image not in param["description"] and video not in param["description"]
    assert body["input"]["content"] == param["description"]
    assert body["input"]["userParams"]["taskType"] == "MULTI_REF_IMG2VIDEO"


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


def artifact_failure_message(error="Generated video has copyright restrictions", *,
                             path="/videos/asset-target", thread_id="s_example", stream_mode="updates",
                             metadata_state="ERROR", role="main_director"):
    location, _, filename = path.rpartition("/")
    return {"thread_id": thread_id, "msg_index": 10, "data": {
        "stream_mode": stream_mode, "status": "completed", "metadata": {"agent_role": role},
        "content": {"_single_asset": {"status": "error", "notify_parts": [{
            "filename": filename, "metadata": {"location": location, "state": metadata_state, "error": error},
        }]}}}}


def failed_artifact_poll(api, history, *, error="Artifact generation failed", path="/videos/asset-target"):
    files = {"data": {"files": {"ui_state.json": [{"content": json.dumps({"asset": {
        "videos": [{"status": "error", "error": error, "path": path}],
    }})}]}}}
    state = {"task_id": 100, "data": {"status": "SUCCEEDED"}}
    api.request = Mock(side_effect=[files, state, history])
    return api.poll("100", "s_example")


def test_generic_artifact_failure_prefers_exact_asset_notification_cause():
    api = client()
    message = "Generated video has copyright restrictions"
    history = {"data": {"turns": [{"messages": [artifact_failure_message(message)]}]}}
    result = failed_artifact_poll(api, history)
    assert result["status"] == "failed" and result["state"] == "SUCCEEDED"
    assert result["error_code"] == "UPSTREAM_FAILED" and result["error"] == message
    assert api.request.call_count == 3
    call = api.request.call_args
    assert call.args == ("GET", GRAPH + "/message_history")
    assert call.kwargs == {"params": {"thread_id": "s_example", "limit": 5}, "timeout": (5, 10)}
    assert not any(call.args[1].endswith("/ainvoke") for call in api.request.call_args_list)


def test_history_network_failure_keeps_confirmed_artifact_failure():
    api = client()
    result = failed_artifact_poll(api, NadError("read timeout"))
    assert result["status"] == "failed" and result["error_code"] == "UPSTREAM_FAILED"
    assert result["error"] == "Artifact generation failed"
    assert api.request.call_count == 3


@pytest.mark.parametrize("history", [
    {}, {"data": {"turns": []}}, {"data": {"turns": "invalid"}},
    {"data": {"turns": [{"messages": [artifact_failure_message("Artifact generation failed")]}]}},
])
def test_history_without_specific_cause_preserves_generic_fallback(history):
    api = client()
    result = failed_artifact_poll(api, history)
    assert result["status"] == "failed" and result["error"] == "Artifact generation failed"
    assert api.request.call_count == 3


@pytest.mark.parametrize("overrides", [
    {"path": "/videos/older-unrelated-asset"}, {"thread_id": "s_other"},
    {"stream_mode": "input"}, {"metadata_state": "COMPLETED"}, {"role": "user"},
])
def test_unrelated_old_failures_and_user_content_do_not_override_current_asset(overrides):
    api = client()
    history = {"data": {"turns": [{"messages": [artifact_failure_message(**overrides)]}]}}
    result = failed_artifact_poll(api, history)
    assert result["error"] == "Artifact generation failed" and result["status"] == "failed"


def test_matching_current_failure_is_selected_instead_of_an_unrelated_older_failure():
    api = client()
    history = {"data": {"turns": [
        {"messages": [artifact_failure_message("unrelated old error", path="/videos/old")]},
        {"messages": [artifact_failure_message("current exact asset error")]},
    ]}}
    result = failed_artifact_poll(api, history)
    assert result["error"] == "current exact asset error"


@pytest.mark.parametrize("error,path", [
    ("already specific upstream failure", "/videos/asset-target"),
    ("Artifact generation failed", ""),
])
def test_specific_failures_or_missing_artifact_identity_do_not_request_history(error, path):
    api = client()
    result = failed_artifact_poll(api, {"data": {"turns": []}}, error=error, path=path)
    assert result["error"] == error and result["status"] == "failed"
    assert api.request.call_count == 2


def test_running_video_never_requests_error_history():
    api = client()
    api.request = Mock(side_effect=[
        {"data": {"files": {"ui_state.json": [{"content": json.dumps({
            "asset": {"videos": [{"status": "running", "path": "/videos/asset-target"}]},
        })}]}}}, {"task_id": 100, "data": {"status": "RUNNING"}},
    ])
    result = api.poll("100", "s_example")
    assert result["status"] == "running" and api.request.call_count == 2


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


def _assert_slots_available(semaphore, count):
    acquired = 0
    try:
        for _ in range(count):
            assert semaphore.acquire(blocking=False)
            acquired += 1
    finally:
        for _ in range(acquired):
            semaphore.release()


@pytest.fixture
def material_slots(monkeypatch):
    import app.client as module

    preparation = threading.BoundedSemaphore(4)
    uploads = threading.BoundedSemaphore(2)
    monkeypatch.setattr(module, "_MATERIAL_PREPARATION_SLOTS", preparation)
    monkeypatch.setattr(module, "_MATERIAL_UPLOAD_SLOTS", uploads)
    monkeypatch.setattr(module, "_MEDIA_CACHE_LOCKS", {})
    monkeypatch.setattr(module, "_MEDIA_SOURCE_FLIGHTS", {})
    yield preparation, uploads
    _assert_slots_available(preparation, 4)
    _assert_slots_available(uploads, 2)
    assert module._MEDIA_SOURCE_FLIGHTS == {}


def _material_client(store, account_id=1):
    return NadClient({"id": account_id, "proxy_url": f"http://127.0.0.1:{9080 + account_id}",
                      "cookie": f"session=account{account_id}", "dfp": f"device{account_id}"}, store=store)


def _material_request(*, images=(), videos=(), audios=(), family="wan"):
    return {"family": family, "images": list(images), "videos": list(videos), "audios": list(audios)}


def _material_receipt(data, kind, account_id=1):
    return {"url": f"https://material.example.test/{account_id}/{kind}/{data.hex()}",
            "kind": kind, "duration": 3, "size": len(data)}


def test_distinct_materials_for_one_account_upload_concurrently(monkeypatch, tmp_path, material_slots):
    store = Store(str(tmp_path / "cache.db"))
    clients = [_material_client(store), _material_client(store)]
    assert clients[0].session is not clients[1].session
    assert clients[0].session.cookies is not clients[1].session.cookies
    overlap = threading.Barrier(2)
    monkeypatch.setattr("app.client._download", lambda source, kind, proxy: (source.encode(), "image/png", "r.png"))

    def upload(data, mime, filename, kind, *, client):
        overlap.wait(timeout=5)
        return _material_receipt(data, kind)

    monkeypatch.setattr("app.client.upload_media_bytes", upload)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(api.upload_inputs, _material_request(images=[source]))
                   for api, source in zip(clients, ["first", "second"])]
        results = [future.result(timeout=8) for future in futures]
    assert results[0]["images"][0]["url"] != results[1]["images"][0]["url"]


def test_same_account_and_digest_share_one_completed_upload(monkeypatch, tmp_path, material_slots):
    store = Store(str(tmp_path / "cache.db"))
    clients = [_material_client(store), _material_client(store)]
    both_downloaded = threading.Barrier(2)

    def download(source, kind, proxy):
        both_downloaded.wait(timeout=5)
        return b"same image", "image/png", "r.png"

    upload = Mock(side_effect=lambda data, mime, filename, kind, **kwargs: _material_receipt(data, kind))
    monkeypatch.setattr("app.client._download", download)
    monkeypatch.setattr("app.client.upload_media_bytes", upload)
    with ThreadPoolExecutor(max_workers=2) as pool:
        # Distinct source URLs with identical bytes still share the digest cache.
        futures = [pool.submit(api.upload_inputs, _material_request(images=[f"source{index}"]))
                   for index, api in enumerate(clients)]
        results = [future.result(timeout=8) for future in futures]
    assert results[0] == results[1] and upload.call_count == 1
    monkeypatch.setattr("app.client._download", lambda *args: (b"same image", "image/png", "r.png"))
    assert clients[0].upload_inputs(_material_request(images=["source"])) == results[0]
    assert upload.call_count == 1
    # One task cannot change the cached receipt used by another task.
    results[0]["images"][0]["url"] = "changed"
    digest = hashlib.sha256(b"same image").hexdigest()
    assert store.media_cache_get(1, "images", digest)["url"].startswith("https://")


@pytest.mark.parametrize("other_account,other_kind", [(2, "images"), (1, "videos")])
def test_material_cache_never_crosses_account_or_kind(monkeypatch, tmp_path, material_slots,
                                                    other_account, other_kind):
    store = Store(str(tmp_path / "cache.db"))
    clients = [_material_client(store), _material_client(store, other_account)]
    proxies = []

    def download(source, kind, proxy):
        proxies.append(proxy)
        return b"same bytes", "image/png", "r.png"

    upload = Mock(side_effect=lambda data, mime, filename, kind, *, client:
                  _material_receipt(data, kind, client.account["id"]))
    monkeypatch.setattr("app.client._download", download)
    monkeypatch.setattr("app.client.upload_media_bytes", upload)
    first = clients[0].upload_inputs(_material_request(images=["source"]))
    second = clients[1].upload_inputs(_material_request(**{other_kind: ["source"]}))
    assert upload.call_count == 2
    assert first["images"][0]["url"] != second[other_kind][0]["url"]
    assert proxies == [api.account["proxy_url"] for api in clients]


def test_global_material_slots_bound_downloaded_payloads_and_uploads(monkeypatch, tmp_path, material_slots):
    store = Store(str(tmp_path / "cache.db"))
    ready = threading.Barrier(8)
    four_downloads, two_uploads = threading.Event(), threading.Event()
    release_downloads, release_uploads = threading.Event(), threading.Event()
    guard = threading.Lock()
    counts = {"prepared": 0, "uploading": 0, "max_prepared": 0, "max_uploading": 0, "downloads": 0}

    def download(source, kind, proxy):
        with guard:
            counts["prepared"] += 1
            counts["downloads"] += 1
            counts["max_prepared"] = max(counts["max_prepared"], counts["prepared"])
            if counts["downloads"] == 4:
                four_downloads.set()
        assert release_downloads.wait(timeout=5)
        return source.encode(), "image/png", "r.png"

    def upload(data, mime, filename, kind, *, client):
        with guard:
            counts["uploading"] += 1
            counts["max_uploading"] = max(counts["max_uploading"], counts["uploading"])
            if counts["uploading"] == 2:
                two_uploads.set()
        try:
            assert release_uploads.wait(timeout=5)
            return _material_receipt(data, kind)
        finally:
            with guard:
                counts["uploading"] -= 1
                counts["prepared"] -= 1

    def prepare(index):
        api = _material_client(store)
        ready.wait(timeout=5)
        return api.upload_inputs(_material_request(images=[str(index)]))

    monkeypatch.setattr("app.client._download", download)
    monkeypatch.setattr("app.client.upload_media_bytes", upload)
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(prepare, index) for index in range(8)]
        try:
            assert four_downloads.wait(timeout=5)
            with guard:
                assert counts["downloads"] == 4
            release_downloads.set()
            assert two_uploads.wait(timeout=5)
            with guard:
                # Two uploads and two waiting uploads still hold all preparation slots.
                assert counts["downloads"] == 4
        finally:
            release_downloads.set()
            release_uploads.set()
        assert all(future.result(timeout=8)["images"] for future in futures)
    assert counts["max_prepared"] == 4 and counts["max_uploading"] == 2
    assert counts["prepared"] == counts["uploading"] == 0


@pytest.mark.parametrize("phase", ["download", "upload", "cache"])
def test_material_failures_release_slots_and_digest_lock_without_retry(monkeypatch, tmp_path, material_slots, phase):
    store = Store(str(tmp_path / "cache.db"))
    api = _material_client(store)
    download = Mock(return_value=(b"retryable material", "image/png", "r.png"))
    upload = Mock(return_value=_material_receipt(b"retryable material", "images"))
    monkeypatch.setattr("app.client._download", download)
    monkeypatch.setattr("app.client.upload_media_bytes", upload)
    original_put = store.media_cache_put
    if phase == "download":
        download.side_effect = RuntimeError("download failed")
    elif phase == "upload":
        upload.side_effect = RuntimeError("upload failed")
    else:
        monkeypatch.setattr(store, "media_cache_put", Mock(side_effect=RuntimeError("cache failed")))
    with pytest.raises(RuntimeError, match=f"{phase} failed"):
        api.upload_inputs(_material_request(images=["source"]))
    assert upload.call_count == (0 if phase == "download" else 1)
    digest = hashlib.sha256(b"retryable material").hexdigest()
    assert store.media_cache_get(1, "images", digest) is None
    for semaphore, limit in zip(material_slots, (4, 2)):
        _assert_slots_available(semaphore, limit)
    download.side_effect = upload.side_effect = None
    monkeypatch.setattr(store, "media_cache_put", original_put)
    assert api.upload_inputs(_material_request(images=["source"]))["images"]


@pytest.mark.parametrize("receipt", [{}, {"url": ""}, {"url": "http://incomplete.example.test/a"}])
def test_incomplete_upload_receipt_is_never_cached(monkeypatch, tmp_path, material_slots, receipt):
    store = Store(str(tmp_path / "cache.db"))
    api = _material_client(store)
    monkeypatch.setattr("app.client._download", lambda *args: (b"image", "image/png", "r.png"))
    upload = Mock(return_value=receipt)
    monkeypatch.setattr("app.client.upload_media_bytes", upload)
    with pytest.raises(NadError, match="no complete material receipt"):
        api.upload_inputs(_material_request(images=["source"]))
    assert upload.call_count == 1
    assert store.media_cache_get(1, "images", hashlib.sha256(b"image").hexdigest()) is None


def test_material_order_and_compliance_are_outside_upload_slots(monkeypatch, tmp_path, material_slots):
    api = _material_client(Store(str(tmp_path / "cache.db")))
    normalized = _material_request(images=["image2", "image1"], videos=["video1"], audios=["audio1"], family="2.0")
    monkeypatch.setattr("app.client._download", lambda source, *args: (source.encode(), "image/png", "r.png"))
    monkeypatch.setattr("app.client.upload_media_bytes", lambda data, mime, filename, kind, **kwargs:
                        _material_receipt(data, kind))
    expected = [_material_receipt(source.encode(), kind)["url"] for kind in ("images", "videos", "audios")
                for source in normalized[kind]]

    def compliance(method, path, urls):
        assert method == "POST" and path == "/apis/nadouai/doubao/upload_asset" and urls == expected
        for semaphore, limit in zip(material_slots, (4, 2)):
            _assert_slots_available(semaphore, limit)
        return {"data": [{"metadata": {"doubaoAssetStatus": "Active"}} for _ in urls]}

    api.request = Mock(side_effect=compliance)
    result = api.upload_inputs(normalized)
    assert [item["url"] for kind in result for item in result[kind]] == expected
    assert api.request.call_count == 1


def _observe_source_waiters(monkeypatch, expected):
    waiting = threading.Event()
    guard = threading.Lock()
    count = 0

    class ObservedFuture(Future):
        def result(self, *args, **kwargs):
            nonlocal count
            with guard:
                count += 1
                if count == expected:
                    waiting.set()
            return super().result(*args, **kwargs)

    monkeypatch.setattr("app.client.Future", ObservedFuture)
    return waiting


def test_concurrent_same_source_downloads_and_uploads_once_without_waiter_slots(monkeypatch, tmp_path, material_slots):
    store = Store(str(tmp_path / "cache.db"))
    clients = [_material_client(store), _material_client(store)]
    owner_started, release_owner = threading.Event(), threading.Event()
    waiter_started = _observe_source_waiters(monkeypatch, 1)

    def download(*args):
        owner_started.set()
        assert release_owner.wait(timeout=5)
        return b"shared image", "image/png", "r.png"

    download_mock = Mock(side_effect=download)
    upload = Mock(side_effect=lambda data, mime, filename, kind, **kwargs: _material_receipt(data, kind))
    monkeypatch.setattr("app.client._download", download_mock)
    monkeypatch.setattr("app.client.upload_media_bytes", upload)
    request = _material_request(images=["https://source.example.test/image.png?sig=one%2Ftwo&expires=123"])
    with ThreadPoolExecutor(max_workers=2) as pool:
        owner = pool.submit(clients[0].upload_inputs, request)
        try:
            assert owner_started.wait(timeout=5)
            waiter = pool.submit(clients[1].upload_inputs, request)
            assert waiter_started.wait(timeout=5)
            _assert_slots_available(material_slots[0], 3)
            _assert_slots_available(material_slots[1], 2)
            assert download_mock.call_count == 1
        finally:
            release_owner.set()
        first, second = owner.result(timeout=8), waiter.result(timeout=8)
    assert first == second and download_mock.call_count == upload.call_count == 1
    assert first["images"][0] is not second["images"][0]
    first["images"][0]["url"] = "changed"
    assert second["images"][0]["url"].startswith("https://")


@pytest.mark.parametrize("other_account,other_kind,other_query", [
    (2, "images", "sig=one%2Ftwo&expires=123"),
    (1, "videos", "sig=one%2Ftwo&expires=123"),
    (1, "images", "sig=one%2Ftwo&expires=456"),
    (1, "images", "sig=one/two&expires=123"),
])
def test_source_flights_isolate_accounts_types_and_complete_signed_query(monkeypatch, tmp_path, material_slots,
                                                                      other_account, other_kind, other_query):
    store = Store(str(tmp_path / "cache.db"))
    clients = [_material_client(store), _material_client(store, other_account)]
    both_downloads = threading.Barrier(2)

    def download(source, kind, proxy):
        both_downloads.wait(timeout=5)
        return source.encode(), "image/png", "r.png"

    download_mock = Mock(side_effect=download)
    upload = Mock(side_effect=lambda data, mime, filename, kind, *, client:
                  _material_receipt(data, kind, client.account["id"]))
    monkeypatch.setattr("app.client._download", download_mock)
    monkeypatch.setattr("app.client.upload_media_bytes", upload)
    base = "https://source.example.test/reference?"
    requests = [_material_request(images=[base + "sig=one%2Ftwo&expires=123"]),
                _material_request(**{other_kind: [base + other_query]})]
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(api.upload_inputs, request) for api, request in zip(clients, requests)]
        results = [future.result(timeout=8) for future in futures]
    assert all(result for result in results) and download_mock.call_count == upload.call_count == 2


def test_completed_source_flight_is_not_a_cache_for_mutable_urls(monkeypatch, tmp_path, material_slots):
    api = _material_client(Store(str(tmp_path / "cache.db")))
    download = Mock(side_effect=[(b"old content", "image/png", "r.png"),
                                 (b"new content", "image/png", "r.png"),
                                 (b"new content", "image/png", "r.png")])
    upload = Mock(side_effect=lambda data, mime, filename, kind, **kwargs: _material_receipt(data, kind))
    monkeypatch.setattr("app.client._download", download)
    monkeypatch.setattr("app.client.upload_media_bytes", upload)
    request = _material_request(images=["https://source.example.test/mutable.png"])
    first, second, third = [api.upload_inputs(request) for _ in range(3)]
    assert first != second and second == third
    assert download.call_count == 3 and upload.call_count == 2


@pytest.mark.parametrize("phase", ["download", "upload", "cache"])
def test_failed_source_owner_fails_all_waiters_and_leaves_no_inflight_state(monkeypatch, tmp_path, material_slots, phase):
    store = Store(str(tmp_path / "cache.db"))
    clients = [_material_client(store) for _ in range(3)]
    owner_started, release_owner = threading.Event(), threading.Event()
    waiters_started = _observe_source_waiters(monkeypatch, 2)
    fail_owner = True
    original_put = store.media_cache_put

    def fail_in_phase(current):
        if fail_owner and phase == current:
            owner_started.set()
            assert release_owner.wait(timeout=5)
            raise RuntimeError("owner preparation failed")

    def download(*args):
        fail_in_phase("download")
        return b"shared material", "image/png", "r.png"

    def upload(data, mime, filename, kind, **kwargs):
        fail_in_phase("upload")
        return _material_receipt(data, kind)

    def put(*args):
        fail_in_phase("cache")
        return original_put(*args)

    download_mock, upload_mock = Mock(side_effect=download), Mock(side_effect=upload)
    monkeypatch.setattr("app.client._download", download_mock)
    monkeypatch.setattr("app.client.upload_media_bytes", upload_mock)
    monkeypatch.setattr(store, "media_cache_put", put)
    request = _material_request(images=["https://source.example.test/shared.png"])
    with ThreadPoolExecutor(max_workers=3) as pool:
        owner = pool.submit(clients[0].upload_inputs, request)
        try:
            assert owner_started.wait(timeout=5)
            waiters = [pool.submit(api.upload_inputs, request) for api in clients[1:]]
            assert waiters_started.wait(timeout=5)
            assert download_mock.call_count == 1
        finally:
            release_owner.set()
        for future in [owner, *waiters]:
            with pytest.raises(RuntimeError, match="owner preparation failed"):
                future.result(timeout=8)
    assert download_mock.call_count == 1
    assert upload_mock.call_count == (0 if phase == "download" else 1)
    for semaphore, limit in zip(material_slots, (4, 2)):
        _assert_slots_available(semaphore, limit)
    fail_owner = False
    assert clients[0].upload_inputs(request)["images"]
    assert download_mock.call_count == 2
