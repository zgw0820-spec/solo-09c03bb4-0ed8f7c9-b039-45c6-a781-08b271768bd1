# 磁带恢复排程与读片巡检 API

用 FastAPI 实现的两个服务：

1. **`/schedule` 排程**：给定中转盘空闲容量和磁带目录，求出把全部文件从磁带恢复到新存储所需的**最少装带次数**，并给出每次装带的带号、读取顺序和交付时点。
2. **`/audit` 巡检**：用同样的目录格式和两台带机编号开一轮读片校验；按回执号成批报送/撤回结果，列出能解释全部有效回执的**极小故障组合**，并给出供 `/schedule` 使用的目录（只滤掉每个组合都判坏的副本）。

## 启动

```bash
docker compose up --build
```

打开 <http://localhost:8000/docs>，各端点都可用 **Try it out** 直接试跑。

```bash
# 排程样例
curl -s http://localhost:8000/example | \
  curl -s -X POST http://localhost:8000/schedule \
       -H 'Content-Type: application/json' --data-binary @-
```

巡检的完整流程（开轮 → 报送两批 → 查看结论 → 取排程目录 → 撤回误报）：

```bash
# 1) 开一轮巡检：/schedule 格式的目录 + 两台带机
curl -s -X POST http://localhost:8000/audit/rounds \
     -H 'Content-Type: application/json' \
     --data-binary @examples/audit_round.json
# 记下返回的 round_id，下称 $RID

# 2) 按回执号成批报送（同号原样重报不重复计数）
curl -s -X POST http://localhost:8000/audit/rounds/$RID/batches \
     -H 'Content-Type: application/json' \
     --data-binary @examples/audit_batch_1.json
curl -s -X POST http://localhost:8000/audit/rounds/$RID/batches \
     -H 'Content-Type: application/json' \
     --data-binary @examples/audit_batch_2.json

# 3) 巡检结论：全部极小故障组合、确定/可能故障、停用或隔离建议
curl -s http://localhost:8000/audit/rounds/$RID

# 4) 供 /schedule 使用的目录：只滤掉每个组合都判坏的副本
curl -s http://localhost:8000/audit/rounds/$RID/schedule-catalogue

# 5) 撤回误报批次（该回执号此后不得复用）
curl -s -X POST http://localhost:8000/audit/rounds/$RID/retract \
     -H 'Content-Type: application/json' \
     --data-binary @examples/audit_retract.json
```

也可以不用 Docker：`pip install -r requirements.txt && uvicorn app.main:app --port 8000`
（巡检数据默认写到 `data/audit.db`，可用 `AUDIT_DB_PATH` 改）。

巡检样例文件：`examples/audit_round.json`（开轮）、`audit_batch_1.json` /
`audit_batch_2.json`（两批回执）、`audit_retract.json`（撤回）。上面的 5 步
对应的实际响应见 `examples/audit_response.json`（`round_id` 以 `<ROUND_ID>`
占位）；其中第 2 批证明 DRV-2 其它副本正常，因而唯一极小解释是副本 `a1-d2`
损坏，目录滤掉它；撤回第 1 批后 `a1-d2` 恢复为可选、解释变为“无故障”。

### 端点总览

| 方法 | 路径                                   | 说明 |
| ---- | -------------------------------------- | ---- |
| GET  | `/docs`                                | Swagger UI（可交互试跑） |
| GET  | `/example`                             | 可直接提交的排程样例请求 |
| POST | `/schedule`                            | 计算最少装带次数的排程 |
| POST | `/audit/rounds`                        | 开一轮巡检（目录 + 两台带机编号） |
| GET  | `/audit/rounds/{round_id}`             | 巡检结论：全部极小故障组合与建议 |
| GET  | `/audit/rounds/{round_id}/state`       | 轮次状态与修订号 |
| GET  | `/audit/rounds/{round_id}/schedule-catalogue` | 供 `/schedule` 的过滤后目录 |
| POST | `/audit/rounds/{round_id}/batches`     | 按回执号成批报送读片结果 |
| POST | `/audit/rounds/{round_id}/retract`     | 按回执号整批撤回 |
| GET  | `/audit/example`, `/audit/example/batch` | 巡检开轮 / 回执批次样例 |
| GET  | `/healthz`                             | 健康检查（容器 HEALTHCHECK 使用） |

## 巡检规则

- **开轮**：`tape_units` 恰好两台带机；`catalogue` 沿用 `/schedule` 的
  `files → shards → replicas` 结构，每个副本可直接带目录内唯一的 `replica_id`
  （可省略，省略时按 `文件/分片@带机:位置` 自动生成），副本必须落在两台带机
  之一。上限：12 文件、24 分片、48 副本。
- **回执**：每条指明 `tape`（带机）、具体副本（`replica_id`，或退而用
  `position` 指明带上的位置）、所属 `file_id`/`shard_id`，以及 `kind` 为
  `pass` 或 `fail`。批次统一挂在 `receipt_id`（回执号）下。
- **读片模型**：带机与副本都正常则通过，任一有故障则失败；持续故障合计
  **最多 2 处**（带机 + 副本）。
- **归因**：有效回执 = 通过的回执都避开故障点、失败的回执都至少命中一个故障点。
  失败读片构成顶点覆盖问题，故障组合不超过 2 个，故可精确枚举出全部
  **包含意义下的极小**组合（去掉任一故障就解释不通）。通过的回执会把对应的
  带机和副本钉为健康，永不进入解释。
- **结论**：`certain_faults` 是每个组合都包含的故障（可据此**停用带机**或
  **隔离副本**）；`possible_faults` 只在部分组合中出现。`verdict` 与 `roadmap`
  给出可执行建议：`retire`（停用/隔离）或 `verify`（换机/复读确认），并对每个
  极小组合给出可读 `rationale`。`undecided_replicas` 列出只在部分组合下判坏、
  因而仍保留的副本。
- **重报/冲突/撤回**：同号同内容重报返回 `duplicate`（即便携带旧修订号也按幂等
  处理），不重复计数；同号不同内容返回 409；撤回后整批失效且**该号不得复用**。
  撤回导致某副本不再在所有组合中判坏时，它从过滤目录中恢复为可选
  （`restored_replicas`）。
- **整批拒收**：若加入新批次后有效回执无法由任何 ≤2 处故障解释，该批次整体
  不写入，409 响应体的 `conflicts` 给出一个极小的冲突回执集；若批次引用了目录
  中不存在/带机或位置不匹配的副本，同样整批不写入并返回 422 的结构化结果
  （`status=rejected`，`conflicts` 列出每个问题）。
- **供排程的目录**：只滤掉**每个极小组合都判坏**的副本（`excluded_replicas`），
  原文件与分片全部保留（即使某分片因此没有副本，也保留分片让 `/schedule`
  如实报缺失）；处于不确定状态的副本保留并列入 `undecided_replicas`。有效回执
  矛盾时该端点返回 409，要求先撤回误报。
- **并发与持久化**：SQLite + `BEGIN IMMEDIATE` 事务，报送/撤回在写锁内重新
  读取有效回执再校验提交，已生效改动不会被并发更新覆盖；可选
  `expected_revision` 提供乐观并发保护（不匹配返回 409）。数据库落在 Compose
  具名卷 `audit-data` 上，重启后目录仍由同一批有效回执重新导出。

## 排程请求 / 响应

```json
{
  "staging_capacity": 10,
  "files": [
    {
      "id": "file-A",
      "shards": [
        {
          "id": "a1",
          "size": 2,
          "replicas": [
            {"tape": "T1", "position": 10},
            {"tape": "T2", "position": 5}
          ]
        }
      ]
    }
  ]
}
```

- `staging_capacity`：中转盘当前空闲量（单位任意，前后一致即可）
- `files[].shards[].size`：该分片需要的中转空间
- `replicas[].tape` / `position`：副本所在带号与位置；同一装带内的读取位置必须非降序
- 分片的 `replicas` 为空数组表示该分片缺失

样例见 `examples/sample_request.json` 与 `examples/sample_response.json`。响应中
每次装带给出 `tape`、`reads`（升序）、`delivers_file`，以及
`staging_before/peak/after`；无解时 `feasible=false` 并给出
`missing_shards` / `capacity_issues`。

## 排程算法

1. **静态判定**：任一分片无副本 → 缺失；任一文件分片总量 > 中转盘容量 → 无法
   校验交付。这两种情况即全部不可行原因。
2. **状态**：只记录“已读分片集合”；已交付文件集合和当前占用都由该集合唯一确定。
3. **搜索**：以“已读集合”为节点、一次装带为一条边做广度优先搜索；首次到达
   全集时的层数就是最少装带次数（精确最优）。
4. **单次装带的可行读集**：对某盘带枚举可读分片的子集，用“按子集求最小磁头
   位置”的动态规划精确判定能否一次装带完成（容量峰值在交付前计入）。
5. **下界解释**：按“每盘带一次能覆盖的分片”算集合覆盖下界；达到下界即证明
   最优，否则在 `explanation` 中说明容量约束导致重复装带，并由 BFS 穷尽更少
   次数证明最优。

## 测试

```bash
python -m pytest -q
```

- `tests/test_solver.py`：`replay()` 独立重放排程、`brute_force_min()` 穷举对拍
  最少装带数（含随机实例）。
- `tests/test_audit.py`：独立暴力枚举所有 ≤2 处故障集合，与 API 的极小组合对拍；
  覆盖重报不重复计数、同号异内容拒收、解释不通整批拒收、撤回后号码不复用与
  副本恢复可选、目录只滤确定损坏副本、并发不丢更新、重开连接后目录一致。

## 文件结构

```
app/
  main.py           FastAPI 应用、排程路由与样例
  models.py         排程请求/响应模型与上限校验
  solver.py         最少装带次数求解器
  audit_models.py   巡检请求/响应模型（含 /schedule 格式目录）
  audit_logic.py    回执归因：极小故障组合、冲突集、确定性判定
  audit_store.py    SQLite 持久化、批次/撤回事务、目录过滤
  audit_api.py      巡检 HTTP 路由
tests/
  test_solver.py    求解器、重放校验、暴力对拍
  test_api.py       排程 HTTP 接口测试
  test_audit.py     巡检归因对拍、冲突、撤回、并发、持久化
examples/           排程与巡检的样例请求/响应
Dockerfile / docker-compose.yml
```
