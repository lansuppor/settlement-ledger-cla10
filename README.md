# 经营单据与结算服务

本地可运行的多租户经营单据服务。当前支持受理订单、按标识读取订单、登记收款并核对未收金额、受理/完成/拒绝退款单并保持收款与退款金额守恒；数据落本地 SQLite 文件库，服务为单进程 HTTP 服务。

## 环境与安装

- Python 3.11
- `python3 -m venv .venv && . .venv/bin/activate && pip install -e .`

## 启动

- `python3 -m app.entry --port 8000`
- 健康检查：`GET /health`

## 测试

- `pytest -q`
- 静态检查：`ruff check .`

## 已有公开接口

- `POST /orders`：受理订单。请求字段 `tenant`、`order_id`、`amount_cents`、`currency`。成功返回 201 与订单对象；参数不合法返回 400；同一租户重复受理返回 409。
- `GET /orders/{order_id}`：按标识读取订单。租户通过请求头 `X-Tenant` 传入；不存在返回 404；跨租户读取返回 404（不泄漏对象是否存在）。
- `POST /orders/{order_id}/payments`：登记收款。请求字段 `amount_cents`；超过未收金额返回 409；成功返回 200 与订单的 `paid_cents`、`outstanding_cents`。
- `GET /health`：返回服务与数据库状态。

## 退款接口

租户一律通过请求头 `X-Tenant` 传入；退款请求标识（`refund_request_id`）由客户端提供，仅在该租户内唯一，与订单标识分离，专用于请求去重。所有金额为最小货币单位正整数，退款币种沿用原订单币种。

- `POST /orders/{order_id}/refunds`：发起退款（受理）。请求字段 `refund_request_id`、`amount_cents`。
  - 首次受理返回 `201`，退款单状态为 `accepted`，同时占用订单的未决退款额度。
  - 同一（租户, 请求标识）相同内容重复提交：返回 `200` 与首次受理的退款单，不重复记账。
  - 同一请求标识但订单或金额不同：返回 `409`，`detail.reason = request_conflict`，首次单据不被改写。
  - 金额使已退（含未决）超过已收：`409`，`reason = amount_exceeds_paid`，拒绝单持久化、不产生占额。
  - 订单不存在或不属于本租户：统一 `404`，不泄漏对象是否存在。
- `GET /refunds/{refund_request_id}`：按退款请求标识读取退款单；跨租户或不存在均为 `404`。
- `POST /refunds/{refund_request_id}/complete`：完成退款——未决占额转为累计已退，更新净额与订单状态；仅 `accepted` 可完成，否则 `409`（`reason = invalid_refund_status`）。
- `POST /refunds/{refund_request_id}/reject`：拒绝退款——释放未决占额。可选请求体 `{"reason": "..."}` 记录可区分原因；缺省记为 `operator_rejected`。
- `GET /orders/{order_id}/refunds`：读取订单当前收款/退款汇总（`order` 对象）及该订单全部退款单（`refunds` 数组）；订单不存在或跨租户为 `404`。

订单读模型在原有字段上新增：`refunded_cents`（累计已退）、`pending_refund_cents`（已受理未决）、`net_cents`（净额 = 已收 − 已退）。订单状态：`accepted`（已受理）、`settled`（净额等于订单金额）、`unsettled`（曾收款但净额为零）、`rejected`（被拒，当前流程不产生）。

### 调用示例

```bash
# 1. 受理订单并收款
curl -s -XPOST localhost:8000/orders -H 'Content-Type: application/json' \
  -d '{"tenant":"t1","order_id":"o-100","amount_cents":1000,"currency":"CNY"}'
curl -s -XPOST localhost:8000/orders/o-100/payments -H 'X-Tenant: t1' \
  -H 'Content-Type: application/json' -d '{"amount_cents":1000}'

# 2. 发起退款（幂等键 refund_request_id 由客户端生成）
curl -s -XPOST localhost:8000/orders/o-100/refunds -H 'X-Tenant: t1' \
  -H 'Content-Type: application/json' \
  -d '{"refund_request_id":"ref-20261002-0001","amount_cents":300}'

# 3. 完成 / 拒绝退款
curl -s -XPOST localhost:8000/refunds/ref-20261002-0001/complete -H 'X-Tenant: t1'
curl -s -XPOST localhost:8000/refunds/ref-20261002-0001/reject -H 'X-Tenant: t1' \
  -H 'Content-Type: application/json' -d '{"reason":"fraud_check_failed"}'

# 4. 查询退款单与订单收款退款汇总
curl -s localhost:8000/refunds/ref-20261002-0001 -H 'X-Tenant: t1'
curl -s localhost:8000/orders/o-100/refunds -H 'X-Tenant: t1'
```

### 金额守恒

任一时刻成立：`已退（含未决占额） ≤ 已收`；`未收 = 订单金额 − 已收`；`净额 = 已收 − 已退`。受理时按 `可退额度 = 已收 − 已退 − 未决占额` 校验，违反守恒的受理整体拒绝；完成把未决占额转入已退，拒绝释放占额，不存在半生效状态。

## 数据与配置

- 数据库文件默认 `var/app.sqlite`（不入库）。
- 环境变量：`APP_DB`（数据库路径）、`APP_PORT`（监听端口）、`APP_TENANT_HEADER`（默认 `X-Tenant`）。

## 当前限制

- 单进程运行，单库写入，未做连接池与写并发调优。
- 租户通过请求头声明，未接入真实身份提供方。
- 无缓存层；批量导入只支持小样本同步方式。
- 收款只支持整单登记，未实现分期与对账；订单尚无被拒入口（被拒订单的退款拦截已在规则层预留）。
