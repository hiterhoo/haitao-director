# 可视化平台配置
# 部署后只需要改这里，代码不用动。
# 部署前在本地是测试值，部署到仙宫云实例后按实际情况改。
import os

BASE = os.path.dirname(os.path.abspath(__file__))

# 平台版本号：从 1.0.0 开始，每次加新功能或修 bug 就把末位 +1（1.0.1、1.0.2…）。
# 页面左下角和 /api/health 会显示，自更新靠它比大小判断有没有新版。
VERSION = "1.0.2"

# 远程更新清单地址（拉取式自动更新）：填了之后，实例会定期去这个地址
# 拉一个 JSON 清单 {"version": 数字, "package_url": "更新包直链", "sha256": "包的sha256(可选)", "notes": "更新说明(可选)"}，
# 发现版本号比本地大就自动下载、校验、更新（备份+重启+失败回滚，和手动上传更新同一套）。
# 留空 = 不启用远程更新，只能在页面上手动上传更新包。也可以用环境变量 UPDATE_MANIFEST_URL 覆盖。
UPDATE_MANIFEST_URL = os.environ.get("UPDATE_MANIFEST_URL", "https://raw.githubusercontent.com/hiterhoo/haitao-director/main/version.json")


def _env_int(name, default):
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return int(default)


def _env_bool(name, default="false"):
    return os.environ.get(name, default).strip().lower() in ("true", "1", "yes", "on")


# 是否全自动更新（发现新版且队列空闲时自动装）。关掉后只在页面上提示，由用户点「一键更新」。
AUTO_UPDATE = _env_bool("AUTO_UPDATE", "true")
# 自动检查间隔（小时）
UPDATE_CHECK_INTERVAL_H = _env_int("UPDATE_CHECK_INTERVAL_H", "6")

# 平台自身监听的端口。仙宫云 OS 桌面快捷方式的 info.yaml 里 port 要写同一个数。
# 80 需要 root，实例里默认用 8080。
PORT = _env_int("VISUAL_PORT", "8080")
HOST = os.environ.get("VISUAL_HOST", "0.0.0.0")

# ComfyUI 地址。实例本机上就是 127.0.0.1:8188，一般不用改。
# 如果 ComfyUI 换了端口，只改这一行。
COMFYUI_URL = os.environ.get("COMFYUI_URL", "http://127.0.0.1:8188")

# 没有 ComfyUI 时（本地测试、或实例还没装好）用模拟模式：
# 任务只进队列、不真生成，方便先点一点界面。
# 默认关（走真实 ComfyUI）；本地测试时设 MOCK_MODE=true（1/yes/on 也认）。
MOCK_MODE = _env_bool("MOCK_MODE")

# ComfyUI 的输入 / 输出目录。平台上传的文件直接存进 input，
# 资产管理直接读 output，和 ComfyUI 完全同步（同一份磁盘）。
# 实例上是 /root/ComfyUI/input 和 /root/ComfyUI/output；
# 本地测试时没有这两个目录就用平台目录下的 input/ 和 output/（绝对路径，不依赖启动目录）。
COMFYUI_INPUT_DIR = os.environ.get(
    "COMFYUI_INPUT_DIR",
    "/root/ComfyUI/input" if os.path.isdir("/root/ComfyUI/input") else os.path.join(BASE, "input"),
)
COMFYUI_OUTPUT_DIR = os.environ.get(
    "COMFYUI_OUTPUT_DIR",
    "/root/ComfyUI/output" if os.path.isdir("/root/ComfyUI/output") else os.path.join(BASE, "output"),
)

# 生成结果保存目录（保留兼容，实际资产以 COMFYUI_OUTPUT_DIR 为准）。
OUTPUT_DIR = os.environ.get("OUTPUT_DIR", COMFYUI_OUTPUT_DIR)

# 工作流模板目录。部署后把从 ComfyUI 导出的 API 格式 workflow JSON
# 放进来，文件名对上下面几项，平台就会用真实工作流提交。
WORKFLOW_DIR = os.environ.get("WORKFLOW_DIR", os.path.join(BASE, "workflows"))
IMAGE_WORKFLOW = "qwen_image.json"
VIDEO_WORKFLOW = "minimax_h3.json"
UPSCALE_WORKFLOW = "minimax_h3_upscale.json"
AUDIO_WORKFLOW = "qwen3_tts.json"
AUDIO_CLONE_WORKFLOW = "qwen3_tts_clone.json"

# 锁死的参数，用户在页面上改不了。
LOCKED = {
    "video_steps": 8,          # MiniMax H3 8 步 Turbo
    "image_cfg_default": 1,    # Qwen-Image 2.1 CFG=1
}

# 生成视频后自动压缩一遍（H.264 CRF）：ComfyUI 的 SaveVideo 默认编码
# 码率不受控，同样 5 秒的片子体积能从 2MB 到 16MB 差好几倍。
# 默认关（保留 ComfyUI 原文件、原画质）；想压小体积时设 COMPRESS_VIDEO=true。
# 压完比原文件大（或压失败）就保留原文件。
COMPRESS_VIDEO = _env_bool("COMPRESS_VIDEO")
VIDEO_CRF = _env_int("VIDEO_CRF", "23")

# 视频参考上限：MiniMax H3 官方上限。
REF_LIMITS = {
    "images": 9,   # 参考图最多 9 张
    "videos": 3,   # 参考视频最多 3 条
    "audios": 3,   # 参考音频最多 3 条
}
