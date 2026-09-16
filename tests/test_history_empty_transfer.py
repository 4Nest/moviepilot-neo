import asyncio
import inspect
from pathlib import Path

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.endpoints import history as history_endpoint
from app.db import Base
from app.db.models.transferhistory import TransferHistory
from app.db.user_oper import get_current_admin_async


def _find_empty_transfer_route():
    """在整理历史路由表中定位清空整理记录路由。"""
    for route in history_endpoint.router.routes:
        if getattr(route, "path", None) == "/empty/transfer":
            return route
    raise AssertionError("未注册 /empty/transfer 路由")


def test_empty_transfer_history_route_only_accepts_delete():
    """清空整理记录是破坏性操作，只能走 DELETE，不再接受 GET。"""
    route = _find_empty_transfer_route()

    assert "DELETE" in route.methods
    assert "GET" not in route.methods


def test_empty_transfer_history_requires_admin():
    """清空整理记录接口必须保持 canonical admin 鉴权。"""
    dependency = inspect.signature(history_endpoint.empty_transfer_history).parameters["_"].default.dependency

    assert dependency is get_current_admin_async


def test_empty_transfer_history_returns_deleted_count(tmp_path: Path):
    """清空接口返回实际删除条数，并清空整张整理记录表。"""

    async def run_case():
        """在独立异步库中造数并调用清空接口。"""
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'empty_transfer.db'}")
        SessionFactory = async_sessionmaker(bind=engine)

        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)

            async with SessionFactory() as db:
                db.add_all(
                    [
                        TransferHistory(src="/downloads/a.mkv", title="甲"),
                        TransferHistory(src="/downloads/b.mkv", title="乙"),
                    ]
                )
                await db.commit()

            async with SessionFactory() as db:
                response = await history_endpoint.empty_transfer_history(db=db, _=None)
                remaining = await TransferHistory.async_list(db)
                return response, remaining
        finally:
            await engine.dispose()

    response, remaining = asyncio.run(run_case())

    assert response.success is True
    assert response.data == {"deleted": 2}
    assert remaining == []


def test_empty_transfer_history_returns_zero_when_table_empty(tmp_path: Path):
    """空表清空返回 0 条，仍视为成功。"""

    async def run_case():
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'empty_transfer_empty.db'}")
        SessionFactory = async_sessionmaker(bind=engine)

        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)

            async with SessionFactory() as db:
                return await history_endpoint.empty_transfer_history(db=db, _=None)
        finally:
            await engine.dispose()

    response = asyncio.run(run_case())

    assert response.success is True
    assert response.data == {"deleted": 0}
