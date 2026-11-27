#!/bin/bash
# 开机自启：ComfyUI + 可视化平台
# /scripts/start.sh 开机时自动执行 /scripts/start.d/ 下的所有可执行脚本。
# 下次用本镜像开新实例，ComfyUI（8188）和可视化平台（8080）会自动起来。
# 日志：/root/comfy_run_auto.log、/root/visual-platform/visual-platform.log

# 1. 启动 ComfyUI（若未运行）
if ! curl -fs -m 3 http://127.0.0.1:8188/ >/dev/null 2>&1; then
  nohup /root/miniconda3/bin/python /root/ComfyUI/main.py --listen 0.0.0.0 --port 8188 > /root/comfy_run_auto.log 2>&1 &
fi

# 2. 等 ComfyUI 就绪（最多 5 分钟）
for _ in $(seq 1 60); do
  curl -fs -m 3 http://127.0.0.1:8188/ >/dev/null 2>&1 && break
  sleep 5
done

# 3. 启动可视化平台（若未运行）
if ! curl -fs -m 3 http://127.0.0.1:8080/api/health >/dev/null 2>&1; then
  cd /root/visual-platform || exit 1
  nohup /root/miniconda3/bin/python3 app.py > /root/visual-platform/visual-platform.log 2>&1 &
fi
