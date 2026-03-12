"""
Web 管理界面 — 通过浏览器控制 api_register.py
启动方式: python web_server.py
访问: http://localhost:8899
"""

import json
import os
import queue
import sys
import threading
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn
from urllib.parse import urlparse

# 注册脚本所在目录
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

from api_register import (
    MailAccount,
    register_account,
    get_finished_emails,
    RESULTS_DIR,
    log as reg_log,
    MAX_RETRY_PER_ACCOUNT,
)

WEB_PORT = 8899
HTML_FILE = os.path.join(SCRIPT_DIR, "web_ui.html")

# ═══════════════════════════════════════════════════════
# 全局状态
# ═══════════════════════════════════════════════════════
_state = {
    "running": False,
    "stop_flag": False,
    "total": 0,
    "success": 0,
    "fail": 0,
    "start_time": 0,
}
_state_lock = threading.Lock()
_log_queues: list[queue.Queue] = []  # SSE 客户端队列列表
_log_lock = threading.Lock()


def broadcast(event: dict):
    """广播事件到所有 SSE 客户端"""
    data = json.dumps(event, ensure_ascii=False)
    with _log_lock:
        dead = []
        for q in _log_queues:
            try:
                q.put_nowait(data)
            except queue.Full:
                dead.append(q)
        for q in dead:
            _log_queues.remove(q)


def broadcast_log(text: str, level: str = ""):
    broadcast({"type": "log", "text": text, "level": level})


# ═══════════════════════════════════════════════════════
# 注册线程（复用 api_register 的核心逻辑）
# ═══════════════════════════════════════════════════════
def _register_worker(
    accounts: list[MailAccount],
    proxy: str,
    workers: int,
    mode: str = "register",
    domain_mail: dict = None,
    freemail: dict = None,
):
    """在后台执行批量注册"""
    import random
    from concurrent.futures import ThreadPoolExecutor, as_completed

    total = len(accounts)
    with _state_lock:
        _state["running"] = True
        _state["stop_flag"] = False
        _state["total"] = total
        _state["success"] = 0
        _state["fail"] = 0
        _state["start_time"] = time.time()

    stats_lock = threading.Lock()
    recent_results = []  # 滑动窗口记录最近的成功/失败
    abort_flag = False   # 真正需要中止时才设置

    def _should_abort():
        """检查最近一段时间内是否失败率过高（滑动窗口）"""
        window_size = max(workers * 3, 10)  # 滑动窗口大小
        recent = recent_results[-window_size:]  # 最近 N 个结果
        if len(recent) < 5:  # 至少 5 个结果才开始判断
            return False
        fail_count = sum(1 for r in recent if not r)
        fail_rate = fail_count / len(recent)
        return fail_rate >= 0.9  # 90% 以上失败才中止

    def do_one(acc: MailAccount, idx: int):
        nonlocal abort_flag
        with _state_lock:
            if _state["stop_flag"]:
                return

        if abort_flag:
            broadcast_log(f"🛑 [{acc.email}] 失败率过高，跳过", "warning")
            with _state_lock:
                _state["fail"] += 1
            broadcast({
                "type": "result", "email": acc.email, "success": False,
                "elapsed": "—", "error": "失败率过高，自动跳过",
            })
            return

        broadcast_log(f"\n{'─'*45}", "dim")
        broadcast_log(f"[{idx}/{total}] {acc.email}", "info")

        used_codes = set()
        success = False

        def _check_stop():
            with _state_lock:
                return _state["stop_flag"]

        for attempt in range(1, MAX_RETRY_PER_ACCOUNT + 1):
            if _check_stop():
                broadcast_log("⏹ 收到停止信号", "warning")
                return

            if attempt > 1:
                broadcast_log(f"  重试 #{attempt}...", "warning")
                # 可中断的重试等待
                for _ in range(4):
                    if _check_stop():
                        broadcast_log("⏹ 收到停止信号", "warning")
                        return
                    time.sleep(0.5)

            try:
                result = register_account(
                    acc,
                    proxy,
                    used_codes,
                    mode=mode,
                    cancel_fn=_check_stop,
                    domain_mail=domain_mail,
                    freemail_worker_domain=(freemail or {}).get("worker_domain", ""),
                    freemail_token=(freemail or {}).get("token", ""),
                )

                # 注册完成后再检查一次停止
                if _check_stop():
                    return

                elapsed = round(time.time() - _state["start_time"], 1)
                result["elapsed_seconds"] = elapsed

                # 保存结果
                os.makedirs(RESULTS_DIR, exist_ok=True)
                fpath = os.path.join(RESULTS_DIR, f"{acc.email}.json")
                with open(fpath, "w", encoding="utf-8") as f:
                    json.dump(result, f, indent=2, ensure_ascii=False)

                with stats_lock:
                    recent_results.append(True)
                with _state_lock:
                    _state["success"] += 1

                broadcast_log(f"  🎉 注册成功: {acc.email}", "success")
                broadcast({
                    "type": "result", "email": acc.email, "success": True,
                    "elapsed": f"{elapsed}s", "account_id": result.get("account_id", ""),
                })
                success = True
                break

            except InterruptedError:
                broadcast_log(f"  ⏹ 已取消: {acc.email}", "warning")
                return
            except Exception as e:
                if _check_stop():
                    return
                err = f"{type(e).__name__}: {str(e)[:120]}"
                broadcast_log(f"  ❌ 尝试 {attempt} 失败: {err}", "error")

        if not success:
            with stats_lock:
                recent_results.append(False)
                if _should_abort():
                    abort_flag = True
                    broadcast_log("🛑 近期失败率超过 90%，停止后续任务", "error")
            with _state_lock:
                _state["fail"] += 1
            broadcast({
                "type": "result", "email": acc.email, "success": False,
                "elapsed": "—", "error": str(err)[:100] if 'err' in dir() else "未知错误",
            })

    start = time.time()

    if workers <= 1:
        for i, acc in enumerate(accounts, 1):
            with _state_lock:
                if _state["stop_flag"]:
                    break
            do_one(acc, i)
    else:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futs = {}
            for i, acc in enumerate(accounts, 1):
                delay = ((i - 1) % workers) * random.uniform(1.0, 2.5)
                fut = pool.submit(lambda a=acc, j=i, d=delay: (time.sleep(d) if d > 0 else None, do_one(a, j)))
                futs[fut] = acc.email

            for fut in as_completed(futs):
                try:
                    fut.result()
                except Exception as e:
                    broadcast_log(f"线程异常: {e}", "error")
                # 每个 future 完成后检查停止，提前退出循环
                with _state_lock:
                    if _state["stop_flag"]:
                        # 取消所有未开始的任务
                        for f in futs:
                            f.cancel()
                        break

    elapsed = time.time() - start
    with _state_lock:
        _state["running"] = False

    elapsed_str = f"{elapsed:.1f}s" if elapsed < 60 else f"{elapsed/60:.1f}m"
    broadcast({
        "type": "done",
        "success": _state["success"],
        "fail": _state["fail"],
        "elapsed": elapsed_str,
    })


# ═══════════════════════════════════════════════════════
# HTTP 处理器
# ═══════════════════════════════════════════════════════
class WebHandler(BaseHTTPRequestHandler):

    def do_GET(self):
        path = urlparse(self.path).path

        if path == "/" or path == "/index.html":
            self._serve_html()
        elif path == "/api/logs":
            self._serve_sse()
        elif path == "/api/status":
            self._json_response({
                "running": _state["running"],
                "success": _state["success"],
                "fail": _state["fail"],
                "total": _state["total"],
                "elapsed": time.time() - _state["start_time"] if _state["running"] else 0,
            })
        else:
            self.send_error(404)

    def do_POST(self):
        path = urlparse(self.path).path

        if path == "/api/start":
            self._handle_start()
        elif path == "/api/stop":
            self._handle_stop()
        else:
            self.send_error(404)

    def _serve_html(self):
        try:
            with open(HTML_FILE, "r", encoding="utf-8") as f:
                content = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(content.encode("utf-8"))
        except FileNotFoundError:
            self.send_error(404, "web_ui.html not found")

    def _serve_sse(self):
        """Server-Sent Events 日志流"""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()

        q = queue.Queue(maxsize=200)
        with _log_lock:
            _log_queues.append(q)

        try:
            while True:
                try:
                    data = q.get(timeout=30)
                    self.wfile.write(f"data: {data}\n\n".encode())
                    self.wfile.flush()
                except queue.Empty:
                    # 心跳保活
                    self.wfile.write(b": heartbeat\n\n")
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionError, OSError):
            pass
        finally:
            with _log_lock:
                if q in _log_queues:
                    _log_queues.remove(q)

    def _handle_start(self):
        if _state["running"]:
            self._json_response({"error": "已有任务运行中"})
            return

        body = self._read_body()
        data = json.loads(body)
        accounts_text = data.get("accounts", "")
        proxy = data.get("proxy", "")
        workers = data.get("workers", 1)
        login_mode = data.get("login_mode", False)
        skip_finished = data.get("skip_finished", True)
        domain_mail = data.get("domain_mail", None)  # 域名邮箱配置
        mail_mode = data.get("mail_mode", "outlook")
        freemail = data.get("freemail", None)
        mode = "login" if login_mode else "register"

        if mail_mode == "freemail":
            worker_domain = (freemail or {}).get("worker_domain", "").strip()
            token = (freemail or {}).get("token", "").strip()
            if not worker_domain or not token:
                self._json_response({"error": "FreeMail 模式需要填写 worker 网址和 freemail token"})
                return

        # 解析账号
        accounts = []
        for line in accounts_text.strip().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            try:
                if mail_mode == "freemail":
                    email = line.split("----")[0].strip().lower()
                    if not email:
                        continue
                    accounts.append(MailAccount(email=email, password="", is_freemail=True))
                else:
                    accounts.append(MailAccount.parse(line))
            except ValueError:
                continue

        if not accounts:
            self._json_response({"error": "没有有效的账号"})
            return

        # 过滤已完成的账号（跳过已有凭证 和 注册转登录 独立控制）
        if skip_finished:
            done = get_finished_emails(RESULTS_DIR)
            pending = [a for a in accounts if a.email.lower() not in done]
        else:
            pending = accounts

        if not pending:
            self._json_response({"error": "所有账号已注册完毕"})
            return

        # 启动注册线程
        thread = threading.Thread(
            target=_register_worker,
            args=(pending, proxy, workers, mode, domain_mail, freemail),
            daemon=True,
        )
        thread.start()

        self._json_response({"ok": True, "total": len(pending)})

    def _handle_stop(self):
        with _state_lock:
            _state["stop_flag"] = True
        self._json_response({"ok": True})

    def _json_response(self, data: dict, status=200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self) -> str:
        length = int(self.headers.get("Content-Length", 0))
        return self.rfile.read(length).decode("utf-8")

    def log_message(self, format, *args):
        pass  # 静默 HTTP 日志


# ═══════════════════════════════════════════════════════
# 入口
# ═══════════════════════════════════════════════════════
def main():
    # 重定向 api_register 的日志到 SSE
    import logging

    class SSELogHandler(logging.Handler):
        def emit(self, record):
            msg = self.format(record)
            level = ""
            if record.levelno >= logging.ERROR:
                level = "error"
            elif record.levelno >= logging.WARNING:
                level = "warning"
            elif "成功" in msg or "✅" in msg or "🎉" in msg:
                level = "success"
            elif "INFO" in msg:
                level = "info"
            broadcast_log(msg, level)

    sse_handler = SSELogHandler()
    sse_handler.setFormatter(logging.Formatter("%(message)s"))
    reg_log.addHandler(sse_handler)

    class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
        daemon_threads = True

    server = ThreadedHTTPServer(("0.0.0.0", WEB_PORT), WebHandler)
    print(f"""
╔══════════════════════════════════════════════╗
║  OpenAI 协议注册 — Web 控制台                ║
║                                              ║
║  🌐 http://localhost:{WEB_PORT}                    ║
║  📁 结果目录: {RESULTS_DIR:<29s}║
║                                              ║
║  按 Ctrl+C 退出                              ║
╚══════════════════════════════════════════════╝
""")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n⏹ 已退出")
        server.shutdown()


if __name__ == "__main__":
    main()