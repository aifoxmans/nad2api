"""Account-aware queue and durable Nadou video task execution."""

from __future__ import annotations

from .cost_adapter import build_observations, input_evidence, cost_values

import os
import logging
import statistics
import threading
import time
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from .browser import BrowserError, BrowserRejected
from .browser_runtime import BrowserRuntime
from .catalog import normalize_request
from .client import NadClient, NadError, account_media_lock
from .errors import public_error
from .media import MediaError
from .store import Store


TERMINAL = {"succeeded", "failed", "expired"}
MAX_CREDIT_ACCOUNT_ATTEMPTS = 2
logger = logging.getLogger(__name__)


class AccountCreditRejected(Exception):
    """The upstream explicitly rejected this account after a terminal result."""


class AccountPoolUnavailable(Exception):
    """No enabled account can accept a new task before upstream submission."""


def _credit_rejected(message: str) -> bool:
    value = message.lower()
    return any(token in value for token in (
        "credit not enough", "credits not enough", "not enough credits",
        "insufficient credits", "insufficient balance"))


def _estimate_cost(normalized: dict[str, Any], costs: list[dict[str, Any]]) -> float:
    """Use observed credits per second when exact media counts have no quote.

    The fallback is limited to observations with the same model, resolution,
    video count and audio count. It applies only when several durations show a
    stable rate, so image-count variants cannot invent a price from one task.
    """
    comparable = [item for item in costs if
                  item["model"] == normalized["model"] and
                  item["resolution"] == normalized["resolution"] and
                  item["video_count"] == len(normalized["videos"]) and
                  item["audio_count"] == len(normalized["audios"])]
    exact = [item for item in comparable if
             item["duration"] == normalized["duration"] and
             item["image_count"] == len(normalized["images"])]
    if exact:
        return max(float(item["cost"]) for item in exact)
    observations = [item for item in comparable if
                    float(item["cost"]) > 0 and int(item["duration"]) > 0]
    if len(observations) < 3 or len({item["duration"] for item in observations}) < 2:
        return 0.0
    rates = [float(item["cost"]) / int(item["duration"]) for item in observations]
    rate = statistics.median(rates)
    if any(abs(sample - rate) > rate * 0.05 for sample in rates):
        return 0.0
    return round(rate * int(normalized["duration"]), 2)


DEFAULT_SETTINGS: dict[str, Any] = {
    "task_workers": 4,
    "task_queue_capacity": 100,
    "poll_interval_seconds": 8,
    "task_timeout_seconds": 1800,
    "account_maintenance_interval_seconds": 300,
    "daily_checkin_enabled": False,
    "chrome_executable": os.getenv("NAD_CHROME_EXECUTABLE", "/usr/bin/chromium" if os.name != "nt" else "chrome.exe"),
    "chrome_user_data_root": os.getenv("NAD_CHROME_USER_DATA_ROOT", "/app/data/chrome-profiles"),
    "low_balance_disable_threshold": 0,
    "allow_video_reference_inputs": True,
    "excess_media_policy": "reject",
    "prompt_media_reference_cleanup_enabled": True,
}


class Service:
    def __init__(self, store: Store):
        self.store = store
        self._pool = ThreadPoolExecutor(max_workers=50, thread_name_prefix="nad-task")
        self._lock = threading.RLock()
        self._active: dict[int, int] = {}
        self._claimed_tasks: set[str] = set()
        self._worker_slots = threading.BoundedSemaphore(50)
        self._started = False
        self._stop = threading.Event()
        self._maintenance: threading.Thread | None = None
        self.browser_runtime = BrowserRuntime(store, self.settings)
        self._browser_views: dict[int, tuple[dict[str, Any], bool, float]] = {}
        self._browser_views_lock = threading.RLock()

    def settings(self) -> dict[str, Any]:
        return {**DEFAULT_SETTINGS, **self.store.get_settings()}

    def update_settings(self, values: dict[str, Any]) -> dict[str, Any]:
        allowed = set(DEFAULT_SETTINGS)
        filtered = {key: value for key, value in values.items() if key in allowed}
        limits = {"task_workers": (1, 50), "task_queue_capacity": (0, 10000),
                  "poll_interval_seconds": (2, 120), "task_timeout_seconds": (60, 7200),
                  "account_maintenance_interval_seconds": (30, 86400)}
        for key, (minimum, maximum) in limits.items():
            if key in filtered:
                if isinstance(filtered[key], bool) or not minimum <= int(filtered[key]) <= maximum:
                    raise ValueError(f"invalid {key}")
                filtered[key] = int(filtered[key])
        self.store.update_settings(filtered)
        return self.settings()

    def start(self) -> None:
        self._started = True
        self._maintenance = threading.Thread(target=self._maintain_accounts, daemon=True,
                                             name="nad-account-maintenance")
        self._maintenance.start()
        for task in reversed(self.store.tasks(1000)):
            if task["status"] == "queued":
                self._pool.submit(self._run, task["id"])
            elif task["status"] in {"submitted", "running"} and task["run_id"] and task["thread_id"]:
                self._pool.submit(self._resume_poll, task["id"])
            elif task["status"] == "preparing":
                if int(task["progress"]) < 30 and not task["run_id"]:
                    # Reference upload has not reached the generation RPC. It is
                    # safe to repeat after a restart, including an interrupted
                    # browser upload; the external task ID stays the same.
                    self.store.update_task(task["id"], {"status": "queued", "progress": 0,
                        "account_id": None, "error_code": "", "error_message": ""})
                    self._pool.submit(self._run, task["id"])
                else:
                    self.store.update_task(task["id"], {"status": "submission_unknown", "progress": 35,
                        "error_code": "UPSTREAM_SUBMIT_OUTCOME_UNKNOWN",
                        "error_message": "service restarted during upstream submission; automatic resubmission is disabled",
                        "completed_at": time.time()})

    def stop(self) -> None:
        self._stop.set()
        if self._maintenance and self._maintenance.is_alive():
            self._maintenance.join(timeout=5)
        self._pool.shutdown(wait=False, cancel_futures=False)
        for account_id in list(self._browser_views):
            self.close_browser_view(account_id)
        self.browser_runtime.close_all_idle()

    def open_browser_view(self, account_id: int) -> dict[str, Any]:
        with self._browser_views_lock:
            if account_id in self._browser_views:
                account, owned, _ = self._browser_views[account_id]
                self._browser_views[account_id] = (account, owned, time.monotonic())
                return account
            account = self.store.account(account_id, private=True)
            if not account:
                raise KeyError(account_id)
            current, owned = self.browser_runtime.acquire(account)
            self._browser_views[account_id] = (current, owned, time.monotonic())
            return current

    def browser_view(self, account_id: int) -> dict[str, Any]:
        with self._browser_views_lock:
            if account_id not in self._browser_views:
                raise BrowserError("account browser view is closed")
            account, owned, _ = self._browser_views[account_id]
            self._browser_views[account_id] = (account, owned, time.monotonic())
            return account

    def close_browser_view(self, account_id: int) -> None:
        with self._browser_views_lock:
            entry = self._browser_views.pop(account_id, None)
        if entry:
            self.browser_runtime.release(entry[0], entry[1])

    def _maintain_accounts(self) -> None:
        while not self._stop.is_set():
            with self._browser_views_lock:
                expired = [account_id for account_id, (_, _, touched) in self._browser_views.items()
                           if time.monotonic() - touched > 20 * 60]
            for account_id in expired:
                self.close_browser_view(account_id)
            settings = self.settings()
            needs_rapid_recheck = False
            for account in self.store.accounts(private=True):
                if self._stop.is_set():
                    break
                if not account["enabled"] or not (int(account["cdp_port"] or 0) or account.get("cookie")):
                    continue
                try:
                    checked = self.check_account(int(account["id"]))
                    if checked["status"] == "login_required":
                        needs_rapid_recheck = True
                    if checked["status"] == "active" and settings["daily_checkin_enabled"]:
                        self.checkin_account(int(account["id"]))
                except (BrowserError, NadError, ValueError, KeyError):
                    needs_rapid_recheck = True
                    continue
            interval = max(30, int(settings["account_maintenance_interval_seconds"]))
            # A restarted Chrome often exposes its tab before the app can make
            # authenticated requests. Retry that state promptly instead of
            # leaving a valid account unavailable for the full interval.
            self._stop.wait(min(15, interval) if needs_rapid_recheck else interval)

    def checkin_account(self, account_id: int) -> dict[str, Any]:
        account = self.store.account(account_id, private=True)
        if not account:
            raise KeyError(account_id)
        today = datetime.now(ZoneInfo(os.getenv("NAD_TIME_ZONE", "Asia/Taipei"))).date().isoformat()
        if account.get("last_checkin_date") == today:
            return {"already_checked_in": True, "date": today}
        account, owned = self.browser_runtime.acquire(account)
        client = None
        try:
            client = NadClient(account, store=self.store)
            result = client.daily_checkin()
        finally:
            if client is not None:
                client.close()
            self.browser_runtime.release(account, owned)
        if result.get("supported") is False or result.get("success") is False or result.get("manual_required") or result.get("status") not in {"claimed", "already_claimed"}:
            return {**result, "date": today}
        self.store.update_account(account_id, {"last_checkin_date": today})
        return {"already_checked_in": False, "date": today,
                "received": result.get("receive_quota", 0)}

    def create(self, request: dict[str, Any], *, idempotency_key: str = "") -> dict[str, Any]:
        normalized = normalize_request(request)
        if idempotency_key:
            existing = self.store.idempotent_task(idempotency_key, request)
            if existing:
                return self.public_task(existing)
        settings = self.settings()
        if not bool(settings["allow_video_reference_inputs"]) and normalized["videos"]:
            raise ValueError("video reference inputs are disabled by service settings")
        estimated_cost = _estimate_cost(normalized, self.store.model_costs())
        with self._lock:
            # Idempotent recovery precedes capacity checks, including a full queue.
            if idempotency_key:
                existing = self.store.idempotent_task(idempotency_key, request)
                if existing:
                    return self.public_task(existing)
            if not self._has_eligible_account(estimated_cost, set()):
                raise AccountPoolUnavailable("no available Nadou account")
            if self.store.active_count() >= int(settings["task_workers"]) + int(settings["task_queue_capacity"]):
                raise RuntimeError("NADAPI queue is full")
            task_id = "nad_" + uuid.uuid4().hex[:16]
            task = self.store.create_task(task_id, request, normalized, idempotency_key=idempotency_key)
        if task["id"] != task_id:
            return self.public_task(task)
        if estimated_cost:
            task = self.store.update_task(task_id, {"estimated_cost": estimated_cost})
        self._pool.submit(self._run, task_id)
        return self.public_task(task)

    def _reserve_account(self, estimated_cost: float = 0, *,
                         exclude_ids: set[int] | None = None) -> dict[str, Any] | None:
        excluded = exclude_ids or set()
        with self._lock:
            if sum(self._active.values()) >= int(self.settings()["task_workers"]):
                return None
            accounts = [item for item in self.store.accounts(private=True)
                        if int(item["id"]) not in excluded and item["enabled"]
                        and item["status"] == "active" and item["proxy_url"]
                        and (item["balance"] is None or float(item["balance"]) >= estimated_cost)]
            accounts.sort(key=lambda item: (-int(item["priority"]), -(float(item["balance"] or 0)), item["id"]))
            for account in accounts:
                account_id = int(account["id"])
                active = self._active.get(account_id, 0)
                if active >= int(account["max_concurrency"]):
                    continue
                self._active[account_id] = active + 1
                return account
        return None

    def _has_eligible_account(self, estimated_cost: float, excluded: set[int]) -> bool:
        return any(int(item["id"]) not in excluded and item["enabled"]
                   and item["status"] == "active" and item["proxy_url"]
                   and (item["balance"] is None or float(item["balance"]) >= estimated_cost)
                   for item in self.store.accounts(private=True))

    def _release_account(self, account_id: int) -> None:
        with self._lock:
            self._active[account_id] = max(self._active.get(account_id, 1) - 1, 0)

    def check_account(self, account_id: int, *, start_browser: bool = False) -> dict[str, Any]:
        account = self.store.account(account_id, private=True)
        if not account:
            raise KeyError(account_id)
        account, owned = self.browser_runtime.acquire(account, force_managed=start_browser)
        try:
            return self._check_account_live(account)
        finally:
            self.browser_runtime.release(account, owned)

    def _check_account_live(self, account: dict[str, Any]) -> dict[str, Any]:
        account_id = int(account["id"])
        # Chrome exposes a CDP page before Nadou's JavaScript has finished
        # loading. After a service restart, one early fetch failure should not
        # put an otherwise valid account into login_required for an entire
        # maintenance interval. Complete HTTP rejections still fail promptly.
        for attempt in range(3):
            client = None
            try:
                client = NadClient(account, store=self.store)
                state = client.account_state()
                break
            except BrowserRejected as exc:
                return self.store.update_account(account_id, {"status": "login_required", "last_error": str(exc)[:300]})
            except BrowserError as exc:
                if attempt == 2:
                    return self.store.update_account(account_id, {"status": "login_required", "last_error": str(exc)[:300]})
                time.sleep(1)
            finally:
                if client is not None:
                    client.close()
        balance = state["available_balance"]
        threshold = float(self.settings()["low_balance_disable_threshold"] or 0)
        values: dict[str, Any] = {"balance": balance, "balance_details": state["buckets"],
                                  "status": "active", "last_error": ""}
        if threshold > 0 and balance is not None and balance < threshold:
            values.update({"enabled": False, "status": "disabled_low_balance",
                           "last_error": f"credit balance below {threshold}"})
        return self.store.update_account(account_id, values)

    def _run(self, task_id: str) -> None:
        # Multiple dispatcher/recovery calls must never turn one queued task
        # into concurrent generation requests, even while preparation overlaps.
        with self._lock:
            task = self.store.task(task_id)
            if not task or task["status"] != "queued" or task_id in self._claimed_tasks:
                return
            self._claimed_tasks.add(task_id)
        try:
            self._run_claimed(task_id)
        finally:
            with self._lock:
                self._claimed_tasks.discard(task_id)

    def _run_claimed(self, task_id: str) -> None:
        task = self.store.task(task_id)
        if not task or task["status"] != "queued":
            return
        estimated_cost = float(task.get("estimated_cost") or 0)
        if estimated_cost <= 0:
            estimated_cost = _estimate_cost(task["normalized"], self.store.model_costs())
            if estimated_cost > 0:
                self.store.update_task(task_id, {"estimated_cost": estimated_cost})
        attempted = list(task["attempted_account_ids"])
        credit_reason = task.get("error_message") or "Nadou account has insufficient credits"
        unavailable_code = "INSUFFICIENT_CREDITS" if attempted else "ACCOUNT_POOL_UNAVAILABLE"
        unavailable_message = credit_reason if attempted else "no available Nadou account"
        if not self._has_eligible_account(estimated_cost, set(attempted)):
            self._fail(task_id, unavailable_code, unavailable_message)
            return
        # The executor is only a waiter/dispatcher.  The configurable limit
        # applies to actual upstream work, not to the number of Python threads.
        while not self._stop.is_set():
            current = self.store.task(task_id)
            if not current or current["status"] != "queued":
                return
            if not self._has_eligible_account(estimated_cost, set(attempted)):
                self._fail(task_id, unavailable_code, unavailable_message)
                return
            if not self._worker_slots.acquire(timeout=1):
                continue
            with self._lock:
                if sum(self._active.values()) < int(self.settings()["task_workers"]):
                    break
            self._worker_slots.release()
            time.sleep(1)
        else:
            return
        try:
            while not self._stop.is_set():
                current = self.store.task(task_id)
                if not current or current["status"] != "queued":
                    return
                if len(attempted) >= MAX_CREDIT_ACCOUNT_ATTEMPTS:
                    self._fail(task_id, "INSUFFICIENT_CREDITS", credit_reason)
                    return
                account = None
                account_deadline = time.monotonic() + int(self.settings()["task_timeout_seconds"])
                while not self._stop.is_set() and time.monotonic() < account_deadline:
                    account = self._reserve_account(estimated_cost, exclude_ids=set(attempted))
                    if account or not self._has_eligible_account(
                            estimated_cost, set(attempted)):
                        break
                    self._stop.wait(1)
                if not account:
                    code = "INSUFFICIENT_CREDITS" if attempted else "ACCOUNT_POOL_UNAVAILABLE"
                    message = credit_reason if attempted else "no available Nadou account"
                    self._fail(task_id, code, message)
                    return
                account_id = int(account["id"])
                owned = False
                client = None
                try:
                    if self._stop.is_set():
                        return
                    self.store.update_task(task_id, {"account_id": account_id,
                        "status": "preparing", "progress": 5, "balance_before": None,
                        "run_id": "", "thread_id": "", "error_code": "", "error_message": "",
                        "upstream_request": {}})
                    # Only profile/auth acquisition is account-serialized. Each
                    # task then uses its own client/session for preparation and submit.
                    with account_media_lock(account_id):
                        account, owned = self.browser_runtime.acquire(account)
                        client = NadClient(account, store=self.store)
                    # A just-started Chrome can expose CDP before the app is ready.
                    for attempt in range(12):
                        try:
                            before = client.account_state()["available_balance"]
                            break
                        except BrowserError as exc:
                            if "JavaScript request failed" not in str(exc) or attempt == 11:
                                raise
                            if self._stop.wait(2):
                                return
                    self.store.update_account(account_id, {"balance": before, "status": "active"})
                    self.store.update_task(task_id, {"balance_before": before, "progress": 12})
                    assets = client.upload_inputs(task["normalized"])
                    self.store.update_task(task_id, {"cost_evidence": input_evidence(assets)})
                    self.store.update_task(task_id, {"progress": 30})
                    attempted.append(account_id)
                    self.store.update_task(task_id, {"attempted_account_ids": attempted})
                    try:
                        submitted = client.submit(task["normalized"], assets)
                    except BrowserRejected as exc:
                        if not _credit_rejected(str(exc)):
                            self._fail(task_id, "UPSTREAM_REJECTED", str(exc))
                            return
                        raise AccountCreditRejected(str(exc)) from exc
                    except BrowserError as exc:
                        self._fail(task_id, "UPSTREAM_SUBMIT_OUTCOME_UNKNOWN",
                                   str(exc) + "; automatic resubmission is disabled")
                        return
                    finally:
                        audit = getattr(client, "last_submit_request", None)
                        if isinstance(audit, dict) and audit:
                            self.store.update_task(task_id, {"upstream_request": audit,
                                "thread_id": str((audit.get("query") or {}).get("thread_id") or "")})
                    self.store.update_task(task_id, {**cost_values(submitted), "status": "submitted", "progress": 40,
                                                  "upstream_request": submitted.get("upstream_request") or {},
                                                  "run_id": submitted["run_id"],
                                                  "thread_id": submitted["thread_id"]})
                    self._poll(task_id, client,
                               time.monotonic() + int(self.settings()["task_timeout_seconds"]))
                    return
                except AccountCreditRejected as exc:
                    credit_reason = str(exc)
                    self.store.update_account(account_id, {"last_error": credit_reason[:300]})
                    logger.warning("NAD task %s credit rejection on account %s (attempt %s/%s)",
                                   task_id, account_id, len(attempted), MAX_CREDIT_ACCOUNT_ATTEMPTS)
                    if len(attempted) >= MAX_CREDIT_ACCOUNT_ATTEMPTS:
                        self._fail(task_id, "INSUFFICIENT_CREDITS", credit_reason)
                        return
                    self.store.update_task(task_id, {"status": "queued", "progress": 0,
                        "account_id": None, "run_id": "", "thread_id": "",
                        "balance_before": None, "error_code": "INSUFFICIENT_CREDITS",
                        "error_message": credit_reason})
                    continue
                except (BrowserError, NadError, MediaError, ValueError) as exc:
                    self._fail(task_id, type(exc).__name__.upper(), str(exc))
                    return
                except Exception as exc:
                    self._fail(task_id, "INTERNAL_ERROR", f"{type(exc).__name__}: {exc}")
                    return
                finally:
                    if client is not None:
                        client.close()
                    self.browser_runtime.release(account, owned)
                    self._release_account(account_id)
        finally:
            self._worker_slots.release()

    def _resume_poll(self, task_id: str) -> None:
        task = self.store.task(task_id)
        if not task or not task["account_id"]:
            return
        account = self.store.account(task["account_id"], private=True)
        if not account:
            self._fail(task_id, "ACCOUNT_MISSING", "task account was removed")
            return
        self._worker_slots.acquire()
        with self._lock:
            self._active[int(task["account_id"])] = self._active.get(int(task["account_id"]), 0) + 1
        owned = False
        client = None
        retry_on_other_account = False
        try:
            account, owned = self.browser_runtime.acquire(account)
            client = NadClient(account, store=self.store)
            self._poll(task_id, client,
                       time.monotonic() + int(self.settings()["task_timeout_seconds"]))
        except AccountCreditRejected as exc:
            attempted = list(task["attempted_account_ids"])
            if int(task["account_id"]) not in attempted:
                attempted.append(int(task["account_id"]))
            reason = str(exc)
            self.store.update_account(int(task["account_id"]), {"last_error": reason[:300]})
            if len(attempted) >= MAX_CREDIT_ACCOUNT_ATTEMPTS:
                self._fail(task_id, "INSUFFICIENT_CREDITS", reason)
            else:
                self.store.update_task(task_id, {"status": "queued", "progress": 0,
                    "account_id": None, "run_id": "", "thread_id": "",
                    "attempted_account_ids": attempted, "balance_before": None,
                    "error_code": "INSUFFICIENT_CREDITS", "error_message": reason})
                retry_on_other_account = True
        except Exception as exc:
            self._fail(task_id, type(exc).__name__.upper(), str(exc))
        finally:
            if client is not None:
                client.close()
            self.browser_runtime.release(account, owned)
            self._release_account(int(task["account_id"]))
            self._worker_slots.release()
        if retry_on_other_account and not self._stop.is_set():
            self._pool.submit(self._run, task_id)

    def resume(self, task_id: str) -> dict[str, Any]:
        """Resume queries for a confirmed upstream job; never repeat generation."""
        with self._lock:
            task = self.store.task(task_id)
            if not task:
                raise KeyError(task_id)
            if task["status"] not in {"submission_unknown", "expired"}:
                raise ValueError("only interrupted task queries can be resumed")
            if not task.get("run_id") or not task.get("thread_id") or not task.get("account_id"):
                raise ValueError("no confirmed upstream task ID; inspect the original account before retrying")
            account = self.store.account(int(task["account_id"]), private=True)
            if not account:
                raise ValueError("the original account no longer exists")
            task = self.store.update_task(task_id, {"status": "submitted", "progress": 40,
                "completed_at": None, "error_code": "", "error_message": ""})
            self._pool.submit(self._resume_poll, task_id)
            return self.public_task(task)

    def _poll(self, task_id: str, client: NadClient, deadline: float) -> None:
        errors = 0
        while time.monotonic() < deadline and not self._stop.is_set():
            task = self.store.task(task_id)
            if not task or task["status"] in TERMINAL:
                return
            try:
                state = client.poll(task["run_id"], task["thread_id"])
                errors = 0
            except (BrowserError, NadError):
                errors += 1
                if errors >= 8:
                    self._fail(task_id, "UPSTREAM_SUBMIT_OUTCOME_UNKNOWN",
                               "upstream polling unavailable; the accepted task may still finish")
                    return
                time.sleep(min(60, 2 ** errors))
                continue
            if state["status"] == "succeeded":
                if not state["result_urls"]:
                    errors += 1
                    if errors < 4:
                        time.sleep(3)
                        continue
                    self._fail(task_id, "RESULT_MISSING", "Nadou completed without a video URL")
                    return
                after = None
                try:
                    after = client.account_state()["available_balance"]
                    self.store.update_account(int(task["account_id"]), {"balance": after})
                except BrowserError:
                    pass
                self.store.update_task(task_id, {"status": "succeeded", "progress": 100,
                    "result_urls": state["result_urls"], "balance_after": after,
                    "completed_at": time.time(), **cost_values(state)})
                return
            if state["status"] == "failed":
                reason = state["error"] or "Nadou video generation failed"
                if _credit_rejected(reason):
                    raise AccountCreditRejected(reason)
                code = state.get("error_code") or "UPSTREAM_FAILED"
                self.store.update_task(task_id, cost_values(state))
                self._fail(task_id, code, reason)
                return
            self.store.update_task(task_id, {"status": "running", "progress": max(45, task["progress"])})
            time.sleep(max(2, int(self.settings()["poll_interval_seconds"])))
        if not self._stop.is_set():
            self._fail(task_id, "UPSTREAM_SUBMIT_OUTCOME_UNKNOWN",
                       "polling deadline reached; the accepted upstream task may continue; resume queries only")

    def _fail(self, task_id: str, code: str, message: str) -> None:
        current = self.store.task(task_id) or {}
        if current.get("run_id") and code in {"INTERNAL_ERROR", "BROWSERERROR", "NADERROR", "VALUEERROR", "RESULT_MISSING"}:
            code = "UPSTREAM_SUBMIT_OUTCOME_UNKNOWN"
        unknown = code == "UPSTREAM_SUBMIT_OUTCOME_UNKNOWN"
        self.store.update_task(task_id, {"status": "submission_unknown" if unknown else "failed", "progress": 35 if unknown else 100,
            "error_code": code, "error_message": message[:600], "completed_at": time.time()})

    @staticmethod
    def public_task(task: dict[str, Any]) -> dict[str, Any]:
        result = {"id": task["id"], "object": "video", "created_at": task["created_at"],
                  "status": task["status"], "model": task["model"], "progress": task["progress"],
                  "data": [{"type": "video", "url": url} for url in task["result_urls"]],
                  "account_id": task.get("account_id")}
        rows = build_observations(task)
        row = rows[-1] if rows else {}
        result["credits_consumed"] = row.get("charged") if row.get("charged") is not None else row.get("quoted")
        result["cost_source"] = row.get("source", "unknown")
        if task["status"] in {"failed", "expired", "submission_unknown"}:
            result["error"] = public_error(task["error_code"], task["error_message"])
        return result
