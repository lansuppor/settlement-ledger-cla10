# 经营单据与结算服务

本地可运行的多租户经营单据服务。当前支持受理订单、按标识读取订单、登记收款、退款单受理/完成/拒绝，并核对未收金额与净额；数据落本地 SQLite 文件库，服务为单进程 HTTP 服务。

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
- `POST /orders/{order_id}/payments`：登记收款。请求字段 `amount_cents`；超过未收金额返回 409；成功返回 200 与订单汇总。
- `POST /orders/{order_id}/refunds`：受理退款单。请求字段 `refund_request_id`（客户端提供的请求去重标识，租户内唯一，与订单标识、退款单标识三者分离）、`amount_cents`（最小货币单位整数，必须大于零，币种沿用订单）。
  - 首次受理：201，创建状态为 `accepted` 的退款单，金额计入订单 `pending_refund_cents` 占用。
  - 同租户同 `refund_request_id` 同内容重放：200，返回首次受理的退款单，不重复占额。
  - 同租户同 `refund_request_id` 但订单或金额不同：409，`detail.code = request_conflict`，首次单据不被改写。
  - 金额使“已退 + 未决 + 本次”超过累计已收：409，`detail.code = amount_exceeds`，整体拒绝、无半生效状态。
  - 订单不存在、不属于本租户：404；订单状态为 `rejected`：409，`detail.code = order_state`。
- `GET /orders/{order_id}/refunds`：查询订单的退款单列表；订单不可见时 404。
- `GET /refunds/{refund_id}`：按退款单标识查询退款单；不存在或跨租户返回 404。
- `POST /refunds/{refund_id}/complete`：完成退款单。占用额转入 `refunded_cents`，订单净额与状态随之更新；重复完成幂等返回 200；已拒绝的退款单返回 409。
- `POST /refunds/{refund_id}/reject`：拒绝退款单并释放占用额。请求体 `{"reason_code": "..."}`，取值 `amount_exceeds`、`order_state`、`request_conflict`、`internal_error`（拒绝与内部错误使用不同码，不混为一类；默认 `internal_error`）。
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
curl -s localhost:8000/orders/o1 $T
curl -s localhost:8000/orders/o1/refunds $T
```

收款/退款交替后，订单上的恒等式始终成立：`refunded_cents + pending_refund_cents ≤ paid_cents ≤ amount_cents`，`outstanding_cents = amount_cents − paid_cents`，`net_cents = paid_cents − refunded_cents`。

## 数据与配置

- 数据库文件默认 `var/app.sqlite`（不入库）。
- 环境变量：`APP_DB`（数据库路径）、`APP_PORT`（监听端口）、`APP_TENANT_HEADER`（默认 `X-Tenant`）。
- 建表与增量迁移位于 `migrations/`，按文件名顺序应用，已应用的迁移记录在 `schema_migrations` 中；启动时自动执行。

## 当前限制

- 单进程运行，单库写入（写事务使用 `BEGIN IMMEDIATE` 串行化），未做连接池与写并发调优。
- 租户通过请求头声明，未接入真实身份提供方。
- 无缓存层；批量导入只支持小样本同步方式。
- 收款只支持整单登记，未实现分期与对账。
