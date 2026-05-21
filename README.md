# CF IP Monitor

针对 Cloudflare Anycast IP 的分布式智能优选系统。

## 解决什么问题

- Cloudflare 同一个 IP 在电信 / 联通 / 移动 看到的路由完全不同
- 同一个 IP 在不同时段也可能进入不同 colo 或被限速
- 全量 ~150 万 个 IP 扫一遍太慢, 利用 "C 段共命运" 特性提前剪枝
- 优选结果要能自动同步到智能 DNS, 不能纯靠手动
- **要看到完整线路 (163 / CN2-GIA / 9929 / 9808 / CMI / ...) 和出口城市, 不只是落地 colo**

## 架构

```
+-------------+        +-----------------------------------+
|  Agent (CT) |<------>|                                   |
+-------------+        |     Master                        |
+-------------+        |  +-----------+   +-------------+  |
|  Agent (CU) |<------>|  | Scheduler |-->|  Strategy   |  |
+-------------+        |  +-----------+   +-------------+  |
+-------------+        |        |              |           |
|  Agent (CM) |<------>|     SQLite        Scoring         |
+-------------+        |        |              |           |
                       |        |          Enrichment      |
                       |        |     (5 源 ASN/Geo/ISP)   |
                       |        |              |           |
                       |        |          Labeling        |
                       |        |     (路径线路自动打标)    |
                       |        v              v           |
                       |  TextExporter   HuaweiCloudDNS    |
                       +-----------------------------------+
```

- **Master** 部署在一台公网可达的机器（家里 NAS / 国内云均可）, 负责: 计划探测、收结果、评分、IP 富化、路由打标、输出
- **Agent** 部署在电信 / 联通 / 移动 VPS 上, **不需要开放任何端口**, 主动拉任务回报
- Agent <-> Master 之间走 HTTPS + Bearer token
- 协议 v2 (新增 TRACEROUTE 探测 + ping 分位统计)

## 探测策略

| 阶段 | 任务类型 | 触发频率 | 作用 |
| --- | --- | --- | --- |
| 1. 采样 | TCP_PING `.1 .65 .129 .193` | 每 `full_scan_hours` (默认 24h) | 找出哪些 /24 段是 "活的" |
| 2. 展开 | 全段 TCP_PING | **事件驱动**: 段被判定 alive 立即触发 | 拿到完整候选 IP 列表 |
| 3. 识别 | HTTPS /cdn-cgi/trace | 采样命中即触发 | 拿到 colo (NRT/HKG/LAX/...) |
| 4. 测速 | /__down?bytes=50MB | 每 15 分钟, 取低延迟 Top 20% (可关闭) | 验证带宽 >= 10 MB/s |
| 5. 路径 | TCP traceroute → 443 | 每 24h, 仅对 best snapshot 头部 200 IP | 拿到 ASN/出口/线路标签 |
| 6. 输出 | TextFile + HuaweiCloud DNS | 每 30 分钟 | 推送优选结果 |

C 段早停 + 沉默缓存 (默认 6h TTL) 让全量扫描的实际任务数大幅减少。

### Ping 准确性 (v2)

旧版 TCP_PING 只取 3 次平均, **首次握手偏高会污染 avg**。新版:
- `warmup=1` 次 (默认): 结果不计入统计, 只用来"热"链路
- `retry=6` 次: 正式样本
- 同时存 `latency_min / p50 / p95 / avg / jitter_ms`, scoring 用 **p50** 而非 avg
- jitter (p95-p50) 大的 IP 在评分中会被打折扣 (`max_jitter_ms`)

数据全部进 `probe_raw` 表, 任何延迟的 IP 都不会被丢弃。`scoring.max_latency_ms=300` 是**输出过滤**, 而不是入库过滤。

### 测速可独立关闭

测速会触发 CF 限流 / 国内运营商 QoS, 所以 `master.speed_test.enabled` 是一个独立总开关:

- `enabled: true`  (默认): 完整模式, 跑上面所有阶段
- `enabled: false`: 跳过阶段 4, 评分降级为 "无测速模式"

### Traceroute + 路由分析 (v2 新增)

`master.traceroute.enabled=true` (默认) 时, 调度器会:

- 每 `interval_hours` (默认 24h) 对每个 ISP 的 best snapshot IP × `top_n_per_isp` 跑一次 traceroute
- 每跳 IP 通过**多源融合 IP 库**做 enrichment
- 自动派生:
  - `line_type`: CN2-GIA / CN2-GT / CU-9929 / CMI / CT-163 / CU-4837 / CM-9808 / 未知
  - `exit_city`: 国内最后一跳城市 (广州/上海/北京/...)
  - `asn_path`: 路径 ASN 序列
  - `quality`: 0~1 的线路质量分

代价: 单次 traceroute 5-15s, 单 agent concurrency=30 时 200 个 IP 约 1-2 分钟。
要求 agent 端安装 `traceroute` 系统命令 (`apt install traceroute` 或 `brew install traceroute`)。

## IP 数据库 (多源融合)

放在 `src/cf_ip_monitor/ipdata/`:

| 文件 | 来源 | 用途 | 是否必须 |
| --- | --- | --- | --- |
| GeoLite2-ASN.mmdb | MaxMind | ASN 主源 (RIR 数据) | 是 |
| GeoLite2-Country.mmdb | MaxMind | 国家兜底 | 可选 |
| GeoLite2-City.mmdb | MaxMind | 城市兜底 | 可选 |
| dbip-city-ipv4.mmdb | sapics/ip-location-db | **国际段主力** (实测 CF 命中 100%) | 推荐 |
| ip2region_v4.xdb | lionsoul2014/ip2region | **国内段 + ISP 主力** (结构化 5 字段) | 推荐 |
| qqwry.dat | metowolf/qqwry.dat | ISP 中文兜底 | 可选 |

**查询优先级链**:

```
ASN     : GeoLite2-ASN  ->  ip2region.ISP (text fallback)
Country : dbip-city  ->  GeoLite2-Country  ->  ip2region  ->  qqwry
Region/City:
  [CN]   ip2region (结构化省/市)  ->  qqwry (文本)  ->  dbip-city  ->  MaxMind
  [非CN] dbip-city  ->  MaxMind  ->  ip2region
ISP_CN  : ip2region.ISP  ->  qqwry 文本里挖 (电信/联通/移动/教育网/...)
```

**为什么搞这么复杂?** MaxMind 免费版在 CF 段实测 country 60% / city 24%, 单源不能用。
dbip-city 在 CF 段 country/city 都是 100%, ip2region 在国内段 ISP 字段是结构化的 (而不是 qqwry 那样的文本描述), 三库互补正好覆盖全场景。

### 下载地址

```bash
# MaxMind (需要注册免费账号)
# https://www.maxmind.com/en/geolite2/signup

# dbip-city-ipv4
wget https://github.com/sapics/ip-location-db/raw/main/dbip-city-mmdb/dbip-city-ipv4.mmdb \
    -O src/cf_ip_monitor/ipdata/dbip-city-ipv4.mmdb

# ip2region (体积小, 11MB)
wget https://github.com/lionsoul2014/ip2region/raw/master/data/ip2region.xdb \
    -O src/cf_ip_monitor/ipdata/ip2region_v4.xdb

# qqwry
wget https://github.com/metowolf/qqwry.dat/releases/latest/download/qqwry.dat \
    -O src/cf_ip_monitor/ipdata/qqwry.dat
```

## 评分

完整模式 (`speed_test.enabled=true` + `route_quality_enabled=true`):

```
score = 0.5 * lat_norm * jitter_penalty
      + 0.3 * spd_norm
      + 0.2 * route_quality

lat_norm        = 1 - latency_p50 / max_latency_ms
spd_norm        = min(speed/min_speed, 3) / 3
jitter_penalty  = max(0.5, 1 - jitter_ms / max_jitter_ms)
route_quality   = 由 line_type 派生; CN2-GIA=1.0 / CU-9929=0.95 / CN2-GT=0.85 /
                  CU-Global=0.85 / CMI=0.8 / CT-163=0.55 / 未知=0.4
```

无测速 / 纯延迟模式:

```
score = 0.7 * lat_norm * jitter_penalty + 0.3 * route_quality
```

门槛 (与之前一致):
- latency_p50 <= max_latency_ms
- speed_required=True 时还要求 speed_p50 >= min_speed_mbps

输出按 `(ISP × 地区)` 分桶, 每桶 top N 个。

## 任务调度与断点续传

### 轮次模型 (v2)

每次 `sample_round` 触发会创建一个 `scan_round_id` (UUID), 所有任务都带这个 id。

- `scheduler.full_scan_hours=24` (默认): 一轮周期, 适配 CF 当前规模 (~5950 个 /24)
- **上一轮未跑完时**, 新一轮触发会跳过, 避免任务叠加
- 全部 stage 跑完后 `scan_round.state -> done`

### 事件驱动的 alive → expand

旧版本展开触发依赖"最近 6 分钟" 时间窗——master 中断 >6 分钟会丢失展开机会。
新版本: `c_segment_state.expanded_round` 标记, master 重启后会**主动找出**当前轮内 alive 但未展开的段, 一次性补齐。

### 进度查询

```bash
# 当前轮进度
curl -H "Authorization: Bearer $TOKEN" http://master:8088/v1/round/current

# 历史轮次
curl -H "Authorization: Bearer $TOKEN" http://master:8088/v1/round/list?limit=10
```

返回示例:

```json
{
  "round_id": "8f3a...",
  "started_at": 1779000000000,
  "state": "running",
  "by_stage": {
    "sample":     {"电信": {"done": 23808}},
    "expand":     {"电信": {"pending": 250000, "assigned": 5000, "done": 800000}},
    "http_trace": {"电信": {"done": 60000}}
  }
}
```

### 任务清理

`housekeeping` 每 5 分钟运行:
- requeue assigned 状态卡住 >15 min 的任务
- 删除 `done` 状态 >24h 的任务 (`cleanup_done_after_hours`)

## 一轮全量扫描需要多久?

按 CF 当前 ~5950 /24 段, 单 ISP `concurrency=30`:

| 阶段 | 任务量 | wall time |
| --- | --- | --- |
| 采样 | 24K | 20-40 min |
| 展开 (假设 70% 段 alive) | ~1.06M | 5-10 h |
| HTTP_TRACE | ~600K | 3-6 h |
| 测速 | ~10K | 30-60 min |
| **合计** (排除测速) | ~1.7M | **8-16 h** |
| Traceroute (best snapshot 200 IP/ISP) | 600 | **1-2 min** |

想加速: 提高 `agent.concurrency` (60-120) 或部署更多 agent。

## 输出格式

文本文件 (符合 `IP#备注` 习惯):

```
162.159.43.99#CF优选-电信-日本
104.16.1.2#CF优选-电信-日本
141.101.115.7#CF优选-联通-香港
```

华为云 DNS 智能解析: 同一个域名 (如 `cf.example.com`) 按线路 ID 区分:
- `Dianxin` -> 电信优选 IP 列表
- `Liantong` -> 联通优选 IP 列表
- `Yidong` -> 移动优选 IP 列表

## 部署

项目依赖**统一由 [uv](https://docs.astral.sh/uv/) 管理**, 权威依赖文件是 `pyproject.toml` + `uv.lock`。**不要使用 `pip` / `python -m venv` / `requirements.txt`**。

部署形态:

- **首选: Docker + uv 自动化部署** (Dockerfile 内部用 uv 装依赖, compose 一键起)
- **备选: 裸金属 systemd + uv** (无 Docker 环境时)

### 0. 三种网络场景概览

`config.yaml` 里 `agent.master_url` / `agent.auth_token` / `agent.isp` / `agent.node_name` 与 `master.auth_token` 都默认是 `${VAR}` 占位符, 真值通过环境变量注入。优先级: **CLI > `${VAR}` 展开 > 环境变量 > config 字面值 > 内置默认**。

```
场景 A: 同机 Docker compose (master + agent 在一台机器上, 一个 compose project)
┌──────────────── 一台机 (cf-ip-monitor compose) ────────────────┐
│  ┌─────────────┐                  ┌──────────────────────┐   │
│  │   master    │ ← service name → │  agent-ct / cu / cm  │   │
│  │ :8088 (in)  │   "master:8088"  │                      │   │
│  └─────────────┘                  └──────────────────────┘   │
│       │                                                       │
│       └── 宿主机 ${MASTER_PORT:-8088} (可选直接暴露)            │
└───────────────────────────────────────────────────────────────┘
默认 AGENT_MASTER_URL=http://master:8088

场景 B: 本机 master + 公网 / 内网 IP 直连 (无 nginx 域名)
┌─ master 主机 ─┐                ┌─ agent VPS ─┐
│  master :8088 │ ←─── HTTP ───  │   agent      │
│  暴露 0.0.0.0 │   (公网/内网)  │              │
└───────────────┘                └──────────────┘
agent 端: AGENT_MASTER_URL=http://<MASTER_IP>:8088

场景 C: 公网 master + nginx 域名 + TLS (推荐生产)
┌────── master 主机 ──────┐                ┌─ agent VPS ─┐
│  ┌───────┐   ┌────────┐ │                │              │
│  │ nginx │ ← │ master │ │ ← HTTPS 域名 ─ │   agent      │
│  │ :443  │   │ :8088  │ │  (Let's Enc)   │              │
│  └───────┘   └────────┘ │                │              │
│  ↑ 暴露 80/443          │                │              │
└─────────────────────────┘                └──────────────┘
agent 端: AGENT_MASTER_URL=https://master.example.com
```

### 1. 一次性准备

```bash
# 安装 uv (本机或服务器)
curl -LsSf https://astral.sh/uv/install.sh | sh

# 克隆代码
git clone <repo> cf_ip_monitor
cd cf_ip_monitor

# 准备配置 (默认全是 ${VAR} 占位符, 真值都在 .env)
cp config.example.yaml config.yaml
cp deploy/docker/.env.example .env
$EDITOR .env                          # 至少改 MASTER_AUTH_TOKEN

# 下载 4 个 IP 离线库到 src/cf_ip_monitor/ipdata/ (见上文「IP 数据库」)
# (Docker 会把这个目录挂载到容器内 /app/ipdata, 不打进镜像)
```

`.env` 至少要填的:

```bash
MASTER_AUTH_TOKEN=<长随机字符串>      # 必填, master 与 agent 用同一个

# 场景 A (同机) 通常这一项就够了, agent_token 留空会自动用 master_token
AGENT_MASTER_URL=http://master:8088   # 默认值, 不必改

# 场景 B / C 时
# AGENT_MASTER_URL=https://master.example.com 或 http://<MASTER_IP>:8088
# AGENT_AUTH_TOKEN=<与 MASTER_AUTH_TOKEN 同值>
```

> **安全约束**: `config.yaml` 与 `.env` 已写入 `.gitignore`/`.dockerignore`, 切勿入库。
> `config.yaml` 中所有敏感字段都支持 `${VAR}` 占位符, 实际值通过 `.env` → compose → 容器环境变量注入。

### 1. Docker + uv (推荐)

镜像采用多阶段构建: builder 阶段使用 `ghcr.io/astral-sh/uv` 基础镜像跑 `uv sync --frozen --no-dev --no-editable`, 把项目作为 wheel 装进 `/app/.venv`; runtime 阶段直接复用这个 venv。Dockerfile 通过 `target` 区分 `master` / `agent` 两个镜像。

#### 1.1 一键脚本

```bash
chmod +x scripts/docker-deploy.sh

# 同机部署 (master 与 agent 在同一台机器)
./scripts/docker-deploy.sh master              # 仅 Master
./scripts/docker-deploy.sh master+ct           # Master + 电信 Agent
./scripts/docker-deploy.sh master+cu           # Master + 联通 Agent
./scripts/docker-deploy.sh master+cm           # Master + 移动 Agent
./scripts/docker-deploy.sh all                 # Master + 三网 Agent

# 跨机部署: 单独跑一个 Agent (Master 在别处)
AGENT_MASTER_URL=https://master.example.com:8088 \
AGENT_ISP=电信 \
AGENT_NODE_NAME=ct-hk-01 \
    ./scripts/docker-deploy.sh agent

# 在已运行的 Master 旁边追加一个本机 Agent
./scripts/docker-deploy.sh agent-ct            # 只起 docker-compose.yml 的 agent-ct
./scripts/docker-deploy.sh agent-cu
./scripts/docker-deploy.sh agent-cm

# 运维
./scripts/docker-deploy.sh status              # 看所有服务状态
./scripts/docker-deploy.sh logs                # 跟随全部日志
./scripts/docker-deploy.sh logs master         # 只看 master
./scripts/docker-deploy.sh down                # 全停
./scripts/docker-deploy.sh --help              # 完整说明
```

脚本会自动: 缺 `config.yaml` 从示例复制 → 缺 `.env` 从示例复制 → 缺 `uv.lock` 调 `uv lock` → `docker compose build` → `up -d`。

> 区别提醒:
> - `agent-ct/cu/cm` 走 `docker-compose.yml` (与 master 共用 compose project, 通过服务名 `master` 互联);
> - `agent` 走 `docker-compose.agent.yml` (独立 compose project, 通过 `AGENT_MASTER_URL` 显式指定远端 master)。

#### 1.2 Makefile

```bash
make sync           # 本地 uv sync (开发用)
make lock           # uv lock (改了 pyproject 后)
make build          # docker compose build
make up-master      # 起 Master
make up-agents      # 起所有 Agent (compose profile=agents)
make logs           # 跟随日志
make ps             # 状态
make down           # 全停
```

#### 1.3 手动 compose

```bash
uv lock                                  # 首次或改依赖后
docker compose build
docker compose up -d master              # 只跑 Master
docker compose --profile agents up -d    # 同机三网 Agent
```

容器内挂载与目录:

| 容器内路径 | 来源 | 说明 |
| --- | --- | --- |
| `/app/config.yaml` | bind mount `./config.yaml` (ro) | 改完热重启: `docker compose restart master` |
| `/app/ipdata` | bind mount `./src/cf_ip_monitor/ipdata` (ro) | 由 `ENV CF_IPDATA_DIR=/app/ipdata` 指向, **IP 库不打进镜像** |
| `/app/data` | named volume `master-data` | SQLite 持久化 |
| `/app/output` | named volume `master-output` | 优选文本输出 |

#### 1.4 公网 master + nginx 反代 (场景 C)

```bash
# 0) 解析域名到 master 主机
#    A 记录: master.example.com -> <你的服务器 IP>

# 1) 准备 nginx 配置
cp deploy/nginx/master.conf.example deploy/nginx/master.conf
sed -i 's/master.example.com/<你的域名>/g' deploy/nginx/master.conf

# 2) 申请证书 (二选一)
# 2a) Let's Encrypt (推荐, 生产)
sudo apt install -y certbot
sudo certbot certonly --standalone -d <你的域名>
sudo cp /etc/letsencrypt/live/<你的域名>/fullchain.pem deploy/nginx/certs/
sudo cp /etc/letsencrypt/live/<你的域名>/privkey.pem   deploy/nginx/certs/

# 2b) 或者自签 (仅测试)
./scripts/gen-self-signed-cert.sh <你的域名>

# 3) 起 master + nginx
./scripts/docker-deploy.sh master-edge
# 或 master + 一个本机 agent + nginx 一次起完
./scripts/docker-deploy.sh master-edge+ct

# 4) 验证
curl https://<你的域名>/healthz
```

#### 1.5 远程 VPS 只跑 Agent (场景 B / C)

VPS 上单独跑 Agent, 连远程 master:

```bash
# 在 VPS 上
git clone <repo> cf_ip_monitor && cd cf_ip_monitor

# 把场景 B 或 C 的连接参数填进 .env (或临时 export)
cp deploy/docker/.env.example .env
$EDITOR .env
# 至少需要:
#   AGENT_MASTER_URL=https://master.example.com   # 或 http://<MASTER_IP>:8088
#   AGENT_AUTH_TOKEN=<与 master 一致>
#   AGENT_ISP=电信
#   AGENT_NODE_NAME=ct-hk-01

# IP 库可以不下 (agent 不做 enrich), 直接起:
./scripts/docker-deploy.sh agent

# 或临时 export 不写 .env:
AGENT_MASTER_URL=https://master.example.com \
AGENT_AUTH_TOKEN=xxxx \
AGENT_ISP=电信 AGENT_NODE_NAME=ct-hk-01 \
    ./scripts/docker-deploy.sh agent
```

> Agent 跑 traceroute 需要 raw socket, compose 已加 `cap_add: NET_RAW`, 宿主机一般无需额外配置。
> Agent 连 https 域名时, 默认会校验 TLS 证书; 自签证书测试请用 Let's Encrypt, 或者用 http (不加密)。

#### 1.6 升级 / 回滚 / 备份

```bash
# 升级 (代码或依赖)
git pull
uv lock                                    # 改过 pyproject.toml 才需要
docker compose build --pull
docker compose up -d master
docker compose --profile agents up -d
docker image prune -f

# 仅热更新 config.yaml
docker compose restart master

# 备份 SQLite / 输出
docker run --rm -v cf-ip-monitor_master-data:/data \
    -v "$PWD/backup":/backup alpine \
    tar czf /backup/master-data-$(date +%F).tgz -C /data .
```

### 2. 裸金属 systemd + uv (无 Docker 环境)

```bash
# 0) 装 uv
curl -LsSf https://astral.sh/uv/install.sh | sh

# 1) 部署用户与目录
sudo useradd -r -s /usr/sbin/nologin -m -d /opt/cf_ip_monitor cfip
sudo git clone <repo> /opt/cf_ip_monitor
sudo chown -R cfip:cfip /opt/cf_ip_monitor
cd /opt/cf_ip_monitor

# 2) 系统依赖 (Agent 需要 traceroute; Master 没有强依赖)
sudo apt install -y traceroute            # Debian / Ubuntu
# brew install traceroute                 # macOS

# 3) 依赖按 uv.lock 还原 (生产模式, 不装 dev 依赖)
sudo -u cfip uv sync --frozen --no-dev

# 4) 配置
sudo -u cfip cp config.example.yaml config.yaml
sudoedit /opt/cf_ip_monitor/config.yaml

# 5) IP 库 → /opt/cf_ip_monitor/src/cf_ip_monitor/ipdata/

# 6) systemd unit (Master 与 Agent 二选一或同机各装一个)
sudo cp deploy/master.service /etc/systemd/system/cf-ip-master.service
sudo cp deploy/agent.service  /etc/systemd/system/cf-ip-agent.service
sudo systemctl daemon-reload
sudo systemctl enable --now cf-ip-master
sudo systemctl enable --now cf-ip-agent

# 7) 查看
sudo systemctl status cf-ip-master
sudo journalctl -u cf-ip-master -f
```

升级:

```bash
cd /opt/cf_ip_monitor
sudo -u cfip git pull
sudo -u cfip uv sync --frozen --no-dev
sudo systemctl restart cf-ip-master cf-ip-agent
```

> systemd unit 默认通过 `[project.scripts]` 注册的命令 `/opt/cf_ip_monitor/.venv/bin/cf-ip-master` / `cf-ip-agent` 启动, 不再走 `scripts/run_*.py`。

### 3. 本地开发

```bash
uv sync                                                     # 还原 .venv
uv run cf-ip-master --config config.yaml                    # 起 Master
uv run cf-ip-agent  --config config.yaml \
    --master-url http://127.0.0.1:8088 \
    --isp 电信 --node-name dev-01                            # 另开一个窗口起 Agent
```

依赖管理:

```bash
uv add httpx==0.27.2          # 改 pyproject + 自动刷 lock
uv remove some-pkg
uv lock                       # 仅刷新锁文件
uv sync                       # 应用到 .venv
```

### 4. 验证部署

```bash
# 容器健康
docker compose ps                         # STATUS 应为 (healthy)

# Master API
curl -sf http://localhost:8088/healthz && echo OK
curl -s  http://localhost:8088/v1/stats | jq .
curl -s  http://localhost:8088/v1/round/current | jq .

# 进容器单测一个 IP
docker compose exec master uv run python scripts/probe_single.py 1.1.1.1
```

FastAPI 自带文档: <http://localhost:8088/docs>

## 调试: 单机直接测一组 IP

跳过 master / agent 架构, 直接对几个 IP 跑完整探测:

```bash
# 基础 (TCP + HTTPS trace + speed)
uv run python scripts/probe_single.py 162.159.43.99 104.16.1.2 --bytes 20000000

# 加 traceroute + enrichment
uv run python scripts/probe_single.py 162.159.43.99 --traceroute --enrich --skip-speed

# 加 ICMP 对照 (验证 TCP 延迟是否纯网络层)
uv run python scripts/probe_single.py 162.159.43.99 --icmp

# Docker 环境里就这样:
docker compose exec master uv run python scripts/probe_single.py 162.159.43.99 --traceroute --enrich
```

输出 (JSON 一行一个 IP):

```json
{
  "ip": "162.159.43.99",
  "tcp":   {"ok": true, "min": 38, "p50": 42, "p95": 58, "jitter": 16, "samples": 6, "loss": 0},
  "trace": {"ok": true, "rtt_ms": 45, "colo": "NRT"},
  "speed": {"ok": true, "mbps": 23.4, "bytes": 20000000, "dur": 0.85},
  "traceroute": {"ok": true, "hops": [
      {"hop_idx": 1, "hop_ip": "192.168.0.1", "rtt_ms": 0.5},
      {"hop_idx": 3, "hop_ip": "202.97.50.1", "rtt_ms": 18, "asn": 4134, "country": "CN", "city": "上海市", "isp_cn": "电信"},
      {"hop_idx": 8, "hop_ip": "162.158.0.1", "rtt_ms": 45, "asn": 13335, "country": "JP", "city": "Tokyo"}
  ]},
  "enrich": {"asn": 13335, "as_name": "Cloudflare, Inc.", "country": "JP", "city": "Tokyo"}
}
```

## 数据分析

所有数据落在 `data/cfip.db`:

| 表 | 用途 |
| --- | --- |
| `probe_raw` | 所有原始探测, 含 latency 分位 + dst_* 富化字段 |
| `probe_trace_hops` | traceroute 每跳, 含 asn/country/region/city/isp_cn |
| `ip_route_label` | 派生标签: line_type / exit_city / asn_path / quality |
| `c_segment_state` | C 段 alive/silent 状态机 |
| `task_queue` | 任务持久化 (支持断点续传) |
| `scan_round` | 轮次记录 |
| `best_ip_snapshot` | 优选结果快照 |

常用查询:

```sql
-- 某 IP 在不同小时的 p50 延迟分布
SELECT hour_bucket % 24 AS hour, AVG(latency_p50), COUNT(*)
FROM probe_raw
WHERE ip='162.159.43.99' AND isp='电信' AND kind='tcp_ping' AND ok=1
GROUP BY hour ORDER BY hour;

-- 每个运营商最近 24h 命中的 colo 分布
SELECT isp, colo, COUNT(DISTINCT ip)
FROM probe_raw
WHERE kind='http_trace' AND ok=1 AND measured_at > strftime('%s','now','-1 day')*1000
GROUP BY isp, colo ORDER BY isp, 3 DESC;

-- 看每个运营商各种线路类型有多少 IP
SELECT isp, line_type, exit_city, COUNT(*) AS n
FROM ip_route_label
GROUP BY isp, line_type, exit_city
ORDER BY isp, n DESC;

-- 找走 CN2-GIA 的 IP
SELECT ip, isp, exit_city, asn_path
FROM ip_route_label
WHERE line_type='CN2-GIA';

-- 看某个 IP 的完整路径
SELECT hop_idx, hop_ip, rtt_ms, asn, country, city, isp_cn
FROM probe_trace_hops
WHERE ip='162.159.43.99' AND isp='电信'
  AND measured_at = (
    SELECT MAX(measured_at) FROM probe_trace_hops
    WHERE ip='162.159.43.99' AND isp='电信'
  )
ORDER BY hop_idx;
```

## 配置项速查

见 [config.example.yaml](config.example.yaml), 所有字段都有中文注释。关键的几个:

| 配置 | 默认 | 含义 |
| --- | --- | --- |
| `speed_test.enabled` | true | 总开关; false 时不测带宽, 评分退化为纯延迟 |
| `speed_test.interval_minutes` | 15 | 测速调度周期 |
| `speed_test.top_percentile` | 0.2 | 只对延迟前 N% 的候选测速 |
| `traceroute.enabled` | true | 总开关; false 时不跑路径分析 |
| `traceroute.interval_hours` | 24 | 同一 IP 多久 traceroute 一次 |
| `traceroute.top_n_per_isp` | 200 | 每个 ISP 最多跑多少目标 |
| `scoring.max_latency_ms` | 300 | 延迟上限 (仅过滤输出, 不丢原始数据) |
| `scoring.max_jitter_ms` | 200 | jitter 惩罚的上限 |
| `scoring.route_quality_enabled` | true | 是否加权 route_quality |
| `scoring.top_n_per_bucket` | 10 | 每个 (ISP × 地区) 输出 top N |
| `scoring.lookback_days` | 7 | 评分基于近 N 天数据 |
| `scheduler.full_scan_hours` | 24 | 全量采样周期 (默认从 6 改成 24, 适配 CF 现规模) |
| `scheduler.c_segment_silent_ttl_hours` | 6 | 沉默段缓存时间 |
| `scheduler.cleanup_done_after_hours` | 24 | done 任务保留时长, 防止表膨胀 |

## 环境变量与密钥

`config.yaml` 中的任何字符串值都支持 `${VAR}` 占位符, 运行时由 `cf_ip_monitor` 自动从进程环境变量展开。**所有密钥/凭据都应通过环境变量注入, 不要直接写进 `config.yaml`**。

| 变量 | 用途 | 注入方式 |
| --- | --- | --- |
| `HUAWEICLOUD_AK` | 华为云 DNS Access Key | `.env` → compose `environment` → 容器 |
| `HUAWEICLOUD_SK` | 华为云 DNS Secret Key | 同上 |
| `HUAWEICLOUD_ZONE_ID` | 华为云 DNS Zone ID | 同上 |
| `CF_IPDATA_DIR` | IP 离线库目录覆盖 | Docker 默认 `/app/ipdata`; 裸金属一般无需设置 |
| `LOG_LEVEL` | 日志级别 (默认 INFO) | `.env` 或 systemd `Environment=` |
| `MASTER_PORT` | Master 对外端口 (Docker) | `.env` |
| `AGENT_MASTER_URL` / `AGENT_*_ISP` / `AGENT_*_NODE_NAME` | Agent 节点参数 | `.env` (compose) 或 `export` (远程) |

注入路径:

- **Docker**: 编辑根目录 `.env` → `docker compose up -d` 自动加载, compose 文件里用 `${VAR}` 透传到容器
- **systemd**: 把变量写到 `/etc/cf-ip-monitor.env` (`chmod 600`), unit 已配 `EnvironmentFile=-/etc/cf-ip-monitor.env`
- **开发**: `export VAR=...` 或 `uv run --env-file .env cf-ip-master --config config.yaml`

> `config.yaml`、`.env`、`scripts/config.yaml`、`*.db`、`data/`、`output/`、`src/cf_ip_monitor/ipdata/*.mmdb|*.xdb|*.dat` 都已被 `.gitignore` 与 `.dockerignore` 排除。 提交前可以 `git status` 与 `git diff --cached | grep -iE "ak|sk|token|secret"` 自检。

## 故障排查速查

| 现象 | 原因 / 处理 |
| --- | --- |
| `uv sync --frozen` 报 `lock file is out of sync` | 改过 `pyproject.toml` 没 `uv lock`, 重跑一次 |
| 启动报缺 IP 库 / enrich 全空 | IP 库没下载到 `src/cf_ip_monitor/ipdata/`; Docker 还要确认 `CF_IPDATA_DIR=/app/ipdata` 与挂载点对齐 (新版默认已对齐) |
| Master `/healthz` 返回 404 / 容器 `unhealthy` | `${HUAWEICLOUD_AK}` 等占位符未注入会导致 exporter 初始化失败 → 看 `docker compose logs master` |
| Agent 连不上 Master | 同机用 `http://master:8088`; 跨机用公网地址 + 防火墙放行; 注意 `auth_token` 两边一致 |
| Agent traceroute 报 `Operation not permitted` | compose 已加 `cap_add: NET_RAW`; 裸金属确认 `traceroute` 是 setuid 或 root 运行 |
| 升级后旧任务卡住 | `housekeeping` 5 分钟内会自动 requeue >15 min 的 `assigned` 任务 |
| 切换端口 | 改 `.env` 的 `MASTER_PORT` → `docker compose up -d` |
| 跨架构部署 (M 系开发, x86 服务器) | `docker buildx build --platform linux/amd64,linux/arm64 --target master --push -t <repo>/master:tag .` |

## 已知边界 / 下一步

- HTTPS 探测时 TLS 证书不校验 (因为直连 IP); CF 边缘的统一证书也能过, 但走 IP 时 hostname 不匹配
- speed.cloudflare.com 是 Cloudflare 自家测速终结点, 反映 "你 -> CF 边缘" 这一段
- macOS 上跑 traceroute 需要 sudo (TCP traceroute 要 raw socket); Linux 上 `apt install traceroute` 后是 setuid, 用户态可直接跑
- 后续可加: WebUI 看历史趋势 / 异常告警 / 一键回滚 DNS / Telegram 通知
