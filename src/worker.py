import asyncio
import json
import traceback

from js import Response as JsResponse, Headers as JsHeaders, Object, fetch as js_fetch, URL as JsURL
from pyodide.ffi import to_js
from workers import WorkerEntrypoint

# ============================================================================
# 预序列化大型 JSON（在 snapshot 阶段执行，无 CPU 限制）
# 请求时通过字符串拼接注入，避免 json.dumps 超过 10ms CPU 限制
# ============================================================================

_TOOLS_JSON_STR = r"""[{"name":"Task","description":"Launch a new agent.","input_schema":{"type":"object","properties":{"description":{"type":"string"},"prompt":{"type":"string"},"subagent_type":{"type":"string"},"model":{"type":"string","enum":["sonnet","opus","haiku"]},"resume":{"type":"string"},"run_in_background":{"type":"boolean"}},"required":["description","prompt","subagent_type"]}},{"name":"TaskOutput","description":"Retrieves output from a running or completed task.","input_schema":{"type":"object","properties":{"task_id":{"type":"string"},"block":{"type":"boolean","default":true},"timeout":{"type":"number","default":30000}},"required":["task_id"]}},{"name":"Bash","description":"Executes a bash command.","input_schema":{"type":"object","properties":{"command":{"type":"string"},"timeout":{"type":"number"},"description":{"type":"string"}},"required":["command"]}},{"name":"Glob","description":"Fast file pattern matching.","input_schema":{"type":"object","properties":{"pattern":{"type":"string"},"path":{"type":"string"}},"required":["pattern"]}},{"name":"Grep","description":"Search tool built on ripgrep.","input_schema":{"type":"object","properties":{"pattern":{"type":"string"},"path":{"type":"string"},"glob":{"type":"string"},"output_mode":{"type":"string"}},"required":["pattern"]}},{"name":"Read","description":"Reads a file from the filesystem.","input_schema":{"type":"object","properties":{"file_path":{"type":"string"},"offset":{"type":"number"},"limit":{"type":"number"}},"required":["file_path"]}},{"name":"Edit","description":"Performs exact string replacements in files.","input_schema":{"type":"object","properties":{"file_path":{"type":"string"},"old_string":{"type":"string"},"new_string":{"type":"string"},"replace_all":{"type":"boolean","default":false}},"required":["file_path","old_string","new_string"]}},{"name":"Write","description":"Writes a file to the filesystem.","input_schema":{"type":"object","properties":{"file_path":{"type":"string"},"content":{"type":"string"}},"required":["file_path","content"]}},{"name":"WebFetch","description":"Fetches content from a URL.","input_schema":{"type":"object","properties":{"url":{"type":"string","format":"uri"},"prompt":{"type":"string"}},"required":["url","prompt"]}},{"name":"WebSearch","description":"Search the web.","input_schema":{"type":"object","properties":{"query":{"type":"string"}},"required":["query"]}},{"name":"AskUserQuestion","description":"Ask the user a question.","input_schema":{"type":"object","properties":{"questions":{"type":"array","items":{"type":"object"}}},"required":["questions"]}}]"""

_SYSTEM_JSON_STR = r"""[{"type":"text","text":"You are Claude Code, Anthropic's official CLI for Claude.","cache_control":{"type":"ephemeral"}},{"type":"text","text":"You are an interactive CLI tool that helps users with software engineering tasks. Use the instructions below and the tools available to you to assist the user.","cache_control":{"type":"ephemeral"}}]"""

# 验证预序列化的 JSON 是合法的（snapshot 阶段执行）
_tools_count = len(json.loads(_TOOLS_JSON_STR))

CONFIG = {
    "TARGET_BASE_URL": "https://anyrouter.top/v1",
}


def make_response(body, status=200, content_type="application/json"):
    h = JsHeaders.new()
    h.set("content-type", content_type)
    return JsResponse.new(body, to_js({"status": status, "headers": h}, dict_converter=Object.fromEntries))


def get_claude_headers(is_stream=False, model=""):
    if "opus" in model.lower() or "sonnet" in model.lower():
        beta = "claude-code-20250219,interleaved-thinking-2025-05-14"
    else:
        beta = "interleaved-thinking-2025-05-14"
    headers = {
        "accept": "application/json",
        "content-type": "application/json",
        "connection": "keep-alive",
        "user-agent": "claude-cli/2.0.76 (external, cli)",
        "anthropic-version": "2023-06-01",
        "anthropic-beta": beta,
        "anthropic-dangerous-direct-browser-access": "true",
        "x-app": "cli",
        "x-stainless-arch": "x64",
        "x-stainless-lang": "js",
        "x-stainless-os": "Windows",
        "x-stainless-package-version": "0.70.0",
        "x-stainless-retry-count": "0",
        "x-stainless-runtime": "node",
        "x-stainless-runtime-version": "v24.3.0",
        "x-stainless-timeout": "600",
    }
    if is_stream:
        headers["x-stainless-helper-method"] = "stream"
    return headers


def extract_api_key(request):
    """从请求头中提取 API key（支持 x-api-key 和 Authorization 两种格式）"""
    req_auth = request.headers.get("Authorization")
    if req_auth:
        return str(req_auth).replace("Bearer ", "")
    raw_key = request.headers.get("x-api-key")
    if raw_key:
        return str(raw_key)
    return None


def collect_debug_info(request, api_key):
    """收集调试信息"""
    info = {
        "has_api_key": api_key is not None,
        "api_key_preview": (api_key[:8] + "..." + api_key[-4:]) if api_key else "NONE",
    }
    try:
        recv_h = {}
        for k, v in request.headers.items():
            k, v = str(k), str(v)
            recv_h[k] = v[:30] + "..." if len(v) > 30 else v
        info["received_headers"] = recv_h
    except Exception:
        pass
    return info


def build_body_string(raw_body_text):
    """
    解析请求 body，返回 (最终 body 字符串, model 名, 是否 stream)。
    关键优化：tools/system 通过字符串拼接注入，不经过 json.dumps。
    """
    if not raw_body_text:
        return "", "", False

    raw = json.loads(raw_body_text)
    safe_keys = {'model', 'messages', 'max_tokens', 'metadata', 'stop_sequences',
                 'stream', 'temperature', 'top_k', 'top_p', 'thinking'}
    body = {k: v for k, v in raw.items() if k in safe_keys}

    model = body.get('model', '')
    if 'anyrouter/' in model:
        model = model.replace('anyrouter/', '')
        body['model'] = model

    wants_stream = body.get('stream', False)
    is_claude = 'sonnet' in model.lower() or 'opus' in model.lower() or 'haiku' in model.lower()

    if is_claude:
        # 对 Claude 模型：注入 thinking, metadata
        if ('sonnet' in model.lower() or 'opus' in model.lower()) and 'thinking' not in body:
            body['thinking'] = {"budget_tokens": 10000, "type": "enabled"}
        body['metadata'] = {"user_id": "proxy_user"}

        # 序列化小 dict（不包含 tools/system），然后字符串拼接注入预序列化的大型 JSON
        small_json = json.dumps(body, separators=(',', ':'), ensure_ascii=False)
        # 去掉末尾的 }，拼接 tools 和 system
        body_str = small_json[:-1] + ',"tools":' + _TOOLS_JSON_STR + ',"system":' + _SYSTEM_JSON_STR + '}'
    else:
        body_str = json.dumps(body, separators=(',', ':'), ensure_ascii=False)

    return body_str, model, wants_stream


class Default(WorkerEntrypoint):
    async def fetch(self, request):
        try:
            parsed = JsURL.new(request.url)
            path = str(parsed.pathname)
            method = str(request.method)

            if path == "/" and method == "GET":
                return make_response(json.dumps({"status": "ok", "version": "v27", "tools_loaded": _tools_count}))

            if path == "/config":
                return make_response(json.dumps({"target": CONFIG["TARGET_BASE_URL"], "tools_count": _tools_count}))

            if path == "/debug":
                return await self.debug_proxy(request)

            if path.startswith("/v1/"):
                return await self.handle_proxy(request, path, method)

            return make_response('{"error":"Not Found"}', status=404)
        except Exception as e:
            return make_response(json.dumps({"error": str(e), "trace": traceback.format_exc()}), status=500)

    async def debug_proxy(self, request):
        """调试端点：显示所有收到的头并测试上游连接"""
        info = {"step": "init", "all_received_headers": {}}
        try:
            for key, val in request.headers.items():
                info["all_received_headers"][str(key)] = str(val)[:60]

            api_key = extract_api_key(request)
            info["auth_source"] = "Authorization" if request.headers.get("Authorization") else ("x-api-key" if request.headers.get("x-api-key") else "NONE")
            if api_key:
                info["api_key_preview"] = api_key[:8] + "..." + api_key[-4:]

            headers = get_claude_headers(model="claude-opus-4-6")
            if api_key:
                headers["x-api-key"] = api_key
                headers["Authorization"] = f"Bearer {api_key}"

            js_h = JsHeaders.new()
            for k, v in headers.items():
                js_h.set(k, str(v))

            body = '{"model":"claude-opus-4-6","max_tokens":10,"messages":[{"role":"user","content":"hi"}]}'
            opts = to_js({"method": "POST", "headers": js_h, "body": body}, dict_converter=Object.fromEntries)

            info["step"] = "fetching"
            resp = await js_fetch("https://anyrouter.top/v1/messages?beta=true", opts)
            info["upstream_status"] = resp.status
            info["upstream_body"] = (await resp.text())[:500]
            info["step"] = "done"

        except Exception as e:
            info["error"] = str(e)
            info["trace"] = traceback.format_exc()

        return make_response(json.dumps(info, indent=2, ensure_ascii=False))

    async def handle_proxy(self, request, path, method):
        sub_path = path[4:]
        target_path = f"{CONFIG['TARGET_BASE_URL']}/{sub_path}"
        if sub_path == "messages":
            target_path += "?beta=true"

        # 提取 API key
        api_key = extract_api_key(request)
        debug_info = collect_debug_info(request, api_key)

        # 解析并构建 body（CPU 敏感 - 使用字符串拼接优化）
        body_str = ""
        model_name = ""
        wants_stream = False
        try:
            body_text = await request.text()
            body_str, model_name, wants_stream = build_body_string(body_text)
        except Exception as e:
            debug_info["body_parse_error"] = str(e)

        # 构建转发头
        headers = get_claude_headers(is_stream=wants_stream, model=model_name)
        if api_key:
            headers["x-api-key"] = api_key
            headers["Authorization"] = f"Bearer {api_key}"

        js_headers = JsHeaders.new()
        for k, v in headers.items():
            js_headers.set(k, str(v))

        fetch_init = {"method": method, "headers": js_headers}
        if body_str and method in ("POST", "PUT", "PATCH"):
            fetch_init["body"] = body_str
        fetch_options = to_js(fetch_init, dict_converter=Object.fromEntries)

        # 重试逻辑
        max_attempts = 3
        for attempt in range(max_attempts):
            try:
                resp = await js_fetch(target_path, fetch_options)
                status = resp.status

                if status in (520, 502):
                    if attempt < max_attempts - 1:
                        await asyncio.sleep(1)
                        continue
                    return make_response('{"error":"Network error after max retries"}', status=502)

                if status in (403, 500):
                    error_text = await resp.text()
                    combined = json.dumps({
                        "upstream_status": status,
                        "upstream_error": error_text[:1000],
                        "debug_info": debug_info,
                        "target": target_path,
                        "model": model_name,
                    }, ensure_ascii=False)
                    return make_response(combined, status=status)

                if wants_stream:
                    h = JsHeaders.new()
                    h.set("content-type", "text/event-stream")
                    h.set("cache-control", "no-cache")
                    h.set("x-accel-buffering", "no")
                    return JsResponse.new(
                        resp.body,
                        to_js({"status": status, "headers": h}, dict_converter=Object.fromEntries)
                    )
                else:
                    content = await resp.text()
                    return make_response(content, status=status)

            except Exception as e:
                if attempt >= max_attempts - 1:
                    return make_response(json.dumps({"error": str(e), "debug_info": debug_info}), status=500)
                await asyncio.sleep(1)

        return make_response('{"error":"Unexpected"}', status=500)
