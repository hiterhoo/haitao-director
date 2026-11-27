"""ComfyUI 客户端，只用标准库 + 可选 websocket-client。
提交任务 POST /prompt，查结果 GET /history/{id}，查队列 GET /queue，
查状态 GET /system_stats，采样进度走 WebSocket /ws 实时推。
这是 ComfyUI 官方 API 的常规用法，部署后不用改。
"""
import json
import urllib.error
import urllib.request


def _request(url, payload=None, timeout=30):
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, method="POST" if data else "GET")
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read().decode("utf-8", errors="replace")[:500]
        except Exception:
            body = ""
        return exc.code, {"error": body}
    except Exception as exc:
        return 0, {"error": str(exc)[:300]}


def check_health(comfy_url):
    """ComfyUI 是否在线，返回 (ok, info)。"""
    status, body = _request(comfy_url.rstrip("/") + "/system_stats", timeout=10)
    return status == 200, body


def submit_prompt(comfy_url, workflow, client_id=None):
    """提交一份 workflow，返回 prompt_id，出错返回 None 和错误信息。
    带上 client_id，ComfyUI 才会把该任务的进度推到对应的 WebSocket 上。"""
    payload = {"prompt": workflow}
    if client_id:
        payload["client_id"] = client_id
    status, body = _request(comfy_url.rstrip("/") + "/prompt", payload)
    if status == 200 and isinstance(body, dict) and body.get("prompt_id"):
        return body["prompt_id"], None
    # 把 ComfyUI 的报错提成一句人话，别把整个 dict 塞进任务 note
    err = ""
    if isinstance(body, dict):
        e = body.get("error")
        if isinstance(e, dict):
            err = str(e.get("message") or e.get("type") or "")
            node_errors = body.get("node_errors") or {}
            if node_errors:
                first = next(iter(node_errors.values()), {})
                errs = first.get("errors") or []
                if errs:
                    err += "：" + str(errs[0].get("message") or errs[0].get("type") or "")
        elif e:
            err = str(e)
    return None, (err[:300] or ("HTTP %s" % status))


def get_history_entry(comfy_url, prompt_id):
    """查一次任务结果，返回整个 history 条目（含 status / outputs）。
    ComfyUI 任务出错时 outputs 为空但 status.status_str == "error"，
    调用方据此可以快速判失败，不用干等超时。没查到返回 None。"""
    status, body = _request(
        comfy_url.rstrip("/") + "/history/" + prompt_id, timeout=10
    )
    if status != 200 or not isinstance(body, dict):
        return None
    return body.get(prompt_id) or None


def history_error_text(entry):
    """从 history 条目里提一句人话错误信息，没有就返回空串。"""
    try:
        status = (entry or {}).get("status") or {}
        for msg in status.get("messages") or []:
            if isinstance(msg, (list, tuple)) and len(msg) >= 2 and isinstance(msg[1], dict):
                exc = msg[1].get("exception_message") or msg[1].get("exception_type")
                if exc:
                    return str(exc)[:300]
        return str(status.get("status_str") or "")[:300]
    except Exception:
        return ""


def get_queue(comfy_url):
    """查 ComfyUI 队列，返回 (queue_running, queue_pending) 两个列表。
    每项是 [执行序号, prompt_id, workflow, extra]，失败返回 ([], [])。"""
    status, body = _request(comfy_url.rstrip("/") + "/queue", timeout=10)
    if status != 200 or not isinstance(body, dict):
        return [], []
    return body.get("queue_running") or [], body.get("queue_pending") or []


def listen_progress(comfy_url, client_id, prompt_id, on_progress, stop_event):
    """连 ComfyUI WebSocket，收采样进度推给 on_progress(value, max, node)。
    收到该 prompt_id 的 executed / execution_success 就返回 True；
    连接失败 / 超时返回 False，调用方改走轮询兜底。
    需要 websocket-client，没装就直接返回 False。"""
    try:
        import websocket
    except Exception:
        return False
    ws_url = comfy_url.rstrip("/").replace("http://", "ws://").replace(
        "https://", "wss://"
    ) + "/ws?clientId=" + client_id
    try:
        ws = websocket.create_connection(ws_url, timeout=10)
    except Exception:
        return False
    try:
        ws.settimeout(5)
        while not stop_event.is_set():
            try:
                msg = ws.recv()
            except websocket.WebSocketTimeoutException:
                continue
            except Exception:
                return False  # 连接断了就别忙等空转，调用方走轮询兜底
            try:
                data = json.loads(msg)
            except Exception:
                continue
            if not isinstance(data, dict):
                continue
            if data.get("type") == "progress":
                d = data.get("data") or {}
                if d.get("prompt_id") and d.get("prompt_id") != prompt_id:
                    continue  # 别人的任务进度别串进来
                value, maximum = d.get("value"), d.get("max")
                if isinstance(value, (int, float)) and isinstance(maximum, (int, float)) and maximum:
                    on_progress(value, maximum, d.get("node"))
            elif data.get("type") in ("executed", "execution_success"):
                d = data.get("data") or {}
                if d.get("prompt_id") == prompt_id or not d.get("prompt_id"):
                    return True
            elif data.get("type") == "execution_error":
                d = data.get("data") or {}
                if d.get("prompt_id") == prompt_id:
                    return True
    except Exception:
        return False
    finally:
        try:
            ws.close()
        except Exception:
            pass
    return False
