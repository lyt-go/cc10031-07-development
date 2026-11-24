# 工业传感器时序采集与降采样平台

面向工业传感器高频时序数据的测点接入、边缘缓冲上送、单位标定、时间对齐、降采样归并与分层归档的采集平台。

这是一个前后端分离的管理平台：前端 Vue 3 + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 Vue 3 + Vite + TypeScript 前端
│   ├── src/views/WorkbenchView.vue|tsx  全部模块共享工作台
│   ├── src/data/modules.ts              18个业务模块配置
│   ├── src/api/              统一请求封装
│   └── vite.config.ts        dev server 配置（open: false）
├── backend/                  FastAPI（Python） 后端
│   ├── app/routers/resource.py  通用查询/导出/登记/动作接口
│   ├── app/services/<key>.py    每个模块独立领域逻辑
│   ├── app/services/base.py     共享服务骨架
│   └── app/store.py          内存数据仓库与示例数据
├── .gitignore
└── docker-compose.yml
```

## 启动

### 后端

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./run.sh
```

健康检查：`curl http://127.0.0.1:8000/api/health`

### 前端

```bash
cd frontend
npm ci
npm run dev
```

前端默认监听 `http://127.0.0.1:5173/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8000`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| 测点台账 | `sensor_point` | 测点档案 | 测点编号、所属产线、传感器型号 |
| 边缘缓冲 | `edge_buffer` | 缓冲分片 | 分片编号、测点编号、起始时刻 |
| 批量上送 | `upload_batch` | 上送批次 | 批次编号、分片范围、测点数量 |
| 单位标定 | `unit_calibrate` | 标定记录 | 标定编号、测点编号、原始单位 |
| 时间对齐 | `time_align` | 对齐规则 | 规则编号、对齐粒度、时区基准 |
| 断点填补 | `gap_fill` | 填补区间 | 区间编号、测点编号、缺失起点 |
| 尖峰剔除 | `spike_filter` | 异常采样 | 异常编号、测点编号、采样时刻 |
| 降采样 | `downsample` | 降采样任务 | 任务编号、源序列、源采样频率 |
| 窗口归并 | `rollup_window` | 归并窗口 | 窗口编号、测点编号、窗口类型 |
| 序列索引 | `series_index` | 索引分区 | 索引编号、测点编号、时间分区 |
| 冷热分层 | `tier_storage` | 分层策略 | 策略编号、时间边界、热区范围 |
| 采集心跳 | `device_health` | 心跳记录 | 心跳编号、采集终端、上报时刻 |
| 接入水位 | `ingest_watermark` | 水位标记 | 水位编号、测点编号、事件时间水位 |
| 区间重采 | `range_replay` | 重采任务 | 重采编号、测点编号、起始时刻 |
| 采集血缘 | `series_lineage` | 血缘链路 | 链路编号、测点编号、上送批次 |
| 序列导出 | `series_export` | 导出任务 | 导出编号、测点范围、时间范围 |
| 时序质量 | `series_quality` | 质量报告 | 报告编号、测点编号、统计窗口 |
| 序列快照 | `series_snapshot` | 版本快照 | 快照编号、口径说明、测点范围 |

## 约定

- 全部模块共享`frontend/src/views/WorkbenchView.vue|tsx`和一套资源路由。
- 每个模块仍保留独立`backend/app/services/<模块>.py`；纯前端保留
  `frontend/src/domain/<模块>.ts`，用于承载真实业务差异并维持36题位容量。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

## 事件日交接与按日迁冷

序列索引的每个分区对应一个实际采样日（事件日），以交接号 `HO-<测点>-<事件日>` 为稳定标识。
首个归并窗口归「前段」（旧班交接读取的部分），其余窗口归「末段」。

交接状态落在 `data/handover/<交接号>.json`，冷区归档与写入检查点落在 `data/tier_storage/`。
状态文件与检查点都走「临时文件 + 原子替换」，冷区记录逐条追加并推进检查点，
进程中断后可用原交接号续写，不会重写已归档的分段。

| 接口 | 作用 |
| --- | --- |
| `POST /api/series_index/{id}/handover/windows` | 登记归并窗口；`late_report` 标记封存后的迟报 |
| `POST /api/series_index/{id}/handover/seal` | 新日首个归并窗口确认，旧日封存并关联新旧交接号 |
| `POST /api/series_index/{id}/handover/verify` | 按前段或末段独立核验 |
| `POST /api/series_index/{id}/handover/correction` | 登记末段更正，标记待重算 |
| `POST /api/series_index/{id}/handover/recompute` | 获准后完成更正重算，进版为新的发布口径 |
| `GET /api/handovers[?point=]` | 列出事件日交接状态 |
| `GET /api/handovers/{交接号}/readback[?segment=前段]` | 从冷热分层直接读回，标出实际落在冷区还是热区 |
| `POST /api/handovers/{交接号}/reconcile` | 按整日核对发布值、读回值与待重写分段 |
| `POST /api/handovers/{交接号}/evict` | 把已确认覆盖的分段迁冷；`fail_after` 可模拟写入中断 |
| `GET /api/handovers/{交接号}/checkpoint` | 查询冷区写入检查点 |

迁冷只归档已确认覆盖的分段：因迟报待重写或更正待重算的末段继续留在热区，
事件日整体标为 `partial_cold`，并在读回结果里给出等待原因。
前端在「序列索引」和「冷热分层」两个模块的页脚下方展示同一块交接面板。

## 约定
子方向：数据采集、爬虫与RPA
