# Contract Test Report

- 命令：`uv run pytest -q tests/contract`
- 结果：13 passed in 2.60s

验证全部 Draft 2020-12 Schema、生成 shapes freshness、Session/Registration/Acquire golden examples、Provider acquisition 固定 path/request/response、opaque base64 relay、Provider lifecycle 和 data ingress Bearer boundary。Hub Contract 中不再存在 MQTT/Connector/signaling mailbox。
