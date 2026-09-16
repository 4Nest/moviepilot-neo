import asyncio
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import AsyncMock, patch

from app.api.endpoints.subscribe import create_subscribe
from app.schemas.subscribe import Subscribe
from app.schemas.types import EventType, MediaType


class SubscribeEndpointTest(TestCase):
    """
    订阅接口回归测试。
    """

    def test_read_subscribes_returns_global_list_for_admin(self):
        """
        唯一账号体系下订阅列表直接返回全量数据。
        """
        from app.api.endpoints.subscribe import list_subscribes, read_subscribes

        all_subscribes = [
            _EndpointSubscribe(id=1, username="admin", name="订阅一"),
            _EndpointSubscribe(id=2, username="admin", name="订阅二"),
        ]

        with patch(
            "app.api.endpoints.subscribe.Subscribe.async_list",
            new=AsyncMock(return_value=all_subscribes),
        ):
            api_token_result = asyncio.run(list_subscribes(_="api-token"))
            self.assertEqual([sub.id for sub in api_token_result], [1, 2])

            admin_result = asyncio.run(
                read_subscribes(
                    db=object(),
                    current_user=_EndpointUser(name="admin", is_superuser=True),
                )
            )
            self.assertEqual([sub.id for sub in admin_result], [1, 2])

    def test_read_subscribe_returns_row_by_id(self):
        """
        订阅详情按 ID 直接返回订阅行，归属校验由 admin 依赖统一完成。
        """
        from app.api.endpoints.subscribe import read_subscribe

        subscribe = _EndpointSubscribe(id=1, username="admin", name="测试订阅")

        with patch(
            "app.api.endpoints.subscribe.Subscribe.async_get",
            new=AsyncMock(return_value=subscribe),
        ):
            result = asyncio.run(
                read_subscribe(
                    subscribe_id=1,
                    db=object(),
                    current_user=_EndpointUser(name="admin", is_superuser=True),
                )
            )

        self.assertEqual(result.id, 1)

        with patch(
            "app.api.endpoints.subscribe.Subscribe.async_get",
            new=AsyncMock(return_value=None),
        ):
            result = asyncio.run(
                read_subscribe(
                    subscribe_id=99,
                    db=object(),
                    current_user=_EndpointUser(name="admin", is_superuser=True),
                )
            )

        self.assertIsNone(getattr(result, "id", None))

    def test_owner_can_update_own_subscribe(self):
        """
        owner 可以继续管理自己创建的订阅。
        """
        from app.api.endpoints.subscribe import update_subscribe

        subscribe = _EndpointSubscribe(
            id=4,
            username="alice",
            name="旧标题",
            total_episode=8,
            lack_episode=2,
            vote=0.0,
            sites=[],
            search_imdbid=0,
            filter_groups=[],
            start_episode=0,
        )

        with patch(
            "app.api.endpoints.subscribe.Subscribe.async_get",
            new=AsyncMock(side_effect=[subscribe, subscribe]),
        ), patch(
            "app.api.endpoints.subscribe.eventmanager.async_send_event",
            new=AsyncMock(),
        ) as send_event:
            response = asyncio.run(
                update_subscribe(
                    subscribe_in=Subscribe(
                        id=4,
                        name="新标题",
                        total_episode=8,
                        lack_episode=2,
                    ),
                    db=object(),
                    current_user=_EndpointUser(name="alice", is_superuser=False),
                )
            )

        self.assertTrue(response.success)
        send_event.assert_awaited_once()

    def test_update_subscribe_preserves_existing_owner(self):
        """
        普通更新不得允许请求体改写订阅 owner。
        """
        from app.api.endpoints.subscribe import update_subscribe

        subscribe = _EndpointSubscribe(
            id=12,
            username="alice",
            name="旧标题",
            total_episode=8,
            lack_episode=2,
            vote=0.0,
            sites=[],
            search_imdbid=0,
            filter_groups=[],
            start_episode=0,
        )
        subscribe_in = Subscribe(
            id=12,
            username="bob",
            name="新标题",
            total_episode=8,
            lack_episode=2,
        )

        with patch(
            "app.api.endpoints.subscribe.Subscribe.async_get",
            new=AsyncMock(side_effect=[subscribe, subscribe]),
        ), patch(
            "app.api.endpoints.subscribe.eventmanager.async_send_event",
            new=AsyncMock(),
        ) as send_event:
            response = asyncio.run(
                update_subscribe(
                    subscribe_in=subscribe_in,
                    db=object(),
                    current_user=_EndpointUser(name="alice", is_superuser=False),
                )
            )

        self.assertTrue(response.success)
        self.assertEqual(subscribe.username, "alice")
        event_type, payload = send_event.await_args.args
        self.assertEqual(event_type, EventType.SubscribeModified)
        self.assertNotIn("username", payload["fields"])
        self.assertEqual(payload["subscribe_info"]["username"], "alice")

    def test_superuser_can_update_other_and_legacy_subscribe(self):
        """
        超级用户可以管理他人和 legacy 订阅。
        """
        from app.api.endpoints.subscribe import update_subscribe_status

        current_user = _EndpointUser(name="admin", is_superuser=True)
        for subscribe in [
            _EndpointSubscribe(id=5, username="bob", state="R", name="他人的订阅"),
            _EndpointSubscribe(id=6, username=None, state="R", name="旧订阅"),
        ]:
            with self.subTest(subscribe_id=subscribe.id), patch(
                "app.api.endpoints.subscribe.Subscribe.async_get",
                new=AsyncMock(side_effect=[subscribe, subscribe]),
            ), patch(
                "app.api.endpoints.subscribe.eventmanager.async_send_event",
                new=AsyncMock(),
            ) as send_event:
                response = asyncio.run(
                    update_subscribe_status(
                        subid=subscribe.id,
                        state="S",
                        db=object(),
                        current_user=current_user,
                    )
                )

            self.assertTrue(response.success)
            send_event.assert_awaited_once()
            self.assertEqual(subscribe.state, "S")

    def test_share_subscribe_requires_existing_subscribe(self):
        """
        分享订阅前必须确认订阅行存在。
        """
        from app.api.endpoints.subscribe import subscribe_share
        from app.schemas.subscribe import SubscribeShare

        with patch(
            "app.api.endpoints.subscribe.Subscribe.async_get",
            new=AsyncMock(return_value=None),
        ), patch(
            "app.api.endpoints.subscribe.MoviePilotServerHelper.async_sub_share",
            new=AsyncMock(return_value=(True, "")),
        ) as sub_share:
            response = asyncio.run(
                subscribe_share(
                    sub=SubscribeShare(
                        subscribe_id=7,
                        share_title="分享",
                        share_comment="",
                        share_user="admin",
                    ),
                    db=object(),
                    current_user=_EndpointUser(name="admin", is_superuser=True),
                )
            )

        self.assertFalse(response.success)
        self.assertEqual(response.message, "订阅不存在")
        sub_share.assert_not_awaited()

    def test_subscribe_mediaid_returns_first_matching_candidate(self):
        """
        按媒体查询订阅时返回候选集合中的第一条记录。
        """
        from app.api.endpoints.subscribe import subscribe_mediaid

        first = _EndpointSubscribe(id=13, username="admin", tmdbid=123, season=1)
        second = _EndpointSubscribe(id=14, username="admin", tmdbid=123, season=1)

        with patch(
            "app.api.endpoints.subscribe.Subscribe.async_exists",
            new=AsyncMock(return_value=first),
        ), patch(
            "app.api.endpoints.subscribe.Subscribe.async_get_by_tmdbid",
            new=AsyncMock(return_value=[first, second]),
        ):
            result = asyncio.run(
                subscribe_mediaid(
                    mediaid="tmdb:123",
                    season=1,
                    db=object(),
                    current_user=_EndpointUser(name="admin", is_superuser=True),
                )
            )

        self.assertEqual(result.id, 13)

    def test_delete_subscribe_by_mediaid_deletes_all_candidates(self):
        """
        按媒体删除订阅时，删除候选集合中的全部订阅。
        """
        from app.api.endpoints.subscribe import delete_subscribe_by_mediaid

        first = _EndpointSubscribe(id=15, username="admin", doubanid="douban-1")
        second = _EndpointSubscribe(id=16, username="admin", doubanid="douban-1")
        db = _EndpointAsyncDb()

        with patch(
            "app.api.endpoints.subscribe.Subscribe.async_get_by_doubanid",
            new=AsyncMock(return_value=first),
        ), patch(
            "app.api.endpoints.subscribe.Subscribe.async_list_by_doubanid",
            new=AsyncMock(return_value=[first, second]),
            create=True,
        ), patch(
            "app.api.endpoints.subscribe.build_subscribe_event_payload",
            side_effect=lambda sub: {"id": sub.id, "doubanid": "douban-1"},
        ), patch(
            "app.api.endpoints.subscribe.eventmanager.async_send_event",
            new=AsyncMock(),
        ) as send_event:
            response = asyncio.run(
                delete_subscribe_by_mediaid(
                    mediaid="douban:douban-1",
                    db=db,
                    current_user=_EndpointUser(name="admin", is_superuser=True),
                )
            )

        self.assertTrue(response.success)
        self.assertEqual(db.deleted, [first, second])
        self.assertEqual(send_event.await_count, 2)

    def test_search_subscribes_schedules_single_global_job(self):
        """
        批量搜索入队一个全局订阅搜索任务，归属校验由 admin 依赖统一完成。
        """
        from app.api.endpoints.subscribe import search_subscribes

        background_tasks = _EndpointBackgroundTasks()

        with patch("app.api.endpoints.subscribe.Scheduler") as scheduler_cls:
            response = asyncio.run(
                search_subscribes(
                    background_tasks=background_tasks,
                    db=object(),
                    current_user=_EndpointUser(name="admin", is_superuser=True),
                )
            )

        self.assertTrue(response.success)
        self.assertEqual(len(background_tasks.tasks), 1)
        task = background_tasks.tasks[0]
        self.assertEqual(task["kwargs"]["job_id"], "subscribe_search")
        self.assertEqual(
            {key: task["kwargs"][key] for key in ("sid", "state", "manual")},
            {"sid": None, "state": "R", "manual": True},
        )
        self.assertEqual(scheduler_cls.return_value.start.call_count, 0)

    def test_subscribe_files_returns_info_for_existing_row(self):
        """
        订阅文件接口返回已存在订阅的文件信息。
        """
        from app.api.endpoints.subscribe import subscribe_files

        subscribe = _EndpointSubscribe(id=19, username="admin", name="测试订阅")

        with patch(
            "app.api.endpoints.subscribe.Subscribe.get",
            return_value=subscribe,
        ), patch(
            "app.api.endpoints.subscribe.SubscribeChain"
        ) as subscribe_chain:
            subscribe_chain.return_value.subscribe_files_info.return_value = "files-info"
            result = subscribe_files(
                subscribe_id=19,
                db=object(),
                current_user=_EndpointUser(name="admin", is_superuser=True),
            )

        self.assertEqual(result, "files-info")
        subscribe_chain.return_value.subscribe_files_info.assert_called_once_with(subscribe)

    def test_user_subscribes_route_removed(self):
        """
        按用户名查询订阅的路由已随多用户体系删除。
        """
        from fastapi.routing import APIRoute

        from app.api.endpoints import subscribe as subscribe_endpoint

        paths = {
            route.path
            for route in subscribe_endpoint.router.routes
            if isinstance(route, APIRoute)
        }
        self.assertNotIn("/user/{username}", paths)

    def test_subscribe_oper_async_add_uses_global_duplicate_lookup(self):
        """
        唯一账号体系下新增订阅使用全局去重，不再按 owner 区分。
        """
        from app.db.subscribe_oper import SubscribeOper

        persisted = _EndpointSubscribe(id=21, username="admin")
        created = SimpleNamespace(async_create=AsyncMock())

        with patch("app.db.subscribe_oper.Subscribe") as subscribe_model:
            subscribe_model.async_exists = AsyncMock(side_effect=[None, persisted])
            subscribe_model.return_value = created

            sid, message = asyncio.run(
                SubscribeOper(db=object()).async_add(
                    mediainfo=_EndpointMediaInfo(),
                    username="admin",
                    season=1,
                )
            )

        self.assertEqual(sid, 21)
        self.assertEqual(message, "新增订阅成功")
        self.assertEqual(subscribe_model.async_exists.await_count, 2)
        created.async_create.assert_awaited_once()

    def test_subscribe_history_uses_global_pagination(self):
        """
        唯一账号体系下订阅历史直接按类型全局分页。
        """
        from app.api.endpoints.subscribe import subscribe_history

        histories = [
            _EndpointSubscribe(id=8, username="admin", name="历史一", type=MediaType.MOVIE.value),
            _EndpointSubscribe(id=9, username="admin", name="历史二", type=MediaType.MOVIE.value),
        ]
        db = object()
        global_query = AsyncMock(return_value=histories)

        with patch(
            "app.api.endpoints.subscribe.SubscribeHistory.async_list_by_type",
            new=global_query,
        ):
            result = asyncio.run(
                subscribe_history(
                    mtype=MediaType.MOVIE.value,
                    page=1,
                    count=2,
                    db=db,
                    current_user=_EndpointUser(name="admin", is_superuser=True),
                )
            )

        self.assertEqual([history.id for history in result], [8, 9])
        global_query.assert_awaited_once_with(
            db,
            mtype=MediaType.MOVIE.value,
            page=1,
            count=2,
        )

    def test_delete_subscribe_history_deletes_existing_row(self):
        """
        删除订阅历史按 ID 直接删除存在的记录。
        """
        from app.api.endpoints.subscribe import delete_subscribe_history

        history = _EndpointSubscribe(
            id=11,
            username="admin",
            name="测试历史",
            type=MediaType.MOVIE.value,
        )

        with patch(
            "app.api.endpoints.subscribe.SubscribeHistory.async_get",
            new=AsyncMock(return_value=history),
        ), patch(
            "app.api.endpoints.subscribe.SubscribeHistory.async_delete",
            new=AsyncMock(),
        ) as async_delete:
            response = asyncio.run(
                delete_subscribe_history(
                    history_id=11,
                    db=object(),
                    current_user=_EndpointUser(name="admin", is_superuser=True),
                )
            )

        self.assertTrue(response.success)
        async_delete.assert_awaited_once()

    def test_global_refresh_and_check_require_admin_dependency(self):
        """
        全局订阅任务由 get_current_admin 依赖统一鉴权，端点只负责调度。
        """
        import inspect

        from app.api.endpoints.subscribe import check_subscribes, refresh_subscribes
        from app.db.user_oper import get_current_admin

        for endpoint in [refresh_subscribes, check_subscribes]:
            with self.subTest(endpoint=endpoint.__name__):
                dependency = inspect.signature(endpoint).parameters["current_user"].default
                self.assertIs(dependency.dependency, get_current_admin)

        for endpoint, job_id in [
            (refresh_subscribes, "subscribe_refresh"),
            (check_subscribes, "subscribe_tmdb"),
        ]:
            with self.subTest(endpoint=endpoint.__name__), patch(
                "app.api.endpoints.subscribe.Scheduler"
            ) as scheduler:
                response = endpoint(current_user=_EndpointUser(name="admin", is_superuser=True))

            self.assertTrue(response.success)
            scheduler.return_value.start.assert_called_once_with(job_id)

    def test_create_subscribe_excludes_system_fields_from_write_payload(self):
        """
        新增订阅时不应把历史 ID、媒体元数据和响应派生字段传入持久化链路。
        """
        subscribe_in = Subscribe(
            id=99,
            name="测试剧集",
            year="2026",
            type=MediaType.TV.value,
            season=1,
            poster="old-poster.jpg",
            backdrop="old-backdrop.jpg",
            vote=8.0,
            description="旧历史简介",
            total_episode=10,
            lack_episode=3,
        )

        self.assertEqual(subscribe_in.completed_episode, 7)

        with patch(
            "app.api.endpoints.subscribe.SubscribeChain.async_add",
            new=AsyncMock(return_value=(1, "新增订阅成功")),
        ) as async_add:
            response = asyncio.run(
                create_subscribe(
                    subscribe_in=subscribe_in,
                    current_user=_EndpointUser(name="moviepilot-user", is_superuser=False),
                )
            )

        self.assertTrue(response.success)
        payload = async_add.await_args.kwargs
        for field in ("id", "poster", "backdrop", "vote", "description", "completed_episode"):
            self.assertNotIn(field, payload)
        self.assertEqual(payload["username"], "admin")
        self.assertNotIn("owner_scope", payload)

    def test_create_subscribe_ignores_runtime_fact_fields(self):
        """
        公共新增接口只能写目标和配置，调用方携带的运行事实不得进入新增链路。
        """
        subscribe_in = Subscribe(
            name="测试剧集",
            year="2026",
            type=MediaType.TV.value,
            season=1,
            total_episode=10,
            lack_episode=3,
            note=[1, 2, 3],
            state="S",
            last_update="2026-07-20 12:00:00",
            username="forged-user",
            current_priority=90,
            episode_priority={"1": 90},
            date="2026-07-19 12:00:00",
        )

        with patch(
            "app.api.endpoints.subscribe.SubscribeChain.async_add",
            new=AsyncMock(return_value=(1, "新增订阅成功")),
        ) as async_add:
            response = asyncio.run(
                create_subscribe(
                    subscribe_in=subscribe_in,
                    current_user=_EndpointUser(name="moviepilot-user", is_superuser=False),
                )
            )

        self.assertTrue(response.success)
        payload = async_add.await_args.kwargs
        self.assertEqual(payload["username"], "admin")
        for field in (
            "lack_episode",
            "note",
            "state",
            "last_update",
            "current_priority",
            "episode_priority",
            "date",
            "completed_episode",
        ):
            self.assertNotIn(field, payload)

    def test_create_subscribe_preserves_special_season_zero_with_doubanid(self):
        """
        新增订阅带豆瓣 ID 且显式指定 S0 时，标题规整不应覆盖调用方传入的季号。
        """
        subscribe_in = Subscribe(
            name="测试剧集",
            year="2026",
            type=MediaType.TV.value,
            doubanid="12345",
            season=0,
            total_episode=5,
            lack_episode=5,
        )

        with patch(
            "app.api.endpoints.subscribe.MetaInfo",
            return_value=SimpleNamespace(name="测试剧集", begin_season=None),
        ), patch(
            "app.api.endpoints.subscribe.SubscribeChain.async_add",
            new=AsyncMock(return_value=(1, "新增订阅成功")),
        ) as async_add:
            response = asyncio.run(
                create_subscribe(
                    subscribe_in=subscribe_in,
                    current_user=_EndpointUser(name="moviepilot-user", is_superuser=False),
                )
            )

        self.assertTrue(response.success)
        self.assertEqual(async_add.await_args.kwargs["season"], 0)
        self.assertNotIn("owner_scope", async_add.await_args.kwargs)

    def test_create_subscribe_uses_global_deduplication(self):
        """
        新增订阅保持全局去重语义，不再携带 owner_scope。
        """
        subscribe_in = Subscribe(
            name="测试电影",
            year="2026",
            type=MediaType.MOVIE.value,
        )

        with patch(
            "app.api.endpoints.subscribe.SubscribeChain.async_add",
            new=AsyncMock(return_value=(1, "订阅已存在")),
        ) as async_add:
            response = asyncio.run(
                create_subscribe(
                    subscribe_in=subscribe_in,
                    current_user=_EndpointUser(name="admin", is_superuser=True),
                )
            )

        self.assertTrue(response.success)
        self.assertNotIn("owner_scope", async_add.await_args.kwargs)

    def test_update_status_sends_modified_event_payload_with_scene_and_fields(self):
        """
        状态更新只负责发出订阅修改事件，并携带场景和真实变更字段。
        """
        from app.api.endpoints.subscribe import update_subscribe_status

        subscribe = _EndpointSubscribe(id=5, state="R", name="测试订阅")

        with patch(
            "app.api.endpoints.subscribe.Subscribe.async_get",
            new=AsyncMock(side_effect=[subscribe, subscribe]),
        ), patch(
            "app.api.endpoints.subscribe.eventmanager.async_send_event",
            new=AsyncMock(),
        ) as send_event:
            response = asyncio.run(
                update_subscribe_status(
                    subid=5,
                    state="S",
                    db=object(),
                    current_user=_EndpointUser(name="admin", is_superuser=True),
                )
            )

        self.assertTrue(response.success)
        send_event.assert_awaited_once()
        event_type, payload = send_event.await_args.args
        self.assertEqual(event_type, EventType.SubscribeModified)
        self.assertEqual(payload["subscribe_id"], 5)
        self.assertEqual(payload["scene"], "status")
        self.assertEqual(payload["fields"], ["state"])
        self.assertEqual(payload["old_subscribe_info"]["state"], "R")
        self.assertEqual(payload["subscribe_info"]["state"], "S")

    def test_reset_sends_modified_event_payload_with_reset_scene(self):
        """
        reset 事件需要明确 scene，消费者不需要再从字段差异猜测用户意图。
        """
        from app.api.endpoints.subscribe import reset_subscribes

        subscribe = _EndpointSubscribe(
            id=6,
            state="S",
            name="测试订阅",
            total_episode=10,
            lack_episode=3,
            manual_total_episode=92,
            note=[1, 2],
            current_priority=80,
            episode_priority={"1": 80},
            version_progress={
                "main": {
                    "state": "R",
                    "note": [1, 2],
                    "lack_episode": 3,
                    "completed": False,
                }
            },
        )

        with patch(
            "app.api.endpoints.subscribe.Subscribe.async_get",
            new=AsyncMock(side_effect=[subscribe, subscribe]),
        ), patch(
            "app.api.endpoints.subscribe.eventmanager.async_send_event",
            new=AsyncMock(),
        ) as send_event:
            response = asyncio.run(
                reset_subscribes(
                    subid=6,
                    db=object(),
                    current_user=_EndpointUser(name="admin", is_superuser=True),
                )
            )

        self.assertTrue(response.success)
        send_event.assert_awaited_once()
        event_type, payload = send_event.await_args.args
        self.assertEqual(event_type, EventType.SubscribeModified)
        self.assertEqual(payload["subscribe_id"], 6)
        self.assertEqual(payload["scene"], "reset")
        self.assertEqual(
            payload["fields"],
            [
                "current_priority",
                "episode_priority",
                "lack_episode",
                "manual_total_episode",
                "note",
                "state",
                "version_progress",
            ],
        )
        self.assertEqual(payload["subscribe_info"]["note"], [])
        self.assertEqual(payload["subscribe_info"]["lack_episode"], 10)
        self.assertEqual(payload["subscribe_info"]["manual_total_episode"], 0)
        self.assertEqual(payload["subscribe_info"]["version_progress"], {})

    def test_update_subscribe_sends_modified_event_payload_without_progress_refresh(self):
        """
        普通更新只发送 modify 事件；进度刷新由事件消费者或后续流程处理。
        """
        from app.api.endpoints.subscribe import update_subscribe

        subscribe = _EndpointSubscribe(
            id=7,
            name="旧标题",
            total_episode=8,
            lack_episode=2,
            vote=0.0,
            sites=[],
            search_imdbid=0,
            filter_groups=[],
            start_episode=0,
        )
        subscribe_in = Subscribe(id=7, name="新标题", total_episode=8, lack_episode=2)

        with patch(
            "app.api.endpoints.subscribe.Subscribe.async_get",
            new=AsyncMock(side_effect=[subscribe, subscribe]),
        ), patch(
            "app.api.endpoints.subscribe.eventmanager.async_send_event",
            new=AsyncMock(),
        ) as send_event:
            response = asyncio.run(
                update_subscribe(
                    subscribe_in=subscribe_in,
                    db=object(),
                    current_user=_EndpointUser(name="admin", is_superuser=True),
                )
            )

        self.assertTrue(response.success)
        send_event.assert_awaited_once()
        event_type, payload = send_event.await_args.args
        self.assertEqual(event_type, EventType.SubscribeModified)
        self.assertEqual(payload["subscribe_id"], 7)
        self.assertEqual(payload["scene"], "update")
        self.assertEqual(payload["fields"], ["name"])
        self.assertEqual(payload["old_subscribe_info"]["name"], "旧标题")
        self.assertEqual(payload["subscribe_info"]["name"], "新标题")

    def test_update_subscribe_ignores_runtime_fact_fields(self):
        """
        公共普通更新不得覆盖运行事实，状态调整继续由专用接口负责。
        """
        from app.api.endpoints.subscribe import update_subscribe

        subscribe = _EndpointSubscribe(
            id=8,
            username="alice",
            name="旧标题",
            total_episode=10,
            lack_episode=5,
            state="R",
            note=[1, 2, 3, 4, 5],
            current_priority=60,
            episode_priority={"1": 60},
            last_update="2026-07-19 12:00:00",
            date="2026-07-18 12:00:00",
            sites=[],
            search_imdbid=0,
            filter_groups=[],
            start_episode=0,
        )
        subscribe_in = Subscribe(
            id=8,
            name="新标题",
            total_episode=10,
            lack_episode=0,
            state="S",
            note=[],
            current_priority=100,
            episode_priority={"1": 100},
            last_update="2026-07-20 12:00:00",
            date="2026-07-20 12:00:00",
        )

        with patch(
            "app.api.endpoints.subscribe.Subscribe.async_get",
            new=AsyncMock(side_effect=[subscribe, subscribe]),
        ), patch(
            "app.api.endpoints.subscribe.eventmanager.async_send_event",
            new=AsyncMock(),
        ):
            response = asyncio.run(
                update_subscribe(
                    subscribe_in=subscribe_in,
                    db=object(),
                    current_user=_EndpointUser(name="admin", is_superuser=True),
                )
            )

        self.assertTrue(response.success)
        self.assertEqual(subscribe.name, "新标题")
        self.assertEqual(subscribe.lack_episode, 5)
        self.assertEqual(subscribe.state, "R")
        self.assertEqual(subscribe.note, [1, 2, 3, 4, 5])
        self.assertEqual(subscribe.current_priority, 60)
        self.assertEqual(subscribe.episode_priority, {"1": 60})
        self.assertEqual(subscribe.last_update, "2026-07-19 12:00:00")
        self.assertEqual(subscribe.date, "2026-07-18 12:00:00")

    def test_update_subscribe_derives_lack_when_total_episode_increases(self):
        """
        公共更新扩大目标范围时，缺失集数与人工总集数标记仍由服务端派生。
        """
        from app.api.endpoints.subscribe import update_subscribe

        subscribe = _EndpointSubscribe(
            id=9,
            username="alice",
            name="测试剧集",
            total_episode=10,
            lack_episode=2,
            manual_total_episode=0,
            sites=[],
            search_imdbid=0,
            filter_groups=[],
            start_episode=0,
        )
        subscribe_in = Subscribe(id=9, name="测试剧集", total_episode=12, lack_episode=0)

        with patch(
            "app.api.endpoints.subscribe.Subscribe.async_get",
            new=AsyncMock(side_effect=[subscribe, subscribe]),
        ), patch(
            "app.api.endpoints.subscribe.eventmanager.async_send_event",
            new=AsyncMock(),
        ):
            response = asyncio.run(
                update_subscribe(
                    subscribe_in=subscribe_in,
                    db=object(),
                    current_user=_EndpointUser(name="admin", is_superuser=True),
                )
            )

        self.assertTrue(response.success)
        self.assertEqual(subscribe.total_episode, 12)
        self.assertEqual(subscribe.lack_episode, 4)
        self.assertEqual(subscribe.manual_total_episode, 1)


class _EndpointUser(SimpleNamespace):
    """
    最小用户替身，模拟订阅 endpoint 依赖的用户权限字段。
    """

    def __init__(self, name: str, is_superuser: bool, permissions: dict | None = None):
        super().__init__(
            name=name,
            is_superuser=is_superuser,
            permissions=permissions or {},
        )


class _EndpointAsyncDb:
    """
    最小异步数据库替身，用于观察 endpoint 删除的订阅对象。
    """

    def __init__(self):
        self.deleted = []
        self.committed = False
        self.rolled_back = False

    async def delete(self, obj):
        self.deleted.append(obj)

    async def commit(self):
        self.committed = True

    async def rollback(self):
        self.rolled_back = True


class _EndpointBackgroundTasks:
    """
    最小后台任务替身，记录 endpoint 入队的任务参数。
    """

    def __init__(self):
        self.tasks = []

    def add_task(self, func, **kwargs):
        self.tasks.append({"func": func, "kwargs": kwargs})


class _EndpointMediaInfo:
    """
    最小媒体信息替身，模拟 SubscribeOper 写订阅行所需字段。
    """

    title = "测试剧集"
    year = "2026"
    type = MediaType.TV
    tmdb_id = 123
    imdb_id = "tt123"
    tvdb_id = 456
    douban_id = "douban-1"
    bangumi_id = 789
    anilist_id = 154587
    episode_group = None
    vote_average = 8.0
    overview = "测试简介"

    @staticmethod
    def get_poster_image():
        return "poster.jpg"

    @staticmethod
    def get_backdrop_image():
        return "backdrop.jpg"


class _EndpointSubscribe:
    """
    最小订阅替身，模拟 endpoint 依赖的 ORM 对象接口。
    """

    def __init__(self, **kwargs):
        self.id = kwargs.pop("id", None)
        self.username = kwargs.pop("username", None)
        self.name = kwargs.pop("name", None)
        self.total_episode = kwargs.pop("total_episode", None)
        self.lack_episode = kwargs.pop("lack_episode", None)
        self.state = kwargs.pop("state", None)
        self.note = kwargs.pop("note", None)
        self.current_priority = kwargs.pop("current_priority", None)
        self.episode_priority = kwargs.pop("episode_priority", None)
        self.manual_total_episode = kwargs.pop("manual_total_episode", None)
        # 迁移后该列必有默认值 0,补进替身以保持 old/new 快照键集合与真实 ORM 一致
        self.skip_library_check = kwargs.pop("skip_library_check", 0)
        self.__dict__.update(kwargs)

    def to_dict(self):
        return {
            key: value
            for key, value in self.__dict__.items()
            if value is not None
        }

    async def async_update(self, _db, payload):
        self.__dict__.update(payload)
