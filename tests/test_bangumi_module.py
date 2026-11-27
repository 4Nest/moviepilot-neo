import asyncio
from unittest.mock import MagicMock, patch

from app.core.config import settings
from app.modules.bangumi import BangumiModule
from app.modules.bangumi.bangumi import BangumiApi


class _FakeBangumiApi:
    """
    测试用Bangumi API客户端。
    """

    def __init__(self, personinfo: dict):
        """
        初始化测试用人物详情数据。
        :param personinfo: 人物详情接口返回数据
        """
        self.personinfo = personinfo

    def person_detail(self, person_id: int) -> dict:
        """
        返回同步人物详情数据。
        :param person_id: 人物ID
        :return: 人物详情接口返回数据
        """
        return self.personinfo

    async def async_person_detail(self, person_id: int) -> dict:
        """
        返回异步人物详情数据。
        :param person_id: 人物ID
        :return: 人物详情接口返回数据
        """
        return self.personinfo


def test_bangumi_person_detail_normalizes_numeric_birthday():
    """
    Bangumi同步人物详情应兼容数字生日字段。
    """
    module = BangumiModule()
    module.bangumiapi = _FakeBangumiApi({
        "id": 1001,
        "name": "测试人物",
        "images": {"large": "https://example.com/person.jpg"},
        "summary": "测试简介",
        "birth_day": 22,
        "gender": 1,
    })

    person = module.bangumi_person_detail(1001)

    assert person.birthday == "22"


def test_async_bangumi_person_detail_normalizes_numeric_birthday():
    """
    Bangumi异步人物详情应兼容数字生日字段。
    """
    module = BangumiModule()
    module.bangumiapi = _FakeBangumiApi({
        "id": 1002,
        "name": "测试异步人物",
        "images": {"large": "https://example.com/async-person.jpg"},
        "summary": "测试异步简介",
        "birth_day": 19,
        "gender": 2,
    })

    person = asyncio.run(module.async_bangumi_person_detail(1002))

    assert person.birthday == "19"



def test_bangumi_module_watches_and_passes_image_domain(monkeypatch):
    """
    模块应监听图片代理配置，并把图片地址与数据地址一起交给客户端。
    """
    module = BangumiModule()
    monkeypatch.setattr(settings, "BANGUMI_PROXY_ENABLE", True)
    monkeypatch.setattr(settings, "BANGUMI_API_DOMAIN", "https://bgma.example")
    monkeypatch.setattr(settings, "BANGUMI_IMAGE_DOMAIN", "bgmp.example/mirror/")

    with patch("app.modules.bangumi.BangumiApi") as api_class:
        module.init_module()

    assert api_class.call_args.kwargs["base_url"] == "https://bgma.example"
    assert api_class.call_args.kwargs["image_domain"] == "bgmp.example/mirror/"
    assert "BANGUMI_IMAGE_DOMAIN" in BangumiModule.CONFIG_WATCH
    assert "BANGUMI_API_DOMAIN" in BangumiModule.CONFIG_WATCH
    assert "BANGUMI_PROXY_ENABLE" in BangumiModule.CONFIG_WATCH

    monkeypatch.setattr(settings, "BANGUMI_PROXY_ENABLE", False)
    with patch("app.modules.bangumi.BangumiApi") as api_class:
        module.init_module()

    assert api_class.call_args.kwargs["base_url"] == ""
    assert api_class.call_args.kwargs["image_domain"] == ""


def test_bangumi_api_rewrites_image_hosts_when_image_domain_configured() -> None:
    """
    配置图片代理后，响应里的官方图片域名被替换，页面链接与其它主机保持原样。
    """
    response = MagicMock(status_code=200)
    response.json.return_value = {
        "id": 502123,
        "url": "http://bgm.tv/subject/502123",
        "images": {
            "large": "https://lain.bgm.tv/pic/cover/l/e1/6f/502123_IiVB6.jpg",
            "small": "http://lain.bgm.tv/r/200/pic/cover/l/e1/6f/502123_IiVB6.jpg",
            "spoofer": "http://lain.bgm.tv.evil.example/cover.jpg",
            "external": "https://cdn.example/cover.jpg",
            "missing": None,
        },
        "actors": [{"image": "https://lain.bgm.tv/pic/crt/l/4c/7c/2000_crt_9baR9.jpg"}],
    }
    api = BangumiApi(base_url="", image_domain="https://bgmp.example:8443/mirror/")
    api._req.get_res = MagicMock(return_value=response)

    try:
        result = api.detail(502123)
    finally:
        api.clear_cache()
        api.close()

    assert result["images"] == {
        "large": "https://bgmp.example:8443/mirror/pic/cover/l/e1/6f/502123_IiVB6.jpg",
        "small": "https://bgmp.example:8443/mirror/r/200/pic/cover/l/e1/6f/502123_IiVB6.jpg",
        "spoofer": "http://lain.bgm.tv.evil.example/cover.jpg",
        "external": "https://cdn.example/cover.jpg",
        "missing": None,
    }
    assert result["actors"][0]["image"] == (
        "https://bgmp.example:8443/mirror/pic/crt/l/4c/7c/2000_crt_9baR9.jpg"
    )
    assert result["url"] == "http://bgm.tv/subject/502123"
    assert response.json.return_value["images"]["large"] == (
        "https://lain.bgm.tv/pic/cover/l/e1/6f/502123_IiVB6.jpg"
    )
    assert api._normalize_images(502123) == 502123


def test_bangumi_api_without_image_domain_keeps_payload_identity() -> None:
    """
    未配置图片代理时不改写任何响应节点，图片地址保持官方域名。
    """
    api = BangumiApi(base_url="", image_domain="")
    payload = {"images": {"large": "http://lain.bgm.tv/pic/cover/l/1.jpg"}}

    try:
        assert api.image_proxy is None
        assert api._normalize_images(payload) is payload
    finally:
        api.close()


def test_bangumi_image_proxy_distinguishes_three_modes() -> None:
    """
    图片代理地址按尾部斜杠与查询串区分替换主机、查询拼接和路径拼接。
    """
    normalize = BangumiApi._normalize_image_proxy

    assert (normalize("bgmp.example/").mode, normalize("bgmp.example/").prefix) == (
        "host",
        "https://bgmp.example",
    )
    assert (normalize("https://bgmp.example").mode, normalize("https://bgmp.example").prefix) == (
        "path",
        "https://bgmp.example",
    )
    assert (
        normalize("https://worker.example/?url=").mode,
        normalize("https://worker.example/?url=").prefix,
    ) == ("query", "https://worker.example/?url=")
    assert (
        normalize("HTTP://CDN.Example:8080/mirror/").mode,
        normalize("HTTP://CDN.Example:8080/mirror/").prefix,
    ) == ("host", "HTTP://CDN.Example:8080/mirror")
    assert normalize("https://user:pw@bgmp.example") is None
    assert normalize("ftp://bgmp.example") is None
    assert normalize("//bgmp.example") is None
    assert normalize("http://[bad") is None
    assert normalize("https://bgmp.example/#frag") is None
    assert normalize("") is None
    assert normalize(None) is None


def test_bangumi_image_url_replacement_modes_and_idempotency() -> None:
    """
    验证 host / query / path 三种图片代理拼接方式及幂等性。
    """
    proxy = BangumiApi._normalize_image_proxy("bgmp.example/")

    assert BangumiApi._normalize_image_url(
        "http://lain.bgm.tv/pic/cover/c/1.jpg", proxy
    ) == "https://bgmp.example/pic/cover/c/1.jpg"
    assert BangumiApi._normalize_image_url(
        "https://bgmp.example/pic/cover/c/1.jpg", proxy
    ) == "https://bgmp.example/pic/cover/c/1.jpg"
    assert BangumiApi._normalize_image_url(
        "https://bgm.tv/subject/1", proxy
    ) == "https://bgm.tv/subject/1"
    assert BangumiApi._normalize_image_url("http://[bad", proxy) == "http://[bad"
    assert BangumiApi._normalize_image_url(None, proxy) is None

    query_api = BangumiApi(base_url="", image_domain="https://worker.example/?url=")
    path_api = BangumiApi(base_url="", image_domain="https://worker.example")
    try:
        assert query_api._normalize_image_url(
            "https://lain.bgm.tv/pic/cover/l/1.jpg", query_api.image_proxy
        ) == "https://worker.example/?url=https%3A%2F%2Flain.bgm.tv%2Fpic%2Fcover%2Fl%2F1.jpg"
        assert path_api._normalize_image_url(
            "https://lain.bgm.tv/pic/cover/l/1.jpg", path_api.image_proxy
        ) == "https://worker.example/https://lain.bgm.tv/pic/cover/l/1.jpg"
    finally:
        query_api.close()
        path_api.close()


def test_bangumi_cache_token_includes_image_proxy() -> None:
    """
    切换图片代理必须产生新的缓存身份，避免复用旧来源的图片地址。
    """
    official = BangumiApi(base_url="", image_domain="")
    mirrored = BangumiApi(base_url="", image_domain="bgmp.example")

    try:
        assert official._cache_token != mirrored._cache_token
        assert "bgmp.example" in mirrored._cache_token
    finally:
        official.close()
        mirrored.close()
