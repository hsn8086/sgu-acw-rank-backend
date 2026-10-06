#!/usr/bin/env bash
#
# 生成当天每日一题的题解。
#
# 由 systemd timer 在凌晨四点后调用（见 deploy/）。也可以手动跑：
#
#     ./run.sh              # 生成今天还没写的题解
#     ./run.sh 2026-10-03   # 生成某一天的
#
# **逐题跑，每题一个独立的 pi 进程。**
#
# 一开始是一次调用写完全部，但那样任何一道卡住就全丢：pi 会一直等模型，
# 直到 systemd 的 TimeoutStartSec 把它整个掐掉，已经写完的也白写。
# 2026-10-05 和 10-06 连续两天都是这么失败的（45 分钟里 CPU 只用了不到
# 3 分钟，全在等响应）。
#
# 拆开之后，一道题的失败不影响其他几道，超时也是按题算。
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

# 单题的墙钟上限。写一道题通常两三分钟，给 15 分钟已经很宽裕；
# 超过说明卡住了，没必要继续等。
PER_PROBLEM_TIMEOUT="${SGU_ACW_PER_PROBLEM_TIMEOUT:-900}"

# 生成器有独立的 agent 目录，不碰使用者的个人配置。
export PI_CODING_AGENT_DIR="$PWD/.pi/agent"

API_BASE="${SGU_ACW_API_BASE:-http://127.0.0.1:8091}"

# 问后端要当天的题。这里用 pending 接口而不是自己算日期——题目是后端
# 抽的，而且它顺带会把当天还没抽的题抽出来。
QUERY=""
[[ -n "$DAY" ]] && QUERY="?day=${DAY}"

PENDING=$(curl -fsS \
  -H "X-Editorial-Secret: ${SGU_ACW_EDITORIAL_SECRET}" \
  "${API_BASE}/api/internal/editorials/pending${QUERY}")

# 解析出 (id, key) 列表。用 python 而不是 jq：镜像里已经有 python，
# 少一个依赖。
# 外层用双引号、内层用单引号：反过来的话 shell 会把 python 里的单引号
# 当成字符串结束，代码被截断。
ROWS=$(printf '%s' "$PENDING" | python3 -c "
import json, sys
for item in json.load(sys.stdin):
    print(f\"{item['daily_problem_id']}\t{item['key']}\")
")

if [[ -z "$ROWS" ]]; then
  echo "今天没有待生成的题，结束"
  exit 0
fi

FAILED=0
TOTAL=0
OK=0

while IFS=$'\t' read -r PID KEY; do
  [[ -z "$PID" ]] && continue
  TOTAL=$((TOTAL + 1))

  if [[ -n "$DAY" ]]; then
    PROMPT="写一道题：${KEY}（daily_problem_id=${PID}），日期是 ${DAY}。读题面、写题解、验证三语言代码、submit_editorial 提交。提交成功就停，不要总结。"
  else
    PROMPT="写一道题：${KEY}（daily_problem_id=${PID}）。读题面、写题解、验证三语言代码、submit_editorial 提交。提交成功就停，不要总结。"
  fi

  echo "--- ${KEY} (id=${PID}) ---"

  # 不做交互确认：这是无人值守跑的。
  # 不给 session 文件：每道题独立，没有续接的需求。
  if timeout "$PER_PROBLEM_TIMEOUT" pi -p \
    --approve \
    --no-session \
    --provider sgu \
    --model "${SGU_ACW_EDITORIAL_MODEL:-deepseek-flash}" \
    "$PROMPT"; then
    OK=$((OK + 1))
    echo "--- ${KEY} 完成 ---"
  else
    FAILED=$((FAILED + 1))
    # 单题失败不中断：其他几道还能写。
    echo "--- ${KEY} 失败或超时 ---" >&2
  fi
done <<< "$ROWS"

echo "共 ${TOTAL} 道，成功 ${OK}，失败 ${FAILED}"

# 有失败就返回非零，让 systemd 记成 failed——这样 journalctl 里能一眼
# 看出哪天的任务没跑完。
[[ "$FAILED" -eq 0 ]]
