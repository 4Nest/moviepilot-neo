import asyncio
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.db.models.subscribe import Subscribe
from app.db.models.subscribehistory import SubscribeHistory
from app.db.subscribe_oper import SubscribeOper
from app.schemas.types import MediaType


def _media(episode_group):
    """构造订阅新增路径所需的稳定 MediaInfo 契约替身。"""
    return SimpleNamespace(
        title="测试剧",
        year="2026",
        type=MediaType.TV,
        source="themoviedb",
        media_source="themoviedb",
        media_id="987654321",
        mediaid="tmdb:987654321",
        tmdb_id=987654321,
        imdb_id=None,
        tvdb_id=None,
        douban_id=None,
        bangumi_id=None,
        anilist_id=None,
        episode_group=episode_group,
        vote_average=8.0,
        overview="测试简介",
        get_poster_image=lambda: None,
        get_backdrop_image=lambda: None,
    )


def test_add_history_converts_boolean_integer_flags(monkeypatch):
    """
    写入订阅历史前应把布尔开关转为整型，兼容 PostgreSQL 的严格类型检查。
    """
    captured = {}

    def fake_create(self, _db):
        """
        截获待写入模型，避免测试依赖具体数据库方言的类型宽松行为。
        """
        captured.update({
            "id": self.id,
            "best_version": self.best_version,
            "best_version_full": self.best_version_full,
            "search_imdbid": self.search_imdbid,
        })

    monkeypatch.setattr(SubscribeHistory, "create", fake_create)

    SubscribeOper().add_history(
        id=100,
        name="Test Movie",
        type="电影",
        best_version=False,
        best_version_full=True,
        search_imdbid=False,
        unknown_field=True,
    )

    assert captured == {
        "id": None,
        "best_version": 0,
        "best_version_full": 1,
        "search_imdbid": 0,
    }


@pytest.mark.parametrize("episode_group", [None, "eg-1"])
def test_add_scopes_duplicate_lookup_by_episode_group(episode_group):
    """同步新增前后都必须按剧集组查询，主季和自定义组不能互相去重。"""
    persisted = SimpleNamespace(id=88)
    created = SimpleNamespace(create=MagicMock())

    with patch("app.db.subscribe_oper.Subscribe") as subscribe_model:
        subscribe_model.exists.side_effect = [None, persisted]
        subscribe_model.return_value = created

        sid, message = SubscribeOper(db=object()).add(
            mediainfo=_media(episode_group),
            season=1,
        )

    assert (sid, message) == (88, "新增订阅成功")
    assert subscribe_model.exists.call_count == 2
    assert all(
        call.kwargs["episode_group"] == episode_group
        for call in subscribe_model.exists.call_args_list
    )
    created.create.assert_called_once()


@pytest.mark.parametrize("episode_group", [None, "eg-1"])
def test_async_add_scopes_duplicate_lookup_by_episode_group(episode_group):
    """异步新增与同步路径使用相同的剧集组身份契约。"""
    persisted = SimpleNamespace(id=89)
    created = SimpleNamespace(async_create=AsyncMock())

    with patch("app.db.subscribe_oper.Subscribe") as subscribe_model:
        subscribe_model.async_exists = AsyncMock(side_effect=[None, persisted])
        subscribe_model.return_value = created

        sid, message = asyncio.run(SubscribeOper(db=object()).async_add(
            mediainfo=_media(episode_group),
            season=1,
        ))

    assert (sid, message) == (89, "新增订阅成功")
    assert subscribe_model.async_exists.await_count == 2
    assert all(
        call.kwargs["episode_group"] == episode_group
        for call in subscribe_model.async_exists.await_args_list
    )
    created.async_create.assert_awaited_once()


def test_exists_defaults_to_main_season_episode_group():
    """省略剧集组时按主季查询，显式剧集组按对应范围查询。"""
    oper = SubscribeOper(db=object())
    with patch("app.db.subscribe_oper.Subscribe") as subscribe_model:
        subscribe_model.exists.return_value = SimpleNamespace(id=1)

        assert oper.exists(tmdbid=100, season=1) is True
        assert subscribe_model.exists.call_args.kwargs["episode_group"] is None

        assert oper.exists(tmdbid=100, season=1, episode_group="eg-1") is True
        assert subscribe_model.exists.call_args.kwargs["episode_group"] == "eg-1"

    with patch("app.db.subscribe_oper.SubscribeHistory") as history_model:
        history_model.exists.return_value = SimpleNamespace(id=2)

        assert oper.exist_history(tmdbid=100, season=1) is True
        assert history_model.exists.call_args.kwargs["episode_group"] is None

        assert oper.exist_history(tmdbid=100, season=1, episode_group="eg-1") is True
        assert history_model.exists.call_args.kwargs["episode_group"] == "eg-1"


def test_subscribe_exists_distinguishes_same_season_episode_groups():
    """同一媒体同一季的主季、自定义剧集组应分别命中各自订阅。"""
    oper = SubscribeOper()
    tmdbid = -(900_000_000 + os.getpid())
    created_ids = []
    rows = [
        Subscribe(name="主季订阅", type=MediaType.TV.value, state="N",
                  tmdbid=tmdbid, season=1, episode_group=None),
        Subscribe(name="剧集组订阅", type=MediaType.TV.value, state="N",
                  tmdbid=tmdbid, season=1, episode_group="eg-1"),
    ]
    try:
        for row in rows:
            row.create(oper._db)

        main_season = Subscribe.exists(
            oper._db, tmdbid=tmdbid, season=1, episode_group=None,
        )
        created_ids.append(main_season.id)
        main_name = main_season.name
        episode_group = Subscribe.exists(
            oper._db, tmdbid=tmdbid, season=1, episode_group="eg-1",
        )
        created_ids.append(episode_group.id)
        episode_group_name = episode_group.name

        assert main_name == "主季订阅"
        assert episode_group_name == "剧集组订阅"

        Subscribe.delete(oper._db, rid=created_ids.pop(0))
        assert Subscribe.exists(oper._db, tmdbid=tmdbid, season=1) is None
    finally:
        for subscribe_id in created_ids:
            Subscribe.delete(oper._db, rid=subscribe_id)


def test_subscribe_chain_exists_forwards_episode_group():
    """订阅前置存在性检查必须查询当前剧集组，不能退回主季范围。"""
    from app.chain.subscribe import SubscribeChain

    media = _media("eg-1")
    meta = SimpleNamespace(begin_season=1)
    with patch("app.chain.subscribe.SubscribeOper") as subscribe_oper_cls:
        subscribe_oper_cls.return_value.exists.return_value = True

        assert SubscribeChain.exists(media, meta) is True

    subscribe_oper_cls.return_value.exists.assert_called_once_with(
        tmdbid=media.tmdb_id,
        doubanid=media.douban_id,
        bangumiid=media.bangumi_id,
        anilistid=media.anilist_id,
        media_source="themoviedb",
        media_id=str(media.tmdb_id),
        season=1,
        episode_group="eg-1",
    )


def test_resubscribe_from_history_with_version_rules_preserves_all_mode_and_validates():
    """从历史订阅重新订阅含多版本规则的电视剧时，必须写入 version_mode=all 且列表查询序列化正常。"""
    from app.api.endpoints.subscribe import create_subscribe, subscribe_history
    from app.schemas.subscribe import Subscribe as SubscribeSchema

    tmdbid = -(910_000_000 + os.getpid())
    oper = SubscribeOper()
    created_sub_id = None
    history_row = SubscribeHistory(
        name="多版本测试番剧",
        year="2025",
        type=MediaType.TV.value,
        tmdbid=tmdbid,
        season=1,
        total_episode=12,
        username="admin",
        version_mode="all",
        version_rules=[
            {
                "id": "v1",
                "name": "ADWeb",
                "enabled": True,
                "release_group": "ADWeb",
                "settings": {"sites": [1], "filter_groups": []},
            }
        ],
        version_progress={"v1": {"state": "R", "completed": True, "note": list(range(1, 13))}},
    )
    try:
        async def _load_history():
            return await subscribe_history(
                mtype=MediaType.TV.value,
                page=1,
                count=30,
                db=object(),
                current_user=SimpleNamespace(name="admin", is_superuser=True),
            )

        with patch(
            "app.api.endpoints.subscribe.SubscribeHistory.async_list_by_type",
            new=AsyncMock(return_value=[history_row]),
        ):
            history_items = asyncio.run(_load_history())

        assert len(history_items) == 1
        resubscribe_payload = SubscribeSchema.model_validate(history_items[0].model_dump())

        with patch(
            "app.api.endpoints.subscribe.SubscribeChain.async_add",
            new=AsyncMock(return_value=(101, "新增订阅成功")),
        ) as async_add:
            resp = asyncio.run(
                create_subscribe(
                    subscribe_in=resubscribe_payload,
                    current_user=SimpleNamespace(name="admin", is_superuser=True),
                )
            )
        assert resp.success is True
        forwarded_kwargs = async_add.await_args.kwargs
        assert forwarded_kwargs["version_mode"] == "all"
        assert len(forwarded_kwargs["version_rules"]) == 1

        # 验证真实持久化落库后再走列表响应 schema 校验不抛异常
        sid, msg = oper.add(
            mediainfo=_media(None),
            season=2,
            username="admin",
            total_episode=12,
            lack_episode=12,
            version_rules=forwarded_kwargs["version_rules"],
        )
        created_sub_id = sid
        assert msg == "新增订阅成功"
        persisted = oper.get(sid)
        assert persisted.version_mode == "all"
        validated = SubscribeSchema.model_validate(persisted, from_attributes=True)
        assert validated.version_mode == "all"
        assert len(validated.version_rules) == 1
    finally:
        if created_sub_id:
            Subscribe.delete(oper._db, rid=created_sub_id)


def test_legacy_inconsistent_version_mode_row_self_heals_on_read():
    """存量历史重订遗留 version_rules 非空但 version_mode=any 的行在查询时自动归一为 all。"""
    from app.schemas.subscribe import Subscribe as SubscribeSchema

    dirty_orm_row = SimpleNamespace(
        id=77,
        name="遗留脏数据剧集",
        year="2024",
        type=MediaType.TV.value,
        tmdbid=12345,
        season=1,
        total_episode=24,
        lack_episode=24,
        start_episode=1,
        state="R",
        username="admin",
        version_mode="any",
        version_rules=[
            {
                "id": "main",
                "name": "主版本",
                "enabled": True,
                "release_group": None,
                "settings": {"sites": None, "filter_groups": None},
            }
        ],
        version_progress={
            "main": {"state": "R", "episode_priority": None, "completed": None}
        },
    )
    validated = SubscribeSchema.model_validate(dirty_orm_row, from_attributes=True)
    assert validated.version_mode == "all"
    assert validated.version_rules[0].settings.sites == []
    assert validated.version_rules[0].settings.filter_groups == []
    assert validated.version_progress["main"].episode_priority == {}
    assert validated.version_progress["main"].completed is False


def test_subscribe_models_default_version_mode_consistent_with_version_rules():
    """直接经 ORM 写入 Subscribe / SubscribeHistory 时，version_mode 默认值与 version_rules 保持自洽。"""
    import sqlalchemy as sa
    from sqlalchemy.orm import Session
    from app.db.models.subscribe import Subscribe
    from app.db.models.subscribehistory import SubscribeHistory

    engine = sa.create_engine("sqlite://")
    Subscribe.__table__.create(bind=engine)
    SubscribeHistory.__table__.create(bind=engine)

    rule = [{"id": "v-1", "name": "默认版本", "enabled": True, "settings": {}}]
    with Session(engine) as session:
        s_with_rules = Subscribe(name="带规则订阅", type="电视剧", state="N", version_rules=rule)
        s_without_rules = Subscribe(name="普通订阅", type="电视剧", state="N", version_rules=[])
        h_with_rules = SubscribeHistory(name="带规则历史", type="电视剧", version_rules=rule)
        h_without_rules = SubscribeHistory(name="普通历史", type="电视剧", version_rules=[])
        session.add_all([s_with_rules, s_without_rules, h_with_rules, h_without_rules])
        session.commit()

        assert s_with_rules.version_mode == "all"
        assert s_without_rules.version_mode == "any"
        assert h_with_rules.version_mode == "all"
        assert h_without_rules.version_mode == "any"
    engine.dispose()


def test_fix_subscribe_version_mode_migration_repairs_legacy_rows(monkeypatch):
    """存量数据迁移应把 subscribe / subscribehistory 中 version_rules 非空但 version_mode!=all 的记录修正为 all。"""
    import importlib
    import sqlalchemy as sa
    from app.db.models.subscribe import Subscribe
    from app.db.models.subscribehistory import SubscribeHistory

    migration = importlib.import_module("database.versions.d1e2f3a4b5c6_fix_subscribe_version_mode")
    engine = sa.create_engine("sqlite://")
    Subscribe.__table__.create(bind=engine)
    SubscribeHistory.__table__.create(bind=engine)

    rule = [{"id": "v-1", "name": "默认版本", "enabled": True, "settings": {}}]
    with engine.begin() as conn:
        conn.execute(
            Subscribe.__table__.insert(),
            [
                {"id": 1, "name": "脏订阅", "type": "电视剧", "state": "R", "version_rules": rule, "version_mode": "any"},
                {"id": 2, "name": "空规则订阅", "type": "电视剧", "state": "R", "version_rules": [], "version_mode": "any"},
            ],
        )
        conn.execute(
            SubscribeHistory.__table__.insert(),
            [
                {"id": 1, "name": "脏历史", "type": "电视剧", "version_rules": rule, "version_mode": "any"},
                {"id": 2, "name": "空规则历史", "type": "电视剧", "version_rules": [], "version_mode": "any"},
            ],
        )
        monkeypatch.setattr(migration.op, "get_bind", lambda: conn)
        migration.upgrade()

        sub_modes = dict(
            conn.execute(sa.select(Subscribe.__table__.c.id, Subscribe.__table__.c.version_mode)).all()
        )
        hist_modes = dict(
            conn.execute(sa.select(SubscribeHistory.__table__.c.id, SubscribeHistory.__table__.c.version_mode)).all()
        )
        assert sub_modes == {1: "all", 2: "any"}
        assert hist_modes == {1: "all", 2: "any"}
    engine.dispose()


def test_serialize_subscribe_items_skips_corrupted_row_without_500():
    """列表中存在极端异常记录时仅跳过该条并记录警告，不导致整页返回 500。"""
    from app.api.endpoints.subscribe import _serialize_subscribe_items

    good_row = SimpleNamespace(
        id=1,
        name="正常剧集",
        type=MediaType.TV.value,
        total_episode=12,
        lack_episode=3,
        version_mode="any",
        version_rules=[],
    )
    corrupted_row = SimpleNamespace(
        id=2,
        name="损坏剧集",
        type=MediaType.TV.value,
        total_episode=12,
        lack_episode=3,
        version_mode="corrupted_mode",
        version_rules=[],
    )

    serialized = _serialize_subscribe_items(
        [good_row, corrupted_row],
        scene="订阅历史",
        reset_tv_history_episodes=True,
    )
    assert len(serialized) == 1
    assert serialized[0].id == 1
    assert serialized[0].total_episode == 0
    assert serialized[0].lack_episode == 0
