#!/bin/bash
# 代理池 → 本地 HTTP 转发（解决 Chromium 不支持 socks5 认证）
#
# 架构：
#   Playwright (127.0.0.1:11001..110NN)  ← gost 转发 ← socks5h 认证代理（2090 按账号恒定出口）
#   每个端口 = 一个账号 = 池里一个固定出口 IP
#
# 用法：
#   bash start_gost.sh                 # 默认 6 个账号（t01..t06）
#   bash start_gost.sh 20              # 起 20 个账号
#   bash start_gost.sh 20 stop         # 停掉
#
# 依赖：gost v3（node-4 已装 /usr/local/bin/gost）
set -u

N="${1:-6}"
ACTION="${2:-start}"
P="${TEXTIN_POOL_PASS:-}"
if [ -z "$P" ]; then
  echo "错误：未设置 TEXTIN_POOL_PASS 环境变量（代理池密码）" >&2
  echo "用法：TEXTIN_POOL_PASS='***' bash start_gost.sh 20" >&2
  exit 1
fi
UP="pool.livetest.cn:2090"
BASE_PORT=11000

stop_all() {
  for i in $(seq -w 1 "$N"); do
    pkill -f "127.0.0.1:$((BASE_PORT + 10#$i))" 2>/dev/null
  done
  echo "stopped"
}

start_all() {
  started=0
  for i in $(seq -w 1 "$N"); do
    port=$((BASE_PORT + 10#$i))
    pkill -f "127.0.0.1:$port" 2>/dev/null
    setsid gost -L "http://127.0.0.1:$port" \
      -F "socks5://poolclient-t$i:$P@$UP" >"/tmp/gost$i.log" 2>&1 &
    started=$((started + 1))
  done
  sleep 2
  n_listen=$(ss -ltn 2>/dev/null | grep -c ':110')
  echo "started=$started listening_ports=$n_listen"
}

case "$ACTION" in
  start) start_all ;;
  stop) stop_all ;;
  *) echo "usage: $0 [count] [start|stop]"; exit 1 ;;
esac
