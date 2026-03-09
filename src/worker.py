import asyncio
import json
import sys
import traceback

from js import Response as JsResponse, Headers as JsHeaders, Object, fetch as js_fetch, URL as JsURL
from pyodide.ffi import to_js
from workers import WorkerEntrypoint

CONFIG = {
    "TARGET_BASE_URL": "https://anyrouter.top/v1",
    "DEBUG_MODE": False,
    "CLAUDE_CODE_TOOLS": json.loads(r"""[{"name": "Task", "description": "Launch a new agent to handle complex, multi-step tasks autonomously. \n\nThe Task tool launches specialized agents (subprocesses) that autonomously handle complex tasks. Each agent type has specific capabilities and tools available to it.\n\nAvailable agent types and the tools they have access to:\n- general-purpose: General-purpose agent for researching complex questions, searching for code, and executing multi-step tasks. When you are searching for a keyword or file and are not confident that you will find the right match in the first few tries use this agent to perform the search for you. (Tools: *)\n- statusline-setup: Use this agent to configure the user's Claude Code status line setting. (Tools: Read, Edit)\n- Explore: Fast agent specialized for exploring codebases. Use this when you need to quickly find files by patterns (eg. \"src/components/**/*.tsx\"), search code for keywords (eg. \"API endpoints\"), or answer questions about the codebase (eg. \"how do API endpoints work?\"). When calling this agent, specify the desired thoroughness level: \"quick\" for basic searches, \"medium\" for moderate exploration, or \"very thorough\" for comprehensive analysis across multiple locations and naming conventions. (Tools: All tools)\n- Plan: Software architect agent for designing implementation plans. Use this when you need to plan the implementation strategy for a task. Returns step-by-step plans, identifies critical files, and considers architectural trade-offs. (Tools: All tools)\n- claude-code-guide: Use this agent when the user asks questions (\"Can Claude...\", \"Does Claude...\", \"How do I...\") about: (1) Claude Code (the CLI tool) - features, hooks, slash commands, MCP servers, settings, IDE integrations, keyboard shortcuts; (2) Claude Agent SDK - building custom agents; (3) Claude API (formerly Anthropic API) - API usage, tool use, Anthropic SDK usage. **IMPORTANT:** Before spawning a new agent, check if there is already a running or recently completed claude-code-guide agent that you can resume using the \"resume\" parameter. (Tools: Glob, Grep, Read, WebFetch, WebSearch)\n\nWhen using the Task tool, you must specify a subagent_type parameter to select which agent type to use.\n\nWhen NOT to use the Task tool:\n- If you want to read a specific file path, use the Read or Glob tool instead of the Task tool, to find the match more quickly\n- If you are searching for a specific class definition like \"class Foo\", use the Glob tool instead, to find the match more quickly\n- If you are searching for code within a specific file or set of 2-3 files, use the Read tool instead of the Task tool, to find the match more quickly\n- Other tasks that are not related to the agent descriptions above\n\n\nUsage notes:\n- Always include a short description (3-5 words) summarizing what the agent will do\n- Launch multiple agents concurrently whenever possible, to maximize performance; to do that, use a single message with multiple tool uses\n- When the agent is done, it will return a single message back to you. The result returned by the agent is not visible to the user. To show the user the result, you should send a text message back to the user with a concise summary of the result.\n- You can optionally run agents in the background using the run_in_background parameter. When an agent runs in the background, you will need to use TaskOutput to retrieve its results once it's done. You can continue to work while background agents run - When you need their results to continue you can use TaskOutput in blocking mode to pause and wait for their results.\n- Agents can be resumed using the `resume` parameter by passing the agent ID from a previous invocation. When resumed, the agent continues with its full previous context preserved. When NOT resuming, each invocation starts fresh and you should provide a detailed task description with all necessary context.\n- When the agent is done, it will return a single message back to you along with its agent ID. You can use this ID to resume the agent later if needed for follow-up work.\n- Provide clear, detailed prompts so the agent can work autonomously and return exactly the information you need.\n- Agents with \"access to current context\" can see the full conversation history before the tool call. When using these agents, you can write concise prompts that reference earlier context (e.g., \"investigate the error discussed above\") instead of repeating information. The agent will receive all prior messages and understand the context.\n- The agent's outputs should generally be trusted\n- Clearly tell the agent whether you expect it to write code or just to do research (search, file reads, web fetches, etc.), since it is not aware of the user's intent\n- If the agent description mentions that it should be used proactively, then you should try your best to use it without the user having to ask for it first. Use your judgement.\n- If the user specifies that they want you to run agents \"in parallel\", you MUST send a single message with multiple Task tool use content blocks. For example, if you need to launch both a code-reviewer agent and a test-runner agent in parallel, send a single message with both tool calls.\n\nExample usage:\n\n<example_agent_descriptions>\n\"code-reviewer\": use this agent after you are done writing a signficant piece of code\n\"greeting-responder\": use this agent when to respond to user greetings with a friendly joke\n</example_agent_description>\n\n<example>\nuser: \"Please write a function that checks if a number is prime\"\nassistant: Sure let me write a function that checks if a number is prime\nassistant: First let me use the Write tool to write a function that checks if a number is prime\nassistant: I'm going to use the Write tool to write the following code:\n<code>\nfunction isPrime(n) {\n  if (n <= 1) return false\n  for (let i = 2; i * i <= n; i++) {\n    if (n % i === 0) return false\n  }\n  return true\n}\n</code>\n<commentary>\nSince a signficant piece of code was written and the task was completed, now use the code-reviewer agent to review the code\n</commentary>\nassistant: Now let me use the code-reviewer agent to review the code\nassistant: Uses the Task tool to launch the code-reviewer agent \n</example>\n\n<example>\nuser: \"Hello\"\n<commentary>\nSince the user is greeting, use the greeting-responder agent to respond with a friendly joke\n</commentary>\nassistant: \"I'm going to use the Task tool to launch the greeting-responder agent\"\n</example>\n", "input_schema": {"type": "object", "properties": {"description": {"type": "string", "description": "A short (3-5 word) description of the task"}, "prompt": {"type": "string", "description": "The task for the agent to perform"}, "subagent_type": {"type": "string", "description": "The type of specialized agent to use for this task"}, "model": {"type": "string", "enum": ["sonnet", "opus", "haiku"], "description": "Optional model to use for this agent. If not specified, inherits from parent. Prefer haiku for quick, straightforward tasks to minimize cost and latency."}, "resume": {"type": "string", "description": "Optional agent ID to resume from. If provided, the agent will continue from the previous execution transcript."}, "run_in_background": {"type": "boolean", "description": "Set to true to run this agent in the background. Use TaskOutput to read the output later."}}, "required": ["description", "prompt", "subagent_type"], "additionalProperties": false, "$schema": "http://json-schema.org/draft-07/schema#"}}, {"name": "TaskOutput", "description": "Retrieves output from a running or completed task.", "input_schema": {"type": "object", "properties": {"task_id": {"type": "string"}, "block": {"type": "boolean", "default": true}, "timeout": {"type": "number", "default": 30000}}, "required": ["task_id"]}}, {"name": "Bash", "description": "Executes a bash command.", "input_schema": {"type": "object", "properties": {"command": {"type": "string"}, "timeout": {"type": "number"}, "description": {"type": "string"}}, "required": ["command"]}}, {"name": "Glob", "description": "Fast file pattern matching.", "input_schema": {"type": "object", "properties": {"pattern": {"type": "string"}, "path": {"type": "string"}}, "required": ["pattern"]}}, {"name": "Grep", "description": "Search tool built on ripgrep.", "input_schema": {"type": "object", "properties": {"pattern": {"type": "string"}, "path": {"type": "string"}, "glob": {"type": "string"}, "output_mode": {"type": "string"}}, "required": ["pattern"]}}, {"name": "Read", "description": "Reads a file from the filesystem.", "input_schema": {"type": "object", "properties": {"file_path": {"type": "string"}, "offset": {"type": "number"}, "limit": {"type": "number"}}, "required": ["file_path"]}}, {"name": "Edit", "description": "Performs exact string replacements in files.", "input_schema": {"type": "object", "properties": {"file_path": {"type": "string"}, "old_string": {"type": "string"}, "new_string": {"type": "string"}, "replace_all": {"type": "boolean", "default": false}}, "required": ["file_path", "old_string", "new_string"]}}, {"name": "Write", "description": "Writes a file to the filesystem.", "input_schema": {"type": "object", "properties": {"file_path": {"type": "string"}, "content": {"type": "string"}}, "required": ["file_path", "content"]}}, {"name": "WebFetch", "description": "Fetches content from a URL.", "input_schema": {"type": "object", "properties": {"url": {"type": "string", "format": "uri"}, "prompt": {"type": "string"}}, "required": ["url", "prompt"]}}, {"name": "WebSearch", "description": "Search the web.", "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}}, {"name": "AskUserQuestion", "description": "Ask the user a question.", "input_schema": {"type": "object", "properties": {"questions": {"type": "array", "items": {"type": "object"}}}, "required": ["questions"]}}]"""),
    "CLAUDE_CODE_SYSTEM": json.loads(r"""[{"type": "text", "text": "You are Claude Code, Anthropic's official CLI for Claude.", "cache_control": {"type": "ephemeral"}}, {"type": "text", "text": "You are an interactive CLI tool that helps users with software engineering tasks. Use the instructions below and the tools available to you to assist the user.", "cache_control": {"type": "ephemeral"}}]""")
}


def make_response(body, status=200, content_type="application/json", extra_headers=None):
    h = JsHeaders.new()
    h.set("content-type", content_type)
    if extra_headers:
        for k, v in extra_headers.items():
            h.set(k, v)
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


class Default(WorkerEntrypoint):
    async def fetch(self, request):
        try:
            parsed = JsURL.new(request.url)
            path = str(parsed.pathname)
            method = str(request.method)

            if path == "/" and method == "GET":
                return make_response(json.dumps({"status": "ok", "version": "v23", "tools_loaded": len(CONFIG['CLAUDE_CODE_TOOLS'])}))

            if path == "/config" and method == "GET":
                return make_response(json.dumps(CONFIG, indent=4, ensure_ascii=False))

            if path.startswith("/v1/"):
                return await self.handle_proxy(request, path, method)

            return make_response('{"error":"Not Found"}', status=404)
        except Exception as e:
            return make_response(json.dumps({"error": {"message": str(e)}}), status=500)

    async def handle_proxy(self, request, path, method):
        sub_path = path[4:]
        target_path = f"{CONFIG['TARGET_BASE_URL']}/{sub_path}"
        if sub_path == "messages":
            target_path += "?beta=true"

        body_json = {}
        wants_stream = False
        try:
            body_text = await request.text()
            if body_text:
                raw = json.loads(body_text)
                safe_keys = {'model', 'messages', 'max_tokens', 'metadata', 'stop_sequences',
                             'stream', 'system', 'temperature', 'top_k', 'top_p', 'tools', 'thinking'}
                body_json = {k: v for k, v in raw.items() if k in safe_keys}

                model = body_json.get('model', '')
                if 'anyrouter/' in model:
                    body_json['model'] = model.replace('anyrouter/', '')

                if CONFIG['DEBUG_MODE']:
                    print(f"[PROXY] Original keys: {list(raw.keys())}")
                    print(f"[PROXY] Has tools: {'tools' in raw}, count: {len(raw.get('tools', []))}")

                if ('sonnet' in model.lower() or 'opus' in model.lower() or 'haiku' in model.lower()) and CONFIG['CLAUDE_CODE_TOOLS']:
                    body_json['tools'] = CONFIG['CLAUDE_CODE_TOOLS']
                    if CONFIG['CLAUDE_CODE_SYSTEM']:
                        body_json['system'] = CONFIG['CLAUDE_CODE_SYSTEM']
                    if 'sonnet' in model.lower() or 'opus' in model.lower():
                        if 'thinking' not in body_json:
                            body_json['thinking'] = {"budget_tokens": 10000, "type": "enabled"}
                    body_json['metadata'] = {"user_id": "proxy_user"}

                wants_stream = body_json.get('stream', False)
        except Exception as e:
            if CONFIG['DEBUG_MODE']:
                print(f"[PROXY] Body parse error: {e}")

        model_name = body_json.get('model', '')
        headers = get_claude_headers(is_stream=wants_stream, model=model_name)

        req_auth = request.headers.get("Authorization")
        if req_auth:
            headers["Authorization"] = str(req_auth)
        else:
            api_key = request.headers.get("x-api-key")
            if api_key:
                headers["Authorization"] = f"Bearer {api_key}"

        if CONFIG['DEBUG_MODE']:
            print(f"\n{'=' * 60}")
            print(f"[PROXY] Target: {target_path}")
            print(f"[PROXY] Model: {model_name}")
            print(f"[PROXY] Stream: {wants_stream}")

        js_headers = JsHeaders.new()
        for k, v in headers.items():
            js_headers.set(k, str(v))

        fetch_init = {"method": method, "headers": js_headers}
        if body_json and method in ("POST", "PUT", "PATCH"):
            fetch_init["body"] = json.dumps(body_json)
        fetch_options = to_js(fetch_init, dict_converter=Object.fromEntries)

        max_attempts = 5
        for attempt in range(max_attempts):
            try:
                if CONFIG['DEBUG_MODE']:
                    print(f"[PROXY] Attempt {attempt + 1}/{max_attempts}...")

                resp = await js_fetch(target_path, fetch_options)
                status = resp.status

                if CONFIG['DEBUG_MODE']:
                    print(f"[PROXY] Status: {status}")

                if status in (520, 502):
                    if attempt < max_attempts - 1:
                        await asyncio.sleep(1)
                        continue
                    return make_response('{"error":{"message":"Network error after max retries"}}', status=502)

                if status in (403, 500):
                    error_text = await resp.text()
                    if CONFIG['DEBUG_MODE']:
                        print(f"[PROXY] Error: {error_text[:500]}")
                    return make_response(error_text, status=status)

                if wants_stream:
                    stream_headers = {
                        "cache-control": "no-cache",
                        "x-accel-buffering": "no"
                    }
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
                if CONFIG['DEBUG_MODE']:
                    print(f"[PROXY] Error: {type(e).__name__}: {e}")
                    traceback.print_exc()
                if attempt >= max_attempts - 1:
                    return make_response(json.dumps({"error": {"message": str(e)}}), status=500)
                await asyncio.sleep(1)

        return make_response('{"error":"Unexpected"}', status=500)
