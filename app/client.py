"""Authenticated Nadou web protocol; generation requests are never retried."""

from __future__ import annotations

import hashlib
import json
import secrets
import threading
import time
from typing import Any
from urllib.parse import urlsplit

import requests

from .browser import BASE, BrowserError, BrowserRejected, CDPBrowser, account_proxy_url
from .catalog import bind_prompt
from .media import _download, upload_media_bytes, validate_material_durations
from .store import Store


GRAPH = "/apis/graph/screenplay2drama/nadou_agent/graph"
_MEDIA_LOCKS_GUARD = threading.RLock()
_MEDIA_LOCKS: dict[int, threading.RLock] = {}
_MEDIA_CACHE_LOCKS_GUARD = threading.Lock()
_MEDIA_CACHE_LOCKS: dict[tuple[int, str, str], threading.RLock] = {}
# Bound downloaded payloads separately from the actual upstream upload calls.
# These slots are shared by all task clients; each client keeps its own session.
_MATERIAL_PREPARATION_SLOTS = threading.BoundedSemaphore(4)
_MATERIAL_UPLOAD_SLOTS = threading.BoundedSemaphore(2)


def account_media_lock(account_id: int) -> threading.RLock:
    with _MEDIA_LOCKS_GUARD:
        return _MEDIA_LOCKS.setdefault(account_id, threading.RLock())


def _media_cache_lock(account_id: int, kind: str, digest: str) -> threading.RLock:
    with _MEDIA_CACHE_LOCKS_GUARD:
        return _MEDIA_CACHE_LOCKS.setdefault((account_id, kind, digest), threading.RLock())


def _complete_media_receipt(receipt: Any) -> bool:
    # upload_media_bytes returns only after all chunks and metadata are confirmed.
    # Incomplete responses must never be reused by another task.
    return isinstance(receipt, dict) and isinstance(receipt.get("url"), str) and receipt["url"].startswith("https://")


class NadError(BrowserError):
    pass


def _attachment(item: dict[str, Any]) -> dict[str, Any]:
    return {key: item.get(key, default) for key, default in {
        "name": "reference", "url": "", "thumbnailUrl": "", "mediumThumbnailUrl": "",
        "width": 0, "height": 0, "frameRate": 0, "withAudio": False, "duration": 0, "size": 0,
    }.items()}


def build_video_param(normalized: dict[str, Any], assets: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    prompt = bind_prompt(normalized["prompt"], assets, sources=normalized)
    ordered = [item for kind in ("images", "videos", "audios") for item in assets[kind]]
    params: dict[str, Any] = {
        "description": prompt, "image": [item["url"] for item in ordered],
        "model": normalized["upstream_model"], "ratio": normalized["aspect_ratio"],
        "resolution": normalized["resolution"].replace("4k", "4K").replace("2k", "2K"),
        "duration": str(normalized["duration"]), "generateCount": "1",
    }
    if normalized.get("family") not in {"kling", "minimax"}:
        params["generateAudio"] = normalized.get("generate_audio", True)
    return params


def build_submit_body(param: dict[str, Any], assets: dict[str, list[dict[str, Any]]], *, mode: str) -> dict[str, Any]:
    attachments = [_attachment(item) for kind in ("images", "videos", "audios") for item in assets[kind]]
    task_type = "TEXT2VIDEO" if not attachments else "IMG2VIDEO" if mode in {"i2v", "i2v_first_last"} else "MULTI_REF_IMG2VIDEO"
    return {"input": {
        "content": param["description"], "style": {}, "intention": "user-pick-params", "source": "input",
        "attachments": attachments,
        "userParams": {"taskType": task_type, "generateCount": "1", "modelName": param["model"],
                       "modelType": "video", "params": param},
    }, "bt_extra_body": {"contextvars": {"lang": "zh-CN"}}}


def extract_result_urls(value: dict[str, Any]) -> list[str]:
    """Only output videos in ui_state.json are results; never return input media."""
    urls = []
    for item in (value.get("asset") or {}).get("videos") or []:
        if isinstance(item, dict) and item.get("status") in {"completed", "succeeded", "success"}:
            url = item.get("url")
            if isinstance(url, str) and url.startswith("https://") and url not in urls:
                urls.append(url)
    return urls


def extract_artifact_failure(history: dict[str, Any], thread_id: str, asset_path: str) -> str:
    """Read only a completed server notification for this exact video artifact."""
    data = history.get("data")
    turns = data.get("turns") if isinstance(data, dict) else None
    if not isinstance(turns, list) or not asset_path.startswith("/videos/"):
        return ""
    for turn in reversed(turns[-5:]):
        messages = turn.get("messages") if isinstance(turn, dict) else None
        if not isinstance(messages, list):
            continue
        for message in reversed(messages[-256:]):
            if not isinstance(message, dict) or message.get("thread_id") != thread_id:
                continue
            update = message.get("data")
            if (not isinstance(update, dict) or update.get("stream_mode") != "updates"
                    or update.get("status") != "completed"):
                continue
            role = update.get("metadata")
            if not isinstance(role, dict) or role.get("agent_role") != "main_director":
                continue
            content = update.get("content")
            asset = content.get("_single_asset") if isinstance(content, dict) else None
            if not isinstance(asset, dict) or asset.get("status") not in {"error", "failed"}:
                continue
            parts = asset.get("notify_parts")
            if not isinstance(parts, list):
                continue
            for part in parts[:32]:
                if not isinstance(part, dict):
                    continue
                metadata, filename = part.get("metadata"), part.get("filename")
                if not isinstance(metadata, dict) or metadata.get("state") != "ERROR" or not isinstance(filename, str):
                    continue
                location = metadata.get("location")
                path = filename if filename.startswith("/") else (
                    str(location).rstrip("/") + "/" + filename if isinstance(location, str) else "")
                error = metadata.get("error")
                if path != asset_path or not isinstance(error, str):
                    continue
                error = error.strip()
                if error and error.casefold() != "artifact generation failed":
                    return error[:500]
    return ""


class NadClient:
    def __init__(self, account: dict[str, Any], *, store: Store | None = None):
        self.account = dict(account)
        self.store = store
        proxy = account_proxy_url(account).replace("socks5://", "socks5h://", 1)
        port = int(account.get("cdp_port") or 0)
        self.browser = CDPBrowser(port) if port else None
        if self.browser is not None:
            # Explicitly opened login browsers supersede a previously saved session.
            auth = self.browser.collect_auth()
            self.account.update(auth)
            if store is not None:
                store.update_account(int(account["id"]), auth)
        if not self.account.get("cookie") or not self.account.get("dfp"):
            raise NadError("Nadou session unavailable; complete browser login and verification")
        self.session = requests.Session()
        self.session.trust_env = False
        self.session.proxies.update({"http": proxy, "https": proxy})
        self.session.headers.update({"Accept": "application/json, text/plain, */*", "Origin": BASE,
                                     "Referer": BASE + "/", "dfp": str(self.account["dfp"]), "lang": "zh-CN",
                                     "User-Agent": str(self.account.get("user_agent") or "Mozilla/5.0")})
        for pair in str(self.account["cookie"]).split(";"):
            name, sep, value = pair.strip().partition("=")
            if sep and name:
                self.session.cookies.set(name, value, domain=".nadou.ai", path="/")

    def close(self) -> None:
        self.session.close()

    def request(self, method: str, path: str, body: Any = None, *, params: dict[str, Any] | None = None,
                files: Any = None, timeout: tuple[int, int] = (15, 120)) -> dict[str, Any]:
        if not path.startswith("/apis/") or urlsplit(path).netloc:
            raise NadError("Nadou API path is invalid")
        kwargs: dict[str, Any] = {"params": params, "timeout": timeout, "allow_redirects": False}
        if files is not None:
            kwargs["files"] = files
        elif method != "GET":
            kwargs["json"] = body
        try:
            response = self.session.request(method, BASE + path, **kwargs)
        except requests.RequestException as exc:
            # No retries: a POST timeout may have been accepted upstream.
            raise NadError(f"Nadou {method} request outcome unavailable: {type(exc).__name__}") from exc
        generation = method.upper() == "POST" and path == GRAPH + "/ainvoke"
        try:
            data = response.json()
        except ValueError as exc:
            if not generation and response.status_code >= 400:
                raise BrowserRejected(f"Nadou HTTP {response.status_code}: non-JSON rejection") from exc
            raise NadError("Nadou returned an unrecognized response; request outcome is unknown") from exc
        if not isinstance(data, dict):
            raise NadError("Nadou response envelope is invalid")
        if generation and (response.status_code >= 500 or response.status_code == 408
                           or (not response.ok and data.get("task_id"))
                           or "code" not in data):
            raise NadError(f"Nadou generation response is ambiguous (HTTP {response.status_code}); do not resubmit")
        if not response.ok or str(data.get("code")) != "A00000":
            if generation and (data.get("task_id") or str(data.get("code")) in {"", "None", "A00000"}):
                raise NadError("Nadou generation acceptance is uncertain; do not resubmit")
            message = str(data.get("msg") or data.get("message") or "request rejected")[:500]
            raise BrowserRejected(f"Nadou HTTP {response.status_code} {data.get('code', '')}: {message}")
        return data

    def account_state(self) -> dict[str, Any]:
        data = self.request("GET", "/apis/points/query/summary").get("data")
        if not isinstance(data, dict) or not isinstance(data.get("remainPoints"), (int, float)):
            raise NadError("Nadou credit balance was not returned; verify the login session")
        return {"available_balance": float(data["remainPoints"]), "buckets": data}

    verify = account_state

    def ensure_no_watermark(self) -> str:
        return "upstream_default"

    def daily_checkin(self) -> dict[str, Any]:
        # Only the observed eligible reward is automatic. Captcha/questionnaire
        # completion remains the normal user-driven website flow.
        state = self.request("GET", "/apis/nadouai/points/newbieTask/status").get("data") or {}
        if state.get("status") == 2 and not state.get("quotaExhausted"):
            receipt = self.request("POST", "/apis/nadouai/points/newbieTask/draw", {"dfp": self.account["dfp"]})
            data = receipt.get("data") or {}
            claimed = isinstance(data.get("points"), (int, float)) and data["points"] > 0
            return {"status": "claimed" if claimed else "manual_required", "manual_required": not claimed,
                    "data": data}
        return {"status": "manual_required" if state.get("status") in {0, 1} else "no_eligible_reward",
                "manual_required": state.get("status") in {0, 1},
                "data": state, "daily_signin_observed": False}

    checkin = daily_checkin

    def _upload_input(self, source: str, kind: str, account_id: int) -> dict[str, Any]:
        with _MATERIAL_PREPARATION_SLOTS:
            data = None
            try:
                data, mime, filename = _download(source, kind, str(self.account["proxy_url"]))
                digest = hashlib.sha256(data).hexdigest()
                # Only identical material for the same account shares a flight.
                # A different material must not wait behind this account's upload.
                with _media_cache_lock(account_id, kind, digest):
                    receipt = self.store.media_cache_get(account_id, kind, digest) if self.store else None
                    if not _complete_media_receipt(receipt):
                        with _MATERIAL_UPLOAD_SLOTS:
                            receipt = upload_media_bytes(data, mime, filename, kind, client=self)
                        if not _complete_media_receipt(receipt):
                            raise NadError("Nadou upload returned no complete material receipt")
                        if self.store:
                            self.store.media_cache_put(account_id, kind, digest, receipt)
                    return dict(receipt)
            finally:
                # Release the payload before another worker gets a preparation slot.
                data = None

    def upload_inputs(self, normalized: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
        result: dict[str, list[dict[str, Any]]] = {"images": [], "videos": [], "audios": []}
        account_id = int(self.account["id"])
        for kind in result:
            for source in normalized[kind]:
                # Each task retains the caller's order without sharing its session
                # across threads; independent tasks prepare concurrently.
                result[kind].append(self._upload_input(source, kind, account_id))
        validate_material_durations(normalized, result)
        # Seedance web clients register all materials for compliance before
        # generation, preserving the normal upstream validation mechanism.
        if normalized.get("family") in {"2.0", "2.5"}:
            urls = [item["url"] for kind in result for item in result[kind]]
            if urls:
                registered = self.request("POST", "/apis/nadouai/doubao/upload_asset", urls).get("data")
                if not isinstance(registered, list) or len(registered) != len(urls):
                    raise NadError("Nadou material compliance registration is incomplete")
                for record in registered:
                    metadata = record.get("metadatata") or record.get("metadata") or {}
                    status = metadata.get("doubaoAssetStatus")
                    if status and status != "Active":
                        raise BrowserRejected(f"Nadou material compliance status: {status}")
        return result

    def submit(self, normalized: dict[str, Any], assets: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
        # Captured clients use a client-generated millisecond snowflake thread.
        # One independent thread per task prevents old results being attributed
        # to a later task in the same conversation.
        thread_id = "s_" + str((int(time.time() * 1000) << 22) | secrets.randbits(22))
        self.request("POST", "/apis/nadouai/session/create", {
            "threadId": thread_id, "algorithmName": "screenplay2drama", "agentName": "nadou_agent"})
        param = build_video_param(normalized, assets)
        body = build_submit_body(param, assets, mode=normalized.get("mode", "reference_material"))
        query = {"command": 2, "stream": "true", "thread_id": thread_id}
        self.last_submit_request = {"endpoint": GRAPH + "/ainvoke", "query": query, "body": body}
        response = self.request("POST", GRAPH + "/ainvoke", body, params=query)
        if not response.get("task_id") or response.get("thread_id") != thread_id:
            raise NadError("Nadou submit returned no confirmed task ID; do not automatically resubmit")
        return {"run_id": str(response["task_id"]), "thread_id": thread_id, "state": "RUNNING",
                "param": param, "credits_consumed": None, "cost_evidence": {"source": "unknown"},
                "upstream_request": {"endpoint": GRAPH + "/ainvoke", "query": query, "body": body}}

    def poll(self, run_id: str, thread_id: str) -> dict[str, Any]:
        files = self.request("POST", GRAPH + "/files", {"files": ["ui_state.json"]},
                             params={"thread_id": thread_id})
        ui: dict[str, Any] = {}
        for entry in ((files.get("data") or {}).get("files") or {}).get("ui_state.json") or []:
            try:
                parsed = json.loads(entry["content"])
            except (KeyError, TypeError, ValueError):
                continue
            if isinstance(parsed, dict):
                ui = parsed
                break
        urls = extract_result_urls(ui)
        if urls:
            return {"status": "succeeded", "state": "SUCCEEDED", "result_urls": urls,
                    "error_code": "", "error": "", "credits_consumed": None,
                    "cost_evidence": {"source": "unknown"}}
        videos = (ui.get("asset") or {}).get("videos") or []
        failed = next((item for item in videos if item.get("status") in {"failed", "error"}), None)
        state = self.request("GET", GRAPH + "/state", params={"thread_id": thread_id, "final_result_show": "true"})
        if state.get("task_id") is not None and str(state["task_id"]) != str(run_id):
            raise NadError("Nadou returned a different task ID for this conversation")
        upstream = state.get("data") or {}
        status = str(upstream.get("status") or "RUNNING").upper()
        error = str((failed or {}).get("error") or (failed or {}).get("msg") or upstream.get("msg") or "")[:500]
        terminal_failure = failed is not None or status in {"FAILED", "ERROR", "CANCELED", "CANCELLED", "INTERRUPTED"}
        if failed is not None and error.strip().casefold() == "artifact generation failed":
            path = failed.get("path")
            if isinstance(path, str) and path.startswith("/videos/"):
                try:
                    history = self.request("GET", GRAPH + "/message_history",
                                           params={"thread_id": thread_id, "limit": 5}, timeout=(5, 10))
                    error = extract_artifact_failure(history, thread_id, path) or error
                except Exception:
                    # A failed diagnostic query cannot turn an established video
                    # failure into an uncertain submission or repeat generation.
                    pass
        return {"status": "failed" if terminal_failure else "running", "state": status,
                "result_urls": [], "error_code": "UPSTREAM_FAILED" if terminal_failure else "", "error": error,
                "credits_consumed": None, "cost_evidence": {"source": "unknown"}}


