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

_SYSTEM_JSON_STR = r""""You are Claude Code, Anthropic's official CLI for Claude. You are an interactive CLI tool that helps users with software engineering tasks. Use the instructions below and the tools available to you to assist the user.""""

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
                return make_response(json.dumps({"status": "ok", "version": "v34", "tools_loaded": _tools_count}))

            if path == "/config":
                return make_response(json.dumps({"target": CONFIG["TARGET_BASE_URL"], "tools_count": _tools_count}))

            if path == "/debug":
                return await self.debug_proxy(request)

            if path == "/test-body":
                return await self.test_body_build(request)

            if path == "/test-fetch":
                return await self.test_fetch(request)

            if path == "/test-notool":
                return await self.test_notool(request)

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

    async def test_body_build(self, request):
        """测试 body 构建（不发 fetch），隔离 CPU 超时问题"""
        try:
            body_text = await request.text()
            body_str, model, stream = build_body_string(body_text)
            return make_response(json.dumps({
                "body_length": len(body_str),
                "model": model,
                "stream": stream,
                "body_preview": body_str[:200] + "..." if len(body_str) > 200 else body_str,
                "body_end": body_str[-100:] if len(body_str) > 100 else "",
            }))
        except Exception as e:
            return make_response(json.dumps({"error": str(e), "trace": traceback.format_exc()}), status=500)

    async def test_fetch(self, request):
        """分步测试完整 proxy 流程，返回每步的结果"""
        steps = {}
        try:
            # Step 1: 解析 body
            steps["step1_body"] = "start"
            body_text = await request.text()
            body_str, model, stream = build_body_string(body_text)
            steps["step1_body"] = f"ok, len={len(body_str)}, model={model}"

            # Step 2: 构建 headers
            steps["step2_headers"] = "start"
            api_key = extract_api_key(request)
            headers = get_claude_headers(is_stream=stream, model=model)
            if api_key:
                headers["x-api-key"] = api_key
                headers["Authorization"] = f"Bearer {api_key}"
            steps["step2_headers"] = f"ok, has_key={api_key is not None}"

            # Step 3: 构建 JS headers
            steps["step3_js_headers"] = "start"
            js_h = JsHeaders.new()
            for k, v in headers.items():
                js_h.set(k, str(v))
            steps["step3_js_headers"] = "ok"

            # Step 4: 构建 fetch options
            steps["step4_fetch_opts"] = "start"
            fetch_init = {"method": "POST", "headers": js_h, "body": body_str}
            opts = to_js(fetch_init, dict_converter=Object.fromEntries)
            steps["step4_fetch_opts"] = "ok"

            # Step 5: 发送 fetch
            steps["step5_fetch"] = "start"
            target = "https://anyrouter.top/v1/messages?beta=true"
            resp = await js_fetch(target, opts)
            steps["step5_fetch"] = f"ok, status={resp.status}"

            # Step 6: 读取响应
            steps["step6_response"] = "start"
            resp_text = await resp.text()
            steps["step6_response"] = f"ok, len={len(resp_text)}"
            steps["upstream_body_preview"] = resp_text[:300]

        except Exception as e:
            steps["error"] = str(e)
            steps["trace"] = traceback.format_exc()

        return make_response(json.dumps(steps, indent=2, ensure_ascii=False))

    async def test_notool(self, request):
        """A/B 测试关键字段"""
        results = {}
        api_key = extract_api_key(request)
        headers = get_claude_headers(is_stream=True, model="claude-opus-4-6")
        if api_key:
            headers["x-api-key"] = api_key
            headers["Authorization"] = f"Bearer {api_key}"

        # 测试1：tools + stream:true + 字符串 system（避免 520）
        js_h1 = JsHeaders.new()
        for k, v in headers.items():
            js_h1.set(k, str(v))
        body1 = '{"model":"claude-opus-4-6","max_tokens":50,"stream":true,"messages":[{"role":"user","content":"hi"}],"thinking":{"budget_tokens":10000,"type":"enabled"},"metadata":{"user_id":"proxy_user"},"tools":' + _TOOLS_JSON_STR + ',"system":"You are Claude Code, Anthropic\'s official CLI for Claude. You are an interactive CLI tool that helps users with software engineering tasks."}'
        try:
            r1 = await js_fetch("https://anyrouter.top/v1/messages?beta=true", to_js({"method": "POST", "headers": js_h1, "body": body1}, dict_converter=Object.fromEntries))
            results["stream_str_sys"] = {"status": r1.status, "body": (await r1.text())[:300]}
        except Exception as e:
            results["stream_str_sys"] = {"error": str(e)}

        # 测试2：tools + stream:true + tool_choice:auto + 字符串 system
        js_h2 = JsHeaders.new()
        for k, v in headers.items():
            js_h2.set(k, str(v))
        body2 = '{"model":"claude-opus-4-6","max_tokens":50,"stream":true,"tool_choice":{"type":"auto"},"messages":[{"role":"user","content":"hi"}],"thinking":{"budget_tokens":10000,"type":"enabled"},"metadata":{"user_id":"proxy_user"},"tools":' + _TOOLS_JSON_STR + ',"system":"You are Claude Code, Anthropic\'s official CLI for Claude."}'
        try:
            r2 = await js_fetch("https://anyrouter.top/v1/messages?beta=true", to_js({"method": "POST", "headers": js_h2, "body": body2}, dict_converter=Object.fromEntries))
            results["stream_toolchoice"] = {"status": r2.status, "body": (await r2.text())[:300]}
        except Exception as e:
            results["stream_toolchoice"] = {"error": str(e)}

        # 测试3：最小有效请求 - tools + stream:true 不带 system/thinking/metadata
        js_h3 = JsHeaders.new()
        for k, v in headers.items():
            js_h3.set(k, str(v))
        body3 = '{"model":"claude-opus-4-6","max_tokens":50,"stream":true,"messages":[{"role":"user","content":"hi"}],"tools":' + _TOOLS_JSON_STR + '}'
        try:
            r3 = await js_fetch("https://anyrouter.top/v1/messages?beta=true", to_js({"method": "POST", "headers": js_h3, "body": body3}, dict_converter=Object.fromEntries))
            results["minimal_stream"] = {"status": r3.status, "body": (await r3.text())[:300]}
        except Exception as e:
            results["minimal_stream"] = {"error": str(e)}

        # 测试4：不带 tools，只有 stream:true
        js_h4 = JsHeaders.new()
        for k, v in headers.items():
            js_h4.set(k, str(v))
        body4 = '{"model":"claude-opus-4-6","max_tokens":50,"stream":true,"messages":[{"role":"user","content":"hi"}]}'
        try:
            r4 = await js_fetch("https://anyrouter.top/v1/messages?beta=true", to_js({"method": "POST", "headers": js_h4, "body": body4}, dict_converter=Object.fromEntries))
            results["stream_only"] = {"status": r4.status, "body": (await r4.text())[:300]}
        except Exception as e:
            results["stream_only"] = {"error": str(e)}

        return make_response(json.dumps(results, indent=2, ensure_ascii=False))

    async def handle_proxy(self, request, path, method):
        sub_path = path[4:]
        target_path = f"{CONFIG['TARGET_BASE_URL']}/{sub_path}"
        if sub_path == "messages":
            target_path += "?beta=true"

        # 提取 API key
        api_key = extract_api_key(request)

        # 解析并构建 body（CPU 敏感 - 使用字符串拼接优化）
        body_str = ""
        model_name = ""
        wants_stream = False
        try:
            body_text = await request.text()
            body_str, model_name, wants_stream = build_body_string(body_text)
        except Exception as e:
            return make_response(json.dumps({"error": f"body_parse: {e}"}), status=400)

        # 构建转发头
        headers = get_claude_headers(is_stream=wants_stream, model=model_name)
        if api_key:
            headers["x-api-key"] = api_key
            headers["Authorization"] = f"Bearer {api_key}"

        try:
            js_h = JsHeaders.new()
            for k, v in headers.items():
                js_h.set(k, str(v))
            fi = {"method": method, "headers": js_h}
            if body_str and method in ("POST", "PUT", "PATCH"):
                fi["body"] = body_str
            opts = to_js(fi, dict_converter=Object.fromEntries)

            resp = await js_fetch(target_path, opts)
            status = resp.status

            # 流式响应直接透传
            if wants_stream and status == 200:
                h = JsHeaders.new()
                h.set("content-type", "text/event-stream")
                h.set("cache-control", "no-cache")
                h.set("x-accel-buffering", "no")
                return JsResponse.new(
                    resp.body,
                    to_js({"status": status, "headers": h}, dict_converter=Object.fromEntries)
                )

            # 非流式：读取并转发所有响应（包括错误）
            content = await resp.text()
            return make_response(content, status=status)

        except Exception as e:
            return make_response(json.dumps({"error": str(e), "trace": traceback.format_exc()}), status=500)
