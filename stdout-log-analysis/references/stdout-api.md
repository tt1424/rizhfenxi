# Stdout API Notes

默认凭据文件：

```text
~/.codex/secrets/stdout-log.env
```

当前已知：

- Stdout API: `https://vlogs-api.aispeech.com/stdout/select/logsql/query`
- Syslog API: `https://vlogs-api.aispeech.com/syslog/select/logsql/query`
- 使用 HTTP Basic Auth。
- 请求方式为 POST form-urlencoded，等价于 curl 的多个 `--data`。
- 必须传 `query`、`start`、`end`、`limit`。
- `start/end` 支持相对时间，例如 `72h` 到 `now`。
- 查询窗口不能超过 VictoriaLogs 服务端限制，目前实测上限约 75 小时，建议默认 `72h`。
- Stdout 推荐索引：`k8scluster`、`pod_project`、`pod_name`。
- Syslog 推荐索引：`k8scluster`、`app_name`。
- 脚本默认查 `stdout`；查 Syslog 时加 `--log-type syslog`，此时 `--service` 会映射为 `app_name`。

示例：

```bash
curl --user "your_username:your_password" \
  https://vlogs-api.aispeech.com/stdout/select/logsql/query \
  --data 'query={k8scluster="d1-prod", pod_project="ddsserver-fullduplex"} AND "error" | fields _time, pod_name, _msg' \
  --data 'start=5m' \
  --data 'end=now' \
  --data 'limit=10'
```

```bash
curl --user "your_username:your_password" \
  https://vlogs-api.aispeech.com/syslog/select/logsql/query \
  --data 'query={k8scluster="d1-prod", app_name="dds"} | fields _time, app_name, k8scluster, _msg' \
  --data 'start=5m' \
  --data 'end=now' \
  --data 'limit=10'
```
