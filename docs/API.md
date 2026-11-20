# 接口

`POST /api/run`：JSON中的csv文本，省略使用样例；或multipart字段file。最大5MB。

`POST /api/m1`至`POST /api/m7`：独立运行到指定模块，JSON结构同运行接口。

`GET /api/history`：历史批次。`POST /api/review`接收id、revision和decision（通过或退回）。`GET /api/evidence.csv`提供单一证据导出。

结构错误422，版本变化409，文件过大413；错误返回code与message。所有页面可从左侧七个模块入口访问。

`GET /api/runs/<id>`读取指定批次；页面与证据CSV通过run_id查询参数绑定批次，模块导航保留该参数。

JSON请求体必须是对象，数组、标量、null、损坏JSON及错误上传字段返回400，且不运行默认样例。

## 炉次结算与用电排放证明

`POST /api/ledger/ingest`：JSON 的 `csv` 文本或 multipart `file`，导入炉次台账（投料、产出、电表读数、验收）。重复导入同一条读数不新增记录；同区间同修订号但内容不同返回隔离提示 `CONFLICT`。

`POST /api/ledger/settle`：按当前台账重算结算，数值变化才追加结算版本。

`GET /api/ledger/heats`：每炉投料、产出、产出率、用电、排放、验收、结算状态、未结算字段、变化说明。`GET /api/ledger/heats/<heat>`：附结算版本历史与证明。

`POST /api/ledger/certificate`：`heat`。炉次未结算或验收未完成返回 409（`UNSETTLED` / `ACCEPTANCE`）；同一结算版本重复出证不生成新版本；生成失败返回 500 `CERT_FAILED` 并保留旧证明。

`GET /api/ledger/certificates[?heat=]`、`GET /api/ledger/certificates/<cert_id>`、`GET /api/ledger/certificate.csv?cert_id=`（或 `heat=` 取现行版）：证明只追加，状态为 有效 / 待换证 / 已替代 / 已失效。

页面入口：`/ledger`（炉次结算）、`/ledger/certificates`（证明）。
