#!/usr/bin/env python3
"""海涛AI导演台后端：生图 / 视频 / 音频 / 资产管理，都调同一套 ComfyUI。

- 进度：提交后开一个 WebSocket 线程收 ComfyUI 的采样进度
  （value/max），收不到就按队列位置 + 已用时间兜底显示；
  前端每 2 秒轮询 /api/jobs，看进度条、排队位置、已用时间。
- 上传：/api/upload 直接存进 ComfyUI 的 input 目录，文件名回传给前端，
  生成时按顺序写进工作流的 LoadImage / LoadVideo / LoadAudio 节点。
- 资产：/api/assets 直接扫 ComfyUI 的 output 目录，和 ComfyUI 同步；
  /api/file/<path> 预览下载，DELETE /api/assets/<path> 删除。
- 结果直显：任务完成时 history 里的 outputs 转成 /api/view 链接，
  前端直接 <img> / <video> / <audio> 显示，不用进 ComfyUI。
"""
import json
import mimetypes
import os
import queue
import re
import shutil
import subprocess
import threading
import time
import urllib.parse
import uuid

from flask import Flask, jsonify, render_template, request, send_file

import config
import h3
import h3_routes
import script_routes
import updater
from comfy_client import (
    check_health,
    get_history_entry,
    get_queue,
    history_error_text,
    listen_progress,
    submit_prompt,
)

app = Flask(__name__)
os.makedirs(config.OUTPUT_DIR, exist_ok=True)
os.makedirs(config.COMFYUI_INPUT_DIR, exist_ok=True)
os.makedirs(config.COMFYUI_OUTPUT_DIR, exist_ok=True)

# 文生视频（不传参考图）时工作流的 LoadImage 还指着 cat_ref.png，
# input 里没有这个文件会直接报错：启动时补一张 1x1 占位图。
_cat_ref = os.path.join(config.COMFYUI_INPUT_DIR, "cat_ref.png")
if not os.path.isfile(_cat_ref):
    try:
        import base64 as _b64
        with open(_cat_ref, "wb") as _f:
            _f.write(_b64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8Xw8AAwMCAO+ip1sAAAAASUVORK5CYII="))
    except Exception:
        pass

JOBS = {}          # job_id -> job dict
JOBS_LOCK = threading.Lock()   # 多线程读写 JOBS 时加锁，避免字典竞态偶发 500
JOB_QUEUE = queue.Queue()


def _safe_int(value, default):
    try:
        return int(float(str(value).strip()))
    except Exception:
        return default


def _safe_float(value, default):
    try:
        return float(str(value).strip())
    except Exception:
        return default


def _parse_size(params):
    """从 resolution（如 "1344x768" / "768×1344"）或 ratio（如 "9:16"）
    解析出 (width, height)。解析不出返回 None，调用方保持工作流默认尺寸。
    两者都给了但宽高比对不上时以 ratio 为准（页面默认 ratio=9:16、
    resolution=1536×864 本身就冲突，不能让分辨率默默压过比例）。"""
    ratio = str(params.get("ratio") or "").strip()
    table = {
        "16:9": (1344, 768),
        "9:16": (768, 1344),
        "1:1": (1024, 1024),
        "4:3": (1152, 864),
        "3:4": (864, 1152),
    }
    ratio_size = table.get(ratio)
    res = str(params.get("resolution") or "")
    m = re.search(r"(\d{3,5})\s*[x×X*]\s*(\d{3,5})", res)
    if m:
        w, h = int(m.group(1)), int(m.group(2))
        if 64 <= w <= 8192 and 64 <= h <= 8192:
            if ratio_size and abs(w / h - ratio_size[0] / ratio_size[1]) > 0.05:
                return ratio_size
            return w, h
    return ratio_size


def _inside(base, path):
    """path 是否真的在 base 目录内（防 ../ 穿越；注意不能只用 startswith，
    /a/output2 也以 /a/output 开头）。"""
    return path == base or path.startswith(base + os.sep)


def _snapshot_jobs():
    with JOBS_LOCK:
        return {k: h3.clean_job(v) for k, v in JOBS.items()}


_SAVE_LOCK = threading.Lock()


def _save_all_jobs():
    # 快照+写盘串行化：避免两个线程逆序落盘时旧快照覆盖新快照
    with _SAVE_LOCK:
        h3.save_jobs(_snapshot_jobs())

# H3: 断点续跑，恢复持久化队列。
# running 的任务先拿 prompt_id 去 ComfyUI history 查一遍：
# 已跑完的直接标 done（不再重跑花钱），出错的标 failed，
# 查不到的才重新入队。queued 的直接重新入队。
try:
    _saved = h3.load_jobs()
    for _jid, _j in _saved.items():
        try:
            if _jid in JOBS:
                continue
            JOBS[_jid] = _j
            _st = _j.get("status")
            if _st == "queued":
                _j["progress"] = 0
                JOB_QUEUE.put(_jid)
            elif _st == "running":
                _pid = _j.get("prompt_id")
                _entry = get_history_entry(config.COMFYUI_URL, _pid) if _pid else None
                _outs = (_entry or {}).get("outputs")
                _estr = ((_entry or {}).get("status") or {}).get("status_str")
                if _outs:
                    _j["status"] = "done"
                    _j["progress"] = 100
                    _j["finished"] = _j.get("started") or _j.get("created")
                    _j["outputs"] = _outs
                    _j["_restored_done"] = True  # 文件列表等函数定义完再重建（见 worker 启动后）
                    _j["note"] = "平台重启后从 ComfyUI 记录恢复为已完成"
                elif _estr == "error":
                    _j["status"] = "failed"
                    _j["finished"] = _j.get("started") or _j.get("created")
                    _j["note"] = "ComfyUI 执行出错：" + (history_error_text(_entry) or "未知错误")
                else:
                    _j["status"] = "queued"
                    _j["progress"] = 0
                    JOB_QUEUE.put(_jid)
        except Exception:
            continue
except Exception:
    pass
WORKER_RUNNING = True
ALLOWED_EXTS = {
    "image": {".png", ".jpg", ".jpeg", ".webp", ".bmp"},
    "video": {".mp4", ".mov", ".webm", ".avi", ".mkv"},
    "audio": {".wav", ".mp3", ".flac", ".ogg", ".m4a"},
}
ALL_EXTS = ALLOWED_EXTS["image"] | ALLOWED_EXTS["video"] | ALLOWED_EXTS["audio"]


def load_workflow(name):
    path = os.path.join(config.WORKFLOW_DIR, name)
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _media_kind(filename):
    ext = os.path.splitext(filename)[1].lower()
    for kind, exts in ALLOWED_EXTS.items():
        if ext in exts:
            return kind
    return None


def _nodes_of(wf):
    """ComfyUI API 格式工作流：顶层键就是节点 ID（"1"、"2"…），
    每个值是 {"class_type": ..., "inputs": {...}}。
    兼容 {"nodes": {...}} 这种包了一层的格式。"""
    if not isinstance(wf, dict):
        return {}
    if isinstance(wf.get("nodes"), dict):
        return wf["nodes"]
    return {k: v for k, v in wf.items()
            if isinstance(v, dict) and "class_type" in v}


def _next_id(nodes):
    nums = [int(k) for k in nodes if str(k).isdigit()]
    return str(max(nums) + 1) if nums else "1"


def _find(nodes, *classes):
    for nid, node in nodes.items():
        if node.get("class_type") in classes:
            return nid, node
    return None, None


def apply_params(workflow, params):
    """把页面参数写进 workflow（ComfyUI API 格式，顶层即节点表）。
    1) 文本占位符 {{PROMPT}} / {{TEXT}} 替换；
    2) 通用参数：宽高（resolution 或 ratio 解析）写进 H3 节点 /
       EmptyLatentImage；种子 / 步数 / CFG 写进 KSampler（参数给了才写，
       没给保持工作流默认）；负面词写进 KSampler 的 negative 文本节点；
       生成数量写进 EmptyLatentImage 的 batch_size；
       TTS 的 instruct（音色描述 / 情绪）和种子写进 FB_Qwen3TTSVoiceDesign；
    3) 媒体注入（按 2026-11-18 实例实测的节点定义）：
       - H3 参考生视频（MiniMaxH3ReferenceToVideo）：ref_images / ref_videos /
         ref_video_audios / ref_audios 都是 Autogrow 字典，槽位键从 0 起编号
         （ref_image_0…8、ref_video_0…2、ref_audio_0…2）。第 1 张图复用
         已有的 LoadImage 节点，多出来的图新建 LoadImage 节点接上；
         视频用 VHS_LoadVideo（format="H3"，输出 0 是 IMAGE 帧、输出 2 是音频），
         音频用 LoadAudio 新建节点接上。文生视频不传图时保留工作流默认
         的 cat_ref.png（该节点要求至少 1 张参考图）。
       - Qwen-Image 图生图：工作流里没有 LoadImage，新建 LoadImage +
         VAEEncode，把 KSampler 的 latent_image 指过去；denoise（重绘强度，
         图生图改动原图的程度）由页面参数决定，默认 0.75。
       - Qwen3-TTS VoiceDesign 没有参考音频输入，接不了参考音。
    图片列表先去重（first_image + images + last_image 会重复）。
    没传媒体就不动对应节点；传了但写不进去，记在 note 里。"""
    # 占位符替换必须在 json.loads 之后、按节点字符串值来做：
    # 原实现直接对 JSON 文本 replace，提示词里含英文引号 " 或反斜杠 \ 时
    # 会把未转义字符塞进 JSON 字符串，json.loads 直接抛异常。
    wf = json.loads(json.dumps(workflow, ensure_ascii=False))
    nodes = _nodes_of(wf)
    prompt_text = str(params.get("prompt") or "")
    tts_text = str(params.get("text") or "")
    # 无条件替换：提示词为空时把占位符清成空串，别把字面量 {{PROMPT}} 发给 ComfyUI
    for node in nodes.values():
        inputs = node.get("inputs")
        if not isinstance(inputs, dict):
            continue
        for key, val in inputs.items():
            if not isinstance(val, str):
                continue
            if "{{PROMPT}}" in val:
                inputs[key] = val.replace("{{PROMPT}}", prompt_text)
            if "{{TEXT}}" in val:
                inputs[key] = val.replace("{{TEXT}}", tts_text)

    images, videos, audios = [], [], []
    for src in (params.get("first_image"), *(params.get("images") or []),
                params.get("last_image"), *(params.get("ref_images") or [])):
        if src and src not in images:
            images.append(src)
    for src in (params.get("videos") or []) + (params.get("ref_videos") or []):
        if src and src not in videos:
            videos.append(src)
    for src in (params.get("audios") or []) + (params.get("ref_audios") or []):
        if src and src not in audios:
            audios.append(src)
    images = images[:9]
    videos = videos[:3]
    audios = audios[:3]
    missing = []

    def _new_node(class_type, inputs):
        nid = _next_id(nodes)
        nodes[nid] = {"class_type": class_type, "inputs": inputs}
        # API 格式下新节点直接挂在顶层
        if "nodes" not in wf:
            wf[nid] = nodes[nid]
        return nid

    # --- 通用参数：尺寸 / 种子 / 步数 / CFG / 负面词 / 数量 ---
    size = _parse_size(params)
    seed = params.get("seed")
    try:
        seed = int(seed) if seed is not None and str(seed) != "" else None
    except Exception:
        seed = None

    _ks_id, ks_node = _find(nodes, "KSampler")
    if ks_node is not None:
        ks_in = ks_node.setdefault("inputs", {})
        if seed is not None:
            ks_in["seed"] = seed
        if params.get("steps") is not None:
            ks_in["steps"] = _safe_int(params["steps"], ks_in.get("steps", 8))
        if params.get("cfg") is not None:
            ks_in["cfg"] = _safe_float(params["cfg"], ks_in.get("cfg", 1.0))
        # 负面词：顺着 KSampler 的 negative 输入找到文本节点写进去
        if params.get("negative") is not None:
            neg_ref = ks_in.get("negative")
            if isinstance(neg_ref, (list, tuple)) and neg_ref:
                neg_node = nodes.get(str(neg_ref[0]))
                if neg_node is not None and neg_node.get("class_type") == "CLIPTextEncode":
                    neg_node.setdefault("inputs", {})["text"] = params["negative"] or ""

    # 图片工作流的潜空间尺寸 + 数量
    _el_id, el_node = _find(nodes, "EmptyLatentImage")
    if el_node is not None:
        el_in = el_node.setdefault("inputs", {})
        if size:
            el_in["width"], el_in["height"] = size
        if params.get("count") is not None:
            el_in["batch_size"] = max(1, min(8, _safe_int(params["count"], 1)))

    # --- H3 参考生视频 ---
    h3_id, h3 = _find(nodes, "MiniMaxH3ReferenceToVideo")
    if h3 is not None:
        inputs = h3.setdefault("inputs", {})
        # 时长：5 秒 124 帧，10 秒 241 帧（从 "5秒"/"10秒" 里提数字）
        _dm = re.search(r"\d+", str(params.get("duration", "")))
        _secs = int(_dm.group(0)) if _dm else 5
        inputs["length"] = 241 if _secs >= 10 else 124
        if size:
            inputs["width"], inputs["height"] = size
        if images:
            # 第 1 张复用已有 LoadImage 节点（工作流节点 6）
            li_id, li = _find(nodes, "LoadImage")
            if li is None:
                li_id = _new_node("LoadImage", {"image": images[0]})
            else:
                li.setdefault("inputs", {})["image"] = images[0]
            ref_images = {"ref_image_0": [li_id, 0]}
            for i, fname in enumerate(images[1:], start=1):
                nid = _new_node("LoadImage", {"image": fname})
                ref_images["ref_image_%d" % i] = [nid, 0]
            inputs["ref_images"] = ref_images
        if videos:
            ref_videos, ref_video_audios = {}, {}
            for i, fname in enumerate(videos):
                nid = _new_node("VHS_LoadVideo", {
                    "video": fname, "force_rate": 0, "custom_width": 0,
                    "custom_height": 0, "frame_load_cap": 0,
                    "skip_first_frames": 0, "select_every_nth": 1,
                    "format": "H3",
                })
                ref_videos["ref_video_%d" % i] = [nid, 0]
                ref_video_audios["ref_video_audio_%d" % i] = [nid, 2]
            inputs["ref_videos"] = ref_videos
            inputs["ref_video_audios"] = ref_video_audios
        if audios:
            ref_audios = {}
            for i, fname in enumerate(audios):
                nid = _new_node("LoadAudio", {"audio": fname})
                ref_audios["ref_audio_%d" % i] = [nid, 0]
            inputs["ref_audios"] = ref_audios

    # --- Qwen-Image 图生图 ---
    elif images:
        li_id, li = _find(nodes, "LoadImage")
        ks_id, ks = _find(nodes, "KSampler")
        vae_id, _vae = _find(nodes, "VAELoader")
        if ks is not None and vae_id is not None:
            if li is None:
                li_id = _new_node("LoadImage", {"image": images[0]})
            else:
                li.setdefault("inputs", {})["image"] = images[0]
            enc_id = _new_node("VAEEncode", {
                "pixels": [li_id, 0], "vae": [vae_id, 0],
            })
            ks_inputs = ks.setdefault("inputs", {})
            ks_inputs["latent_image"] = [enc_id, 0]
            # 重绘强度 denoise：图生图时在原图基础上改动多大。
            # 原理是先给原图加噪声再重画，denoise 就是加噪声的强度：
            # 0=几乎不动原图，1=完全重画（近似文生图，原图只剩大概构图）。
            # 0.75 中等改动（保留构图/姿势/颜色）；0.92 接近重画
            # （适合拿定妆图当参考、按新提示词画新场景）。页面可调，默认 0.75。
            ks_inputs["denoise"] = max(0.05, min(1.0, _safe_float(params.get("denoise", 0.75), 0.75)))
        else:
            missing.append("图生图：工作流里没有 KSampler / VAELoader，图片没写进去")

    # --- TTS 设计音色：音色描述 / 情绪 / 种子写进 VoiceDesign 节点 ---
    # 预设音色：描述 + 固定种子，保证同一预设每次都是同一个声音。
    _EMO_EN = {"平静": "calm", "紧张": "tense", "愤怒": "angry", "悲伤": "sad"}
    _VOICE_PRESETS = {
        "男声": ("A clear adult male voice speaking Mandarin Chinese, natural and steady tone", 42),
        "女声": ("A clear adult female voice speaking Mandarin Chinese, gentle and natural tone", 7),
    }
    _tts_id, tts = _find(nodes, "FB_Qwen3TTSVoiceDesign")
    if tts is not None:
        tts_in = tts.setdefault("inputs", {})
        instruct = str(params.get("voice_desc") or "").strip()
        preset_seed = None
        if not instruct:
            preset = _VOICE_PRESETS.get(str(params.get("preset") or ""))
            if preset:
                instruct, preset_seed = preset
        emo = _EMO_EN.get(str(params.get("emotion") or ""))
        if not instruct and emo:
            instruct = "A voice speaking Mandarin Chinese, %s tone" % emo
        elif instruct and emo and params.get("emotion") != "平静":
            instruct = instruct + ", speaking in a %s tone" % emo
        if instruct:
            tts_in["instruct"] = instruct
        # 页面给了种子用页面的；没给（随机）时用预设的固定种子，
        # 这样同一预设多次生成音色一致，自定义描述则每次随机。
        tts_seed = seed if seed is not None else preset_seed
        if tts_seed is not None:
            tts_in["seed"] = tts_seed

    # --- TTS 克隆音色：文本 / 参考文本 / 种子写进克隆节点，
    # 参考音频文件名写进 LoadAudio。节点名按 FB 包命名找几个候选。 ---
    _clone_id, clone = None, None
    for _cand in ("FB_Qwen3TTSClone", "FB_Qwen3TTSVoiceClone", "Qwen3TTSClone"):
        _clone_id, clone = _find(nodes, _cand)
        if clone is not None:
            break
    if clone is not None:
        cin = clone.setdefault("inputs", {})
        if params.get("text"):
            # 真实节点 FB_Qwen3TTSVoiceClone 的文本输入叫 target_text
            # （2026-11-27 在实例上查 /object_info 确认），不是 text。
            cin["target_text"] = params["text"]
        if params.get("ref_text"):
            cin["ref_text"] = params["ref_text"]
        if seed is not None:
            cin["seed"] = seed
        _la_id, la = _find(nodes, "LoadAudio")
        if la is not None and params.get("ref_audio"):
            la.setdefault("inputs", {})["audio"] = params["ref_audio"]
        elif params.get("ref_audio"):
            cin["ref_audio"] = params["ref_audio"]

    # TTS 设计音色工作流没有参考音频输入，有音频参数也接不进去，直接提示
    if audios and h3 is None and _tts_id and clone is None:
        missing.append("TTS 节点没有参考音频输入，音频没写进去")

    # --- 视频超分工作流（minimax_h3_upscale.json）：源视频 / 目标尺寸 / 输出名前缀 ---
    _vhs_id, vhs = _find(nodes, "VHS_LoadVideo")
    if vhs is not None and params.get("upscale_video"):
        vhs.setdefault("inputs", {})["video"] = params["upscale_video"]
    _up_id, up = _find(nodes, "MiniMaxH3LatentUpscaleWithModel")
    if up is not None:
        uin = up.setdefault("inputs", {})
        if params.get("target_w"):
            uin["width"] = int(params["target_w"])
        if params.get("target_h"):
            uin["height"] = int(params["target_h"])
    _vc_id, vc = _find(nodes, "VHS_VideoCombine")
    if vc is not None and params.get("name"):
        vc.setdefault("inputs", {})["filename_prefix"] = str(params["name"])
    return wf, missing


def _resolve_output_filename(filename, subfolder=""):
    """ComfyUI history 给的文件名和磁盘实际文件名可能差一个字符：
    SaveVideo 的磁盘文件是 visual_video_00002_.mp4（.mp4 前多个下划线），
    history 里却是 visual_video_00002.mp4。按磁盘实际文件为准，
    找不到就试着在扩展名前加/去一个下划线。"""
    base = os.path.realpath(config.COMFYUI_OUTPUT_DIR)
    candidates = [filename]
    stem, ext = os.path.splitext(filename)
    if stem.endswith("_"):
        candidates.append(stem[:-1] + ext)
    else:
        candidates.append(stem + "_" + ext)
    for cand in candidates:
        path = os.path.realpath(os.path.join(base, subfolder, cand))
        if _inside(base, path) and os.path.isfile(path):
            return cand
    return filename


def outputs_to_files(outputs):
    """ComfyUI history 的 outputs 转成前端能直接显示的文件列表。
    每项：{filename, kind, url}，url 走 /api/view 代理 ComfyUI 的 /view。
    文件名先按磁盘实际文件修正（见 _resolve_output_filename）。"""
    files = []
    if not isinstance(outputs, dict):
        return files
    for _node_id, node_out in outputs.items():
        for kind_key in ("images", "videos", "audio", "gifs"):
            for item in (node_out or {}).get(kind_key) or []:
                filename = item.get("filename")
                if not filename:
                    continue
                subfolder = item.get("subfolder") or ""
                ftype = item.get("type") or "output"
                filename = _resolve_output_filename(filename, subfolder)
                kind = _media_kind(filename) or (
                    "image" if kind_key == "images"
                    else "video" if kind_key in ("videos", "gifs")
                    else "audio"
                )
                url = "/api/view?filename=%s&subfolder=%s&type=%s" % (
                    urllib.parse.quote(filename), urllib.parse.quote(subfolder), ftype,
                )
                rel_path = os.path.join(subfolder, filename) if subfolder else filename
                files.append({"filename": filename, "kind": kind, "url": url, "path": rel_path})
    return files


def _compress_video(path):
    """把生成的 mp4 压一遍：H.264 CRF（config.VIDEO_CRF）+ AAC 128k。
    ComfyUI SaveVideo 默认编码码率不受控，同样时长的片子体积能差好几倍，
    压完体积稳定、画质肉眼无损。压完比原文件大（或压失败）就保留原文件。"""
    if not config.COMPRESS_VIDEO or not os.path.isfile(path):
        return False
    tmp = path + ".compressing.mp4"
    try:
        subprocess.run([
            "ffmpeg", "-y", "-v", "error", "-i", path,
            "-c:v", "libx264", "-crf", str(config.VIDEO_CRF), "-preset", "medium",
            "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", tmp,
        ], timeout=300, check=False)
    except Exception:
        return False
    if os.path.isfile(tmp) and 0 < os.path.getsize(tmp) < os.path.getsize(path):
        os.replace(tmp, path)
        return True
    try:
        os.remove(tmp)
    except OSError:
        pass
    return False


def worker():
    while WORKER_RUNNING:
        try:
            job_id = JOB_QUEUE.get(timeout=1)
        except queue.Empty:
            continue
        job = JOBS.get(job_id)
        if not job:
            JOB_QUEUE.task_done()
            continue
        try:
            _run_job(job)
        except Exception as exc:
            # 任何意外异常都不允许杀死 worker 线程：线程一死，
            # 后面所有任务永远卡在排队中，只能重启进程才能恢复。
            try:
                job["status"] = "failed"
                job["finished"] = time.time()
                job["note"] = "平台内部错误：%s" % str(exc)[:200]
            except Exception:
                pass
        finally:
            # 终态（done/failed）都落盘，平台重启后不会把失败任务当排队任务重跑
            try:
                _save_all_jobs()
            except Exception:
                pass
            JOB_QUEUE.task_done()


def _rename_video_files(job):
    """生成的视频按「项目名-镜号-生成时间」改名（name 已是 项目-镜号）。"""
    try:
        params = job.get("params") or {}
        base_name = re.sub(r'[/\\:*?"<>|]', "_", str(params.get("name") or "").strip())
        if not base_name:
            return
        ts = time.strftime("%Y%m%d-%H%M%S")
        idx = 0
        for f in list(job.get("files") or []):
            if not isinstance(f, dict) or f.get("kind") != "video":
                continue
            fn = f.get("path") or f.get("filename") or ""
            if not str(fn).lower().endswith(".mp4"):
                continue
            src = os.path.join(config.COMFYUI_OUTPUT_DIR, fn)
            if not os.path.isfile(src):
                continue
            idx += 1
            suffix = "" if idx == 1 else "-%d" % idx
            sub = os.path.dirname(fn)
            new_fn = "%s-%s%s.mp4" % (base_name, ts, suffix)
            dst = os.path.join(config.COMFYUI_OUTPUT_DIR, sub, new_fn)
            n = 1
            while os.path.exists(dst):
                n += 1
                new_fn = "%s-%s%s-%d.mp4" % (base_name, ts, suffix, n)
                dst = os.path.join(config.COMFYUI_OUTPUT_DIR, sub, new_fn)
            try:
                os.rename(src, dst)
            except OSError:
                continue
            rel = os.path.join(sub, new_fn) if sub else new_fn
            f["filename"] = new_fn
            f["path"] = rel
            f["url"] = "/api/view?filename=%s&subfolder=%s&type=output" % (urllib.parse.quote(new_fn), urllib.parse.quote(sub))
    except Exception:
        pass


def _maybe_queue_upscale(job):
    """视频任务勾了「放大到1080P」时，完成后自动排一条超分任务（现成超分工作流）。"""
    try:
        params = job.get("params") or {}
        if job.get("kind") != "video" or params.get("_is_upscale"):
            return
        extras = params.get("extra") or []
        if isinstance(extras, str):
            extras = [extras]
        if "放大到1080P" not in [str(e).strip() for e in extras]:
            return
        src = None
        for f in list(job.get("files") or []):
            fn = (f.get("path") or f.get("filename")) if isinstance(f, dict) else f
            if fn and str(fn).lower().endswith(".mp4"):
                src = fn
                break
        if not src:
            return
        src_path = os.path.join(config.COMFYUI_OUTPUT_DIR, src)
        if not os.path.isfile(src_path):
            src_path = os.path.join(config.OUTPUT_DIR, src)
        if not os.path.isfile(src_path):
            return
        base = os.path.basename(src)
        os.makedirs(config.COMFYUI_INPUT_DIR, exist_ok=True)
        shutil.copyfile(src_path, os.path.join(config.COMFYUI_INPUT_DIR, base))
        # 目标尺寸：横屏 1920x1080，竖屏 1080x1920（面板路径只有 ratio，没有 resolution，用 ratio 兜底）
        tw, th = 1920, 1080
        m = re.match(r"(\d+)x(\d+)", str(params.get("resolution") or ""))
        if m:
            if int(m.group(1)) < int(m.group(2)):
                tw, th = 1080, 1920
        elif str(params.get("ratio") or "") in ("9:16", "3:4"):
            tw, th = 1080, 1920
        name = str(params.get("name") or job["id"]) + "_1080p"
        up_params = {"project": params.get("project", ""), "shot": params.get("shot", ""),
                     "upscale_video": base, "target_w": tw, "target_h": th,
                     "name": name, "_is_upscale": True}
        jid = new_job("video", config.UPSCALE_WORKFLOW, up_params)
        job["note"] = ((job.get("note") or "") + "；已自动排超分任务").strip("；")
        _save_all_jobs()
        return jid
    except Exception:
        return None


def _run_job(job):
    """执行单个任务；抛出的异常由 worker 统一兜底，不会杀死线程。"""
    # 镜头续接：若依赖前镜，前镜未完成则重新入队等待
    params = job.get("params") or {}
    chain_from = params.get("_chain_from")
    if chain_from:
        prev = JOBS.get(chain_from)
        if prev and prev.get("status") in ("queued", "running"):
            waits = int(params.get("_chain_waits") or 0) + 1
            params["_chain_waits"] = waits
            job["params"] = params
            if waits > 300:  # 等了约 10 分钟前镜还没完，降级按无首帧继续，别空转占 worker
                job["note"] = "等待前镜超时，按无首帧继续"
                params.pop("_chain_from", None)
                params.pop("_chain_waits", None)
                job["params"] = params
                prev = None
            else:
                job["status"] = "queued"
                job["progress"] = 0
                job["note"] = "等待前镜完成并自动续接尾帧"
                time.sleep(2)
                JOB_QUEUE.put(job["id"])
                return
        if not prev or prev.get("status") != "done":
            # 前镜失败或已被删除：不再无限等待，按无首帧继续
            job["note"] = "前镜失败或不存在，按无首帧继续"
            params.pop("_chain_from", None)
            job["params"] = params
            prev = None
        # 前镜已完成，抽取尾帧作为本镜首帧
        src = None
        for f in list((prev or {}).get("files") or []):
            fn = (f.get("path") or f.get("filename")) if isinstance(f, dict) else f
            if fn and str(fn).lower().endswith(".mp4"):
                src = os.path.join(config.COMFYUI_OUTPUT_DIR, fn)
                if not os.path.isfile(src):
                    # 再试 output 目录
                    src = os.path.join(config.OUTPUT_DIR, fn)
                break
        if src and os.path.isfile(src):
            name = re.sub(r'[/\\:*?"<>|]', "_", str(params.get("name") or job["id"]))
            tail_name = name + "_" + str(job["id"]) + "_tail.png"
            out = os.path.join(config.COMFYUI_INPUT_DIR, tail_name)
            if h3.last_frame(src, out):
                params["first_image"] = tail_name
                imgs = [tail_name] + [x for x in (params.get("images") or []) if x != tail_name]
                params["images"] = imgs[:9]
                params["note"] = "已自动续接前镜尾帧"
                params.pop("_chain_from", None)
                job["params"] = params
            else:
                job["note"] = "前镜尾帧抽取失败，按无首帧继续"
                params.pop("_chain_from", None)
                job["params"] = params
        else:
            job["note"] = "前镜没有可用视频，按无首帧继续"
            params.pop("_chain_from", None)
            job["params"] = params

    job["status"] = "running"
    job["started"] = time.time()
    job["finished"] = None
    job["progress"] = 0
    if config.MOCK_MODE:
        # 模拟模式：进度 0→100 走 10 秒，方便看进度条。
        for p in range(0, 101, 10):
            job["progress"] = p
            time.sleep(1)
        job["status"] = "done"
        job["finished"] = time.time()
        job["note"] = "模拟模式，未真生成。部署后关掉 MOCK_MODE 即走真实 ComfyUI。"
        return
    workflow = load_workflow(job["workflow_file"])
    if workflow is None:
        job["status"] = "failed"
        job["finished"] = time.time()
        job["note"] = "缺少 workflow 文件：%s，请从 ComfyUI 导出放进 workflows/" % job["workflow_file"]
        return
    workflow, missing = apply_params(workflow, job["params"])
    if missing:
        job["note"] = "；".join(missing)
    client_id = uuid.uuid4().hex[:12]
    prompt_id, err = submit_prompt(config.COMFYUI_URL, workflow, client_id)
    if not prompt_id:
        job["status"] = "failed"
        job["finished"] = time.time()
        job["note"] = "提交 ComfyUI 失败：%s" % err
        return
    job["prompt_id"] = prompt_id
    # WebSocket 收真实采样进度；失败则轮询队列兜底。
    stop_event = threading.Event()
    progress_holder = {"done": False}

    def _on_progress(value, maximum, node):
        job["progress"] = round(value / maximum * 100) if maximum else 0
        if node:
            job["node"] = str(node)

    ws_thread = threading.Thread(
        target=lambda: progress_holder.__setitem__(
            "done",
            listen_progress(
                config.COMFYUI_URL, client_id, prompt_id,
                _on_progress, stop_event,
            ),
        ),
        daemon=True,
    )
    ws_thread.start()
    done = False
    failed = False
    missing_streak = 0
    # 进度兜底的预期耗时（秒）：WebSocket 没进度时按已用时间估，上限 95%
    expected = {"video": 330, "image": 90, "audio": 60}.get(job.get("kind"), 300)
    for _ in range(600):  # 最多等 20 分钟（600 轮 × 2 秒）
        entry = get_history_entry(config.COMFYUI_URL, prompt_id)
        outputs = (entry or {}).get("outputs")
        status_str = ((entry or {}).get("status") or {}).get("status_str")
        if status_str == "error":
            job["status"] = "failed"
            job["finished"] = time.time()
            job["note"] = "ComfyUI 执行出错：" + (history_error_text(entry) or "未知错误")
            failed = True
            break
        if outputs:
            job["status"] = "done"
            job["finished"] = time.time()
            job["progress"] = 100
            job["outputs"] = outputs
            job["files"] = outputs_to_files(outputs)
            _rename_video_files(job)
            # 视频先压一遍再抽封面：ComfyUI 默认编码体积不受控，压完稳定
            for _f in list(job.get("files") or []):
                if isinstance(_f, dict) and _f.get("kind") == "video":
                    _vp = os.path.join(config.COMFYUI_OUTPUT_DIR, _f.get("path") or _f.get("filename") or "")
                    _compress_video(_vp)
            for _f in list(job.get("files") or []):
                _fn = (_f.get("path") or _f.get("filename")) if isinstance(_f, dict) else _f
                _cov = h3.ensure_cover(_fn, config.COMFYUI_OUTPUT_DIR)
                if _cov:
                    job["files"].append({"kind": "image", "filename": _cov, "path": _cov, "url": "/api/file/" + _cov})
            _save_all_jobs()
            _maybe_queue_upscale(job)
            done = True
            break
        # 兜底进度：队列里的位置 + 已用时间。
        try:
            running, pending = get_queue(config.COMFYUI_URL)
            ids_running = [str(x[1]) for x in running if len(x) > 1]
            ids_pending = [str(x[1]) for x in pending if len(x) > 1]
            if prompt_id in ids_pending:
                job["queue_position"] = ids_pending.index(prompt_id) + 1
                job["progress"] = 0
                missing_streak = 0
            elif prompt_id in ids_running:
                job["queue_position"] = 0
                missing_streak = 0
                # 没收到 WebSocket 进度时，按已用时间缓进估计，上限 95%，
                # 只升不降（WebSocket 有真实进度时以真实进度为准）。
                elapsed = time.time() - (job.get("started") or time.time())
                est = min(95, 5 + int(elapsed / expected * 90))
                if est > job.get("progress", 0):
                    job["progress"] = est
            elif entry is None:
                # 既不在队列、history 里也没有：任务可能被清掉或 ComfyUI 重启过
                missing_streak += 1
                if missing_streak >= 15 and time.time() - (job.get("started") or 0) > 30:
                    job["status"] = "failed"
                    job["finished"] = time.time()
                    job["note"] = "ComfyUI 队列里找不到该任务（可能被清掉或 ComfyUI 重启过），请重试。"
                    failed = True
                    break
        except Exception:
            pass
        time.sleep(2)
    stop_event.set()
    if not done and not failed:
        # 超时判死前再查一次：ComfyUI 可能刚跑完，别把已完成的标失败让用户重复花钱
        entry = get_history_entry(config.COMFYUI_URL, prompt_id)
        outputs = (entry or {}).get("outputs")
        if outputs:
            job["status"] = "done"
            job["finished"] = time.time()
            job["progress"] = 100
            job["outputs"] = outputs
            job["files"] = outputs_to_files(outputs)
            _rename_video_files(job)
            _save_all_jobs()
            _maybe_queue_upscale(job)
        else:
            job["status"] = "failed"
            job["finished"] = time.time()
            job["note"] = "等待超时，任务可能仍在 ComfyUI 里生成，先去资产页看看再决定是否重跑。"


threading.Thread(target=worker, daemon=True).start()

# 恢复的已完成任务：重建文件列表（改名+超分补跑），函数都定义完了再做
for _j in list(JOBS.values()):
    if _j.get("_restored_done"):
        _j.pop("_restored_done", None)
        try:
            _j["files"] = outputs_to_files(_j.get("outputs") or {})
            _rename_video_files(_j)
            _maybe_queue_upscale(_j)
        except Exception:
            pass
_save_all_jobs()


def new_job(kind, workflow_file, params):
    job_id = uuid.uuid4().hex[:12]
    params = h3.enrich_params(params, kind)
    with JOBS_LOCK:
        JOBS[job_id] = {
            "id": job_id, "kind": kind, "workflow_file": workflow_file,
            "params": params, "status": "queued", "created": time.time(),
            "progress": 0, "queue_position": None,
        }
    JOB_QUEUE.put(job_id)
    _save_all_jobs()
    return job_id


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/gpu")
def gpu():
    """当前实例 GPU / 显存用量：nvidia-smi 直读，没有显卡就返回 ok=false。"""
    try:
        out = subprocess.check_output(
            ["nvidia-smi", "--query-gpu=name,utilization.gpu,memory.used,memory.total,temperature.gpu,power.draw",
             "--format=csv,noheader,nounits"],
            timeout=10, text=True,
        ).strip().splitlines()
        def _f(x):
            try:
                return float(x)
            except Exception:
                return 0.0  # 部分卡 power.draw 是 [N/A]，别让整个接口挂掉
        gpus = []
        for line in out:
            parts = [p.strip() for p in line.split(",")]
            if len(parts) < 6:
                continue
            used, total = _f(parts[2]), _f(parts[3])
            gpus.append({
                "name": parts[0],
                "util": _f(parts[1]),
                "mem_used": used, "mem_total": total,
                "mem_used_gb": round(used / 1024, 1),
                "mem_total_gb": round(total / 1024, 1),
                "mem_pct": round(used / total * 100) if total else 0,
                "temp": _f(parts[4]), "power": _f(parts[5]),
            })
        return jsonify({"ok": bool(gpus), "gpus": gpus})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)[:200], "gpus": []})


@app.route("/api/health")
def health():
    ok, info = check_health(config.COMFYUI_URL)
    running, pending = ([], []) if not ok else get_queue(config.COMFYUI_URL)
    return jsonify({
        "version": getattr(config, "VERSION", "?"),
        "mock": config.MOCK_MODE,
        "comfyui_url": config.COMFYUI_URL,
        "comfyui_online": ok,
        "queue": JOB_QUEUE.qsize(),
        "comfyui_running": len(running),
        "comfyui_pending": len(pending), "disk_free_gb": h3.disk_free_gb(config.OUTPUT_DIR),
    })


@app.route("/api/upload", methods=["POST"])
def upload():
    """上传参考文件：图片 / 视频 / 音频，直接存进 ComfyUI input 目录。
    前端按 kind 传 image/video/audio，后端校验后缀和数量上限。"""
    kind = request.form.get("kind", "image")
    limit = {"image": config.REF_LIMITS["images"],
             "video": config.REF_LIMITS["videos"],
             "audio": config.REF_LIMITS["audios"]}.get(kind, 9)
    files = request.files.getlist("file")
    if not files:
        return jsonify({"error": "没有收到文件"}), 400
    if len(files) > limit:
        return jsonify({"error": "一次最多上传 %d 个%s文件" % (limit, kind)}), 400
    saved = []
    for f in files:
        filename = os.path.basename(f.filename or "")
        if not filename:
            continue
        ext = os.path.splitext(filename)[1].lower()
        if ext not in ALLOWED_EXTS.get(kind, ALL_EXTS):
            return jsonify({"error": "不支持的文件类型：%s" % filename}), 400
        # 同名文件加 uuid 短串后缀，避免覆盖。
        dest = os.path.join(config.COMFYUI_INPUT_DIR, filename)
        if os.path.exists(dest):
            stem, _ext = os.path.splitext(filename)
            filename = "%s_%s%s" % (stem, uuid.uuid4().hex[:8], _ext)
            dest = os.path.join(config.COMFYUI_INPUT_DIR, filename)
        f.save(dest)
        saved.append({"filename": filename, "kind": kind,
                      "size": os.path.getsize(dest),
                      "url": "/api/file_input/%s" % filename})
    return jsonify({"files": saved, "limit": limit})
@app.route("/api/file_input/<path:filename>")
def file_input(filename):
    """预览 input 里的上传文件（防目录穿越）。"""
    base = os.path.realpath(config.COMFYUI_INPUT_DIR)
    path = os.path.realpath(os.path.join(base, filename))
    if not _inside(base, path) or not os.path.isfile(path):
        return jsonify({"error": "文件不存在"}), 404
    return send_file(path)


@app.route("/api/view")
def view():
    """代理 ComfyUI 的 /view，让前端直接显示生成结果。
    ComfyUI 找不到文件时（比如 history 文件名和磁盘差一个下划线），
    兜底直接读 ComfyUI output 目录里的实际文件。"""
    import urllib.parse
    import urllib.request
    filename = request.args.get("filename", "")
    subfolder = request.args.get("subfolder", "")
    ftype = request.args.get("type", "output")
    filename = _resolve_output_filename(filename, subfolder)
    url = "%s/view?filename=%s&subfolder=%s&type=%s" % (
        config.COMFYUI_URL.rstrip("/"), urllib.parse.quote(filename), urllib.parse.quote(subfolder), ftype,
    )
    try:
        with urllib.request.urlopen(url, timeout=30) as resp:
            data = resp.read()
            mime = resp.headers.get("Content-Type") or mimetypes.guess_type(filename)[0] or "application/octet-stream"
            return data, 200, {"Content-Type": mime}
    except Exception:
        pass
    base = os.path.realpath(config.COMFYUI_OUTPUT_DIR)
    path = os.path.realpath(os.path.join(base, subfolder, filename))
    if _inside(base, path) and os.path.isfile(path):
        return send_file(path)
    return jsonify({"error": "文件不存在：%s" % filename}), 404


@app.route("/api/image", methods=["POST"])
def image():
    p = request.get_json(force=True) or {}
    params = {
        "mode": p.get("mode", "文生图"),
        "prompt": p.get("prompt", ""),
        "project": p.get("project", ""),
        "shot": p.get("shot", ""),
        "character_id": p.get("character_id", ""),
        "negative": p.get("negative", ""),
        "ratio": p.get("ratio", "9:16"),
        "resolution": p.get("resolution", "1536×864"),
        "count": _safe_int(p.get("count", 1), 1),
        "style": p.get("style", "漫画"),
        # CFG：前端明确传了才用前端的；没传用配置默认值 1。
        # 不能落到工作流模板值 4.0，Qwen-Image 2.1 用 CFG 4 画面会过饱和。
        "cfg": p.get("cfg") if str(p.get("cfg") or "").strip() != "" else config.LOCKED["image_cfg_default"],
        "seed": p.get("seed", "随机"),
        # 重绘强度 denoise（图生图改动原图的程度，0~1，默认 0.75），页面滑杆可调
        "denoise": p.get("denoise"),
        "extra": p.get("extra", []),
        "images": (p.get("images") or [])[:1],  # 图生图 1 张参考图
    }
    return jsonify({"job_id": new_job("image", config.IMAGE_WORKFLOW, params)})


@app.route("/api/video", methods=["POST"])
def video():
    p = request.get_json(force=True) or {}
    # MiniMax H3：步数锁死 8 步 Turbo，用户改不了。
    # 文生视频保留；图生 / 参考生 / 首尾帧另外支持导入图片/视频/音频。
    mode = p.get("mode", "参考生视频")
    params = {
        "mode": mode,
        "prompt": p.get("prompt", ""),
        "project": p.get("project", ""),
        "shot": p.get("shot", ""),
        "character_id": p.get("character_id", ""),
        "ratio": p.get("ratio", "9:16"),
        "duration": p.get("duration", "5秒"),
        "steps": config.LOCKED["video_steps"],
        "ref_strength": _safe_float(p.get("ref_strength", 1.0), 1.0),
        "camera": p.get("camera", "推镜头"),
        "extra": p.get("extra", []),
        # 图生视频：1 张首帧；首尾帧：首 + 尾 2 张；参考生：最多 9 图 + 3 视频 + 3 音频。
        "first_image": p.get("first_image") or (p.get("images") or [None])[0],
        "last_image": p.get("last_image"),
        "images": (p.get("images") or [])[:config.REF_LIMITS["images"]],
        "videos": (p.get("videos") or [])[:config.REF_LIMITS["videos"]],
        "audios": (p.get("audios") or [])[:config.REF_LIMITS["audios"]],
    }
    return jsonify({"job_id": new_job("video", config.VIDEO_WORKFLOW, params)})


@app.route("/api/audio", methods=["POST"])
def audio():
    p = request.get_json(force=True) or {}
    params = {
        "voice_mode": p.get("voice_mode", "设计音色"),
        "project": p.get("project", ""),
        "shot": p.get("shot", ""),
        "character_id": p.get("character_id", ""),
        "preset": p.get("preset", "男声"),
        "voice_desc": p.get("voice_desc", ""),
        "text": p.get("text", ""),
        "emotion": p.get("emotion", "平静"),
        "seed": p.get("seed", "随机"),
        "ref_audio": p.get("ref_audio"),
        "ref_text": p.get("ref_text", ""),
    }
    wf = config.AUDIO_CLONE_WORKFLOW if params["voice_mode"] == "克隆音色" else config.AUDIO_WORKFLOW
    return jsonify({"job_id": new_job("audio", wf, params)})


@app.route("/api/jobs")
def jobs():
    with JOBS_LOCK:
        items = sorted(JOBS.values(), key=lambda j: j.get("created") or 0, reverse=True)[:30]
        # 深拷贝再出锁：worker 会原地改 params/files，浅拷贝序列化时可能竞态 500
        # 遇到 RuntimeError（字典在序列化过程中变化）重试一次
        for attempt in range(2):
            try:
                items = json.loads(json.dumps(items, ensure_ascii=False, default=str))
                break
            except RuntimeError:
                if attempt == 1:
                    raise
    for j in items:
        j.pop("outputs", None)
    for j in items:
        # 已用时间：跑完的任务用 finished 停表，不再一直涨；还在跑的才按当前时间算
        j["elapsed"] = round((j.get("finished") or time.time()) - (j.get("started") or j["created"]))
    # 不在这里写盘：前端每 2 秒轮询一次，写盘只在任务状态变化时做（worker / new_job）
    return jsonify({"jobs": items})
@app.route("/api/assets")
def assets():
    """资产管理：扫 ComfyUI output 目录，和 ComfyUI 同步。
    返回按时间倒序的文件列表，含类型、大小、预览链接。"""
    base = os.path.realpath(config.COMFYUI_OUTPUT_DIR)
    items = []
    for root, _dirs, files in os.walk(base):
        for name in files:
            kind = _media_kind(name)
            if not kind:
                continue
            path = os.path.join(root, name)
            rel = os.path.relpath(path, base)
            try:
                items.append({
                    "path": rel, "filename": name, "kind": kind,
                    "size": os.path.getsize(path),
                    "mtime": os.path.getmtime(path),
                    "url": "/api/file/%s" % rel,
                })
            except OSError:
                continue  # 文件正被删除/写入时跳过，别让整个列表 500
    items.sort(key=lambda x: x["mtime"], reverse=True)
    return jsonify({"assets": items[:200], "total": len(items)})


@app.route("/api/file/<path:rel>")
def serve_asset(rel):
    base = os.path.realpath(config.COMFYUI_OUTPUT_DIR)
    path = os.path.realpath(os.path.join(base, rel))
    if not _inside(base, path) or not os.path.isfile(path):
        return jsonify({"error": "文件不存在"}), 404
    return send_file(path)


@app.route("/api/assets/<path:rel>", methods=["DELETE"])
def delete_asset(rel):
    base = os.path.realpath(config.COMFYUI_OUTPUT_DIR)
    path = os.path.realpath(os.path.join(base, rel))
    if not _inside(base, path) or not os.path.isfile(path):
        return jsonify({"error": "文件不存在"}), 404
    os.remove(path)
    return jsonify({"ok": True, "deleted": rel})


# H3 补齐接口
try:
    h3_routes.register(app, {"config": config, "h3": h3, "JOBS": JOBS, "JOBS_LOCK": JOBS_LOCK, "JOB_QUEUE": JOB_QUEUE, "new_job": new_job})
except Exception as _e:
    print("h3_routes register failed:", _e)

# 剧本拆分接口（可配置 LLM + 拆分 + 资产生成）
try:
    script_routes.register(app, {"config": config, "h3": h3, "JOBS": JOBS, "JOBS_LOCK": JOBS_LOCK, "JOB_QUEUE": JOB_QUEUE, "new_job": new_job})
except Exception as _e:
    print("script_routes register failed:", _e)

# 平台自更新接口（页面上传更新包 → 备份 → 覆盖 → 自动重启/回滚）
try:
    updater.register(app, {"config": config, "JOBS": JOBS, "JOBS_LOCK": JOBS_LOCK})
except Exception as _e:
    print("updater register failed:", _e)

if __name__ == "__main__":
    app.run(host=config.HOST, port=config.PORT)
