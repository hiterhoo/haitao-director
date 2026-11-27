# H3 补充功能：持久化、角色库、封面、断点续跑、打包
import copy
import json
import os
import random
import shutil
import subprocess
import zipfile

BASE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE, "data")
os.makedirs(DATA_DIR, exist_ok=True)


def _path(name):
    return os.path.join(DATA_DIR, name)


def load_json(name, default):
    p = _path(name)
    try:
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default
    except Exception:
        # 文件存在但损坏：将坏文件重命名移走，避免下次再次读取同一文件
        corrupt_name = p + ".corrupt." + os.urandom(4).hex()
        try:
            os.rename(p, corrupt_name)
        except Exception:
            # 重命名失败（如跨设备、权限不足），尝试删除原文件，防止下次再次进入此分支
            try:
                os.remove(p)
            except Exception:
                pass
        return default
def save_json(name, data):
    tmp = _path(name) + ".tmp." + os.urandom(6).hex()
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, _path(name))
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


def get_characters():
    return load_json("characters.json", [])


def save_characters(chars):
    save_json("characters.json", chars)


def load_jobs():
    return load_json("jobs.json", {})


def save_jobs(jobs):
    save_json("jobs.json", jobs)


def clean_job(j):
    keep = ["id", "kind", "workflow_file", "params", "status", "created", "started", "finished", "progress", "queue_position", "note", "files", "prompt_id"]
    # 深拷贝：worker 会原地改 params/files，浅引用落盘序列化时会竞态
    return copy.deepcopy({k: j.get(k) for k in keep if k in j})


def ensure_cover(filename, output_dir):
    if not filename:
        return None
    low = filename.lower()
    if low.endswith((".png", ".jpg", ".jpeg", ".webp")):
        return None  # 图片本身就是图，不需要封面（返回原名会导致 files 重复）
    if not low.endswith(".mp4"):
        return None
    base, _ext = os.path.splitext(filename)
    cover = base + "_cover.jpg"
    src = os.path.join(output_dir, filename)
    dst = os.path.join(output_dir, cover)
    if os.path.isfile(dst):
        return cover
    if not os.path.isfile(src):
        return None
    try:
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", "1", "-i", src, "-frames:v", "1", "-q:v", "4", dst], timeout=60, check=False)
    except Exception:
        return None
    return cover if os.path.isfile(dst) else None


def last_frame(video_path, out_path):
    try:
        r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", video_path], capture_output=True, text=True, timeout=30)
        dur = float(r.stdout.strip())
        ss = max(dur - 0.2, 0)
    except Exception:
        return False  # 探测失败就别拿首帧冒充尾帧，上层会按无首帧继续
    try:
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", str(ss), "-i", video_path, "-frames:v", "1", "-q:v", "3", out_path], timeout=60, check=False)
    except Exception:
        return False
    return os.path.isfile(out_path)


def resolve_seed(params):
    seed = params.get("seed")
    try:
        if seed is None or seed == "" or str(seed) == "随机":
            raise ValueError("random")
        s = max(0, int(seed))
        params["seed"] = s
        return s
    except Exception:
        s = random.randint(0, 2 ** 31 - 1)
        params["seed"] = s
        return s


# 图片风格下拉 → 拼进提示词的固定描述
# 「根据提示词自动生成」不在此表中，表示不加任何固定风格描述，完全按用户提示词走
STYLE_MAP = {
    "漫画": "漫画风格，清晰线条，鲜明色彩，二次元构图",
    "写实": "写实摄影风格，自然光影，真实皮肤质感，电影感",
    "日漫": "日式动漫风格，精致人物，柔和色调，动画构图",
    "水彩": "水彩画风格，柔和笔触，通透色彩，艺术感",
}

# 运镜下拉 → 自然中文描述，拼进视频提示词
CAMERA_MAP = {
    "推镜头": "镜头缓慢向前推进",
    "拉远": "镜头缓慢向后拉远",
    "左平移": "镜头向左平移",
    "右平移": "镜头向右平移",
    "升降": "镜头缓慢升降",
}

def enrich_params(params, kind):
    params = dict(params or {})
    # 已经 enrich 过的 params（"再来一条" / 尾帧续接会把旧 params 原样传回来）
    # 不再重复拼风格/运镜，避免点几次叠几层。
    already = bool(params.get("_enriched"))
    project = str(params.get("project") or "").strip()
    shot = str(params.get("shot") or "").strip()
    char = None
    cid = params.get("character_id")
    if cid:
        for c in get_characters():
            if str(c.get("id")) == str(cid):
                char = c
                break

    if char:
        params["character_name"] = char.get("name", "")
        if kind == "image" and char.get("image"):
            imgs = [char["image"]] + [x for x in (params.get("images") or []) if x != char["image"]]
            params["images"] = imgs[:9]
        if kind == "video" and char.get("image") and not params.get("first_image"):
            params["first_image"] = char["image"]
        if kind == "video" and char.get("audio"):
            auds = [char["audio"]] + [x for x in (params.get("audios") or []) if x != char["audio"]]
            params["audios"] = auds[:3]
        if kind == "audio":
            # 只在用户没填时才用角色的，别覆盖面板手填的自定义音色
            if char.get("voice_desc") and not str(params.get("voice_desc") or "").strip():
                params["voice_desc"] = char["voice_desc"]
            if char.get("audio") and not params.get("ref_audio"):
                params["ref_audio"] = char["audio"]
    # 图片风格下拉（漫画/写实/日漫/水彩）：选了就拼在提示词前面
    ui_style = str(params.get("style") or "").strip()
    if ui_style and ui_style in STYLE_MAP and not already and params.get("prompt"):
        params["prompt"] = STYLE_MAP[ui_style] + "，" + params["prompt"]

    # 视频运镜真正生效
    camera = str(params.get("camera") or "").strip()
    if camera and camera in CAMERA_MAP and not already:
        cam_desc = CAMERA_MAP[camera]
        prompt = str(params.get("prompt") or "")
        if cam_desc and cam_desc not in prompt:
            params["prompt"] = (prompt + "，" + cam_desc).strip("，") if prompt else cam_desc

    if project and not str(params.get("name") or "").strip():
        # 已有 name 的不覆盖（超分任务的 _1080p 后缀靠这个保住）
        if shot:
            try:
                n = int(shot)
                shot_s = "%02d" % n
            except Exception:
                shot_s = shot
            params["name"] = project + "-" + shot_s
        else:
            params["name"] = project
    if kind == "audio":
        # 音频没给种子时不要随机化：留空让 apply_params 用预设音色的固定种子，
        # 保证同一预设每次同一个声音
        _sd = params.get("seed")
        if _sd is None or _sd == "" or str(_sd) == "随机":
            params.pop("seed", None)
        else:
            resolve_seed(params)
    else:
        resolve_seed(params)
    params["_enriched"] = True
    return params


def disk_free_gb(path):
    try:
        u = shutil.disk_usage(path)
        return round(u.free / (1024 ** 3), 2)
    except Exception:
        return None


def make_zip(project, jobs, output_dir, zip_path):
    names = []
    output_dir_real = os.path.realpath(output_dir)
    for j in jobs:
        for f in (j.get("files") or []):
            if not isinstance(f, dict):
                continue
            fn = f.get("path") or f.get("filename")
            if not fn:
                continue
            # 防止路径穿越：只允许 output_dir 内的文件
            src = os.path.join(output_dir, fn)
            try:
                src_real = os.path.realpath(src)
            except Exception:
                continue
            if not (src_real == output_dir_real or src_real.startswith(output_dir_real + os.sep)):
                continue
            if not os.path.isfile(src_real):
                continue
            if fn not in names:
                names.append(fn)
    if not names:
        return None
    written = 0
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        for fn in names:
            src = os.path.join(output_dir, fn)
            # 再次确认安全（虽然第一次已检查，但文件系统可能变化）
            try:
                src_real = os.path.realpath(src)
            except Exception:
                continue
            if not (src_real == output_dir_real or src_real.startswith(output_dir_real + os.sep)):
                continue
            if os.path.isfile(src_real):
                z.write(src_real, os.path.basename(fn))
                written += 1
    if not written:
        try:
            os.remove(zip_path)
        except OSError:
            pass
        return None
    return zip_path