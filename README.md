# 经营单据与结算服务

本地可运行的多租户经营单据服务。当前支持受理订单、按标识读取订单、登记收款、退款单受理/退回补件/补件提交/完成/拒绝，并核对未收金额与净额；数据落本地 SQLite 文件库，服务为单进程 HTTP 服务。

## 环境与安装

- Python 3.11
- `python3 -m venv .venv && . .venv/bin/activate && pip install -e ".[dev]"`

## 启动

- `python3 -m app.entry --port 8000`
- 健康检查：`GET /health`

## 测试

- `pytest -q`
- 静态检查：`ruff check .`

## 已有公开接口

所有读写接口都通过请求头 `X-Tenant`（订单受理除外，租户在请求体中）做租户隔离；跨租户访问一律返回 404，不泄漏对象是否存在。

- `POST /orders`：受理订单。请求字段 `tenant`、`order_id`、`amount_cents`、`currency`。成功返回 201 与订单对象；参数不合法返回 400；同一租户重复受理返回 409。
- `GET /orders/{order_id}`：按标识读取订单。不存在或跨租户返回 404。返回体含金额守恒字段：
  - `paid_cents`：累计已收
  - `refunded_cents`：累计已退（仅“已完成”退款计入）
  - `pending_refund_cents`：已受理未决退款的占用额
  - `outstanding_cents = amount_cents - paid_cents`：未收金额
  - `net_cents = paid_cents - refunded_cents`：净额
  - `status`：`accepted`（受理/部分结算）、`settled`（净额等于订单金额）、`open`（曾收款且净额回到零）
- `POST /orders/{order_id}/payments`：登记收款。请求字段 `amount_cents`；超过未收金额返回 409；成功返回 200 与订单汇总。订单存在收款计划时，一次登记金额必须等于从当前未收讫期次起若干个连续期次的剩余金额合计（一次收满一期或多期，不留零头），否则返回 409 且订单与各期进度均不改变。
- `POST /orders/{order_id}/payment-plan`：为未收到任何款项的订单建立分期收款计划（201）。请求体 `{"items": [{"term_id": "...", "amount_cents": n}, ...]}`：期次标识在订单内唯一、每期金额大于零、全部期次金额之和必须等于订单金额，币种沿用订单。已登记收款的订单（`order_state`）、重复建立（`plan_exists`）、期次重复或合计不等（`invalid_plan`）一律返回 409 整体拒绝，不写入任何期次；订单不存在或跨租户返回 404。建立计划不改变订单的未收金额、已收金额与状态。
- `DELETE /orders/{order_id}/payment-plan`：取消收款计划，仅限订单未收到任何款项时（否则 409 `order_state`；无计划时 409 `plan_not_found`）。取消后订单回到无计划状态，可重新建立；成功返回 200 与订单汇总。
- `GET /orders/{order_id}/payment-plan`：查询收款计划与各期收讫进度。返回每期 `term_id`、`amount_cents`、`paid_cents`、`settled`，以及订单整体 `planned_cents`（已计划金额）与 `settled_count`（已收讫期数）；订单不存在、跨租户或无计划返回 404。
- `POST /orders/{order_id}/refunds`：受理退款单。请求字段 `refund_request_id`（客户端提供的请求去重标识，租户内唯一，与订单标识、退款单标识三者分离）、`amount_cents`（最小货币单位整数，必须大于零，币种沿用订单）。
  - 首次受理：201，创建状态为 `accepted` 的退款单，金额计入订单 `pending_refund_cents` 占用。
  - 同租户同 `refund_request_id` 同内容重放：200，返回首次受理的退款单，不重复占额。
  - 同租户同 `refund_request_id` 但订单或金额不同：409，`detail.code = request_conflict`，首次单据不被改写。
  - 金额使“已退 + 未决 + 本次”超过累计已收：409，`detail.code = amount_exceeds`，整体拒绝、无半生效状态。
  - 订单不存在、不属于本租户：404；订单状态为 `rejected`：409，`detail.code = order_state`。
- `GET /orders/{order_id}/refunds`：查询订单的退款单列表；订单不可见时 404。每张退款单含 `status` 与 `held_cents`（当前占用额：`accepted`/`awaiting_supplement` 时等于退款金额，终态为 0）。
- `GET /refunds/{refund_id}`：按退款单标识查询退款单；不存在或跨租户返回 404。返回体含当前状态 `status` 与当前占用额 `held_cents`。
- `POST /refunds/{refund_id}/complete`：完成退款单。占用额转入 `refunded_cents`，订单净额与状态随之更新；重复完成幂等返回 200；已拒绝的退款单返回 409；处于 `awaiting_supplement` 的退款单不能完成，返回 409（`detail.code = refund_state`）。
- `POST /refunds/{refund_id}/reject`：拒绝退款单并释放占用额。请求体 `{"reason_code": "..."}`，取值 `amount_exceeds`、`order_state`、`request_conflict`、`internal_error`（拒绝与内部错误使用不同码，不混为一类；默认 `internal_error`）。在 `awaiting_supplement` 状态也可拒绝，按当前金额释放占用。
- `POST /refunds/{refund_id}/supplement-return`：把状态为 `accepted` 的退款单退回客户补件。请求体 `{"reason_code": "missing_proof|wrong_account|amount_mismatch", "note": "非空说明"}`。成功 201，退款单进入 `awaiting_supplement`，占用额不变并写一条 `returned` 记录。
  - 等待补件期间以完全相同的事由码与说明重复发起：视为同一操作重放，200 返回同一退款单，不新增记录。
  - 事由码或说明与首次不同：409，`detail.code = supplement_conflict`，首次退回记录不被改写。
  - 已完成或已拒绝的退款单退回补件：409（`detail.code = refund_final`）；退款单不存在或跨租户：404。
- `POST /refunds/{refund_id}/supplement`：补件完成后提交新的退款金额继续审核。请求体 `{"amount_cents": n}`，`n` 必须为大于零的整数，币种沿用订单。成功 201，退款单回到 `accepted`，按新金额重算本单占用（小于原占用立即释放超出部分，大于原占用在守恒允许范围内追加占用），并写一条 `supplemented` 记录（含补件前后金额）。
  - 守恒校验在计入本单占用的前提下进行：将本单占用替换为新金额后，`已退 + 未决` 不得超过累计已收；不满足返回 409，`detail.code = amount_exceeds`，退款单仍停留在 `awaiting_supplement`，金额、占用与记录均不改变。
  - 补件通过后以相同新金额重复提交：幂等重放，200 返回同一退款单，不新增记录；以不同金额再次提交：409。
- `GET /refunds/{refund_id}/supplements`：按退款单标识查询退回补件/补件通过记录，最新记录排在前。每条记录含 `event_type`（`returned`/`supplemented`）、`reason_code`、`note`、`amount_before_cents`、`amount_after_cents`、`created_at`；退款单不存在或跨租户返回 404。
- `GET /health`：返回服务与数据库状态。

### 调用示例

```bash
T="-H X-Tenant:acme"
curl -s -XPOST localhost:8000/orders -H 'Content-Type: application/json' \
  -d '{"tenant":"acme","order_id":"o1","amount_cents":1000,"currency":"CNY"}'
curl -s -XPOST localhost:8000/orders/o1/payments $T -H 'Content-Type: application/json' \
  -d '{"amount_cents":1000}'
# 受理退款（refund_request_id 由客户端生成并负责重放）
curl -s -XPOST localhost:8000/orders/o1/refunds $T -H 'Content-Type: application/json' \
  -d '{"refund_request_id":"req-20261002-001","amount_cents":300}'
# 退款单走完外部打款后：完成或拒绝（拒绝须给原因码）
curl -s -XPOST localhost:8000/refunds/<refund_id>/complete $T
curl -s -XPOST localhost:8000/refunds/<refund_id>/reject $T -H 'Content-Type: application/json' \
  -d '{"reason_code":"internal_error"}'
# 退回补件：accepted -> awaiting_supplement（占用保留；同事由同说明重放返回 200）
curl -s -XPOST localhost:8000/refunds/<refund_id>/supplement-return $T \
  -H 'Content-Type: application/json' \
  -d '{"reason_code":"missing_proof","note":"缺少打款凭证，请补件"}'
# 补件完成后提交新金额：awaiting_supplement -> accepted，占用按新金额重算
curl -s -XPOST localhost:8000/refunds/<refund_id>/supplement $T \
  -H 'Content-Type: application/json' -d '{"amount_cents":200}'
# 查询退回/补件记录（最新在前）
curl -s localhost:8000/refunds/<refund_id>/supplements $T
curl -s localhost:8000/orders/o1 $T
curl -s localhost:8000/orders/o1/refunds $T

# 分期收款计划：仅未收款订单可建立，期次金额合计须等于订单金额
curl -s -XPOST localhost:8000/orders/o2/payment-plan $T -H 'Content-Type: application/json' \
  -d '{"items":[{"term_id":"q1","amount_cents":600},{"term_id":"q2","amount_cents":400}]}'
# 有计划的订单按期收款：一次须收满一个或多个连续期次（600、或 1000，不可 500）
curl -s -XPOST localhost:8000/orders/o2/payments $T -H 'Content-Type: application/json' \
  -d '{"amount_cents":600}'
# 查询各期收讫进度；未收款前也可 DELETE 取消计划后重建
curl -s localhost:8000/orders/o2/payment-plan $T
curl -s -XDELETE localhost:8000/orders/o2/payment-plan $T
```

收款/退款交替后，订单上的恒等式始终成立：`refunded_cents + pending_refund_cents ≤ paid_cents ≤ amount_cents`，`outstanding_cents = amount_cents − paid_cents`，`net_cents = paid_cents − refunded_cents`。存在收款计划时另有：已收金额等于已收讫各期金额之和，各期已收之和等于订单已收金额。

## 数据与配置

- 数据库文件默认 `var/app.sqlite`（不入库）。
- 环境变量：`APP_DB`（数据库路径）、`APP_PORT`（监听端口）、`APP_TENANT_HEADER`（默认 `X-Tenant`）。
- 建表与增量迁移位于 `migrations/`，按文件名顺序应用，已应用的迁移记录在 `schema_migrations` 中；启动时自动执行。

## 当前限制

- 单进程运行，单库写入（写事务使用 `BEGIN IMMEDIATE` 串行化），未做连接池与写并发调优。
- 租户通过请求头声明，未接入真实身份提供方。
- 无缓存层；批量导入只支持小样本同步方式。
- 收款计划仅支持整单一次性建立/取消，不支持计划执行中的变更与对账。
