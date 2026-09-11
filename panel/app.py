#!/usr/bin/env python3
"""
sing-box Gateway Management Panel
=================================
Alpine Linux 旁路由网关管理面板 (Flask)

功能:
  - sing-box 状态查看 / 启停控制
  - 配置文件在线编辑 / 模板切换
  - 订阅管理 (添加/删除/更新, 自动合并入 config.json)
  - 订阅格式转换 (V2Ray base64 / Clash YAML → sing-box JSON)
  - 核心版本检测 / 一键升级
  - 网络信息 / 透明网关状态

依赖: flask, requests, pyyaml
"""

import argparse
import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import threading
import time
import urllib.parse
from datetime import datetime
from pathlib import Path

import requests

try:
    import yaml
except ImportError:
    yaml = None

from flask import Flask, request, jsonify, Response, render_template_string

app = Flask(__name__)

# ============================================================
# 路径常量
# ============================================================
SINGBOX_BIN = os.environ.get("SINGBOX_BIN", "/usr/local/bin/sing-box")
CONFIG_DIR = os.environ.get("CONFIG_DIR", "/etc/sing-box")
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.json")
SUB_FILE = os.path.join(CONFIG_DIR, "subscriptions.json")
TEMPLATE_DIR = os.path.join(CONFIG_DIR, "templates")
LOG_DIR = "/var/log/sing-box"
LOG_FILE = os.path.join(LOG_DIR, "sing-box-stderr.log")
PANEL_DIR = "/opt/singbox-gateway"
PANEL_CONFIG = os.path.join(CONFIG_DIR, "panel.json")
DASHBOARD_DIR = os.path.join(PANEL_DIR, "dashboards")
CLASH_API = "http://127.0.0.1:9090"

# ============================================================
# 工具函数
# ============================================================

def run_cmd(cmd, timeout=30):
    """执行 shell 命令, 返回 (returncode, stdout, stderr)"""
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout.strip(), r.stderr.strip()
    except subprocess.TimeoutExpired:
        return -1, "", "command timeout"
    except Exception as e:
        return -1, "", str(e)


def get_panel_config():
    defaults = {
        "panel_port": 9999,
        "tproxy_port": 7893,
        "lan_subnet": "192.168.1.0/24",
        "gateway_mode": "tproxy",
        "clash_secret": "",
    }
    if os.path.exists(PANEL_CONFIG):
        try:
            with open(PANEL_CONFIG) as f:
                defaults.update(json.load(f))
        except Exception:
            pass
    return defaults


def save_panel_config(cfg):
    with open(PANEL_CONFIG, "w") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)


def get_singbox_version():
    code, out, _ = run_cmd(f'"{SINGBOX_BIN}" version')
    if code == 0:
        m = re.search(r'sing-box version\s+(\S+)', out)
        if m:
            return m.group(1)
    return "unknown"


def get_latest_version():
    try:
        r = requests.get(
            "https://api.github.com/repos/SagerNet/sing-box/releases/latest",
            timeout=10,
            headers={"User-Agent": "singbox-panel"},
        )
        if r.status_code == 200:
            return r.json().get("tag_name", "unknown")
    except Exception:
        pass
    return "unknown"


def get_service_status():
    code, _, _ = run_cmd("rc-service sing-box status")
    # OpenRC: status returns 0 if running, 3 if stopped
    return code == 0


def get_uptime():
    code, out, _ = run_cmd("rc-service sing-box status 2>&1")
    # 粗略估算: 用进程启动时间
    code2, out2, _ = run_cmd("pgrep -x sing-box")
    if code2 == 0 and out2:
        code3, start_str, _ = run_cmd(
            f"ps -o etimes= -p {out2.splitlines()[0]} 2>/dev/null"
        )
        if code3 == 0 and start_str.strip().isdigit():
            secs = int(start_str.strip())
            h, m, s = secs // 3600, (secs % 3600) // 60, secs % 60
            return f"{h}h {m}m {s}s"
    return "—"


def get_clash_info():
    """通过 clash API 获取实时连接数"""
    cfg = get_panel_config()
    secret = cfg.get("clash_secret", "")
    headers = {}
    if secret:
        headers["Authorization"] = f"Bearer {secret}"
    try:
        r = requests.get(f"{CLASH_API}/connections", headers=headers, timeout=3)
        if r.status_code == 200:
            data = r.json()
            return {"connections": len(data.get("connections", [])),
                    "upload": data.get("upload", 0),
                    "download": data.get("download", 0)}
    except Exception:
        pass
    return None


def get_network_info():
    code, ip_out, _ = run_cmd("ip -4 addr show scope global | grep inet | awk '{print $2}'")
    code2, gw_out, _ = run_cmd("ip route show default | awk '{print $3}' | head -1")
    code3, iface_out, _ = run_cmd("ip route show default | awk '{print $5}' | head -1")
    code4, fwd_out, _ = run_cmd("cat /proc/sys/net/ipv4/ip_forward")
    code5, _, _ = run_cmd("nft list ruleset 2>/dev/null | grep -q sing-box; echo $?")
    return {
        "ip_addresses": ip_out.strip().split("\n") if ip_out else [],
        "default_gateway": gw_out.strip() if gw_out else "",
        "interface": iface_out.strip() if iface_out else "",
        "ip_forward": fwd_out.strip() == "1" if fwd_out else False,
        "nftables_active": code5 == 0,
    }


def get_recent_logs(lines=50):
    if not os.path.exists(LOG_FILE):
        # 尝试 stdout 日志
        alt = os.path.join(LOG_DIR, "sing-box-stdout.log")
        if os.path.exists(alt):
            log_file = alt
        else:
            return "无日志"
    else:
        log_file = LOG_FILE
    code, out, _ = run_cmd(f"tail -n {lines} '{log_file}'")
    return out if out else "日志为空"


# ============================================================
# 仪表盘 (Clash API 前端) 管理
# ============================================================
# 内置仪表盘注册表: name → (仓库, 资源名匹配, 描述)
# 这些是预编译的静态前端, 下载解压后本地托管, 通过 :9999 访问
DASHBOARD_REGISTRY = {
    "metacubexd": {
        "repo": "MetaCubeX/metacubexd",
        "asset": "compressed-dist.tgz",
        "desc": "MetaCubeX 官方面板 (功能最全, 推荐)",
    },
    "zashboard": {
        "repo": "Zephyruso/zashboard",
        "asset": "dist.zip",
        "desc": "Zashboard 现代面板 (含中文字体)",
    },
    "zashboard-lite": {
        "repo": "Zephyruso/zashboard",
        "asset": "dist-no-fonts.zip",
        "desc": "Zashboard 精简版 (不含字体, 体积小)",
    },
    "yacd": {
        "repo": "haishanh/yacd",
        "asset": "yacd.tar.xz",
        "desc": "yacd 经典面板",
    },
}


def get_active_dashboard():
    cfg = get_panel_config()
    name = cfg.get("active_dashboard", "")
    if name and os.path.exists(os.path.join(DASHBOARD_DIR, name, "index.html")):
        return name
    # 自动检测已安装的第一个
    if os.path.isdir(DASHBOARD_DIR):
        for d in sorted(os.listdir(DASHBOARD_DIR)):
            if os.path.exists(os.path.join(DASHBOARD_DIR, d, "index.html")):
                return d
    return ""


def list_dashboards():
    """返回所有仪表盘状态: 注册表中的 + 已安装但不在注册表的"""
    installed = []
    if os.path.isdir(DASHBOARD_DIR):
        installed = [d for d in os.listdir(DASHBOARD_DIR)
                     if os.path.exists(os.path.join(DASHBOARD_DIR, d, "index.html"))]
    result = []
    for name, info in DASHBOARD_REGISTRY.items():
        result.append({
            "name": name,
            "repo": info["repo"],
            "desc": info["desc"],
            "installed": name in installed,
            "in_registry": True,
        })
    # 已安装但不在注册表 (手动放入的)
    for d in installed:
        if d not in DASHBOARD_REGISTRY:
            result.append({"name": d, "repo": "", "desc": "自定义仪表盘",
                           "installed": True, "in_registry": False})
    active = get_active_dashboard()
    for r in result:
        r["active"] = (r["name"] == active)
    return result


def install_dashboard(name):
    """下载并安装仪表盘: 查 GitHub release → 匹配资源 → 解压 → 部署"""
    info = DASHBOARD_REGISTRY.get(name)
    if not info:
        return False, f"未知的仪表盘: {name}"
    repo = info["repo"]
    asset_match = info["asset"]
    # 查最新 release
    try:
        r = requests.get(
            f"https://api.github.com/repos/{repo}/releases/latest",
            timeout=15, headers={"User-Agent": "singbox-panel"},
        )
        if r.status_code != 200:
            return False, f"GitHub API 错误: {r.status_code}"
        release = r.json()
    except Exception as e:
        return False, f"获取 release 失败: {e}"
    # 匹配资源 (精确名 → 后缀兜底)
    asset_url = ""
    fallback_url = ""
    for a in release.get("assets", []):
        aname = a.get("name", "")
        if aname == asset_match:
            asset_url = a["browser_download_url"]
            break
        if not fallback_url and any(aname.endswith(ext) for ext in
                                    (".tgz", ".tar.gz", ".tar.xz", ".zip")):
            fallback_url = a["browser_download_url"]
    if not asset_url:
        asset_url = fallback_url
    if not asset_url:
        return False, f"release 中未找到匹配资源 (期望 {asset_match})"
    # 下载
    tmp_archive = f"/tmp/dashboard-{name}.archive"
    try:
        rr = requests.get(asset_url, timeout=120,
                         headers={"User-Agent": "singbox-panel"})
        rr.raise_for_status()
        with open(tmp_archive, "wb") as f:
            f.write(rr.content)
    except Exception as e:
        return False, f"下载失败: {e}"
    # 解压到临时目录
    import tempfile, zipfile, tarfile
    tmp_extract = tempfile.mkdtemp(prefix="dash-")
    try:
        if asset_url.endswith(".zip"):
            with zipfile.ZipFile(tmp_archive) as zf:
                zf.extractall(tmp_extract)
        elif asset_url.endswith((".tar.xz",)):
            with tarfile.open(tmp_archive, "r:xz") as tf:
                tf.extractall(tmp_extract)
        else:  # tgz / tar.gz
            with tarfile.open(tmp_archive, "r:gz") as tf:
                tf.extractall(tmp_extract)
    except Exception as e:
        shutil.rmtree(tmp_extract, ignore_errors=True)
        os.unlink(tmp_archive)
        return False, f"解压失败: {e}"
    os.unlink(tmp_archive)
    # 找到 index.html 所在目录作为仪表盘根
    root = None
    for dirpath, dirnames, filenames in os.walk(tmp_extract):
        if "index.html" in filenames:
            root = dirpath
            break
    if not root:
        shutil.rmtree(tmp_extract, ignore_errors=True)
        return False, "解压后未找到 index.html (可能资源结构不符)"
    # 部署到 DASHBOARD_DIR/<name>/
    target = os.path.join(DASHBOARD_DIR, name)
    if os.path.exists(target):
        shutil.rmtree(target)
    os.makedirs(DASHBOARD_DIR, exist_ok=True)
    shutil.move(root, target)
    shutil.rmtree(tmp_extract, ignore_errors=True)
    return True, f"仪表盘 {name} 安装成功 (版本 {release.get('tag_name','?')})"


# ============================================================
# 订阅解析器 — 核心: 各种格式 → sing-box outbounds
# ============================================================

def safe_b64decode(data):
    """容错 base64 解码 (补 padding)"""
    data = data.strip()
    data = data.replace("-", "+").replace("_", "/")
    padding = 4 - len(data) % 4
    if padding < 4:
        data += "=" * padding
    try:
        return base64.b64decode(data).decode("utf-8")
    except Exception:
        return None


def parse_ss(uri):
    """解析 ss:// 链接 → sing-box shadowsocks outbound"""
    try:
        if not uri.startswith("ss://"):
            return None
        # 去掉 ss:// 和 #name
        body = uri[5:]
        name = ""
        if "#" in body:
            body, name_part = body.split("#", 1)
            name = urllib.parse.unquote(name_part)

        # SIP002: ss://base64(method:password)@host:port  或  ss://method:password@host:port
        if "@" in body:
            userinfo, hostport = body.rsplit("@", 1)
            if ":" in userinfo and ":" not in userinfo.split(":")[0]:
                # 非编码: method:password
                parts = userinfo.split(":")
                method = parts[0]
                password = ":".join(parts[1:])
            else:
                decoded = safe_b64decode(userinfo)
                if decoded and ":" in decoded:
                    method, password = decoded.split(":", 1)
                else:
                    return None
        else:
            # 旧格式: ss://base64(method:password@host:port)
            decoded = safe_b64decode(body)
            if not decoded or "@" not in decoded:
                return None
            userinfo, hostport = decoded.rsplit("@", 1)
            method, password = userinfo.split(":", 1)

        if ":" in hostport:
            host, port = hostport.rsplit(":", 1)
        else:
            host, port = hostport, "8388"

        return {
            "tag": name or f"ss-{host}",
            "type": "shadowsocks",
            "server": host,
            "server_port": int(port),
            "method": method,
            "password": password,
        }
    except Exception:
        return None


def parse_vmess(uri):
    """解析 vmess:// 链接 → sing-box vmess outbound"""
    try:
        if not uri.startswith("vmess://"):
            return None
        decoded = safe_b64decode(uri[8:])
        if not decoded:
            return None
        cfg = json.loads(decoded)

        network = cfg.get("net", "tcp")
        tls_cfg = {}
        if cfg.get("tls") == "tls":
            tls_cfg = {"enabled": True}
            sni = cfg.get("sni") or cfg.get("host", "")
            if sni:
                tls_cfg["server_name"] = sni
            if cfg.get("verifycert") in ("false", False, 0):
                tls_cfg["insecure"] = True
        elif cfg.get("tls") == "reality":
            tls_cfg = {"enabled": True, "reality": {"enabled": True}}

        transport = {}
        if network == "ws":
            transport = {
                "type": "ws",
                "path": cfg.get("path", "/"),
            }
            if cfg.get("host"):
                transport["headers"] = {"Host": cfg["host"]}
        elif network == "grpc":
            transport = {"type": "grpc", "service_name": cfg.get("path", "")}
        elif network == "http" or network == "httpupgrade":
            transport = {
                "type": network,
                "path": cfg.get("path", "/"),
            }
            if cfg.get("host"):
                transport["host"] = cfg["host"]
        elif network == "quic":
            transport = {"type": "quic"}

        outbound = {
            "tag": cfg.get("ps", "") or f"vmess-{cfg.get('add','')}",
            "type": "vmess",
            "server": cfg.get("add", ""),
            "server_port": int(cfg.get("port", 443)),
            "uuid": cfg.get("id", ""),
            "security": "auto",
            "alter_id": int(cfg.get("aid", 0)),
        }
        if transport:
            outbound["transport"] = transport
        if tls_cfg:
            outbound["tls"] = tls_cfg
        return outbound
    except Exception:
        return None


def parse_vless(uri):
    """解析 vless:// 链接 → sing-box vless outbound"""
    try:
        if not uri.startswith("vless://"):
            return None
        parsed = urllib.parse.urlparse(uri)
        qs = urllib.parse.parse_qs(parsed.query)

        name = urllib.parse.unquote(parsed.fragment) if parsed.fragment else ""
        server = parsed.hostname
        port = parsed.port or 443
        uuid = parsed.username

        tls_cfg = {}
        security = qs.get("security", [""])[0]
        if security == "tls" or security == "reality":
            tls_cfg = {"enabled": True}
            sni = qs.get("sni", [""])[0]
            if sni:
                tls_cfg["server_name"] = sni
            fp = qs.get("fp", [""])[0]
            if fp:
                tls_cfg["utls"] = {"enabled": True, "fingerprint": fp}
            if security == "reality":
                pub = qs.get("pbk", [""])[0]
                sid = qs.get("sid", [""])[0]
                tls_cfg["reality"] = {
                    "enabled": True,
                    "public_key": pub,
                    "short_id": sid,
                }
            alpn = qs.get("alpn", [""])[0]
            if alpn:
                tls_cfg["alpn"] = alpn.split(",")
            if qs.get("allowInsecure", [""])[0] == "1":
                tls_cfg["insecure"] = True

        transport = {}
        net_type = qs.get("type", ["tcp"])[0]
        if net_type == "ws":
            transport = {"type": "ws", "path": qs.get("path", ["/"])[0]}
            host = qs.get("host", [""])[0]
            if host:
                transport["headers"] = {"Host": host}
        elif net_type == "grpc":
            transport = {"type": "grpc", "service_name": qs.get("serviceName", [""])[0]}
        elif net_type in ("http", "httpupgrade", "xhttp"):
            transport = {"type": net_type, "path": qs.get("path", ["/"])[0]}
            host = qs.get("host", [""])[0]
            if host:
                transport["host"] = host

        flow = qs.get("flow", [""])[0]

        outbound = {
            "tag": name or f"vless-{server}",
            "type": "vless",
            "server": server,
            "server_port": port,
            "uuid": uuid,
        }
        if flow:
            outbound["flow"] = flow
        if transport:
            outbound["transport"] = transport
        if tls_cfg:
            outbound["tls"] = tls_cfg
        return outbound
    except Exception:
        return None


def parse_trojan(uri):
    """解析 trojan:// 链接 → sing-box trojan outbound"""
    try:
        if not uri.startswith("trojan://"):
            return None
        parsed = urllib.parse.urlparse(uri)
        qs = urllib.parse.parse_qs(parsed.query)
        name = urllib.parse.unquote(parsed.fragment) if parsed.fragment else ""

        tls_cfg = {"enabled": True}
        sni = qs.get("sni", [""])[0] or parsed.hostname
        if sni:
            tls_cfg["server_name"] = sni
        if qs.get("allowInsecure", [""])[0] == "1":
            tls_cfg["insecure"] = True
        alpn = qs.get("alpn", [""])[0]
        if alpn:
            tls_cfg["alpn"] = alpn.split(",")

        transport = {}
        net_type = qs.get("type", ["tcp"])[0]
        if net_type == "ws":
            transport = {"type": "ws", "path": qs.get("path", ["/"])[0]}
            host = qs.get("host", [""])[0]
            if host:
                transport["headers"] = {"Host": host}
        elif net_type == "grpc":
            transport = {"type": "grpc", "service_name": qs.get("serviceName", [""])[0]}

        outbound = {
            "tag": name or f"trojan-{parsed.hostname}",
            "type": "trojan",
            "server": parsed.hostname,
            "server_port": parsed.port or 443,
            "password": parsed.username,
            "tls": tls_cfg,
        }
        if transport:
            outbound["transport"] = transport
        return outbound
    except Exception:
        return None


def parse_hysteria2(uri):
    """解析 hysteria2:// 或 hy2:// 链接 → sing-box hysteria2 outbound"""
    try:
        if uri.startswith("hysteria2://"):
            body = uri[12:]
        elif uri.startswith("hy2://"):
            body = uri[6:]
        else:
            return None

        name = ""
        if "#" in body:
            body, name_part = body.split("#", 1)
            name = urllib.parse.unquote(name_part)

        # password@host:port?params
        if "@" in body:
            password, hostport_q = body.rsplit("@", 1)
        else:
            return None

        hostport, _, query = hostport_q.partition("?")
        qs = urllib.parse.parse_qs(query)
        if ":" in hostport:
            host, port = hostport.rsplit(":", 1)
        else:
            host, port = hostport, "443"

        tls_cfg = {"enabled": True}
        sni = qs.get("sni", [""])[0]
        if sni:
            tls_cfg["server_name"] = sni
        if qs.get("insecure", ["0"])[0] == "1":
            tls_cfg["insecure"] = True
        alpn = qs.get("alpn", [""])[0]
        if alpn:
            tls_cfg["alpn"] = alpn.split(",")

        outbound = {
            "tag": name or f"hy2-{host}",
            "type": "hysteria2",
            "server": host,
            "server_port": int(port),
            "password": urllib.parse.unquote(password),
            "tls": tls_cfg,
        }
        up = qs.get("up", [""])[0]
        down = qs.get("down", [""])[0]
        if up:
            outbound["up_mbps"] = int(up) if up.isdigit() else up
        if down:
            outbound["down_mbps"] = int(down) if down.isdigit() else down
        obfs = qs.get("obfs", [""])[0]
        if obfs:
            outbound["obfs"] = {"type": obfs, "password": qs.get("obfs-password", [""])[0]}
        return outbound
    except Exception:
        return None


def parse_tuic(uri):
    """解析 tuic:// 链接 → sing-box tuic outbound"""
    try:
        if not uri.startswith("tuic://"):
            return None
        parsed = urllib.parse.urlparse(uri)
        qs = urllib.parse.parse_qs(parsed.query)
        name = urllib.parse.unquote(parsed.fragment) if parsed.fragment else ""

        tls_cfg = {"enabled": True}
        sni = qs.get("sni", [""])[0] or parsed.hostname
        if sni:
            tls_cfg["server_name"] = sni
        alpn = qs.get("alpn", [""])[0]
        if alpn:
            tls_cfg["alpn"] = alpn.split(",")
        if qs.get("allowInsecure", [""])[0] == "1":
            tls_cfg["insecure"] = True

        outbound = {
            "tag": name or f"tuic-{parsed.hostname}",
            "type": "tuic",
            "server": parsed.hostname,
            "server_port": parsed.port or 443,
            "uuid": parsed.username,
            "password": parsed.password or "",
            "congestion_control": qs.get("congestion_control", ["bbr"])[0],
            "udp_relay_mode": qs.get("udp_relay_mode", ["native"])[0],
            "tls": tls_cfg,
        }
        return outbound
    except Exception:
        return None


def _clash_build_tls(p):
    """从 Clash proxy 字典构建 sing-box TLS 配置 (vmess/vless/trojan/hy2/tuic 通用)"""
    tls = {}
    ptype = p.get("type", "").lower()
    # 是否启用 TLS
    if p.get("tls") or p.get("reality-opts"):
        tls["enabled"] = True
    elif ptype == "trojan":
        tls["enabled"] = True  # trojan 默认 TLS
    if not tls.get("enabled"):
        return {}
    # SNI
    sni = p.get("servername") or p.get("sni") or p.get("host") or p.get("server", "")
    if sni:
        tls["server_name"] = sni
    if p.get("skip-cert-verify"):
        tls["insecure"] = True
    if p.get("disable-sni"):
        tls["insecure"] = True
    # ALPN
    alpn = p.get("alpn")
    if alpn:
        tls["alpn"] = alpn if isinstance(alpn, list) else [alpn]
    # uTLS 指纹
    fp = p.get("client-fingerprint") or p.get("fingerprint")
    if fp:
        tls["utls"] = {"enabled": True, "fingerprint": fp}
    # REALITY
    reality = p.get("reality-opts") or {}
    if reality and (reality.get("public-key") or reality.get("short-id")):
        tls["reality"] = {"enabled": True}
        if reality.get("public-key"):
            tls["reality"]["public_key"] = reality["public-key"]
        if reality.get("short-id"):
            tls["reality"]["short_id"] = reality["short-id"]
    return tls


def _clash_build_transport(p):
    """从 Clash proxy 字典构建 sing-box transport 配置"""
    net = (p.get("network") or "tcp").lower()
    if net == "tcp":
        return {}
    if net == "ws":
        ws = p.get("ws-opts") or {}
        t = {"type": "ws", "path": ws.get("path", "/")}
        headers = ws.get("headers") or {}
        if headers:
            t["headers"] = {str(k): str(v) for k, v in headers.items()}
        if ws.get("max-early-data") is not None:
            t["max_early_data"] = ws["max-early-data"]
        if ws.get("early-data-header-name"):
            t["early_data_header_name"] = ws["early-data-header-name"]
        return t
    if net == "grpc":
        grpc = p.get("grpc-opts") or {}
        return {"type": "grpc", "service_name": grpc.get("grpc-service-name", "")}
    if net == "h2":
        h2 = p.get("h2-opts") or {}
        t = {"type": "http", "path": h2.get("path", "/")}
        host = h2.get("host")
        if host:
            t["host"] = host if isinstance(host, list) else [host]
        return t
    if net == "http":
        ho = p.get("http-opts") or {}
        path = ho.get("path")
        if isinstance(path, list):
            path = path[0] if path else "/"
        t = {"type": "http", "path": path or "/"}
        headers = ho.get("headers") or {}
        if headers:
            host = list(headers.keys())[0] if headers else ""
            if host:
                t["host"] = [host]
        return t
    if net == "httpupgrade":
        ws = p.get("ws-opts") or p.get("httpupgrade-opts") or {}
        return {"type": "httpupgrade", "path": ws.get("path", "/")}
    if net == "quic":
        return {"type": "quic"}
    if net == "xhttp":
        xo = p.get("xhttp-opts") or p.get("ws-opts") or {}
        return {"type": "xhttp", "path": xo.get("path", "/")}
    return {}


def parse_clash_yaml(text):
    """解析 Clash YAML 订阅 → sing-box outbounds 列表 (全协议全传输)

    支持: ss / vmess / vless / trojan / hysteria2 / tuic / wireguard / socks5 / http
    传输: tcp / ws / grpc / h2 / http / httpupgrade / quic / xhttp
    TLS : tls / reality-opts / client-fingerprint / alpn / skip-cert-verify
    """
    if not yaml:
        return []
    try:
        data = yaml.safe_load(text)
    except Exception:
        return []
    proxies = data.get("proxies") or []
    outbounds = []
    for p in proxies:
        try:
            ptype = (p.get("type") or "").lower()
            tag = p.get("name") or p.get("server") or "node"
            server = p.get("server", "")
            port = int(p.get("port", 443))
            ob = None
            if ptype == "ss":
                ob = {"tag": tag, "type": "shadowsocks", "server": server,
                      "server_port": port, "method": p.get("cipher", ""),
                      "password": p.get("password", "")}
                plugin = p.get("plugin")
                if plugin:
                    ob["plugin"] = plugin
                    popts = p.get("plugin-opts") or {}
                    if popts:
                        ob["plugin_opts"] = popts
            elif ptype == "vmess":
                ob = {"tag": tag, "type": "vmess", "server": server,
                      "server_port": port, "uuid": p.get("uuid", ""),
                      "security": p.get("cipher", "auto"),
                      "alter_id": int(p.get("alterId", 0))}
                if p.get("global-padding"):
                    ob["global_padding"] = True
                if p.get("authenticated-length"):
                    ob["authenticated_length"] = True
                pe = p.get("packet-encoding")
                if pe == "xudp":
                    ob["packet_encoding"] = "xudp"
                elif pe == "packetaddr":
                    ob["packet_encoding"] = "packetaddr"
            elif ptype == "vless":
                ob = {"tag": tag, "type": "vless", "server": server,
                      "server_port": port, "uuid": p.get("uuid", "")}
                if p.get("flow"):
                    ob["flow"] = p["flow"]
                pe = p.get("packet-encoding")
                if pe == "xudp":
                    ob["packet_encoding"] = "xudp"
                elif pe == "packetaddr":
                    ob["packet_encoding"] = "packetaddr"
            elif ptype == "trojan":
                ob = {"tag": tag, "type": "trojan", "server": server,
                      "server_port": port, "password": p.get("password", "")}
            elif ptype in ("hysteria2", "hy2"):
                ob = {"tag": tag, "type": "hysteria2", "server": server,
                      "server_port": port, "password": p.get("password", "")}
                up = p.get("up")
                down = p.get("down")
                if up:
                    ob["up_mbps"] = _parse_bw(up)
                if down:
                    ob["down_mbps"] = _parse_bw(down)
                if p.get("obfs"):
                    ob["obfs"] = {"type": p["obfs"]}
                    if p.get("obfs-password"):
                        ob["obfs"]["password"] = p["obfs-password"]
            elif ptype == "tuic":
                ob = {"tag": tag, "type": "tuic", "server": server,
                      "server_port": port, "uuid": p.get("uuid", ""),
                      "password": p.get("password", "")}
                ob["congestion_control"] = p.get("congestion-controller", "bbr")
                ob["udp_relay_mode"] = p.get("udp-relay-mode", "native")
                if p.get("reduce-rtt"):
                    ob["reduce_rtt"] = True
            elif ptype == "wireguard":
                ob = {"tag": tag, "type": "wireguard", "server": server,
                      "server_port": port}
                priv = p.get("private-key") or p.get("privateKey", "")
                ob["private_key"] = priv
                pub = p.get("public-key") or p.get("publicKey")
                if pub:
                    ob["peer_public_key"] = pub
                if p.get("ip"):
                    ob["local_addresses"] = [p["ip"]] if isinstance(p["ip"], str) else p["ip"]
                if p.get("mtu"):
                    ob["mtu"] = int(p["mtu"])
                continue  # wireguard 的 tls/transport 不适用, 直接跳过
            elif ptype in ("socks5", "socks"):
                ob = {"tag": tag, "type": "socks", "server": server,
                      "server_port": port}
                if p.get("username"):
                    ob["username"] = p["username"]
                if p.get("password"):
                    ob["password"] = p["password"]
                if p.get("udp") is not None:
                    ob["udp"] = p["udp"]
                outbounds.append(ob)
                continue
            elif ptype == "http":
                ob = {"tag": tag, "type": "http", "server": server,
                      "server_port": port}
                if p.get("username"):
                    ob["username"] = p["username"]
                if p.get("password"):
                    ob["password"] = p["password"]
                if p.get("tls"):
                    ob["tls"] = {"enabled": True}
                outbounds.append(ob)
                continue
            else:
                continue
            # 通用 TLS + transport (除 wireguard/socks/http)
            tls = _clash_build_tls(p)
            if tls:
                ob["tls"] = tls
            transport = _clash_build_transport(p)
            if transport:
                ob["transport"] = transport
            outbounds.append(ob)
        except Exception:
            continue
    return outbounds


def _parse_bw(val):
    """解析带宽值 (Clash: '30 Mbps' / 数字) → Mbps 整数"""
    if isinstance(val, (int, float)):
        return int(val)
    m = re.search(r'(\d+)', str(val))
    return int(m.group(1)) if m else 0


# ============================================================
# 防DNS泄露 + 国内外分流规则构建
# ============================================================

def extract_proxy_domains(outbounds):
    """从节点列表中提取代理服务器域名 (非IP) → 用于防DNS泄露规则

    代理服务器自身的域名必须走直连DNS解析, 否则会形成循环依赖:
    解析代理域名→走代理DNS→代理DNS要通过代理→代理域名还没解析→死循环
    """
    domains = set()
    ip_re = re.compile(r'^(\d{1,3}\.){3}\d{1,3}$')
    for ob in outbounds:
        server = ob.get("server", "")
        if server and not ip_re.match(server):
            domains.add(server)
    return sorted(domains) if domains else None


def build_anti_leak_dns(proxy_domains=None):
    """构建防DNS泄露 DNS 配置 (sing-box 1.12+ 新格式)

    防泄露策略:
    1. 代理服务器域名 → 直连DNS (防止循环依赖)
    2. 广告域名 → REFUSED (DNS层拦截)
    3. clash_mode直连 → 直连DNS
    4. clash_mode全局 → 代理DNS
    5. 国内域名 → 直连DNS (CDN优化, 走国内DNS更快)
    6. 其他所有A/AAAA查询 → fakeip (彻底防泄露: 返回假IP, 真实DNS在代理端解析)
    7. 兜底 → 代理DNS (DoH走代理, ISP看不到DNS查询)
    """
    servers = [
        {
            "type": "https",
            "tag": "proxy-dns",
            "server": "1.1.1.1",
            "detour": "select"
        },
        {
            "type": "udp",
            "tag": "direct-dns",
            "server": "223.5.5.5",
            "detour": "direct"
        },
        {
            "type": "fakeip",
            "tag": "fakeip-dns",
            "inet4_range": "198.18.0.0/15"
        }
    ]

    rules = []

    # 规则1: 代理服务器域名 → 直连DNS (防循环依赖, 必须第一条)
    if proxy_domains:
        rules.append({
            "domain": proxy_domains,
            "action": "route",
            "server": "direct-dns"
        })

    # 规则2: 广告域名 → DNS层拒绝
    rules.append({
        "rule_set": "geosite-category-ads-all",
        "rcode": "REFUSED"
    })

    # 规则3: clash_mode 直连 → 直连DNS
    rules.append({
        "clash_mode": "direct",
        "action": "route",
        "server": "direct-dns"
    })

    # 规则4: clash_mode 全局 → 代理DNS
    rules.append({
        "clash_mode": "global",
        "action": "route",
        "server": "proxy-dns"
    })

    # 规则5: 国内域名 → 直连DNS (CDN优化)
    rules.append({
        "rule_set": "geosite-cn",
        "action": "route",
        "server": "direct-dns"
    })

    # 规则6: 其他所有A/AAAA查询 → fakeip (核心防泄露)
    rules.append({
        "query_type": ["A", "AAAA"],
        "action": "route",
        "server": "fakeip-dns"
    })

    return {
        "servers": servers,
        "rules": rules,
        "final": "proxy-dns",
        "strategy": "ipv4_only",
        "reverse_mapping": True,
        "disable_cache": False,
        "disable_expire": False
    }


def build_geo_route_config():
    """构建国内外分流路由配置 (sing-box 1.12+ 新格式)

    分流策略:
    1. 嗅探协议 → 获取域名 (配合fakeip实现域名路由)
    2. 劫持DNS → 所有DNS查询交给sing-box处理 (fakeip必需)
    3. clash_mode 支持
    4. 广告 → 拒绝 (路由层拦截)
    5. 国外服务 (Google/Telegram/YouTube/Netflix/GitHub) → 代理
    6. 国内域名+IP → 直连
    7. 私有IP → 直连
    8. 兜底 → 代理 (未匹配的全部走代理)
    """
    rules = [
        # 嗅探协议 (从连接中提取域名, 配合fakeip的reverse_mapping)
        {"action": "sniff"},
        # 劫持所有DNS查询 (fakeip工作前提: DNS必须经过sing-box)
        {"protocol": "dns", "action": "hijack-dns"},
        # clash_mode 支持
        {"clash_mode": "direct", "outbound": "direct"},
        {"clash_mode": "global", "outbound": "select"},
        # 广告拦截
        {"rule_set": "geosite-category-ads-all", "action": "reject"},
        # 国外服务 → 强制走代理 (在国内域名规则之前, 确保不被误判为直连)
        {
            "rule_set": [
                "geosite-google",
                "geosite-telegram",
                "geosite-youtube",
                "geosite-netflix",
                "geosite-github"
            ],
            "outbound": "select"
        },
        # 国内域名 + 国内IP → 直连
        {
            "rule_set": ["geosite-cn", "geoip-cn"],
            "outbound": "direct"
        },
        # 私有/本地IP → 直连
        {"ip_is_private": True, "outbound": "direct"}
    ]

    rule_sets = [
        {
            "tag": "geosite-cn",
            "type": "remote",
            "format": "binary",
            "url": "https://raw.githubusercontent.com/SagerNet/sing-geosite/rule-set/geosite-cn.srs",
            "download_detour": "select"
        },
        {
            "tag": "geoip-cn",
            "type": "remote",
            "format": "binary",
            "url": "https://raw.githubusercontent.com/SagerNet/sing-geoip/rule-set/geoip-cn.srs",
            "download_detour": "select"
        },
        {
            "tag": "geosite-category-ads-all",
            "type": "remote",
            "format": "binary",
            "url": "https://raw.githubusercontent.com/SagerNet/sing-geosite/rule-set/geosite-category-ads-all.srs",
            "download_detour": "select"
        },
        {
            "tag": "geosite-google",
            "type": "remote",
            "format": "binary",
            "url": "https://raw.githubusercontent.com/SagerNet/sing-geosite/rule-set/geosite-google.srs",
            "download_detour": "select"
        },
        {
            "tag": "geosite-telegram",
            "type": "remote",
            "format": "binary",
            "url": "https://raw.githubusercontent.com/SagerNet/sing-geosite/rule-set/geosite-telegram.srs",
            "download_detour": "select"
        },
        {
            "tag": "geosite-youtube",
            "type": "remote",
            "format": "binary",
            "url": "https://raw.githubusercontent.com/SagerNet/sing-geosite/rule-set/geosite-youtube.srs",
            "download_detour": "select"
        },
        {
            "tag": "geosite-netflix",
            "type": "remote",
            "format": "binary",
            "url": "https://raw.githubusercontent.com/SagerNet/sing-geosite/rule-set/geosite-netflix.srs",
            "download_detour": "select"
        },
        {
            "tag": "geosite-github",
            "type": "remote",
            "format": "binary",
            "url": "https://raw.githubusercontent.com/SagerNet/sing-geosite/rule-set/geosite-github.srs",
            "download_detour": "select"
        }
    ]

    return {
        "rules": rules,
        "rule_set": rule_sets,
        "auto_detect_interface": True,
        "final": "select",
        "default_domain_resolver": {
            "server": "direct-dns"
        }
    }


# ============================================================
# Clash proxy-groups → sing-box selector/urltest 转换
# ============================================================

def parse_clash_proxy_groups(clash_groups, node_tags):
    """转换 Clash proxy-groups → sing-box selector/urltest outbounds

    支持: select / url-test / fallback / load-balance
    映射: DIRECT → direct, REJECT → 跳过(由路由规则处理)
    解析: 代理组间引用 (如"节点选择"引用"自动选择")
    """
    group_tags = [str(g.get("name", "")) for g in clash_groups if g.get("name")]
    sg_groups = []

    for g in clash_groups:
        gname = str(g.get("name", ""))
        if not gname:
            continue
        gtype = (g.get("type") or "").lower()
        proxies = g.get("proxies", [])

        # 映射 Clash 代理名称 → sing-box tag
        outbound_tags = []
        for p in proxies:
            p = str(p)
            if p == "DIRECT":
                outbound_tags.append("direct")
            elif p == "REJECT":
                continue  # 跳过, 由路由规则 action:reject 处理
            elif p in node_tags or p in group_tags:
                outbound_tags.append(p)
            # else: 未知代理, 跳过

        if gtype == "select":
            sg_groups.append({
                "tag": gname,
                "type": "selector",
                "outbounds": outbound_tags if outbound_tags else ["direct"],
                "default": outbound_tags[0] if outbound_tags else "direct"
            })
        elif gtype in ("url-test", "fallback", "load-balance"):
            url = g.get("url", "https://www.gstatic.com/generate_204")
            interval = g.get("interval", 300)
            if isinstance(interval, (int, float)):
                interval = f"{int(interval)}s"
            entry = {
                "tag": gname,
                "type": "urltest",
                "outbounds": [t for t in outbound_tags if t != "direct"] or ["direct"],
                "url": url,
                "interval": str(interval),
                "tolerance": int(g.get("tolerance", 50))
            }
            sg_groups.append(entry)

    return sg_groups


def parse_clash_yaml_full(text):
    """完整解析 Clash YAML → {outbounds, proxy_groups, rules}

    相比 parse_clash_yaml (仅返回 outbounds), 此函数额外解析:
    - proxy-groups → sing-box selector/urltest
    - rules → Clash 路由规则 (供参考)
    """
    if not yaml:
        return {"outbounds": [], "proxy_groups": [], "rules": []}
    try:
        data = yaml.safe_load(text)
    except Exception:
        return {"outbounds": [], "proxy_groups": [], "rules": []}

    outbounds = parse_clash_yaml(text)
    node_tags = [n["tag"] for n in outbounds]

    clash_groups = data.get("proxy-groups") or []
    proxy_groups = parse_clash_proxy_groups(clash_groups, node_tags)

    clash_rules = data.get("rules") or []

    return {
        "outbounds": outbounds,
        "proxy_groups": proxy_groups,
        "rules": clash_rules,
    }


def merge_nodes_with_groups(config, nodes, proxy_groups):
    """合并节点 + Clash代理组到配置中

    输出结构: select → 代理组(selector/urltest) → 节点 → direct
    select 引用第一个 selector 代理组, 兜底 direct
    """
    group_tags = [g["tag"] for g in proxy_groups]

    # 第一个 selector 类型的代理组作为 select 默认值
    first_selector = next(
        (g["tag"] for g in proxy_groups if g.get("type") == "selector"),
        group_tags[0] if group_tags else "direct"
    )

    # 构建 select 出站
    select_outbound = {
        "tag": "select",
        "type": "selector",
        "outbounds": group_tags + ["direct"] if group_tags else ["direct"],
        "default": first_selector
    }

    # 保留模板的 direct
    direct_outbounds = [
        o for o in config.get("outbounds", [])
        if o.get("tag") == "direct" and o.get("type") == "direct"
    ]
    if not direct_outbounds:
        direct_outbounds = [{"tag": "direct", "type": "direct"}]

    # 组装: select → 代理组 → 节点 → direct
    config["outbounds"] = [select_outbound] + proxy_groups + nodes + direct_outbounds

    return config


def parse_subscription(text):
    """
    自动识别订阅格式并解析为 sing-box outbounds 列表
    支持: base64 V2Ray 链接, Clash YAML, sing-box JSON
    """
    text = text.strip()
    if not text:
        return []

    # 1. 尝试解析为 sing-box JSON (含 outbounds 字段)
    try:
        data = json.loads(text)
        if isinstance(data, dict) and "outbounds" in data:
            return [o for o in data["outbounds"] if o.get("type") not in ("direct", "block", "dns", "selector", "urltest")]
    except (json.JSONDecodeError, ValueError):
        pass

    # 2. 尝试解析为 Clash YAML
    if yaml and ("proxies:" in text or "proxy-groups:" in text):
        nodes = parse_clash_yaml(text)
        if nodes:
            return nodes

    # 3. 尝试 base64 解码后按行解析 V2Ray 链接
    decoded = safe_b64decode(text)
    if decoded and ("://" in decoded):
        text = decoded

    # 4. 按行解析各类链接
    outbounds = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        node = None
        if line.startswith("ss://"):
            node = parse_ss(line)
        elif line.startswith("vmess://"):
            node = parse_vmess(line)
        elif line.startswith("vless://"):
            node = parse_vless(line)
        elif line.startswith("trojan://"):
            node = parse_trojan(line)
        elif line.startswith("hysteria2://") or line.startswith("hy2://"):
            node = parse_hysteria2(line)
        elif line.startswith("tuic://"):
            node = parse_tuic(line)
        if node:
            outbounds.append(node)

    return outbounds


def fetch_subscription(url, timeout=30):
    """获取订阅内容"""
    headers = {"User-Agent": "sing-box/1.0"}
    r = requests.get(url, timeout=timeout, headers=headers)
    r.raise_for_status()
    return r.text


def load_subscriptions():
    if os.path.exists(SUB_FILE):
        try:
            with open(SUB_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return []


def save_subscriptions(subs):
    with open(SUB_FILE, "w") as f:
        json.dump(subs, f, indent=2, ensure_ascii=False)


def load_config():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def save_config(cfg):
    # 先格式化校验
    tmp = CONFIG_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)
    os.replace(tmp, CONFIG_FILE)


def merge_nodes_into_config(config, nodes):
    """
    将节点列表合并进 config.json:
    - 替换 selector / urltest 的 outbounds 为节点 tag + direct
    - 在 outbounds 中插入节点 (在 direct 之前)
    """
    node_tags = [n["tag"] for n in nodes]

    # 去重: 移除已有的节点类型 (非 direct/block/dns/selector/urltest)
    existing_types = {"direct", "block", "dns", "selector", "urltest"}
    config["outbounds"] = [o for o in config.get("outbounds", [])
                           if o.get("type") in existing_types]

    # 找到 direct 在 outbounds 中的位置, 在它前面插入节点
    outbounds = config["outbounds"]
    insert_idx = len(outbounds)
    for i, o in enumerate(outbounds):
        if o.get("tag") == "direct":
            insert_idx = i
            break

    for n in reversed(nodes):
        outbounds.insert(insert_idx, n)

    # 更新 selector 和 urltest 引用
    for o in outbounds:
        if o.get("type") == "selector" and o.get("tag") in ("select", "selector", "🚀 节点选择"):
            o["outbounds"] = node_tags + [t for t in o["outbounds"] if t in ("direct", "🎯 全球直连")]
            o["default"] = node_tags[0] if node_tags else "direct"
        elif o.get("type") == "urltest" and o.get("tag") in ("auto", "♻️ 自动选择"):
            o["outbounds"] = node_tags

    return config


def validate_config(config_path):
    """用 sing-box check 校验配置"""
    code, out, err = run_cmd(f'"{SINGBOX_BIN}" check -c "{config_path}"')
    if code == 0:
        return True, "配置校验通过"
    return False, err or out or "配置校验失败"


def build_full_config_from_nodes(nodes, template_name="tproxy", clash_full=None):
    """从节点列表 + 模板构建完整 sing-box 配置 (含防DNS泄露 + 国内外分流)

    自动注入:
    1. 防DNS泄露 DNS 配置 (fakeip + 国内外分流DNS + 代理服务器域名防循环)
    2. 国内外分流路由规则 (geosite-cn/geoip-cn 直连, 国外服务走代理)
    3. 如有 Clash proxy-groups, 转换为 sing-box selector/urltest
    """
    template_path = os.path.join(TEMPLATE_DIR, f"config-{template_name}.json")
    if not os.path.exists(template_path):
        template_path = os.path.join(TEMPLATE_DIR, "config-tproxy.json")
    if not os.path.exists(template_path):
        # 兜底: 使用当前配置
        config = load_config()
    else:
        with open(template_path) as f:
            config = json.load(f)
    # 清理注释字段
    config = {k: v for k, v in config.items() if not k.startswith("_")}

    # 节点去重 (按 tag)
    seen = set()
    unique_nodes = []
    for n in nodes:
        tag = n.get("tag", "")
        if tag and tag not in seen:
            seen.add(tag)
            unique_nodes.append(n)
    nodes = unique_nodes

    # 提取代理服务器域名 (防DNS泄露: 代理服务器域名必须走直连DNS)
    proxy_domains = extract_proxy_domains(nodes)

    # 注入防DNS泄露 DNS 配置 (覆盖模板的 DNS)
    config["dns"] = build_anti_leak_dns(proxy_domains)

    # 注入国内外分流路由规则 (覆盖模板的 route)
    config["route"] = build_geo_route_config()

    # 合并节点 + 代理组
    if clash_full and clash_full.get("proxy_groups"):
        config = merge_nodes_with_groups(config, nodes, clash_full["proxy_groups"])
    else:
        config = merge_nodes_into_config(config, nodes)

    return config


# ============================================================
# API 路由
# ============================================================

@app.route("/")
def index():
    return render_template_string(DASHBOARD_HTML)


@app.route("/api/status")
def api_status():
    running = get_service_status()
    return jsonify({
        "running": running,
        "version": get_singbox_version(),
        "latest": get_latest_version(),
        "uptime": get_uptime() if running else "—",
        "clash": get_clash_info() if running else None,
        "network": get_network_info(),
    })


@app.route("/api/config", methods=["GET"])
def api_get_config():
    cfg = load_config()
    # 去掉注释字段
    clean = {k: v for k, v in cfg.items() if not k.startswith("_")}
    return jsonify(clean)


@app.route("/api/config", methods=["POST"])
def api_set_config():
    data = request.get_json()
    if not data:
        return jsonify({"error": "no data"}), 400
    # 校验
    tmp = CONFIG_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    ok, msg = validate_config(tmp)
    if not ok:
        os.unlink(tmp)
        return jsonify({"error": msg}), 400
    os.replace(tmp, CONFIG_FILE)
    return jsonify({"ok": True, "message": "配置已保存"})


@app.route("/api/check", methods=["POST"])
def api_check_config():
    ok, msg = validate_config(CONFIG_FILE)
    return jsonify({"ok": ok, "message": msg})


@app.route("/api/start", methods=["POST"])
def api_start():
    code, out, err = run_cmd("rc-service sing-box start")
    return jsonify({"ok": code == 0, "message": out + err})


@app.route("/api/stop", methods=["POST"])
def api_stop():
    code, out, err = run_cmd("rc-service sing-box stop")
    return jsonify({"ok": code == 0, "message": out + err})


@app.route("/api/restart", methods=["POST"])
def api_restart():
    code, out, err = run_cmd("rc-service sing-box restart")
    return jsonify({"ok": code == 0, "message": out + err})


@app.route("/api/subscriptions", methods=["GET"])
def api_list_subs():
    return jsonify(load_subscriptions())


@app.route("/api/subscriptions", methods=["POST"])
def api_add_sub():
    data = request.get_json()
    name = data.get("name", "").strip()
    url = data.get("url", "").strip()
    if not name or not url:
        return jsonify({"error": "name and url required"}), 400
    subs = load_subscriptions()
    subs.append({"name": name, "url": url, "added_at": datetime.now().isoformat()})
    save_subscriptions(subs)
    return jsonify({"ok": True, "subscriptions": subs})


@app.route("/api/subscriptions/<name>", methods=["DELETE"])
def api_del_sub(name):
    subs = load_subscriptions()
    subs = [s for s in subs if s["name"] != name]
    save_subscriptions(subs)
    return jsonify({"ok": True, "subscriptions": subs})


@app.route("/api/subscriptions/update", methods=["POST"])
def api_update_subs():
    """拉取所有订阅, 合并节点, 更新 config.json (含防DNS泄露+国内外分流), 重启 sing-box"""
    subs = load_subscriptions()
    if not subs:
        return jsonify({"error": "没有订阅, 请先添加"}), 400
    all_nodes = []
    all_groups = []
    errors = []
    for s in subs:
        try:
            text = fetch_subscription(s["url"])
            if yaml and ("proxies:" in text or "proxy-groups:" in text):
                clash_full = parse_clash_yaml_full(text)
                all_nodes.extend(clash_full["outbounds"])
                # 只取第一个 Clash 订阅的代理组 (避免多订阅组冲突)
                if not all_groups and clash_full.get("proxy_groups"):
                    all_groups = clash_full["proxy_groups"]
            else:
                nodes = parse_subscription(text)
                all_nodes.extend(nodes)
        except Exception as e:
            errors.append(f"{s['name']}: {e}")
    if not all_nodes:
        return jsonify({"error": "未解析到任何节点", "details": errors}), 400
    # 构建: 防泄露DNS + 国内外分流 + 节点 + 代理组
    cfg = get_panel_config()
    clash_full = {"proxy_groups": all_groups} if all_groups else None
    config = build_full_config_from_nodes(
        all_nodes, cfg.get("gateway_mode", "tproxy"), clash_full
    )
    # 校验
    tmp = CONFIG_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)
    ok, msg = validate_config(tmp)
    if not ok:
        os.unlink(tmp)
        return jsonify({"error": f"合并后配置校验失败: {msg}"}), 500
    os.replace(tmp, CONFIG_FILE)
    # 重启
    run_cmd("rc-service sing-box restart")
    return jsonify({
        "ok": True,
        "node_count": len(all_nodes),
        "group_count": len(all_groups),
        "errors": errors,
        "message": f"成功更新 {len(all_nodes)} 个节点"
                   + (f", {len(all_groups)} 个代理组" if all_groups else "")
                   + ", sing-box 已重启",
    })


@app.route("/api/convert", methods=["POST"])
def api_convert():
    """转换订阅链接 → sing-box JSON 配置 (预览)

    自动检测格式: Clash YAML → 全量解析(含代理组) / V2Ray Base64 / sing-box JSON
    自动注入: 防DNS泄露(fakeip) + 国内外分流(geosite/geoip)
    """
    data = request.get_json()
    url = data.get("url", "").strip()
    template = data.get("template", "tproxy")
    if not url:
        return jsonify({"error": "url required"}), 400
    try:
        text = fetch_subscription(url)
        # 检测 Clash YAML → 全量解析 (含 proxy-groups)
        clash_full = None
        if yaml and ("proxies:" in text or "proxy-groups:" in text):
            clash_full = parse_clash_yaml_full(text)
            nodes = clash_full["outbounds"]
        else:
            nodes = parse_subscription(text)
        if not nodes:
            return jsonify({"error": "未能从订阅中解析到节点"}), 400
        full_config = build_full_config_from_nodes(nodes, template, clash_full)
        result = {
            "ok": True,
            "node_count": len(nodes),
            "nodes": [n["tag"] for n in nodes],
            "config": full_config,
        }
        if clash_full and clash_full.get("proxy_groups"):
            result["proxy_groups"] = [g["tag"] for g in clash_full["proxy_groups"]]
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/convert/download", methods=["POST"])
def api_convert_download():
    """转换并下载为 JSON 文件 (含防DNS泄露 + 国内外分流)"""
    data = request.get_json()
    url = data.get("url", "").strip()
    template = data.get("template", "tproxy")
    if not url:
        return jsonify({"error": "url required"}), 400
    try:
        text = fetch_subscription(url)
        clash_full = None
        if yaml and ("proxies:" in text or "proxy-groups:" in text):
            clash_full = parse_clash_yaml_full(text)
            nodes = clash_full["outbounds"]
        else:
            nodes = parse_subscription(text)
        if not nodes:
            return jsonify({"error": "未能从订阅中解析到节点"}), 400
        config = build_full_config_from_nodes(nodes, template, clash_full)
        content = json.dumps(config, indent=2, ensure_ascii=False)
        return Response(
            content,
            mimetype="application/json",
            headers={"Content-Disposition": "attachment; filename=sing-box-config.json"},
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/convert/apply", methods=["POST"])
def api_convert_apply():
    """转换订阅并直接应用: 下载→解析→合并进模板(防DNS泄露+国内外分流)
    →sing-box check 校验→写入 config.json→重启 sing-box. 一键本地生效."""
    data = request.get_json() or {}
    url = data.get("url", "").strip()
    template = data.get("template", "tproxy")
    if not url:
        return jsonify({"error": "url required"}), 400
    try:
        text = fetch_subscription(url)
    except Exception as e:
        return jsonify({"error": f"订阅获取失败: {e}"}), 500
    # 检测 Clash YAML → 全量解析 (含 proxy-groups)
    clash_full = None
    if yaml and ("proxies:" in text or "proxy-groups:" in text):
        clash_full = parse_clash_yaml_full(text)
        nodes = clash_full["outbounds"]
    else:
        nodes = parse_subscription(text)
    if not nodes:
        return jsonify({"error": "未能从订阅中解析到节点"}), 400
    # 备份当前配置
    if os.path.exists(CONFIG_FILE):
        shutil.copy2(CONFIG_FILE, CONFIG_FILE + ".bak")
    # 构建完整配置 (模板 + 防泄露DNS + 国内外分流 + 节点)
    try:
        config = build_full_config_from_nodes(nodes, template, clash_full)
    except Exception as e:
        return jsonify({"error": f"配置构建失败: {e}"}), 500
    # sing-box check 校验
    tmp = CONFIG_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)
    ok, msg = validate_config(tmp)
    if not ok:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return jsonify({"error": f"配置校验失败: {msg}", "node_count": len(nodes)}), 500
    # 写入 config.json
    os.replace(tmp, CONFIG_FILE)
    # 重启 sing-box
    rc, out, err = run_cmd("rc-service sing-box restart")
    restart_ok = rc == 0
    result = {
        "ok": True,
        "node_count": len(nodes),
        "nodes": [n["tag"] for n in nodes],
        "template": template,
        "restarted": restart_ok,
        "message": f"成功转换 {len(nodes)} 个节点并应用为 {template} 模板, "
                   + ("sing-box 已重启" if restart_ok else "配置已写入但重启失败, 请手动重启"),
    }
    if clash_full and clash_full.get("proxy_groups"):
        result["proxy_groups"] = [g["tag"] for g in clash_full["proxy_groups"]]
    return jsonify(result)


@app.route("/api/core/version")
def api_core_version():
    return jsonify({"installed": get_singbox_version(), "latest": get_latest_version()})


@app.route("/api/core/update", methods=["POST"])
def api_core_update():
    """下载并安装最新 sing-box 核心"""
    arch = run_cmd("uname -m")[1]
    arch_map = {
        "x86_64": "linux-amd64",
        "aarch64": "linux-arm64",
        "armv7l": "linux-armv7",
    }
    goarch = arch_map.get(arch, "linux-amd64")
    latest = get_latest_version()
    if latest == "unknown":
        return jsonify({"error": "无法获取最新版本"}), 500
    ver = latest.lstrip("v")
    url = f"https://github.com/SagerNet/sing-box/releases/download/{latest}/sing-box-{ver}-{goarch}.tar.gz"
    tmp = f"/tmp/sing-box-{ver}.tar.gz"
    code, out, err = run_cmd(f'wget -q -O "{tmp}" "{url}"', timeout=120)
    if code != 0:
        return jsonify({"error": f"下载失败: {err}"}), 500
    extract_dir = f"/tmp/sing-box-{ver}"
    run_cmd(f'rm -rf "{extract_dir}" && mkdir -p "{extract_dir}"')
    code, out, err = run_cmd(f'tar xzf "{tmp}" -C "{extract_dir}"')
    if code != 0:
        return jsonify({"error": f"解压失败: {err}"}), 500
    bin_path = f"{extract_dir}/sing-box-{ver}-{goarch}/sing-box"
    if not os.path.exists(bin_path):
        # 尝试直接找
        code2, found, _ = run_cmd(f'find "{extract_dir}" -name sing-box -type f')
        if found:
            bin_path = found.splitlines()[0]
    # 停止服务 → 替换 → 启动
    run_cmd("rc-service sing-box stop")
    shutil.copy2(bin_path, SINGBOX_BIN)
    os.chmod(SINGBOX_BIN, 0o755)
    run_cmd("rc-service sing-box start")
    run_cmd(f"rm -rf '{tmp}' '{extract_dir}'")
    return jsonify({"ok": True, "version": get_singbox_version(), "message": "核心升级完成"})


@app.route("/api/network")
def api_network():
    return jsonify(get_network_info())


@app.route("/api/templates")
def api_templates():
    templates = []
    if os.path.isdir(TEMPLATE_DIR):
        for fn in sorted(os.listdir(TEMPLATE_DIR)):
            if fn.endswith(".json"):
                templates.append(fn)
    return jsonify(templates)


@app.route("/api/template/apply", methods=["POST"])
def api_apply_template():
    data = request.get_json()
    template = data.get("template", "")
    tp = os.path.join(TEMPLATE_DIR, template)
    if not os.path.exists(tp):
        return jsonify({"error": "模板不存在"}), 400
    with open(tp) as f:
        tmpl = json.load(f)
    tmpl = {k: v for k, v in tmpl.items() if not k.startswith("_")}
    # 保留当前节点
    config = load_config()
    existing_nodes = [o for o in config.get("outbounds", [])
                      if o.get("type") not in ("direct", "block", "dns", "selector", "urltest")]
    # 注入防DNS泄露 + 国内外分流
    proxy_domains = extract_proxy_domains(existing_nodes) if existing_nodes else None
    tmpl["dns"] = build_anti_leak_dns(proxy_domains)
    tmpl["route"] = build_geo_route_config()
    if existing_nodes:
        merge_nodes_into_config(tmpl, existing_nodes)
    # 校验
    tmp = CONFIG_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(tmpl, f, indent=2, ensure_ascii=False)
    ok, msg = validate_config(tmp)
    if not ok:
        os.unlink(tmp)
        return jsonify({"error": f"模板校验失败: {msg}"}), 400
    os.replace(tmp, CONFIG_FILE)
    run_cmd("rc-service sing-box restart")
    return jsonify({"ok": True, "message": f"已应用模板 {template} (含防DNS泄露+国内外分流)"})


@app.route("/api/logs")
def api_logs():
    lines = request.args.get("lines", 50, type=int)
    return jsonify({"logs": get_recent_logs(lines)})


@app.route("/api/panel-config", methods=["GET"])
def api_panel_config():
    return jsonify(get_panel_config())


@app.route("/api/panel-config", methods=["POST"])
def api_set_panel_config():
    data = request.get_json()
    cfg = get_panel_config()
    cfg.update(data)
    save_panel_config(cfg)
    return jsonify({"ok": True, "config": cfg})


@app.route("/api/tproxy/setup", methods=["POST"])
def api_tproxy_setup():
    """配置 nftables 规则 + 路由表 (tproxy 透明网关)"""
    data = request.get_json()
    lan_subnet = data.get("lan_subnet", "192.168.1.0/24")
    tproxy_port = data.get("tproxy_port", 7893)
    gateway_ip = data.get("gateway_ip", "")

    # 生成 nftables 规则
    nft_rule = f'''#!/usr/sbin/nft -f
flush ruleset

define LAN_SUBNET = {lan_subnet}
define TPROXY_PORT = {tproxy_port}

table ip sing-box {{
    chain prerouting {{
        type filter hook prerouting priority mangle; policy accept;
        ip daddr 0.0.0.0/8 return
        ip daddr 10.0.0.0/8 return
        ip daddr 100.64.0.0/10 return
        ip daddr 127.0.0.0/8 return
        ip daddr 169.254.0.0/16 return
        ip daddr 172.16.0.0/12 return
        ip daddr 192.0.0.0/24 return
        ip daddr 192.168.0.0/16 return
        ip daddr 224.0.0.0/4 return
        ip daddr 240.0.0.0/4 return
        ip daddr 255.255.255.255/32 return
        ip daddr $LAN_SUBNET tcp dport != 53 return
        ip daddr $LAN_SUBNET udp dport != 53 return
        meta mark 1 return
        ip protocol tcp tproxy to :$TPROXY_PORT meta mark set 1
        ip protocol udp tproxy to :$TPROXY_PORT meta mark set 1
    }}
    chain output {{
        type route hook output priority mangle; policy accept;
        ip daddr 0.0.0.0/8 return
        ip daddr 10.0.0.0/8 return
        ip daddr 100.64.0.0/10 return
        ip daddr 127.0.0.0/8 return
        ip daddr 169.254.0.0/16 return
        ip daddr 172.16.0.0/12 return
        ip daddr 192.0.0.0/24 return
        ip daddr 192.168.0.0/16 return
        ip daddr 224.0.0.0/4 return
        ip daddr 240.0.0.0/4 return
        ip daddr 255.255.255.255/32 return
        ip daddr $LAN_SUBNET tcp dport != 53 return
        ip daddr $LAN_SUBNET udp dport != 53 return
        meta mark 1 return
        ip protocol tcp meta mark set 1
        ip protocol udp meta mark set 1
    }}
    chain postrouting {{
        type nat hook postrouting priority 100; policy accept;
        ip saddr $LAN_SUBNET masquerade
    }}
}}'''
    nft_path = "/etc/nftables-sing-box.nft"
    with open(nft_path, "w") as f:
        f.write(nft_rule)

    # 加载内核模块
    run_cmd("modprobe nft_tproxy 2>/dev/null; modprobe xt_TPROXY 2>/dev/null")

    # 应用 nftables
    code, out, err = run_cmd(f'nft -f "{nft_path}"')
    if code != 0:
        return jsonify({"error": f"nftables 加载失败: {err}"}), 500

    # 设置路由表
    run_cmd("ip route del local default dev lo table 100 2>/dev/null")
    run_cmd("ip route add local default dev lo table 100")
    run_cmd("ip rule del fwmark 1 table 100 2>/dev/null")
    run_cmd("ip rule add fwmark 1 table 100")

    # 保存到 panel config
    cfg = get_panel_config()
    cfg["lan_subnet"] = lan_subnet
    cfg["tproxy_port"] = tproxy_port
    cfg["gateway_mode"] = "tproxy"
    save_panel_config(cfg)

    return jsonify({
        "ok": True,
        "message": f"tproxy 透明网关已配置 (网段: {lan_subnet}, 端口: {tproxy_port})",
    })


@app.route("/api/tproxy/clear", methods=["POST"])
def api_tproxy_clear():
    """清除 nftables 规则和路由表"""
    run_cmd("nft delete table ip sing-box 2>/dev/null")
    run_cmd("ip route del local default dev lo table 100 2>/dev/null")
    run_cmd("ip rule del fwmark 1 table 100 2>/dev/null")
    return jsonify({"ok": True, "message": "tproxy 规则已清除"})


# ============================================================
# 仪表盘 (Clash API 前端) 路由
# ============================================================

@app.route("/api/dashboards")
def api_dashboards():
    """列出所有可用/已安装的仪表盘"""
    return jsonify(list_dashboards())


@app.route("/api/dashboards/install", methods=["POST"])
def api_dashboard_install():
    """从 GitHub 下载并安装仪表盘"""
    data = request.get_json() or {}
    name = data.get("name", "").strip()
    if not name:
        return jsonify({"error": "name required"}), 400
    ok, msg = install_dashboard(name)
    if not ok:
        return jsonify({"error": msg}), 500
    # 安装后自动设为活动仪表盘 (如果是第一个)
    if not get_active_dashboard():
        cfg = get_panel_config()
        cfg["active_dashboard"] = name
        save_panel_config(cfg)
    return jsonify({"ok": True, "message": msg, "dashboards": list_dashboards()})


@app.route("/api/dashboards/active")
def api_dashboard_active():
    return jsonify({"active": get_active_dashboard()})


@app.route("/api/dashboards/<name>/activate", methods=["POST"])
def api_dashboard_activate(name):
    name = name.strip()
    if not os.path.exists(os.path.join(DASHBOARD_DIR, name, "index.html")):
        return jsonify({"error": f"仪表盘 {name} 未安装"}), 400
    cfg = get_panel_config()
    cfg["active_dashboard"] = name
    save_panel_config(cfg)
    return jsonify({"ok": True, "message": f"已切换到 {name}",
                    "url": f"/dashboard/{name}/"})


@app.route("/api/dashboards/<name>", methods=["DELETE"])
def api_dashboard_delete(name):
    target = os.path.join(DASHBOARD_DIR, name.strip())
    if not os.path.isdir(target):
        return jsonify({"error": "仪表盘不存在"}), 404
    shutil.rmtree(target, ignore_errors=True)
    cfg = get_panel_config()
    if cfg.get("active_dashboard") == name:
        cfg["active_dashboard"] = ""
        save_panel_config(cfg)
    return jsonify({"ok": True, "message": f"已删除 {name}",
                    "dashboards": list_dashboards()})


@app.route("/ui/")
@app.route("/ui")
def ui_redirect():
    """根 /ui/ 跳转到当前活动仪表盘"""
    active = get_active_dashboard()
    if active:
        return Response(f'<meta http-equiv="refresh" content="0;url=/dashboard/{active}/">',
                        mimetype="text/html")
    return Response("<h3>暂无已安装的仪表盘</h3><p>请先在「面板管理」页安装一个仪表盘。</p>",
                    mimetype="text/html")


@app.route("/dashboard/<name>/")
@app.route("/dashboard/<name>/<path:p>")
def serve_dashboard(name, p="index.html"):
    """静态托管仪表盘文件 (防目录穿越)"""
    name = name.strip()
    base = os.path.realpath(os.path.join(DASHBOARD_DIR, name))
    target = os.path.realpath(os.path.join(base, p))
    # 防穿越: target 必须在 base 之内
    if not target.startswith(base + os.sep) and target != base:
        return Response("Forbidden", status=403)
    if not os.path.isfile(target):
        # SPA 兜底: 不存在的路径回退 index.html
        target = os.path.join(base, "index.html")
        if not os.path.isfile(target):
            return Response("仪表盘未安装, 请先安装", status=404)
    # index.html 注入 Clash API 提示 (仅首页)
    if p == "index.html":
        try:
            with open(target, "r", encoding="utf-8") as f:
                content = f.read()
            # 注入一段提示脚本: 若未配置后端, 提示连接 :9090
            inject = (
                "<script>window.__SINGBOX_PANEL=1;"
                "window.__CLASH_API_HINT=' Clash API 运行在 :9090, 首次使用请在仪表盘设置中填入后端地址';"
                "</script>"
            )
            if "</head>" in content:
                content = content.replace("</head>", inject + "</head>", 1)
            return Response(content, mimetype="text/html")
        except Exception:
            pass
    import mimetypes
    mime, _ = mimetypes.guess_type(target)
    return Response(open(target, "rb").read(), mimetype=mime or "application/octet-stream")


# ============================================================
# Dashboard HTML
# ============================================================

DASHBOARD_HTML = r"""
<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>sing-box 旁路由网关面板</title>
<style>
:root {
  --bg: #0f1117; --surface: #1a1d27; --surface2: #252836; --border: #2d3142;
  --text: #e4e6ed; --text-dim: #8b8fa3; --primary: #6c5ce7; --primary-dim: #5849c2;
  --green: #00b894; --red: #e74c3c; --orange: #fdcb6e; --blue: #0984e3;
}
* { margin: 0; padding: 0; box-sizing: border-box; }
body { font-family: -apple-system, system-ui, sans-serif; background: var(--bg); color: var(--text); min-height: 100vh; }
.container { max-width: 1100px; margin: 0 auto; padding: 20px; }
header { display: flex; justify-content: space-between; align-items: center; padding: 16px 0; border-bottom: 1px solid var(--border); margin-bottom: 24px; }
header h1 { font-size: 20px; }
header h1 span { color: var(--primary); }
nav { display: flex; gap: 8px; flex-wrap: wrap; }
nav button { padding: 6px 14px; background: var(--surface); border: 1px solid var(--border); border-radius: 6px; color: var(--text-dim); cursor: pointer; font-size: 13px; transition: all .2s; }
nav button:hover, nav button.active { background: var(--primary); color: #fff; border-color: var(--primary); }
.card { background: var(--surface); border: 1px solid var(--border); border-radius: 10px; padding: 20px; margin-bottom: 16px; }
.card h2 { font-size: 14px; color: var(--text-dim); margin-bottom: 16px; font-weight: 500; }
.stat-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 12px; }
.stat { background: var(--surface2); border-radius: 8px; padding: 16px; text-align: center; }
.stat .value { font-size: 24px; font-weight: 700; }
.stat .label { font-size: 11px; color: var(--text-dim); margin-top: 4px; }
.stat .value.green { color: var(--green); }
.stat .value.red { color: var(--red); }
.stat .value.blue { color: var(--blue); }
.btn { padding: 8px 18px; border: 1px solid var(--border); border-radius: 6px; background: var(--surface2); color: var(--text); cursor: pointer; font-size: 13px; transition: all .2s; }
.btn:hover { border-color: var(--primary); }
.btn.primary { background: var(--primary); border-color: var(--primary); color: #fff; }
.btn.danger { background: var(--red); border-color: var(--red); color: #fff; }
.btn.green { background: var(--green); border-color: var(--green); color: #fff; }
.btn:disabled { opacity: .5; cursor: not-allowed; }
textarea { width: 100%; min-height: 320px; background: var(--bg); border: 1px solid var(--border); border-radius: 6px; color: var(--text); font-family: 'SF Mono', monospace; font-size: 12px; padding: 12px; resize: vertical; }
input, select { width: 100%; background: var(--bg); border: 1px solid var(--border); border-radius: 6px; color: var(--text); padding: 8px 12px; font-size: 13px; }
label { font-size: 12px; color: var(--text-dim); display: block; margin-bottom: 4px; }
.row { display: flex; gap: 12px; flex-wrap: wrap; align-items: end; }
.row > div { flex: 1; min-width: 120px; }
.actions { display: flex; gap: 8px; margin-top: 12px; flex-wrap: wrap; }
.sub-item { display: flex; justify-content: space-between; align-items: center; padding: 10px 14px; background: var(--surface2); border-radius: 6px; margin-bottom: 8px; }
.sub-item .name { font-weight: 600; }
.sub-item .url { font-size: 11px; color: var(--text-dim); word-break: break-all; }
.badge { display: inline-block; padding: 2px 8px; border-radius: 10px; font-size: 11px; }
.badge.green { background: rgba(0,184,148,.2); color: var(--green); }
.badge.red { background: rgba(231,76,60,.2); color: var(--red); }
#msg { position: fixed; bottom: 20px; right: 20px; padding: 12px 20px; border-radius: 8px; font-size: 13px; z-index: 999; display: none; }
.log-box { background: var(--bg); border: 1px solid var(--border); border-radius: 6px; padding: 12px; max-height: 400px; overflow-y: auto; font-family: monospace; font-size: 11px; white-space: pre-wrap; }
.hidden { display: none; }
@media (max-width: 600px) { .row { flex-direction: column; } .container { padding: 12px; } }
</style>
</head>
<body>
<div class="container">
  <header>
    <h1>🌐 sing-box <span>旁路由网关</span></h1>
    <nav>
      <button class="active" onclick="showTab('dashboard')">概览</button>
      <button onclick="showTab('config')">配置</button>
      <button onclick="showTab('subs')">订阅</button>
      <button onclick="showTab('convert')">转换</button>
      <button onclick="showTab('update')">更新</button>
      <button onclick="showTab('dashboard-mgr')">仪表盘</button>
      <button onclick="showTab('network')">网络</button>
      <button onclick="showTab('logs')">日志</button>
    </nav>
  </header>

  <!-- 概览 -->
  <div id="tab-dashboard" class="tab">
    <div class="card">
      <h2>运行状态</h2>
      <div class="stat-grid" id="stats"></div>
    </div>
    <div class="card">
      <h2>服务控制</h2>
      <div class="actions">
        <button class="btn green" onclick="api('start')">启动</button>
        <button class="btn danger" onclick="api('stop')">停止</button>
        <button class="btn primary" onclick="api('restart')">重启</button>
        <button class="btn" onclick="loadStatus()">刷新</button>
      </div>
    </div>
    <div class="card">
      <h2>网络信息</h2>
      <div id="net-info" class="stat-grid"></div>
    </div>
  </div>

  <!-- 配置 -->
  <div id="tab-config" class="tab hidden">
    <div class="card">
      <h2>当前配置 (config.json)</h2>
      <textarea id="cfg-text"></textarea>
      <div class="actions">
        <button class="btn primary" onclick="saveConfig()">保存并校验</button>
        <button class="btn" onclick="loadConfig()">重新加载</button>
        <button class="btn" onclick="checkConfig()">仅校验</button>
      </div>
    </div>
    <div class="card">
      <h2>模板切换</h2>
      <div class="row">
        <div><label>选择模板</label><select id="tpl-select"></select></div>
        <div style="flex:0"><button class="btn primary" onclick="applyTemplate()">应用模板</button></div>
      </div>
    </div>
  </div>

  <!-- 订阅 -->
  <div id="tab-subs" class="tab hidden">
    <div class="card">
      <h2>添加订阅</h2>
      <div class="row">
        <div><label>名称</label><input id="sub-name" placeholder="机场A"></div>
        <div style="flex:2"><label>订阅地址</label><input id="sub-url" placeholder="https://..."></div>
        <div style="flex:0"><button class="btn primary" onclick="addSub()">添加</button></div>
      </div>
    </div>
    <div class="card">
      <h2>订阅列表</h2>
      <div id="sub-list"></div>
      <div class="actions">
        <button class="btn green" onclick="updateSubs()">更新所有订阅</button>
      </div>
    </div>
  </div>

  <!-- 转换 -->
  <div id="tab-convert" class="tab hidden">
    <div class="card">
      <h2>订阅转换 (→ sing-box JSON)</h2>
      <p style="color:var(--text-dim);font-size:12px;margin-bottom:12px">支持: Clash YAML (ss/vmess/vless/trojan/hysteria2/tuic/wireguard/socks5) / V2Ray Base64 / sing-box JSON<br>✅ 自动注入: 防DNS泄露(fakeip+分流DNS) + 国内外分流(geosite/geoip) + Clash代理组转换</p>
      <div class="row">
        <div style="flex:3"><label>订阅地址</label><input id="conv-url" placeholder="https://..."></div>
        <div><label>模板</label><select id="conv-tpl"></select></div>
      </div>
      <div class="actions">
        <button class="btn primary" onclick="convertSub()">转换预览</button>
        <button class="btn green" onclick="convertApply()">⚡ 转换并应用 (写入 config.json + 重启)</button>
        <button class="btn" onclick="convertDownload()">下载 JSON</button>
      </div>
    </div>
    <div class="card hidden" id="conv-result">
      <h2>转换结果</h2>
      <div id="conv-info" style="margin-bottom:12px;font-size:13px;"></div>
      <textarea id="conv-text" style="min-height:400px"></textarea>
    </div>
  </div>

  <!-- 更新 -->
  <div id="tab-update" class="tab hidden">
    <div class="card">
      <h2>核心版本</h2>
      <div class="stat-grid" id="ver-stats"></div>
      <div class="actions">
        <button class="btn primary" onclick="updateCore()">一键升级核心</button>
        <button class="btn" onclick="loadVersion()">检查更新</button>
      </div>
    </div>
    <div class="card">
      <h2>面板更新</h2>
      <p style="color:var(--text-dim);font-size:12px">面板代码位于 /opt/singbox-gateway/panel/，可通过重新运行部署脚本更新:</p>
      <pre style="background:var(--bg);padding:12px;border-radius:6px;font-size:11px;margin-top:8px">bash /opt/singbox-gateway/sing-box-gateway-deploy.sh --update-panel</pre>
    </div>
  </div>

  <!-- 仪表盘 -->
  <div id="tab-dashboard-mgr" class="tab hidden">
    <div class="card">
      <h2>Clash API 仪表盘 (前端)</h2>
      <p style="color:var(--text-dim);font-size:12px;margin-bottom:12px">这些是 sing-box 内置 Clash API (:9090) 的 Web 前端, 本地托管, 无需外网。安装后在仪表盘设置中填入后端地址 <b>http://本机IP:9090</b> 即可连接。</p>
      <div id="dash-list"></div>
    </div>
    <div class="card">
      <h2>当前活动仪表盘</h2>
      <div id="dash-active" style="font-size:14px;margin-bottom:12px"></div>
      <a class="btn primary" id="dash-open" href="/ui/" target="_blank" style="text-decoration:none;display:inline-block">打开仪表盘 ↗</a>
    </div>
  </div>

  <!-- 网络 -->
  <div id="tab-network" class="tab hidden">
    <div class="card">
      <h2>透明网关 (tproxy) 配置</h2>
      <div class="row">
        <div><label>内网网段</label><input id="lan-subnet" value="192.168.1.0/24"></div>
        <div><label>tproxy 端口</label><input id="tproxy-port" value="7893" type="number"></div>
      </div>
      <div class="actions">
        <button class="btn primary" onclick="setupTproxy()">应用 tproxy 规则</button>
        <button class="btn danger" onclick="clearTproxy()">清除规则</button>
      </div>
    </div>
    <div class="card">
      <h2>当前网络状态</h2>
      <div id="net-status" class="stat-grid"></div>
    </div>
  </div>

  <!-- 日志 -->
  <div id="tab-logs" class="tab hidden">
    <div class="card">
      <h2>sing-box 日志</h2>
      <div class="actions" style="margin-bottom:12px">
        <button class="btn" onclick="loadLogs(50)">刷新 (50行)</button>
        <button class="btn" onclick="loadLogs(200)">加载 200 行</button>
      </div>
      <div id="log-box" class="log-box">点击刷新加载日志...</div>
    </div>
  </div>
</div>

<div id="msg"></div>

<script>
function showTab(name) {
  document.querySelectorAll('.tab').forEach(t => t.classList.add('hidden'));
  document.getElementById('tab-' + name).classList.remove('hidden');
  document.querySelectorAll('nav button').forEach(b => b.classList.remove('active'));
  [...document.querySelectorAll('nav button')].find(b => b.textContent.includes({dashboard:'概览',config:'配置',subs:'订阅',convert:'转换',update:'更新','dashboard-mgr':'仪表盘',network:'网络',logs:'日志'}[name])).classList.add('active');
  if (name === 'dashboard') loadStatus();
  if (name === 'config') { loadConfig(); loadTemplates('tpl-select'); }
  if (name === 'convert') loadTemplates('conv-tpl');
  if (name === 'update') loadVersion();
  if (name === 'dashboard-mgr') loadDashboards();
  if (name === 'network') loadNetStatus();
}

function msg(text, isErr) {
  const el = document.getElementById('msg');
  el.textContent = text;
  el.style.background = isErr ? 'var(--red)' : 'var(--green)';
  el.style.color = '#fff';
  el.style.display = 'block';
  setTimeout(() => el.style.display = 'none', 3000);
}

async function api(action) {
  const r = await fetch('/api/' + action, { method: 'POST' });
  const d = await r.json();
  msg(d.message || d.error || (d.ok ? '操作成功' : '操作失败'), !d.ok);
  loadStatus();
}

async function loadStatus() {
  try {
    const r = await fetch('/api/status');
    const d = await r.json();
    const clash = d.clash ? `<div class="stat"><div class="value blue">${d.clash.connections||0}</div><div class="label">活跃连接</div></div>` : '';
    document.getElementById('stats').innerHTML = `
      <div class="stat"><div class="value ${d.running?'green':'red'}">${d.running?'运行中':'已停止'}</div><div class="label">核心状态</div></div>
      <div class="stat"><div class="value">${d.version}</div><div class="label">当前版本</div></div>
      <div class="stat"><div class="value blue">${d.latest}</div><div class="label">最新版本</div></div>
      <div class="stat"><div class="value">${d.uptime}</div><div class="label">运行时长</div></div>
      ${clash}
    `;
    const n = d.network;
    document.getElementById('net-info').innerHTML = `
      <div class="stat"><div class="value" style="font-size:14px">${(n.ip_addresses||[]).join('<br>')||'—'}</div><div class="label">本机 IP</div></div>
      <div class="stat"><div class="value" style="font-size:14px">${n.default_gateway||'—'}</div><div class="label">默认网关</div></div>
      <div class="stat"><div class="value ${n.ip_forward?'green':'red'}" style="font-size:14px">${n.ip_forward?'已开启':'未开启'}</div><div class="label">IP 转发</div></div>
      <div class="stat"><div class="value ${n.nftables_active?'green':'red'}" style="font-size:14px">${n.nftables_active?'已加载':'未加载'}</div><div class="label">nftables</div></div>
    `;
  } catch(e) { msg('加载状态失败', true); }
}

async function loadConfig() {
  const r = await fetch('/api/config');
  const d = await r.json();
  document.getElementById('cfg-text').value = JSON.stringify(d, null, 2);
}

async function saveConfig() {
  try {
    const cfg = JSON.parse(document.getElementById('cfg-text').value);
    const r = await fetch('/api/config', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify(cfg) });
    const d = await r.json();
    msg(d.message || d.error, !d.ok);
  } catch(e) { msg('JSON 解析错误: ' + e.message, true); }
}

async function checkConfig() {
  const r = await fetch('/api/check', { method: 'POST' });
  const d = await r.json();
  msg(d.message, !d.ok);
}

async function loadTemplates(selectId) {
  const r = await fetch('/api/templates');
  const d = await r.json();
  const sel = document.getElementById(selectId);
  sel.innerHTML = d.map(t => `<option value="${t}">${t}</option>`).join('');
}

async function applyTemplate() {
  const tpl = document.getElementById('tpl-select').value;
  const r = await fetch('/api/template/apply', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({template: tpl}) });
  const d = await r.json();
  msg(d.message || d.error, !d.ok);
}

async function loadSubs() {
  const r = await fetch('/api/subscriptions');
  const d = await r.json();
  document.getElementById('sub-list').innerHTML = d.length ? d.map(s => `
    <div class="sub-item">
      <div><div class="name">${s.name}</div><div class="url">${s.url}</div></div>
      <button class="btn danger" onclick="delSub('${s.name}')">删除</button>
    </div>`).join('') : '<p style="color:var(--text-dim)">暂无订阅</p>';
}

async function addSub() {
  const name = document.getElementById('sub-name').value;
  const url = document.getElementById('sub-url').value;
  const r = await fetch('/api/subscriptions', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({name, url}) });
  const d = await r.json();
  msg(d.ok ? '订阅已添加' : (d.error||'失败'), !d.ok);
  document.getElementById('sub-name').value = '';
  document.getElementById('sub-url').value = '';
  loadSubs();
}

async function delSub(name) {
  const r = await fetch('/api/subscriptions/' + encodeURIComponent(name), { method: 'DELETE' });
  loadSubs();
  msg('订阅已删除');
}

async function updateSubs() {
  msg('正在更新订阅...');
  const r = await fetch('/api/subscriptions/update', { method: 'POST' });
  const d = await r.json();
  msg(d.message || d.error, !d.ok);
}

async function convertSub() {
  const url = document.getElementById('conv-url').value;
  const tpl = document.getElementById('conv-tpl').value;
  if (!url) { msg('请输入订阅地址', true); return; }
  msg('转换中...');
  const r = await fetch('/api/convert', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({url, template: tpl}) });
  const d = await r.json();
  if (d.ok) {
    document.getElementById('conv-result').classList.remove('hidden');
    document.getElementById('conv-info').innerHTML = `✅ 成功解析 <b>${d.node_count}</b> 个节点: ${d.nodes.join(', ')}`;
    document.getElementById('conv-text').value = JSON.stringify(d.config, null, 2);
    msg('转换成功');
  } else {
    msg(d.error || '转换失败', true);
  }
}

function convertDownload() {
  const url = document.getElementById('conv-url').value;
  const tpl = document.getElementById('conv-tpl').value;
  fetch('/api/convert/download', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({url, template: tpl}) })
    .then(r => r.blob())
    .then(b => { const a = document.createElement('a'); a.href = URL.createObjectURL(b); a.download = 'sing-box-config.json'; a.click(); });
}

async function convertApply() {
  const url = document.getElementById('conv-url').value;
  const tpl = document.getElementById('conv-tpl').value;
  if (!url) { msg('请输入订阅地址', true); return; }
  if (!confirm('将转换订阅并直接写入 config.json 然后 sing-box check 校验 + 重启, 确认?')) return;
  msg('正在转换并应用...');
  const r = await fetch('/api/convert/apply', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({url, template: tpl}) });
  const d = await r.json();
  if (d.ok) {
    document.getElementById('conv-result').classList.remove('hidden');
    document.getElementById('conv-info').innerHTML = `✅ 成功解析 <b>${d.node_count}</b> 个节点, 已应用 <b>${d.template}</b> 模板并重启`;
    msg(d.message || '已应用', false);
  } else {
    msg(d.error || '应用失败', true);
  }
}

async function loadVersion() {
  const r = await fetch('/api/core/version');
  const d = await r.json();
  const same = d.installed === d.latest;
  document.getElementById('ver-stats').innerHTML = `
    <div class="stat"><div class="value">${d.installed}</div><div class="label">已安装版本</div></div>
    <div class="stat"><div class="value ${same?'green':'orange'}">${d.latest}</div><div class="label">最新版本 ${same?'(最新)':'(可更新)'}</div></div>
  `;
}

async function updateCore() {
  if (!confirm('确定要升级 sing-box 核心吗? 服务将短暂中断.')) return;
  msg('正在下载并升级...');
  const r = await fetch('/api/core/update', { method: 'POST' });
  const d = await r.json();
  msg(d.message || d.error, !d.ok);
  loadVersion();
  loadStatus();
}

async function loadNetStatus() {
  const r = await fetch('/api/network');
  const n = await r.json();
  document.getElementById('net-status').innerHTML = `
    <div class="stat"><div class="value" style="font-size:14px">${(n.ip_addresses||[]).join('<br>')||'—'}</div><div class="label">本机 IP</div></div>
    <div class="stat"><div class="value" style="font-size:14px">${n.default_gateway||'—'}</div><div class="label">默认网关</div></div>
    <div class="stat"><div class="value" style="font-size:14px">${n.interface||'—'}</div><div class="label">出口网卡</div></div>
    <div class="stat"><div class="value ${n.ip_forward?'green':'red'}" style="font-size:14px">${n.ip_forward?'已开启':'未开启'}</div><div class="label">IP 转发</div></div>
    <div class="stat"><div class="value ${n.nftables_active?'green':'red'}" style="font-size:14px">${n.nftables_active?'已加载':'未加载'}</div><div class="label">nftables</div></div>
  `;
}

async function setupTproxy() {
  const lan = document.getElementById('lan-subnet').value;
  const port = document.getElementById('tproxy-port').value;
  const r = await fetch('/api/tproxy/setup', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({lan_subnet: lan, tproxy_port: port}) });
  const d = await r.json();
  msg(d.message || d.error, !d.ok);
  loadNetStatus();
}

async function clearTproxy() {
  const r = await fetch('/api/tproxy/clear', { method: 'POST' });
  const d = await r.json();
  msg(d.message, !d.ok);
  loadNetStatus();
}

async function loadLogs(lines) {
  const r = await fetch('/api/logs?lines=' + lines);
  const d = await r.json();
  document.getElementById('log-box').textContent = d.logs;
}

async function loadDashboards() {
  const r = await fetch('/api/dashboards');
  const d = await r.json();
  const list = document.getElementById('dash-list');
  list.innerHTML = d.map(db => `
    <div class="sub-item">
      <div>
        <div class="name">${db.name}${db.active?' <span class="badge green">活动中</span>':''}${db.installed?' <span class="badge green">已安装</span>':' <span class="badge red">未安装</span>'}</div>
        <div class="url">${db.desc}${db.repo?' · '+db.repo:''}</div>
      </div>
      <div style="display:flex;gap:6px">
        ${db.installed ? `
          <button class="btn ${db.active?'':''}" ${db.active?'disabled':''} onclick="activateDash('${db.name}')">${db.active?'活动中':'切换为活动'}</button>
          <a class="btn" href="/dashboard/${db.name}/" target="_blank" style="text-decoration:none;display:inline-block">打开</a>
          <button class="btn danger" onclick="delDash('${db.name}')">删除</button>
        ` : `
          <button class="btn primary" onclick="installDash('${db.name}')">安装</button>
        `}
      </div>
    </div>`).join('');
  const active = d.find(x => x.active);
  document.getElementById('dash-active').textContent = active ? `当前活动: ${active.name} (${active.desc})` : '未设置活动仪表盘';
}

async function installDash(name) {
  msg('正在下载并安装 ' + name + ' ...');
  const r = await fetch('/api/dashboards/install', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({name}) });
  const d = await r.json();
  msg(d.message || d.error, !d.ok);
  loadDashboards();
}

async function activateDash(name) {
  const r = await fetch('/api/dashboards/' + encodeURIComponent(name) + '/activate', { method: 'POST' });
  const d = await r.json();
  msg(d.message || d.error, !d.ok);
  loadDashboards();
}

async function delDash(name) {
  if (!confirm('确定删除仪表盘 ' + name + '?')) return;
  const r = await fetch('/api/dashboards/' + encodeURIComponent(name), { method: 'DELETE' });
  const d = await r.json();
  msg(d.message || d.error, !d.ok);
  loadDashboards();
}

loadStatus();
loadSubs();
setInterval(loadStatus, 10000);
</script>
</body>
</html>
"""


# ============================================================
# 入口
# ============================================================

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="sing-box Gateway Panel")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=9999)
    args = parser.parse_args()
    app.run(host=args.host, port=args.port, debug=False)
