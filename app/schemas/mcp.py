"""
MCP 协议与工具调用的数据结构
"""

from typing import Any, Dict, Optional, Union

from pydantic import BaseModel, Field


class ToolCallRequest(BaseModel):
    """
    工具调用请求模型
    """
    tool_name: str = Field(..., description="工具名称")
    arguments: Dict[str, Any] = Field(default_factory=dict, description="工具参数")


class ToolCallData(BaseModel):
    """
    工具调用成功后的业务数据
    """
    result: str = Field(description="工具执行结果")


class McpToolInfo(BaseModel):
    """
    MCP 工具摘要
    """
    name: str = Field(description="工具名称")
    description: str = Field(default="", description="工具说明")
    inputSchema: Dict[str, Any] = Field(description="工具参数 JSON Schema")


class McpJsonRpcErrorBody(BaseModel):
    """
    JSON-RPC 错误对象
    """
    code: int = Field(description="错误码")
    message: str = Field(description="错误信息")
    data: Optional[Any] = Field(default=None, description="附加错误数据")


class McpJsonRpcError(BaseModel):
    """
    JSON-RPC 错误响应
    """
    jsonrpc: str = Field(default="2.0", description="协议版本")
    id: Optional[Union[str, int]] = Field(default=None, description="请求ID")
    error: McpJsonRpcErrorBody = Field(description="错误对象")


class McpJsonRpcResponse(BaseModel):
    """
    JSON-RPC 成功响应
    """
    jsonrpc: str = Field(default="2.0", description="协议版本")
    id: Optional[Union[str, int]] = Field(default=None, description="请求ID")
    result: Any = Field(default=None, description="调用结果")
