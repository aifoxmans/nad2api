from html.parser import HTMLParser

import pytest

from app.catalog import bind_prompt, prompt_references


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
