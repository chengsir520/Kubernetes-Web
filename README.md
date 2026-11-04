# Kubernetes 集群浏览器

轻量只读 Kubernetes Web 面板。页面左侧提供集群总览、Pods、Nodes、Namespaces 导航；从 Namespace 可直接筛选 Pods。依赖固定为兼容 Kubernetes 1.28 和 Python 3.6 的版本。

## Linux 部署

把项目文件复制到 Linux 服务器目录，确保 `kubeconfig.yaml` 与 `server.py`、`k8s.sh` 在同一目录。面板启动时会自动读取此文件的 `current-context`，不上传或改写 kubeconfig。运行用户需要有读取 Pod、Node 和 Namespace 的 RBAC 权限，服务器本身也必须能访问 kubeconfig 中配置的 Kubernetes API 地址。

```bash
chmod +x k8s.sh
./k8s.sh install
./k8s.sh start
```

后台管理：

```bash
./k8s.sh status
./k8s.sh restart
./k8s.sh stop
```

默认监听 `0.0.0.0:8080`，浏览器访问 `http://服务器公网IP:8080`。首次启动会生成访问密码并打印一次；副本保存在 `logs/dashboard.password`（文件权限仅限当前用户）。也可设置环境变量 `K8S_DASHBOARD_PASSWORD` 指定密码。

可通过环境变量调整监听地址/端口：

```bash
K8S_DASHBOARD_HOST=0.0.0.0 K8S_DASHBOARD_PORT=8080 ./k8s.sh start
```

云平台安全组和服务器防火墙需要放行所选端口。建议只允许你的固定公网 IP 访问，并通过 HTTPS 反向代理保护登录凭据；如果由 HTTPS 代理访问，可给服务进程配置 `K8S_COOKIE_SECURE=1`。

## 功能

- 集群 Pod、Node、Ready Node、Namespace 数量概览。
- Namespace Pod 数量和快捷入口。
- Node 状态、角色、Kubernetes 版本和内部 IP。
- 按 Namespace 查看 Pod 状态、容器就绪数、Node、重启次数和创建时间。
- 只读，不会创建、修改或删除 Kubernetes 资源。
- 日志写入 `logs/k8s-dashboard.log` 和 `logs/launcher.log`，不记录访问密码或 kubeconfig 内容。
- Pod 总数最多扫描 100,000 个；单个 Namespace 最多展示 3,000 个 Pod。
