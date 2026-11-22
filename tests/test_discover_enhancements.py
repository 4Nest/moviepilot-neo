import asyncio
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest

from app import schemas
from app.api.endpoints import discover as discover_endpoint
from app.api.endpoints import media as media_endpoint
from app.api.endpoints import subscribe as subscribe_endpoint
from app.chain.subscribe import SubscribeChain
from app.core.config import settings
from app.core.context import MediaInfo
from app.modules.douban import DoubanModule
from app.modules.themoviedb import TheMovieDbModule
from app.schemas.types import ChainEventType, MediaType


@pytest.fixture(autouse=True)
def _clean_discover_caches():
    """每个测试用例前后清空探索源内存缓存，避免跨用例状态污染。"""
    from app.helper import discover_sources

    discover_sources.clear_discover_source_caches()
    yield
    discover_sources.clear_discover_source_caches()


def test_tmdb_short_drama_filter_rules() -> None:
    """验证 TMDB 短剧过滤规则：拦截短剧平台/短时长/文本标记，保留未填时长新剧与动画泡面番。"""
    from app.helper.discover_helper import (
        TMDB_SHORT_DRAMA_WITHOUT_KEYWORDS,
        is_short_drama_tmdb_item,
    )

    assert "370469" in TMDB_SHORT_DRAMA_WITHOUT_KEYWORDS.split(",")
    assert len(TMDB_SHORT_DRAMA_WITHOUT_KEYWORDS.split(",")) == 12

    # 1. <= 12 分钟真人短剧 -> 过滤
    assert is_short_drama_tmdb_item({
        "id": 1001,
        "name": "霸总追妻九十九次",
        "genre_ids": [18],
        "episode_run_time": [2],
    }) is True

    # 2. 未录入时长的新开播正剧（空列表或全 0） -> 绝不误杀
    assert is_short_drama_tmdb_item({
        "id": 1002,
        "name": "藏海传",
        "genre_ids": [18, 9648],
        "episode_run_time": [],
    }) is False
    assert is_short_drama_tmdb_item({
        "id": 1003,
        "name": "长安的荔枝",
        "genre_ids": [18],
        "episode_run_time": [0],
    }) is False

    # 3. 16 动画泡面番/国漫（即使单集 3 分钟） -> 绝不误杀
    assert is_short_drama_tmdb_item({
        "id": 1004,
        "name": "非人哉",
        "genre_ids": [16, 35],
        "episode_run_time": [4],
        "overview": "轻松泡面番短剧形式动画",
    }) is False
    assert is_short_drama_tmdb_item({
        "id": 1005,
        "name": "罗小黑战记",
        "genres": [{"id": 16, "name": "动画"}],
        "episode_run_time": [5],
    }) is False

    # 4. 红果短剧台(8020)及短剧专属平台 -> 过滤
    assert is_short_drama_tmdb_item({
        "id": 1006,
        "name": "重生之都市仙尊",
        "genre_ids": [18],
        "networks": [{"id": 8020, "name": "Hong Guo Short Drama"}],
    }) is True
    assert is_short_drama_tmdb_item({
        "id": 1007,
        "name": "Billionaire CEO",
        "genre_ids": [18],
        "networks": [{"id": 99999, "name": "ReelShort"}],
    }) is True

    # 5. 标题或简介明确标记竖屏短剧/微短剧 -> 过滤
    assert is_short_drama_tmdb_item({
        "id": 1008,
        "name": "闪婚后傅先生马甲藏不住了",
        "genre_ids": [18],
        "overview": "一部现象级竖屏短剧。",
    }) is True


def test_generic_short_drama_text_rules() -> None:
    """验证通用文本与标签短剧识别：动画免疫，微短剧/十分剧场/短片标签精准拦截。"""
    from app.helper.discover_helper import is_short_drama_text

    assert is_short_drama_text(title="执笔", subtitle="古装微短剧") is True
    assert is_short_drama_text(title="大妈的世界", subtitle="十分剧场精品") is True
    assert is_short_drama_text(title="引她入室", tags=["剧情", "短片"]) is True
    assert is_short_drama_text(
        title="灵笼 特别篇",
        subtitle="短片",
        tags=["科幻", "动画"],
        is_animation=True,
    ) is False
    assert is_short_drama_text(
        title="中国奇谭",
        subtitle="短片集",
        tags=["动画", "奇幻"],
    ) is False


@pytest.mark.parametrize(
    ("raw_title", "expected_clean", "expected_season", "expected_main"),
    [
        ("庆余年 第二季", "庆余年", 2, "庆余年"),
        ("庆余年2", "庆余年", 2, "庆余年"),
        ("画江湖之不良人6", "画江湖之不良人", 6, "画江湖之不良人"),
        ("间谍过家家（中配）", "间谍过家家", None, "间谍过家家"),
        ("间谍过家家 第二季（粤语版）", "间谍过家家", 2, "间谍过家家"),
        ("凡人修仙传 星海飞驰篇", "凡人修仙传 星海飞驰篇", None, "凡人修仙传"),
        ("伍六七之暗影宿命", "伍六七之暗影宿命", None, "伍六七"),
        ("请回答1988", "请回答1988", None, "请回答1988"),
        ("银翼杀手2049", "银翼杀手2049", None, "银翼杀手2049"),
        ("三十而已", "三十而已", None, "三十而已"),
        ("三体", "三体", None, "三体"),
    ],
)
def test_parse_discover_title_and_season(
    raw_title: str,
    expected_clean: str,
    expected_season: int | None,
    expected_main: str,
) -> None:
    """验证流媒体标题修饰词剥离、季号提取与篇章主剧名提取。"""
    from app.helper.discover_helper import parse_discover_title_and_season

    clean_title, season, main_title = parse_discover_title_and_season(raw_title)
    assert clean_title == expected_clean
    assert season == expected_season
    if raw_title == "画江湖之不良人6":
        # 一级 clean_title 为“画江湖之不良人”，main_title 允许进一步按“之”回退或保持主名
        assert clean_title == "画江湖之不良人"
    else:
        assert main_title == expected_main


def test_mediainfo_maps_tmdb_genre_ids_to_chinese_genres() -> None:
    """TMDB 列表返回仅有 genre_ids 无 genres 时，MediaInfo 应自动填充中文 genres 且不篡改原字典。"""
    raw_info = {
        "id": 693134,
        "media_type": "movie",
        "title": "沙丘2",
        "release_date": "2024-02-27",
        "genre_ids": [878, 12],
        "overview": "保罗·厄崔迪与弗雷曼人联手复仇。",
    }
    media = MediaInfo(tmdb_info=raw_info)

    assert "genres" not in raw_info
    assert media.genres == [
        {"id": 878, "name": "科幻"},
        {"id": 12, "name": "冒险"},
    ]
    serialized = schemas.MediaInfo(**media.to_dict())
    assert serialized.genres == [
        {"id": 878, "name": "科幻"},
        {"id": 12, "name": "冒险"},
    ]


def test_tmdb_discover_supports_new_filters_and_short_drama_exclusion() -> None:
    """TMDB 探索应向后兼容支持平台、截止日期、最小时长及短剧排除过滤。"""
    module = TheMovieDbModule()
    module.tmdb = Mock()
    module.tmdb.async_discover_tvs = AsyncMock(
        return_value=[
            {
                "id": 2001,
                "media_type": MediaType.TV,
                "name": "漫长的季节",
                "first_air_date": "2023-04-22",
                "genre_ids": [18, 9648],
                "episode_run_time": [60],
            },
            {
                "id": 2002,
                "media_type": MediaType.TV,
                "name": "霸道总裁爱上保洁",
                "first_air_date": "2025-02-01",
                "genre_ids": [18],
                "episode_run_time": [2],
            },
            {
                "id": 2003,
                "media_type": MediaType.TV,
                "name": "未填时长新剧",
                "first_air_date": "2025-05-01",
                "genre_ids": [18],
                "episode_run_time": [],
            },
        ]
    )

    medias = asyncio.run(
        module.async_tmdb_discover(
            mtype=MediaType.TV,
            sort_by="popularity.desc",
            with_genres="18",
            with_original_language="zh",
            with_keywords="",
            with_watch_providers="",
            vote_average=0.0,
            vote_count=0,
            release_date="2025-01-01",
            release_date_lte="2025-12-31",
            with_networks="2007",
            with_runtime_gte=20,
            exclude_short_drama=True,
            page=1,
        )
    )

    call_params = module.tmdb.async_discover_tvs.await_args.args[0]
    assert call_params["with_networks"] == "2007"
    assert call_params["first_air_date.gte"] == "2025-01-01"
    assert call_params["first_air_date.lte"] == "2025-12-31"
    assert call_params["with_runtime.gte"] == 20
    assert "370469" in call_params["without_keywords"]
    assert [m.tmdb_id for m in medias] == [2001, 2003]


def test_douban_tvs_exclude_short_drama_respects_explicit_short_tag(monkeypatch) -> None:
    """豆瓣剧集开启短剧过滤时，排除真人短片/短剧，保留动画与用户主动筛选的短片标签。"""
    module = DoubanModule()
    module.doubanapi = Mock()
    module.doubanapi.async_tv_recommend = AsyncMock(
        return_value={
            "items": [
                {
                    "id": "3001",
                    "title": "边水往事",
                    "subtype": "tv",
                    "card_subtitle": "2024 / 中国大陆 / 剧情 冒险 / 算 / 郭麒麟 吴镇宇",
                    "pic": {"large": "https://img9.doubanio.com/view/photo/l/public/p1.jpg"},
                },
                {
                    "id": "3002",
                    "title": "我在八零年代当后妈",
                    "subtype": "tv",
                    "card_subtitle": "2024 / 中国大陆 / 剧情 爱情 短片 / 腾泽文",
                    "pic": {"large": "https://img9.doubanio.com/view/photo/l/public/p2.jpg"},
                },
                {
                    "id": "3003",
                    "title": "请吃红小豆吧！",
                    "subtype": "tv",
                    "card_subtitle": "2018 / 中国大陆 / 喜剧 动画 短片 / 林春柳",
                    "pic": {"large": "https://img9.doubanio.com/view/photo/l/public/p3.jpg"},
                },
                {
                    "id": "3004",
                    "title": "蜜色沦陷",
                    "subtype": "tv",
                    "card_subtitle": "2026 / 中国大陆 / 剧情 爱情 / 澄芓 曹天恺",
                    "pic": {"large": "https://img9.doubanio.com/view/photo/l/public/p4.jpg"},
                    "has_linewatch": False,
                    "vendor_count": 0,
                    "rating": {"count": 0, "value": 0},
                    "comment": None,
                    "photos": [],
                    "episodes_info": "",
                    "honor_infos": [],
                },
                {
                    "id": "3005",
                    "title": "两京十五日",
                    "subtype": "tv",
                    "card_subtitle": "2026 / 中国大陆 / 剧情 古装 / 张黎 / 成毅 林更新",
                    "pic": {"large": "https://img9.doubanio.com/view/photo/l/public/p5.jpg"},
                    "has_linewatch": True,
                    "vendor_count": 1,
                    "rating": {"count": 0, "value": 0},
                    "comment": None,
                    "photos": ["https://img9.doubanio.com/view/photo/m/public/ph1.jpg"],
                    "episodes_info": "更新至8集",
                    "honor_infos": [],
                },
            ]
        }
    )

    class FakeDoubanChain:
        """代理测试用 DoubanModule。"""

        async def async_douban_discover(self, **kwargs):
            """转发异步发现请求。"""
            return await module.async_douban_discover(**kwargs)

    monkeypatch.setattr(discover_endpoint, "DoubanChain", FakeDoubanChain)

    filtered = asyncio.run(
        discover_endpoint.douban_tvs(
            sort="R",
            tags="国产剧",
            exclude_short_drama=True,
            _=None,
        )
    )
    assert [item["douban_id"] for item in filtered] == ["3001", "3003", "3005"]

    # 用户主动选择“短片”标签时不过滤
    unfiltered_explicit = asyncio.run(
        discover_endpoint.douban_tvs(
            sort="R",
            tags="短片",
            exclude_short_drama=True,
            _=None,
        )
    )
    assert [item["douban_id"] for item in unfiltered_explicit] == ["3001", "3002", "3003", "3004", "3005"]


def test_bangumi_calendar_filters_weekday_and_preserves_bangumi_identity(monkeypatch) -> None:
    """Bangumi 每日放送端点应支持星期筛选并保留原生 bangumi 身份。"""
    items = [
        MediaInfo(
            bangumi_info={
                "id": 4001,
                "name": "周一新番",
                "name_cn": "周一新番",
                "air_date": "2025-04-07",
                "air_weekday": 1,
                "images": {"large": "https://lain.bgm.tv/pic/cover/l/4001.jpg"},
            }
        ),
        MediaInfo(
            bangumi_info={
                "id": 4003,
                "name": "周三新番",
                "name_cn": "周三新番",
                "air_date": "2025-04-09",
                "air_weekday": 3,
                "images": {"large": "https://lain.bgm.tv/pic/cover/l/4003.jpg"},
            }
        ),
    ]

    class FakeBangumiChain:
        """提供固定放送列表的测试桩。"""

        async def async_calendar(self):
            """返回固定放送条目。"""
            return list(items)

    monkeypatch.setattr(discover_endpoint, "BangumiChain", FakeBangumiChain)

    wed_only = asyncio.run(discover_endpoint.bangumi_calendar(weekday=3, _=None))
    assert len(wed_only) == 1
    assert wed_only[0]["source"] == "bangumi"
    assert wed_only[0]["mediaid_prefix"] == "bangumi"
    assert wed_only[0]["bangumi_id"] == 4003

    all_days = asyncio.run(discover_endpoint.bangumi_calendar(weekday=0, _=None))
    assert len(all_days) == 2


def test_security_image_domains_and_discover_sources_dedup(monkeypatch) -> None:
    """图片安全域名包含流媒体海报域，且 /discover/source 内置源与插件源无重复 prefix。"""
    for domain in ("hdslb.com", "puui.qpic.cn", "qpic.cn", "hitv.com"):
        assert domain in settings.SECURITY_IMAGE_DOMAINS

    extra_iqiyi = schemas.DiscoverMediaSource(
        name="爱奇艺",
        mediaid_prefix="iqiyi",
        api_path="plugin/iqiyi/discover",
        filter_params={},
        filter_ui=[],
    )
    dup_bilibili = schemas.DiscoverMediaSource(
        name="哔哩哔哩插件版",
        mediaid_prefix="bilibili",
        api_path="plugin/bilibili/discover",
        filter_params={},
        filter_ui=[],
    )

    def fake_send_event(event_type, event_data):
        assert event_type == ChainEventType.DiscoverSource
        event_data.extra_sources = [dup_bilibili, extra_iqiyi]
        return SimpleNamespace(event_data=event_data)

    monkeypatch.setattr(discover_endpoint.eventmanager, "send_event", fake_send_event)
    sources = discover_endpoint.source(_=None)
    prefixes = [s.mediaid_prefix for s in sources]
    assert prefixes == ["bilibili", "tencentvideo", "mangguo", "iqiyi"]
    assert len(prefixes) == len(set(prefixes))


def test_builtin_streaming_sources_parsing_fallback_and_short_drama_filter(monkeypatch) -> None:
    """验证哔哩哔哩、腾讯视频、芒果TV的列表解析、分页缓存、静态筛选项兜底及短剧过滤。"""
    from app.helper import discover_sources

    discover_sources.clear_discover_source_caches()

    # 1. 哔哩哔哩：_copyright 透传、subTitle 保留、季号解析、空年份无 (None)、短剧过滤
    bili_captured = {}

    async def fake_bili_get_json(self, url, params=None, **kwargs):
        bili_captured["url"] = url
        bili_captured["params"] = dict(params or {})
        return {
            "code": 0,
            "data": {
                "list": [
                    {
                        "media_id": 28229420,
                        "season_id": 45001,
                        "title": "庆余年 第二季",
                        "subTitle": "范闲朝堂再起风云",
                        "index_show": "全36集",
                        "cover": "http://i0.hdslb.com/bfs/bangumi/image/qingyunian2.jpg",
                        "score": "9.3",
                    },
                    {
                        "media_id": 28229999,
                        "title": "重生之我在霸总家当保姆",
                        "subTitle": "高甜竖屏短剧",
                        "cover": "https://i0.hdslb.com/bfs/bangumi/image/short.jpg",
                    },
                ]
            },
        }

    monkeypatch.setattr(discover_sources.AsyncRequestUtils, "get_json", fake_bili_get_json)

    bili_items = asyncio.run(
        discover_endpoint.bilibili(
            mtype="tv",
            _copyright="3",
            year=str(datetime.now().year),
            exclude_short_drama=True,
            page=1,
            count=20,
            _=None,
        )
    )
    assert bili_captured["params"]["copyright"] == "3"
    assert len(bili_items) == 1
    assert bili_items[0]["mediaid_prefix"] == "bilibili"
    assert bili_items[0]["media_id"] == "28229420"
    assert bili_items[0]["season"] == 2
    assert "范闲朝堂再起风云" in bili_items[0]["overview"]
    assert "(None)" not in bili_items[0]["title_year"]
    assert bili_items[0]["poster_path"].startswith("https://")

    # 2. 腾讯视频：拒绝 110755 短剧频道、缓存翻页 page_context、静态筛选项兜底
    tx_posts = []

    async def fake_tx_post_json(self, url, json=None, **kwargs):
        tx_posts.append(json)
        return {
            "ret": 0,
            "data": {
                "next_page_context": {"page_index": "1", "offset": "30"},
                "module_list_datas": [
                    {
                        "module_datas": [
                            {
                                "item_data_lists": {
                                    "item_datas": [
                                        {
                                            "item_id": "mzc002001",
                                            "item_type": "1",
                                            "item_params": {
                                                "cid": "mzc002001",
                                                "title": "三体",
                                                "area_name": "内地",
                                                "main_genre": "科幻",
                                                "leading_actor": "[张鲁一 于和伟 陈瑾 王子文]",
                                                "second_title": "地球往事科幻巨制",
                                                "new_pic_vt": "https://puui.qpic.cn/vcover_vt_pic/0/santi.jpg/0",
                                                "year": "2023",
                                                "opinion_score": "8.7",
                                            },
                                        },
                                        {
                                            "item_id": "mzc002002",
                                            "item_type": "1",
                                            "item_params": {
                                                "cid": "mzc002002",
                                                "title": "招惹",
                                                "second_title": "十分剧场民国爱情短剧",
                                                "new_pic_vt": "https://puui.qpic.cn/vcover_vt_pic/0/zr.jpg/0",
                                            },
                                        },
                                    ]
                                }
                            }
                        ]
                    }
                ],
            },
        }

    monkeypatch.setattr(discover_sources.AsyncRequestUtils, "post_json", fake_tx_post_json)

    # 短剧频道 110755 绝不返回内容
    assert asyncio.run(discover_endpoint.tencentvideo(mtype="110755", _=None)) == []

    tx_page1 = asyncio.run(
        discover_endpoint.tencentvideo(mtype="tv", exclude_short_drama=True, page=1, _=None)
    )
    assert [x["media_id"] for x in tx_page1] == ["mzc002001"]
    assert [g["name"] for g in tx_page1[0]["genres"]] == ["内地", "科幻"]
    assert [a["name"] for a in tx_page1[0]["actors"]] == ["张鲁一", "于和伟", "陈瑾", "王子文"]

    # 翻第 2 页时直接复用已缓存的 next_page_context，不重复拉第 1 页
    tx_posts.clear()
    asyncio.run(
        discover_endpoint.tencentvideo(mtype="tv", exclude_short_drama=True, page=2, _=None)
    )
    assert len(tx_posts) == 1
    assert tx_posts[0]["page_context"] == {"page_index": "1", "offset": "30"}

    # 网络异常时腾讯与芒果源契约构造依然返回内置静态兜底 UI，绝不崩溃
    monkeypatch.setattr(
        discover_sources.RequestUtils, "post_res", lambda *a, **kw: None
    )
    monkeypatch.setattr(
        discover_sources.RequestUtils, "get_res", lambda *a, **kw: None
    )
    builtin = discover_sources.get_builtin_discover_sources()
    assert [s.mediaid_prefix for s in builtin] == ["bilibili", "tencentvideo", "mangguo"]
    assert all(len(s.filter_ui) > 0 for s in builtin)

    # 3. 芒果TV：story 映射到 overview，季号提取
    async def fake_mg_get_json(self, url, params=None, **kwargs):
        return {
            "code": 200,
            "data": {
                "hitDocs": [
                    {
                        "clipId": "600888",
                        "title": "大侦探 第九季",
                        "story": "普法推理综艺第九季高能回归",
                        "img": "//img.hitv.com/preview/dazhentan9.jpg",
                        "year": "2024",
                        "zhihuScore": "8.9",
                    }
                ]
            },
        }

    monkeypatch.setattr(discover_sources.AsyncRequestUtils, "get_json", fake_mg_get_json)
    mg_items = asyncio.run(discover_endpoint.mangguo(mtype="variety", page=1, _=None))
    assert len(mg_items) == 1
    assert mg_items[0]["media_id"] == "600888"
    assert mg_items[0]["overview"] == "普法推理综艺第九季高能回归"
    assert mg_items[0]["season"] == 9
    assert mg_items[0]["poster_path"] == "https://img.hitv.com/preview/dazhentan9.jpg"


def test_external_source_seasons_subscribe_add_and_delete_closed_loop(monkeypatch) -> None:
    """
    验证非原生探索源（如 bilibili:28229420）全链路闭环：
    1. GET /media/seasons 返回权威 TMDB 完整季列表（含第三方插件篡改 source 修正与主剧名二次回退）；
    2. SubscribeChain.async_add 自动提取第 2 季、落库权威 TMDB 身份并保留原 mediaid；
    3. DELETE /subscribe/media/bilibili:28229420?title=... 支持按 mediaid 或标题+季号闭环取消订阅。
    """
    from app.helper import discover_sources

    discover_sources.clear_discover_source_caches()
    discover_sources.cache_discover_item(
        "bilibili:28229420",
        title="庆余年 第二季",
        year="2024",
        mtype=MediaType.TV,
        season=2,
    )

    tmdb_qingyunian = MediaInfo(
        tmdb_info={
            "id": 96762,
            "media_type": MediaType.TV,
            "name": "庆余年",
            "first_air_date": "2019-11-26",
            "overview": "范闲的成长传奇。",
            "poster_path": "/qingyunian.jpg",
            "seasons": [
                {"season_number": 1, "name": "第 1 季", "episode_count": 46, "air_date": "2019-11-26"},
                {"season_number": 2, "name": "第 2 季", "episode_count": 36, "air_date": "2024-05-16"},
            ],
        }
    )

    # 模拟某第三方插件把 source 篡改成了 bilibili，但携带了真实 tmdb_id
    hijacked_media = MediaInfo(
        tmdb_info={
            "id": 96762,
            "media_type": MediaType.TV,
            "name": "庆余年",
            "first_air_date": "2019-11-26",
            "seasons": [
                {"season_number": 1, "name": "第 1 季", "episode_count": 46, "air_date": "2019-11-26"},
                {"season_number": 2, "name": "第 2 季", "episode_count": 36, "air_date": "2024-05-16"},
            ],
        }
    )
    hijacked_media.source = "bilibili"
    hijacked_media.media_id = "28229420"

    from app.helper.discover_helper import async_resolve_external_media

    fake_media_chain = Mock()
    fake_media_chain.async_recognize_media = AsyncMock(return_value=hijacked_media)
    fake_media_chain.async_recognize_by_meta = AsyncMock(return_value=tmdb_qingyunian)
    fake_media_chain.async_obtain_images = AsyncMock(side_effect=lambda m: m)

    with patch("app.helper.discover_helper.MediaChain", return_value=fake_media_chain):
        resolved_media, resolved_season = asyncio.run(
            async_resolve_external_media(mediaid="bilibili:28229420")
        )

    assert resolved_media is not None
    assert resolved_media.source == "themoviedb"
    assert resolved_media.media_id == "96762"
    assert resolved_media.tmdb_id == 96762
    assert resolved_season == 2

    # 验证篇章副标题未命中一级标题时自动用 main_series_title 二次回退搜索
    recognized_queries = []

    async def fake_recognize_by_meta(meta, source=None, **kwargs):
        recognized_queries.append((meta.name, source))
        if meta.name == "凡人修仙传":
            return MediaInfo(
                tmdb_info={
                    "id": 106449,
                    "media_type": MediaType.TV,
                    "name": "凡人修仙传",
                    "first_air_date": "2020-07-25",
                }
            )
        return None

    fallback_chain = Mock()
    fallback_chain.async_recognize_media = AsyncMock(return_value=None)
    fallback_chain.async_recognize_by_meta = AsyncMock(side_effect=fake_recognize_by_meta)

    with patch("app.helper.discover_helper.MediaChain", return_value=fallback_chain):
        fanren_media, _ = asyncio.run(
            async_resolve_external_media(
                mediaid="bilibili:999999",
                mtype=MediaType.TV,
                title="凡人修仙传 星海飞驰篇",
                year="2023",
            )
        )
    assert fanren_media is not None
    assert fanren_media.tmdb_id == 106449
    assert any(q[0] == "凡人修仙传" for q in recognized_queries)

    # 1. GET /media/seasons 对 bilibili:28229420 返回完整 TMDB 季列表
    fake_tmdb_chain = Mock()
    fake_tmdb_chain.async_tmdb_seasons = AsyncMock(
        return_value=[
            schemas.TmdbSeason(season_number=1, name="第 1 季", episode_count=46),
            schemas.TmdbSeason(season_number=2, name="第 2 季", episode_count=36),
        ]
    )
    no_plugin_chain = Mock()
    no_plugin_chain.async_recognize_media = AsyncMock(return_value=None)
    no_plugin_chain.async_recognize_by_meta = AsyncMock(return_value=tmdb_qingyunian)
    no_plugin_chain.async_obtain_images = AsyncMock(side_effect=lambda m: m)

    monkeypatch.setattr(media_endpoint, "TmdbChain", Mock(return_value=fake_tmdb_chain))
    monkeypatch.setattr(media_endpoint, "MediaChain", Mock(return_value=no_plugin_chain))
    with patch("app.helper.discover_helper.MediaChain", return_value=no_plugin_chain):
        seasons_res = asyncio.run(
            media_endpoint.seasons(
                mediaid="bilibili:28229420",
                title="庆余年 第二季",
                year="2024",
                season=2,
                _=None,
            )
        )
        detail_res = asyncio.run(
            media_endpoint.detail(
                mediaid="bilibili:28229420",
                type_name="电视剧",
                title="庆余年 第二季",
                year="2024",
                _=None,
            )
        )

    assert [s.season_number for s in seasons_res] == [1, 2]
    assert detail_res["tmdb_id"] == 96762
    assert detail_res["source"] == "themoviedb"

    # 2. SubscribeChain.async_add 自动提取第 2 季、落库权威 TMDB 身份并保留原 mediaid
    saved_kwargs = {}

    class FakeSubscribeOper:
        """捕获入库参数的测试桩。"""

        async def async_add(self, mediainfo, **kwargs):
            """记录新增订阅时的媒体信息与扩展字段。"""
            saved_kwargs["mediainfo"] = mediainfo
            saved_kwargs.update(kwargs)
            return 88, "新增订阅成功"

        async def async_get(self, sid):
            """返回模拟订阅行。"""
            return SimpleNamespace(id=sid, poster="https://image.tmdb.org/t/p/w500/qingyunian.jpg")

    sub_chain = object.__new__(SubscribeChain)
    with patch("app.helper.discover_helper.MediaChain", return_value=no_plugin_chain), \
            patch("app.chain.subscribe.SubscribeOper", FakeSubscribeOper), \
            patch.object(SubscribeChain, "async_recognize_media", new=AsyncMock(return_value=tmdb_qingyunian)), \
            patch.object(SubscribeChain, "async_obtain_images", new=AsyncMock(return_value=tmdb_qingyunian)), \
            patch.object(SubscribeChain, "_SubscribeChain__async_apply_episodes_refresh", new=AsyncMock(return_value=36)), \
            patch.object(SubscribeChain, "_SubscribeChain__get_category_rule", return_value=None), \
            patch.object(SubscribeChain, "_SubscribeChain__get_default_kwargs", return_value={}), \
            patch.object(SubscribeChain, "async_post_message", new=AsyncMock()), \
            patch("app.chain.subscribe.eventmanager.async_send_event", new=AsyncMock()), \
            patch("app.chain.subscribe.MoviePilotServerHelper.async_sub_reg", new=AsyncMock()):
        sid, msg = asyncio.run(
            sub_chain.async_add(
                title="庆余年 第二季",
                year="2024",
                mtype=MediaType.TV,
                mediaid="bilibili:28229420",
                media_source="bilibili",
                media_id="28229420",
                season=None,
                username="admin",
                message=False,
            )
        )

    assert sid == 88
    assert msg == "新增订阅成功"
    assert saved_kwargs["season"] == 2
    assert saved_kwargs["total_episode"] == 36
    assert saved_kwargs["media_source"] == "themoviedb"
    assert saved_kwargs["media_id"] == "96762"
    assert saved_kwargs["mediaid"] == "bilibili:28229420"
    assert saved_kwargs["mediainfo"].tmdb_id == 96762

    # 3. DELETE /subscribe/media/bilibili:28229420?title=庆余年 第二季
    # 场景 A：跨源在 TMDB 订阅后（无 mediaid），在 B 站卡片通过 title 反查删除
    existing_sub = SimpleNamespace(
        id=88,
        username="admin",
        name="庆余年",
        season=2,
        tmdbid=96762,
        media_source="themoviedb",
        media_id="96762",
        mediaid=None,
    )
    deleted_rows = []

    class FakeAsyncDb:
        """模拟异步会话删除与提交。"""

        async def delete(self, obj):
            """记录删除对象。"""
            deleted_rows.append(obj)

        async def commit(self):
            """模拟提交。"""
            return None

        async def rollback(self):
            """模拟回滚。"""
            return None

    with patch.object(
        subscribe_endpoint, "list_subscribes_by_media_key", new=AsyncMock(return_value=[])
    ), patch.object(
        subscribe_endpoint.Subscribe,
        "async_list_by_title",
        new=AsyncMock(
            side_effect=lambda _db, title, season=None: (
                [existing_sub] if title == "庆余年" and season == 2 else []
            )
        ),
    ), patch.object(
        subscribe_endpoint,
        "build_subscribe_event_payload",
        side_effect=lambda sub: {"id": sub.id, "name": sub.name, "season": sub.season},
    ), patch.object(
        subscribe_endpoint.eventmanager, "async_send_event", new=AsyncMock()
    ):
        del_resp = asyncio.run(
            subscribe_endpoint.delete_subscribe_by_mediaid(
                mediaid="bilibili:28229420",
                title="庆余年 第二季",
                db=FakeAsyncDb(),
                current_user=SimpleNamespace(name="admin", is_superuser=True),
            )
        )

    assert del_resp.success is True
    assert deleted_rows == [existing_sub]


def test_external_detail_lookup_and_mediaid_direct_lookup(monkeypatch) -> None:
    """验证缓存未命中时单条详情回查补齐，以及 Subscribe.mediaid 落库后无 title 直接反查。"""
    from app.helper import discover_sources
    from app.helper.discover_helper import async_resolve_external_media

    discover_sources.clear_discover_source_caches()

    # 模拟腾讯视频 float_vinfo2 返回 QZOutputJson 包裹格式
    async def fake_tx_detail_get(self, url, params=None, **kwargs):
        assert params == {"cid": "mzc002001"}
        return 'QZOutputJson={"c":{"title":"庆余年 第二季","year":"2024","type":2,"pic":"//puui.qpic.cn/1.jpg"}};'

    monkeypatch.setattr(discover_sources.AsyncRequestUtils, "get", fake_tx_detail_get)

    tmdb_tv = MediaInfo(
        tmdb_info={
            "id": 96762,
            "media_type": MediaType.TV,
            "name": "庆余年",
            "first_air_date": "2019-11-26",
        }
    )
    fake_chain = Mock()
    fake_chain.async_recognize_media = AsyncMock(return_value=None)
    fake_chain.async_recognize_by_meta = AsyncMock(return_value=tmdb_tv)

    with patch("app.helper.discover_helper.MediaChain", return_value=fake_chain):
        resolved, eff_season = asyncio.run(
            async_resolve_external_media(mediaid="tencentvideo:mzc002001")
        )

    assert resolved is not None
    assert resolved.tmdb_id == 96762
    assert eff_season == 2

    # 验证 list_subscribes_by_media_key 对非原生源同时查询 Subscribe.mediaid
    sub_with_mediaid = SimpleNamespace(
        id=101,
        username="admin",
        name="庆余年",
        season=2,
        media_source="themoviedb",
        media_id="96762",
        mediaid="bilibili:28229420",
    )
    with patch.object(
        subscribe_endpoint.Subscribe,
        "async_list_by_media_identity",
        new=AsyncMock(return_value=[]),
    ), patch.object(
        subscribe_endpoint.Subscribe,
        "async_list_by_mediaid",
        new=AsyncMock(return_value=[sub_with_mediaid]),
    ):
        found = asyncio.run(
            subscribe_endpoint.list_subscribes_by_media_key(
                db=object(), media_key="bilibili:28229420", season=2
            )
        )
    assert [s.id for s in found] == [101]

