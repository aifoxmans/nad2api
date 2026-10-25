# Nadou protocol integration

This document describes the normal browser requests observed on 2026-10-25 and the current public web client configuration. All examples are synthetic. Browser recordings, cookies, device identifiers, account identifiers, signed upload URLs, and user media are excluded from this public repository.

## Authentication and account isolation

The API origin is `https://www.nadou.ai`. Authenticated `/apis/` calls use the account's normal website cookies, its existing `dfp` value, `lang: zh-CN`, and its browser user agent. The official client reads its device value from `window.dfp.tryGetFingerPrint()`. Nadou's login browser collects existing cookies including HttpOnly cookies through CDP; credentials are stored privately in the service database and hidden from public account responses.

Each account has a separate HTTP session and proxy. Requests do not inherit machine proxy environment variables. Upload batches and submission are serialized for the selected account. A newly completed website login supersedes previously saved cookies. Saved authorized sessions can make protocol requests without keeping Chromium running. CAPTCHA and login challenges must be completed through the normal website flow; the service neither manufactures device fingerprints nor fabricates CAPTCHA tokens.

## Reference uploads

Files smaller than 5 MiB use `POST /apis/nadouai/file/upload` with multipart field `file`.

For larger files the normal browser sequence is:

1. `POST /apis/nadouai/qichuan/file/upload/request` with `fileType` and `fileSize`.
2. Upload chunks of up to 10 MiB to the returned signed `https://intl-upload.iqiyi.com/upload` URL. Multipart fields are `file_id`, inclusive `range`, `range_md5`, and `file`. Check that the returned file ID, MD5, and accepted range match. Account cookies and the device header are never attached to this upload host.
3. `POST /apis/nadouai/qichuan/file/upload/finish` with `fileId`.
4. `POST /apis/nadouai/material/upload_url?url=...` registers the returned `staticwebPath` and returns a material URL and metadata.

Metadata durations are milliseconds; generation attachments use seconds. The upload cache is keyed by account, media kind, and source SHA-256. Material order and duplicate input slots are retained. Before Seedance generation, `POST /apis/nadouai/doubao/upload_asset` registers all uploaded material URLs for the upstream's normal compliance check. An explicit non-active compliance result is rejected before generation.

## Prompt references and generation

Public requests accept standard `prompt`, `images`/`image_urls`, `videos`/`video_urls`, `audios`/`audio_urls`, or Seedance `content` entries with `text`, `image_url`, `video_url`, and `audio_url`. Callers do not need a separate `contents`, `settings`, or `options` object.

References such as `视频1`, `video2`, `v3`, `视4`, `图片1`, `图1`, `img2`, `Image3`, `Audio1`, and `音频2` are converted into the web composer's escaped HTML reference tags. Each tag retains its media-kind index and uploaded URL. Caller HTML is escaped; it cannot become arbitrary page markup. Explicit missing references and out-of-range indices fail locally.

The service creates one independent conversation per task with `POST /apis/nadouai/session/create` (`threadId`, `algorithmName: screenplay2drama`, `agentName: nadou_agent`). It submits once to:

```text
POST /apis/graph/screenplay2drama/nadou_agent/graph/ainvoke
?command=2&stream=true&thread_id=s_EXAMPLE
```

The JSON body has `input.content`, `style`, `intention: user-pick-params`, `source: input`, `attachments`, and `userParams`. `userParams.params` contains the description HTML, the ordered URLs of all media under `image` (including audio/video, as in the captured client), `model`, `ratio`, `resolution`, string `duration`, and `generateCount: "1"`. Models whose observed interface exposes the toggle also receive `generateAudio`. `bt_extra_body.contextvars.lang` is `zh-CN`.

With materials the default task type is `MULTI_REF_IMG2VIDEO`; without materials it is `TEXT2VIDEO`. Explicit first/last frame modes use `IMG2VIDEO`. The successful envelope contains `code: A00000`, `thread_id`, and `task_id`.

Generation POSTs are never automatically retried. Network failures, malformed responses, HTTP 408/5xx, conflicting task IDs, and other ambiguous acceptance outcomes remain unknown; they must not trigger an automatic duplicate generation. Complete business rejections with an explicit non-success code are distinguished from uncertain submission.

## Polling and output identity

`POST .../graph/files?thread_id=...` with `{"files":["ui_state.json"]}` returns the generated output inventory. Completed records under `asset.videos` provide the result URL and actual dimensions/duration. Input attachment URLs are never treated as outputs. `GET .../graph/state?thread_id=...&final_result_show=true` provides graph status and confirms task identity. The normal browser also uses `message_history` and a streaming `messages` endpoint for conversation presentation; API polling does not need an open WebSocket.

## Credit state and rewards

`GET /apis/points/query/summary` returns `remainPoints`, `freeRemainPoints`, `rechargeRemainPoints`, and bucket details. `GET /apis/points/query/detail` lists credit transactions, but observed entries do not identify their generation task. An account-wide balance change or transaction time alone cannot prove a particular task's consumption. Task consumption remains unknown unless an explicit task-associated source becomes available; no fabricated actual charge is emitted.

The recorded new-user reward sequence is `newbieTask/status`, `open`, `captcha/init`, normal human CAPTCHA completion, `submit`, and `draw`, under `/apis/nadouai/points/`. Only an already eligible status `2` can be drawn automatically, using the account's existing device value. A CAPTCHA/questionnaire requirement is surfaced for manual completion. A positive points receipt is required before marking a claim successful. No recurring daily sign-in endpoint was observed; the service does not pretend the new-user reward is a daily check-in.

## Model configuration and compatibility names

Capabilities come from `GET /apis/nadouai/task/configs`; advertised configuration support is distinct from successful live generation of every parameter combination. The public `/v1/models` metadata makes this distinction explicit. Default resolution is 720p unless the alias has a resolution suffix. Unsupported resolutions, ratios, duration ranges, material types, and counts fail before submission.

| Public family | Actual Nadou model | Material limit (images/audio/video) | Duration |
|---|---|---|---|
| `sd-2-0`, 1080p, 4k | `seedance-2-0` | 9 / 3 / 3 | 4–15s |
| `sd-2-0-fast` | `seedance-2-0-fast` | 9 / 3 / 3 | 4–15s |
| `sd-2-0-mini` | `seedance-2-0-mini` | 9 / 3 / 3 | 4–15s |
| `sd-2-5`, 480p, 1080p | `seedance-2-5` | 30 / 10 / 10 | 4–30s |
| `wan-3.0`, 480p, 1080p | `wan-3-0` | 10 / 5 / 5 | 2–30s |
| `minimax-h3-768p` | `minimax-h3`, actual 720p | 9 / 3 / 3, at most 12 total | 4–15s |
| `kling-4.0`, 1080p | `keling-4-0` | 10 / 0 / 5 | 3–30s |
| `vidu-q4`, 540p | `vidu-q4-preview` | 15 / 3 / 0 | 3–16s |

Mini/Fast support 480p and 720p; standard 2.0 additionally supports 1080p and 4K. Vidu's observed multi-reference interface supports images and audio, and has no text-only entry. Audio references must be 3–12 seconds each for Vidu and 1–15 seconds each for Seedance 2.0. Other per-file constraints are validated from returned metadata before generation. No additional total audio duration allowance is claimed from an unobserved rule.

`sd-2-5-480p-preview` is a compatibility alias for the current `seedance-2-5` at 480p: a separate 2.5 Preview model was not observed. `minimax-h3-768p` preserves the public routing name but actually requests 720p; neither metadata nor response claims a 768p output. Public Vidu Q4 aliases use the observed Q4 Preview upstream identifier. These mappings are explicitly marked as aliases in model metadata rather than invented upstream parameters.
