# 部署到公网（Supabase + 后端 + 前端）

> 后端 Railway 或 Render 二选一（见「二」和「二之二」），
> 前端 Vercel 或 Netlify 二选一（见「三」和「三之二」），四种组合都验证过。

## 📍 当前进度（2026-08，接手的人先看这段）

代码侧**已经全部就绪并验证过**，剩下的全是在三个网页控制台里填环境变量。

已确认的项目信息：
- Supabase 组织 `ValueBetAi`，项目 `Valuebetai`
- Project URL：`https://tsvdgrhrwhqdsamdpl….supabase.co`（前缀已确认，完整串在控制台）
- Publishable key：`sb_publishable_PW3ah5HXgfgAhTNRFM98BA_IGvoaTQq`（公开值）
- **新版密钥体系**（控制台显示 "Publishable and secret API keys"），
  所以大概率是 ES256 非对称签名，走 JWKS 那条路

还没拿到的：
- [ ] 数据库连接串（Connect → Direct → **Session pooler**，不要 Direct connection，
      那个是 IPv6 only、Railway 连不上）
- [x] 签名方式已不需要确认（见下）
      **这一步不用做了。** 实测该端点返回 "Secret API key required"——
      取公钥反而要存一个高权限密钥，本末倒置。后端已改成把令牌交给
      Supabase 的 /auth/v1/user 去验，跟签名算法无关，
      只要 `SUPABASE_URL` + `SUPABASE_ANON_KEY`（都是公开值）。

  > 不带 `?apikey=` 会返回 `{"message":"No API key found in request"}`——
  > Supabase 把整个 /auth/v1 放在 API 网关后面。后端取公钥时也必须带这个头，
  > 已经处理（见 `backend/app/auth.py` 的 `_jwk_client`）。

踩过并已修的坑（别再走一遍）：
- Supabase 给的连接串是 `postgres://`，SQLAlchemy 2.x 只认 `postgresql://`
- 直连 5432 是 IPv6 only，Railway 连不上，要用 pooler
- JWKS 端点要 apikey 头，而且**只认 secret key**——所以干脆不走本地验签了
- 只支持 HS256 的话新项目会全部 401，且报错看不出是算法问题

---


部署完就能在世界任何地方用，不需要家里电脑开着。

**本地一键启动完全不受影响** —— 不配任何环境变量时，代码走 SQLite + 无认证，
跟以前一模一样。云端和本地是同一份代码，靠环境变量分叉。

---

## 一、Supabase（数据库 + 登录）

1. supabase.com 建项目，记住数据库密码。
2. **Project Settings → Database → Connection string → URI**，
   选 **Connection pooling** 那个（端口 **6543**），复制下来。

   > 不要用直连的 5432。Railway 的实例会反复重启，每次重启都留下没关掉的连接，
   > 直连很快就会撞上 Supabase 的连接数上限，表现为随机 500。

3. **Project Settings → API**，抄下三个值：
   - `Project URL` → 前端要用
   - `anon public` key → 前端要用（公开的，打进前端包里没关系）
   - 后端验签用的值，**看你的项目是哪一代**：
     - 设置页有 **JWT Settings → JWT Secret** → 旧项目，抄这个（私密，绝不能放前端）
     - 只有 **JWT Signing Keys**、找不到 JWT Secret → 新项目（2025 年后建的都是），
       后端改填 `SUPABASE_URL=https://xxxxx.supabase.co` 就行，它会自己去取公钥，
       不需要任何私密值

     > 新项目用 ES256 非对称签名。如果后端只支持旧的 HS256，部署完会所有请求 401，
     > 而报错写的是「令牌无效」，看不出真正原因是算法不匹配。两条路都已支持。

4. **Authentication → Providers → Email** 打开。
   如果不想收确认邮件，把 **Confirm email** 关掉，注册完直接能登录。

---

## 二、Railway（后端）

1. railway.app → New Project → Deploy from GitHub repo，选这个仓库。
2. 仓库根目录已有 `railway.json`，构建配置不用管。
3. **Variables** 里加三个（参考 `backend/.env.example`）：

   ```
   DATABASE_URL      = 第一步复制的 6543 连接串
   FRONTEND_ORIGINS  = https://你的项目.vercel.app

   # 下面二选一：
   SUPABASE_URL        = https://xxxxx.supabase.co     ← 新项目用这两个
   SUPABASE_ANON_KEY   = sb_publishable_xxxxx
   # SUPABASE_JWT_SECRET = 你的jwt密钥                ← 旧项目用这个
   ```

   > `FRONTEND_ORIGINS` 可以等 Vercel 部署完拿到域名再回来填。

4. 部署完拿到形如 `https://xxx.up.railway.app` 的地址。
   访问 `https://xxx.up.railway.app/api/health` 应该返回：

   ```json
   {"ok": true, "auth_enabled": true}
   ```

   **`auth_enabled` 必须是 `true`。** 如果是 `false`，说明 `SUPABASE_JWT_SECRET`
   没配上，接口是裸奔的，立刻去补。

   > 其实配错了根本起不来——后端有个启动自检：一旦发现用的是远程数据库
   > 却没有 JWT 密钥，会直接拒绝启动并打出原因。这是故意的：宁可部署失败，
   > 也不要一个「看起来正常但所有人都能读你实盘记录」的服务。

---

## 二之二、Render（后端的另一个选择）

Railway 的免费额度是**一次性 30 天试用**，用完就得付费。Render 有永久免费档，
代价是闲置 15 分钟后休眠、下次访问要等约 50 秒冷启动。两边配置几乎一样。

1. render.com → New → **Web Service** → 选这个仓库。
2. 构建设置：

   | 项 | 填什么 |
   |---|---|
   | Root Directory | `backend` |
   | Build Command | `pip install -r requirements.txt` |
   | Start Command | `uvicorn app.main:app --host 0.0.0.0 --port $PORT` |

   > Start Command 必须带 `--port $PORT`。`backend/Procfile` 里那行没写端口
   > （Railway 能自己探测），Render 只认 `$PORT`，不写起不来。

3. **环境变量**：跟 Railway 那节完全一样的四个（`DATABASE_URL`、`SUPABASE_URL`、
   `SUPABASE_ANON_KEY`、`FRONTEND_ORIGINS`），外加**必须**加这一个：

   ```
   PYTHON_VERSION = 3.12.10
   ```

   ### ⚠️ 不加这个，构建必然失败——这是真踩过的坑

   Render 新建服务默认用最新的 Python（2026-08 时是 3.14），而
   `requirements.txt` 里锁的两个包**在 3.14 上没有预编译 wheel**：

   - `pydantic==2.9.2` → 依赖 `pydantic-core 2.23.4`，pip 只能去源码编译。
     它是 Rust 写的，要走 maturin + cargo，而 Render 构建环境的
     cargo 目录是只读的，于是报一串看不出所以然的错：
     `Read-only file system (os error 30)` → `maturin failed` →
     `metadata-generation-failed`。
     **报错里一个字都没提 Python 版本**，很容易往依赖冲突的方向查偏。
   - `psycopg2-binary==2.9.10` → 3.14 上压根没有这个版本的 wheel
     （最低要 2.9.11）。这个还没轮到报错，前一个就先挂了。

   实测确认过（三个版本各跑一遍 `pip download --only-binary=:all:`）：

   | Python | pydantic-core 2.23.4 | psycopg2-binary 2.9.10 |
   |---|---|---|
   | 3.12 | ✅ 有 wheel | ✅ 有 wheel |
   | 3.13 | ✅ 有 wheel | ✅ 有 wheel |
   | 3.14 | ❌ 要源码编译 | ❌ 没有 |

   仓库根目录的 `.python-version` 写的就是 `3.12.10`，但**别指望它**——
   Render 主要认 `PYTHON_VERSION` 这个环境变量。根目录那个 `runtime.txt`
   是 Heroku 的约定，Render 不读，所以它写着 3.12 也拦不住这个问题。

   > 想彻底摆脱版本锁，得把 `pydantic` 和 `psycopg2-binary` 升到有 3.14
   > wheel 的版本。但 pydantic 跨小版本有过 breaking change，升级要重跑
   > 一遍接口验收，不是改个数字就完事——现在没有非升不可的理由。

4. 部署完同样访问 `/api/health`，必须是 `{"ok": true, "auth_enabled": true}`。

---

## 三、Vercel（前端）

1. vercel.com → New Project → 选同一个仓库。
2. Root Directory 保持默认（仓库根目录）即可——根目录的 `vercel.json` 会
   进 `frontend/` 构建、把 `frontend/dist` 作为产物发布。

   > 设成 `frontend` 也能用，那时生效的是 `frontend/vercel.json`，两份配置等价。
   >
   > 之前踩过的坑：Root Directory 留在根目录、而根目录又没有 `vercel.json` 时，
   > Vercel 因为找不到 `package.json` 不会把它当前端项目，直接把仓库根目录
   > 当静态站点发布。构建显示 **Ready**，但根目录没有 `index.html`，
   > 打开任何路径都是 **404: NOT_FOUND**——是「成功地发布了空站点」，
   > 不是构建失败，所以日志里看不出问题。根目录这份 `vercel.json` 就是为了
   > 堵死这个失败模式：无论 Root Directory 指哪边都能构建出正确的产物。

3. **Environment Variables** 加三个（参考 `frontend/.env.example`）：

   ```
   VITE_API_BASE          = https://xxx.up.railway.app/api      ← 末尾带 /api
   VITE_SUPABASE_URL      = https://xxxxx.supabase.co
   VITE_SUPABASE_ANON_KEY = eyJhbGciOi...
   ```

   > `VITE_` 开头的变量是**打包时**替换进代码的，不是运行时读的。
   > 所以改完这三个值必须重新构建（Deployments → 最新那条 → `···` → Redeploy，
   > 取消勾选 Build Cache）才会生效，光在设置页保存没有任何作用。

4. 部署完拿到 `https://你的项目.vercel.app`，回 Railway 把这个域名填进
   `FRONTEND_ORIGINS`（带 `https://`，结尾不要斜杠——CORS 是精确字符串比对），
   然后 Railway 会自动重启。

   注意用**生产域名**访问。Vercel 每个 preview 部署都有独立域名，不在白名单里，
   浏览器会拦掉它的跨域请求。

---

## 三之二、Netlify（前端的另一个选择）

这套前端是纯静态的 Vite 打包产物，没用到任何 Vercel 专属功能，换成
Netlify 部署完全等价——两边可以**同时留着**，`FRONTEND_ORIGINS` 支持填
多个域名（逗号分隔），不需要二选一。实际动机：Vercel 的域名在部分网络
环境下连不上，Netlify 连得上，就留一份 Netlify 的链接给连不上的人用。

1. 仓库根目录已有 `netlify.toml`，构建配置不用管：

   ```toml
   [build]
     base = "frontend"
     command = "npm install && npm run build"
     publish = "dist"          # 相对 base 算，不是相对仓库根目录

   [[redirects]]
     from = "/*"
     to = "/index.html"
     status = 200
   ```

   > 踩过的坑：`publish` 一旦设了 `base`，路径就是相对 `base` 算的，
   > 写成 `"frontend/dist"` 会被解析成 `frontend/frontend/dist`，部署时
   > 报"目录不存在"。用 Netlify 自己那个解析配置的包（`@netlify/config`）
   > 实际跑过一遍才发现，不是查文档猜的。

2. app.netlify.com → **Add new site → Import an existing project** →
   选这个仓库。Netlify 会自动读到 `netlify.toml`，构建设置那几栏
   （Base directory / Build command / Publish directory）应该已经
   自动填好，不用手动改。

3. **Site configuration → Environment variables** 加三个，跟 Vercel 那边
   一模一样：

   ```
   VITE_API_BASE          = https://xxx.up.railway.app/api      ← 末尾带 /api
   VITE_SUPABASE_URL      = https://xxxxx.supabase.co
   VITE_SUPABASE_ANON_KEY = eyJhbGciOi...
   ```

   同样是**打包时**替换进代码的，改完要重新部署（Deploys → Trigger
   deploy → Clear cache and deploy site）才会生效。

4. 部署完拿到 `https://随机名字.netlify.app`（可以在 Site configuration →
   Domain management 里改成自己取的名字）。回 Railway 的 `FRONTEND_ORIGINS`
   把这个域名**追加**进去（逗号分隔，不要替换掉 Vercel 那个）：

   ```
   FRONTEND_ORIGINS = https://你的项目.vercel.app,https://随机名字.netlify.app
   ```

   Railway 会自动重启。两个域名都能正常登录、都能拉到数据。

---

## 四、验收

打开 Vercel 的域名，应该看到登录页。注册一个账号，登录，能看到六个赛事的数据。

再验一下**没登录时接口是关着的**——这是整件事的重点：

```bash
curl -s -o /dev/null -w "%{http_code}\n" https://xxx.up.railway.app/api/matches
# 必须是 401。如果是 200，认证没生效，立刻检查 SUPABASE_JWT_SECRET。
```

---

## 数据迁移？不用

比赛、预测、回测数据全都能从 openfootball 重新抓，第一次启动会自动跑一遍。
只有你自己的注单记录（虚拟盘/实盘/价格记录）是本地独有的——如果本地已经记了
一些想带过去，可以之后单独导，但现在这些表是空的，直接部署就行。

---

## 费用

| 服务 | 费用 |
|---|---|
| Supabase | 免费额度够用。**但免费项目闲置 7 天会被暂停**，暂停后第一次访问要手动唤醒 |
| Railway | 约 $5/月起（没有长期免费额度了） |
| Vercel | 个人使用免费 |

Supabase 那个 7 天暂停是唯一容易踩的坑：如果你有一阵子没用，回来发现连不上，
去 Supabase 控制台点一下恢复即可。

---

## 一件必须知道的事

部署到公网意味着**你的实盘记录、资金曲线、下注历史都在别人的服务器上**。
认证挡住了未登录的人，但 Supabase 和 Railway 的运维是能看到数据库内容的。
如果你介意这一点，用局域网 + 隧道（Cloudflare Tunnel / Tailscale）
是更保守的选择——数据一直在你自己机器上，代价是家里电脑要开着。
