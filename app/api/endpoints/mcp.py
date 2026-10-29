"""
提供 MCP 工具发现与调用的 HTTP 接入（JSON-RPC 2.0 over Streamable HTTP）
"""

import json
from dataclasses import dataclass
from typing import Annotated, Any, Awaitable, Callable, Dict, List, Optional, Union

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, Response

from app import schemas
from app.chain.download import DownloadChain
from app.chain.media import MediaChain
from app.chain.subscribe import SubscribeChain
from app.core.config import settings
from app.core.event import eventmanager
from app.core.security import verify_apikey
from app.db.subscribe_oper import SubscribeOper
from app.log import logger
from app.schemas.mcp import McpJsonRpcError, McpJsonRpcResponse, McpToolInfo, ToolCallData, ToolCallRequest
from app.schemas.types import EventType, MediaType
from version import APP_VERSION

router = APIRouter()

# MCP 协议版本
MCP_PROTOCOL_VERSIONS = ["2025-11-25", "2025-06-18", "2024-11-05"]
# 默认使用最新版本
MCP_PROTOCOL_VERSION = MCP_PROTOCOL_VERSIONS[0]


@dataclass
class McpTool:
    """
    MCP 工具定义：名称、说明、参数 JSON Schema 与异步处理器
    """
    name: str
    description: str
    input_schema: Dict[str, Any]
    handler: Callable[[Dict[str, Any]], Awaitable[str]]


def _model_to_dict(instance: Any) -> Dict[str, Any]:
    """
    将 ORM 实例已加载字段转为字典，避免异步上下文中触发懒加载
    """
    values = instance.__dict__
    return {column.name: values.get(column.name) for column in instance.__table__.columns}


def _dumps(data: Any) -> str:
    """
    序列化工具结果为 JSON 文本
    """
    return json.dumps(data, ensure_ascii=False, default=str)


async def _tool_media_search(arguments: Dict[str, Any]) -> str:
    """
    按标题搜索媒体信息

    :param arguments: 工具参数，title 必填，source 可选
    :return: 识别元数据与候选媒体列表的 JSON 文本
    """
    title = arguments.get("title")
    if not title or not isinstance(title, str):
        raise ValueError("缺少必填参数 title")
    meta, medias = await run_in_threadpool(
        MediaChain().search, title=title, source=arguments.get("source")
    )
    items = []
    for media in (medias or [])[:10]:
        items.append({
            "title": media.title,
            "year": media.year,
            "type": media.type.value if media.type else None,
            "tmdb_id": media.tmdb_id,
            "douban_id": media.douban_id,
            "bangumi_id": media.bangumi_id,
            "media_source": media.source,
            "media_id": media.media_id,
            "overview": (media.overview or "")[:200],
        })
    result = {
        "meta": {
            "name": getattr(meta, "name", None),
            "year": getattr(meta, "year", None),
            "type": meta.type.value if getattr(meta, "type", None) else None,
            "season": getattr(meta, "begin_season", None),
            "episode": getattr(meta, "begin_episode", None),
        } if meta else None,
        "results": items,
    }
    return _dumps(result)


async def _tool_subscribe_list(arguments: Dict[str, Any]) -> str:
    """
    查询订阅列表

    :param arguments: 工具参数，state 可选（R 订阅中 / P 暂停 / S 完成）
    :return: 订阅列表的 JSON 文本
    """
    subscribes = await run_in_threadpool(SubscribeOper().list, arguments.get("state"))
    return _dumps([_model_to_dict(subscribe) for subscribe in subscribes])


async def _tool_subscribe_add(arguments: Dict[str, Any]) -> str:
    """
    识别媒体信息并添加订阅

    :param arguments: 工具参数，title 必填，year/type/season/tmdbid/media_source/media_id 可选
    :return: 订阅 ID 与结果消息的 JSON 文本
    """
    title = arguments.get("title")
    if not title or not isinstance(title, str):
        raise ValueError("缺少必填参数 title")
    mtype = None
    if arguments.get("type"):
        try:
            mtype = MediaType(arguments["type"])
        except ValueError:
            raise ValueError(f"不支持的媒体类型：{arguments['type']}，仅支持 电影/电视剧")
    season = arguments.get("season")
    if season is not None and not isinstance(season, int):
        raise ValueError("season 必须为整数")
    tmdbid = arguments.get("tmdbid")
    if tmdbid is not None and not isinstance(tmdbid, int):
        raise ValueError("tmdbid 必须为整数")
    sid, message = await SubscribeChain().async_add(
        title=title,
        year=str(arguments.get("year") or ""),
        mtype=mtype,
        season=season,
        tmdbid=tmdbid,
        media_source=arguments.get("media_source"),
        media_id=str(arguments["media_id"]) if arguments.get("media_id") else None,
        username=settings.SUPERUSER,
        exist_ok=True,
    )
    return _dumps({"success": bool(sid), "id": sid, "message": message})


async def _tool_subscribe_delete(arguments: Dict[str, Any]) -> str:
    """
    删除订阅并广播删除事件

    :param arguments: 工具参数，id 必填
    :return: 删除结果的 JSON 文本
    """
    sid = arguments.get("id")
    if not isinstance(sid, int):
        raise ValueError("缺少必填参数 id（整数）")
    subscribe_oper = SubscribeOper()
    subscribe = await run_in_threadpool(subscribe_oper.get, sid)
    if not subscribe:
        raise ValueError(f"订阅不存在：{sid}")
    subscribe_info = _model_to_dict(subscribe)
    await run_in_threadpool(subscribe_oper.delete, sid)
    await eventmanager.async_send_event(
        EventType.SubscribeDeleted,
        {"subscribe_id": sid, "subscribe_info": subscribe_info},
    )
    return _dumps({"success": True, "id": sid})


async def _tool_download_list(arguments: Dict[str, Any]) -> str:
    """
    查询正在下载的任务列表

    :param arguments: 工具参数，name 可选（指定下载器）
    :return: 下载任务列表的 JSON 文本
    """
    torrents = await run_in_threadpool(DownloadChain().downloading, arguments.get("name"))
    return _dumps([torrent.model_dump(mode="json") for torrent in torrents])


async def _tool_download_control(arguments: Dict[str, Any]) -> str:
    """
    控制下载任务

    :param arguments: 工具参数，hash 与 action 必填，name 可选
    :return: 操作结果的 JSON 文本
    """
    hash_str = arguments.get("hash")
    if not hash_str or not isinstance(hash_str, str):
        raise ValueError("缺少必填参数 hash")
    action = arguments.get("action")
    if action not in ("start", "stop", "delete"):
        raise ValueError("action 仅支持 start/stop/delete")
    name = arguments.get("name")
    download_chain = DownloadChain()
    if action == "delete":
        ret = await run_in_threadpool(download_chain.remove_downloading, hash_str, name)
    else:
        ret = await run_in_threadpool(download_chain.set_downloading, hash_str, action, name)
    return _dumps({"success": bool(ret), "hash": hash_str, "action": action})


async def _tool_system_status(arguments: Dict[str, Any]) -> str:
    """
    查询系统运行状态摘要

    :param arguments: 工具参数（无）
    :return: 版本、订阅数与下载中任务数的 JSON 文本
    """
    subscribes = await run_in_threadpool(SubscribeOper().list)
    torrents = await run_in_threadpool(DownloadChain().downloading)
    return _dumps({
        "version": APP_VERSION,
        "subscribe_count": len(subscribes),
        "downloading_count": len(torrents),
    })


# MCP 工具注册表
MCP_TOOLS: List[McpTool] = [
    McpTool(
        name="media.search",
        description="按标题搜索电影、电视剧等媒体信息，返回候选列表（含 TMDB/豆瓣 ID，可用于添加订阅）",
        input_schema={
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "搜索关键词，可含年份、季号，如「庆余年 第二季」"},
                "source": {"type": "string", "description": "请求级搜索数据源，如 themoviedb/douban/bangumi，省略时使用系统设置"},
            },
            "required": ["title"],
        },
        handler=_tool_media_search,
    ),
    McpTool(
        name="subscribe.list",
        description="查询订阅列表",
        input_schema={
            "type": "object",
            "properties": {
                "state": {"type": "string", "description": "订阅状态过滤：R 订阅中 / P 暂停 / S 完成，省略返回全部"},
            },
        },
        handler=_tool_subscribe_list,
    ),
    McpTool(
        name="subscribe.add",
        description="添加订阅。可先用 media.search 获取 tmdbid 等身份信息以提高识别准确度",
        input_schema={
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "媒体标题"},
                "year": {"type": "string", "description": "年份"},
                "type": {"type": "string", "description": "媒体类型：电影/电视剧"},
                "season": {"type": "integer", "description": "季号（电视剧）"},
                "tmdbid": {"type": "integer", "description": "TMDB ID"},
                "media_source": {"type": "string", "description": "媒体来源，如 themoviedb/douban/bangumi/anilist"},
                "media_id": {"type": "string", "description": "来源对应的媒体 ID"},
            },
            "required": ["title"],
        },
        handler=_tool_subscribe_add,
    ),
    McpTool(
        name="subscribe.delete",
        description="按订阅 ID 删除订阅",
        input_schema={
            "type": "object",
            "properties": {
                "id": {"type": "integer", "description": "订阅 ID，可通过 subscribe.list 获取"},
            },
            "required": ["id"],
        },
        handler=_tool_subscribe_delete,
    ),
    McpTool(
        name="download.list",
        description="查询正在下载的任务列表",
        input_schema={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "指定下载器名称，省略时查询默认下载器"},
            },
        },
        handler=_tool_download_list,
    ),
    McpTool(
        name="download.control",
        description="控制下载任务：开始、停止或删除",
        input_schema={
            "type": "object",
            "properties": {
                "hash": {"type": "string", "description": "下载任务 hash，可通过 download.list 获取"},
                "action": {"type": "string", "enum": ["start", "stop", "delete"], "description": "操作类型"},
                "name": {"type": "string", "description": "指定下载器名称，省略时操作默认下载器"},
            },
            "required": ["hash", "action"],
        },
        handler=_tool_download_control,
    ),
    McpTool(
        name="system.status",
        description="查询系统运行状态摘要：版本、订阅数、下载中任务数",
        input_schema={
            "type": "object",
            "properties": {},
        },
        handler=_tool_system_status,
    ),
]

MCP_TOOL_MAP: Dict[str, McpTool] = {tool.name: tool for tool in MCP_TOOLS}


def create_jsonrpc_response(request_id: Union[str, int, None], result: Any) -> Dict[str, Any]:
    """
    创建 JSON-RPC 成功响应
    """
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def create_jsonrpc_error(
        request_id: Union[str, int, None], code: int, message: str, data: Any = None
) -> Dict[str, Any]:
    """
    创建 JSON-RPC 错误响应
    """
    error_detail: Dict[str, Any] = {"code": code, "message": message}
    error = {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": error_detail,
    }
    if data is not None:
        error_detail["data"] = data
    return error


async def handle_initialize(params: Dict[str, Any]) -> Dict[str, Any]:
    """
    处理初始化请求，协商协议版本
    """
    protocol_version = params.get("protocolVersion")
    client_info = params.get("clientInfo", {})

    logger.info(f"MCP 初始化请求: 客户端={client_info.get('name')}, 协议版本={protocol_version}")

    negotiated_version = MCP_PROTOCOL_VERSION
    if protocol_version in MCP_PROTOCOL_VERSIONS:
        negotiated_version = protocol_version
    else:
        logger.warning(f"协议版本不匹配: 客户端={protocol_version}, 使用服务器版本={negotiated_version}")

    return {
        "protocolVersion": negotiated_version,
        "capabilities": {
            "tools": {
                "listChanged": False
            },
        },
        "serverInfo": {
            "name": "MoviePilot",
            "version": APP_VERSION,
            "description": "MoviePilot MCP Server - 影视自动化管理工具",
        },
        "instructions": "MoviePilot MCP 服务器，提供媒体搜索、订阅管理、下载控制等工具。",
    }


async def handle_tools_list() -> Dict[str, Any]:
    """
    处理工具列表请求
    """
    return {
        "tools": [
            {
                "name": tool.name,
                "description": tool.description,
                "inputSchema": tool.input_schema,
            }
            for tool in MCP_TOOLS
        ]
    }


async def handle_tools_call(params: Dict[str, Any]) -> Dict[str, Any]:
    """
    处理工具调用请求，业务异常以 isError 结果返回
    """
    tool_name = params.get("name")
    arguments = params.get("arguments", {})

    if not tool_name:
        raise ValueError("缺少工具名称")
    if not isinstance(arguments, dict):
        raise ValueError("arguments 必须为对象")

    tool = MCP_TOOL_MAP.get(tool_name)
    if not tool:
        raise ValueError(f"工具 '{tool_name}' 未找到")

    try:
        result_text = await tool.handler(arguments)
        return {"content": [{"type": "text", "text": result_text}]}
    except ValueError:
        raise
    except Exception as e:
        logger.error(f"工具调用失败: {tool_name}, 错误: {e}", exc_info=True)
        return {
            "content": [{"type": "text", "text": f"错误: {str(e)}"}],
            "isError": True,
        }


async def _dispatch_jsonrpc_method(
        method: Any,
        params: Dict[str, Any],
        request_id: Union[str, int, None],
) -> Union[JSONResponse, Response]:
    """
    分派一个已通过基础格式校验的 MCP JSON-RPC 方法
    """
    if method == "initialize":
        result = await handle_initialize(params)
        return JSONResponse(content=create_jsonrpc_response(request_id, result))
    if method == "notifications/initialized":
        if request_id is None:
            return Response(status_code=204)
        return JSONResponse(
            status_code=400,
            content=create_jsonrpc_error(request_id, -32600, "initialized must be a notification"),
        )
    if method == "tools/list":
        result = await handle_tools_list()
        return JSONResponse(content=create_jsonrpc_response(request_id, result))
    if method == "tools/call":
        result = await handle_tools_call(params)
        return JSONResponse(content=create_jsonrpc_response(request_id, result))
    if method == "ping":
        return JSONResponse(content=create_jsonrpc_response(request_id, {}))
    return JSONResponse(content=create_jsonrpc_error(request_id, -32601, f"Method not found: {method}"))


@router.post(
    "",
    summary="MCP JSON-RPC 端点",
    response_model=McpJsonRpcResponse,
    responses={
        400: {"model": McpJsonRpcError, "description": "JSON-RPC 请求错误"},
        204: {"description": "JSON-RPC 通知已接收"},
    },
)
async def mcp_jsonrpc(
        request: Request, _: Annotated[Union[str, None], Depends(verify_apikey)] = None
) -> Union[JSONResponse, Response]:
    """
    MCP 标准 JSON-RPC 2.0 端点

    处理所有 MCP 协议消息（初始化、工具列表、工具调用等）
    """
    try:
        body = await request.json()
    except Exception as e:
        logger.error(f"解析请求体失败: {e}")
        return JSONResponse(
            status_code=400,
            content=create_jsonrpc_error(None, -32700, "Parse error", str(e)),
        )

    # 验证 JSON-RPC 格式
    if not isinstance(body, dict) or body.get("jsonrpc") != "2.0":
        return JSONResponse(
            status_code=400,
            content=create_jsonrpc_error(
                body.get("id") if isinstance(body, dict) else None, -32600, "Invalid Request"
            ),
        )

    method = body.get("method")
    params = body.get("params", {})
    request_id = body.get("id")

    try:
        return await _dispatch_jsonrpc_method(method, params, request_id)
    except ValueError as e:
        logger.warning(f"MCP 请求参数错误: {e}")
        return JSONResponse(
            status_code=400,
            content=create_jsonrpc_error(request_id, -32602, "Invalid params", str(e)),
        )
    except Exception as e:
        logger.error(f"处理 MCP 请求失败: {e}", exc_info=True)
        return JSONResponse(
            status_code=500,
            content=create_jsonrpc_error(request_id, -32603, "Internal error", str(e)),
        )


@router.delete(
    "",
    summary="终止 MCP 会话",
    status_code=204,
    response_class=Response,
    response_model=None,
)
async def delete_mcp_session(
        _: Annotated[Union[str, None], Depends(verify_apikey)] = None,
) -> Response:
    """
    终止 MCP 会话（无状态模式下仅返回成功）
    """
    return Response(status_code=204)


@router.get(
    "/tools",
    summary="列出所有可用工具",
    response_model=List[McpToolInfo],
)
async def list_tools(_: Annotated[str, Depends(verify_apikey)]) -> Any:
    """
    获取所有可用的工具列表

    返回每个工具的名称、描述和参数定义
    """
    return [
        {
            "name": tool.name,
            "description": tool.description,
            "inputSchema": tool.input_schema,
        }
        for tool in MCP_TOOLS
    ]


@router.post(
    "/tools/call",
    summary="调用工具",
    response_model=schemas.Response,
)
async def call_tool(request: ToolCallRequest, _: Annotated[Union[str, None], Depends(verify_apikey)] = None) -> Any:
    """
    调用指定工具（REST 便捷入口，等价于 JSON-RPC tools/call）

    :param request: 工具名称和对应参数
    :param _: 已通过 API Key 校验的依赖结果
    :return: 工具执行结果；工具不存在或参数错误时返回失败响应
    """
    tool = MCP_TOOL_MAP.get(request.tool_name)
    if not tool:
        return schemas.Response(success=False, message=f"工具 '{request.tool_name}' 未找到")
    try:
        result_text = await tool.handler(request.arguments)
        return schemas.Response(success=True, data=ToolCallData(result=result_text).model_dump())
    except Exception as e:
        logger.error(f"调用工具 {request.tool_name} 失败: {e}", exc_info=True)
        return schemas.Response(success=False, message=f"调用工具失败: {str(e)}")


@router.get(
    "/tools/{tool_name}",
    summary="获取工具详情",
    response_model=McpToolInfo,
)
async def get_tool_info(tool_name: str, _: Annotated[str, Depends(verify_apikey)]) -> Any:
    """
    获取指定工具的详细信息

    :param tool_name: 工具名称
    :param _: 已验证的 API Key
    :return: 工具名称、描述和参数 Schema
    :raises HTTPException: 工具不存在时为 404
    """
    tool = MCP_TOOL_MAP.get(tool_name)
    if not tool:
        raise HTTPException(status_code=404, detail=f"工具 '{tool_name}' 未找到")
    return {
        "name": tool.name,
        "description": tool.description,
        "inputSchema": tool.input_schema,
    }
