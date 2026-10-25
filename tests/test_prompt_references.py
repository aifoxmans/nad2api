from html.parser import HTMLParser

import pytest

from app.catalog import bind_prompt, normalize_request, prompt_references


ASSETS = {"images": [{"url": f"https://media.example.test/i{i}?x=1&y=2", "thumbnailUrl": ""} for i in range(1, 4)],
          "videos": [{"url": f"https://media.example.test/v{i}"} for i in range(1, 5)],
          "audios": [{"url": f"https://media.example.test/a{i}"} for i in range(1, 3)]}


class References(HTMLParser):
    def __init__(self):
        super().__init__()
        self.references = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "span" and attrs.get("data-type") == "reference-tag":
            self.references.append((attrs["data-value"], attrs["data-url"]))


def test_aliases_reference_correct_media_without_reordering():
    prompt = "视频1|video2|v3|视4|图片1|图1|img2|Image3|Audio1|音频2"
    parsed = References()
    parsed.feed(bind_prompt(prompt, ASSETS))
    assert [label for label, _ in parsed.references] == ["视频1", "视频2", "视频3", "视频4", "图片1", "图片1", "图片2", "图片3", "音频1", "音频2"]
    assert parsed.references[4][1] == ASSETS["images"][0]["url"]
    assert parsed.references[-1][1] == ASSETS["audios"][1]["url"]


def test_chinese_adjacent_labels_and_html_are_safe():
    value = bind_prompt('参考图1向左，视频1向右 <script>alert("x")</script>\n音频1', ASSETS)
    parsed = References()
    parsed.feed(value)
    assert len(parsed.references) == 3
    assert "<script>" not in value
    assert "&lt;script&gt;" in value
    assert "<br>" in value


def test_missing_or_out_of_range_references_are_not_silently_rebound():
    with pytest.raises(ValueError, match="only 3 images"):
        prompt_references("图4", ASSETS, strict_prompt_media_references=True)
    with pytest.raises(ValueError, match="unbound"):
        bind_prompt("@audio3", ASSETS, strict_prompt_media_references=True)
    empty = {kind: [] for kind in ASSETS}
    assert bind_prompt("视频1是一段标题", empty) == "<p>视频1是一段标题</p>"


def test_loose_numbered_references_keep_invalid_tokens_as_safe_plain_text():
    prompt = "@图片1|图片4|@图片9|图0|视频2|@视频5|视0|Audio1|@音频3|音0 <script>alert(1)</script>"
    assert prompt_references(prompt, ASSETS) == [("images", 1), ("videos", 2), ("audios", 1)]
    bound = bind_prompt(prompt, ASSETS)
    parsed = References()
    parsed.feed(bound)
    assert parsed.references == [("图片1", ASSETS["images"][0]["url"]),
                                 ("视频2", ASSETS["videos"][1]["url"]),
                                 ("音频1", ASSETS["audios"][0]["url"])]
    for token in ("图片4", "@图片9", "图0", "@视频5", "视0", "@音频3", "音0"):
        assert token in bound
    assert "<script>" not in bound and "&lt;script&gt;" in bound


@pytest.mark.parametrize("token", ["Image4", "@图4", "图0", "@Video5", "v0", "@Audio3", "音0"])
def test_strict_mode_rejects_out_of_range_numbered_references(token):
    with pytest.raises(ValueError, match="only"):
        prompt_references(token, ASSETS, strict_prompt_media_references=True)
    with pytest.raises(ValueError, match="unbound"):
        bind_prompt(token, ASSETS, strict_prompt_media_references=True)


@pytest.mark.parametrize("strict", [False, True])
def test_valid_numbered_references_bind_identically_in_both_modes(strict):
    prompt = "Image2 @v3 Audio1"
    assert prompt_references(prompt, ASSETS, strict_prompt_media_references=strict) == [
        ("images", 2), ("videos", 3), ("audios", 1)]
    parsed = References()
    parsed.feed(bind_prompt(prompt, ASSETS, strict_prompt_media_references=strict))
    assert parsed.references == [("图片2", ASSETS["images"][1]["url"]),
                                 ("视频3", ASSETS["videos"][2]["url"]),
                                 ("音频1", ASSETS["audios"][0]["url"])]


def test_loose_mode_keeps_explicit_and_zero_references_without_that_media_kind():
    empty = {kind: [] for kind in ASSETS}
    prompt = "@Image1 图0 @video9 视0 @Audio2 音0"
    assert prompt_references(prompt, empty) == []
    assert bind_prompt(prompt, empty) == "<p>" + prompt + "</p>"
    with pytest.raises(ValueError, match="only 0 images"):
        prompt_references(prompt, empty, strict_prompt_media_references=True)
    with pytest.raises(ValueError, match="unbound"):
        bind_prompt(prompt, empty, strict_prompt_media_references=True)
    assert bind_prompt("视频1是一个章节", empty, strict_prompt_media_references=True) == "<p>视频1是一个章节</p>"


@pytest.mark.parametrize("strict", [False, True])
def test_exact_source_url_never_hides_a_missing_uploaded_receipt(strict):
    source = "https://source.example.test/source.png"
    empty = {kind: [] for kind in ASSETS}
    sources = {**empty, "images": [source]}
    with pytest.raises(ValueError, match="unbound"):
        bind_prompt(source, empty, sources=sources, strict_prompt_media_references=strict)


@pytest.mark.parametrize("strict", [False, True])
@pytest.mark.parametrize("token", ["Image2", "@Image2"])
def test_valid_source_number_never_hides_a_missing_uploaded_receipt(strict, token):
    sources = {"images": ["https://source.example.test/1.png", "https://source.example.test/2.png"],
               "videos": [], "audios": []}
    assets = {"images": [{"url": "https://uploaded.example.test/1.png"}], "videos": [], "audios": []}
    with pytest.raises(ValueError, match="unbound"):
        bind_prompt(token, assets, sources=sources, strict_prompt_media_references=strict)


@pytest.mark.parametrize("token", ["Image2", "@Image2"])
def test_source_count_bounds_numbered_references_even_with_extra_upload_receipts(token):
    sources = {"images": ["https://source.example.test/1.png"], "videos": [], "audios": []}
    assets = {"images": [{"url": "https://uploaded.example.test/1.png"},
                         {"url": "https://uploaded.example.test/2.png"}], "videos": [], "audios": []}
    assert bind_prompt(token, assets, sources=sources) == "<p>" + token + "</p>"
    with pytest.raises(ValueError, match="unbound"):
        bind_prompt(token, assets, sources=sources, strict_prompt_media_references=True)


def test_admin_reference_setting_is_snapshotted_and_caller_cannot_override_it():
    body = {"prompt": "Image1 @Image99", "image_urls": ["https://source.example.test/i.png"],
            "strict_prompt_media_references": True}
    loose = normalize_request(body)
    assert loose["strict_prompt_media_references"] is False
    assert loose["prompt"] == body["prompt"] and body["strict_prompt_media_references"] is True
    with pytest.raises(ValueError, match="only 1 images"):
        normalize_request({**body, "strict_prompt_media_references": False}, strict_prompt_media_references=True)
    strict = normalize_request({**body, "prompt": "Image1", "strict_prompt_media_references": False},
                               strict_prompt_media_references=True)
    assert strict["strict_prompt_media_references"] is True


@pytest.mark.parametrize("strict", [False, True])
def test_reference_setting_does_not_relax_actual_media_capabilities(strict):
    with pytest.raises(ValueError, match="at most 9 images"):
        normalize_request({"prompt": "@Image99", "image_urls": ["https://source.example.test/i.png"] * 10},
                          strict_prompt_media_references=strict)
    with pytest.raises(ValueError, match="at most 0 audios"):
        normalize_request({"model": "kling-4.0", "prompt": "@Audio99", "audio_urls": ["https://source.example.test/a.mp3"]},
                          strict_prompt_media_references=strict)


def test_explicit_inline_video_is_deduplicated_and_existing_image_rebound():
    image = "https://source.example.test/product.jpg"
    video = "https://source.example.test/reference.mp4"
    prompt = f"参考视频链接：{video}\n仍参考上面的片段 {video}\n{image}"
    body = {"model": "sd-2-0-fast", "prompt": prompt, "image_urls": [image],
            "duration": 15, "resolution": "720p", "aspect_ratio": "9:16"}
    normalized = normalize_request(body)
    assert normalized["images"] == [image]
    assert normalized["videos"] == [video]
    assert body["prompt"] == prompt and body["image_urls"] == [image] and "video_urls" not in body
    assets = {"images": [{"url": "https://uploaded.example.test/image.jpg"}],
              "videos": [{"url": "https://uploaded.example.test/video.mp4"}], "audios": []}
    parsed = References()
    bound = bind_prompt(prompt, assets, sources=normalized)
    parsed.feed(bound)
    assert parsed.references == [("视频1", assets["videos"][0]["url"]),
                                 ("视频1", assets["videos"][0]["url"]),
                                 ("图片1", assets["images"][0]["url"])]
    assert image not in bound and video not in bound


@pytest.mark.parametrize("label,extension,kind", [
    ("参考视频", "mp4", "videos"), ("视频链接", "mov", "videos"),
    ("参考图片", "png", "images"), ("图片链接", "webp", "images"),
    ("参考音频", "mp3", "audios"), ("音频链接", "wav", "audios"),
    ("reference video", "webm", "videos"), ("Reference image URL", "jpg", "images"),
    ("REFERENCE AUDIO", "m4a", "audios"),
])
def test_only_explicit_reference_labels_supplement_direct_media(label, extension, kind):
    url = "https://source.example.test/file." + extension
    normalized = normalize_request({"model": "sd-2-0-fast", "prompt": f"{label}：{url}"})
    assert normalized[kind] == [url]


def test_plain_links_and_alias_like_url_paths_remain_plain_text():
    page = "https://docs.example.test/image99/video77.html?audio44=two&image1=one"
    unlabelled = "https://source.example.test/video2.mp4"
    prompt = f"Read {page}; ordinary attachment example {unlabelled}"
    normalized = normalize_request({"prompt": prompt})
    assert not any(normalized[kind] for kind in ("images", "videos", "audios"))
    assert prompt_references(prompt, {"images": [], "videos": [], "audios": []}) == []
    bound = bind_prompt(prompt, {"images": [], "videos": [], "audios": []}, sources=normalized)
    assert "reference-tag" not in bound
    assert "image99/video77.html?audio44=two&amp;image1=one" in bound
    assert unlabelled in bound


def test_signed_queries_and_balanced_parentheses_are_not_rewritten():
    source = "https://source.example.test/video2(clip).mp4?sig=a%2Fb%29%3D&image1=unchanged"
    prompt = f"参考视频：({source})，音频链接：【https://source.example.test/audio1.mp3】。"
    normalized = normalize_request({"prompt": prompt})
    assert normalized["videos"] == [source]
    assert normalized["audios"] == ["https://source.example.test/audio1.mp3"]
    uploaded = "https://uploaded.example.test/video2.mp4?signature=keep%2Fvalue%3D&image77=unchanged"
    assets = {"images": [], "videos": [{"url": uploaded}],
              "audios": [{"url": "https://uploaded.example.test/audio.mp3"}]}
    parsed = References()
    bound = bind_prompt(prompt, assets, sources=normalized)
    parsed.feed(bound)
    assert parsed.references[0] == ("视频1", uploaded)
    assert len(parsed.references) == 2
    assert "signature=keep%2Fvalue%3D&amp;image77=unchanged" in bound
    assert "</span>)，" in bound


def test_reference_list_appends_in_order_and_exact_url_tokens_only():
    first = "https://source.example.test/one.jpg"
    second = "https://source.example.test/two.jpg"
    different = first + ".extra"
    prompt = f"参考图片：{first}、{second}\n普通链接 {different}"
    normalized = normalize_request({"prompt": prompt, "image_urls": [second]})
    assert normalized["images"] == [second, first]
    assets = {"images": [{"url": "https://uploaded.example.test/2.jpg"},
                         {"url": "https://uploaded.example.test/1.jpg"}], "videos": [], "audios": []}
    parsed = References()
    bound = bind_prompt(prompt, assets, sources=normalized)
    parsed.feed(bound)
    assert [item[0] for item in parsed.references] == ["图片2", "图片1"]
    assert different in bound


def test_reference_label_does_not_capture_links_after_unrelated_prose():
    page = "https://docs.example.test/video.html"
    later = "https://source.example.test/not-a-reference.mp4"
    prompt = f"参考视频说明请阅读 {page}\n这段只是例子，不作素材 {later}"
    normalized = normalize_request({"prompt": prompt})
    assert normalized["videos"] == []


@pytest.mark.parametrize("opening,closing", [("【", "】"), ("（", "）"), ("《", "》"), ("“", "”"), ("‘", "’")])
def test_chinese_wrappers_are_outside_signed_media_urls(opening, closing):
    url = "https://media.example.test/reference.mp4?signature=abc%2Fdef%3D&expires=123&tail="
    normalized = normalize_request({"prompt": f"参考视频：{opening}{url}{closing}"})
    assert normalized["videos"] == [url]
