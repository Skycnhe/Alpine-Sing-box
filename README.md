# sing-box 旁路由网关自动部署 (Alpine Linux)

> 基于 sing-box 核心的 Alpine Linux 旁路由透明代理网关，自带 Web 管理面板、订阅解析/转换、一键更新。

## 一键安装

在 Alpine Linux 上执行以下命令，全自动完成下载 + 部署 + 启动：

```bash
wget -qO- https://github.com/Skycnhe/Alpine-Sing-box/archive/refs/heads/Hk001.tar.gz | tar xz -C /tmp && cd /tmp/Alpine-Sing-box-Hk001 && bash sing-box-gateway-deploy.sh --install
```

> 如果系统已有 `curl`，也可用：
> ```bash
> curl -fsSL https://github.com/Skycnhe/Alpine-Sing-box/archive/refs/heads/Hk001.tar.gz | tar xz -C /tmp && cd /tmp/Alpine-Sing-box-Hk001 && bash sing-box-gateway-deploy.sh --install
> ```

部署完成后，脚本会自动安装 **`sb`** 快捷命令。之后直接输入 `sb` 即可打开管理菜单，无需再记脚本路径：

```bash
sb                    # 打开交互式管理菜单
sb --update-core      # 更新 sing-box 核心
sb --convert-apply <订阅URL>   # 转换 Clash 订阅并直接应用
sb --help             # 查看帮助
```

管理面板地址：`http://<旁路由IP>:9999`

> **Clash API 后端密钥**: `singbox-gateway`（连接仪表盘时填入）

---

## 目录

- [一键安装](#一键安装)
- [功能概览](#功能概览)
- [架构设计](#架构设计)
- [快速部署](#快速部署)
- [透明网关模式](#透明网关模式)
- [管理面板](#管理面板)
- [订阅转换](#订阅转换)
- [命令行工具](#命令行工具)
- [多仪表盘管理](#多仪表盘管理)
- [透明网关模板收集](#透明网关模板收集)
- [旁路由网络配置](#旁路由网络配置)
- [目录结构](#目录结构)
- [常见问题](#常见问题)

---

## 功能概览

| 功能 | 说明 |
|------|------|
| **自动部署** | 一键安装 sing-box 核心 + Web 面板 + OpenRC 服务 + 所有依赖 |
| **透明网关** | 支持 tproxy（nftables 重定向）和 tun（自动路由）两种模式 |
| **Web 面板** | Flask 管理面板：状态/配置/订阅/转换/核心更新/网络/日志 |
| **多仪表盘** | 内置 metacubexd / zashboard / yacd 等前端，在线安装/切换/删除/更新 |
| **订阅管理** | 添加/删除/更新订阅，自动拉取节点并合并入 config.json |
| **订阅转换** | 全协议解析器，支持 Clash YAML / V2Ray base64 → sing-box JSON |
| **转换并应用** | 一键将 Clash 订阅转为 config.json + sing-box check 校验 + 重启，直接本地生效 |
| **核心更新** | 面板内一键检测+下载+安装最新 sing-box 核心 |
| **模板系统** | 内置 3 套透明网关模板，面板内一键切换 |

## 架构设计

```
┌──────────────────────────────────────────────────────┐
│                    Alpine Linux (旁路由)                │
│                                                        │
│  ┌─────────────┐    ┌──────────────┐    ┌──────────┐  │
│  │  Web 面板    │───▶│  sing-box     │───▶│  节点     │  │
│  │  (Flask)     │    │  核心 (tproxy)│    │  代理出口  │  │
│  │  :9999       │    │  :7893/:2080  │    └──────────┘  │
│  └──────┬───────┘    └──────┬───────┘                   │
│         │                   │                            │
│         ▼                   ▼                            │
│  ┌─────────────┐    ┌──────────────┐                   │
│  │  订阅解析器   │    │  nftables     │                   │
│  │  (V2Ray/     │    │  (tproxy规则)  │                   │
│  │   Clash→SB)  │    │  fwmark→table  │                   │
│  └─────────────┘    │  100→lo       │                   │
│                      └──────────────┘                   │
│                                                        │
│  OpenRC 服务: sing-box / singbox-panel / nftables-sb  │
└──────────────────────────────────────────────────────┘
         ▲                          ▲
         │ DHCP 网关指向旁路由 IP     │
    ┌────┴────┐               ┌─────┴─────┐
    │ 局域网   │               │  主路由    │
    │ 设备们   │               │ (192.168  │
    │          │               │   .1)     │
    └─────────┘               └───────────┘
```

---

## 快速部署

> 如果只想一键完成，直接用[一键安装](#一键安装)命令即可。以下为手动步骤。

### 1. 将项目上传到 Alpine Linux

```bash
# 方式一: git clone
git clone -b Hk001 https://github.com/Skycnhe/Alpine-Sing-box.git
cd Alpine-Sing-box

# 方式二: 下载 tar.gz
wget -O sb.tar.gz https://github.com/Skycnhe/Alpine-Sing-box/archive/refs/heads/Hk001.tar.gz
tar xzf sb.tar.gz && cd Alpine-Sing-box-Hk001

# 方式三: scp -r sing-box-gateway/ root@<alpine-ip>:/root/
```

### 2. 执行部署

```bash
bash sing-box-gateway-deploy.sh
```

> 或直接用非交互模式：`bash sing-box-gateway-deploy.sh --install`

脚本会自动完成：
1. `apk add` 安装依赖（nftables / iproute2 / python3 / iptables / jq ...）
2. 从 GitHub 下载最新 sing-box 核心（自动适配 amd64/arm64/armv7）
3. 安装 3 套透明网关模板到 `/etc/sing-box/templates/`
4. 创建 Python venv + 安装 Flask 面板
5. 创建 3 个 OpenRC 服务（sing-box / singbox-panel / nftables-sing-box）
6. 配置 IP 转发 + 加载 tproxy 内核模块
7. 启动所有服务
8. 安装 `sb` 快捷命令到 `/usr/local/bin/sb`

### 3. 访问面板

部署完成后，浏览器访问：

```
http://<旁路由IP>:9999
```

---

## 透明网关模式

本方案内置 3 套模板，覆盖不同使用场景：

### 模板一：tproxy 透明网关（推荐旁路由）

**文件**: `templates/config-tproxy.json`

**原理**: 使用 nftables 将局域网设备的 TCP/UDP 流量重定向到 sing-box tproxy 端口，配合 `fwmark` + 路由表 100 实现透明代理。

**特点**:
- ✅ 局域网设备无需任何配置，设网关为旁路由 IP 即可
- ✅ DNS 透明劫持，防止 DNS 污染
- ⚠️ 需要 nftables 规则和内核 tproxy 模块

**关键配置**:
```json
"inbounds": [{
    "type": "tproxy",
    "tag": "tproxy-in",
    "listen": "::",
    "listen_port": 7893,
    "sniff": true,
    "sniff_override_destination": true
}]
```

**配套 nftables 规则** (`nftables/sing-box-tproxy.nft`):
```nft
table ip sing-box {
    chain prerouting {
        type filter hook prerouting priority mangle; policy accept;
        ip daddr $RESERVED_IP return
        ip daddr $LAN_SUBNET tcp dport != 53 return
        ip daddr $LAN_SUBNET udp dport != 53 return
        meta mark 1 return
        ip protocol tcp tproxy to :7893 meta mark set 1
        ip protocol udp tproxy to :7893 meta mark set 1
    }
    chain output {
        type route hook output priority mangle; policy accept;
        # ... (本机出站流量打 fwmark)
    }
}
```

**路由表配置**:
```bash
ip route add local default dev lo table 100
ip rule add fwmark 1 table 100
```

> 面板「网络」页可一键生成并应用这些规则，无需手动配置。

### 模板二：TUN 自动路由（无需防火墙规则）

**文件**: `templates/config-tun.json`

**原理**: sing-box 创建 TUN 虚拟网卡，通过 `auto_route` + `strict_route` 自动接管路由，不需要 nftables/iptables。

**特点**:
- ✅ 无需任何防火墙规则，最简单
- ✅ sing-box 全自动管理路由表
- ⚠️ 旁路由模式下需确保主路由 DHCP 下发网关指向本机

**关键配置**:
```json
"inbounds": [{
    "type": "tun",
    "tag": "tun-in",
    "inet4_address": "172.19.0.1/30",
    "auto_route": true,
    "strict_route": true,
    "sniff": true,
    "sniff_override_destination": true
}]
```

### 模板三：Mixed 代理（客户端模式）

**文件**: `templates/config-mixed.json`

**原理**: 仅提供 HTTP/SOCKS5 混合代理端口，不做透明网关。

**特点**:
- ✅ 最轻量，不需要任何防火墙/路由配置
- ⚠️ 局域网设备需手动设置代理指向 `旁路由IP:2080`

---

## 管理面板

面板地址: `http://<旁路由IP>:9999`

> **Clash API 后端地址**: `http://<旁路由IP>:9090`
> **Clash API 密钥 (Secret)**: `singbox-gateway`

### 功能页面

| 页面 | 功能 |
|------|------|
| **概览** | 核心/版本/运行状态/连接数/IP转发/nftables状态/服务启停 |
| **配置** | 在线编辑 config.json（带 JSON 校验），模板一键切换 |
| **订阅** | 添加/删除订阅链接，一键拉取+合并节点+重启核心 |
| **转换** | 输入订阅 URL → 输出完整 sing-box JSON（可下载） |
| **更新** | 检测核心版本，一键下载安装最新 sing-box |
| **网络** | tproxy 规则一键配置（网段+端口），网络状态查看 |
| **日志** | 实时查看 sing-box 运行日志 |

### Clash API

所有模板均开启了 sing-box 内置 Clash API (`0.0.0.0:9090`)，并设置了后端密钥。可配合 [yacd](https://yacd.haishan.me) / [metacubexd](https://d.metacubex.one) 等面板实时切换节点：

| 配置项 | 值 |
|--------|-----|
| **后端地址 (Host)** | `http://<旁路由IP>:9090` |
| **密钥 (Secret)** | `singbox-gateway` |

> 密钥定义在 `config.json` 的 `experimental.clash_api.secret` 字段。如需修改，编辑该字段后 `rc-service sing-box restart`。

---

## 订阅转换

面板内置订阅解析器，**无需部署额外的 subconverter**，支持以下格式 → sing-box JSON：

| 输入格式 | 支持的协议 |
|----------|-----------|
| **Clash YAML** | ss, vmess, vless, trojan, hysteria2, tuic, wireguard, socks5, http（全传输：ws/grpc/h2/http/httpupgrade/quic/xhttp；全 TLS：tls/reality-opts/client-fingerprint/alpn/skip-cert-verify） |
| **V2Ray Base64** | `ss://` `vmess://` `vless://` `trojan://` `hysteria2://` `hy2://` `tuic://` |
| **sing-box JSON** | 直接透传 outbounds |

### 使用方式

**转换预览**: 「转换」页 → 输入订阅 URL → 选择模板 → 点击「转换预览」→ 查看节点和生成的 JSON

**转换并应用（一键本地生效）**: 点击「⚡ 转换并应用」→ 自动执行：下载订阅 → 解析 → 合并进模板 → `sing-box check` 校验 → 写入 `config.json` → 重启 sing-box。Clash 订阅直接变成本地可用的透明网关配置。

**命令行**:
```bash
# 转换为 sing-box JSON（默认 tproxy 模板，预览输出）
sb --convert "https://example.com/sub"

# 指定 tun 模板
sb --convert "https://example.com/sub" tun

# 转换并直接应用（写入 config.json + 校验 + 重启）
sb --convert-apply "https://example.com/sub" tproxy
```

### Clash 订阅完整支持矩阵

| 协议 | TLS | 传输 | 特殊字段 |
|------|-----|------|----------|
| ss | — | tcp | cipher, password, plugin |
| vmess | tls/reality | ws/grpc/h2/http/quic | alterId, cipher, client-fingerprint, ws-opts(headers, early-data) |
| vless | tls/reality | ws/grpc/h2/xhttp | flow(xtls-rprx-vision), reality-opts(public-key, short-id) |
| trojan | tls(默认) | ws/grpc/h2 | sni, skip-cert-verify |
| hysteria2 | tls | quic | up/down, obfs(salamander)+password |
| tuic | tls | quic | congestion-controller, udp-relay-mode, reduce-rtt |
| wireguard | — | — | private-key, public-key, ip, mtu |
| socks5/http | tls(可选) | tcp | username, password, udp |

### 订阅管理流程

```
添加订阅 URL → 面板拉取 → 自动识别格式 → 解析为节点列表
→ 合并入 config.json 的 outbounds → sing-box check 校验
→ 重启 sing-box → 完成
```

---

## 命令行工具

部署完成后自动安装 **`sb`** 快捷命令（`/usr/local/bin/sb`），可直接使用：

| 命令 | 作用 |
|------|------|
| `sb` | 交互式菜单（默认） |
| `sb --install` | 完整部署（非交互） |
| `sb --update-core` | 更新 sing-box 核心 |
| `sb --update-panel` | 更新 Flask 管理面板 |
| `sb --update-dashboard <name>` | 安装/更新仪表盘（metacubexd/zashboard/yacd） |
| `sb --update-sub` | 拉取并合并所有订阅 |
| `sb --convert <URL> [模板]` | 转换订阅为 sing-box JSON（预览） |
| `sb --convert-apply <URL> [模板]` | 转换并应用（写入 config.json + 重启） |
| `sb --install-shortcut` | 重新安装 sb 快捷命令 |
| `sb --status` | 查看当前配置与状态 |
| `sb --uninstall` | 交互式卸载 |
| `sb --help` | 显示帮助 |

> `sb` 等价于 `bash /opt/singbox-gateway/sing-box-gateway-deploy.sh`，所有参数原样透传。

**OpenRC 服务管理**:
```bash
rc-service sing-box start|stop|restart|status
rc-service singbox-panel start|stop|restart|status
rc-service nftables-sing-box start|stop
rc-update add sing-box default       # 开机自启
```

---

## 多仪表盘管理

面板内置 **Clash API 前端仪表盘**管理，可在线安装/切换/删除/更新多种主流面板，全部本地托管，无需外网访问即可使用。

### 内置仪表盘

| 仪表盘 | 来源仓库 | 说明 |
|--------|---------|------|
| **metacubexd** | MetaCubeX/metacubexd | 官方面板，功能最全（推荐） |
| **zashboard** | Zephyruso/zashboard | 现代 UI，含中文字体 |
| **zashboard-lite** | Zephyruso/zashboard | 精简版，不含字体，体积小 |
| **yacd** | haishanh/yacd | 经典面板 |

### 使用方式

**Web 面板**: 进入「仪表盘」页 → 选择仪表盘 → 安装/切换为活动/删除 → 点「打开仪表盘」

**命令行**:
```bash
sb --update-dashboard metacubexd
# 或交互菜单选 8) 面板管理
```

安装后访问 `http://<旁路由IP>:9999/ui/` 自动跳转到当前活动仪表盘。首次使用在仪表盘设置中填入：

| 配置项 | 值 |
|--------|-----|
| **后端地址 (Host)** | `http://<旁路由IP>:9090` |
| **密钥 (Secret)** | `singbox-gateway` |

---

## 透明网关模板收集

本项目收集并整理了以下可用于 sing-box 透明网关的模板/资源：

### 内置模板

| 模板文件 | 模式 | 适用场景 |
|----------|------|---------|
| `templates/config-tproxy.json` | tproxy + nftables | 旁路由透明网关（推荐） |
| `templates/config-tun.json` | TUN auto_route | 简单透明网关（无需防火墙规则） |
| `templates/config-mixed.json` | Mixed (HTTP+SOCKS) | 客户端代理（非透明） |

### 规则集资源

模板中使用 sing-box 官方规则集（`.srs` 二进制格式）：

| 规则集 | 用途 | 来源 |
|--------|------|------|
| `geosite-cn` | 国内域名直连 | `SagerNet/sing-geosite` |
| `geoip-cn` | 国内 IP 直连 | `SagerNet/sing-geoip` |
| `geosite-category-ads-all` | 广告拦截 | `SagerNet/sing-geosite` |
| `geosite-geolocation-!cn` | 非中国域名走代理 | `SagerNet/sing-geosite` |

### 外部参考项目

以下项目提供了 sing-box 透明网关的配置思路，本项目综合参考：

| 项目 | 说明 |
|------|------|
| [mario-huang/sing-box-bypass-router-transparent-proxy](https://github.com/mario-huang/sing-box-bypass-router-transparent-proxy-configuration) | 旁路由 TUN 模式透明代理教程 |
| [ak1ra-lab/sing-box-tproxy](https://github.com/ak1ra-lab/sing-box-tproxy) | Ansible 自动化 tproxy 透明代理 |
| [sing-box-tproxy.github.io](https://github.com/sing-box-tproxy/sing-box-tproxy.github.io) | TProxy 透明代理网关文档 |
| [lyc8503/sing-box-rules](https://github.com/lyc8503/sing-box-rules) | sing-box 规则集集合 |

### nftables 规则参考

`nftables/sing-box-tproxy.nft` 包含完整的 tproxy 透明网关 nftables 规则，需替换的变量：

| 变量 | 说明 | 示例 |
|------|------|------|
| `LAN_SUBNET` | 内网网段 | `192.168.1.0/24` |
| `TPROXY_PORT` | tproxy 监听端口 | `7893` |
| `LAN_GATEWAY` | 旁路由自身 IP | `192.168.1.6` |

---

## 旁路由网络配置

### 方式一：主路由 DHCP 下发（推荐）

在**主路由**的 DHCP 设置中：
- 网关地址 → 旁路由 IP（如 `192.168.1.6`）
- DNS 服务器 → 旁路由 IP

这样局域网所有设备自动走旁路由，无需逐台设置。

### 方式二：设备手动指定

在需要代理的设备上手动设置：
- 网关 → 旁路由 IP
- DNS → 旁路由 IP

### 网络拓扑

```
互联网
  │
  ▼
┌──────────┐
│  主路由    │ 192.168.1.1 (DHCP 网关→.6, DNS→.6)
│ (光猫)    │
└────┬─────┘
     │
     ▼
┌──────────────┐
│  旁路由       │ 192.168.1.6
│ (Alpine+SB)  │ tproxy:7893 / panel:9999
└──────────────┘
     ▲
     │ DHCP 下发网关 .6
┌────┴─────┐
│ 局域网设备 │ 手机/电脑/电视 → 自动透明代理
└──────────┘
```

---

## 目录结构

```
sing-box-gateway/
├── sing-box-gateway-deploy.sh     # 主部署脚本（入口）
├── panel/
│   └── app.py                     # Flask 管理面板（含订阅解析器）
├── templates/
│   ├── config-tproxy.json         # tproxy 透明网关模板
│   ├── config-tun.json            # TUN 自动路由模板
│   └── config-mixed.json          # Mixed 代理模板
├── nftables/
│   └── sing-box-tproxy.nft        # tproxy nftables 规则
├── openrc/
│   ├── sing-box.initd             # sing-box OpenRC 服务
│   ├── sing-box.confd             # sing-box 配置
│   ├── singbox-panel.initd        # 面板 OpenRC 服务
│   └── singbox-panel.confd         # 面板配置
└── README.md                      # 本文档

安装后系统路径:
/etc/sing-box/
├── config.json                    # 当前生效的配置
├── config.json.bak                # 配置备份
├── subscriptions.json             # 订阅列表
├── panel.json                     # 面板配置
├── templates/                     # 模板文件
├── cache.db                       # sing-box 缓存
└── .version                       # 已安装版本

/opt/singbox-gateway/
├── sing-box-gateway-deploy.sh     # 部署脚本副本 (sb 快捷命令指向此文件)
├── templates/                     # 模板文件副本
└── panel/
    ├── app.py                     # 面板代码
    └── venv/                      # Python 虚拟环境

/usr/local/bin/
└── sb                             # 快捷命令 (→ exec bash /opt/singbox-gateway/sing-box-gateway-deploy.sh)

/etc/init.d/
├── sing-box                       # 核心服务
├── singbox-panel                  # 面板服务
└── nftables-sing-box              # nftables 规则服务
```

---

## 常见问题

### Q: tproxy 模式下设备不能上网？

1. 确认旁路由 IP 转发已开启: `cat /proc/sys/net/ipv4/ip_forward` 应为 `1`
2. 确认 nftables 规则已加载: `nft list ruleset | grep sing-box`
3. 确认路由表存在: `ip route show table 100` 应有 `local default dev lo`
4. 确认 `ip rule` 有 `fwmark 1` 规则
5. 在面板「网络」页重新应用 tproxy 规则

### Q: TUN 模式下设备不能上网？

TUN 模式依赖 `auto_route`，确保：
1. sing-box 以 root 运行
2. 内核支持 TUN（`/dev/net/tun` 存在）
3. 主路由 DHCP 网关已指向旁路由

### Q: 订阅更新后节点不生效？

1. 检查面板「日志」页是否有报错
2. 确认订阅 URL 可访问（在面板「转换」页测试）
3. 手动重启: `rc-service sing-box restart`

### Q: 核心更新失败？

1. 检查网络: `curl -sL https://api.github.com/repos/SagerNet/sing-box/releases/latest | jq .tag_name`
2. 手动下载: 参考脚本中的 URL 格式
3. 架构不匹配: 运行 `uname -m` 确认

### Q: 面板打不开？

```bash
# 检查面板状态
rc-service singbox-panel status
# 查看面板日志
cat /var/log/sing-box/panel-stderr.log
# 手动启动测试
/opt/singbox-gateway/panel/venv/bin/python /opt/singbox-gateway/panel/app.py --port 9999
```

### Q: 如何修改面板端口？

编辑 `/etc/conf.d/singbox-panel`，修改 `panel_port`，然后:
```bash
rc-service singbox-panel restart
```

### Q: 如何使用 Clash 面板切换节点？

所有模板已开启 Clash API (`:9090`)，并设置了后端密钥 `singbox-gateway`。访问:
- [yacd 面板](https://yacd.haishan.me) → 填入 `http://<IP>:9090`，密钥 `singbox-gateway`
- [metacubexd](https://d.metacubex.one) → 填入 `http://<IP>:9090`，密钥 `singbox-gateway`

> 也可直接用内置仪表盘：`http://<旁路由IP>:9999/ui/`，首次打开同样填后端地址 + 密钥。

---

## 依赖清单

脚本会自动安装以下 Alpine 包：

| 包 | 用途 |
|----|------|
| `nftables` | 透明网关防火墙规则 |
| `iproute2` | 路由表/策略路由配置 |
| `iptables` | tproxy 内核模块 |
| `python3` | 管理面板运行时 |
| `py3-pip` | 安装 Flask 等包 |
| `py3-virtualenv` | Python 虚拟环境 |
| `jq` | JSON 处理 |
| `curl` / `wget` | 下载核心和订阅 |
| `openrc` | 服务管理 |
| `procps` | 进程管理 |

Python 面板依赖（自动安装到 venv）：

| 包 | 用途 |
|----|------|
| `flask` | Web 面板后端 |
| `requests` | HTTP 请求（拉取订阅） |
| `pyyaml` | Clash YAML 解析 |
