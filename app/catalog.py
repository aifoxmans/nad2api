"""Nadou video capabilities observed in the authenticated web configuration."""

from __future__ import annotations

import html
import re
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ModelSpec:
    id: str
    upstream_model: str
    resolution: str
    family: str
    inferred_alias: str = ""

    @property
    def limits(self) -> dict[str, int]:
        return dict({"2.0": {"images": 9, "videos": 3, "audios": 3},
                     "2.5": {"images": 30, "videos": 10, "audios": 10},
                     "wan": {"images": 10, "videos": 5, "audios": 5},
                     "minimax": {"images": 9, "videos": 3, "audios": 3},
                     "kling": {"images": 10, "videos": 5, "audios": 0},
                     "vidu": {"images": 15, "videos": 0, "audios": 3}}[self.family])

    @property
    def max_duration(self) -> int:
        return {"2.0": 15, "2.5": 30, "wan": 30, "minimax": 15, "kling": 30, "vidu": 16}[self.family]

    @property
    def min_duration(self) -> int:
        return {"wan": 2, "kling": 3, "vidu": 3}.get(self.family, 4)

    @property
    def resolutions(self) -> tuple[str, ...]:
        if self.family == "2.0":
            return ("480p", "720p") if self.upstream_model.endswith(("-fast", "-mini")) else ("480p", "720p", "1080p", "4k")
        return {"2.5": ("480p", "720p", "1080p"), "wan": ("480p", "720p", "1080p"),
                "minimax": ("720p", "2k"), "kling": ("720p", "1080p"),
                "vidu": ("540p", "720p", "1080p", "2k", "4k")}[self.family]

    @property
    def ratios(self) -> set[str]:
        if self.family == "kling":
            return {"21:9", "16:9", "1:1", "9:16"}
        if self.family == "vidu":
            return {"16:9", "1:1", "9:16", "4:3", "3:4"}
        return {"21:9", "16:9", "4:3", "1:1", "3:4", "9:16"}


MODELS = {spec.id: spec for spec in (
    ModelSpec("sd-2-0", "seedance-2-0", "720p", "2.0"),
    ModelSpec("sd-2-0-1080p", "seedance-2-0", "1080p", "2.0"),
    ModelSpec("sd-2-0-4k", "seedance-2-0", "4k", "2.0"),
    ModelSpec("sd-2-0-fast", "seedance-2-0-fast", "720p", "2.0"),
    ModelSpec("sd-2-0-mini", "seedance-2-0-mini", "720p", "2.0"),
    ModelSpec("sd-2-5", "seedance-2-5", "720p", "2.5"),
    ModelSpec("sd-2-5-480p", "seedance-2-5", "480p", "2.5"),
    ModelSpec("sd-2-5-1080p", "seedance-2-5", "1080p", "2.5"),
    ModelSpec("sd-2-5-480p-preview", "seedance-2-5", "480p", "2.5",
              "Compatibility alias for current Seedance 2.5 480p; no separate preview model was observed."),
    ModelSpec("wan-3.0", "wan-3-0", "720p", "wan"),
    ModelSpec("wan-3.0-480p", "wan-3-0", "480p", "wan"),
    ModelSpec("wan-3.0-1080p", "wan-3-0", "1080p", "wan"),
    ModelSpec("minimax-h3-768p", "minimax-h3", "720p", "minimax",
              "Public compatibility name; Nadou produces actual 720p, not 768p."),
    ModelSpec("kling-4.0", "keling-4-0", "720p", "kling"),
    ModelSpec("kling-4.0-1080p", "keling-4-0", "1080p", "kling"),
    ModelSpec("vidu-q4", "vidu-q4-preview", "720p", "vidu",
              "Public Q4 alias maps to the observed Vidu Q4 Preview model."),
    ModelSpec("vidu-q4-540p", "vidu-q4-preview", "540p", "vidu",
              "Public Q4 alias maps to the observed Vidu Q4 Preview model."),
)}

RATIOS = {"16:9", "9:16", "1:1", "4:3", "3:4", "21:9"}
ALIASES = re.compile(r"(?<![A-Za-z0-9_])(?P<explicit>@)?(?P<kind>视频|video|v|视|图片|image|img|图|音频|audio|音)\s*(?P<index>\d+)(?![A-Za-z0-9_])", re.I)


def media_alias_kind(label: str) -> str:
    if label.lower() in {"视频", "video", "v", "视"}:
        return "videos"
    if label.lower() in {"图片", "image", "img", "图"}:
        return "images"
    return "audios"


def prompt_references(prompt: str, media: dict[str, list[Any]]) -> list[tuple[str, int]]:
    found = []
    for match in ALIASES.finditer(prompt):
        kind, index = media_alias_kind(match.group("kind")), int(match.group("index"))
        if not media[kind] and not match.group("explicit"):
            continue
        if not 1 <= index <= len(media[kind]):
            raise ValueError(f"prompt references {match.group(0)} but only {len(media[kind])} {kind} were provided")
        found.append((kind, index))
    return found


def bind_prompt(prompt: str, assets: dict[str, list[Any]]) -> str:
    """Build the same escaped reference-tag HTML that the web composer submits."""
    chunks: list[str] = []
    end = 0
    for match in ALIASES.finditer(prompt):
        chunks.append(html.escape(prompt[end:match.start()]).replace("\n", "<br>"))
        kind, index = media_alias_kind(match.group("kind")), int(match.group("index"))
        if not assets[kind] and not match.group("explicit"):
            chunks.append(html.escape(match.group(0)))
        else:
            if not 1 <= index <= len(assets[kind]):
                raise ValueError(f"unbound media reference: {match.group(0)}")
            asset = assets[kind][index - 1]
            asset = asset if isinstance(asset, dict) else {"url": asset}
            url = html.escape(str(asset["url"]), quote=True)
            thumb = html.escape(str(asset.get("thumbnailUrl") or ""), quote=True)
            label = {"images": "图片", "videos": "视频", "audios": "音频"}[kind] + str(index)
            image = f'<img src="{thumb}" class="reference-tag-thumbnail">' if thumb else ""
            chunks.append(f'<span data-type="reference-tag" data-value="{label}" data-url="{url}" '
                          f'data-thumbnail-url="{thumb}" class="reference-tag image-tag" '
                          f'data-id="nad-{kind}-{index}" value="{label}" url="{url}" thumbnailurl="{thumb}">'
                          f'{image}<span class="reference-tag-text">@{label}</span></span>')
        end = match.end()
    chunks.append(html.escape(prompt[end:]).replace("\n", "<br>"))
    return "<p>" + "".join(chunks) + "</p>"


def _url(item: Any) -> str:
    if isinstance(item, dict):
        item = item.get("url") or item.get("image_url") or item.get("video_url") or item.get("audio_url")
        if isinstance(item, dict):
            item = item.get("url")
    if not isinstance(item, str) or not item.strip():
        raise ValueError("media reference must contain a nonempty URL")
    return item.strip()


def _media_list(body: dict[str, Any], names: tuple[str, ...]) -> list[str]:
    value: Any = []
    for name in names:
        if name in body:
            value = body[name]
            break
    if value is None:
        return []
    return [_url(item) for item in (value if isinstance(value, list) else [value])]


def normalize_request(body: dict[str, Any]) -> dict[str, Any]:
    name = str(body.get("model") or "sd-2-0-mini").strip()
    spec = MODELS.get(name)
    if spec is None:
        raise ValueError(f"unsupported model: {name}")
    media = {"images": _media_list(body, ("image_urls", "images", "reference_images", "image_url", "image", "input_reference")),
             "videos": _media_list(body, ("video_urls", "videos", "reference_videos", "video_url", "video")),
             "audios": _media_list(body, ("audio_urls", "audios", "reference_audios", "audio_url", "audio"))}
    prompt = str(body.get("prompt") or "").strip()
    content = body.get("content")
    if isinstance(content, list):
        texts = []
        for item in content:
            if not isinstance(item, dict):
                raise ValueError("content items must be objects")
            kind = item.get("type")
            if kind == "text":
                texts.append(str(item.get("text") or ""))
            elif kind in {"image_url", "video_url", "audio_url"}:
                key = {"image_url": "images", "video_url": "videos", "audio_url": "audios"}[kind]
                media[key].append(_url(item.get(kind)))
            else:
                raise ValueError(f"unsupported content type: {kind}")
        if not prompt:
            prompt = "\n".join(texts).strip()
    if not prompt:
        raise ValueError("prompt is required")
    if len(prompt) > (5000 if spec.family == "vidu" else 7000 if spec.family == "minimax" else 10000):
        raise ValueError("prompt exceeds this upstream model's limit")
    raw_duration = body.get("duration", body.get("seconds", spec.min_duration))
    if isinstance(raw_duration, bool) or not str(raw_duration).isdigit():
        raise ValueError("duration must be an integer number of seconds")
    duration = int(raw_duration)
    if not spec.min_duration <= duration <= spec.max_duration:
        raise ValueError(f"duration must be {spec.min_duration}–{spec.max_duration} seconds for {name}")
    ratio = str(body.get("aspect_ratio") or body.get("ratio") or "16:9").lower()
    if ratio not in spec.ratios:
        raise ValueError(f"unsupported aspect ratio for {name}: {ratio}")
    resolution = str(body.get("resolution") or spec.resolution).lower()
    if resolution not in spec.resolutions:
        raise ValueError(f"unsupported resolution for {name}: {resolution}")
    fixed = name.endswith(("-1080p", "-480p", "-540p", "-4k", "-preview", "-768p"))
    if fixed and resolution != spec.resolution:
        raise ValueError(f"{name} maps to actual {spec.resolution}; choose another model alias")
    for kind, limit in spec.limits.items():
        if len(media[kind]) > limit:
            raise ValueError(f"{name} supports at most {limit} {kind}")
    if spec.family == "minimax" and sum(map(len, media.values())) > 12:
        raise ValueError("minimax-h3 supports at most 12 total reference materials")
    if str(body.get("n", body.get("generate_count", 1))) != "1":
        raise ValueError("only one video per task is supported")
    prompt_references(prompt, media)
    has_media = any(media.values())
    if spec.family == "vidu" and not has_media:
        raise ValueError("the observed Vidu Q4 interface requires reference materials")
    mode = str(body.get("mode") or ("reference_material" if has_media else "t2v"))
    if mode not in {"t2v", "i2v", "i2v_first_last", "reference_images", "reference_material"}:
        raise ValueError(f"unsupported video mode: {mode}")
    if mode == "t2v" and has_media:
        raise ValueError("t2v cannot include reference materials")
    if mode in {"i2v", "i2v_first_last"} and (media["videos"] or media["audios"]):
        raise ValueError("first/last frame mode supports only images")
    if mode in {"i2v", "i2v_first_last"} and not media["images"]:
        raise ValueError("image-to-video requires an image")
    if mode in {"i2v", "i2v_first_last"} and len(media["images"]) > 2:
        mode = "reference_material"
    if spec.family == "vidu" and mode in {"i2v", "i2v_first_last"} and len(media["images"]) > 1:
        raise ValueError("Vidu first-frame mode supports one image; use reference_material for multiple images")
    generate_audio = body.get("generate_audio", body.get("enable_audio", True))
    if not isinstance(generate_audio, bool):
        if str(generate_audio).lower() not in {"true", "false"}:
            raise ValueError("generate_audio must be a boolean")
        generate_audio = str(generate_audio).lower() == "true"
    if spec.family in {"kling", "minimax"} and not generate_audio:
        raise ValueError(f"{name} does not expose an audio-disable parameter in the observed Nadou interface")
    return {"model": name, "upstream_model": spec.upstream_model, "family": spec.family,
            "duration": duration, "resolution": resolution, "aspect_ratio": ratio,
            "prompt": prompt, "mode": mode, "generate_audio": generate_audio, **media}


def public_models() -> list[dict[str, Any]]:
    return [{"id": spec.id, "object": "model", "owned_by": "nadapi",
             "meta": {"label": spec.id, "upstream_model": spec.upstream_model,
                      "actual_default_resolution": spec.resolution, "capability_source": "observed_web_config",
                      "inferred_alias": bool(spec.inferred_alias), "alias_note": spec.inferred_alias,
                      "combination_generation_verified": False},
             "capabilities": {"durations": list(range(spec.min_duration, spec.max_duration + 1)),
                              "resolutions": [spec.resolution] if spec.id.endswith(("-1080p", "-480p", "-540p", "-4k", "-preview", "-768p")) else list(spec.resolutions),
                              "aspect_ratios": sorted(spec.ratios), "media_limits": spec.limits}}
            for spec in MODELS.values()]
