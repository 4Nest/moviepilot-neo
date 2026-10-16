"""中途订阅沿用原版进度口径，起始集之前的集数视为已完成。"""

from typing import Optional

import pytest

from app import schemas


@pytest.mark.parametrize("total,lack,expected", [
    (30, 14, 16), (16, 0, 16), (30, 19, 11), (30, -1, 30),
    (30, 40, 0), (10, 0, 10),
])
def test_ordinary_progress_starts_at_requested_episode(total: int, lack: int, expected: int) -> None:
    """第12集开始、下载到第16集显示16集完成，进度不能超出整季总数。"""
    subscribe = schemas.Subscribe(
        name="美人余", type="电视剧", start_episode=12,
        total_episode=total, lack_episode=lack,
    )
    assert subscribe.completed_episode == expected


@pytest.mark.parametrize("full,priority,episode_priority,expected", [
    (0, None, {"1": 100, "11": 100, "12": 100, "13": 80, "16": 100, "31": 100}, 13),
    (0, 80, {}, 11), (0, 100, {}, 30),
    (1, 80, {"12": 100}, 11), (1, 100, {}, 30),
])
def test_best_version_progress_includes_skipped_episodes(
        full: int, priority: Optional[int], episode_priority: dict[str, int], expected: int,
) -> None:
    """分集与全集洗版均计入跳过集数，已有前序集记录不能重复累加。"""
    subscribe = schemas.Subscribe(
        name="美人余", type="电视剧", start_episode=12, total_episode=30,
        best_version=1, best_version_full=full, current_priority=priority,
        episode_priority=episode_priority,
    )
    assert subscribe.completed_episode == expected


@pytest.mark.parametrize("best_version,skip_library_check", [(0, 0), (0, 1), (1, 0)])
def test_single_version_progress_respects_its_own_start(best_version: int, skip_library_check: int) -> None:
    """单版本起始集覆盖父设置时也计入前序集，并与下载记录去重。"""
    subscribe = schemas.Subscribe(
        name="美人余", type="电视剧", start_episode=1, total_episode=30,
        skip_library_check=skip_library_check,
        version_rules=[{"id": "main", "name": "主版本", "settings": {
            "start_episode": 12, "best_version": best_version,
        }}],
        version_progress={"main": {"note": list(range(1, 17))}},
    )
    assert subscribe.completed_episode == 16


@pytest.mark.parametrize("start,total,note,expected", [
    (12, 30, [], 11), (12, 30, [12, 13, 14, 15, 16], 16),
    (12, 16, [12, 13, 14, 15, 16], 16), (12, 10, [], 10),
    (0, 30, [], 0),
])
def test_version_progress_counts_prefix_without_download_facts(
        start: int, total: int, note: list[int], expected: int,
) -> None:
    """跳过集数不依赖媒体库存在，不写入下载事实，刷新响应后仍应保留。"""
    subscribe = schemas.Subscribe(
        type="电视剧", start_episode=start, total_episode=total,
        version_rules=[{"id": "main", "name": "主版本", "settings": {}}],
        version_progress={"main": {"note": note}},
    )
    assert subscribe.completed_episode == expected
    assert subscribe.version_progress["main"].note == note
    assert schemas.Subscribe.model_validate(subscribe.model_dump()).completed_episode == expected


def test_multiple_versions_count_shared_prefix_once() -> None:
    """主卡片合并版本下载记录时，公共前序集和重复下载都只计算一次。"""
    subscribe = schemas.Subscribe(
        type="电视剧", start_episode=12, total_episode=30,
        version_rules=[
            {"id": "a", "name": "A", "settings": {}},
            {"id": "b", "name": "B", "settings": {}},
            {"id": "c", "name": "C", "enabled": False, "settings": {}},
        ],
        version_progress={
            "a": {"note": [1, 11, 12, 13, 14], "known_episodes": [17, 18]},
            "b": {"note": [14, 15, 16, 31]}, "c": {"note": [17]},
        },
    )
    assert subscribe.completed_episode == 16


def test_multiple_versions_respect_independent_ranges() -> None:
    """各版本按自己的起始集补齐前序进度，停用版本和范围外记录不参与。"""
    subscribe = schemas.Subscribe(
        type="电视剧", start_episode=1, total_episode=30,
        version_rules=[
            {"id": "a", "name": "A", "settings": {"start_episode": 10}},
            {"id": "b", "name": "B", "settings": {
                "start_episode": 12, "total_episode": 16, "manual_total_episode": 1,
            }},
            {"id": "c", "name": "C", "enabled": False, "settings": {"start_episode": 25}},
        ],
        version_progress={
            "a": {"note": [10, 11, 12]}, "b": {"note": [12, 13, 14, 15, 16, 17]},
        },
    )
    assert subscribe.completed_episode == 16
