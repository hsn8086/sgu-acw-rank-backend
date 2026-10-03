# 题解生成

用 pi + LLM 为当天的每日一题写题解，提交到后端数据库。

## 时间线

```
04:00  换题（daily.py 抽出当天三道，落库）
04:30  生成本脚本跑 → 题解存为草稿
20:00  后端自动公布题解（sync.py 每 5 分钟检查一次到点）
次日 04:00  后端自动公布参考代码
```

生成放在换题之后、公布之前，中间那段时间是给人过一眼的窗口。

## 目录

```
.pi/
  agent/models.json      模型端点（密钥用环境变量插值，不写死）
  extensions/editorial.ts  四个工具：读题面、验证代码、提交题解、报告失败
  APPEND_SYSTEM.md       给模型的指令（怎么写题解）
  settings.json          defaultTools: []，关掉所有内置工具
data/
  cf_statements.jsonl    2024 年后 CF 题面（5.7MB，从 CPRet 数据集抽取）
  have_statements.json   题号索引，供后端回填 problem_cache.has_statement
run.sh                   入口，systemd 调它
```

## 权限

生成器**没有**内置工具：`read` / `bash` / `edit` / `write` 全部关掉
（`.pi/settings.json` 里 `defaultTools: []`）。它能做的事只有扩展提供的那几个：

| 工具 | 作用 |
|---|---|
| `list_pending_problems` | 问后端今天要写哪些题 |
| `get_problem_statement` | 从本地题面索引读题面 |
| `verify_solution` | 在临时目录里编译运行代码 |
| `submit_editorial` | 提交题解 |
| `report_failure` | 报告写不出来 |

这样它碰不到文件系统、碰不到环境变量里的密钥，也不会误删东西。

`verify_solution` 自己建临时目录、限超时（编译 30s、运行 20s）、不继承环境变量
（代码读不到 `SGU_ACW_*`）。这是为了在「能验证代码」和「不放开 bash」之间取平衡。

## 题面数据

`data/cf_statements.jsonl` 从 [CPRet 数据集](https://huggingface.co/datasets/coldchair16/CPRet-Embeddings)
抽取，只保留 **2024 年之前**的题（contest id < 1918），因为每日一题只抽这个范围。

为什么不要新题：新比赛的参赛人数和 rating 分布还在形成中，标出来的分与实际
难度对不上。老题经过足够多人的检验，标多少就是多少。

**这份数据不会自己更新。** 但影响不大——老题不会变，索引是完整的：

| 档位 | 候选池 | 有题面 |
|---|---|---|
| 800-1000 | 1387 | 1387 |
| 1100-1300 | 1078 | 1078 |
| 1400-1600 | 1126 | 1126 |

抽签会跳过没有题面的题（`problem_cache.has_statement`），所以缺题面不会
导致「抽到题却写不出题解」。当前候选池与题面索引完全一致。

重新生成索引（需要原始数据集）：

```bash
# data/cpret/probs_2609.jsonl 是 CPRet 的原始数据，不在版本库里
python3 - <<'PY'
import json, re
pat = re.compile(r'codeforces\.com/problemset/problem/(\d+)/([A-Za-z]\d?)')
luogu = re.compile(r'https://www\.luogu\.com\.cn/problem/CF(\d+)([A-Za-z]\d?)$')
seen = {}
for line in open('data/cpret/probs_2609.jsonl'):
    row = json.loads(line)
    if row['source'] not in ('Codeforces', 'Luogu'):
        continue
    m = pat.search(row['url']) or luogu.match(row['url'])
    if not m:
        continue
    key = f'{m.group(1)}{m.group(2)}'
    if key in seen and 'codeforces.com' in seen[key]['url']:
        continue
    seen[key] = {'key': key, 'title': row['title'], 'url': row['url'], 'text': row['text']}

old = {k: v for k, v in seen.items() if int(re.match(r'(\d+)', k).group(1)) < 1918}
with open('tools/generate-editorial/data/cf_statements.jsonl', 'w') as f:
    for k in sorted(old, key=lambda k: (int(re.match(r'(\d+)', k).group(1)), k)):
        f.write(json.dumps(old[k], ensure_ascii=False) + '\n')
json.dump(sorted(old), open('tools/generate-editorial/data/have_statements.json', 'w'))
PY
```

生成完要把索引回填到后端：

```bash
python -m app.import_statements
```

## 本地跑

需要后端在跑，并且配了 `SGU_ACW_EDITORIAL_SECRET`（`.env` 里）。

```bash
cd tools/generate-editorial
export SGU_ACW_EDITORIAL_SECRET=local-dev-editorial-secret
export SGU_ACW_LLM_KEY=<密钥>
./run.sh
```

密钥放在仓库根目录的 `sgu-deepseek.txt`（已 gitignore）。**不要提交它。**

## 部署

```bash
# 1. 同步代码（含题面数据）
rsync -az --delete \
  --exclude tests --exclude .git --exclude .venv --exclude __pycache__ \
  ./ root@<vps>:/opt/sgu-acw-api/

rsync -az tools/generate-editorial/ root@<vps>:/opt/sgu-acw-editorial/

# 2. 装 pi（生成器需要 node）
ssh root@<vps> 'curl -fsSL https://get.pnpm.io/install.sh | sh -'

# 3. 写密钥文件（两个密钥，不要放进版本库）
cat > /etc/sgu-acw-editorial.env <<'EOF'
SGU_ACW_EDITORIAL_SECRET=<与后端一致>
SGU_ACW_LLM_KEY=<模型密钥>
SGU_ACW_API_BASE=http://127.0.0.1:8091
SGU_ACW_EDITORIAL_MODEL=deepseek-flash
EOF
chmod 600 /etc/sgu-acw-editorial.env

# 4. 后端也要有同一个 EDITORIAL_SECRET（/etc/sgu-acw-api.env）

# 5. 回填题面标记
cd /opt/sgu-acw-api && set -a && . /etc/sgu-acw-api.env && set +a \
  && ./.venv/bin/python -m app.import_statements

# 6. 装 timer
cp deploy/sgu-acw-editorial.{service,timer} /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now sgu-acw-editorial.timer
```

## 排查

```bash
# 今天生成了几篇、什么状态
curl -H "X-Editorial-Secret: $SGU_ACW_EDITORIAL_SECRET" \
  http://127.0.0.1:8091/api/internal/editorials/status

# 手动跑一次
systemctl start sgu-acw-editorial.service
journalctl -u sgu-acw-editorial -n 50

# 手动公布一篇（不等 20:00）
curl -X POST -b <管理员 cookie> \
  http://127.0.0.1:8091/api/daily/editorials/<id>/publish
```

生成失败的题在库里 `status='failed'`，`error` 字段是原因。前端会显示
「题解生成失败」而不是空白。
