# H3 补齐：角色库 / 批量导入 / 尾帧续接 / 封面 / 打包 / 断点续跑 / 复现
import os
import re
import time
import uuid
from flask import jsonify, request, send_file


def _inside(base, path):
    return path == base or path.startswith(base + os.sep)


def _find_output(ctx, filename):
    out_dir = os.path.realpath(ctx["config"].COMFYUI_OUTPUT_DIR)
    if not filename:
        return None
    p = os.path.realpath(os.path.join(out_dir, filename))
    if _inside(out_dir, p) and os.path.isfile(p):
        return p
    base = os.path.basename(filename)
    for root, _dirs, names in os.walk(out_dir):
        if base in names:
            cand = os.path.realpath(os.path.join(root, base))
            if _inside(out_dir, cand):
                return cand
    return None


def _save(ctx):
    lock = ctx.get("JOBS_LOCK")
    if lock:
        with lock:
            snapshot = {k: ctx["h3"].clean_job(v) for k, v in ctx["JOBS"].items()}
    else:
        snapshot = {k: ctx["h3"].clean_job(v) for k, v in dict(ctx["JOBS"]).items()}
    ctx["h3"].save_jobs(snapshot)


def _wf(ctx, kind):
    cfg = ctx["config"]
    if kind == "video":
        return cfg.VIDEO_WORKFLOW
    if kind == "audio":
        return cfg.AUDIO_WORKFLOW
    return cfg.IMAGE_WORKFLOW


def register(app, ctx):
    h3 = ctx["h3"]
    JOBS = ctx["JOBS"]
    JOB_QUEUE = ctx["JOB_QUEUE"]
    new_job = ctx["new_job"]
    config = ctx["config"]

    @app.route("/api/characters", methods=["GET", "POST"])
    def api_characters():
        if request.method == "GET":
            return jsonify({"characters": h3.get_characters()})
        p = request.get_json(force=True, silent=True) or {}
        with ctx["JOBS_LOCK"]:
            chars = h3.get_characters()
            ch = {"id": uuid.uuid4().hex[:8], "name": p.get("name", "角色"), "image": p.get("image", ""), "audio": p.get("audio", ""), "voice_desc": p.get("voice_desc", ""), "look_desc": p.get("look_desc", ""), "style_block": p.get("style_block", "")}
            chars.append(ch)
            h3.save_characters(chars)
        return jsonify({"ok": True, "character": ch})

    @app.route("/api/characters/<cid>", methods=["DELETE"])
    def api_character_del(cid):
        with ctx["JOBS_LOCK"]:
            chars = [c for c in h3.get_characters() if str(c.get("id")) != str(cid)]
            h3.save_characters(chars)
        return jsonify({"ok": True})


    @app.route("/api/batch", methods=["POST"])
    def api_batch():
        p = request.get_json(force=True, silent=True) or {}
        project = str(p.get("project") or "").strip()
        kind = p.get("kind") or "video"
        if kind not in ("image", "video"):
            return jsonify({"ok": False, "error": "批量导入只支持 image / video"}), 400
        chain = bool(p.get("chain")) and kind == "video"
        lines = p.get("lines") or []
        if isinstance(p.get("text"), str) and p.get("text").strip():
            lines = [{"prompt": ln} for ln in p["text"].strip().splitlines() if ln.strip()]
        # 参考图从请求里取（顶层 images / first_image，或每行自带），不再写死任何项目的图
        top_images = [x for x in (p.get("images") or []) if x]
        top_first = p.get("first_image") or (top_images[0] if top_images else None)
        created = []
        prev_jid = None
        for i, ln in enumerate(lines):
            if isinstance(ln, str):
                ln = {"prompt": ln}
            shot = str(ln.get("shot") or (i + 1))
            line_images = [x for x in (ln.get("images") or top_images) if x]
            line_first = ln.get("first_image") or top_first
            if kind == "image":
                params = {"project": project, "shot": shot, "prompt": ln.get("prompt", ""), "mode": "文生图", "ratio": ln.get("ratio") or p.get("ratio") or "16:9", "resolution": ln.get("resolution") or p.get("resolution") or "1344x768", "images": line_images[:1], "style": ln.get("style") or p.get("style")}
            else:
                params = {"project": project, "shot": shot, "prompt": ln.get("prompt", ""), "mode": "参考生视频", "duration": ln.get("duration") or p.get("duration") or "5秒", "ratio": ln.get("ratio") or p.get("ratio") or "16:9", "resolution": ln.get("resolution") or p.get("resolution") or "1344x768", "ref_strength": ln.get("ref_strength") or p.get("ref_strength") or "1.0", "camera": ln.get("camera") or p.get("camera") or "", "first_image": line_first, "images": line_images}
                if chain and prev_jid and i > 0:
                    params["_chain_from"] = prev_jid
            jid = new_job(kind, _wf(ctx, kind), params)
            created.append(jid)
            prev_jid = jid
        return jsonify({"ok": True, "created": created, "chain": chain})

    @app.route("/api/jobs/<jid>/retry", methods=["POST"])
    def api_retry(jid):
        job = JOBS.get(jid)
        if not job:
            return jsonify({"ok": False, "error": "任务不存在"}), 404
        if job.get("status") in ("queued", "running"):
            return jsonify({"ok": False, "error": "任务进行中"}), 400
        # 重试前删掉旧文件，避免资产页出现无主文件
        for f in list(job.get("files") or []):
            fn = (f.get("path") or f.get("filename")) if isinstance(f, dict) else f
            fp = _find_output(ctx, fn)
            if fp:
                try:
                    os.remove(fp)
                except OSError:
                    pass
        job["status"] = "queued"
        job["progress"] = 0
        job["note"] = ""
        job["files"] = []
        job["started"] = None
        job["finished"] = None
        job["prompt_id"] = None
        JOB_QUEUE.put(jid)
        _save(ctx)
        return jsonify({"ok": True, "id": jid})

    @app.route("/api/jobs/<jid>", methods=["GET", "PATCH", "DELETE"])
    def api_job_one(jid):
        job = JOBS.get(jid)
        if not job:
            return jsonify({"ok": False, "error": "任务不存在"}), 404
        if request.method == "GET":
            # 单个任务查询：队列列表只回最近 30 条，老任务的状态靠这个查
            j = dict(job)
            j.pop("outputs", None)
            j["elapsed"] = round((j.get("finished") or time.time()) - (j.get("started") or j.get("created") or time.time()))
            return jsonify({"ok": True, "job": j})
        if request.method == "DELETE":
            if job.get("status") == "running":
                return jsonify({"ok": False, "error": "任务正在运行，不能删除"}), 400
            for f in list(job.get("files") or []):
                fn = (f.get("path") or f.get("filename")) if isinstance(f, dict) else f
                fp = _find_output(ctx, fn)
                if fp:
                    try:
                        os.remove(fp)
                    except OSError:
                        pass
            JOBS.pop(jid, None)
            _save(ctx)
            return jsonify({"ok": True})
        p = request.get_json(force=True, silent=True) or {}
        if "name" in p:
            job.setdefault("params", {})["name"] = p.get("name") or ""
        if "note" in p:
            job["note"] = p.get("note") or ""
        _save(ctx)
        return jsonify({"ok": True, "id": jid})

    @app.route("/api/tail/<jid>", methods=["POST"])
    def api_tail(jid):
        job = JOBS.get(jid)
        if not job:
            return jsonify({"ok": False, "error": "任务不存在"}), 404
        src = None
        for f in list(job.get("files") or []):
            fn = (f.get("path") or f.get("filename")) if isinstance(f, dict) else f
            if fn and str(fn).lower().endswith(".mp4"):
                src = _find_output(ctx, fn)
                break
        if not src:
            return jsonify({"ok": False, "error": "该任务没有视频文件"}), 400
        name = re.sub(r'[/\\:*?"<>|]', "_", str((job.get("params") or {}).get("name") or jid))
        tail_name = name + "_" + str(jid) + "_tail.png"
        out = os.path.join(config.COMFYUI_INPUT_DIR, tail_name)
        if not h3.last_frame(src, out):
            return jsonify({"ok": False, "error": "抽尾帧失败"}), 500
        params = dict(job.get("params") or {})
        params["first_image"] = tail_name
        params["images"] = [tail_name] + [x for x in (params.get("images") or []) if x != tail_name][:8]
        params["note"] = "尾帧续接"
        nid = new_job("video", config.VIDEO_WORKFLOW, params)
        return jsonify({"ok": True, "id": nid, "tail": tail_name})

    @app.route("/api/zip/<project>", methods=["GET"])
    def api_zip(project):
        from flask import after_this_request
        # 项目名只取文件名部分，防 ../ 穿越写到 output 目录外
        project = os.path.basename(project).strip()
        if not project or project in (".", ".."):
            return jsonify({"ok": False, "error": "项目名不合法"}), 400
        jobs = [j for j in JOBS.values() if (j.get("params") or {}).get("project") == project]
        zip_path = os.path.join(config.COMFYUI_OUTPUT_DIR, "%s_%d.zip" % (project, int(time.time())))
        made = h3.make_zip(project, jobs, config.COMFYUI_OUTPUT_DIR, zip_path)
        if not made:
            return jsonify({"ok": False, "error": "该项目暂无可打包文件"}), 404

        @after_this_request
        def remove_zip(response):
            try:
                os.remove(zip_path)
            except OSError:
                pass
            return response

        return send_file(zip_path, as_attachment=True, download_name=project + ".zip")
    @app.route("/api/assets/clear", methods=["POST"])
    def api_assets_clear():
        base = os.path.realpath(config.COMFYUI_OUTPUT_DIR)
        exts = (".png", ".jpg", ".jpeg", ".webp", ".mp4", ".webm", ".flac", ".wav", ".mp3", ".zip")
        # 1. 在锁内收集受保护的文件名（basename）
        protected = set()
        with ctx["JOBS_LOCK"]:
            for j in JOBS.values():
                if j.get("status") in ("queued", "running"):
                    for f in (j.get("files") or []):
                        if isinstance(f, dict):
                            fn = f.get("path") or f.get("filename")
                            if fn:
                                protected.add(os.path.basename(fn))
        # 2. 锁外删除文件
        count = 0
        for root, _dirs, files in os.walk(base):
            for name in files:
                if name.lower().endswith(exts):
                    if name in protected:
                        continue
                    try:
                        os.remove(os.path.join(root, name))
                        count += 1
                    except OSError:
                        pass
        # 3. 在锁内清理 JOBS：非 queued/running 的 files 清空，done/failed 的任务移除
        removed = 0
        with ctx["JOBS_LOCK"]:
            to_remove = []
            for jid, j in JOBS.items():
                status = j.get("status")
                if status in ("queued", "running"):
                    # 保留 files 不变
                    continue
                # 非运行中任务：清空 files
                if j.get("files"):
                    j["files"] = []
                # 标记 done/failed 任务移除
                if status in ("done", "failed"):
                    to_remove.append(jid)
            for jid in to_remove:
                JOBS.pop(jid, None)
                removed += 1
        _save(ctx)
        return jsonify({"ok": True, "deleted": count, "jobs_removed": removed})
    @app.route("/api/jobs/clear", methods=["POST"])
    def api_jobs_clear():
        # 清空队列：删除已完成/失败/排队中任务的记录及其文件；
        # 正在运行的任务不动（worker 还在写它），返回保留数。
        removed = 0
        kept = 0
        for jid in list(JOBS.keys()):
            job = JOBS.get(jid)
            if not job:
                continue
            if job.get("status") == "running":
                kept += 1
                continue
            for f in list(job.get("files") or []):
                fn = (f.get("path") or f.get("filename")) if isinstance(f, dict) else f
                fp = _find_output(ctx, fn)
                if fp:
                    try:
                        os.remove(fp)
                    except OSError:
                        pass
            JOBS.pop(jid, None)
            removed += 1
        _save(ctx)
        return jsonify({"ok": True, "removed": removed, "kept_running": kept})
