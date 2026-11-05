#!/usr/bin/env python3
"""Read-only Kubernetes dashboard with local kubeconfig and password login."""
import errno
import hmac
import json
import logging
import mimetypes
import os
import secrets
import signal
import subprocess
import sys
import threading
import time
import uuid
from collections import Counter
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler
from logging.handlers import RotatingFileHandler
from pathlib import Path
from urllib.error import URLError
from urllib.parse import parse_qs, urlparse
from urllib.request import urlopen
try:
    from http.server import ThreadingHTTPServer
except ImportError:  # Python 3.6
    from http.server import HTTPServer
    from socketserver import ThreadingMixIn

    class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
        daemon_threads = True

from kubernetes import client, config
from kubernetes.client.exceptions import ApiException

ROOT = Path(__file__).resolve().parent
LOG_DIR = ROOT / "logs"
LOG_DIR.mkdir(mode=0o700, exist_ok=True)
PID_FILE = LOG_DIR / "server.pid"
URL_FILE = LOG_DIR / "server.url"
PASSWORD_FILE = LOG_DIR / "dashboard.password"
KUBECONFIG_FILE = ROOT / "kubeconfig.yaml"
HOST = os.environ.get("K8S_DASHBOARD_HOST", "0.0.0.0")
PORT = int(os.environ.get("K8S_DASHBOARD_PORT", "8080"))
COOKIE_SECURE = os.environ.get("K8S_COOKIE_SECURE", "0") == "1"
SESSION_COOKIE = "k8s_session"
SESSION_TTL = 12 * 60 * 60
SESSIONS = {}
SESSION_LOCK = threading.Lock()
LOGGER = logging.getLogger("k8s-dashboard")
LOGGER.setLevel(logging.INFO)
handler = RotatingFileHandler(LOG_DIR / "k8s-dashboard.log", maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8")
handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
LOGGER.addHandler(handler)
LOCK = threading.RLock()
API_CLIENT = None
CORE = None
ACTIVE_CONTEXT = ""
CONNECT_ERROR = ""
TASKS = {}
TASK_LOCK = threading.Lock()

RESOURCE_MAP = {
    "deployments": ("apps", "Deployment"), "statefulsets": ("apps", "StatefulSet"),
    "daemonsets": ("apps", "DaemonSet"), "jobs": ("batch", "Job"), "cronjobs": ("batch", "CronJob"),
    "configmaps": ("core", "ConfigMap"), "secrets": ("core", "Secret"),
    "services": ("core", "Service"), "ingresses": ("networking", "Ingress"),
    "pvcs": ("core", "PersistentVolumeClaim"), "pvs": ("core", "PersistentVolume"),
    "storageclasses": ("storage", "StorageClass"), "resourcequotas": ("core", "ResourceQuota"),
}

def resource_api(core, key):
    group, _kind = RESOURCE_MAP[key]
    api_client = core.api_client
    return {"apps": client.AppsV1Api, "batch": client.BatchV1Api, "core": client.CoreV1Api,
            "networking": client.NetworkingV1Api, "storage": client.StorageV1Api}[group](api_client=api_client)

def call_resource(api, key, verb, namespace=None, name=None, body=None):
    methods = {
      "deployments": ("list_namespaced_deployment", "read_namespaced_deployment", "patch_namespaced_deployment", "delete_namespaced_deployment"),
      "statefulsets": ("list_namespaced_stateful_set", "read_namespaced_stateful_set", "patch_namespaced_stateful_set", "delete_namespaced_stateful_set"),
      "daemonsets": ("list_namespaced_daemon_set", "read_namespaced_daemon_set", "patch_namespaced_daemon_set", "delete_namespaced_daemon_set"),
      "jobs": ("list_namespaced_job", "read_namespaced_job", "patch_namespaced_job", "delete_namespaced_job"),
      "cronjobs": ("list_namespaced_cron_job", "read_namespaced_cron_job", "patch_namespaced_cron_job", "delete_namespaced_cron_job"),
      "configmaps": ("list_namespaced_config_map", "read_namespaced_config_map", "patch_namespaced_config_map", "delete_namespaced_config_map"),
      "secrets": ("list_namespaced_secret", "read_namespaced_secret", "patch_namespaced_secret", "delete_namespaced_secret"),
      "services": ("list_namespaced_service", "read_namespaced_service", "patch_namespaced_service", "delete_namespaced_service"),
      "ingresses": ("list_namespaced_ingress", "read_namespaced_ingress", "patch_namespaced_ingress", "delete_namespaced_ingress"),
      "pvcs": ("list_namespaced_persistent_volume_claim", "read_namespaced_persistent_volume_claim", "patch_namespaced_persistent_volume_claim", "delete_namespaced_persistent_volume_claim"),
      "pvs": ("list_persistent_volume", "read_persistent_volume", "patch_persistent_volume", "delete_persistent_volume"),
      "storageclasses": ("list_storage_class", "read_storage_class", "patch_storage_class", "delete_storage_class"),
      "resourcequotas": ("list_namespaced_resource_quota", "read_namespaced_resource_quota", "patch_namespaced_resource_quota", "delete_namespaced_resource_quota")}
    method_name = methods[key][verb]
    if verb == 0 and not namespace and "namespaced" in method_name:
        all_namespaces = {"deployments":"list_deployment_for_all_namespaces", "statefulsets":"list_stateful_set_for_all_namespaces", "daemonsets":"list_daemon_set_for_all_namespaces", "jobs":"list_job_for_all_namespaces", "cronjobs":"list_cron_job_for_all_namespaces", "configmaps":"list_config_map_for_all_namespaces", "secrets":"list_secret_for_all_namespaces", "services":"list_service_for_all_namespaces", "ingresses":"list_ingress_for_all_namespaces", "pvcs":"list_persistent_volume_claim_for_all_namespaces", "resourcequotas":"list_resource_quota_for_all_namespaces"}
        method_name = all_namespaces.get(key, method_name)
    method = getattr(api, method_name)
    if verb == 0:
        return method(namespace, _request_timeout=25) if namespace and "namespaced" in method_name else method(_request_timeout=25)
    if namespace and "namespaced" in methods[key][verb]: return method(name, namespace, body=body, _request_timeout=25) if verb >= 2 else method(name, namespace, _request_timeout=25)
    return method(name, body=body, _request_timeout=25) if verb >= 2 else method(name, _request_timeout=25)

def plain(obj):
    return client.ApiClient().sanitize_for_serialization(obj)


def load_password():
    configured = os.environ.get("K8S_DASHBOARD_PASSWORD", "")
    if configured:
        return configured, False
    if PASSWORD_FILE.exists():
        return PASSWORD_FILE.read_text(encoding="utf-8").strip(), False
    password = secrets.token_urlsafe(24)
    fd = os.open(PASSWORD_FILE, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as out:
        out.write(password + "\n")
    return password, True


ACCESS_PASSWORD, NEW_PASSWORD = load_password()


def response_json(value: dict) -> bytes:
    return json.dumps(value, ensure_ascii=False).encode("utf-8")


def current_core() -> client.CoreV1Api:
    if CORE is None:
        raise RuntimeError(CONNECT_ERROR or "尚未连接 Kubernetes 集群")
    return CORE


def close_client() -> None:
    global API_CLIENT, CORE, ACTIVE_CONTEXT
    if API_CLIENT:
        API_CLIENT.close()
    API_CLIENT, CORE, ACTIVE_CONTEXT = None, None, ""


def connect_local_kubeconfig() -> None:
    global API_CLIENT, CORE, ACTIVE_CONTEXT, CONNECT_ERROR
    if not KUBECONFIG_FILE.is_file():
        CONNECT_ERROR = f"项目目录中未找到 {KUBECONFIG_FILE.name}"
        LOGGER.warning("kubeconfig_missing path=%s", KUBECONFIG_FILE.name)
        return
    candidate = None
    try:
        candidate = config.new_client_from_config(config_file=str(KUBECONFIG_FILE), persist_config=False)
        candidate.configuration.connection_pool_maxsize = 20
        candidate_core = client.CoreV1Api(api_client=candidate)
        candidate_core.list_namespace(limit=1, _request_timeout=12)
        with LOCK:
            close_client()
            API_CLIENT, CORE = candidate, candidate_core
            candidate = None
            ACTIVE_CONTEXT = "current-context"
            CONNECT_ERROR = ""
        LOGGER.info("cluster_connected source=kubeconfig.yaml")
    except Exception as exc:
        if candidate:
            candidate.close()
        CONNECT_ERROR = f"连接失败：{exc}"
        LOGGER.warning("initial_connect_failed type=%s message=%s", type(exc).__name__, str(exc)[:300])


def nodes_payload(nodes):
    result = []
    for node in nodes:
        labels = node.metadata.labels or {}
        roles = sorted(label.split("node-role.kubernetes.io/", 1)[-1] if label.startswith("node-role.kubernetes.io/") else "control-plane" for label in labels if label.startswith("node-role.kubernetes.io/") or label == "kubernetes.io/role")
        conditions = node.status.conditions or []
        ready = any(item.type == "Ready" and item.status == "True" for item in conditions)
        addresses = {item.type: item.address for item in (node.status.addresses or [])}
        result.append({"name": node.metadata.name, "ready": ready, "roles": roles, "version": node.status.node_info.kubelet_version if node.status.node_info else "", "ip": addresses.get("InternalIP", "")})
    return result


class Handler(BaseHTTPRequestHandler):
    server_version = "K8sDashboard/2.0"
    sys_version = ""

    def log_message(self, _format: str, *_args) -> None:
        return

    def send_json(self, status, value, extra_headers=None):
        body = response_json(value)
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "same-origin")
        for key, val in (extra_headers or {}).items():
            self.send_header(key, val)
        self.end_headers()
        self.wfile.write(body)
        LOGGER.info("request method=%s route=%s status=%s", self.command, urlparse(self.path).path, status)

    def read_payload(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        if length < 0 or length > 64 * 1024:
            raise ValueError("请求内容过大")
        raw = self.rfile.read(length) if length else b"{}"
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError("请求内容不是有效 JSON") from exc
        if not isinstance(value, dict):
            raise ValueError("请求内容格式无效")
        return value

    def origin_allowed(self) -> bool:
        origin = self.headers.get("Origin")
        if not origin:
            return True
        parsed = urlparse(origin)
        host = self.headers.get("Host", "").lower()
        return parsed.netloc.lower() == host and parsed.scheme in ("http", "https")

    def authenticated(self) -> bool:
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie", ""))
            token = cookie[SESSION_COOKIE].value
        except (KeyError, Exception):
            return False
        with SESSION_LOCK:
            expiry = SESSIONS.get(token, 0)
            if expiry < time.time():
                SESSIONS.pop(token, None)
                return False
            SESSIONS[token] = time.time() + SESSION_TTL
            return True

    def guard(self, public: bool = False) -> bool:
        if not self.origin_allowed():
            self.send_json(403, {"error": "请求来源无效"})
            return False
        if not public and not self.authenticated():
            self.send_json(401, {"error": "登录已过期，请重新登录"})
            return False
        return True

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/healthz":
            return self.send_json(200, {"ok": True})
        if path == "/api/status":
            if not self.guard(): return
            return self.send_json(200, {"connected": CORE is not None, "context": ACTIVE_CONTEXT, "error": CONNECT_ERROR})
        if not path.startswith("/api/") and path not in ("/api/summary", "/api/pods"):
            return self.serve_static(path)
        if not self.guard(): return
        query = parse_qs(urlparse(self.path).query)
        if path == "/api/task":
            if not self.guard(): return
            with TASK_LOCK: task = TASKS.get(query.get("id", [""])[0])
            if not task: return self.send_json(404, {"error": "任务不存在"})
            return self.send_json(200, task)
        try:
            with LOCK:
                core = current_core()
                if path == "/api/summary":
                    nodes = core.list_node(_request_timeout=20).items
                    namespaces = core.list_namespace(_request_timeout=20).items
                    counts: Counter[str] = Counter()
                    total = 0
                    continuation = None
                    truncated = False
                    while True:
                        page = core.list_pod_for_all_namespaces(limit=500, _continue=continuation, _request_timeout=30)
                        for pod in page.items:
                            total += 1
                            counts[pod.metadata.namespace or "default"] += 1
                        continuation = getattr(page.metadata, "_continue", None)
                        if not continuation: break
                        if total >= 100000:
                            truncated = True
                            break
                    ns = [{"name": item.metadata.name, "pod_count": counts[item.metadata.name]} for item in namespaces]
                    ns.sort(key=lambda item: item["name"])
                    node_data = nodes_payload(nodes)
                    return self.send_json(200, {"pod_count": total, "pods_truncated": truncated, "node_count": len(nodes), "ready_node_count": sum(n["ready"] for n in node_data), "namespace_count": len(namespaces), "namespaces": ns, "nodes": node_data})
                if path == "/api/pods":
                    namespace = query.get("namespace", [""])[0]
                    if not namespace or len(namespace) > 253: return self.send_json(400, {"error": "请先选择 Namespace"})
                    result = []
                    continuation = None
                    while True:
                        page = core.list_namespaced_pod(namespace, limit=500, _continue=continuation, _request_timeout=30)
                        for pod in page.items:
                            containers = pod.spec.containers or []
                            statuses = pod.status.container_statuses or []
                            result.append({"name": pod.metadata.name, "phase": pod.status.phase or "Unknown", "ready": f"{sum(1 for item in statuses if item.ready)}/{len(containers)}", "node": pod.spec.node_name or "", "restarts": sum(item.restart_count or 0 for item in statuses), "created": pod.metadata.creation_timestamp.isoformat(timespec="seconds") if pod.metadata.creation_timestamp else ""})
                        continuation = getattr(page.metadata, "_continue", None)
                        if not continuation: break
                        if len(result) >= 3000: break
                    return self.send_json(200, {"pods": result[:3000], "truncated": bool(continuation), "limit": 3000})
                if path == "/api/nodes":
                    nodes = core.list_node(_request_timeout=25).items
                    data = nodes_payload(nodes)
                    for item, node in zip(data, nodes):
                        item.update({"conditions": plain(node.status.conditions or []), "capacity": plain(node.status.capacity or {}), "allocatable": plain(node.status.allocatable or {}), "info": plain(node.status.node_info) if node.status.node_info else {}, "labels": node.metadata.labels or {}, "taints": plain(node.spec.taints or []), "pods": [plain(p) for p in core.list_pod_for_all_namespaces(field_selector="spec.nodeName=" + node.metadata.name, _request_timeout=20).items]})
                    return self.send_json(200, {"nodes": data})
                if path == "/api/metrics":
                    custom = client.CustomObjectsApi(api_client=API_CLIENT)
                    kind = query.get("kind", ["nodes"])[0]
                    if kind not in ("nodes", "pods"):
                        LOGGER.warning("metrics_request_invalid kind=%s", kind)
                        return self.send_json(400, {"error": "kind 仅支持 nodes 或 pods"})
                    resource = "nodes" if kind == "nodes" else "pods"
                    LOGGER.info("metrics_request_start api=metrics.k8s.io/v1beta1 resource=%s", resource)
                    value = custom.list_cluster_custom_object("metrics.k8s.io", "v1beta1", resource, _request_timeout=15)
                    LOGGER.info("metrics_request_success api=metrics.k8s.io/v1beta1 resource=%s count=%s", resource, len(value.get("items", [])))
                    return self.send_json(200, {"items": value.get("items", [])})
                if path == "/api/namespaces":
                    result = []
                    for ns in core.list_namespace(_request_timeout=25).items:
                        quotas = core.list_namespaced_resource_quota(ns.metadata.name, _request_timeout=15).items
                        result.append({"name": ns.metadata.name, "status": ns.status.phase, "labels": ns.metadata.labels or {}, "quotas": [plain(q) for q in quotas]})
                    return self.send_json(200, {"namespaces": result})
                if path == "/api/resource":
                    key = query.get("type", [""])[0]; namespace = query.get("namespace", [""])[0]; name = query.get("name", [""])[0]
                    if key not in RESOURCE_MAP: return self.send_json(400, {"error": "不支持的资源类型"})
                    api = resource_api(core, key)
                    obj = call_resource(api, key, 1 if name else 0, namespace or None, name or None)
                    if name:
                        doc = plain(obj)
                        return self.send_json(200, {"resource": doc, "yaml": __import__('yaml').safe_dump(doc, allow_unicode=True, sort_keys=False)})
                    return self.send_json(200, {"items": plain(obj.items)})
                if path == "/api/pod":
                    namespace = query.get("namespace", [""])[0]; name = query.get("name", [""])[0]
                    if not namespace or not name: return self.send_json(400, {"error": "缺少 Pod 参数"})
                    pod = core.read_namespaced_pod(name, namespace, _request_timeout=20)
                    events = core.list_namespaced_event(namespace, field_selector="involvedObject.name=" + name, _request_timeout=20).items
                    return self.send_json(200, {"pod": plain(pod), "events": plain(events), "describe": "\n".join(["Name: " + name, "Namespace: " + namespace, "Phase: " + str(pod.status.phase), "Node: " + str(pod.spec.node_name or ""), "IP: " + str(pod.status.pod_ip or ""), "Conditions: " + json.dumps(plain(pod.status.conditions or []), ensure_ascii=False)])})
                if path == "/api/logs":
                    namespace = query.get("namespace", [""])[0]; name = query.get("name", [""])[0]; container = query.get("container", [""])[0]
                    logs = core.read_namespaced_pod_log(name, namespace, container=container or None, tail_lines=500, timestamps=True, _request_timeout=25)
                    return self.send_json(200, {"logs": logs})
            return self.send_json(404, {"error": "接口未找到"})
        except ApiException as exc:
            LOGGER.warning("kubernetes_api_error status=%s reason=%s", exc.status, exc.reason)
            if path == "/api/metrics": LOGGER.error("metrics_request_failed status=%s reason=%s body=%s", exc.status, exc.reason, (exc.body or "")[:1000])
            return self.send_json(502, {"error": f"Kubernetes API 错误 {exc.status}: {exc.reason or '请求失败'}"})
        except Exception as exc:
            LOGGER.exception("get_failed route=%s", path)
            if path == "/api/metrics": LOGGER.exception("metrics_request_failed route=%s", path)
            return self.send_json(502, {"error": f"读取集群数据失败：{exc}"})

    def serve_static(self, path: str) -> None:
        relative = "index.html" if path in ("", "/") else path.lstrip("/")
        candidate = (ROOT / relative).resolve()
        if ROOT not in candidate.parents or not candidate.is_file():
            return self.send_json(404, {"error": "未找到页面"})
        body = candidate.read_bytes()
        content_type = mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", f"{content_type}; charset=utf-8" if content_type.startswith("text/") or content_type == "application/javascript" else content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if not self.guard(public=path == "/api/login"): return
        global CONNECT_ERROR
        try:
            payload = self.read_payload()
            if path == "/api/login":
                if not hmac.compare_digest(str(payload.get("password", "")), ACCESS_PASSWORD):
                    return self.send_json(401, {"error": "访问密码不正确"})
                token = secrets.token_urlsafe(32)
                with SESSION_LOCK: SESSIONS[token] = time.time() + SESSION_TTL
                cookie = f"{SESSION_COOKIE}={token}; Path=/; HttpOnly; SameSite=Strict; Max-Age={SESSION_TTL}" + ("; Secure" if COOKIE_SECURE else "")
                return self.send_json(200, {"ok": True}, {"Set-Cookie": cookie})
            if path == "/api/logout":
                cookie = SimpleCookie()
                try:
                    cookie.load(self.headers.get("Cookie", "")); token = cookie[SESSION_COOKIE].value
                    with SESSION_LOCK: SESSIONS.pop(token, None)
                except Exception: pass
                return self.send_json(200, {"ok": True}, {"Set-Cookie": f"{SESSION_COOKIE}=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0"})
            if path == "/api/connect":
                connect_local_kubeconfig()
                if CORE is None: return self.send_json(502, {"error": CONNECT_ERROR or "连接失败"})
                return self.send_json(200, {"connected": True, "context": ACTIVE_CONTEXT})
            if path == "/api/resource/apply" or path == "/api/resource/patch" or path == "/api/resource/delete":
                import yaml
                docs = list(yaml.safe_load_all(payload.get("yaml", ""))) if path.endswith("apply") else ([yaml.safe_load(payload.get("yaml", ""))] if payload.get("yaml") else [payload.get("body", {})])
                def task_runner(task_id):
                    state = {"id": task_id, "status": "running", "steps": [], "error": ""}
                    with TASK_LOCK: TASKS[task_id] = state
                    try:
                        for doc in docs:
                            if not isinstance(doc, dict) or not doc.get("kind"): continue
                            meta = doc.get("metadata") or {}; name = meta.get("name"); ns = meta.get("namespace", "default")
                            kind = doc["kind"].lower()
                            key = {v[1].lower(): k for k, v in RESOURCE_MAP.items()}.get(kind)
                            if path.endswith("apply"):
                                if not key: raise ValueError("暂不支持该资源类型：" + doc["kind"])
                                api = resource_api(current_core(), key)
                                try: call_resource(api, key, 1, ns, name)
                                except ApiException as exc:
                                    if exc.status != 404: raise
                                    call_resource(api, key, 0, ns)
                                    fn = getattr(api, {"deployments":"create_namespaced_deployment", "statefulsets":"create_namespaced_stateful_set", "daemonsets":"create_namespaced_daemon_set", "jobs":"create_namespaced_job", "cronjobs":"create_namespaced_cron_job", "configmaps":"create_namespaced_config_map", "secrets":"create_namespaced_secret", "services":"create_namespaced_service", "ingresses":"create_namespaced_ingress", "pvcs":"create_namespaced_persistent_volume_claim"}.get(key, ""), None)
                                    if fn is None: raise ValueError("此资源暂不支持创建")
                                    fn(ns, doc, _request_timeout=25)
                                else: call_resource(api, key, 2, ns, name, doc)
                            elif path.endswith("patch"):
                                if key not in RESOURCE_MAP: raise ValueError("不支持的资源类型")
                                patch_doc = dict(doc)
                                meta_patch = dict(patch_doc.get("metadata") or {})
                                for field in ("creationTimestamp", "deletionTimestamp", "deletionGracePeriodSeconds", "managedFields", "resourceVersion", "selfLink", "uid", "generation"):
                                    meta_patch.pop(field, None)
                                patch_doc["metadata"] = meta_patch
                                patch_doc.pop("status", None)
                                call_resource(resource_api(current_core(), key), key, 2, ns, name, patch_doc)
                            else:
                                if key not in RESOURCE_MAP: raise ValueError("不支持的资源类型")
                                call_resource(resource_api(current_core(), key), key, 3, ns, name)
                            state["steps"].append({"resource": doc["kind"] + "/" + str(name), "status": "complete"})
                            LOGGER.info("resource_task_step_complete task_id=%s action=%s kind=%s namespace=%s name=%s", task_id, path.rsplit("/", 1)[-1], doc["kind"], ns, name)
                        state["status"] = "complete"
                        LOGGER.info("resource_task_complete task_id=%s steps=%s", task_id, len(state["steps"]))
                    except Exception as exc:
                        state["status"] = "failed"; state["error"] = str(exc)[:500]
                        LOGGER.exception("resource_task_failed task_id=%s", task_id)
                task_id = uuid.uuid4().hex
                threading.Thread(target=task_runner, args=(task_id,), daemon=True).start()
                return self.send_json(202, {"task_id": task_id})
            if path == "/api/namespace":
                name = str(payload.get("name", ""))
                if not name: return self.send_json(400, {"error": "请输入 Namespace 名称"})
                current_core().create_namespace(client.V1Namespace(metadata=client.V1ObjectMeta(name=name)), _request_timeout=20)
                return self.send_json(201, {"ok": True})
            if path == "/api/node/schedule":
                name = payload.get("name"); cordon = bool(payload.get("cordon"))
                current_core().patch_node(name, {"spec": {"unschedulable": cordon}}, _request_timeout=20)
                return self.send_json(200, {"ok": True})
            if path == "/api/task":
                with TASK_LOCK: task = TASKS.get(payload.get("task_id"))
                if not task: return self.send_json(404, {"error": "任务不存在"})
                return self.send_json(200, task)
            if path == "/api/pod/evict":
                namespace, name = payload.get("namespace"), payload.get("name")
                eviction = {"apiVersion":"policy/v1", "kind":"Eviction", "metadata":{"name":name,"namespace":namespace}}
                client.PolicyV1Api(api_client=API_CLIENT).create_namespaced_pod_eviction(name, namespace, eviction, _request_timeout=25)
                return self.send_json(202, {"ok": True})
            if path == "/api/pod/exec":
                return self.send_json(501, {"error": "WebSocket exec 需部署终端代理；当前服务不开放原始命令通道"})
            return self.send_json(404, {"error": "接口未找到"})
        except ValueError as exc:
            return self.send_json(400, {"error": str(exc)})
        except Exception as exc:
            LOGGER.exception("post_failed route=%s", path)
            return self.send_json(500, {"error": f"请求失败：{exc}"})


def run_server() -> None:
    global PORT
    httpd = None
    for port in range(PORT, min(PORT + 11, 65536)):
        try:
            httpd = ThreadingHTTPServer((HOST, port), Handler)
            PORT = port
            break
        except OSError as exc:
            if exc.errno != errno.EADDRINUSE: raise
    if httpd is None: raise RuntimeError(f"端口 {PORT}–{min(PORT + 10, 65535)} 均被占用")
    health_url = f"http://127.0.0.1:{PORT}"
    URL_FILE.write_text(health_url, encoding="utf-8")
    LOGGER.info("server_started host=%s port=%s", HOST, PORT)
    connect_local_kubeconfig()
    print(f"Kubernetes 面板已启动：监听 {HOST}:{PORT}；浏览器访问 http://服务器公网IP:{PORT}。", flush=True)
    if NEW_PASSWORD and len(sys.argv) > 1 and sys.argv[1].lower() == "foreground":
        print(f"首次访问密码：{ACCESS_PASSWORD}\n密码副本已保存到：{PASSWORD_FILE}", flush=True)
    if CONNECT_ERROR: print(f"集群连接状态：{CONNECT_ERROR}", flush=True)

    def stop_handler(_signum, _frame): raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, stop_handler)
    try: httpd.serve_forever()
    except KeyboardInterrupt: pass
    finally:
        with LOCK: close_client()
        httpd.server_close()
        safe_unlink(URL_FILE)
        try:
            if PID_FILE.read_text(encoding="utf-8").strip() == str(os.getpid()): safe_unlink(PID_FILE)
        except OSError: pass
        LOGGER.info("server_stopped")


def process_alive(pid: int) -> bool:
    try: os.kill(pid, 0); return True
    except ProcessLookupError: return False
    except PermissionError: return True


def service_responds(url: str) -> bool:
    try:
        with urlopen(url.rstrip("/") + "/healthz", timeout=1) as response: return response.status == 200
    except (OSError, URLError, ValueError): return False


def safe_unlink(path):
    try:
        path.unlink()
    except OSError:
        pass


def start_server() -> int:
    if PID_FILE.exists():
        try: old_pid = int(PID_FILE.read_text(encoding="utf-8").strip())
        except (OSError, ValueError): old_pid = -1
        if old_pid > 0 and process_alive(old_pid):
            url = URL_FILE.read_text(encoding="utf-8").strip() if URL_FILE.exists() else ""
            if url and service_responds(url): print(f"Kubernetes 面板已在后台运行，监听 {HOST}:{PORT}；浏览器访问 http://服务器公网IP:{urlparse(url).port}"); return 0
            print("后台服务正在启动，请稍后查看 logs/launcher.log。"); return 1
        safe_unlink(PID_FILE); safe_unlink(URL_FILE)
    output = (LOG_DIR / "launcher.log").open("ab")
    try:
        child = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "run"], cwd=ROOT, stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT, start_new_session=True, close_fds=True)
    finally: output.close()
    PID_FILE.write_text(str(child.pid), encoding="utf-8")
    for _ in range(100):
        if child.poll() is not None:
            safe_unlink(PID_FILE); print(f"启动失败，请查看日志：{LOG_DIR / 'launcher.log'}"); return 1
        if URL_FILE.exists():
            url = URL_FILE.read_text(encoding="utf-8").strip()
            if url and service_responds(url):
                print(f"Kubernetes 面板已在后台启动，监听地址：{HOST}:{PORT}")
                print(f"浏览器请访问：http://服务器公网IP:{urlparse(url).port}（云安全组需放行此端口）")
                if NEW_PASSWORD: print(f"首次访问密码：{ACCESS_PASSWORD}")
                return 0
        time.sleep(0.1)
    print(f"服务仍在启动。日志：{LOG_DIR / 'launcher.log'}")
    return 0


def stop_server() -> int:
    try: pid = int(PID_FILE.read_text(encoding="utf-8").strip())
    except (FileNotFoundError, OSError, ValueError): print("没有由启动脚本启动的后台服务。"); return 0
    if not process_alive(pid):
        safe_unlink(PID_FILE); safe_unlink(URL_FILE); print("后台服务已经停止。"); return 0
    try: os.kill(pid, signal.SIGTERM)
    except ProcessLookupError: pass
    for _ in range(50):
        if not process_alive(pid): break
        time.sleep(0.1)
    if process_alive(pid): print(f"已发送停止信号（PID {pid}）。")
    else: safe_unlink(PID_FILE); safe_unlink(URL_FILE); print("Kubernetes 面板后台服务已停止。")
    return 0


if __name__ == "__main__":
    command = sys.argv[1].lower() if len(sys.argv) > 1 else "start"
    if command == "start": raise SystemExit(start_server())
    if command == "stop": raise SystemExit(stop_server())
    if command == "restart": stop_server(); raise SystemExit(start_server())
    if command == "status":
        if PID_FILE.exists() and URL_FILE.exists():
            url = URL_FILE.read_text(encoding="utf-8").strip()
            if service_responds(url): print(f"正在运行：{url}"); raise SystemExit(0)
        print("后台服务未运行。"); raise SystemExit(1)
    if command in ("run", "foreground"): run_server()
    else: print("用法：python server.py [start|stop|restart|status|foreground]；不带参数时在后台启动。"); raise SystemExit(2)
