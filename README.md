# Wave Money

号码波动推荐工具。Nuxt 3 + FastAPI + Supabase PostgreSQL。

## 技术栈

| 层 | 技术 | 部署 |
|----|------|------|
| 前端 | Nuxt 3, Vue 3, Tailwind CSS | Vercel |
| 后端 | FastAPI, uvicorn | Render |
| 数据库 | PostgreSQL (Supabase 托管) | Supabase |

## 目录结构

```
wave-money/
├── frontend/                 # Nuxt 3
│   ├── pages/
│   │   ├── index.vue         # 首页 / 录入页
│   │   └── recommend.vue     # 推荐结果页
│   ├── composables/useApi.ts
│   └── nuxt.config.ts
├── backend/                  # FastAPI
│   ├── main.py
│   ├── db.py                 # asyncpg 连接池
│   ├── repository.py         # 存储层（Postgres / 内存回退）
│   ├── services/lottery.py   # 推荐算法（纯函数）
│   ├── models/
│   ├── routers/
│   └── requirements.txt
├── schema.sql                # Supabase 初始化脚本
├── render.yaml
└── README.md
```

---

## 业务规则

### 号码范围

号码 1 到 49。

### 生肖规则

每 12 个号码为同一生肖，即 `n` 与 `n ± 12` 同肖：

```
01 / 13 / 25 / 37 / 49
02 / 14 / 26 / 38
03 / 15 / 27 / 39
...
```

最新开奖号的所有同肖号码称为**重肖**。默认**不避开**；可在设置中开启「避开重肖」（`exclude_repeat_zodiac`），开启后才排除最新一期同肖组。

### 波动规则（相邻两期差值绝对值）

| 类型 | 条件 | 枚举值 |
|------|------|--------|
| 小波动 | 差值 ≤ 10 | `small` |
| 常规波动 | 10 < 差值 ≤ 30 | `normal` |
| 大跳 | 差值 > 30 | `big` |

阈值可通过 `PUT /api/settings` 调整（`small_max` / `normal_max`）。

### 推荐规则

1. 始终避开最新一期的开奖号本身
2. **避开重肖**（可选，默认关）：设置开启 `exclude_repeat_zodiac` 后，才额外排除最新开奖号的**全部重肖号码**；默认关闭时同肖号可以入选
3. 默认推荐 6 个号码，总金额 50 元（均注下派生单注约 8 元；50÷6 有余数时补给第一注）
4. 号码尽量分散在小波动 / 常规波动 / 大跳三类中
5. 某类波动从最新号出发无解时（例如最新号为 21 时无大跳号），结果中明确标注「本期大跳无号」
6. **波动回补逻辑**（上期波动 → 本期侧重顺序）：

   | 上期波动 | 本期侧重顺序 |
   |----------|--------------|
   | 小波动 | 常规波动 → 大跳 → 小波动 |
   | 常规波动 | 小波动 → 大跳 → 常规波动 |
   | 大跳 | 小波动（回补） → 常规波动 → 大跳 |

7. 每个推荐号标注：与最新开奖号的差值、所属波动类型、是否重肖、侧重等级（主推 / 次选 / 防守）

**候选池内部排序**：先取历史遗漏最久的（历史出现次数最少），再取与最新号差值最小的。保证结果可复现。

**注数补齐**：若某类波动无解导致不足配置注数，从仍有候选的池中补齐（允许同类多取一个）。

### 侧重与金额

**最大投注金额（`total_amount`）是唯一的预算真值**，所有模式都从它取预算；
金额最小单位（`amount_unit`，默认 5 元）约束随机分配的各注金额。
旧的 `bet_unit`（单注金额）已降级为**只读派生展示值**（= 最大投注 ÷ 有效注数），
接口仍会回该字段，但它不再是可写输入。

| 模式 | 枚举值 | 分配（默认 6 注 / 50 元） | 默认最大投注 |
|------|--------|------|------|
| 均注 | `even` | 10/8/8/8/8/8（余数补给第一注） | 50 元 |
| 侧重 | `weighted` | 主推占约 4/6，其余均分（余数补给次选） | 50 元 |
| 单挑 | `single` | 1 个号 50 元（整份预算押 1 注） | 50 元 |
| 随机分配 | `random` | 按最小单位随机拆给各注，各注金额可不等，总和精确等于最大投注 | 50 元 |

随机分配是**按最大投注拆分的分配**，不是收益预期，也不构成任何策略优势。
同一期（同一份最新开奖）+ 同一组参数 → 分配完全一致（种子由期号与参数派生，可复现）；
请求里可传可选的 `amount_seed` 重掷得到另一份分配。

### 一键复制格式（竞猜投注串）

按金额分组：独额写 `号码：金额元`；相同金额合并为 `号1、号2：各金额元`。
组间用中文分号，分号后换行；末行 `合计：N元。`

```
30：20元；
15、39：各15元；
合计：50元。

9、19、31：各10元；
合计：30元。

9：50元；
合计：50元。
```

### 数据存储

| 表 | 字段 |
|----|------|
| `draws` | `id`, `draw_date`, `period`, `special_number`, `created_at`（特码的唯一事实来源） |
| `settings` | `user_id`, `key`, `value`（主键 `(user_id, key)`；`user_id = 0` 是**全局默认模板**） |
| `users` | `id`, `phone`(unique), `password_hash`, `password_salt`, `role`, `status`, `created_at`, `approved_at`, `last_login_at` |
| `recommend_rounds` | 每用户每期采用快照；`stake_mode`：`SIMULATED`（模拟买入，默认）/ `REAL`（真实买入）/ `SKIPPED`（未买）。当前收益均为**模拟试算**；后期支持真实买入标记与逐期修正 |
| `zodiac_years` / `zodiac_numbers` | 农历年边界与 49 号码 → 生肖派生表 |

- 支持导入 / 导出 JSON（只含 `draws`，version 2）
- 支持清空历史

> ⚠️ **`items` 表的漂移（已知，勿踩）**：`schema.sql`（新库一次性初始化）里还留着脚手架时代的 `items` 建表 + 3 行示例数据，但 `backend/repository.py` 的 `SCHEMA_STATEMENTS`（`ensure_schema` / 迁移脚本走的那份）**早已不含 `items`**。也就是说：跑迁移脚本升级的库里没有 `items` 表，`GET /api/items` 会走异常兜底返回内置示例数据——不影响任何业务。以 `SCHEMA_STATEMENTS` 为准即可；`items` 属脚手架遗留，后续可整体下线。

> 未配置 `DATABASE_URL` 时后端退化为**内存存储**（进程重启即丢失），仅用于本地预览。生产环境必须配置 Supabase 连接串，否则历史无法持久化。

---

## 本地启动

### 1. 数据库（Supabase）

1. 在 [supabase.com](https://supabase.com) 创建项目
2. 打开 **SQL Editor**，执行 [`schema.sql`](./schema.sql)
3. 从 **Project Settings → Database → Connection string** 复制 **Connection pooling**（pooler）连接串 —— 直连主机 `db.<ref>.supabase.co` 只有 IPv6，云平台（Render 等）连不上，本地直连可用但生产必须用 pooler

### 2. 后端

```powershell
cd backend
python -m venv .venv
.\.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
```

编辑 `.env`：

```env
DATABASE_URL=postgresql://postgres:...@db.xxx.supabase.co:5432/postgres
CORS_ORIGINS=http://localhost:3000

# 会话签名密钥（生产必填）。生成：python -c "import secrets; print(secrets.token_urlsafe(48))"
SESSION_SECRET=
# 权限校验总开关（默认 true；显式 false 才是开发旁路）
AUTH_ENFORCED=true
```

启动：

```powershell
.\.venv\Scripts\uvicorn main:app --reload --port 8000
```

启动日志会打印当前存储后端（`postgres` 或 `memory`）以及 `AUTH_ENFORCED` 的开关状态。

### 2.5 创建管理员账号（必做，否则没人能进后台）

新注册用户一律 `PENDING`，**必须由管理员审批才能登录**，所以第一个管理员只能从命令行直接建：

```powershell
cd D:\myproject\wave-money
$env:ADMIN_PHONE='13800138000'
$env:ADMIN_PASSWORD='换成你的强密码'
.\backend\.venv\Scripts\python.exe .\backend\scripts\create_admin.py
```

- **幂等**：手机号已存在则提升为 `ADMIN` + `APPROVED`；不加 `--password` 时不会改动已有密码。
- 密码**不会**被打印或写进日志；也可用 `--phone` / `--password` 传参（会留在 shell 历史里，用完请清理），或 `--promote-only` 只提权不改密。
- 该脚本直连数据库，不受 `AUTH_ENFORCED` 影响，任何时候都能救回后台。

### 3. 前端

```powershell
cd frontend
copy .env.example .env
npm install
npm run dev
```

打开 http://localhost:3000

---

## API

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/health` | 健康检查 → `{"status":"ok"}` |
| GET | `/api/history` | 历史开奖（最新在前，`number` = 特码，附 `draw_date` / `period` / 波动） |
| POST | `/api/draws/quick` | 快捷录入一期：`{"special_number":21,"period":270,"draw_date":"2026-09-27"}`（期号 / 日期可省略） |
| GET | `/api/draws/next-period` | 快捷录入表单预填：建议期号 + 今天（UTC+8）+ 最新一期 |
| GET | `/api/settings` | 读取配置 |
| PUT | `/api/settings` | 更新 `small_max` / `normal_max` / `total_amount` / `amount_unit` / `mode` / `pick_count` / `exclude_repeat_zodiac`（避开重肖，默认 `false`） |
| POST | `/api/recommend` | 生成推荐；body `{"mode":"random","number":21,"amount_seed":3}` 均可选 |
| GET | `/api/export` | 导出 JSON |
| POST | `/api/import?replace=true` | 导入 JSON（仅 `ADMIN`） |
| GET | `/api/items` | 脚手架示例数据（表缺失时返回内置样例，见「数据存储」的漂移说明） |

认证与用户管理接口（详见下方「认证与权限（RBAC）」）：

| 方法 | 路径 | 所需角色 |
|------|------|----------|
| GET | `/api/auth/config` | 公开（前端用来读灰度开关） |
| POST | `/api/auth/register` | 公开 → 建 `PENDING` 账号 |
| POST | `/api/auth/login` | 公开 → 下发会话 Cookie |
| GET | `/api/auth/me` | 任意登录用户 |
| POST | `/api/auth/logout` | 公开（幂等） |
| GET | `/api/admin/users?status=` | `ADMIN` |
| POST | `/api/admin/users/{phone}/approve` \| `/reject` \| `/status` \| `/role` | `ADMIN` |

`/api/recommend` 返回示例（最新号 21、上期 11、侧重模式）：

```json
{
  "latest": 21,
  "previous": 11,
  "prev_wave": { "number": 11, "diff": 10, "type": "small", "label": "小波动" },
  "copy_text": "10：20元；\n20、32：各5元；\n合计：30元。",
  "picks": [
    { "number": 10, "diff": 11, "wave_type": "normal", "role": "primary",
      "role_label": "主推", "amount": 20, "is_repeat_zodiac": false }
  ],
  "missing_waves": [{ "type": "big", "label": "大跳", "note": "本期大跳无号" }]
}
```

---

## 认证与权限（RBAC）

手机号 + 密码登录，三档角色，服务端强制校验。密码用标准库 `hashlib.scrypt`
（每用户随机 salt）+ `hmac.compare_digest` 校验；会话是自签的 HMAC-SHA256 Cookie
（`HttpOnly` / `SameSite=Lax` / 签名内含服务端过期时间），**没有引入任何新依赖**。

### 角色与访问矩阵

| 接口 | 所需角色 |
|------|----------|
| `GET /api/health` | 公开 |
| **只读浏览（匿名可读）**：`GET /api/draws`、`GET /api/draws/latest`、`GET /api/history`、`GET /api/zodiac/*`、`GET /api/stats/trend`、`GET /api/stats/frequency`、`GET /api/stats/zodiac-trend` | 匿名（**无需登录**；带会话时按本人设置算阈值） |
| `GET /api/draws/next-period`、`GET/PUT /api/settings` | `USER`+（任意**已审批**用户，非 VIP 专属） |
| `POST /api/recommend`（财富密码）、`GET /api/export`、`GET /api/stats/pnl`、`POST /api/stats/backtest` | `VIP` / `ADMIN` |
| `POST /api/draws/quick` / `{id}/correct` / `import`、`DELETE /api/draws*`、`POST /api/import` | `ADMIN` |
| `/api/admin/*`（用户列表 / 审批 / 改状态 / 改角色） | `ADMIN` |

账号状态：`PENDING`（待审批，注册后的默认值）→ `APPROVED`（已通过）/ `REJECTED`（已拒绝）/ `DISABLED`（已停用）。
只有 `APPROVED` 能登录。登录接口**先校验密码再看状态**，所以「待审批」这类差异文案只有密码正确的人能看到，不构成手机号枚举面。

自锁保护：管理员不能降低自己的角色、不能改自己的审批状态，也不能撤掉**最后一个**已审批管理员（返回 `409`）。

### ⚠️ API 开关 `AUTH_ENFORCED`

| `AUTH_ENFORCED` | 行为 |
|-----------------|------|
| `true` / 未设置（**默认**） | 按上表严格执行；未登录 → `401`，角色不足 → `403` |
| `false`（显式旁路） | 角色依赖一律放行，业务接口无需登录即可访问；`/api/auth/*` 仍正常工作 |

前端菜单与路由**不依赖**此开关：未登录在 UI 上始终只见首页 / 登录 / 注册。
本开关只控制后端 API；生产务必保持 `true`。角色检查内部没有任何静默 fail-open 分支。
后端启动时会打印一行醒目的开关状态日志，便于确认。

### 会话 Cookie 与登录方案（**同源反代，推荐**）

- 密钥来自 `SESSION_SECRET`（HMAC-SHA256）。**生产环境缺失会直接启动失败**；本地开发缺失会打印告警并使用不安全的兜底密钥。换值会让所有已登录会话立刻失效。
- **持久登录**：会话写在浏览器持久 Cookie `wm_session`（`HttpOnly` + `Max-Age`，等同于 localStorage 级别的持久层，关浏览器再开仍有效）。默认 **365 天**；可用 `JWT_EXPIRE_DAYS`（天）或 `SESSION_TTL_SECONDS`（秒，优先）覆盖。只有用户清站点数据 / 点退出登录 / 密钥轮换 / 账号被停用，会话才会失效。
- **推荐部署形态 = 同源反代**：前端（Vercel）把 `/api/**` 反代到后端（Render），浏览器**只看到前端域名**，请求是同站的。此时：
  - `SESSION_COOKIE_SAMESITE` 保持默认 `lax` 即可，**不要**改成 `none`；
  - `CORS_ORIGINS` 基本用不上（请求由 Nitro 服务端发起，没有浏览器 Origin）。
- `SESSION_COOKIE_SECURE`：代码会按 `request.url.scheme`（或 `X-Forwarded-Proto`）自动判 Secure，但 uvicorn 默认**不信任代理头**，代理后可能判不出来。所以生产**显式设 `SESSION_COOKIE_SECURE=true`**（`render.yaml` 已钉死）。
- 仅当**真正跨站**部署（浏览器直连 `*.onrender.com`，不走反代）时才需要 `SESSION_COOKIE_SAMESITE=none`（`none` 会自动强制 `Secure`，只能在 HTTPS 下用），且 `CORS_ORIGINS` 不能写 `*`（`allow_credentials` 会被关掉，浏览器不携带 Cookie，登录必失败）。

### 按用户隔离的 `settings`

`settings` 主键从 `(key)` 改为 `(user_id, key)`：

- **存量那 5 行全局配置原样保留**在 `user_id = 0`，它是「全局默认模板」，一行没删；
- 新用户自己没有行 → 读到的就是这份全局模板（首次打开就能用）；
- 用户一旦 `PUT /api/settings`，只写自己 `user_id` 的行，**全局模板与其它用户完全不受影响**；
- `/api/stats/trend` 会用**调用者自己的** `small_max` / `normal_max`，不再串号。

迁移脚本（幂等，只做加法，不碰 `draws`）：

```powershell
cd D:\myproject\wave-money
.\backend\.venv\Scripts\python.exe .\backend\scripts\migrate_users_and_per_user_settings.py
```

---

## 部署

前端（Vercel）与后端（Render）通过环境变量互相寻址；域名和密钥一律不进仓库，全部在 Dashboard 配置。

**登录方案 = 同源反代（推荐）**：Vercel 上的 Nuxt 把 `/api/**` 反代到 Render 后端，浏览器只访问前端域名，前后端**同站**。因此 Cookie 用默认 `SameSite=lax`、不需要 `none`，也不需要靠 CORS 传凭证。

### 部署顺序

| 步骤 | 在哪做 | 产出 / 动作 | 给谁用 |
|------|--------|-------------|--------|
| 1 | Supabase Dashboard | `DATABASE_URL`（**必须用 pooler 主机**，见下） | Render 后端 |
| 2 | Render | 建后端 → 得到 `https://<app>.onrender.com` | Vercel 前端 |
| 3 | 本地终端 | 跑 `create_admin.py` 建第一个超管 | 进后台的唯一入口 |
| 4 | Vercel | Root Directory = `frontend`，设 `NUXT_API_PROXY_TARGET` = 后端地址 | 浏览器同源调 API |
| 5 | Render（可选） | 把前端域名回填 `CORS_ORIGINS`（自动重新部署） | 仅直连后端 / 调试时需要 |

> 同源反代下第 5 步**不是必需**（浏览器请求由 Nitro 服务端转发，没有 Origin 头）。仍然建议填上，方便直连 `/docs`、Postman 调试，也让日后改回跨站部署时不用返工。

> ⚠️ **两个最常见的阻塞点**，按顺序排除：
> 1. **数据库连不上** → `DATABASE_URL` 用了直连主机 `db.<ref>.supabase.co`。该主机**只有 IPv6**，Render 出站只有 IPv4，必然超时。必须换成 Supabase **Connection Pooling（pooler）** 主机：`aws-0-<region>.pooler.supabase.com`。
> 2. **登录了但刷新就掉登录** → 前端没走同源反代（`NUXT_PUBLIC_API_URL` 或 `NUXT_API_PROXY_TARGET` 没配好）。见「前端 → Vercel」。

### 后端 → Render

#### 方式 A：Blueprint（推荐，直接读 `render.yaml`）

1. Render Dashboard → **New +** → **Blueprint**
2. 选择仓库 `liyuanchuan-debug/money-app`（`render.yaml` 在仓库根目录，无需填 Root Directory）
3. Render 按 `render.yaml` 创建 Web Service `wave-money-api`，**region 已经是 `singapore`**（对齐 Supabase 首尔，见下方说明）
4. 首次创建时会逐个询问 `sync: false` 的变量，此时粘贴 `DATABASE_URL` 和 `CORS_ORIGINS`

> `sync: false` 就是 Render 保存密钥的方式：值不写进仓库，只在 Dashboard 填。也因为如此，后续再改 `render.yaml` 时这批变量会被忽略，要改直接去 Dashboard 改。

#### 方式 B：手动 Web Service

| 设置项 | 值 |
|--------|-----|
| Repository | `liyuanchuan-debug/money-app` |
| Language | `Python 3` |
| Root Directory | `backend` |
| Build Command | `pip install -r requirements.txt` |
| Start Command | `uvicorn main:app --host 0.0.0.0 --port $PORT` |
| Health Check Path | `/api/health` |
| Instance Type | `Free`（可改） |

**环境变量**

| Key | 值 | 说明 |
|-----|-----|------|
| `PYTHON_VERSION` | `3.13.2` | 已写入 `render.yaml`。Render 新服务默认 3.14.x，而 `asyncpg==0.30.0` 没有 3.14 的预编译 wheel，必须钉在 3.13 |
| `DATABASE_URL` | Supabase **pooler** 连接串 | 密钥，只在 Dashboard 填。必须是 pooler 主机（直连只有 IPv6，Render 连不上），见下 |
| `CORS_ORIGINS` | 前端域名，逗号分隔 | 同源反代下可选（调试用）；见下方「CORS 写法」 |
| `SESSION_SECRET` | `render.yaml` 里 `generateValue: true` 自动生成 | 会话签名密钥。**缺失时后端直接启动失败**（Render 会注入 `RENDER=true`，等价生产环境）。换值会让所有已登录会话失效 |
| `AUTH_ENFORCED` | `true` | API 鉴权总开关；显式 `false` 才是开发旁路 |
| `SESSION_COOKIE_SECURE` | `true` | 已写入 `render.yaml`。uvicorn 默认不信任代理的 `X-Forwarded-Proto`，靠 `request.url.scheme` 自动判可能失效，显式钉死 |
| `SESSION_COOKIE_SAMESITE` | `lax`（默认） | 同源反代方案下保持 `lax`，**不要**写 `none`（那是跨站部署才需要） |
| `region` | `singapore` | 已写入 `render.yaml`。**创建服务后不可修改**，所以必须在第一次创建前就定好；Supabase 在 `ap-northeast-2`（首尔），singapore 是 Render 最近的可选区域，默认 `oregon` 会跨太平洋绕远 |

**Supabase 连接串在哪**：Supabase Dashboard → **Project Settings → Database → Connection string**。选 **Connection pooling**（不是 Direct connection）。

- **必须用 pooler 主机**：`aws-0-<region>.pooler.supabase.com`。直连主机 `db.<ref>.supabase.co` 只解析到 **IPv6**，Render 只有 IPv4 出口，表现是启动时 `Database pool init failed` 超时。
- pooler 用户名是 `postgres.<project-ref>`（注意带项目引用后缀），**不是** 单纯的 `postgres`。
- 端口：`5432` = Session 模式（长连接池，本项目用这个即可）；`6543` = Transaction 模式。
- asyncpg 的 DSN 解析显式接受 `postgresql://` 与 `postgres://` 两种 scheme，Supabase 给的 `postgresql://` 原样粘贴即可，不用改写。
- 密码里若有 `@` `#` `/` 等字符必须先 URL 编码，否则连接串会被解析错。

**CORS 写法**（对应 `backend/main.py` 的 `_cors_config()`）

| `CORS_ORIGINS` | 效果 |
|----------------|------|
| `https://wave-money.vercel.app` | 只放行该域名 |
| `https://*.vercel.app` | 正则放行整个 `vercel.app`，覆盖每次部署都变的 Preview 子域名 |
| `*` | 放行所有来源，同时自动关闭 `allow_credentials` |

> 为什么 `*` 要单独处理：Starlette 在 `allow_origins=["*"]` + `allow_credentials=True` 时，只有请求带 Cookie 才会回显真实 Origin；普通响应返回的是 `Access-Control-Allow-Origin: *` 加 `Access-Control-Allow-Credentials: true`，而浏览器对这种组合下的携带凭证请求会直接拒绝。所以 `*` 分支显式关掉凭证。生产环境建议写真实域名，Preview 用 `https://*.vercel.app`。
>
> 同源反代下浏览器不直连后端，这段 CORS 只影响直连调试，写真实前端域名或 `https://*.vercel.app` 都行；只有**跨站**方案才必须严格配对 Cookie 的 `SameSite`。

### 建第一个管理员（必做，否则没人能进后台）

新注册用户一律 `PENDING`，**必须由管理员审批才能登录**，而第一个管理员只能从命令行直接建。部署完后端、`DATABASE_URL` 已通之后：

```powershell
cd D:\myproject\wave-money
$env:ADMIN_PHONE='13800138000'
$env:ADMIN_PASSWORD='换成你的强密码'
.\backend\.venv\Scripts\python.exe .\backend\scripts\create_admin.py
```

- 脚本**直连数据库**（读 `backend/.env` 的 `DATABASE_URL`），不受 `AUTH_ENFORCED` / 后端服务影响，随时可救回后台。
- **幂等**：手机号已存在则提升为 `ADMIN` + `APPROVED`；不加 `--password` 不会改动已有密码。
- 云上执行不便时，也可在渲染机 / 本地用生产 `DATABASE_URL` 跑同一条命令。

### 前端 → Vercel

1. Vercel → **Add New → Project** → Import Git Repository 选 `liyuanchuan-debug/money-app`
2. **Root Directory** 设为 `frontend`（**必须**；不设的话 Vercel 在仓库根目录找不到 `package.json`）
3. **Framework Preset** 选 `Nuxt.js`（一般会自动识别）。Build Command 保持默认 `nuxt build`，Output Directory 不要手填
4. **Environment Variables** 加一条（同源反代方案只需这一条）：

| Key | 值 |
|-----|-----|
| `NUXT_API_PROXY_TARGET` | Render 后端地址，如 `https://wave-money-api.onrender.com`（**结尾不要带 `/`**） |

`NUXT_PUBLIC_API_URL` **留空**（或干脆不设）：空串 = 浏览器打同源 `/api/*`，再由 Nuxt 反代到 `NUXT_API_PROXY_TARGET`，Cookie 落在前端域名上，登录最稳。

> ⚠️ `NUXT_API_PROXY_TARGET` 是**构建期**变量：它的值在 `nuxt build` 时被烤进 Nitro 的 `routeRules` 代理规则（见 `frontend/nuxt.config.ts`），不是运行时读取。**改完必须 Redeploy**（关掉构建缓存重建），只改环境变量不重新部署不会生效。

5. Deploy

> 本项目不需要 `frontend/vercel.json`。Nitro 检测到 `VERCEL` 环境变量后会自动切到 `vercel` preset，产物本身就是 Vercel Build Output API 的结构，Vercel 的 Nuxt 预设也已覆盖 Build Command 与输出目录；额外写 `vercel.json` 只是重复默认值，还可能和自动检测冲突。

> 小提示：`frontend/package-lock.json` 的 `resolved` 地址全部指向 `registry.npmmirror.com`（国内镜像）。Vercel 构建会按 lockfile 去该镜像取包，镜像公开可访问、通常能装上，只是比官方源慢；若 install 阶段超时，可在 Vercel 环境变量里加 `NPM_CONFIG_REGISTRY=https://registry.npmjs.org` 与 `NPM_CONFIG_REPLACE_REGISTRY_HOST=always`，或本地用官方源重建 lockfile。

### 验证部署

| 检查 | 地址 / 位置 | 期望结果 |
|------|-------------|----------|
| 健康检查 | `https://<render-app>.onrender.com/api/health` | `{"status":"ok"}` |
| 接口文档 | `https://<render-app>.onrender.com/docs` | Swagger UI 正常打开 |
| 存储后端 | `https://<render-app>.onrender.com/api/export` | 返回体含 `"storage": "postgres"`（不是 `memory`） |
| 数据库连通 | Render → Logs | `Active storage backend: postgres`（看到 `memory` 说明 DSN 有问题，常见是没用 pooler 主机） |
| 前端页面 | `https://<vercel-app>.vercel.app` | 能录入开奖号并看到推荐结果 |
| 同源反代 | DevTools → Network → 任一 `/api/...` 请求 | Request URL 是**前端自己的域名**（`https://<vercel-app>.vercel.app/api/...`），不是 `onrender.com`；状态 200 |
| 会话 Cookie | DevTools → Application → Cookies | 存在 `wm_session`，`Secure` + `SameSite=Lax`，Domain 是前端域名 |
| 匿名只读 | 无痕窗口打开前端首页 | `/api/draws`、`/api/history` 等只读接口正常返回（不要求登录） |
| 登录态保持 | 登录后刷新 / 重开浏览器 | 仍是登录状态（掉登录 = 反代没生效，跨站了） |

后端启动日志（Render → Logs）里确认连的是 Supabase 还是内存回退：

```text
Active storage backend: postgres   # 已连上 Supabase，数据持久化
Active storage backend: memory     # 回退内存存储，进程重启即丢数据
```

看到 `memory` 时往上看一行：`DATABASE_URL not set` 说明变量没配上；`Database pool init failed` 说明连接串有问题（最常见是密码没 URL 编码，或 Supabase 项目处于暂停状态）。

环境变量一律通过各平台 Dashboard 配置，不硬编码。
