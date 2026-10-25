# NAD2API

Nadou video task gateway with a Pip-style account pool, proxied browser login, task console, settings, and API documentation. Each account keeps its own browser profile, authenticated session, and proxy. Video requests follow the normal web upload, material validation, submission, and result-query workflow.

## Start

Copy `.env.example` to `.env` and set different, random API, administrator, synchronization, and cost synchronization keys. Create the external Docker network `my-shared-net` if it does not already exist, then pull the published image and start the service:

```sh
docker compose pull nad2api
docker compose up -d --no-build nad2api
```

The console listens on port `8816`. Sign in using `NAD_ADMIN_TOKEN`, add an account with its proxy, complete the normal website login in the embedded browser, and verify the account. Captchas and other interactive verification remain manual. Account sessions and tasks are persisted in `./data`; keep this directory private and backed up.

Administrators may import an existing authorized session through `POST /api/accounts` using `name`, `proxy_url`, `cookie`, `dfp`, and `user_agent`, then call `POST /api/accounts/{id}/check`. Credential fields are write-only in the management API. Normal callers use only the service API key, never website credentials.

## Video API

Use `Authorization: Bearer <NAD_API_KEY>` or `X-API-Key`.

```http
POST /v1/videos
Content-Type: application/json
Idempotency-Key: your-unique-request-id
```

```json
{
  "model": "sd-2-0-mini",
  "prompt": "Use image1 for the landscape, video1 for motion, and audio1 for the sound reference.",
  "duration": 4,
  "resolution": "480p",
  "aspect_ratio": "16:9",
  "image_urls": ["https://example.com/image.png"],
  "video_urls": ["https://example.com/reference.mp4"],
  "audio_urls": ["https://example.com/reference.mp3"]
}
```

The response contains a `nad_...` task ID. Query `GET /v1/videos/{id}` until completion; successful outputs appear in `data[].url`. `GET /v1/videos/{id}/content` redirects to the first result. Standard Seedance `content` entries containing `text`, `image_url`, `video_url`, and `audio_url` are accepted without extra parameter wrappers.

Prompt aliases such as `视频1`, `video2`, `v3`, `视4`, `图片1`, `图1`, `img2`, `Image3`, `Audio1`, and `音频2` are converted into the upstream web composer's reference tags. References remain one-based within each media type. Out-of-range references are rejected before generation. Material limits are enforced before upload, and duration checks use returned upload metadata.

An idempotency key belongs to one normalized request. Reusing it for a different request is rejected. A generation submission is never retried after an ambiguous network error. `submission_unknown` means the original task requires verification; it must not trigger automatic failover or a second generation. If original task IDs are available, administrators can use `POST /api/tasks/{id}/resume` to resume querying that same task.

## Models

Aliases without a resolution suffix default to **720p**. `GET /v1/models` returns actual resolutions, durations, ratios, material limits, and compatibility notes.

| Family | Public models | Reference limits |
|---|---|---|
| Seedance 2.0 | `sd-2-0`, `sd-2-0-1080p`, `sd-2-0-4k`, `sd-2-0-fast`, `sd-2-0-mini` | 9 images, 3 audio clips, 3 videos |
| Seedance 2.5 | `sd-2-5`, `sd-2-5-480p`, `sd-2-5-1080p`, `sd-2-5-480p-preview` | 30 images, 10 audio clips, 10 videos |
| Wan 3.0 | `wan-3.0`, `wan-3.0-480p`, `wan-3.0-1080p` | See model capabilities |
| MiniMax H3 | `minimax-h3-768p` | See model capabilities |
| Kling 4.0 | `kling-4.0`, `kling-4.0-1080p` | See model capabilities |
| Vidu Q4 | `vidu-q4`, `vidu-q4-540p` | 15 images, 3 audio clips; no video references |

The observed website configuration exposes Seedance 2.5 without a separate preview version; `sd-2-5-480p-preview` is an explicitly marked compatibility alias for its 480p mode. The upstream MiniMax H3 configuration exposes actual 720p rather than 768p; `minimax-h3-768p` preserves the public name while reporting actual 720p capability. Vidu Q4 aliases map to the observed upstream Q4 Preview model. Configuration-derived combinations are distinguished from actual generation acceptance results.

## Credits and errors

The account balance is used for account management. It is not treated as a task debit. A task reports credits only when there is task-specific evidence; otherwise `credits_consumed` is `null` and the console shows an unknown source. Purchase prices can be maintained separately for the cost reference view.

The reward operation claims only rewards that the upstream explicitly marks as ready. The captured protocol does not establish a separate daily check-in endpoint, so the service does not invent one. Interactive verification and questionnaires remain in the website flow.

Public errors use a bounded normalization vocabulary. Administrators can inspect both the normalized response and the upstream diagnostic in task details. Clear-completed removes terminal task records and their associated local data while preserving accounts and unresolved tasks.

## GitHub Actions and preproduction

`.github/workflows/deploy-test.yml` validates the application and builds/pushes its image on GitHub-hosted runners. The `nad2api-pre` self-hosted runner only pulls the published image, restarts this service, and configures the Cloudflared route. It does not build an image on the deployment host.

Prepare a private `.env` in `DEPLOY_PATH`, register the repository runner with label `nad2api-pre`, and configure its Docker and Cloudflared permissions. Workflow deployment variables are `DEPLOY_PATH`, `DEPLOY_SERVICE`, `IMAGE_REGISTRY`, `IMAGE_NAMESPACE`, `IMAGE_NAME`, `IMAGE_TAG`, `NAD2API_HOSTNAME`, `NAD2API_ORIGIN`, and `CLOUDFLARED_CONFIG`. GHCR uses the workflow's scoped `GITHUB_TOKEN`; external SSH deployments should use `DEPLOY_HOST`, `DEPLOY_USER`, `DEPLOY_SSH_KEY`, and `DEPLOY_PORT`.

Services on the shared Docker network can use `http://nad2api:8816`. The LAN console is available at the host IP and mapped port. Existing environment files, account data, and compose configuration are preserved on subsequent deployments; deployment waits rather than interrupting active tasks or logins.

Raw protocol captures, authenticated sessions, personal media, environment files, databases, and private deployment receipts are excluded from this public repository.
