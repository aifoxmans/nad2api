import importlib
from concurrent.futures import ThreadPoolExecutor
import threading
import time

import pytest
from fastapi.testclient import TestClient

from app.browser import BrowserError, BrowserRejected
from app.service import Service
from app.store import Store


class Queue:
    def __init__(self):
        self.calls = []

    def submit(self, fn, *args):
        self.calls.append((fn, args))

    def shutdown(self, **kwargs):
        pass


class Client:
    submissions = 0
    error = None

    def __init__(self, account, *, store=None):
        self.account = account

    def account_state(self):
        return {"available_balance": 100, "buckets": {"remainPoints": 100}}

    def close(self):
        pass

    def upload_inputs(self, normalized):
        return {"images": [], "videos": [], "audios": []}

    def submit(self, normalized, assets):
        type(self).submissions += 1
        if self.error:
            raise self.error
        return {"run_id": "upstream-task", "thread_id": "account-thread"}

    def poll(self, run_id, thread_id):
        assert (run_id, thread_id) == ("upstream-task", "account-thread")
        return {"status": "succeeded", "result_urls": ["https://cdn.test/result.mp4"]}

    def daily_checkin(self):
        return {"status": "manual_required", "manual_required": True}


@pytest.fixture
def service(tmp_path, monkeypatch):
    store = Store(str(tmp_path / "nad.db"))
    account = store.upsert_account({"name": "one", "cookie": "session=private", "dfp": "device",
                                    "proxy_url": "socks5://proxy.test:1080"})
    store.update_account(account["id"], {"status": "active", "balance": 100})
    value = Service(store)
    value._pool = Queue()
    Client.submissions, Client.error = 0, None
    monkeypatch.setattr("app.service.NadClient", Client)
    yield value
    value.stop()


def test_prompt_reference_validation_defaults_to_permissive_snapshot(service):
    assert service.settings()["strict_prompt_media_references"] is False
    request = {"model": "sd-2-0-mini", "prompt": "Use image2 in the next scene",
               "image_urls": ["https://example.test/image.png"]}
    task = service.create(request)
    stored = service.store.task(task["id"])
    assert stored["status"] == "queued"
    assert stored["normalized"]["strict_prompt_media_references"] is False
    assert stored["normalized"]["prompt"] == request["prompt"]
    assert stored["request"] == request
    assert len(service._pool.calls) == 1
    assert Client.submissions == 0


@pytest.mark.parametrize("enabled", [False, True])
def test_strict_prompt_reference_setting_persists_after_service_restart(service, enabled):
    result = service.update_settings({"strict_prompt_media_references": enabled})
    assert result["strict_prompt_media_references"] is enabled
    assert service.store.get_settings()["strict_prompt_media_references"] is enabled
    restarted = Service(Store(str(service.store.path)))
    restarted._pool = Queue()
    try:
        assert restarted.settings()["strict_prompt_media_references"] is enabled
    finally:
        restarted.stop()


@pytest.mark.parametrize("invalid", ["false", "true", 0, 1, None, [], {}])
def test_strict_prompt_reference_setting_requires_json_boolean(service, invalid):
    with pytest.raises(ValueError, match="strict_prompt_media_references must be a boolean"):
        service.update_settings({"strict_prompt_media_references": invalid})
    assert service.settings()["strict_prompt_media_references"] is False
    assert "strict_prompt_media_references" not in service.store.get_settings()


def test_strict_reference_rejection_never_persists_or_queues_task(service):
    service.update_settings({"strict_prompt_media_references": True})
    request = {"model": "sd-2-0-mini", "prompt": "Use image2 in the next scene",
               "image_urls": ["https://example.test/image.png"]}
    with pytest.raises(ValueError):
        service.create(request, idempotency_key="strict-rejected")
    assert service.store.tasks() == []
    assert service._pool.calls == []
    assert service.store.idempotent_task("strict-rejected", request) is None
    assert Client.submissions == 0


def test_accepted_idempotency_precedes_changed_reference_settings(service, monkeypatch):
    request = {"model": "sd-2-0-mini", "prompt": "Use image2 in the next scene",
               "image_urls": ["https://example.test/image.png"]}
    task = service.create(request, idempotency_key="accepted-permissive")
    service.update_settings({"strict_prompt_media_references": True})
    # Recovery must not re-read settings or re-normalize an accepted request.
    def settings_must_not_be_read():
        raise AssertionError("accepted idempotency lookup must precede settings")

    monkeypatch.setattr(service, "settings", settings_must_not_be_read)
    again = service.create(request, idempotency_key="accepted-permissive")
    assert again["id"] == task["id"]
    assert len(service.store.tasks()) == len(service._pool.calls) == 1
    assert service.store.task(task["id"])["normalized"]["strict_prompt_media_references"] is False


def test_queued_tasks_keep_reference_validation_snapshot_after_settings_change(service, monkeypatch):
    observed = []

    class SnapshotClient(Client):
        def upload_inputs(self, normalized):
            observed.append((normalized["prompt"], normalized["strict_prompt_media_references"]))
            return super().upload_inputs(normalized)

    monkeypatch.setattr("app.service.NadClient", SnapshotClient)
    first = service.create({"model": "sd-2-0-mini", "prompt": "Use image2",
                            "image_urls": ["https://example.test/image.png"]})
    service.update_settings({"strict_prompt_media_references": True})
    second = service.create({"model": "sd-2-0-mini", "prompt": "Use image1",
                             "image_urls": ["https://example.test/image.png"]})
    # The permissive task runs after strict validation was enabled.
    service._run(first["id"])
    service.update_settings({"strict_prompt_media_references": False})
    # The strict task runs after the setting was disabled.
    service._run(second["id"])
    assert observed == [("Use image2", False), ("Use image1", True)]
    for task, expected in ((first, False), (second, True)):
        stored = service.store.task(task["id"])
        assert stored["status"] == "succeeded"
        assert stored["normalized"]["strict_prompt_media_references"] is expected


def test_settings_api_boolean_validation_and_reference_rejection_are_isolated(service, monkeypatch):
    monkeypatch.setenv("NAD_API_KEY", "test-api")
    monkeypatch.setenv("NAD_ADMIN_TOKEN", "test-admin")
    monkeypatch.setenv("NAD_DATA_DIR", str(service.store.path.parent / "unused-main"))
    main = importlib.import_module("app.main")
    monkeypatch.setattr(main, "store", service.store)
    monkeypatch.setattr(main, "service", service)
    monkeypatch.setattr(main, "API_KEY", "settings-api")
    monkeypatch.setattr(main, "ADMIN_TOKEN", "settings-admin")
    # Without a context manager, TestClient never enters the real lifespan or
    # starts account maintenance/workers. The fixture's Queue records dispatch.
    api = TestClient(main.app)
    admin = {"X-Admin-Token": "settings-admin"}
    caller = {"Authorization": "Bearer settings-api", "Idempotency-Key": "permissive-api-case"}
    assert api.get("/api/settings").status_code == 401
    assert api.get("/api/settings", headers=admin).json()["strict_prompt_media_references"] is False
    for headers in ({}, caller):
        unauthorized = api.patch("/api/settings", headers=headers,
                                 json={"strict_prompt_media_references": True})
        assert unauthorized.status_code == 401
        assert service.settings()["strict_prompt_media_references"] is False
    strict = api.patch("/api/settings", headers=admin,
                       json={"strict_prompt_media_references": True})
    assert strict.status_code == 200
    assert strict.json()["strict_prompt_media_references"] is True
    invalid = api.patch("/api/settings", headers=admin,
                        json={"strict_prompt_media_references": "false"})
    assert invalid.status_code == 422
    assert api.get("/api/settings", headers=admin).json()["strict_prompt_media_references"] is True
    request = {"model": "sd-2-0-mini", "prompt": "Use image2",
               "image_urls": ["https://example.test/image.png"]}
    rejected = api.post("/v1/videos", headers=caller, json=request)
    assert rejected.status_code == 422
    assert rejected.headers["X-NADAPI-Submit-Outcome"] == "not-accepted"
    assert service.store.tasks() == [] and service._pool.calls == []
    permissive = api.patch("/api/settings", headers=admin,
                           json={"strict_prompt_media_references": False})
    assert permissive.status_code == 200
    assert permissive.json()["strict_prompt_media_references"] is False
    accepted = api.post("/v1/videos", headers=caller, json=request)
    assert accepted.status_code == 200
    assert service.store.task(accepted.json()["id"])["normalized"]["strict_prompt_media_references"] is False
    assert api.patch("/api/settings", headers=admin,
                     json={"strict_prompt_media_references": True}).status_code == 200
    replay = api.post("/v1/videos", headers=caller, json=request)
    assert replay.status_code == 200 and replay.json()["id"] == accepted.json()["id"]
    assert len(service.store.tasks()) == len(service._pool.calls) == 1
    assert Client.submissions == 0


def test_cookie_only_account_executes_one_task_without_browser(service):
    task = service.create({"model": "sd-2-0-mini", "prompt": "lake", "resolution": "480p"}, idempotency_key="one")
    service._run(task["id"])
    result = service.public_task(service.store.task(task["id"]))
    assert result["status"] == "succeeded"
    assert result["data"] == [{"type": "video", "url": "https://cdn.test/result.mp4"}]
    assert result["credits_consumed"] is None
    assert result["cost_source"] == "unknown"
    assert Client.submissions == 1
    assert service.create({"model": "sd-2-0-mini", "prompt": "lake", "resolution": "480p"}, idempotency_key="one")["id"] == task["id"]
    assert len(service._pool.calls) == 1


def test_same_account_tasks_prepare_concurrently_with_distinct_generation_ids(service, monkeypatch):
    service.store.update_account(1, {"max_concurrency": 2})
    barrier = threading.Barrier(2)
    submissions = []
    guard = threading.Lock()

    class ConcurrentClient(Client):
        def upload_inputs(self, normalized):
            self.label = normalized["prompt"]
            task = next(task for task in service.store.tasks() if task["prompt"] == self.label)
            assert task["status"] == "preparing" and task["account_id"] == 1
            # The previous account-wide lock prevented a second task from
            # reaching this point until the first finished all preparation.
            barrier.wait(timeout=3)
            return {"images": [], "videos": [], "audios": []}

        def submit(self, normalized, assets):
            with guard:
                submissions.append(self.label)
            return {"run_id": "run-" + self.label, "thread_id": "thread-" + self.label}

        def poll(self, run_id, thread_id):
            assert (run_id, thread_id) == ("run-" + self.label, "thread-" + self.label)
            return {"status": "succeeded", "result_urls": ["https://cdn.test/" + self.label + ".mp4"]}

    monkeypatch.setattr("app.service.NadClient", ConcurrentClient)
    tasks = [service.create({"model": "sd-2-0-mini", "prompt": label}) for label in ("one", "two")]
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(service._run, task["id"]) for task in tasks]
        for future in futures:
            future.result(timeout=5)
    assert sorted(submissions) == ["one", "two"]
    stored = [service.store.task(task["id"]) for task in tasks]
    assert all(task["status"] == "succeeded" for task in stored)
    assert {task["run_id"] for task in stored} == {"run-one", "run-two"}
    assert {task["thread_id"] for task in stored} == {"thread-one", "thread-two"}
    assert service._active[1] == 0


@pytest.mark.parametrize("ambiguous", [False, True])
def test_duplicate_dispatch_while_still_queued_never_submits_twice(service, monkeypatch, ambiguous):
    service.store.update_account(1, {"max_concurrency": 2})
    reserved, release = threading.Event(), threading.Event()
    reserve = service._reserve_account

    def paused_reserve(*args, **kwargs):
        account = reserve(*args, **kwargs)
        if account:
            reserved.set()
            assert release.wait(timeout=3)
        return account

    monkeypatch.setattr(service, "_reserve_account", paused_reserve)
    Client.error = BrowserError("connection closed after generation POST") if ambiguous else None
    task = service.create({"model": "sd-2-0-mini", "prompt": "lake"})
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(service._run, task["id"])
        try:
            assert reserved.wait(timeout=2)
            assert service.store.task(task["id"])["status"] == "queued"
            duplicate = pool.submit(service._run, task["id"])
            duplicate.result(timeout=1)
        finally:
            release.set()
        first.result(timeout=4)
    stored = service.store.task(task["id"])
    assert Client.submissions == 1
    assert stored["status"] == ("submission_unknown" if ambiguous else "succeeded")
    if ambiguous:
        assert not stored["run_id"]
        service._run(task["id"])
        assert Client.submissions == 1
    else:
        assert (stored["run_id"], stored["thread_id"]) == ("upstream-task", "account-thread")
    assert service._active[1] == 0


def test_ambiguous_generation_is_never_replayed_or_counted_as_executing(service):
    Client.error = BrowserError("connection closed after POST")
    task = service.create({"model": "sd-2-0-mini", "prompt": "lake"})
    service._run(task["id"])
    stored = service.store.task(task["id"])
    assert stored["status"] == "submission_unknown"
    assert service.public_task(stored)["error"]["category"] == "submission_unknown"
    service._run(task["id"])
    assert Client.submissions == 1
    assert service.store.active_account_count(1) == 0
    assert service._active[1] == 0


def test_explicit_rejection_is_terminal_without_ambiguous_state(service):
    Client.error = BrowserRejected("Nadou rejected unsupported reference")
    task = service.create({"model": "sd-2-0-mini", "prompt": "lake"})
    service._run(task["id"])
    assert service.store.task(task["id"])["status"] == "failed"
    assert Client.submissions == 1


def test_manual_reward_does_not_mark_daily_claim_complete(service):
    result = service.checkin_account(1)
    assert result["manual_required"]
    assert service.store.account(1)["last_checkin_date"] == ""


def test_restart_requeues_only_upload_phase_not_generation_boundary(service, monkeypatch):
    body = {"model": "sd-2-0-mini", "prompt": "lake"}
    first, second = service.create(body), service.create(body)
    service.store.update_task(first["id"], {"status": "preparing", "progress": 12})
    service.store.update_task(second["id"], {"status": "preparing", "progress": 30})
    service._pool.calls.clear()
    monkeypatch.setattr(service, "_maintain_accounts", lambda: None)
    service.start()
    assert service.store.task(first["id"])["status"] == "queued"
    assert service.store.task(second["id"])["status"] == "submission_unknown"
    assert len(service._pool.calls) == 1
    assert service._pool.calls[0][1] == (first["id"],)


def test_poll_network_loss_preserves_accepted_task(service, monkeypatch):
    task = service.create({"model": "sd-2-0-mini", "prompt": "lake"})
    service.store.update_task(task["id"], {"status": "running", "run_id": "upstream-task", "thread_id": "thread", "account_id": 1})

    class Offline:
        def poll(self, *_):
            raise BrowserError("network unavailable")

    monkeypatch.setattr("app.service.time.sleep", lambda _: None)
    service._poll(task["id"], Offline(), time.monotonic() + 100)
    stored = service.store.task(task["id"])
    assert stored["status"] == "submission_unknown"
    assert stored["run_id"] == "upstream-task"


def test_poll_deadline_keeps_confirmed_ids_and_resume_only_polls(service):
    task = service.create({"model": "sd-2-0-mini", "prompt": "lake"})
    service.store.update_task(task["id"], {"status": "running", "run_id": "upstream-task",
                                         "thread_id": "account-thread", "account_id": 1})
    service._poll(task["id"], Client({"id": 1}), time.monotonic() - 1)
    stored = service.store.task(task["id"])
    assert stored["status"] == "submission_unknown"
    assert stored["run_id"] == "upstream-task"
    result = service.resume(task["id"])
    assert result["status"] == "submitted"
    assert service._pool.calls[-1][0] == service._resume_poll
    service._resume_poll(task["id"])
    assert service.store.task(task["id"])["status"] == "succeeded"
    assert Client.submissions == 0


def test_resume_rejects_missing_upstream_identity_and_duplicate_resume(service):
    task = service.create({"model": "sd-2-0-mini", "prompt": "lake"})
    service._fail(task["id"], "UPSTREAM_SUBMIT_OUTCOME_UNKNOWN", "network unavailable")
    with pytest.raises(ValueError, match="no confirmed"):
        service.resume(task["id"])
    service.store.update_task(task["id"], {"run_id": "upstream-task", "thread_id": "account-thread", "account_id": 1})
    service.resume(task["id"])
    with pytest.raises(ValueError, match="only interrupted"):
        service.resume(task["id"])


def test_api_auth_not_accepted_header_and_idempotency(tmp_path, monkeypatch):
    monkeypatch.setenv("NAD_API_KEY", "test-api")
    monkeypatch.setenv("NAD_ADMIN_TOKEN", "test-admin")
    monkeypatch.setenv("NAD_DATA_DIR", str(tmp_path))
    main = importlib.import_module("app.main")
    main.service._pool = Queue()
    api = TestClient(main.app)
    assert api.get("/health").json()["models"] == 17
    assert api.get("/api/accounts").status_code == 401
    headers = {"Authorization": "Bearer test-api", "Idempotency-Key": "task-one"}
    request = {"model": "sd-2-0-mini", "prompt": "lake"}
    rejected = api.post("/v1/videos", json=request, headers=headers)
    assert rejected.status_code == 503
    assert rejected.headers["X-NADAPI-Submit-Outcome"] == "not-accepted"
    account = main.store.upsert_account({"name": "one", "cookie": "private", "dfp": "device",
                                         "proxy_url": "socks5://proxy.test:1080"})
    main.store.update_account(account["id"], {"status": "active"})
    first = api.post("/v1/videos", json=request, headers=headers)
    second = api.post("/api/v3/contents/generations/tasks", json=request, headers=headers)
    assert first.status_code == second.status_code == 200
    assert first.json()["id"] == second.json()["id"]
    conflict = api.post("/v1/videos", json={**request, "prompt": "different"}, headers=headers)
    assert conflict.status_code == 409
    accounts = api.get("/api/accounts", headers={"X-Admin-Token": "test-admin"}).json()
    assert not {"cookie", "dfp", "user_agent"} & accounts[0].keys()
    main.store.update_task(first.json()["id"], {"account_id": account["id"], "status": "submission_unknown"})
    protected = api.patch(f"/api/accounts/{account['id']}", json={"cookie": "different-session"},
                          headers={"X-Admin-Token": "test-admin"})
    assert protected.status_code == 409
    assert main.store.account(account["id"], private=True)["cookie"] == "private"
    main.service.stop()
