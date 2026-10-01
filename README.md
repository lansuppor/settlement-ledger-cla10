# 经营单据与结算服务

本地可运行的多租户经营单据服务。当前支持受理订单、按标识读取订单、登记收款并核对未收金额；数据落本地 SQLite 文件库，服务为单进程 HTTP 服务。

## 环境与安装

- Python 3.11
- `python3 -m venv .venv && . .venv/bin/activate && pip install -e .`

## 启动

- `python3 -m app.entry --port 8000`
- 健康检查：`GET /health`

## 测试

- `pytest -q`
- 静态检查：`ruff check .`
- 幂等场景演示（真实 HTTP 调用，含 8 路并发与崩溃恢复）：`python3 scripts/demo_idempotency.py`；对已运行的服务演示：`python3 scripts/demo_idempotency.py --port 8000`

## 已有公开接口

- `POST /orders`：受理订单（幂等）。请求字段 `tenant`、`order_id`、`amount_cents`、`currency`、`idempotency_key`、`request_fingerprint`（后两者均为非空字符串，指纹由调用方对订单内容做稳定摘要得到）。成功返回 201 与订单对象；缺少幂等键或指纹返回 422（参数不合法，不落数据）。同一（租户, 订单标识）重复受理返回 409，detail 为 `order already accepted`，不改变已存在单据。同一（租户, 幂等键）携带相同指纹再次到达视为重放：返回 201、首次受理的同一订单对象，并带响应头 `X-Idempotent-Replay: true`，不新建订单、不改变订单与收款；同键但指纹不同返回 409，detail 为 `idempotency key reused with different request fingerprint`，不覆盖首次受理数据。
- `GET /orders/{order_id}`：按标识读取订单。租户通过请求头 `X-Tenant` 传入；不存在返回 404；跨租户读取返回 404（不泄漏对象是否存在）。
- `POST /orders/{order_id}/payments`：登记收款。请求字段 `amount_cents`；超过未收金额返回 409；成功返回 200 与订单的 `paid_cents`、`outstanding_cents`。
- `GET /health`：返回服务与数据库状态。

## 数据与配置

- 数据库文件默认 `var/app.sqlite`（不入库）。
- 环境变量：`APP_DB`（数据库路径）、`APP_PORT`（监听端口）、`APP_TENANT_HEADER`（默认 `X-Tenant`）。

## 当前限制

- 单进程运行，单库写入，未做连接池与写并发调优。
- 租户通过请求头声明，未接入真实身份提供方。
- 无缓存层；批量导入只支持小样本同步方式。
- 收款只支持整单登记，未实现分期、退款与对账。
