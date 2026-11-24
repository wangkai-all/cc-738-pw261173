# 河网卷积分析台（RiverNet Convolution Workbench）

一个**零第三方依赖、可本地一键启动**的河网时间序列分析工具。它把上游流量按每条河段的离散传播核逐段卷积，再与下游观测对照，避免“同一时刻直接相减”造成的时滞错误。

> **虚构数据声明**：内置测站、河网与洪水过程均为人工虚构，仅用于算法和交互演示。本项目**不用于真实水文预报、预警、调度或任何安全决策**。

---

## 1. 快速开始

### 方式一：启动脚本

```bash
cd /workspace
./start.sh
```

默认监听 `0.0.0.0:8000`，浏览器打开：

```text
http://127.0.0.1:8000
```

可改端口：

```bash
PORT=8123 ./start.sh
```

### 方式二：Python 入口

```bash
python3 run.py
# 或
python3 -m rivernet

# 可选参数
python3 run.py --host 127.0.0.1 --port 8123
```

环境要求：

- Python 3.10+（在 Python 3.11.2 上开发和验证）
- 不需要 `pip install`，后端仅使用 Python 标准库
- 前端为原生 HTML/CSS/JavaScript，不需要 npm 构建或外部 CDN

运行测试：

```bash
python3 -m unittest discover -s tests -v
```

当前交付快照：**43 个测试全部通过**，覆盖卷积边界、NaN/缺测传播、全缺测来源导出、同源洪峰边界、核约束拟合、DAG/环路、菱形多路径、质量守恒、多级汇合、增量缓存、方案快照和 HTTP API。

---

## 2. 它解决什么问题

上游几处流量变化不会同时到达下游，而是经过河段调蓄和传播后延迟出现。若直接计算：

```text
下游(t) − 上游(t)
```

会把“尚未到达的波”误判成支流贡献或残差。

本项目对河段 `u → v` 使用离散卷积：

\[
r_{u\to v}(t)=\sum_{j=0}^{m-1} w_j\,x_u(t-\ell_{\min}-j)
\]

其中：

- `w_j ≥ 0`
- `Σ_j w_j = 1`，传播核总权重守恒
- `ℓ_min … ℓ_max` 是允许的整数样本滞后范围
- 缺测输入不会被当作 0；只要某个有效权重抽头需要缺测样本，严格卷积结果就是不可识别（JSON 中为 `null`）

汇合处不是简单同时刻相加，而是：

1. 从每个来源沿每条路径逐段卷积；
2. 路径核通过核卷积自动合成；
3. 同一来源存在多条路径（如菱形河网）时，在公共滞后网格上合并；
4. 真正的分流/分配通过边的 `share` 比例缩放，避免质量重复计算。

---

## 3. 内置样例

| ID | 说明 |
|---|---|
| `conservation-tree` | 240 小时、5 个虚构测站、两级汇合：YL/QJ→SG，SG/BS→LJK；含已知合成核、噪声和停测缺口，可验证质量守恒与贡献闭合 |
| `interval-15min` | 北沟/南坡/三岔河，15 分钟采样；固定物理滞后换算成更多样本 |
| `interval-60min` | 同一河网的 1 小时采样版本 |
| `interval-360min` | 6 小时采样版本；同一物理滞后对应更少样本 |
| `cycle-error` | A→B→C→A 的错误环路，另含 C→D；服务会拒绝传播计算并在图中红色显示环路 |

样例文件也已导出到：

```text
examples/data/<example-id>/
├── project.json   # 完整 bundle，可直接通过 API 打开
├── flows.csv      # 宽表时间序列
└── network.json   # 河网结构
```

可重新生成：

```bash
python3 scripts/generate_examples.py
```

---

## 4. 网页功能

打开页面后会自动加载 `conservation-tree`。

### 左侧：河网与河段

- 自动按拓扑层级绘制 DAG；
- 点击边或节点选择河段；
- 显示每条边的样本滞后范围、小时滞后和核权重和；
- 环路样例显示错误链，例如 `B → C → A → B`，并禁用所有传播结果。

### ① 河段校准与过程线

- 编辑最小/最大滞后（小时），页面按采样间隔换算为样本数；
- “应用滞后范围”会生成该范围内的缺省守恒核；
- 用双滑块选择校准期；
- 可添加一个或多个手动排除区间；
- 校准只使用所选校准期内的有效行，评估期绝不参与拟合；
- 拟合核满足非负、和为 1；
- 图中可切换上游实测、卷积到达、扣除其他支流后的拟合目标、下游实测、其他支流；
- 显示校准 RMSE/R²/bias、样本外评估 RMSE/R²；
- 显示当前核柱状图和三态可识别范围。

拟合使用投影单纯形最小二乘：BB 步长 + Armijo 投影梯度，并用主动集 KKT 精修。无第三方数值库。

### ② 洪峰移动

- 选择源站和目标站；
- 沿主路径逐站显示洪峰时刻、洪峰流量、相对源峰滞后；
- 根据路径合成核的众数滞后预测洪峰到达；
- 每个路径节点有共享时间轴的小多图和洪峰标记；
- 多路径时明确提示路径数量。

### ③ 各站贡献 / 下载

- 选择目标汇合站或控制站；
- 每个测站先计算其“本地增量入流”，再沿全部路径传播到目标；
- 展示各来源贡献线、部分残波虚线、可识别总量和目标实测；
- 显示每个来源的路径数、平均贡献、积分水量和可识别区间；
- 在完整可识别域内给出闭合误差：

\[
\text{closure}(t)=Q_{\text{observed}}(t)-\sum_s C_{s\to target}(t)
\]

守恒合成例的最大闭合误差约为 `1e-14`（浮点机器精度）。

提供两种 CSV：

- **宽表**：每个来源一列，加 `modeled_total / observed / closure_residual`；
- **长表（tidy）**：每时每站一行，含：
  - `exact`
  - `partial_after_outage`
  - `unidentifiable`
  - 部分抽头下界和已知权重比例

下载地址：

```text
/api/contributions.csv?sid=<session>&target=<station>
/api/contributions.csv?sid=<session>&target=<station>&format=long
```

缺测在 CSV 中为空单元格，不会写成 0。

### ④ 残差来源

对目标站的每条直接入边显示：

- 当前核传播后的支流过程；
- 支流平均贡献和大致来源占比；
- 每条边自己的评估 RMSE；
- 可识别/残波终点；
- 目标站缺测段；
- 残差过程：

\[
R(t)=Q_{\text{target}}(t)-\sum_{e\in In(target)} share_e\,k_e\circledast Q_{from(e)}(t)
\]

正残差通常对应本地入流、未测支流或核低估；负残差通常对应核高估/观测误差。缺测导致的空白是“不可识别”，不会被解释成新的水源。

### ⑤ 校准方案

- 每个方案有 `S001...` 编号；
- 保存名称、边、校准时间、滞后范围、核、指标、排除区间；
- 每个方案还保存不可变 `snapshot`：当时的上游切片（含最大滞后所需的前置样本）、目标切片、滞后网格、使用行和排除区间；
- 后续修改河网、当前核、校准窗口或再次校准，都不会改变旧方案；
- “用自带切片重放拟合”会仅用快照重新拟合，并显示与原权重的最大偏差（通常 `< 1e-8`）。

### 增量重算提示

修改一条边的核或滞后范围后，后端计算脏边集合：

- 当前边；
- 从该边终点可达的所有下游边上的传播结果。

上游和不相关旁支的缓存保留。页面横幅会列出本次重算边/节点与缓存边，便于核对“改一段只重算下游”。

---

## 5. 停测与三态可识别性

设最后一个有效观测在 `L`，当前核的有效滞后范围为 `[a,b]`：

- **exact / 完整可识别**：所有显著权重对应的输入抽头均有效。严格域通常延续到 `L+a`；
- **partial / 部分残波**：只有部分较短滞后抽头已知。非负流量和非负权重下，已知抽头之和可作为贡献**下界**，通常延续到 `L+b`；
- **unidentifiable / 不可识别**：没有任何有效抽头，输出 `null`，不补零。

前端用绿色轨道表示 exact、黄色斜纹表示 partial；长表 CSV 明确写出状态。

---

## 6. 导入自己的虚构数据

网页点击“导入 CSV / 河网”，或调用 `POST /api/import`。

### 流量 CSV：宽表

```csv
time,A,B,C
2026-03-01T00:00,4.1,3.2,8.0
2026-03-01T01:00,4.0,,8.4
2026-03-01T02:00,3.7,3.5,
```

第一列必须是时间，其余列为测站 ID。

### 流量 CSV：长表

```csv
time,station,flow
2026-03-01T00:00,A,4.1
2026-03-01T00:00,B,3.2
2026-03-01T01:00,A,4.0
```

以下值会被识别为缺测，绝不会补零：

```text
空单元格, NA, N/A, null, NaN, nan, -9999
```

### 河网 JSON

最小结构：

```json
{
  "edges": [
    {"id": "e1", "from": "A", "to": "C", "lag_min": 1, "lag_max": 3},
    {"id": "e2", "from": "B", "to": "C", "lag_min": 2, "weights": [0.2, 0.6, 0.2]}
  ]
}
```

字段说明：

- `id`：河段唯一 ID；
- `from` / `to`：上下游测站；
- `share`：汇合/分流比例，默认 `1`；
- `lag_min`：第一个权重对应的样本滞后；
- `weights`：可选，必须非负且和为 1；
- 只给 `lag_min + lag_max` 时，会生成归一化的缺省单峰核。

也支持直接提交完整 bundle 到 `POST /api/session/bundle`。

---

## 7. 主要 HTTP API

| 方法/路径 | 用途 |
|---|---|
| `GET /api/health` | 健康检查 |
| `GET /api/examples` | 内置样例列表 |
| `POST /api/session/example` | 打开内置样例，body：`{"example_id":"conservation-tree"}` |
| `POST /api/session/bundle` | 打开完整 JSON bundle |
| `POST /api/import` | 导入 CSV 与 network JSON |
| `GET /api/state?sid=...` | 河网、测站、边、方案和网格状态 |
| `GET /api/edge?sid=...&edge_id=...` | 河段过程线、残差、可识别性、洪峰、指标 |
| `POST /api/edge/kernel` | 直接保存约束核 |
| `POST /api/edge/lag_range` | 修改滞后范围并生成缺省核 |
| `POST /api/calibrate` | 在校准切片内拟合并可应用 |
| `GET /api/contributions?sid=...&target=...` | 各站贡献分解 |
| `GET /api/contributions.csv?...&format=wide\|long` | 下载贡献 CSV |
| `GET /api/residual?sid=...&target=...` | 残差来源 |
| `GET /api/peaks?sid=...&source=...&target=...` | 洪峰移动 |
| `GET /api/scheme?sid=...&scheme_id=...` | 查看方案（含快照） |
| `POST /api/scheme/apply` | 应用旧方案 |
| `POST /api/scheme/refit` | 用方案快照重放拟合 |

错误统一为：

```json
{"ok": false, "error": {"code": "CYCLE_DETECTED", "message": "...", "details": {}}}
```

常见状态码：`400` 输入错误、`404` 会话/对象不存在、`409` 河网存在环路、`422` 校准样本不足。

---

## 8. 项目结构

```text
workspace/
├── run.py                         # 根目录启动入口
├── start.sh                       # 一键启动脚本
├── rivernet/
│   ├── __main__.py                # python -m rivernet
│   ├── server.py                  # 标准库 HTTP API + 静态文件
│   ├── engine.py                  # Project：缓存、校准、贡献、残差、洪峰
│   ├── timeseries.py              # 卷积、NaN 传播、可识别性、核合成
│   ├── kernels.py                 # 单纯形投影约束拟合
│   ├── network.py                 # DAG 校验、拓扑、环、路径、脏边
│   ├── examples.py                # 虚构合成样例
│   └── importer.py                # CSV/JSON 导入
├── web/
│   ├── index.html                 # 页面骨架
│   ├── styles.css                 # 设计令牌、布局、深浅色
│   ├── charts.js                  # 无依赖 SVG 折线/核柱/提示层
│   └── app.js                     # 状态、API、DAG、五个交互面板
├── examples/data/                 # 导出的样例 CSV/JSON/project bundle
├── scripts/generate_examples.py
└── tests/                         # 40 个 unittest
```

---

## 9. 已验证的关键性质

测试和 HTTP smoke check 已验证：

- 单位置脉冲出现在正确滞后位置；
- 卷积与暴力参考实现一致；
- 左边界 `edge="missing"` 与 `edge="zero"` 的差异；
- 内部 NaN 严格传播，缺测不补零；
- 停测后 exact / partial / unidentifiable 三态；
- 非负归一核的总质量守恒；
- 核合成满足结合路径上的逐级卷积；
- 菱形多路径按路径合并，`share=0.6/0.4` 时总质量不翻倍；
- 多级汇合贡献在完整可识别域内闭合到机器精度；
- 修改一条边只重算脏边及下游；
- 评估期数据污染不会改变已完成拟合；
- 校准方案保存自己的切片快照，快照重放偏差约 `1e-9`；
- 环路样例返回 `CYCLE_DETECTED`，不输出伪计算结果；
- 15/60/360 分钟样例均可实例化并完成贡献分解；
- 真实 HTTP 服务下静态资源、校准、refit、贡献、残差、洪峰和 CSV 下载均正常。

当前环境没有 Chromium/Playwright/jsdom，因此未做像素级浏览器截图；已完成 `node --check`、DOM/API 契约审查和真实 HTTP 验证。
