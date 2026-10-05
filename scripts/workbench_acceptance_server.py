"""Isolated synthetic workbench, with bounded real MiMo or deterministic HTTP SSE.

Never points at an existing project. No credentials are persisted or printed.
"""
from __future__ import annotations

import argparse
import json
import os
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from popper.chat import make_chat_client
from popper.core import ProtocolError
from popper.demo import initialize_demo
from popper.server import Workstation
from popper.sessions import AgentSessionStore


class MockProvider(ThreadingHTTPServer):
    def __init__(self):
        super().__init__(("127.0.0.1", 0), MockHandler)
        self.requests = []
        self.release = threading.Event()
        self.started = threading.Event()


class MockHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.server.requests.append(request)
        last = next((item.get("content", "") for item in reversed(request["messages"])
                     if item["role"] == "user"), "")
        if "[fail]" in last:
            self.send_error(503, "synthetic provider failure")
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()

        def emit(delta=None, **extra):
            frame = {"choices": [{"delta": delta or {}}], **extra}
            self.wfile.write(("data: " + json.dumps(frame) + "\n\n").encode())
            self.wfile.flush()

        try:
            emit({"content": "合成回复："})
            self.server.started.set()
            if "[slow]" in last:
                self.server.release.wait(5)
            if "[drop]" in last:
                return
            tool_results = [item for item in request["messages"] if item["role"] == "tool"]
            if "[tool]" in last and not tool_results:
                emit({"tool_calls": [{"index": 0, "type": "function", "function": {
                    "name": "workspace_file_read", "arguments": '{"path":'}}]})
                emit({"tool_calls": [{"index": 0, "function": {"arguments": '"notes.txt"}'}}]})
            else:
                emit({"content": "已核对真实工具结果。" if tool_results else "只用于验收，不构成科研结论。"})
            emit(usage={"prompt_tokens": 80, "completion_tokens": 20, "total_tokens": 100})
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass


class LimitedClient:
    """Fail closed after missing usage or a failed paid request; never retry."""
    supports_cancellation = True

    def __init__(self, client, *, calls=0, tokens=0, token_limit=20_000):
        self.client, self.descriptor = client, client.descriptor
        if hasattr(client, "context_tools"):
            self.context_tools = client.context_tools
        self.calls, self.tokens, self.blocked = calls, tokens, False
        self.token_limit = token_limit
        self.lock = threading.Lock()

    def __call__(self, system, messages, **kwargs):
        with self.lock:
            if self.blocked or self.calls >= 3 or self.tokens >= self.token_limit:
                raise ProtocolError("真实模型验收请求上限或用量核验限制已触发")
            self.calls += 1
        try:
            result = self.client(system, messages, **kwargs)
            total = getattr(result, "usage", {}).get("total_tokens")
            with self.lock:
                if not isinstance(total, int) or total < 0:
                    self.blocked = True
                else:
                    self.tokens += total
            print(json.dumps({"real_requests": self.calls, "usage_tokens": total,
                              "blocked": self.blocked}), flush=True)
            return result
        except Exception:
            self.blocked = True
            print(json.dumps({"real_requests": self.calls, "blocked": True}), flush=True)
            raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=["mock", "real"], default="mock")
    parser.add_argument("--port", type=int, default=8768)
    parser.add_argument("--resume-mock-project", type=Path,
                        help="Resume an existing synthetic fixture; mock mode only")
    parser.add_argument("--resume-real-project", type=Path,
                        help="Resume a synthetic real fixture without resetting verified usage")
    parser.add_argument("--real-token-budget", type=int, default=20_000)
    args = parser.parse_args()
    resumed = args.resume_mock_project or args.resume_real_project
    if args.resume_mock_project and args.resume_real_project:
        parser.error("Choose only one resume mode")
    if args.real_token_budget < 1:
        parser.error("Token budget must be positive")
    if resumed:
        root = resumed.resolve()
        if ((args.resume_mock_project and args.mode != "mock")
                or (args.resume_real_project and args.mode != "real")
                or root.parent != Path(tempfile.gettempdir()).resolve()
                or not root.name.startswith("popper-acceptance-") or not root.is_dir()):
            parser.error("Only an existing temporary popper-acceptance fixture in its matching mode can be resumed")
    else:
        root = Path(tempfile.mkdtemp(prefix="popper-acceptance-"))
        initialize_demo(root, "ode-convergence")
        (root / "notes.txt").write_text("Synthetic acceptance only. Marker: POPPER_SYNTHETIC_42.\n", encoding="utf-8")
    provider = None
    if args.mode == "mock":
        provider = MockProvider()
        threading.Thread(target=provider.serve_forever, daemon=True).start()
        os.environ["POPPER_API_KEY"] = "local-synthetic-placeholder"
        endpoint = f"http://127.0.0.1:{provider.server_port}/v1"
        client = make_chat_client(endpoint, "acceptance-mock", enable_file_tools=True)
        model = "acceptance-mock"
    else:
        model = os.environ["POPPER_CHAT_MODEL"]
        endpoint = os.environ["POPPER_CHAT_BASE_URL"]
        calls = tokens = 0
        if args.resume_real_project:
            store = AgentSessionStore(root)
            try:
                jobs = [job for session in store.list_sessions()
                        for job in store.agent_jobs(session["session_id"])]
                if not jobs or any(job["status"] != "succeeded" or job["total_tokens"] <= 0
                                   for job in jobs):
                    parser.error("Cannot resume paid calls with active, failed or unverified usage")
                calls = len(jobs)
                tokens = sum(job["total_tokens"] for job in jobs)
            finally:
                store.close()
        client = LimitedClient(make_chat_client(endpoint, model, enable_file_tools=True),
                               calls=calls, tokens=tokens, token_limit=args.real_token_budget)
        print(json.dumps({"previous_requests": calls, "previous_tokens": tokens,
                          "token_limit": args.real_token_budget}), flush=True)
    server = Workstation(("127.0.0.1", args.port), root, trusted_local=True,
                         chat_llm=client, chat_model=model, chat_base_url=endpoint)
    server.environment_label = "临时验收环境 · 合成项目 · " + ("本地模拟模型" if args.mode == "mock" else "受限真实模型")
    print(json.dumps({"project": str(root), "port": args.port, "mode": args.mode}), flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()
        if provider:
            provider.shutdown()


if __name__ == "__main__":
    main()
