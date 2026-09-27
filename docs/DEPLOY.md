# 部署手册（手把手版）

面向**不熟悉命令行、不熟悉云平台**的使用者：从注册账号开始，一步一步把「四叶沙盘」部署到线上。

- 本文只讲**怎么点、怎么填、怎么验证**，不涉及代码改动。
- 所有命令都是 **Windows PowerShell**，可以直接复制粘贴（不要带行号）。
- **本文不含任何真实密钥**：所有密码、域名、连接串一律写成占位符，请按提示替换成你自己的。
- 仓库 `README.md` 里有一份「部署」速查表，本文是它的**逐步展开版**：遇到细节看本文。

### 本次部署的既定前提（先说清楚）

| 项目 | 选择 |
|------|------|
| 数据库 | **已经有的 Supabase 生产库**（已建表、已有数据），本次**不用**新建、**不用**再跑 `schema.sql` |
| 后端 | Render（**免费档**，接受冷启动休眠） |
| 前端 | Vercel（**免费档 / Hobby**） |
| 域名 | 用平台默认域名，不用自己的域名 |
| 登录方案 | **同源反代**：浏览器只访问 Vercel 域名，Nuxt 在服务端把 `/api/**` 转发给 Render 后端 |
| Cookie 策略 | `SESSION_COOKIE_SAMESITE=lax`（默认值，**不要改**） |

> 预计总用时：**30～45 分钟**。其中「等 Render 部署」和「等 Vercel 构建」大部分是干等。

---

## 0. 总览：三条腿分别干什么

```text
   浏览器（手机 / 电脑）
        │   只访问 https://<你的项目>.vercel.app
        ▼
 ┌──────────────────────────────────────────────┐
 │ 1) Vercel —— 前端（Nuxt 3）                  │
 │    渲染页面；并在服务端把 /api/** 转发给后端   │
 └──────────────────────────────────────────────┘
        │   Nuxt(Nitro) 服务端反代 /api/**
        │   → https://<你的-render-地址>.onrender.com
        ▼
 ┌──────────────────────────────────────────────┐
 │ 2) Render —— 后端（FastAPI + uvicorn）       │
 │    所有业务接口、登录会话 Cookie、权限校验      │
 └──────────────────────────────────────────────┘
        │   asyncpg 连接池（用 Supabase 连接池主机）
        ▼
 ┌──────────────────────────────────────────────┐
 │ 3) Supabase —— 数据库（PostgreSQL）          │
 │    开奖记录、用户与角色、每人设置、采用快照     │
 └──────────────────────────────────────────────┘
```

一句话版：

- **Vercel**：给用户看的网页，顺便当「门房」把 `/api` 请求转给后端。
- **Render**：真正干活的接口服务，判断你是谁、能不能用某个功能。
- **Supabase**：把数据存住，重启服务也不会丢。
- **反代的意义**：浏览器只跟 Vercel 打交道，前端和后端**在浏览器看来是同一个网站**，所以登录 Cookie 用默认的 `lax` 就能稳定持久登录，不需要跨站那套麻烦配置。

### 六个步骤的先后顺序（不要乱序）

| 步骤 | 在哪做 | 产出 |
|------|--------|------|
| 1 | render.com / vercel.com | 注册两个账号（用 GitHub 登录） |
| 2 | Supabase Dashboard | 一条**连接池（pooler）**连接串 |
| 3 | Render Dashboard | 后端跑起来 → 拿到 `https://xxx.onrender.com` |
| 4 | **本机 PowerShell**（Render 免费实例没有 Shell） | 第一个**超管账号**（没有它谁都进不了后台） |
| 5 | Vercel Dashboard | 前端跑起来 → 拿到 `https://xxx.vercel.app` |
| 6 | Render Dashboard | 把前端域名回填 `CORS_ORIGINS` |

---

## 1. 第一步：注册 Render 和 Vercel

### 1.1 注册 Render

1. 打开 <https://dashboard.render.com/register>
2. **选「GitHub」登录**（`Sign up with GitHub` / `Continue with GitHub`），然后点 `Authorize Render`。
   - 为什么用 GitHub：两个平台都要从 GitHub 拉代码，用同一个账号最省事，也少记一套密码。
3. 会让你建一个 **Workspace（工作区）**：名字随便填（英文），个人使用保持默认即可。
   - 工作区的**计划（plan）选 `Hobby`**（$0/月，免费的），不要选 Pro / Scale。
   - 注意区分两个词：**工作区计划**是 Hobby；而具体服务的**实例类型（Instance Type）**是 `Free`——本项目在 `render.yaml` 里已经写成 `plan: free`，你不用手动选。
4. **不需要绑信用卡**：Render 的「Free 实例类型」不需要付款方式（只有你自己主动升级付费实例、或超出免费额度时才需要）。
5. 授权仓库：Render 会装一个 GitHub App，问你能访问哪些仓库。
   - 选 `Only select repositories`，勾上 `liyuanchuan-debug/money-app`。
   - **私有仓库必须授权**；公开仓库同样需要授权 Render 才能拉代码。

> 免费档要知道的：免费 Web Service **15 分钟没有访问就会休眠**，下一次访问要**等约 1 分钟**才能唤醒。这是免费档的正常行为，不是网站坏了。

### 1.2 注册 Vercel

1. 打开 <https://vercel.com/signup>
2. **选「Continue with GitHub」**，点 `Authorize Vercel`。
3. 问你团队 / 计划时选 **Hobby（Free）**——个人项目用的免费档，不要选 Pro。
4. 同样会让 Vercel 的 GitHub App 授权，勾上 `liyuanchuan-debug/money-app`。

> Hobby 档定位是**个人非商业项目**。如果你的用途是商业运营，之后可能需要升级计划——这一步不影响本次部署。

---

## 2. 第二步：从 Supabase 拿连接串（这一步最容易出错）

### 2.1 为什么必须用「连接池（pooler）」主机

Supabase 给你两种连接方式：

| 方式 | 主机长什么样 | Render 能用吗 |
|------|--------------|---------------|
| 直连（Direct connection） | `db.<项目引用>.supabase.co:5432` | ❌ **连不上**。这个域名**只解析到 IPv6 地址**，而 Render 免费实例只有 IPv4 出口，结果就是连接一直超时 |
| **连接池（Session pooler / Supavisor）** | `aws-0-<区域>.pooler.supabase.com:5432` | ✅ **用这个**。它就是为「只有 IPv4 的网络」准备的，端口 `5432` 是会话模式，长连接池对本项目正合适 |

看到 `Database pool init failed` / `Active storage backend: memory` 这类现象，九成是这里用错了主机。

### 2.2 在 Dashboard 哪里找

1. 打开你的 Supabase 项目。
2. 点页面**右上角的 `Connect` 按钮**（老界面也可以走：左侧齿轮 `Project Settings` → `Database` → `Connection string`）。
3. 在连接串面板里，**选 `Session pooler`（会话池，端口 `5432`）**，不要选 `Direct connection`，也不用选 `Transaction pooler`（6543）。
4. 选 **`URI`** 那个页签，复制那串，形如：

   ```text
   postgresql://postgres.<项目引用>:[YOUR-PASSWORD]@aws-0-<区域>.pooler.supabase.com:5432/postgres
   ```

5. 把 `[YOUR-PASSWORD]` 替换成你的数据库密码（找不到就在 `Project Settings → Database` 里重置一个）。

> **注意主机名的前缀**：常见是 `aws-0-<区域>`，但新项目的 Dashboard 也可能显示 `aws-1-<区域>`。
> **判断标准只有一条：主机名里必须带 `pooler.supabase.com`**，不能是 `db.<项目引用>.supabase.co`。
> 本仓库现有生产库的区域是 `ap-northeast-2`（首尔），所以主机形如 `aws-0-ap-northeast-2.pooler.supabase.com`。

### 2.3 密码里有特殊字符怎么办（必须 URL 编码）

连接串是「一个网址」，密码里的特殊字符会**破坏网址结构**。密码里如果出现 `@ # / : ? & %` 这类字符（尤其是 `@`），必须先编码。

在 PowerShell 里跑这一行（把引号里的内容换成你的真实密码）：

```powershell
[System.Uri]::EscapeDataString('你的数据库密码')
```

输出的那串就是「编码后的密码」，用它替换连接串里的 `[YOUR-PASSWORD]`。

**然后拼出最终连接串并复制到剪贴板**（把两个占位符都换成你的值）：

```powershell
$dbpass = [System.Uri]::EscapeDataString('你的数据库密码')
$DATABASE_URL = "postgresql://postgres.<项目引用>:$dbpass@aws-0-ap-northeast-2.pooler.supabase.com:5432/postgres"
Set-Clipboard -Value $DATABASE_URL
Write-Output $DATABASE_URL
```

> 密码里只有字母和数字时，编码前后是一样的，可以直接粘贴 Dashboard 给的原串。
> 本机 `backend\.env` 里那串 `DATABASE_URL` 已经是**正确的 pooler 形态**，如果你确认它就是生产库，可以**整串直接复制**到 Render（但**不要**把它提交进 git，`.env` 已在 `.gitignore` 里）。

---

## 3. 第三步：把后端部署到 Render

仓库根目录已经放好了 `render.yaml`（Render 的「蓝图 / Blueprint」文件），所以**推荐用 Blueprint 方式**：变量、区域、启动命令都已经写在文件里，你只需要填两个值。

`render.yaml` 里已经钉好的关键设置（**不用你手改**）：

| 设置 | 值 | 为什么 |
|------|-----|--------|
| 服务名 | `wave-money-api` | 决定默认域名前缀 |
| 根目录 | `backend` | 只构建后端 |
| 计划 | `free` | 免费档 |
| 区域 | `singapore` | **创建后不能改**。数据库在首尔，新加坡是 Render 离得最近的可选区域 |
| 构建命令 | `pip install -r requirements.txt` | |
| 启动命令 | `uvicorn main:app --host 0.0.0.0 --port $PORT` | 端口由 Render 注入，**不是**写死的 8000 |
| 健康检查 | `/api/health` | |
| `PYTHON_VERSION` | `3.13.2` | 依赖 `asyncpg==0.30.0` 没有 Python 3.14 的预编译包，必须钉 3.13 |
| `AUTH_ENFORCED` | `true` | 权限校验总开关，线上**必须**是 true |
| `SESSION_COOKIE_SECURE` | `true` | 代理后面自动判断协议不可靠，显式钉死 |
| `SESSION_COOKIE_SAMESITE` | `lax` | 同源反代方案的标准值，**不要**改成 `none` |
| `JWT_EXPIRE_DAYS` | `365` | 登录后一年内有效（持久登录） |

### 3.1 创建服务

1. Render Dashboard 右上角 **`New +`** → **`Blueprint`**。
2. 选择仓库 `liyuanchuan-debug/money-app`，分支用默认的 `main`。
3. Render 会自动读到仓库根目录的 `render.yaml`，显示要创建 1 个 Web Service（`wave-money-api`），并**逐个问你那两个没写在文件里的变量**（第 3.2 节）。
4. 点 `Apply` / `Create`，开始第一次构建（约 3～6 分钟）。

> 如果创建时**被要求添加付款方式**：先确认你没有误选付费实例类型；仍被拦的话，可以改用**方式 B**（见 3.4）手动建服务，效果一样。

### 3.2 创建时问你的变量：每个填什么

| 变量名 | 填什么 | 从哪拿 |
|--------|--------|--------|
| `DATABASE_URL` | 第二步拼好的**完整连接串**（`postgresql://postgres.<项目引用>:<编码后密码>@aws-0-<区域>.pooler.supabase.com:5432/postgres`） | 第二步；本机 `backend\.env` 里也有一份可用值 |
| `CORS_ORIGINS` | **先占位**：`https://example.vercel.app`（创建向导一般不让留空）。第六步再回来改成真实 Vercel 域名 | 第六步 |

**只有这两个**会被问，其它变量都在 `render.yaml` 里写好了，这是正常的。

关于 `SESSION_SECRET`（会话签名密钥）：

- `render.yaml` 里写的是 `generateValue: true`，**Render 会自动生成并长期保存**，创建时**不会问你**。
- **千万不要**把本机 `backend\.env` 里那串 `SESSION_SECRET` 贴上去。本地那串是开发用的，且**换值会让所有已登录用户的会话立刻失效**。
- 线上如果缺这个值，后端会**直接启动失败**（这是故意的保护）。

### 3.3 部署完拿到后端地址

服务页顶部会显示地址，形如 `https://wave-money-api.onrender.com`。

- 如果 `wave-money-api` 这个名字被别的用户占了，Render 会自动加后缀（例如 `https://wave-money-api-a1b2.onrender.com`）——**一律以页面上实际显示的为准**，第五步要原样填进 Vercel。
- 第一次请求可能因为冷启动要等约 1 分钟；**先发一次健康检查把服务唤醒**：

```powershell
Invoke-RestMethod 'https://<你的-render-地址>.onrender.com/api/health'
```

期望输出：`status` 为 `ok`。

顺手看一眼 Render 的 **Logs** 页，应该能看到这一行（在 4.3 节会再确认一次）：

```text
Active storage backend: postgres
```

### 3.4 方式 B：手动建 Web Service（Blueprint 出问题时的备选）

| 设置项 | 值 |
|--------|-----|
| Repository | `liyuanchuan-debug/money-app` |
| Language / Runtime | `Python 3` |
| Branch | `main` |
| **Root Directory** | `backend` |
| Build Command | `pip install -r requirements.txt` |
| Start Command | `uvicorn main:app --host 0.0.0.0 --port $PORT` |
| Health Check Path | `/api/health` |
| Instance Type | **Free** |
| Region | `Singapore`（**创建后不可改**） |

环境变量按 3.2 那张表填 `DATABASE_URL`、`CORS_ORIGINS`，并把上表 3.1 那一组（`PYTHON_VERSION=3.13.2`、`AUTH_ENFORCED=true`、`SESSION_COOKIE_SECURE=true`、`SESSION_COOKIE_SAMESITE=lax`、`JWT_EXPIRE_DAYS=365`）也一条条加上，`SESSION_SECRET` 用 Render 的「Generate」按钮生成。

---

## 4. 第四步：建第一个超管（必做，否则没人能进后台）

**为什么必须做**：新注册的用户一律是 `PENDING`（待审批）状态，**只有管理员审批过才能登录**；而第一个管理员不可能由「审批」产生——只能绕过 HTTP 直接连数据库创建。

仓库已经准备好了这个脚本：`backend/scripts/create_admin.py`（**幂等**：手机号已存在就把它提升为 `ADMIN` + `APPROVED`，不会重复建号）。

脚本特点：

- 直连数据库，**不看** `AUTH_ENFORCED`，任何时候都能拿回后台权限；
- 手机号必须是 **11 位中国大陆手机号**；密码至少 **6 位**；
- **绝不打印密码**，输出里手机号也是脱敏的。

### 4.0 ⚠️ 先说一条免费档的限制：Render 免费实例没有 Shell

Render 官方文档明确写着：**免费（Free）实例不支持 Shell / SSH**，网页版 `Shell` 页签只在付费实例上可用。所以「在 Render 页面里开个终端敲命令」这条路，**免费档走不通**。

因此本章给两种方式，按你的情况选：

- **方式 A（本机跑）**：免费档下的**唯一可行方式**，也是推荐方式，一次点鼠标都不用。
- **方式 B（Render Shell 跑）**：命令照样给出，留给你**将来把实例升级到付费档**（Starter 起，约 $7/月）时使用——付费实例才有 Shell 页签。

两种方式做的事完全一样，都是「直连数据库建号」，随便哪种都能达到目的。

### 4.1 方式 A：在自己电脑上跑（免费档用这个）

前提：本机有 `backend\.venv`（Windows 开发环境已装好，Python 3.13.2 + asyncpg 0.30.0，与 Render 上一致）。

打开 PowerShell，逐行复制粘贴：

```powershell
# 1) 进项目目录
cd D:\myproject\wave-money

# 2) 让脚本连生产库（把占位符换成第二步拼好的那串）
#    如果你的 backend\.env 里已经是生产库连接串，这一步可以直接跳过
$env:DATABASE_URL = 'postgresql://postgres.<项目引用>:<编码后密码>@aws-0-ap-northeast-2.pooler.supabase.com:5432/postgres'

# 3) 管理员手机号 + 强密码（密码至少 6 位）
$env:ADMIN_PHONE = '13800138000'
$env:ADMIN_PASSWORD = '换成你的强密码'

# 4) 建号（幂等，可重复跑）
.\backend\.venv\Scripts\python.exe .\backend\scripts\create_admin.py

# 5) 跑完清掉这个窗口里的明文变量（推荐）
Remove-Item Env:DATABASE_URL, Env:ADMIN_PHONE, Env:ADMIN_PASSWORD
```

期望输出（**不会**显示密码，手机号也是脱敏的）：

```text
管理员账号就绪（幂等）：
  手机号        : 138****8000
  - 新建账号
  - 角色 USER → ADMIN
  - 状态 PENDING → APPROVED
  角色 / 状态   : ADMIN / APPROVED
  已审批管理员数: 1
```

几个要点：

- **环境变量优先于**脚本读到的 `backend\.env`，所以第 2 步这样写就能指定目标库；不写则用 `backend\.env` 里的。
- 脚本**不需要** `SESSION_SECRET`，也**不依赖** Render 后端是否在跑——它只连数据库。
- 只想把已有账号提权、**不改密码**（例如前任管理员离职后收回权限）：

  ```powershell
  .\backend\.venv\Scripts\python.exe .\backend\scripts\create_admin.py --phone 13800138000 --promote-only
  ```

- `--phone` / `--password` 传参也能用，但密码会留在 shell 历史里，**不推荐**，优先用环境变量。
- 万一本机 venv 丢了需要重建：

  ```powershell
  cd D:\myproject\wave-money\backend
  python -m venv .venv
  .\.venv\Scripts\python.exe -m pip install -r requirements.txt
  ```

### 4.2 方式 B：在 Render 的 Shell 里跑（仅付费实例可用）

> 免费档**看不到** Shell 页签（见 4.0），本节命令在升级到付费实例后才用得上。付费实例的 Shell 页签在服务页左侧。

```bash
# 1) 进入后端目录（Render 把代码放在 /opt/render/project/src）
cd /opt/render/project/src/backend

# 2) 只有在下一步报 ModuleNotFoundError: asyncpg 时，才需要先跑这一行
source /opt/render/project/src/.venv/bin/activate

# 3) 传手机号和密码（不要写成命令行参数，免得留在历史里）
export ADMIN_PHONE='13800138000'
export ADMIN_PASSWORD='换成你的强密码'

# 4) 建号（幂等，可重复跑）
python scripts/create_admin.py

# 5) 跑完关掉这个 Shell 标签页；想清掉会话历史可以顺手
history -c
```

- Render 的 Shell 会自动带上服务里配置的环境变量（包括 `DATABASE_URL`），**不用**再手填连接串。
- 如果第 1 步报 `No such file or directory`：先跑 `ls` 看清自己在哪个目录，多半已经在项目根，改成 `cd backend` 即可。

### 4.3 建完之后

1. 脚本输出里出现 `角色 / 状态 : ADMIN / APPROVED`、`已审批管理员数: 1` 就算成功。
2. 这一步**不用**等前端：脚本只连数据库。等第五步前端部署好之后，再打开 `https://<你的-vercel-域名>/login` 用这个手机号 + 密码登录验证（见第 7 节第 6 项）。
3. **以后**新用户注册后（默认 `PENDING`），直接在 `/admin/users` 点「通过 / 拒绝 / 改角色」即可，**不用**再跑脚本。

---

## 5. 第五步：把前端部署到 Vercel

### 5.1 导入项目

1. Vercel Dashboard → **`Add New...`** → **`Project`** → `Import Git Repository`。
2. 选仓库 `liyuanchuan-debug/money-app`。
3. 在 `Configure Project` 页面：

| 设置项 | 填什么 | 说明 |
|--------|--------|------|
| Project Name | 默认即可（一般会带出 `money-app`） | 决定默认域名前缀 |
| **Root Directory** | **`frontend`** | ⚠️ **必须改**。点 `Edit` 填进去。不改的话 Vercel 在仓库根找不到 `package.json`，构建直接失败 |
| Framework Preset | `Nuxt.js`（一般会自动识别） | 识别不对才手动选 |
| Build Command | 保持默认 | 不要手填 |
| Output Directory | 保持默认（留空） | 不要手填 |
| Install Command | 保持默认 | 不要手填 |

4. 展开 **Environment Variables**，加一条（**在第一次部署之前就加**）：

| Key | Value | Environments |
|-----|-------|--------------|
| `NUXT_API_PROXY_TARGET` | `https://<你的-render-地址>.onrender.com` | 三个都勾：Production / Preview / Development |

⚠️ **结尾不要带 `/`**。

5. 点 `Deploy`，等 2～4 分钟。

### 5.2 `NUXT_API_PROXY_TARGET` 为什么必须勾三个环境、为什么必须 Redeploy

- 这条变量是**构建期**变量：它的值在 `nuxt build` 时被**烤进** Nuxt 的 `/api/**` 反代规则（见 `frontend/nuxt.config.ts` 的 `routeRules`），**不是**运行时读取。
- 所以：**改完这个变量必须 Redeploy（重新部署）才生效**。只在设置里改值、不重新部署，线上仍然按旧值走——表现为前端页面能打开、但所有接口都 500。
- Preview / Development 也勾上，是为了以后每次预览部署都能连上后端。

另外 `NUXT_PUBLIC_API_URL` **保持不设或留空**：空表示「浏览器打同源 `/api`」，正是不用跨站配置、登录最稳的方案。

### 5.3 把 Node 版本钉到 22

Vercel 新项目默认用 **Node 24.x**，本项目要钉在 **22.x**（本机开发也是 22）。

1. 项目 → **Settings** → **Build and Deployment** → **Node.js Version** → 选 **`22.x`**。
2. 该设置**只对之后的部署生效**，所以保存后再重新部署一次：
   `Deployments` → 最新那条 → 右侧 `...` → **`Redeploy`**（**不要**勾选使用构建缓存的选项，直接重建）。

> 也可以在你的 `package.json` 里写 `engines.node` 来覆盖，但那是改代码；本项目不改代码，用上面的下拉框即可。

### 5.4 拿到前端地址

项目页顶部 / `Settings → Domains` 会显示默认域名，形如 `https://<项目名>.vercel.app`。

- 若该名字已被占用，Vercel 会给一个带后缀的域名（例如 `https://money-app-abc123.vercel.app`）——**以实际显示的为准**。
- **把这个域名原样记下来**，第六步要用。

---

## 6. 第六步：回填 CORS（把占位换成真实域名）

1. Render Dashboard → 服务 `wave-money-api` → 左侧 **Environment**。
2. 找到 `CORS_ORIGINS`，把占位值改成真实 Vercel 域名（**逗号分隔，不要带结尾斜杠**）：

   ```text
   https://<你的-项目名>.vercel.app
   ```

   想让 Vercel 的每次「预览部署」（域名带随机后缀）也能直连后端调试，可以再补一条通配：

   ```text
   https://<你的-项目名>.vercel.app,https://*.vercel.app
   ```

3. 点 **`Save Changes`**。Render 会**自动重新部署**（约 1～3 分钟）；如果没自动跑，去 `Manual Deploy` → `Deploy latest commit`。

**解释一下为什么这一步「不是必需、但建议做」**：同源反代下，浏览器根本不直接访问 Render，请求是 Nuxt 服务端发起的（没有浏览器 `Origin` 头），所以 CORS 用不上。填上它的价值是：以后你想在浏览器里直接打开 `https://<你的-render-地址>.onrender.com/docs`（FastAPI 自带的接口文档）、或者用 Postman 调试时，不会被浏览器拦。

⚠️ **绝对不要**把 `CORS_ORIGINS` 写成单独的 `*`：代码里 `*` 会**自动关闭** `allow_credentials`，浏览器就不再携带登录 Cookie，表现为「登录看着成功，下一个请求又变回未登录」。

---

## 7. 验证清单（部署完逐条打勾）

| # | 检查项 | 怎么查 | 期望结果 |
|---|--------|--------|----------|
| 1 | 后端健康检查 | PowerShell：`Invoke-RestMethod 'https://<你的-render-地址>.onrender.com/api/health'` | `status` = `ok`（第一次可能要等约 1 分钟冷启动，超时就再试一次） |
| 2 | 存储后端是数据库（**最关键**） | Render → 服务 → **Logs**，找启动行 | `Active storage backend: postgres`。若是 `memory` → 说明连接串有问题（见第 8 节），数据不会留存 |
| 3 | 存储后端二次确认 | 用超管登录前端后，浏览器直接打开 `https://<你的-vercel-域名>/api/export` | 返回的 JSON 里 `"storage": "postgres"`（顺便证明反代通了） |
| 4 | 访客能看公开页 | 开一个**无痕窗口**，依次打开 `/`、`/history`、`/zodiac`、`/stats/trend`、`/stats/frequency`、`/stats/zodiac-trend` | 都能正常显示数据，不被要求登录 |
| 5 | 访客被挡在个人能力页外 | 无痕窗口依次打开 `/recommend`、`/settings`、`/stats/pnl`、`/stats/backtest`、`/entry`、`/draws`、`/admin/users` | 一律跳转到 `/login` |
| 6 | 超管能登录 | `/login` 用第四步的手机号 + 密码登录 | 登录成功；能打开 `/admin/users` 看到用户列表（新注册的人在这里点「通过」才能登录） |
| 7 | 持久登录（反代是否真的生效） | 登录后**刷新页面**、再**关掉浏览器重开** | 仍然是登录状态。若刷新就掉登录 → 见第 8 节 |
| 8 | 反代确实走的是前端域名 | F12 → Network → 随便点一个 `/api/...` 请求 | `Request URL` 是**你自己 Vercel 的域名**（`https://xxx.vercel.app/api/...`），**不是** `onrender.com`；状态码 200 |
| 9 | 会话 Cookie 正确 | F12 → Application → Cookies | 有 `wm_session`，标记 `Secure` + `SameSite=Lax`，Domain 是 Vercel 域名 |
| 10 | 纠正开奖 | 登录管理员 → `/draws`（或 `/entry`）→ 对某一期点「纠正」，改一个号 | 回执显示新旧特码，并给出「重算条数」（`resettled_rounds`） |
| 11 | 赔率与模拟收益仪表 | `/settings` 设置赔率（默认 47）→ `/stats/pnl` | 页面显示当前赔率、模拟成本 / 模拟兑付 / 累计模拟盈亏；数据不足时显示「数据不足」提示而不是报错 |
| 12 | 冷启动是否符合预期 | 隔 20 分钟不访问，再打开前端 | 第一次请求等约 1 分钟属于**免费档正常现象** |

> 第 3、10、11 条需要登录（`/api/export`、`/stats/pnl` 需要 VIP 及以上；纠正开奖需要 ADMIN）。用第四步建的超管账号即可。

---

## 8. 常见坑（按现象查）

| 现象 | 最可能的原因 | 怎么修 |
|------|--------------|--------|
| **页面上一片 401**（所有接口未登录/失效） | ① 后端没重启，环境变量没生效；② `SESSION_SECRET` 变了（换值会让所有会话失效）；③ 反代没生效导致请求其实跨站了 | 先看第 7 节第 8 项确认请求域名；再在 Render 点 `Manual Deploy → Deploy latest commit` 让变量生效；最后确认自己确实登录过（F12 看有没有 `wm_session`） |
| **线上所有接口 500**（页面能打开，数据全空） | `NUXT_API_PROXY_TARGET` 没设，或者设了但**没有 Redeploy**（它是构建期变量，旧构建里还指向 `http://127.0.0.1:8000`） | 去 Vercel `Settings → Environment Variables` 确认值**不带结尾 `/`**、三个环境都勾了；然后 `Deployments → Redeploy` **重建**，不要只改运行时 |
| **登录成功，但一刷新就掉登录** | ① 把 `SESSION_COOKIE_SAMESITE` 错设成了 `none`；② `CORS_ORIGINS` 写成了 `*`（凭证被关掉）；③ `NUXT_PUBLIC_API_URL` 被填成了 onrender 地址，导致浏览器直连后端（跨站） | 三处都按本文恢复：`SESSION_COOKIE_SAMESITE=lax`、`CORS_ORIGINS` 写真域名（**不要**单独 `*`）、`NUXT_PUBLIC_API_URL` 留空。反代方案**不需要**跨站配置 |
| **后端日志报 `Database pool init failed`，随后 `Active storage backend: memory`** | `DATABASE_URL` 用了直连主机 `db.<项目引用>.supabase.co`（只有 IPv6，Render 连不上）；或密码没做 URL 编码；或 Supabase 项目被暂停 | 换成 `aws-0-<区域>.pooler.supabase.com:5432` 的 **Session pooler** 串；密码用 `[System.Uri]::EscapeDataString()` 编码；去 Supabase 看项目是否 Active |
| **后端启动就崩，日志说 `SESSION_SECRET 未配置`** | 用了手动方式建服务但漏了 `SESSION_SECRET` | 在 Render `Environment` 里加 `SESSION_SECRET`，点 Render 自带的「Generate」生成（**不要**填本地那串） |
| **首次访问要等很久才出页面** | Render 免费实例 15 分钟无流量就休眠，冷启动约 1 分钟 | 正常现象，接受即可。要消除只能升级付费实例 |
| **Vercel 构建卡在 install / 超时** | `frontend/package-lock.json` 里的下载地址全部指向国内镜像 `registry.npmmirror.com`，Vercel 取包偏慢 | 在 Vercel 加两个环境变量后 Redeploy：`NPM_CONFIG_REGISTRY=https://registry.npmjs.org`、`NPM_CONFIG_REPLACE_REGISTRY_HOST=always` |
| **本地要手测接口（不登录也能通）** | 线上有权限校验，本地想省事 | 仅本地开发时临时 `AUTH_ENFORCED=false` **做旁路**，跑完改回 `true`。线上**永远不要**设成 false：那等于所有接口（含 `/api/admin/*`）无需登录就能访问 |
| **Render 服务页找不到 `Shell` 页签** | 免费实例**不支持** Shell / SSH（官方限制），不是你的账号有问题 | 用第 4.1 节：在**本机** PowerShell 里跑 `create_admin.py`。想用 Shell 只能把实例升级到付费档（Starter 起） |
| **Render 免费额度** | 每个工作区每月 750 实例小时，免费档够跑**一个**常驻服务 | 别在同一个工作区再开第二个常驻免费 Web Service |

---

## 9. 回滚（出问题怎么退回去）

| 层 | 怎么回滚 | 说明 |
|----|----------|------|
| 前端（Vercel） | `Deployments` → 找到**上一个成功的生产部署** → 右侧 `...` → `Promote to Production`（或对该部署点 `Redeploy`） | 前端回滚**秒级**生效，不需要重新构建 |
| 后端（Render） | 服务 → `Deploys` → 找到**上一个成功的部署** → 菜单里选 `Rollback`；没有该选项就点该部署的 `Redeploy` | 回滚后留意环境变量仍是当前值 |
| 数据库（Supabase） | **本轮部署没有任何破坏性 DDL**：后端启动时的 `ensure_schema()` 只跑 `CREATE TABLE IF NOT EXISTS` / `ADD COLUMN IF NOT EXISTS` 这类幂等语句，不改也不删任何已有数据 | 也就是说**不需要**回滚数据库。真要恢复数据，去 Supabase Dashboard 的 `Database → Backups` 看你项目实际可用的备份能力（免费档能力有限，重要的库建议自己定期用 `/api/export` 导出 JSON） |

回滚后**务必重跑第 7 节的验证清单**，特别是第 2、6、7 项。

---

## 10. 哪些必须手动做、哪些以后可以自动化

### 必须手动做一次（一次性）

1. 注册 Render 和 Vercel 账号 + 授权 GitHub App（涉及交互式登录，无法脚本化）。
2. 在 Supabase Dashboard 复制 Session pooler 连接串、URL 编码密码。
3. Render Blueprint 创建时填 `DATABASE_URL` / `CORS_ORIGINS` 两个变量。
4. **建第一个超管**（`create_admin.py`）——因为「没有管理员就没法审批管理员」，这一步在设计上就必须人工执行；免费档没有 Render Shell，**在本机 PowerShell 里跑**（第 4.1 节）。
5. Vercel 的 `Root Directory=frontend`、`NUXT_API_PROXY_TARGET`、**Node 22**（都是平台侧的界面设置）。
6. 第六步回填 `CORS_ORIGINS`（可选但建议）。
7. 用第 7 节清单验收一遍。

### 以后可以自动化 / 优化（不急）

| 事项 | 说明 |
|------|------|
| 审批新用户 | 已有管理员后就不需要命令行：在 `/admin/users` 页面点「通过 / 拒绝 / 改角色」。 |
| 后端变量与重部署 | 已经 Blueprint 化：以后改 `render.yaml`（非 `sync: false` 的项）就是「改文件即部署」，不用再点界面。 |
| 域名与预览环境 | 想给前端预览部署开直连调试，`CORS_ORIGINS` 加一条 `https://*.vercel.app` 一次覆盖所有预览域名。 |
| 冷启动 | 想消除休眠只能升级 Render 付费实例（免费档无解）。 |
| 上线前自检 | 可以把第 7 节的 1～5 项写成一个 `scripts/check_deploy.ps1`（健康检查 + 存储后端 + 反代），每次部署后跑一次。 |
| 数据备份 | 定期调 `/api/export` 导出 JSON 存档；这是当前成本最低的备份手段。 |
| 自定义域名 | 以后若买域名，Vercel 加域名 + 平台自动签 HTTPS；**同源反代方案下后端什么都不用改**。 |

---

## 附：本文用到的占位符对照

| 占位符 | 含义 |
|--------|------|
| `<项目引用>` / `<PROJECT_REF>` | Supabase 的项目引用（Dashboard URL 里那串小写字母数字） |
| `<区域>` | Supabase 项目所在区域，本项目是 `ap-northeast-2`（首尔） |
| `<你的-render-地址>` | 例如 `wave-money-api.onrender.com`（以 Render 页面实际显示为准） |
| `<你的-项目名>` / `<你的-vercel-域名>` | 例如 `money-app.vercel.app`（以 Vercel 页面实际显示为准） |
