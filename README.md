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

## 已有公开接口

- `POST /orders`：受理订单（支持可重放接收）。请求字段 `tenant`、`order_id`、`amount_cents`、`currency`、`idempotency_key`、`request_fingerprint`；后两者为非空字符串：
  - 幂等键唯一范围为（租户, 幂等键），与订单标识的（租户, 订单标识）唯一性相互独立。
  - 首次受理：201，新建订单并登记幂等记录。
  - 同键同指纹重放：201，响应头 `X-Idempotency-Replay: true`，不新建订单、不改变订单与收款，返回首次受理的订单对象快照。
  - 同键不同指纹：422（幂等键被用于不同业务内容），不覆盖首次受理的订单、金额、币种或收款。
  - 订单标识重复（即便使用新幂等键）：409，不改变已存在单据；与 422 明确可区分。
  - 缺少幂等键或指纹（含空串）：400，不落任何数据。
  - 受理为单事务原子写入，并发下同键仅一笔真正受理；提交前崩溃不留下任何记录，重启后用同键重试等价于首次受理。
- `GET /orders/{order_id}`：按标识读取订单。租户通过请求头 `X-Tenant` 传入；不存在返回 404；跨租户读取返回 404（不泄漏对象是否存在）。
- `POST /orders/{order_id}/payments`：登记收款。请求字段 `amount_cents`；超过未收金额返回 409；成功返回 200 与订单的 `paid_cents`、`outstanding_cents`。收款登记不经过幂等键机制。
- `GET /health`：返回服务与数据库状态。

## 幂等受理演示

真实 HTTP 调用的端到端演示（临时库 + 真实 uvicorn 进程，覆盖首次受理、重放、指纹冲突、订单重复、缺参数、并发唯一受理、提交前崩溃恢复）：

- `python scripts/demo_idempotency.py`

其中崩溃场景由环境变量 `APP_CRASH_BEFORE_COMMIT=1` 触发（在事务提交前硬退出，仅供演练与测试使用）。

## 数据与配置

- 数据库文件默认 `var/app.sqlite`（不入库）。
- 环境变量：`APP_DB`（数据库路径）、`APP_PORT`（监听端口）、`APP_TENANT_HEADER`（默认 `X-Tenant`）。

## 当前限制

- 单进程运行，单库写入，未做连接池与写并发调优。
- 租户通过请求头声明，未接入真实身份提供方。
- 无缓存层；批量导入只支持小样本同步方式。
- 收款只支持整单登记，未实现分期、退款与对账。
