#!/bin/bash
# 在仙宫云实例里部署可视化平台。部署后只改 config.py，不用改代码。
# 用法（在实例终端里，进入源码目录后执行）：
#   bash deploy.sh
set -e
cd "$(dirname "$0")"

# 1. 装依赖
pip install -r requirements.txt

# 2. 读配置里的端口（默认 8080）
PORT=$(python3 -c "import config; print(config.PORT)")

# 3. 后台启动平台，日志写到 visual-platform.log
nohup python3 app.py > visual-platform.log 2>&1 &
echo "可视化平台已启动，端口 $PORT，本机访问 http://127.0.0.1:$PORT"
echo "公网访问：https://<实例ID>-$PORT.container.x-gpu.com"
echo "日志：tail -f visual-platform.log"

# 4. 做仙宫云 OS 桌面快捷方式（.app），实例管理页就会出现入口
#    官方做法：桌面快捷方式在 /.xgcos/desktop/，info.yaml 写名字和端口。
DESKTOP=/.xgcos/desktop
if [ -d "$DESKTOP" ]; then
  mkdir -p "$DESKTOP/海影-AI漫剧工作台.app"
  cp shortcut/info.yaml "$DESKTOP/海影-AI漫剧工作台.app/info.yaml"
  # 图标：先复制占位图，部署后换成 512x512 的 web.png 即可
  cp shortcut/web.png "$DESKTOP/海影-AI漫剧工作台.app/web.png" 2>/dev/null || true
  echo "桌面快捷方式已建好：$DESKTOP/海影-AI漫剧工作台.app，桌面右键刷新就能看到"
  echo "注意：info.yaml 里的 port 必须和 config.py 的 PORT 一致（当前 $PORT）"
else
  echo "没找到 $DESKTOP（不是仙宫云 OS 桌面环境），跳过快捷方式，只启动了平台"
  echo "快捷方式文件在 shortcut/ 里，部署后手动复制过去也行"
fi

# 5. 存镜像前确认：平台已启动、快捷方式已建好，再在控制台把实例存成镜像
echo "下一步：在控制台把实例存成镜像，部署这个镜像就自带可视化平台入口。"
