# 特征工程与数据挖掘工作台

面向特征构建、特征选择、数据切片、模型训练与挖掘结果管理的数据分析特征工程平台。

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
| 特征源管理 | `feature_source` | 特征源 | 源编号、源名称、数据表名 |
| 特征定义 | `feature_define` | 特征定义 | 特征编号、特征名称、特征类型 |
| 特征组管理 | `feature_group` | 特征组 | 组编号、组名称、包含特征 |
| 数据切片配置 | `data_slice` | 切片配置 | 切片编号、切片名称、数据来源 |
| 特征计算任务 | `feature_compute` | 计算任务 | 任务编号、关联特征、数据切片 |
| 特征筛选策略 | `feature_selection` | 筛选策略 | 策略编号、特征集、筛选方法 |
| 特征重要性分析 | `feature_iv` | 重要性评估 | 评估编号、关联特征、信息值IV |
| 特征快照管理 | `feature_snapshot` | 特征快照 | 快照编号、特征集、快照日期 |
| 训练流水线 | `train_pipeline` | 流水线配置 | 流水线编号、流水线名称、特征集 |
| 模型注册中心 | `model_registry` | 模型注册 | 模型编号、模型名称、算法类型 |
| 批量预测任务 | `predict_batch` | 预测任务 | 任务编号、模型编号、数据切片 |
| 挖掘结果管理 | `mining_result` | 挖掘结果 | 结果编号、分析主题、算法类型 |
| 聚类分析配置 | `cluster_config` | 聚类配置 | 配置编号、特征集、算法类型 |
| 特征相关性矩阵 | `feature_corr` | 相关性矩阵 | 矩阵编号、特征集、计算方法 |
| 特征漂移监控 | `drift_monitor` | 漂移监控 | 监控编号、关联特征、基准分布 |
| 数据导出管理 | `data_export` | 导出任务 | 任务编号、数据来源、导出范围 |
| 流水线调度 | `pipeline_schedule` | 调度配置 | 调度编号、关联流水线、Cron表达式 |
| 业务场景标注 | `scenario_label` | 场景标注 | 标注编号、场景名称、关联特征 |

## 约定

- 全部模块共享`frontend/src/views/WorkbenchView.vue|tsx`和一套资源路由。
- 每个模块仍保留独立`backend/app/services/<模块>.py`；纯前端保留
  `frontend/src/domain/<模块>.ts`，用于承载真实业务差异并维持36题位容量。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

## 特征字段修订链路

同一个特征源字段只对应一个特征编号（`FEAT-S<源号>-F<字段序号>`）。字段回执与
字段发现都走 `app/services/revision_support.py` 的修订登记：

- 同一来源、同一规范字段、等价内容只登记一次；重复提交与清单换序不会新增草稿，
  字段编号也不会因换序而变化。
- 更正字段描述或计算规则关联同一来源字段，在原特征编号下形成一条待校验修订；
  校验入口按特征编号只保留最新一条待校验修订。
- 修订通过（`approve`）后才成为当前生效版；校验通过前下发的新计算任务仍使用旧的
  生效修订。迟到的旧回执匹配到历史修订即忽略，不会新增修订、也不会把当前定义改回旧版。
- 计算任务在下发时绑定当时的生效修订；历史任务保留原取数规则，重放沿用原绑定。

| 接口 | 用途 |
| --- | --- |
| `POST /api/feature_source/{id}/discover` | 提交字段清单（支持换序、等价重复） |
| `POST /api/feature_source/{id}/receipts` | 送入字段回执与更正（`receipt` / `corrections`） |
| `GET /api/feature_source/{id}/fields` | 按来源查看字段清单与各修订状态 |
| `GET /api/feature_define/pending` | 校验入口的待校验修订（可按来源/字段过滤） |
| `GET /api/feature_define/detail/{id或特征编号}` | 定义详情：来源、各修订、当前生效版 |
| `POST /api/feature_define/{id或特征编号}/approve` | 修订生效，旧生效版自动废弃 |
| `POST /api/feature_define/push` | 按当前生效修订下发计算任务（可带 `input_value`） |
| `POST /api/feature_compute/replay/{task_id}` | 按历史任务已绑定的修订重放 |
| `GET /api/feature_compute/history` | 计算历史：来源标识、绑定修订、当前生效版本 |

演练脚本：`backend/scripts/rehearse_revisions.py`；
回归测试：`backend/tests/test_field_revisions.py`。

领域：数据工程
子方向：数据分析与数据应用
