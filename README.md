# SGU ACW Rank Backend

韶关学院算法竞赛工作坊的成员与榜单后端。FastAPI + SQLModel + SQLite。

前端是静态站（Cloudflare Pages），通过 Pages Function 把 `/api/*` 同源代理到这里，
所以浏览器看到的始终是 `sgu-acw.zh314.xyz`，不存在跨域 Cookie 问题。

## 跑起来

```bash
uv sync
uv run uvicorn app.main:app --reload --port 8091
```

首次启动会自动建库（默认 `./sgu-acw.db`）。健康检查：

```bash
curl localhost:8091/api/healthz
```

## 配置

全部走环境变量，前缀 `SGU_ACW_`，也可写在同目录 `.env`：

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `SGU_ACW_DATABASE_URL` | `sqlite:///./sgu-acw.db` | 生产用 `/var/lib/sgu-acw/api.db` |
| `SGU_ACW_SESSION_TTL_DAYS` | `30` | 会话有效期 |
| `SGU_ACW_COOKIE_NAME` | `sgu_acw_session` | 会话 Cookie 名 |
| `SGU_ACW_COOKIE_SECURE` | `true` | 本地 http 调试时设 `false` |
| `SGU_ACW_COOKIE_DOMAIN` | 空 | 同源代理下留空即可 |
| `SGU_ACW_ORIGIN_SECRET` | 空 | 与 Pages Function 共享的口令，见下文 |
| `SGU_ACW_BOOTSTRAP_ROOT_STUDENT_ID` | 空 | 首次启动时创建 root 账号的学号 |
| `SGU_ACW_BOOTSTRAP_ROOT_PASSWORD` | 空 | 同上，仅在账号不存在时使用 |

## 源站保护

后端在 VPS 上直接监听端口。加了 Pages Function 之后，攻击者仍可绕过
Cloudflare 直接打源站，所以 Function 转发时会带上 `X-Origin-Secret`，
后端拒绝没有这个头的请求。

本地开发时把 `SGU_ACW_ORIGIN_SECRET` 留空即可关闭该检查。

## 目录

```
app/
  main.py        应用装配、生命周期
  config.py      配置
  db.py          引擎与会话
  models.py      表定义
  security.py    密码哈希、会话令牌
  student_id.py  学号解析
  deps.py        依赖注入（当前用户、权限）
  routers/       各模块路由
```

## 学号

学号规则见 sgu-aio 的 `docs/学号规则/README.md`：11 位，前 2 位入学年份、
第 3 位培养层次、4–5 位学院码、6–7 位专业号、第 8 位班级、9–11 位序号；
联培为 12 位，末位字母。解析结果在注册时落库，榜单查询不再重新解析。
