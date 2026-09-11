import asyncio
import unittest
from unittest.mock import AsyncMock, Mock, patch

import app.api.endpoints.media as media_endpoint
from app.core.context import MediaInfo
from app.schemas import MediaType


class RecognizeEndpointTest(unittest.TestCase):
    """
    media/recognize 端点：未匹配到媒体时也要返回元信息，
    前端识别测试页据此展示识别词处理结果（识别标题、生效识别词）。
    """

    def test_unrecognized_still_returns_metainfo(self):
        chain = Mock()
        chain.async_recognize_by_meta = AsyncMock(return_value=None)
        with patch.object(media_endpoint, "MediaChain", return_value=chain):
            result = asyncio.run(
                media_endpoint.recognize(
                    title="Nisekoi False Love S02E12 [1080p]",
                    subtitle=None,
                    custom_words="Nisekoi False Love => Nisekoi",
                    source=None,
                    _="token",
                )
            )

        self.assertFalse(result.get("media_info"))
        # 识别词处理后的标题（识别标题）
        self.assertEqual(result["meta_info"]["org_string"], "Nisekoi S02E12 [1080p]")
        self.assertEqual(result["meta_info"]["apply_words"], ["Nisekoi False Love => Nisekoi"])
        # 识别词处理前的原始输入
        self.assertEqual(result["meta_info"]["title"], "Nisekoi False Love S02E12 [1080p]")

    def test_recognized_returns_metainfo_and_mediainfo(self):
        chain = Mock()
        chain.async_recognize_by_meta = AsyncMock(
            return_value=MediaInfo(tmdb_id=62640, type=MediaType.TV, title="伪恋", year="2014")
        )
        with patch.object(media_endpoint, "MediaChain", return_value=chain):
            result = asyncio.run(
                media_endpoint.recognize(
                    title="Nisekoi S02E12 [1080p]",
                    subtitle=None,
                    custom_words=None,
                    source=None,
                    _="token",
                )
            )

        self.assertEqual(result["meta_info"]["org_string"], "Nisekoi S02E12 [1080p]")
        self.assertEqual(result["media_info"]["tmdb_id"], 62640)


if __name__ == "__main__":
    unittest.main()
