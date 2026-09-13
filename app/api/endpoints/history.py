from pathlib import Path
from typing import List, Any, Optional

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app import schemas
from app.chain.storage import StorageChain
from app.core.event import eventmanager
from app.core.security import verify_token
from app.db import get_async_db, get_db
from app.db.models import User
from app.db.models.downloadhistory import DownloadHistory, DownloadFiles
from app.db.models.transferhistory import TransferHistory
from app.db.user_oper import (
    get_current_active_manage_user,
    get_current_active_superuser,
    get_current_active_superuser_async,
)
from app.schemas.types import EventType
from app.utils.jieba import cut as jieba_cut

router = APIRouter()


@router.get(
    "/download",
    summary="查询下载历史记录",
    response_model=List[schemas.DownloadHistory],
)
async def download_history(
    page: Optional[int] = 1,
    count: Optional[int] = 30,
    db: AsyncSession = Depends(get_async_db),
    _: schemas.TokenPayload = Depends(verify_token),
) -> Any:
    """
    按下载时间倒序查询下载历史记录
    """
    return await DownloadHistory.async_list_by_page(db, page, count)


@router.delete("/download", summary="删除下载历史记录", response_model=schemas.Response)
async def delete_download_history(
    history_in: schemas.DownloadHistory,
    db: AsyncSession = Depends(get_async_db),
    _: schemas.TokenPayload = Depends(verify_token),
) -> Any:
    """
    删除下载历史记录
    """
    await DownloadHistory.async_delete(db, history_in.id)
    return schemas.Response(success=True)


def _glob_to_like(pattern: str) -> str:
    """
    将 glob 通配符模式转换为 SQL LIKE 模式（使用 \\ 作为转义字符）
    """
    result = pattern.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return result.replace("*", "%").replace("?", "_")


@router.get("/transfer", summary="查询整理记录", response_model=schemas.Response)
async def transfer_history(
    title: Optional[str] = None,
    page: Optional[int] = 1,
    count: Optional[int] = 30,
    status: Optional[bool] = None,
    db: AsyncSession = Depends(get_async_db),
    _: schemas.TokenPayload = Depends(verify_token),
) -> Any:
    """
    查询整理记录，title 支持通配符 * 和 ?（如 *.mkv、*2024*）
    """
    if title == "失败":
        title = None
        status = False
    elif title == "成功":
        title = None
        status = True

    if title:
        if "*" in title or "?" in title:
            like_pattern = _glob_to_like(title)
            total = await TransferHistory.async_count_by_title(
                db, title=like_pattern, status=status, wildcard=True
            )
            result = await TransferHistory.async_list_by_title(
                db, title=like_pattern, page=page, count=count, status=status, wildcard=True
            )
        else:
            words = jieba_cut(title, HMM=False)
            like_pattern = "%".join(words)
            total = await TransferHistory.async_count_by_title(
                db, title=like_pattern, status=status
            )
            result = await TransferHistory.async_list_by_title(
                db, title=like_pattern, page=page, count=count, status=status
            )
    else:
        result = await TransferHistory.async_list_by_page(
            db, page=page, count=count, status=status
        )
        total = await TransferHistory.async_count(db, status=status)

    return schemas.Response(
        success=True,
        data={
            "list": [item.to_dict() for item in result],
            "total": total,
        },
    )


@router.delete("/transfer", summary="删除整理记录", response_model=schemas.Response)
def delete_transfer_history(
    history_in: schemas.TransferHistory,
    deletesrc: Optional[bool] = False,
    deletedest: Optional[bool] = False,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_active_manage_user),
) -> Any:
    """
    删除整理记录
    """
    history: TransferHistory = TransferHistory.get(db, history_in.id)
    if not history:
        return schemas.Response(success=False, message="记录不存在")
    # 册除媒体库文件
    if deletedest and history.dest_fileitem:
        dest_fileitem = schemas.FileItem(**history.dest_fileitem)
        StorageChain().delete_media_file(dest_fileitem)

    # 删除源文件
    if deletesrc and history.src_fileitem:
        src_fileitem = schemas.FileItem(**history.src_fileitem)
        state = StorageChain().delete_media_file(src_fileitem)
        if not state:
            return schemas.Response(
                success=False, message=f"{src_fileitem.path} 删除失败"
            )
        # 删除下载记录中关联的文件
        DownloadFiles.delete_by_fullpath(db, Path(src_fileitem.path).as_posix())
        # 发送事件
        eventmanager.send_event(
            EventType.DownloadFileDeleted,
            {"src": history.src, "hash": history.download_hash},
        )
    # 删除记录
    TransferHistory.delete(db, history_in.id)
    return schemas.Response(success=True)




@router.get("/empty/transfer", summary="清空整理记录", response_model=schemas.Response)
async def empty_transfer_history(
    db: AsyncSession = Depends(get_async_db),
    _: User = Depends(get_current_active_superuser_async),
) -> Any:
    """
    清空整理记录
    """
    await TransferHistory.async_truncate(db)
    return schemas.Response(success=True)
