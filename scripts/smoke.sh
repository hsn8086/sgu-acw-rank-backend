#!/usr/bin/env bash
# 本地端到端演练。对着跑起来的服务逐条发请求，把关键响应打出来。
#
#   ./scripts/smoke.sh                      # 默认 http://127.0.0.1:8091
#   BASE=http://127.0.0.1:8091 ./scripts/smoke.sh
#
# 每步之间会暂停等你按回车，方便逐步观察。设 AUTO=1 可以一路跑完。

set -euo pipefail

BASE="${BASE:-http://127.0.0.1:8091}"
ROOT_ID="${ROOT_ID:-24125023005}"
ROOT_PW="${ROOT_PW:-devpassword123}"

ROOT_JAR=$(mktemp)
MEMBER_JAR=$(mktemp)
trap 'rm -f "$ROOT_JAR" "$MEMBER_JAR"' EXIT

# 每次跑用一个不重复的学号，这样脚本对着同一个库反复跑也不会撞上「已注册」。
# 格式：25|1|25|02|3|NNN，末三位是班内序号。
MEMBER_ID=$(printf '25125023%03d' $((RANDOM % 1000)))
MEMBER_PW='memberpass123'

step() {
  printf '\n\033[1;36m── %s\033[0m\n' "$*"
  if [ "${AUTO:-0}" != "1" ]; then
    read -r -p "   按回车继续… " _
  fi
}

# 打印 JSON，尽量中文可读
jq_or_cat() {
  if command -v python3 >/dev/null; then
    python3 -c 'import json,sys;print(json.dumps(json.load(sys.stdin),ensure_ascii=False,indent=2))' 2>/dev/null || cat
  else
    cat
  fi
}

step "0. 健康检查"
curl -sS "$BASE/api/healthz" | jq_or_cat

step "1. 全新实例：只有引导出来的 root，没有其他账号"
curl -sS "$BASE/api/bootstrap" | jq_or_cat

step "2. root 登录（学号 ${ROOT_ID}）"
curl -sS -c "$ROOT_JAR" -X POST "$BASE/api/auth/login" \
  -H 'Content-Type: application/json' \
  -d "{\"student_id\":\"$ROOT_ID\",\"password\":\"$ROOT_PW\"}" | jq_or_cat

step "3. 看一下种下的 Cookie（HttpOnly，JS 读不到）"
# curl 的 cookie jar 把 HttpOnly 行写成注释形式 #HttpOnly_<host>，所以这里
# 不能按普通注释过滤。
awk '/sgu_acw_session/ {
       host = $0; sub(/^#HttpOnly_/, "", host); split(host, a, /[\t ]/)
       printf "   %s  →  %s = %.12s…   (HttpOnly)\n", a[1], $6, $7
     }' "$ROOT_JAR"

step "4. 当前身份"
curl -sS -b "$ROOT_JAR" "$BASE/api/auth/me" | jq_or_cat

step "5. 发一个邀请码：2 人 / 48 小时"
INVITE=$(curl -sS -b "$ROOT_JAR" -X POST "$BASE/api/admin/invites" \
  -H 'Content-Type: application/json' \
  -d '{"max_uses":2,"ttl_hours":48,"grants_role":"member","note":"本地演练"}')
echo "$INVITE" | jq_or_cat
CODE=$(echo "$INVITE" | python3 -c 'import json,sys;print(json.load(sys.stdin)["code"])')

step "6. 用邀请码注册一个成员，只填学号姓名密码"
echo "   学号 ${MEMBER_ID}；注意 enroll_year / college_code / major_code 是服务端从学号解析出来的"
REGISTER=$(curl -sS -c "$MEMBER_JAR" -X POST "$BASE/api/auth/register" \
  -H 'Content-Type: application/json' \
  -d "{\"invite_code\":\"$CODE\",\"student_id\":\"$MEMBER_ID\",\"display_name\":\"张三\",\"password\":\"$MEMBER_PW\"}")
echo "$REGISTER" | jq_or_cat
MEMBER_UID=$(echo "$REGISTER" | python3 -c 'import json,sys;print(json.load(sys.stdin)["id"])')

if [ "$(curl -sS -o /dev/null -w '%{http_code}' -b "$MEMBER_JAR" "$BASE/api/auth/me")" != "200" ]; then
  echo "   !! 注册后拿不到会话，后面几步会全部 401，先查这个" >&2
fi

step "7. 邀请码已被消耗 1/2，还没耗尽"
curl -sS -b "$ROOT_JAR" "$BASE/api/admin/invites" | jq_or_cat

step "8. 管理员给这个成员写备注（仅管理员可见）"
curl -sS -b "$ROOT_JAR" -X PATCH "$BASE/api/admin/users/$MEMBER_UID" \
  -H 'Content-Type: application/json' \
  -d '{"admin_note":"已核实，此人就是张三"}' | jq_or_cat

step "9. 成员自己看自己：没有 admin_note 这个字段"
curl -sS -b "$MEMBER_JAR" "$BASE/api/auth/me" | jq_or_cat

step "10. 成员目录（登录可见）：同样不含 admin_note"
curl -sS -b "$MEMBER_JAR" "$BASE/api/users" | jq_or_cat

step "11. 成员想进管理接口 —— 应当 403"
curl -sS -o /dev/null -w '   HTTP %{http_code}\n' -b "$MEMBER_JAR" "$BASE/api/admin/users"

step "12. 成员想给自己提权 —— 角色字段被忽略"
curl -sS -b "$MEMBER_JAR" -X PATCH "$BASE/api/auth/me" \
  -H 'Content-Type: application/json' \
  -d '{"role":"admin"}' | jq_or_cat

step "13. 非法学号注册 —— 统一提示，不说明哪一位错"
for sid in 12345 24999999999 2412502300X; do
  printf '   %-16s ' "$sid"
  curl -sS -X POST "$BASE/api/auth/register" \
    -H 'Content-Type: application/json' \
    -d "{\"invite_code\":\"$CODE\",\"student_id\":\"$sid\",\"display_name\":\"测试\",\"password\":\"password123\"}" \
    -w ' [%{http_code}]\n'
done

step "14. 学号不存在 / 密码错误 —— 两种说法完全一样"
for payload in '{"student_id":"24125023099","password":"whatever12345"}' \
               "{\"student_id\":\"$ROOT_ID\",\"password\":\"wrong-password\"}"; do
  printf '   %s\n     → ' "$payload"
  curl -sS -X POST "$BASE/api/auth/login" -H 'Content-Type: application/json' -d "$payload" -w ' [%{http_code}]\n'
done

step "15. root 不能通过邀请码授予 root"
curl -sS -b "$ROOT_JAR" -X POST "$BASE/api/admin/invites" \
  -H 'Content-Type: application/json' \
  -d '{"max_uses":1,"ttl_hours":1,"grants_role":"root"}' -w ' [%{http_code}]\n'

step "16. root 账号不能被降级 / 停用"
printf '   降级: '; curl -sS -o /dev/null -w '%{http_code}\n' -b "$ROOT_JAR" \
  -X PATCH "$BASE/api/admin/users/1" -H 'Content-Type: application/json' -d '{"role":"member"}'
printf '   停用: '; curl -sS -o /dev/null -w '%{http_code}\n' -b "$ROOT_JAR" \
  -X PATCH "$BASE/api/admin/users/1" -H 'Content-Type: application/json' -d '{"status":"disabled"}'

step "17. 管理员重置成员密码（一次性返回临时密码）"
curl -sS -b "$ROOT_JAR" -X POST "$BASE/api/admin/users/$MEMBER_UID/reset-password" | jq_or_cat
echo "   拉到临时密码后，成员原有会话应当已经失效："
curl -sS -o /dev/null -w '   /auth/me → HTTP %{http_code}\n' -b "$MEMBER_JAR" "$BASE/api/auth/me"

step "18. 管理概览"
curl -sS -b "$ROOT_JAR" "$BASE/api/admin/stats" | jq_or_cat

printf '\n\033[1;32m演练结束。\033[0m 清理：rm -f %s/sgu-acw.db*\n\n' "$(pwd)"
