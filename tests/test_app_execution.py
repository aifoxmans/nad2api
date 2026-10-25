import importlib
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
