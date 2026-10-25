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
        prompt_references("图4", ASSETS)
    with pytest.raises(ValueError, match="unbound"):
        bind_prompt("@audio3", ASSETS)
    empty = {kind: [] for kind in ASSETS}
    assert bind_prompt("视频1是一段标题", empty) == "<p>视频1是一段标题</p>"


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
