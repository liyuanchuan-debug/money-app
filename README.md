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

最新开奖号的所有同肖号码称为**重肖**，推荐号必须避开。

### 波动规则（相邻两期差值绝对值）

| 类型 | 条件 | 枚举值 |
|------|------|--------|
| 小波动 | 差值 ≤ 10 | `small` |
| 常规波动 | 10 < 差值 ≤ 30 | `normal` |
| 大跳 | 差值 > 30 | `big` |

阈值可通过 `PUT /api/settings` 调整（`small_max` / `normal_max`）。

### 推荐规则

1. 避开最新一期的开奖号本身
2. 避开最新开奖号的**全部重肖号码**
3. 推荐 3 个号码，默认每注 10 元，总金额 30 元
4. 三个号码尽量分散在小波动 / 常规波动 / 大跳三类中
5. 某类波动从最新号出发无解时（例如最新号为 21 时无大跳号），结果中明确标注「本期大跳无号」
6. **波动回补逻辑**（上期波动 → 本期侧重顺序）：

   | 上期波动 | 本期侧重顺序 |
   |----------|--------------|
   | 小波动 | 常规波动 → 大跳 → 小波动 |
   | 常规波动 | 小波动 → 大跳 → 常规波动 |
   | 大跳 | 小波动（回补） → 常规波动 → 大跳 |

7. 每个推荐号标注：与最新开奖号的差值、所属波动类型、是否重肖、侧重等级（主推 / 次选 / 防守）

**候选池内部排序**：先取历史遗漏最久的（历史出现次数最少），再取与最新号差值最小的。保证结果可复现。

**注数补齐**：若某类波动无解导致不足 3 注，从仍有候选的池中补齐（允许同类多取一个）。

### 侧重与金额

| 模式 | 枚举值 | 分配 | 总额 |
|------|--------|------|------|
| 均注 | `even` | 3 个号各 10 元 | 30 元 |
| 侧重 | `weighted` | 主推 20 元，次选 5 元，防守 5 元 | 30 元 |
| 单挑 | `single` | 1 个号 30 元 | 30 元 |

### 一键复制格式（竞猜投注串）

```
均注：09,19,31 各10元 共30元
侧重：09 主推20元,19 次选5元,31 防守5元 共30元
单挑：09 30元
```

### 数据存储

| 表 | 字段 |
|----|------|
| `records` | `id`, `number`, `created_at` |
| `settings` | `key`, `value` |

- 支持导入 / 导出 JSON
- 支持清空历史

> 未配置 `DATABASE_URL` 时后端退化为**内存存储**（进程重启即丢失），仅用于本地预览。生产环境必须配置 Supabase 连接串，否则历史无法持久化。

---

## 本地启动

### 1. 数据库（Supabase）

1. 在 [supabase.com](https://supabase.com) 创建项目
2. 打开 **SQL Editor**，执行 [`schema.sql`](./schema.sql)
3. 从 **Project Settings → Database** 复制连接串

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
```

启动：

```powershell
.\.venv\Scripts\uvicorn main:app --reload --port 8000
```

启动日志会打印当前存储后端（`postgres` 或 `memory`）。

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
| GET | `/api/records?limit=10` | 历史开奖号（倒序） |
| POST | `/api/records` | 新增开奖号 `{"number": 21}`，号码须在 1-49 |
| DELETE | `/api/records` | 清空历史 |
| GET | `/api/settings` | 读取配置 |
| PUT | `/api/settings` | 更新 `small_max` / `normal_max` / `bet_unit` / `mode` |
| POST | `/api/recommend` | 生成推荐；body `{"mode":"even","number":21}` 均可选 |
| GET | `/api/export` | 导出 JSON |
| POST | `/api/import?replace=true` | 导入 JSON |
| GET | `/api/items` | 示例数据（脚手架保留） |

`/api/recommend` 返回示例（最新号 21、上期 11、侧重模式）：

```json
{
  "latest": 21,
  "previous": 11,
  "prev_wave": { "number": 11, "diff": 10, "type": "small", "label": "小波动" },
  "copy_text": "10 主推20元,20 次选5元,32 防守5元 共30元",
  "picks": [
    { "number": 10, "diff": 11, "wave_type": "normal", "role": "primary",
      "role_label": "主推", "amount": 20, "is_repeat_zodiac": false }
  ],
  "missing_waves": [{ "type": "big", "label": "大跳", "note": "本期大跳无号" }]
}
```

---

## 部署

### 后端 → Render

1. 推送仓库到 GitHub
2. Render 用 `render.yaml` 创建 Blueprint（或手动建 Web Service，Root Directory 设 `backend`）
3. Dashboard 配置环境变量：
   - `DATABASE_URL` = Supabase 连接串
   - `CORS_ORIGINS` = 你的 Vercel 域名

启动命令（已写入 `render.yaml`）：

```bash
uvicorn main:app --host 0.0.0.0 --port $PORT
```

### 前端 → Vercel

1. 导入 GitHub 仓库
2. **Root Directory** 设为 `frontend`
3. Framework 自动识别为 Nuxt
4. 环境变量：`NUXT_PUBLIC_API_URL` = Render 后端地址（不带结尾斜杠）

环境变量一律通过各平台 Dashboard 配置，不硬编码。
