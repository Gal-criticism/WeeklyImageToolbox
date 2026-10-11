"""Small, safe diagnostics shared by desktop and pipeline code."""
from __future__ import annotations

import re
import sqlite3
import urllib.parse
import json
import httpx
from typing import Any


_SECRET = re.compile(r"(?i)(authorization|api[_-]?key|token|key|password|secret|signature)(\s*[=:]\s*)([^\s&;,]+)")
_BEARER = re.compile(r"(?i)\bBearer\s+[^\s,;]+")
_KEY_VALUE = re.compile(r"\b\d{3,}\|[A-Za-z0-9_-]{24,}\b")


def _safe_message(value: str) -> str:
    value = _BEARER.sub("Bearer [已隐藏]", value)
    value = _KEY_VALUE.sub("[已隐藏]", value)
    value = _SECRET.sub(lambda m: f"{m.group(1)}{m.group(2)}[已隐藏]", value)
    # URLs may carry secrets in query parameters.
    def clean_url(match: re.Match[str]) -> str:
        raw = match.group(0)
        try:
            parts = urllib.parse.urlsplit(raw)
            query = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
            query = [(k, "[已隐藏]" if re.search(r"(?i)(api[_-]?key|token|key|authorization|password|secret|signature)", k) else v) for k, v in query]
            return urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path, urllib.parse.urlencode(query), parts.fragment))
        except Exception:
            return raw
    return re.sub(r"https?://[^\s\]\[()<>]+", clean_url, value)


def diagnose_error(error: BaseException, stage: str = "unknown", source: str | None = None,
                   candidate_id: str | None = None) -> dict[str, Any]:
    """Return a stable, redacted diagnostic record for a caught error."""
    import json
    import urllib.error
    import urllib
    import socket

    response = getattr(error, "response", None)
    status = getattr(response, "status_code", None) or getattr(error, "code", None) or getattr(error, "status", None)
    if isinstance(error, (TimeoutError, socket.timeout, httpx.TimeoutException)):
        code, retryable, message = "timeout", True, "请求超时，可稍后重试"
    elif isinstance(error, urllib.error.HTTPError) or status:
        status = int(status) if str(status).isdigit() else status
        code = "http_429" if status == 429 else "http_error"
        retryable = status == 429 or (isinstance(status, int) and status >= 500)
        message = "请求过于频繁，请稍后重试" if status == 429 else f"服务请求失败（HTTP {status}）"
    elif isinstance(error, PermissionError):
        code, retryable, message = "file_permission", True, "文件无法写入或正被占用，请关闭占用程序并检查目录权限后重试"
    elif isinstance(error, FileNotFoundError):
        code, retryable, message = "file_missing", False, "所需文件不存在，请检查任务目录与来源记录"
    elif isinstance(error, sqlite3.OperationalError) and "locked" in str(error).lower():
        code, retryable, message = "sqlite_locked", True, "本地数据库正被占用，请稍后重试"
    elif (isinstance(error, (json.JSONDecodeError, UnicodeDecodeError))
          or isinstance(getattr(error, "__cause__", None), (json.JSONDecodeError, UnicodeDecodeError))):
        code, retryable, message = "corrupt_json", False, "数据格式损坏，请保留原文件并检查该记录"
    elif (type(error).__name__ in {"UnidentifiedImageError", "DecompressionBombError", "ImageConversionError"}
          or isinstance(error, OSError) and any(value in str(error).casefold()
                for value in ("broken data stream", "image file is truncated", "cannot identify image"))):
        code, retryable, message = "image_decode", False, "图片无法解码或转换，请查看原图与失败记录"
    elif isinstance(error, (ConnectionError, urllib.error.URLError, httpx.NetworkError, httpx.RemoteProtocolError)):
        code, retryable, message = "connect", True, "连接失败，请检查网络或代理设置后重试"
    elif isinstance(error, sqlite3.Error):
        code, retryable, message = "sqlite_error", False, "本地任务数据库读取失败，请保留数据并查看技术详情"
    else:
        code, retryable = "error", False
        text = _safe_message(str(error))
        message = text if re.search(r"[\u4e00-\u9fff]", text) else "操作失败，请查看技术详情中的原始诊断"
    result = {"stage": str(stage), "source": _safe_message(source) if source else None, "candidate": _safe_message(str(candidate_id)) if candidate_id else None,
              "code": code, "retryable": retryable, "message": _safe_message(message),
              "type": type(error).__name__, "http_status": status,
              "detail": _safe_message(str(error)) or type(error).__name__}
    return result


def format_error(error: BaseException, stage: str = "unknown") -> str:
    return diagnose_error(error, stage=stage)["message"]


def sanitize_diagnostic(value):
    if isinstance(value, str):
        return _safe_message(value)
    if isinstance(value, dict):
        return {str(key): "[已隐藏]" if re.fullmatch(r"(?i)(authorization|api[_-]?key|token|secret|password)", str(key))
                else sanitize_diagnostic(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [sanitize_diagnostic(item) for item in value]
    return value
