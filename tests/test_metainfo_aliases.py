"""多语言动漫发布名的识别回归测试。"""

from unittest.mock import patch

import pytest

from app.core.metainfo import MetaInfo
from app.core.meta.metaanime import extract_multilingual_anime_names
from app.schemas.types import MediaType


@pytest.mark.parametrize("aliases", [
    "侦探已经死了。 第二季 / 探偵はもう、死んでいる。 Season 2 / The Detective Is Already Dead Season 2",
    "探偵はもう、死んでいる。 Season 2 / 侦探已经死了。 第二季 / The Detective Is Already Dead Season 2",
    "The Detective Is Already Dead Season 2 / 探偵はもう、死んでいる。 Season 2 / 侦探已经死了。 第二季",
])
@pytest.mark.parametrize("parser", ["python", "rust", "default"])
def test_multilingual_anime_preserves_search_names(aliases: str, parser: str) -> None:
    """重复季号不能吞掉英文别名，日文汉字也不能被当作中文清理。"""
    title = f"[NEST] {aliases} - 01 [CR WEB-DL 1080p AVC AAC][简日内封]"
    if parser == "default":
        meta = MetaInfo(title)
    else:
        parsed = None if parser == "python" else {
            "kind": "anime",
            "title": title,
            "org_string": title,
            "type": MediaType.TV.value,
            "en_name": "はもう、 んでいる。",
            "begin_season": 2,
            "total_season": 1,
            "begin_episode": 1,
            "total_episode": 1,
            "resource_team": "NEST",
            "resource_pix": "1080p",
            "video_encode": "AVC",
            "audio_encode": "AAC",
        }
        with patch("app.core.metainfo.rust_accel.parse_metainfo", return_value=parsed):
            meta = MetaInfo(title)

    assert meta.cn_name == "侦探已经死了。"
    assert meta.en_name == "The Detective Is Already Dead"
    assert meta.name == "The Detective Is Already Dead"
    assert meta.type == MediaType.TV
    assert meta.season == "S02"
    assert meta.episode == "E01"
    assert meta.resource_team == "NEST"
    assert meta.resource_pix == "1080p"
    assert meta.video_encode == "AVC"
    assert meta.audio_encode == "AAC"
    assert meta.org_string == title


@pytest.mark.parametrize("use_rust", [False, True])
def test_multilingual_anime_respects_custom_words(use_rust: bool) -> None:
    """修复别名时仍须使用识别词处理后的名称。"""
    title = (
        "[NEST] 旧中文名。 第二季 / 探偵はもう、死んでいる。 Season 2 / "
        "The Detective Was Already Dead Season 2 - 01 [CR WEB-DL 1080p AVC AAC][简日内封]"
    )
    words = ["旧中文名 => 侦探已经死了", "The Detective Was Already Dead => The Detective Is Already Dead"]
    if use_rust:
        meta = MetaInfo(title, custom_words=words)
    else:
        with patch("app.core.metainfo.rust_accel.parse_metainfo", return_value=None):
            meta = MetaInfo(title, custom_words=words)

    assert meta.cn_name == "侦探已经死了。"
    assert meta.en_name == "The Detective Is Already Dead"
    assert meta.season == "S02"
    assert meta.episode == "E01"
    assert meta.apply_words == words
    assert meta.original_name == "The Detective Was Already Dead"


@pytest.mark.parametrize("title", [
    "[NEST] 侦探已经死了。 / The Detective Is Already Dead - 01 [简 / 日 / 内封]",
    "[NEST] 侦探已经死了。 / The Detective Is Already Dead - 01 [日本語 / CHS / CHT]",
    "[NEST] The Detective Is Already Dead / 探偵はもう、死んでいる。 Season 2 - 01",
    "[NEST] 侦探已经死了。 / 探偵はもう、死んでいる。 Season 2 - 01",
])
def test_multilingual_anime_ignores_incomplete_aliases(title: str) -> None:
    """字幕标签内的斜杠和不完整的别名不能触发中日英覆盖。"""
    assert extract_multilingual_anime_names(title) == (None, None)
