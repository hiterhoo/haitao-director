# 剧本拆分：可配置的 LLM 接口 + AI 拆分 + 角色/场景资产生成 + 每集拆分镜（第二期）
#
# 接口走 OpenAI 兼容的 /chat/completions 协议，DeepSeek、OpenAI、腾讯混元、
# 阿里通义（兼容模式）、Moonshot、智谱等主流服务都能直接用：
# 在页面「剧本」页签里填 API 地址、API Key、模型名即可，key 只存本机
# data/llm_config.json，不写进代码。分享源码给别人时不要带 data/ 目录。
import json
import os
import re
import shutil
import threading
import time
import urllib.error
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor

from flask import jsonify, request

CFG_NAME = "llm_config.json"
SCRIPTS_NAME = "scripts.json"

# 拆分是异步任务：POST 创建任务后立刻返回，前端轮询状态，避免长请求超时
_SPLIT_TASKS = {}
_SPLIT_LOCK = threading.Lock()
# scripts.json 读-改-写锁：后台拆分线程和页面保存会并发，别互相覆盖丢数据
_SCRIPTS_LOCK = threading.RLock()


def _set_split_progress(task_id, done, total):
    with _SPLIT_LOCK:
        t = _SPLIT_TASKS.get(task_id)
        if t:
            t["done"], t["total"] = done, total


def _prune_split_tasks():
    with _SPLIT_LOCK:
        if len(_SPLIT_TASKS) > 20:
            # 只删已结束的任务，运行中的删了前端会轮询到 404、结果状态也会丢
            done = [k for k, v in _SPLIT_TASKS.items() if v.get("status") in ("done", "failed")]
            old = sorted(done, key=lambda x: _SPLIT_TASKS[x]["created"])[: len(_SPLIT_TASKS) - 20]
            for k in old:
                _SPLIT_TASKS.pop(k, None)
CHUNK_MAX = 9000          # 单次喂给模型的剧本字符上限
EP_MARKER = re.compile(r"(?m)^[ \t#]*第\s*([0-9]+|[一二三四五六七八九十百千零〇两]+)\s*[集话章回][^\n]*$")

EXTRACT_SYSTEM = (
    "你是短剧剧本拆解助手。用户给你一集剧本原文，你只输出一个 JSON 对象，不要解释、不要 markdown 代码块。"
    "字段：title（本集标题，没有就概括一个）、synopsis（一句话梗概）、"
    "characters（本集出场角色数组，每项含 name 角色名、look_desc 外貌服装标志物的固定描述、voice_desc 声音特点，没有就空字符串）、"
    "scenes（本集场景数组，每项含 name 场景名、description 场景固定描述）、"
    "dialogues（台词数组，每项含 character 说话角色名、line 台词原文，只抄原文不要改写）。"
    "角色和场景描述要具体、可直接当绘画提示词用；原文没写的外貌不要编造，写“未交代”。"
)

STORYBOARD_SYSTEM = (
    "你是短剧分镜师。用户给你一集的梗概、角色、场景和台词，你把这一集拆成分镜，"
    "只输出一个 JSON 对象，不要解释、不要 markdown 代码块。"
    "字段：shots（数组，每项含 shot 镜号从1起、camera 景别和运镜（如“中景，缓慢推镜头”）、"
    "visual 本镜画面描述（中文）、dialogue 本镜台词（没有就空字符串，保留原文）、"
    "characters 本镜出场角色名数组、scene 本镜场景名、prompt 本镜的视频生成提示词）。"
    "每镜约5秒，按剧情节奏拆，一般15到30镜；台词要分配到对应的镜头里，不要丢台词。"
    "prompt 必须用 MiniMax H3 官方 Ref2VA 六段式格式，六段顺序固定："
    "subject_definitions / summary / retention_analysis / detailed_description / overall_soundscape / non_diegetic_music。"
    "写法：subject_definitions 把每个出场角色定义为 <Subject N> 并说明其外貌来自 <Picture N>（按出场顺序编号）；"
    "summary 一两句概括本镜；retention_analysis 逐个参考图写保留程度，角色定妆图用 fully_preserved；"
    "detailed_description 写 [Shot 1] 开头，包含风格（从剧本内容判断，如水墨国风/3D卡通）、场景、角色动作、运镜（写成自然句）、台词（保留中文原文，标明谁说）；"
    "overall_soundscape 1到2句环境声；non_diegetic_music 没有配乐写 N/A。"
    "prompt 主体用英文，台词保留中文。不要单独的负面词字段，要排除的东西写成普通句子放进描述。"
)

SPLIT_SYSTEM = (
    "你是短剧剧本拆解助手。用户给你一段没有分集标记的剧本原文，你把它按剧情节奏拆成若干集，"
    "只输出一个 JSON 对象，不要解释、不要 markdown 代码块。"
    "字段：title（剧名，没有就概括一个）、episodes（数组，每项含 title 本集标题、synopsis 一句话梗概、"
    "characters 本集出场角色数组（每项 name、look_desc 外貌服装标志物固定描述、voice_desc 声音特点）、"
    "scenes 本集场景数组（每项 name、description 场景固定描述）、"
    "dialogues 台词数组（每项 character、line 台词原文））。"
    "每集长度尽量均匀；原文没写的外貌不要编造，写“未交代”。"
)


def _norm_base(url):
    u = str(url or "").strip().rstrip("/")
    # 有人会把完整接口地址粘进来，去掉尾巴避免拼出 .../chat/completions/chat/completions
    if u.endswith("/chat/completions"):
        u = u[: -len("/chat/completions")]
    return u


def _cfg(h3):
    c = h3.load_json(CFG_NAME, {}) or {}
    return {
        "base_url": _norm_base(c.get("base_url")),
        "api_key": str(c.get("api_key") or ""),
        "model": str(c.get("model") or "").strip(),
    }


def _mask(key):
    if not key:
        return ""
    return "••••" + key[-4:] if len(key) > 4 else "••••"


def _chat(cfg, system, user, timeout=180):
    """调一次 OpenAI 兼容 chat/completions，返回回复文本。失败抛 RuntimeError。"""
    if not cfg["base_url"] or not cfg["api_key"] or not cfg["model"]:
        raise RuntimeError("LLM 接口未配置完整：请先在剧本页签填 API 地址、Key 和模型名")
    url = cfg["base_url"] + "/chat/completions"
    base = {
        "model": cfg["model"],
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "temperature": 0.2,
        "max_tokens": 8192,  # 分镜一集输出很长，不设上限容易被 provider 默认值截断
    }
    last_err = None
    for body in (dict(base, response_format={"type": "json_object"}), base):
        req = urllib.request.Request(
            url,
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json", "Authorization": "Bearer " + cfg["api_key"]},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data = json.loads(r.read().decode("utf-8", "replace"))
            content = data["choices"][0]["message"]["content"] or ""
            if isinstance(content, list):  # 部分网关返回分段 list
                content = "".join(str(part.get("text") or "") if isinstance(part, dict) else str(part) for part in content)
            return str(content)
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8", "replace")[:300]
            except Exception:
                pass
            last_err = RuntimeError("API 返回 HTTP %s：%s" % (e.code, detail))
            if e.code != 400:
                break
            # 400 可能是对方不支持 response_format，去掉再试一次
        except Exception as e:
            last_err = RuntimeError("调用 API 失败：%s" % e)
            break
    raise last_err or RuntimeError("调用 API 失败")


def _extract_json(text, cfg=None):
    t = str(text or "").strip()
    t = re.sub(r"^```(?:json)?\s*", "", t)
    t = re.sub(r"\s*```$", "", t)
    i, j = t.find("{"), t.rfind("}")
    if i < 0 or j <= i:
        raise RuntimeError("模型没有返回 JSON")
    frag = t[i:j + 1]
    try:
        return json.loads(frag)
    except json.JSONDecodeError as e:
        if cfg is None:
            raise RuntimeError("模型返回的 JSON 解析失败（%s），可重试一次或换个模型" % e.msg)
        # 让模型只修 JSON 再输出一次：弱模型常见的尾随逗号/单引号靠这一步兜底
        fixed = _chat(cfg, "你是 JSON 修复助手。用户给你一段解析失败的 JSON 和报错，你只输出修复后的合法 JSON 对象，不要解释、不要代码块，不要改动内容。",
                      "报错：%s\n\n%s" % (e, frag[:30000]))
        t2 = re.sub(r"^```(?:json)?\s*", "", str(fixed or "").strip())
        t2 = re.sub(r"\s*```$", "", t2)
        i2, j2 = t2.find("{"), t2.rfind("}")
        if i2 < 0 or j2 <= i2:
            raise RuntimeError("模型返回的 JSON 解析失败，修复重试也没成功，可换个模型再试")
        try:
            return json.loads(t2[i2:j2 + 1])
        except json.JSONDecodeError:
            raise RuntimeError("模型返回的 JSON 解析失败，修复重试也没成功，可换个模型再试")


def _norm_char(c):
    c = c if isinstance(c, dict) else {}
    return {
        "name": str(c.get("name") or "").strip(),
        "look_desc": str(c.get("look_desc") or "").strip(),
        "voice_desc": str(c.get("voice_desc") or "").strip(),
    }


def _norm_scene(s):
    s = s if isinstance(s, dict) else {}
    return {"name": str(s.get("name") or "").strip(), "description": str(s.get("description") or "").strip()}


def _norm_episode(e, no):
    e = e if isinstance(e, dict) else {}
    chars = [_norm_char(c) for c in (e.get("characters") or [])]
    scenes = [_norm_scene(s) for s in (e.get("scenes") or [])]
    diags = []
    for d in (e.get("dialogues") or []):
        if isinstance(d, dict):
            diags.append({"character": str(d.get("character") or "").strip(), "line": str(d.get("line") or "").strip()})
    return {
        "episode": no,
        "title": str(e.get("title") or ("第%d集" % no)).strip(),
        "synopsis": str(e.get("synopsis") or "").strip(),
        "characters": [c for c in chars if c["name"]],
        "scenes": [s for s in scenes if s["name"]],
        "dialogues": [d for d in diags if d["line"]],
    }


def split_local(text):
    """按「第X集」标记本地分集。返回 [(标记行, 正文), ...]，找不到返回 []。"""
    marks = list(EP_MARKER.finditer(text))
    if len(marks) < 2:
        return []
    out = []
    for i, m in enumerate(marks):
        start = m.end()
        end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
        out.append((m.group(0).strip(), text[start:end].strip()))
    return out


def _merge_globals(episodes):
    chars, scenes = {}, {}
    for ep in episodes:
        for c in ep["characters"]:
            g = chars.setdefault(c["name"], dict(c, episodes=[]))
            g["episodes"].append(ep["episode"])
            if len(c["look_desc"]) > len(g["look_desc"]):
                g["look_desc"] = c["look_desc"]
            if not g["voice_desc"] and c["voice_desc"]:
                g["voice_desc"] = c["voice_desc"]
        for s in ep["scenes"]:
            g = scenes.setdefault(s["name"], dict(s, episodes=[]))
            g["episodes"].append(ep["episode"])
            if len(s["description"]) > len(g["description"]):
                g["description"] = s["description"]
    return list(chars.values()), list(scenes.values())


def do_split(text, cfg, title_hint="", progress_cb=None):
    # 配置没填全就直接报错，不要让每一集都“失败”一遍还假装拆分成功
    if not cfg["base_url"] or not cfg["api_key"] or not cfg["model"]:
        raise RuntimeError("LLM 接口未配置完整：请先在剧本页签填 API 地址、Key 和模型名")
    chunks = split_local(text)
    episodes = []
    title = title_hint.strip()
    if chunks:
        done_count = [0]
        done_lock = threading.Lock()

        def one(item):
            idx, (mark, body) = item
            try:
                truncated = len(body) > CHUNK_MAX
                raw = _chat(cfg, EXTRACT_SYSTEM, (mark + "\n" + body[:CHUNK_MAX]))
                ep = _norm_episode(_extract_json(raw, cfg), idx + 1)
                if truncated:
                    ep["note"] = "本集原文较长，只分析了前 %d 字" % CHUNK_MAX
                return ep
            except Exception as e:
                # 单集失败不拖垮整本：留个空集+失败原因，用户可手动补全
                ep = _norm_episode({}, idx + 1)
                ep["title"] = (mark[:30] or ep["title"])
                ep["note"] = "本集 AI 提取失败：%s（可手动补全后保存）" % e
                return ep
            finally:
                if progress_cb:
                    with done_lock:
                        done_count[0] += 1
                        progress_cb(done_count[0], len(chunks))

        with ThreadPoolExecutor(max_workers=4) as ex:
            episodes = list(ex.map(one, enumerate(chunks)))
        # 全部集都失败（多半是 Key 错/地址错）就整体报错，别返回一堆空集
        if episodes and all("提取失败" in (e.get("note") or "") for e in episodes):
            raise RuntimeError(episodes[0]["note"].replace("本集 AI 提取失败：", "").replace("（可手动补全后保存）", ""))
    else:
        pieces = [text[i:i + CHUNK_MAX] for i in range(0, len(text), CHUNK_MAX)] or [text]
        failed = 0
        for pi, piece in enumerate(pieces):
            try:
                raw = _chat(cfg, SPLIT_SYSTEM, piece)
                data = _extract_json(raw, cfg)
            except Exception as e:
                # 单片失败隔离：留个空集+原因，别把已拆好的集和已花的调用费全丢
                failed += 1
                ep = _norm_episode({}, len(episodes) + 1)
                ep["note"] = "本段 AI 拆分失败：%s（可手动补全后保存）" % e
                episodes.append(ep)
                if progress_cb:
                    progress_cb(pi + 1, len(pieces))
                continue
            if not title:
                title = str(data.get("title") or "").strip()
            for e in (data.get("episodes") or []):
                episodes.append(_norm_episode(e, len(episodes) + 1))
            if progress_cb:
                progress_cb(pi + 1, len(pieces))
        if failed and failed == len(pieces):
            raise RuntimeError(episodes[0]["note"].replace("本段 AI 拆分失败：", "").replace("（可手动补全后保存）", ""))
    if not episodes:
        raise RuntimeError("没有拆出任何一集，请检查剧本文本或换个模型再试")
    chars, scenes = _merge_globals(episodes)
    return {
        "id": uuid.uuid4().hex[:12],
        "title": title or "未命名剧本",
        "created": time.time(),
        "source_chars": len(text),
        "episodes": episodes,
        "characters": chars,
        "scenes": scenes,
    }


def _norm_shot(s, no):
    s = s if isinstance(s, dict) else {}
    chars = [str(c).strip() for c in (s.get("characters") or []) if str(c).strip()]
    return {
        "shot": no,
        "camera": str(s.get("camera") or "").strip(),
        "visual": str(s.get("visual") or "").strip(),
        "dialogue": str(s.get("dialogue") or "").strip(),
        "characters": chars,
        "scene": str(s.get("scene") or "").strip(),
        "prompt": str(s.get("prompt") or "").strip(),
    }


def do_storyboard(script, episode, cfg, progress_cb=None):
    if not cfg["base_url"] or not cfg["api_key"] or not cfg["model"]:
        raise RuntimeError("LLM 接口未配置完整：请先在剧本页签填 API 地址、Key 和模型名")
    lines = []
    lines.append("剧名：%s" % script.get("title", ""))
    lines.append("本集：第%s集 %s" % (episode.get("episode"), episode.get("title", "")))
    lines.append("梗概：%s" % episode.get("synopsis", ""))
    lines.append("角色：")
    for c in episode.get("characters") or []:
        lines.append("- %s：%s" % (c.get("name", ""), c.get("look_desc", "")))
    lines.append("场景：")
    for s in episode.get("scenes") or []:
        lines.append("- %s：%s" % (s.get("name", ""), s.get("description", "")))
    lines.append("台词（按顺序）：")
    for d in episode.get("dialogues") or []:
        lines.append("%s：%s" % (d.get("character", ""), d.get("line", "")))
    full = "\n".join(lines)
    user = full[:12000]
    raw = _chat(cfg, STORYBOARD_SYSTEM, user)
    data = _extract_json(raw, cfg)
    shots = [_norm_shot(s, i + 1) for i, s in enumerate(data.get("shots") or [])]
    shots = [s for s in shots if s["prompt"] or s["visual"]]
    if not shots:
        raise RuntimeError("没有拆出分镜，请检查本集内容或换个模型再试")
    if len(full) > 12000:
        # 台词过长被截断：在任务结果里标注，别让用户以为分镜丢了台词是模型问题
        shots[0]["note"] = "本集内容较长，分镜只分析了前 12000 字，尾部台词可能没拆到"
    if progress_cb:
        progress_cb(1, 1)
    return shots


def _ensure_input(config, filename):
    """参考图要能被 ComfyUI 的 LoadImage 找到：文件在 output 里就复制一份到 input。
    返回可在工作流里用的文件名（找不到返回原名，让 ComfyUI 自己报错）。"""
    if not filename:
        return filename
    base = os.path.basename(filename)
    in_path = os.path.join(config.COMFYUI_INPUT_DIR, base)
    if os.path.isfile(in_path):
        return base
    out_dir = os.path.realpath(config.COMFYUI_OUTPUT_DIR)
    cand = os.path.realpath(os.path.join(out_dir, filename))
    if not (cand == out_dir or cand.startswith(out_dir + os.sep)):
        return base  # 路径穿越（../../ 等）：不复制，让 ComfyUI 按原名报错
    if not os.path.isfile(cand):
        cand = None
        for root, _dirs, names in os.walk(out_dir):
            if base in names:
                cand = os.path.join(root, base)
                break
    if cand and os.path.isfile(cand):
        try:
            os.makedirs(config.COMFYUI_INPUT_DIR, exist_ok=True)
            shutil.copyfile(cand, in_path)
            return base
        except OSError:
            return filename
    return filename


def _load_scripts(h3):
    d = h3.load_json(SCRIPTS_NAME, {}) or {}
    return d if isinstance(d, dict) else {}


def register(app, ctx):
    h3 = ctx["h3"]
    config = ctx["config"]
    new_job = ctx["new_job"]

    # ---------- LLM 接口配置（可配置，OpenAI 兼容协议） ----------
    @app.route("/api/llm/config", methods=["GET", "POST"])
    def api_llm_config():
        if request.method == "GET":
            c = _cfg(h3)
            return jsonify({"base_url": c["base_url"], "model": c["model"], "has_key": bool(c["api_key"]), "key_hint": _mask(c["api_key"])})
        p = request.get_json(force=True, silent=True) or {}
        c = _cfg(h3)
        if "base_url" in p:
            c["base_url"] = _norm_base(p.get("base_url"))
        if "model" in p:
            c["model"] = str(p.get("model") or "").strip()
        key = p.get("api_key")
        if p.get("clear_key"):
            c["api_key"] = ""
        elif isinstance(key, str) and key.strip() and not key.strip().startswith("•"):
            c["api_key"] = key.strip()
        h3.save_json(CFG_NAME, c)
        return jsonify({"ok": True, "has_key": bool(c["api_key"]), "key_hint": _mask(c["api_key"])})

    @app.route("/api/llm/test", methods=["POST"])
    def api_llm_test():
        try:
            reply = _chat(_cfg(h3), "你是连通性测试助手。", "只回复两个字：正常", timeout=60)
            return jsonify({"ok": True, "reply": reply.strip()[:50]})
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 400

    # ---------- 剧本拆分 ----------
    @app.route("/api/script/split", methods=["POST"])
    def api_script_split():
        p = request.get_json(force=True, silent=True) or {}
        text = str(p.get("text") or "")
        if len(text.strip()) < 50:
            return jsonify({"ok": False, "error": "剧本文本太短，至少粘贴或上传完整剧本"}), 400
        if len(text) > 500000:
            return jsonify({"ok": False, "error": "剧本超过 50 万字，请分段拆分（避免一次调用太多 API）"}), 400
        cfg = _cfg(h3)
        if not cfg["base_url"] or not cfg["api_key"] or not cfg["model"]:
            return jsonify({"ok": False, "error": "LLM 接口未配置完整：请先在剧本页签填 API 地址、Key 和模型名"}), 400
        title = str(p.get("title") or "")
        task_id = uuid.uuid4().hex[:12]
        with _SPLIT_LOCK:
            _SPLIT_TASKS[task_id] = {"status": "running", "done": 0, "total": 0, "created": time.time(), "script": None, "error": ""}

        def run():
            try:
                script = do_split(text, cfg, title, lambda d, t: _set_split_progress(task_id, d, t))
                with _SCRIPTS_LOCK:
                    scripts = _load_scripts(h3)
                    scripts[script["id"]] = script
                    h3.save_json(SCRIPTS_NAME, scripts)
                with _SPLIT_LOCK:
                    t = _SPLIT_TASKS.get(task_id)
                    if t:
                        t.update({"status": "done", "script": script})
            except Exception as e:
                with _SPLIT_LOCK:
                    t = _SPLIT_TASKS.get(task_id)
                    if t:
                        t.update({"status": "failed", "error": str(e)})

        threading.Thread(target=run, daemon=True).start()
        _prune_split_tasks()
        return jsonify({"ok": True, "task_id": task_id})

    @app.route("/api/script/split/<task_id>")
    def api_script_split_status(task_id):
        with _SPLIT_LOCK:
            t = _SPLIT_TASKS.get(task_id)
            if not t:
                return jsonify({"ok": False, "error": "任务不存在（可能已过期）"}), 404
            return jsonify({"ok": True, "kind": t.get("kind", "split"), "status": t["status"], "done": t["done"], "total": t["total"],
                            "script": t["script"], "shots": t.get("shots"), "episode": t.get("episode"), "error": t["error"]})

    # ---------- 每集拆分镜（第二期） ----------
    @app.route("/api/script/storyboard", methods=["POST"])
    def api_script_storyboard():
        p = request.get_json(force=True, silent=True) or {}
        scripts = _load_scripts(h3)
        s = scripts.get(str(p.get("script_id") or ""))
        if not s:
            return jsonify({"ok": False, "error": "剧本不存在"}), 404
        ep_no = p.get("episode")
        ep = next((e for e in s.get("episodes") or [] if e.get("episode") == ep_no), None)
        if ep is None:
            return jsonify({"ok": False, "error": "集数不存在"}), 404
        cfg = _cfg(h3)
        if not cfg["base_url"] or not cfg["api_key"] or not cfg["model"]:
            return jsonify({"ok": False, "error": "LLM 接口未配置完整：请先在剧本页签填 API 地址、Key 和模型名"}), 400
        task_id = uuid.uuid4().hex[:12]
        with _SPLIT_LOCK:
            _SPLIT_TASKS[task_id] = {"kind": "storyboard", "status": "running", "done": 0, "total": 1,
                                     "created": time.time(), "script": None, "shots": None, "episode": ep_no, "error": ""}

        def run():
            try:
                shots = do_storyboard(s, ep, cfg, lambda d, t: _set_split_progress(task_id, d, t))
                with _SCRIPTS_LOCK:
                    scripts2 = _load_scripts(h3)
                    s2 = scripts2.get(s["id"])
                    if s2:
                        ep2 = next((e for e in s2.get("episodes") or [] if e.get("episode") == ep_no), None)
                        if ep2 is not None:
                            ep2["shots"] = shots
                            h3.save_json(SCRIPTS_NAME, scripts2)
                with _SPLIT_LOCK:
                    t = _SPLIT_TASKS.get(task_id)
                    if t:
                        t.update({"status": "done", "shots": shots})
            except Exception as e:
                with _SPLIT_LOCK:
                    t = _SPLIT_TASKS.get(task_id)
                    if t:
                        t.update({"status": "failed", "error": str(e)})

        threading.Thread(target=run, daemon=True).start()
        _prune_split_tasks()
        return jsonify({"ok": True, "task_id": task_id})

    @app.route("/api/script/storyboard/queue", methods=["POST"])
    def api_script_storyboard_queue():
        """整集分镜入队生成视频。
        mode:
          - all      : 全部重新生成（默认）
          - missing  : 只生成还没有任务或任务已删除的镜头
          - failed   : 只重试失败的镜头
        chain: true 时启用镜头续接（后一镜自动用前一镜视频尾帧作首帧）
        """
        p = request.get_json(force=True, silent=True) or {}
        mode = str(p.get("mode") or "all").strip().lower()
        if mode not in ("all", "missing", "failed"):
            mode = "all"
        chain = bool(p.get("chain"))
        ratio = str(p.get("ratio") or "16:9")
        resolution = {"16:9": "1344x768", "9:16": "768x1344", "1:1": "1024x1024"}.get(ratio, "1344x768")

        with _SCRIPTS_LOCK:
            scripts = _load_scripts(h3)
            s = scripts.get(str(p.get("script_id") or ""))
            if not s:
                return jsonify({"ok": False, "error": "剧本不存在"}), 404
            ep_no = p.get("episode")
            ep = next((e for e in s.get("episodes") or [] if e.get("episode") == ep_no), None)
            if ep is None or not ep.get("shots"):
                return jsonify({"ok": False, "error": "这一集还没有分镜，先拆分镜"}), 400

            project = str(s.get("title") or "剧本").strip()
            char_lib = {str(c.get("name") or "").strip(): c for c in h3.get_characters()}
            scene_items = {x.get("name"): x for x in s.get("scenes") or []}
            JOBS = ctx["JOBS"]
            JOBS_LOCK = ctx["JOBS_LOCK"]

            def _job(jid):
                if not jid:
                    return None
                with JOBS_LOCK:
                    return JOBS.get(str(jid))

            created = []
            skipped = []
            warnings = []
            prev_jid = None
            for idx, sh in enumerate(ep["shots"]):
                old_jid = sh.get("job_id")
                old_job = _job(old_jid)
                old_status = (old_job or {}).get("status")

                # 按 mode 决定是否跳过
                if mode == "missing":
                    if old_job and old_status in ("queued", "running", "done"):
                        skipped.append({"shot": sh.get("shot"), "reason": "已有任务"})
                        prev_jid = old_jid or prev_jid
                        continue
                elif mode == "failed":
                    # 只重试：有 job_id 且任务失败，或有 job_id 但任务已被删除（old_job 为 None）
                    if old_jid:
                        if old_job is None or old_status == "failed":
                            pass  # 需要重试
                        else:
                            skipped.append({"shot": sh.get("shot"), "reason": "非失败任务"})
                            prev_jid = old_jid or prev_jid
                            continue
                    else:
                        # 从未排过队（没有 job_id）的镜头在 failed 模式下跳过
                        skipped.append({"shot": sh.get("shot"), "reason": "未生成过"})
                        continue

                images = []
                for name in sh.get("characters") or []:
                    ch = char_lib.get(str(name).strip())
                    if ch and ch.get("image"):
                        fn = _ensure_input(config, ch["image"])
                        if fn not in images:
                            images.append(fn)
                    elif name and name not in warnings:
                        warnings.append(name)
                sc = scene_items.get(sh.get("scene") or "")
                if sc and sc.get("asset_job"):
                    job = _job(sc["asset_job"]) or {}
                    files = job.get("files") or []
                    if job.get("status") == "done" and files and isinstance(files[0], dict):
                        fn = _ensure_input(config, files[0].get("filename") or files[0].get("path") or "")
                        if fn and fn not in images:
                            images.append(fn)

                params = {
                    "project": project,
                    "shot": str(sh.get("shot") or ""),
                    "prompt": sh.get("prompt") or sh.get("visual") or "",
                    "mode": "参考生视频",
                    "duration": "5秒",
                    "ratio": ratio,
                    "resolution": resolution,
                    "ref_strength": "1.0",
                    "first_image": images[0] if images else None,
                    "images": images[:9],
                }

                if chain and prev_jid and idx > 0:
                    params["_chain_from"] = prev_jid

                jid = new_job("video", config.VIDEO_WORKFLOW, params)
                sh["job_id"] = jid
                created.append(jid)
                prev_jid = jid

            # 保存修改后的 shots
            h3.save_json(SCRIPTS_NAME, scripts)

        return jsonify({
            "ok": True,
            "created": created,
            "skipped": skipped,
            "warnings": warnings,
            "mode": mode,
            "chain": chain,
        })
    @app.route("/api/script/list")
    def api_script_list():
        items = [
            {"id": s.get("id"), "title": s.get("title", ""), "created": s.get("created", 0), "episodes": len(s.get("episodes") or [])}
            for s in _load_scripts(h3).values()
        ]
        items.sort(key=lambda x: x["created"], reverse=True)
        return jsonify({"scripts": items})

    @app.route("/api/script/<sid>", methods=["GET", "PUT", "DELETE"])
    def api_script_one(sid):
        if request.method == "GET":
            scripts = _load_scripts(h3)
            if sid not in scripts:
                return jsonify({"ok": False, "error": "剧本不存在"}), 404
            return jsonify({"ok": True, "script": scripts[sid]})
        if request.method == "DELETE":
            with _SCRIPTS_LOCK:
                scripts = _load_scripts(h3)
                if sid not in scripts:
                    return jsonify({"ok": False, "error": "剧本不存在"}), 404
                scripts.pop(sid, None)
                h3.save_json(SCRIPTS_NAME, scripts)
            return jsonify({"ok": True})
        p = request.get_json(force=True, silent=True) or {}
        # 结构校验：脏数据存进去会让后续接口连环 500，存之前挡住
        for k in ("episodes", "characters", "scenes"):
            if k in p and not isinstance(p[k], list):
                return jsonify({"ok": False, "error": "%s 必须是数组" % k}), 400
            if k in p and any(not isinstance(x, dict) for x in p[k]):
                return jsonify({"ok": False, "error": "%s 里每一项必须是对象" % k}), 400
        if "episodes" in p:
            for e in p["episodes"]:
                try:
                    e["episode"] = int(e.get("episode"))
                except (TypeError, ValueError):
                    return jsonify({"ok": False, "error": "episode 必须是整数"}), 400
        with _SCRIPTS_LOCK:
            scripts = _load_scripts(h3)
            if sid not in scripts:
                return jsonify({"ok": False, "error": "剧本不存在"}), 404
            s = scripts[sid]
            for k in ("title", "episodes", "characters", "scenes"):
                if k in p:
                    s[k] = p[k]
            h3.save_json(SCRIPTS_NAME, scripts)
        return jsonify({"ok": True, "script": s})

    # ---------- 角色 / 场景一键生成资产 ----------
    @app.route("/api/script/asset", methods=["POST"])
    def api_script_asset():
        p = request.get_json(force=True, silent=True) or {}
        typ = p.get("type")
        name = str(p.get("name") or "").strip()
        if typ not in ("character", "scene"):
            return jsonify({"ok": False, "error": "type 必须是 character 或 scene"}), 400

        with _SCRIPTS_LOCK:
            scripts = _load_scripts(h3)
            s = scripts.get(str(p.get("script_id") or ""))
            if not s:
                return jsonify({"ok": False, "error": "剧本不存在"}), 404

            JOBS = ctx["JOBS"]
            JOBS_LOCK = ctx["JOBS_LOCK"]

            def _running_job(item):
                jid = item.get("asset_job")
                if not jid:
                    return None
                with JOBS_LOCK:
                    job = JOBS.get(str(jid))
                if job and job.get("status") in ("queued", "running"):
                    return str(jid)
                return None

            def _save_item(item, job_id, extra=None):
                # 这里已经在 _SCRIPTS_LOCK 内，但 _save_item 也会 acquire _SCRIPTS_LOCK，依赖 RLock 可重入
                with _SCRIPTS_LOCK:
                    scripts2 = _load_scripts(h3)
                    s2 = scripts2.get(s["id"])
                    if s2:
                        pool = s2.get("characters") if typ == "character" else s2.get("scenes")
                        it2 = next((x for x in pool or [] if x.get("name") == name), None)
                        if it2 is not None:
                            it2["asset_job"] = job_id
                            if extra:
                                it2.update(extra)
                            h3.save_json(SCRIPTS_NAME, scripts2)

            if typ == "character":
                item = next((c for c in s.get("characters") or [] if c.get("name") == name), None)
                if not item:
                    return jsonify({"ok": False, "error": "角色不存在"}), 404
                running = _running_job(item)
                if running:
                    return jsonify({"ok": True, "job_id": running, "character_id": item.get("character_id", ""), "already": True})
                desc = item.get("look_desc") or ""
                prompt = "角色定妆照，正面全身站姿，纯色背景，%s：%s" % (name, desc)
                params = {"mode": "文生图", "prompt": prompt, "project": str(s.get("title") or "剧本").strip(), "shot": "定妆-" + name,
                          "ratio": "1:1", "resolution": "1024x1024", "count": 1, "seed": "随机",
                          "cfg": config.LOCKED["image_cfg_default"], "images": []}
                job_id = new_job("image", config.IMAGE_WORKFLOW, params)
                # 同步建/更新角色库条目
                with JOBS_LOCK:
                    chars = h3.get_characters()
                    ch = next((c for c in chars if c.get("name") == name), None)
                    if ch is None:
                        ch = {"id": uuid.uuid4().hex[:8], "name": name, "image": "", "audio": "",
                              "voice_desc": item.get("voice_desc", ""), "look_desc": desc, "style_block": ""}
                        chars.append(ch)
                    else:
                        ch["look_desc"] = desc or ch.get("look_desc", "")
                        if item.get("voice_desc"):
                            ch["voice_desc"] = item["voice_desc"]
                    h3.save_characters(chars)
                _save_item(item, job_id, {"character_id": ch["id"]})
                return jsonify({"ok": True, "job_id": job_id, "character_id": ch["id"]})

            if typ == "scene":
                item = next((x for x in s.get("scenes") or [] if x.get("name") == name), None)
                if not item:
                    return jsonify({"ok": False, "error": "场景不存在"}), 404
                running = _running_job(item)
                if running:
                    return jsonify({"ok": True, "job_id": running, "already": True})
                desc = item.get("description") or ""
                prompt = "场景参考图，无人物，%s：%s" % (name, desc)
                params = {"mode": "文生图", "prompt": prompt, "project": str(s.get("title") or "剧本").strip(), "shot": "场景-" + name,
                          "ratio": "16:9", "resolution": "1344x768", "count": 1, "seed": "随机",
                          "cfg": config.LOCKED["image_cfg_default"], "images": []}
                job_id = new_job("image", config.IMAGE_WORKFLOW, params)
                _save_item(item, job_id)
                return jsonify({"ok": True, "job_id": job_id})

        return jsonify({"ok": False, "error": "type 必须是 character 或 scene"}), 400
    @app.route("/api/characters/<cid>/image", methods=["POST"])
    def api_character_bind_image(cid):
        p = request.get_json(force=True, silent=True) or {}
        img = str(p.get("image") or "").strip()
        if not img or os.path.splitext(img)[1].lower() not in (".png", ".jpg", ".jpeg", ".webp", ".bmp"):
            return jsonify({"ok": False, "error": "请传图片文件名"}), 400
        with ctx["JOBS_LOCK"]:
            chars = h3.get_characters()
            ch = next((c for c in chars if str(c.get("id")) == str(cid)), None)
            if not ch:
                return jsonify({"ok": False, "error": "角色不存在"}), 404
            # 生成图在 output 里，LoadImage 只认 input：绑定时复制一份过去
            ch["image"] = _ensure_input(config, img)
            h3.save_characters(chars)
        return jsonify({"ok": True, "character": ch})
