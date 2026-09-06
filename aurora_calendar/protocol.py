"""Aurora Tool 契约的独立服务端边界，不导入 Host 源码。"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from typing import Any

from mcp.server.extension import Extension
from mcp.types import CallToolResult, TextContent

CONTRACT = "org.aurorabot/tool-contract"
_logger = logging.getLogger(__name__)


class ToolContract(Extension):
    identifier = CONTRACT

    def settings(self) -> dict[str, Any]:
        return {"version": 1}


def success(data: dict[str, object]) -> CallToolResult:
    text = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return CallToolResult(content=[TextContent(text=text)], structured_content=data)


def failure(message: str, *, unknown: bool = False) -> CallToolResult:
    meta = {CONTRACT: {"status": "unknown"}} if unknown else None
    return CallToolResult(content=[TextContent(text=message)], is_error=True, _meta=meta)


def invoke(action: Callable[[], dict[str, object]], *, mutating: bool = False) -> CallToolResult:
    """明确校验拒绝为 failed；无法确认写入效果时保守返回 unknown。"""
    try:
        return success(action())
    except (ValueError, LookupError) as error:
        return failure(str(error))
    except OSError:
        return failure("存储访问失败；写入效果无法确认" if mutating else "存储读取失败", unknown=mutating)
    except Exception as error:
        _logger.error("工具边界异常，类型=%s", type(error).__name__)
        return failure("工具执行异常，效果无法确认" if mutating else "工具读取异常", unknown=mutating)
