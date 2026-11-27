# 海涛AI导演台（工作台源码）

一个跑在云 GPU 实例上的短剧生产工作台：生图、生视频、配音、资产管理、剧本拆分，全部在一个网页里完成。后端是 Flask，生成走本机 ComfyUI。

## 功能

- **生图**：文生图 / 图生图（重绘强度可调），Qwen-Image
- **视频**：文生 / 图生 / 参考生 / 首尾帧，MiniMax H3（8 步 Turbo）
- **音频**：设计音色（男声/女声/自定义描述/情绪）+ 克隆音色（上传参考音频+文字稿），Qwen3-TTS
- **风格与角色**：按项目保存风格块和负面提示词；角色资产库（定妆图/参考音频/绑定风格块），提交时自动带入
- **分镜表批量导入**：整表粘贴自动拆成一队任务挨个跑
- **剧本拆分**：上传整本剧本，AI 自动拆成一集一集，提取每集的角色和场景，角色/场景可一键生成资产（定妆图/场景图）
- **资产管理**：和 ComfyUI output 目录同步，打包下载、清空资产、清空队列、断点续跑

## 部署要求

- 一台装好 ComfyUI 的 GPU 机器（本工作台按 MiniMax H3 + Qwen-Image + Qwen3-TTS 的工作流写的，工作流模板在 `workflows/` 目录）
- Python 3.10+，`pip install -r requirements.txt`（只有 Flask 和 websocket-client）

## 启动

```bash
cd visual-platform
pip install -r requirements.txt
python3 app.py        # 默认 8080 端口，浏览器打开 http://<机器IP>:8080
```

配置都在 `config.py`（端口、ComfyUI 地址、输入输出目录、模拟模式开关），一般不用改。没有 ComfyUI 时可以设环境变量 `MOCK_MODE=true` 先看界面（任务只进队列不真生成）。

## 剧本拆分的 AI 接口

「剧本」页签里填你自己的 API：选服务商预设（DeepSeek / OpenAI / 腾讯混元 / 阿里通义 / Moonshot / 智谱）或自定义，填 **API 地址 + API Key + 模型名**，点保存、测试连接即可。接口走 OpenAI 兼容的 `/chat/completions` 协议，绝大多数服务商都能用。Key 只存在本机 `data/llm_config.json`，不进代码。

## 平台自更新

页面最下方有「平台更新」卡：选新版源码包（.tar.gz，里面是 `visual-platform/` 目录）+ 填更新口令，点「上传并更新」即可，不用重新部署。更新会自动备份旧代码（在平台目录旁边的 `visual-platform-bak-*`，只留最近 3 个）、覆盖新代码、重启平台；新版起不来会自动回滚旧版。`data/`（任务、剧本、Key、角色库）更新时不覆盖。更新口令在 `data/update_token.txt`（首次启动自动生成，也可用环境变量 `UPDATE_TOKEN` 指定）；更新日志在 `data/update.log`。版本号在 `config.py` 的 `VERSION`（纯数字，每次发新版 +1），页面左下角和右上角会显示。

### 远程自动更新（发一次，所有人自动跟上）

在 `config.py` 里填 `UPDATE_MANIFEST_URL`（或环境变量同名），指向一个 JSON 清单地址，实例会每 6 小时（`UPDATE_CHECK_INTERVAL_H` 可改）自动检查：发现版本号比本地大、且队列空闲时，自动下载、校验、更新（`AUTO_UPDATE=false` 可关掉自动装，改为页面点「检查远程更新」→「一键更新到新版」手动确认）。清单格式：

```json
{"version": 2, "package_url": "https://…/visual-platform-v2.tar.gz", "sha256": "更新包的sha256（建议填，会校验）", "notes": "这次改了什么（可选，页面会显示）"}
```

发布新版时：把 `config.py` 的 `VERSION` +1、打好 tar.gz 上传到清单指向的位置、把清单的 version 和 sha256 改掉即可，所有配了这个地址的实例会自动跟上，不用逐个通知。

## 注意

- `data/` 目录里有你的 API Key、角色库、拆分结果等数据，**分享源码时不要带上 data/**。
- 生成参数（分辨率、种子、CFG 等）由页面写进工作流；视频步数锁死 8 步，图片 CFG 默认 1。
