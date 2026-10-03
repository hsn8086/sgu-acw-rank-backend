#!/usr/bin/env bash
#
# 生成当天三道每日一题的题解。
#
# 由 systemd timer 在凌晨四点后调用（见 deploy/）。也可以手动跑：
#
#     ./run.sh              # 生成今天还没写的题解
#     ./run.sh 2026-10-03   # 生成某一天的
#
# 需要这些环境变量（生产由 /etc/sgu-acw-editorial.env 提供）：
#
#     SGU_ACW_EDITORIAL_SECRET  与后端共享的密钥
#     SGU_ACW_LLM_KEY           模型端点的密钥
#
# 可选的：
#
#     SGU_ACW_API_BASE          默认 http://127.0.0.1:8091
#     SGU_ACW_EDITORIAL_MODEL   默认 deepseek-flash

set -euo pipefail

cd "$(dirname "$0")"

if [[ -z "${SGU_ACW_EDITORIAL_SECRET:-}" ]]; then
  echo "缺少 SGU_ACW_EDITORIAL_SECRET" >&2
  exit 1
fi

if [[ -z "${SGU_ACW_LLM_KEY:-}" ]]; then
  echo "缺少 SGU_ACW_LLM_KEY" >&2
  exit 1
fi

DAY="${1:-}"

# 生成器有独立的 agent 目录，不碰使用者的个人配置。
export PI_CODING_AGENT_DIR="$PWD/.pi/agent"

if [[ -n "$DAY" ]]; then
  PROMPT="用 list_pending_problems 拿到 ${DAY} 的题，逐道写题解并提交。"
else
  PROMPT="用 list_pending_problems 拿到今天的题，逐道写题解并提交。"
fi

# 不做交互确认：这是无人值守跑的。
# 不给 session 文件：每天一次，没有续接的需求。
exec pi -p \
  --approve \
  --no-session \
  --provider sgu \
  --model "${SGU_ACW_EDITORIAL_MODEL:-deepseek-flash}" \
  "$PROMPT"
