from pathlib import Path
from typing import Any, List, Annotated, Optional

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app import schemas
from app.chain.media import MediaChain
from app.chain.transfer import TransferChain
from app.core.config import settings, global_vars
from app.core.security import verify_token, verify_apitoken
from app.db import get_db
from app.db.models import User
from app.db.models.transferhistory import TransferHistory
from app.db.user_oper import get_current_admin
from app.helper.directory import DirectoryHelper
from app.log import logger
from app.schemas import (
    MediaType,
    FileItem,
    ManualTransferItem,
    EpisodeFormatRecommendItem,
)

router = APIRouter()


@router.get("/name", summary="查询整理后的名称", response_model=schemas.Response)
def query_name(
    path: str, filetype: str, _: schemas.TokenPayload = Depends(verify_token)
) -> Any:
    """
    查询整理后的名称
    :param path: 文件路径
    :param filetype: 文件类型
    :param _: Token校验
    """
    context = MediaChain().recognize_by_path(
        path,
        obtain_images=False,
    )
    if not context or not context.media_info:
        return schemas.Response(success=False, message="未识别到媒体信息")
    new_path = TransferChain().recommend_name(
        meta=context.meta_info, mediainfo=context.media_info
    )
    if not new_path:
        return schemas.Response(success=False, message="未识别到新名称")
    if filetype == "dir":
        media_path = DirectoryHelper.get_media_root_path(
            rename_format=settings.RENAME_FORMAT(context.media_info.type),
            rename_path=Path(new_path),
        )
        if media_path:
            new_name = media_path.name
        else:
            # fallback
            parents = Path(new_path).parents
            if len(parents) > 2:
                new_name = parents[1].name
            else:
                new_name = parents[0].name
    else:
        new_name = Path(new_path).name
    return schemas.Response(success=True, data={"name": new_name})


@router.get("/queue", summary="查询整理队列", response_model=List[schemas.TransferJob])
async def query_queue(_: schemas.TokenPayload = Depends(verify_token)) -> Any:
    """
    查询整理队列
    :param _: Token校验
    """
    return TransferChain().get_queue_tasks()


@router.delete(
    "/queue", summary="从整理队列中删除任务", response_model=schemas.Response
)
async def remove_queue(
    fileitem: schemas.FileItem, _: schemas.TokenPayload = Depends(verify_token)
) -> Any:
    """
    查询整理队列
    :param fileitem: 文件项
    :param _: Token校验
    """
    TransferChain().remove_from_queue(fileitem)
    # 取消整理
    global_vars.stop_transfer(fileitem.path)
    return schemas.Response(success=True)


def _get_history_source_fileitem(history: TransferHistory) -> FileItem:
    """
    根据整理历史推导重新整理的源文件项：成功移动的记录使用目的文件，其余使用原始源文件。
    """
    if history.status and ("move" in history.mode):
        return FileItem(**history.dest_fileitem)
    return FileItem(**history.src_fileitem)


def _resolve_manual_transfer_source_fileitems(
    transer_item: ManualTransferItem, db: Session
) -> tuple[List[FileItem], Optional[str]]:
    """
    从手动整理请求中解析源文件项。
    """
    if transer_item.logids:
        fileitems: List[FileItem] = []
        for logid in transer_item.logids:
            history: TransferHistory = TransferHistory.get(db, logid)
            if not history:
                return [], f"整理记录不存在，ID：{logid}"
            fileitems.append(_get_history_source_fileitem(history))
        return fileitems, None

    if transer_item.logid:
        history: TransferHistory = TransferHistory.get(db, transer_item.logid)
        if not history:
            return [], f"整理记录不存在，ID：{transer_item.logid}"
        return [_get_history_source_fileitem(history)], None

    if transer_item.fileitems:
        return [fileitem for fileitem in transer_item.fileitems if fileitem], None
    if transer_item.fileitem:
        return [transer_item.fileitem], None
    return [], None


def _get_manual_transfer_histories(
    transer_item: ManualTransferItem, db: Session
) -> tuple[List[TransferHistory], Optional[str]]:
    """
    按请求中的 logids（优先）或 logid 批量查询整理历史记录，任一记录不存在时返回错误信息。
    """
    logids = transer_item.logids or ([transer_item.logid] if transer_item.logid else [])
    histories: List[TransferHistory] = []
    for logid in logids:
        history: TransferHistory = TransferHistory.get(db, logid)
        if not history:
            return [], f"整理记录不存在，ID：{logid}"
        histories.append(history)
    return histories, None


def _build_manual_transfer_options(
    transer_item: ManualTransferItem, history: Optional[TransferHistory] = None
) -> dict:
    """
    生成单个整理源的识别与下载上下文。

    未携带历史记录时直接使用请求参数；携带历史记录时，只在复用历史识别信息的情况下
    用历史记录覆盖媒体、季集与下载器信息，并在非预览、非重新整理时记录需清理的旧目的文件。
    """
    options: dict = {
        "downloader": None,
        "download_hash": None,
        "cleanup_dest_fileitem": None,
        "type_name": transer_item.type_name,
        "tmdbid": transer_item.tmdbid,
        "doubanid": transer_item.doubanid,
        "bangumiid": transer_item.bangumiid,
        "anilistid": transer_item.anilistid,
        "media_source": transer_item.media_source,
        "media_id": transer_item.media_id,
        "season": transer_item.season,
        "episode_group": transer_item.episode_group,
        "episode_detail": transer_item.episode_detail,
    }
    if history is None:
        return options

    if (
        not (history.status and ("move" in history.mode))
        and history.dest_fileitem
        and not transer_item.preview
        and not transer_item.reorganize
    ):
        options["cleanup_dest_fileitem"] = FileItem(**history.dest_fileitem)

    if not transer_item.from_history:
        return options

    # 下载器与 Hash 是同一组下载上下文，重新识别时由当前文件路径重新匹配。
    options["downloader"] = history.downloader
    options["download_hash"] = history.download_hash
    options["type_name"] = history.type if history.type else transer_item.type_name
    options["tmdbid"] = int(history.tmdbid) if history.tmdbid else transer_item.tmdbid
    options["doubanid"] = (
        str(history.doubanid) if history.doubanid else transer_item.doubanid
    )
    options["bangumiid"] = history.bangumiid or transer_item.bangumiid
    options["anilistid"] = history.anilistid or transer_item.anilistid
    options["media_source"] = history.media_source or transer_item.media_source
    options["media_id"] = history.media_id or transer_item.media_id
    options["season"] = (
        int(str(history.seasons).replace("S", ""))
        if history.seasons
        else transer_item.season
    )
    options["episode_group"] = history.episode_group or transer_item.episode_group
    if history.episodes:
        if "-" in str(history.episodes):
            # E01-E03多集合并
            episode_start, episode_end = str(history.episodes).split("-")
            episode_list: list[int] = list(
                range(
                    int(episode_start.replace("E", "")),
                    int(episode_end.replace("E", "")) + 1,
                )
            )
            options["episode_detail"] = ",".join(str(e) for e in episode_list)
        else:
            # E01单集
            options["episode_detail"] = str(history.episodes).replace("E", "")
    return options


def _deduplicate_fileitems(fileitems: List[FileItem]) -> List[FileItem]:
    """
    按存储和路径去重文件项。
    """
    dedup_fileitems: List[FileItem] = []
    seen_paths = set()
    for current_fileitem in fileitems:
        storage = current_fileitem.storage or "local"
        path = current_fileitem.path
        if not path:
            continue
        key = (storage, path)
        if key in seen_paths:
            continue
        seen_paths.add(key)
        dedup_fileitems.append(current_fileitem)
    return dedup_fileitems


def _build_manual_transfer_target_path(
    directory: Optional[schemas.TransferDirectoryConf] = None,
) -> schemas.ManualTransferTargetPath:
    """
    根据目录配置生成手动整理目的路径响应。
    """
    if not directory or not directory.library_path:
        return schemas.ManualTransferTargetPath()

    return schemas.ManualTransferTargetPath(
        target_storage=directory.library_storage or "local",
        target_path=directory.library_path,
        transfer_type=directory.transfer_type,
        scrape=directory.scraping or False,
        library_type_folder=directory.library_type_folder or False,
        library_category_folder=directory.library_category_folder or False,
    )


def _get_manual_transfer_target_key(
    directory: schemas.TransferDirectoryConf,
) -> tuple[Optional[str], Optional[str]]:
    """
    生成目的目录唯一键。
    """
    return (
        directory.library_storage or "local",
        Path(directory.library_path).as_posix() if directory.library_path else None,
    )


@router.post(
    "/manual/target-path",
    summary="匹配手动转移目的路径",
    response_model=schemas.Response,
)
def match_manual_transfer_target_path(
    transer_item: ManualTransferItem,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_admin),
) -> Any:
    """
    根据源文件匹配手动整理目的路径。

    :param transer_item: 手工整理项
    :param db: 数据库
    :param _: Token校验
    """
    src_fileitems, error_message = _resolve_manual_transfer_source_fileitems(
        transer_item=transer_item,
        db=db,
    )
    if error_message:
        return schemas.Response(success=False, message=error_message)

    matched_directories: List[schemas.TransferDirectoryConf] = []
    target_storage = transer_item.target_storage or None
    for src_fileitem in _deduplicate_fileitems(src_fileitems):
        directory = DirectoryHelper().get_dir(
            media=None,
            storage=src_fileitem.storage or "local",
            src_path=Path(src_fileitem.path),
            target_storage=target_storage,
        )
        if not directory or not directory.library_path:
            return schemas.Response(
                success=True,
                data=schemas.ManualTransferTargetPath().model_dump(),
            )
        matched_directories.append(directory)

    if not matched_directories:
        return schemas.Response(
            success=True,
            data=schemas.ManualTransferTargetPath().model_dump(),
        )

    first_directory = matched_directories[0]
    first_key = _get_manual_transfer_target_key(first_directory)
    if any(
        _get_manual_transfer_target_key(directory) != first_key
        for directory in matched_directories[1:]
    ):
        return schemas.Response(
            success=True,
            data=schemas.ManualTransferTargetPath().model_dump(),
        )

    return schemas.Response(
        success=True,
        data=_build_manual_transfer_target_path(first_directory).model_dump(),
    )


@router.post(
    "/manual/history",
    summary="查询手动转移成功历史",
    response_model=schemas.Response,
)
def query_manual_transfer_history(
    transer_item: ManualTransferItem,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_admin),
) -> Any:
    """
    查询文件或目录命中的成功整理记录。

    :param transer_item: 手工整理项
    :param db: 数据库
    :param _: Token校验
    """
    src_fileitems, error_message = _resolve_manual_transfer_source_fileitems(
        transer_item=transer_item,
        db=db,
    )
    if error_message:
        return schemas.Response(success=False, message=error_message)

    histories = TransferChain().get_manual_transfer_histories(
        _deduplicate_fileitems(src_fileitems)
    )
    history_info = schemas.ManualTransferHistoryInfo(
        reorganize=bool(histories),
        history_count=len(histories),
    )
    return schemas.Response(success=True, data=history_info.model_dump())


@router.post("/manual", summary="手动转移", response_model=schemas.Response)
def manual_transfer(
    transer_item: ManualTransferItem,
    background: Optional[bool] = False,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_admin),
) -> Any:
    """
    手动转移，文件或历史记录，支持自定义剧集识别格式
    :param transer_item: 手工整理项
    :param background: 后台运行
    :param db: 数据库
    :param _: Token校验
    """
    target_path = Path(transer_item.target_path) if transer_item.target_path else None

    # 每个整理源携带各自的识别上下文（历史记录来源会带上自己的媒体、季集与下载信息）
    sources: List[dict] = []
    if transer_item.logids or transer_item.logid:
        histories, error_message = _get_manual_transfer_histories(transer_item, db)
        if error_message:
            return schemas.Response(success=False, message=error_message)
        for history in histories:
            sources.append(
                {
                    "fileitem": _get_history_source_fileitem(history),
                    # 历史记录来源强制转移
                    "force": True,
                    "options": _build_manual_transfer_options(transer_item, history),
                }
            )
    else:
        src_fileitems, _error = _resolve_manual_transfer_source_fileitems(
            transer_item=transer_item, db=db
        )
        for src_fileitem in src_fileitems:
            sources.append(
                {
                    "fileitem": src_fileitem,
                    "force": False,
                    "options": _build_manual_transfer_options(transer_item),
                }
            )

    # 按存储和路径去重，保留首次出现的整理源
    dedup_sources: List[dict] = []
    seen_paths = set()
    for source in sources:
        current_fileitem = source["fileitem"]
        storage = current_fileitem.storage or "local"
        path = current_fileitem.path
        if not path:
            continue
        key = (storage, path)
        if key in seen_paths:
            continue
        seen_paths.add(key)
        dedup_sources.append(source)
    sources = dedup_sources
    if not sources:
        return schemas.Response(success=False, message="缺少参数")

    # 逐个整理源解析媒体类型与自定义格式，发现非法参数时在执行任何整理前返回
    for source in sources:
        options = source["options"]
        # 类型（“自动/auto/none”按未指定处理）
        mtype = None
        type_name = str(options["type_name"]).strip() if options["type_name"] else ""
        if type_name and type_name.lower() not in {"自动", "auto", "none"}:
            try:
                mtype = MediaType(type_name)
            except ValueError:
                return schemas.Response(
                    success=False, message=f"不支持的媒体类型：{type_name}"
                )
        # 自定义格式
        epformat = None
        if (
            transer_item.episode_offset
            or transer_item.episode_part
            or options["episode_detail"]
            or transer_item.episode_format
        ):
            epformat = schemas.EpisodeFormat(
                format=transer_item.episode_format,
                detail=options["episode_detail"],
                part=transer_item.episode_part,
                offset=transer_item.episode_offset,
            )
        source["mtype"] = mtype
        source["epformat"] = epformat

    def _run_chain_manual_transfer(source: dict, sync_extra_files: bool):
        """
        按整理源自身的上下文调用整理链手动整理。
        """
        options = source["options"]
        return TransferChain().manual_transfer(
            fileitem=source["fileitem"],
            target_storage=transer_item.target_storage,
            target_path=target_path,
            tmdbid=options["tmdbid"],
            doubanid=options["doubanid"],
            bangumiid=options["bangumiid"],
            anilistid=options["anilistid"],
            media_source=options["media_source"],
            media_id=options["media_id"],
            mtype=source["mtype"],
            season=options["season"],
            episode_group=options["episode_group"],
            transfer_type=transer_item.transfer_type,
            epformat=source["epformat"],
            min_filesize=transer_item.min_filesize,
            scrape=transer_item.scrape,
            library_type_folder=transer_item.library_type_folder,
            library_category_folder=transer_item.library_category_folder,
            force=source["force"],
            background=background,
            downloader=options["downloader"],
            download_hash=options["download_hash"],
            preview=transer_item.preview,
            reorganize=transer_item.reorganize,
            sync_extra_files=sync_extra_files,
            cleanup_dest_fileitem=options["cleanup_dest_fileitem"],
        )

    explicit_selected_files = bool(transer_item.fileitems)
    # 前端显式选择的文件或多条历史记录需要逐个处理并汇总结果
    batch_mode = explicit_selected_files or len(sources) > 1

    def _build_failure_preview_item(file_item: FileItem, message: str) -> dict:
        """
        构造手动整理预览失败项。
        """
        return {
            "source": file_item.path if file_item else None,
            "target": None,
            "target_dir": None,
            "success": False,
            "message": message,
            "type": None,
            "title": None,
            "season": None,
            "episode": None,
            "episode_end": None,
            "part": None,
            "org_string": None,
            "apply_words": [],
            "resource_team": None,
            "customization": None,
        }

    def _merge_messages(messages: List[str]) -> str:
        """
        合并手动整理批量预览提示信息。
        """
        valid_messages = [msg for msg in messages if msg]
        if not valid_messages:
            return ""
        return "、".join(valid_messages[:2]) + (
            f"，等{len(valid_messages)}条消息" if len(valid_messages) > 2 else ""
        )

    # 批量处理：显式文件列表按选中的文件逐个处理，避免将目录整体展开；历史记录按各自上下文逐条处理。
    if batch_mode:
        preview_items: List[dict] = []
        error_messages: List[str] = []
        all_success = True
        for source in sources:
            src_fileitem = source["fileitem"]
            state, errormsg = _run_chain_manual_transfer(
                source, sync_extra_files=not explicit_selected_files
            )
            if transer_item.preview:
                if isinstance(errormsg, dict):
                    preview_items.extend(errormsg.get("items") or [])
                    if errormsg.get("message"):
                        error_messages.append(errormsg.get("message"))
                    if not state:
                        all_success = False
                else:
                    if errormsg:
                        error_messages.append(str(errormsg))
                    preview_items.append(
                        _build_failure_preview_item(src_fileitem, str(errormsg))
                    )
                    all_success = False
            elif not state:
                all_success = False
                if isinstance(errormsg, list):
                    error_messages.extend([str(msg) for msg in errormsg if msg])
                elif errormsg:
                    error_messages.append(str(errormsg))

        if transer_item.preview:
            merged_preview_items: List[dict] = []
            seen_sources = set()
            for preview_item in preview_items:
                source_path = preview_item.get("source")
                if source_path in seen_sources:
                    continue
                seen_sources.add(source_path)
                merged_preview_items.append(preview_item)
            merged_message = _merge_messages(error_messages)
            preview_data = {
                "summary": {
                    "total": len(merged_preview_items),
                    "success": len(
                        [item for item in merged_preview_items if item.get("success")]
                    ),
                    "failed": len(
                        [item for item in merged_preview_items if not item.get("success")]
                    ),
                },
                "items": merged_preview_items,
                "message": merged_message,
            }
            return schemas.Response(
                success=True,
                message=merged_message or None,
                data=preview_data,
            )

        if not all_success:
            return schemas.Response(
                success=False,
                message=_merge_messages(error_messages),
            )
        return schemas.Response(success=True)

    # 开始转移
    state, errormsg = _run_chain_manual_transfer(sources[0], sync_extra_files=True)
    # 失败
    if not state:
        if isinstance(errormsg, list):
            errormsg = f"整理完成，{len(errormsg)} 个文件转移失败！"
        if isinstance(errormsg, dict):
            return schemas.Response(
                success=True,
                message=errormsg.get("message"),
                data=errormsg,
            )
        return schemas.Response(success=False, message=errormsg)
    # 成功
    if transer_item.preview:
        return schemas.Response(success=True, data=errormsg or {})
    return schemas.Response(success=True)


@router.post(
    "/episode-format/recommend",
    summary="推荐集数定位模板",
    response_model=schemas.Response,
)
def recommend_episode_format(
    recommend_item: EpisodeFormatRecommendItem,
    _: User = Depends(get_current_admin),
) -> Any:
    """
    根据目录样本推荐集数定位模板
    :param recommend_item: 推荐请求
    :param _: Token校验
    """
    target_path = recommend_item.fileitem.path if recommend_item.fileitem else None
    logger.info(f"开始推荐集数定位模板：{target_path}")
    state, errmsg, data = TransferChain().recommend_episode_format(
        fileitem=recommend_item.fileitem,
        fileitems=recommend_item.fileitems,
    )
    if not state:
        logger.warn(f"推荐集数定位模板失败：{target_path} - {errmsg}")
        return schemas.Response(success=False, message=errmsg)
    logger.info(
        f"推荐集数定位模板成功：{target_path} - 规则 {data.get('rule_name') if data else None}"
    )
    return schemas.Response(success=True, data=data)


@router.get("/now", summary="立即执行下载器文件整理", response_model=schemas.Response)
def now(_: Annotated[str, Depends(verify_apitoken)]) -> Any:
    """
    立即执行下载器文件整理 API_TOKEN认证（?token=xxx）
    """
    TransferChain().process()
    return schemas.Response(success=True)
