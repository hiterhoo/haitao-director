#!/bin/bash
# 自更新重启脚本：由 updater.py 在更新包覆盖完成后以脱离进程方式调用。
# 用法: update_restart.sh <平台目录> <端口> <python路径> <备份目录>
# 流程：杀旧进程 → 起新版 → 轮询 /api/health 最多 30 秒 → 失败则回滚备份再起。
# 日志写到 <平台目录>/data/update.log（data/ 更新时不覆盖，日志能留住）。
BASE="$1"; PORT="$2"; PY="$3"; BAK="$4"
LOG="$BASE/data/update.log"
mkdir -p "$BASE/data"

log() { echo "[$(date '+%F %T')] $*" >> "$LOG"; }

kill_port() {
  if command -v fuser >/dev/null 2>&1; then
    fuser -k "$PORT/tcp" >>"$LOG" 2>&1
  elif command -v lsof >/dev/null 2>&1; then
    kill $(lsof -t -i:"$PORT") >>"$LOG" 2>&1
  fi
}

start_app() {
  cd "$BASE" || exit 1
  nohup "$PY" app.py >> "$BASE/visual-platform.log" 2>&1 &
}

wait_health() {
  for _ in $(seq 1 30); do
    if curl -fs -m 2 "http://127.0.0.1:$PORT/api/health" >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done
  return 1
}

log "开始重启（新版），备份在 $BAK"
sleep 2
kill_port
sleep 1
start_app
if wait_health; then
  log "新版健康检查通过，更新完成"
  # 只保留最近 3 个备份
  ls -dt "$(dirname "$BASE")"/visual-platform-bak-* 2>/dev/null | tail -n +4 | xargs -r rm -rf
  exit 0
fi

log "新版健康检查失败，回滚到 $BAK"
kill_port
sleep 1
for e in "$BASE"/*; do
  n=$(basename "$e")
  case "$n" in data|input|output|__pycache__|*.log) continue ;; esac
  rm -rf "$e"
done
cp -a "$BAK"/. "$BASE"/ 2>>"$LOG"
start_app
if wait_health; then
  log "已回滚旧版并重启成功"
else
  log "回滚后仍起不来，请看 visual-platform.log 手动处理"
fi
