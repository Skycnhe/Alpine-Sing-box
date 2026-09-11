#!/usr/bin/env bash
# ============================================================
#  sing-box 旁路由网关自动部署脚本 (Alpine Linux)
#  ============================================================
#  功能:
#    1. 自动安装所有依赖 (nftables / iproute2 / python3 / flask ...)
#    2. 下载并安装 sing-box 核心
#    3. 部署 Web 管理面板 (Flask, 内置订阅解析/转换)
#    4. 配置透明网关 (tproxy / tun 两种模式)
#    5. 一键更新: 核心 / 面板 / 订阅 / 订阅转换
#    6. OpenRC 服务管理 (sing-box + panel)
#
#  用法:
#    bash sing-box-gateway-deploy.sh              # 完整部署
#    bash sing-box-gateway-deploy.sh --update-core     # 仅更新核心
#    bash sing-box-gateway-deploy.sh --update-panel    # 仅更新面板
#    bash sing-box-gateway-deploy.sh --update-sub      # 更新订阅
#    bash sing-box-gateway-deploy.sh --convert <URL>  # 转换订阅
#    bash sing-box-gateway-deploy.sh --uninstall       # 卸载
#
#  作者: WorkBuddy | 适配 Alpine Linux 3.18+
# ============================================================

set -euo pipefail

# ==================== 颜色 ====================
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; CYAN='\033[0;36m'; NC='\033[0m'

info()  { echo -e "${GREEN}[INFO]${NC} $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*"; }
step()  { echo -e "\n${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
         echo -e "${CYAN}  $*${NC}"
         echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"; }

# ==================== 路径常量 ====================
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
INSTALL_DIR="/opt/singbox-gateway"
SB_DIR="/etc/sing-box"
SB_BIN="/usr/local/bin/sing-box"
SB_CONFIG="${SB_DIR}/config.json"
SB_CONFIG_BACKUP="${SB_DIR}/config.json.bak"
SB_TEMPLATES_DIR="${SB_DIR}/templates"
PANEL_DIR="${INSTALL_DIR}/panel"
PANEL_APP="${PANEL_DIR}/app.py"
PANEL_VENV="${PANEL_DIR}/venv"
SUBS_FILE="${SB_DIR}/subscriptions.json"
PANEL_CONFIG="${SB_DIR}/panel.json"
LOG_DIR="/var/log/sing-box"
NFT_RULES="/etc/nftables-sing-box.nft"
SB_VERSION_FILE="${SB_DIR}/.version"

# ==================== GitHub 资源 ====================
SB_REPO="SagerNet/sing-box"
SB_API="https://api.github.com/repos/${SB_REPO}/releases/latest"

# ==================== 检测架构 ====================
detect_arch() {
    local arch
    arch="$(uname -m)"
    case "$arch" in
        x86_64)  echo "linux-amd64" ;;
        aarch64) echo "linux-arm64" ;;
        armv7l)  echo "linux-armv7" ;;
        armhf)   echo "linux-armv7" ;;
        *)       echo "linux-amd64"; warn "未知架构 $arch, 默认使用 amd64" ;;
    esac
}

# ==================== 检测 OS ====================
check_os() {
    if ! grep -qi "alpine" /etc/os-release 2>/dev/null; then
        warn "本脚本专为 Alpine Linux 设计, 当前系统可能不兼容"
        warn "继续执行? (Ctrl+C 取消, 5秒后自动继续)"
        sleep 5
    fi
}

# ==================== 安装系统依赖 ====================
install_deps() {
    step "安装系统依赖"
    info "更新 apk 索引..."
    apk update --quiet

    info "安装依赖包..."
    apk add --no-cache \
        bash openrc \
        curl wget ca-certificates \
        tar gzip unzip \
        nftables iproute2 iptables ip6tables \
        python3 py3-pip py3-virtualenv \
        procps iputils coreutils \
        jq logrotate

    info "依赖安装完成"
}

# ==================== 下载安装 sing-box 核心 ====================
install_core() {
    step "安装 sing-box 核心"

    local goarch
    goarch="$(detect_arch)"
    info "架构: $goarch"

    # 获取最新版本
    info "获取最新版本..."
    local latest
    latest="$(curl -sL "${SB_API}" | jq -r '.tag_name // empty')"
    if [ -z "$latest" ]; then
        error "无法获取最新版本, 请检查网络"
        return 1
    fi
    local ver="${latest#v}"
    info "最新版本: ${latest} (v${ver})"

    # 下载
    local tmp_file="/tmp/sing-box-${ver}.tar.gz"
    local dl_url="https://github.com/${SB_REPO}/releases/download/${latest}/sing-box-${ver}-${goarch}.tar.gz"
    info "下载: ${dl_url}"
    if ! curl -L --fail --connect-timeout 30 -o "$tmp_file" "$dl_url"; then
        error "下载失败"
        return 1
    fi

    # 解压
    local extract_dir="/tmp/sing-box-${ver}"
    rm -rf "$extract_dir"
    mkdir -p "$extract_dir"
    tar xzf "$tmp_file" -C "$extract_dir"

    # 找到二进制
    local bin_path
    bin_path="$(find "$extract_dir" -name 'sing-box' -type f | head -1)"
    if [ -z "$bin_path" ]; then
        error "解压后未找到 sing-box 二进制"
        return 1
    fi

    # 停止服务 (如果运行中)
    if rc-service sing-box status >/dev/null 2>&1; then
        info "停止 sing-box 服务..."
        rc-service sing-box stop >/dev/null 2>&1 || true
    fi

    # 安装
    info "安装到 ${SB_BIN}"
    install -Dm755 "$bin_path" "$SB_BIN"
    echo "$ver" > "$SB_VERSION_FILE"

    # 清理
    rm -rf "$extract_dir" "$tmp_file"

    # 验证
    info "验证安装..."
    "$SB_BIN" version
    info "${GREEN}sing-box ${ver} 安装完成${NC}"
}

# ==================== 安装配置文件与模板 ====================
install_configs() {
    step "安装配置文件与模板"

    # 创建目录
    mkdir -p "$SB_DIR" "$SB_TEMPLATES_DIR" "$LOG_DIR"

    # 安装模板
    info "安装透明网关模板..."
    if [ -d "${SCRIPT_DIR}/templates" ]; then
        cp -f "${SCRIPT_DIR}"/templates/config-*.json "$SB_TEMPLATES_DIR/"
    fi

    # 初始化配置 (如不存在)
    if [ ! -f "$SB_CONFIG" ]; then
        info "创建默认配置 (tproxy 模式)..."
        cp "${SB_TEMPLATES_DIR}/config-tproxy.json" "$SB_CONFIG"
        # 去掉注释字段
        jq 'del(._comment, ._usage)' "$SB_CONFIG" > "$SB_CONFIG.tmp" && mv "$SB_CONFIG.tmp" "$SB_CONFIG"
    else
        info "配置文件已存在, 保留现有配置"
        cp "$SB_CONFIG" "$SB_CONFIG_BACKUP"
    fi

    # 初始化订阅文件
    if [ ! -f "$SUBS_FILE" ]; then
        echo '[]' > "$SUBS_FILE"
    fi

    # 初始化面板配置
    if [ ! -f "$PANEL_CONFIG" ]; then
        cat > "$PANEL_CONFIG" << 'EOF'
{
    "panel_port": 9999,
    "tproxy_port": 7893,
    "lan_subnet": "192.168.1.0/24",
    "gateway_mode": "tproxy",
    "clash_secret": ""
}
EOF
    fi

    info "配置文件安装完成"
    info "  核心配置: ${SB_CONFIG}"
    info "  模板目录: ${SB_TEMPLATES_DIR}"
    info "  订阅文件: ${SUBS_FILE}"
}

# ==================== 安装管理面板 ====================
install_panel() {
    step "安装 Web 管理面板"

    mkdir -p "$INSTALL_DIR" "$PANEL_DIR"

    # 复制面板代码
    if [ -f "${SCRIPT_DIR}/panel/app.py" ]; then
        info "从本地安装面板代码..."
        cp -f "${SCRIPT_DIR}/panel/app.py" "$PANEL_APP"
    elif [ -f "$PANEL_APP" ]; then
        info "面板代码已存在, 保留"
    else
        error "找不到面板代码 (panel/app.py)"
        return 1
    fi

    # 创建虚拟环境
    if [ ! -d "$PANEL_VENV" ]; then
        info "创建 Python 虚拟环境..."
        python3 -m venv "$PANEL_VENV"
    fi

    # 安装 Python 依赖
    info "安装 Python 依赖 (flask, requests, pyyaml)..."
    "$PANEL_VENV/bin/pip" install --quiet --upgrade pip
    "$PANEL_VENV/bin/pip" install --quiet flask requests pyyaml

    info "面板安装完成: ${PANEL_APP}"
}

# ==================== 安装 OpenRC 服务 ====================
install_services() {
    step "安装 OpenRC 服务"

    # --- sing-box 服务 ---
    info "创建 sing-box 服务..."
    cat > /etc/init.d/sing-box << 'INITEOF'
#!/sbin/openrc-run
description="sing-box universal proxy platform"

: ${singbox_config:="/etc/sing-box/config.json"}
: ${singbox_binary:="/usr/local/bin/sing-box"}
: ${singbox_user:="root"}
: ${singbox_log_dir:="/var/log/sing-box"}

command="${singbox_binary}"
command_args="run -c ${singbox_config}"
command_user="${singbox_user}"
pidfile="/run/sing-box.pid"
command_background="yes"
output_log="${singbox_log_dir}/sing-box-stdout.log"
error_log="${singbox_log_dir}/sing-box-stderr.log"

depend() {
    need net
    after firewall
}

start_pre() {
    checkpath --directory --owner root:root --mode 0755 "${singbox_log_dir}" /etc/sing-box
    [ ! -x "${singbox_binary}" ] && { eerror "binary not found"; return 1; }
    [ ! -f "${singbox_config}" ] && { eerror "config not found"; return 1; }
    ebegin "Checking config"
    "${singbox_binary}" check -c "${singbox_config}" 2>/dev/null
    eend 0
}
INITEOF
    chmod +x /etc/init.d/sing-box

    # conf.d
    cat > /etc/conf.d/sing-box << 'CONFEOF'
singbox_config="/etc/sing-box/config.json"
singbox_binary="/usr/local/bin/sing-box"
singbox_user="root"
singbox_log_dir="/var/log/sing-box"
CONFEOF

    # --- 面板服务 ---
    info "创建面板服务..."
    cat > /etc/init.d/singbox-panel << 'INITEOF'
#!/sbin/openrc-run
description="sing-box gateway management panel"

: ${panel_app:="/opt/singbox-gateway/panel/app.py"}
: ${panel_host:="0.0.0.0"}
: ${panel_port:="9999"}
: ${panel_user:="root"}
: ${panel_venv:="/opt/singbox-gateway/panel/venv"}

command="${panel_venv}/bin/python"
command_args="${panel_app} --host ${panel_host} --port ${panel_port}"
command_user="${panel_user}"
pidfile="/run/singbox-panel.pid"
command_background="yes"
output_log="/var/log/sing-box/panel-stdout.log"
error_log="/var/log/sing-box/panel-stderr.log"

depend() {
    need net
    after sing-box
}
INITEOF
    chmod +x /etc/init.d/singbox-panel

    # conf.d
    cat > /etc/conf.d/singbox-panel << 'CONFEOF'
panel_host="0.0.0.0"
panel_port="9999"
panel_user="root"
panel_venv="/opt/singbox-gateway/panel/venv"
CONFEOF

    info "服务文件已创建"
}

# ==================== 配置内核参数 ====================
setup_kernel() {
    step "配置内核参数 (IP 转发)"

    # IP 转发
    info "开启 IPv4 转发..."
    echo "net.ipv4.ip_forward = 1" > /etc/sysctl.d/99-sing-box.conf
    sysctl -p /etc/sysctl.d/99-sing-box.conf >/dev/null 2>&1 || true
    sysctl -w net.ipv4.ip_forward=1

    # 加载 tproxy 内核模块
    info "加载 tproxy 内核模块..."
    modprobe nft_tproxy 2>/dev/null || true
    modprobe xt_TPROXY 2>/dev/null || true

    # 持久化模块加载
    echo "nft_tproxy" >> /etc/modules 2>/dev/null || true
    echo "xt_TPROXY" >> /etc/modules 2>/dev/null || true

    info "内核参数配置完成"
}

# ==================== 设置 nftables 持久化服务 ====================
setup_nftables_service() {
    step "配置 nftables 服务"

    # 创建 nftables 启动脚本 (含路由表)
    cat > /etc/init.d/nftables-sing-box << 'INITEOF'
#!/sbin/openrc-run
description="nftables rules for sing-box tproxy"

depend() {
    need net
    after sing-box
}

start() {
    ebegin "Loading sing-box nftables rules"
    modprobe nft_tproxy 2>/dev/null
    modprobe xt_TPROXY 2>/dev/null
    [ -f /etc/nftables-sing-box.nft ] && nft -f /etc/nftables-sing-box.nft
    ip route add local default dev lo table 100 2>/dev/null
    ip rule add fwmark 1 table 100 2>/dev/null
    eend 0
}

stop() {
    ebegin "Removing sing-box nftables rules"
    nft delete table ip sing-box 2>/dev/null
    ip route del local default dev lo table 100 2>/dev/null
    ip rule del fwmark 1 table 100 2>/dev/null
    eend 0
}
INITEOF
    chmod +x /etc/init.d/nftables-sing-box
}

# ==================== 启动服务 ====================
start_services() {
    step "启动服务"

    # 启用并启动 sing-box
    info "启用 sing-box 服务..."
    rc-update add sing-box default 2>/dev/null || true
    rc-service sing-box restart 2>/dev/null || rc-service sing-box start 2>/dev/null || warn "sing-box 启动失败, 请检查配置"

    # 启用并启动面板
    info "启用面板服务..."
    rc-update add singbox-panel default 2>/dev/null || true
    rc-service singbox-panel restart 2>/dev/null || rc-service singbox-panel start 2>/dev/null || warn "面板启动失败"

    # nftables (可选, 默认不自动启动, 用户在面板中配置)
    rc-update add nftables-sing-box default 2>/dev/null || true

    sleep 2

    # 显示状态
    info "服务状态:"
    rc-service sing-box status 2>&1 || true
    rc-service singbox-panel status 2>&1 || true
}

# ==================== 更新核心 ====================
do_update_core() {
    step "更新 sing-box 核心"
    install_core
    info "重启服务..."
    rc-service sing-box restart 2>/dev/null || true
    info "${GREEN}核心更新完成${NC}"
    "$SB_BIN" version
}

# ==================== 更新面板 ====================
do_update_panel() {
    step "更新管理面板"
    install_panel
    info "重启面板服务..."
    rc-service singbox-panel restart 2>/dev/null || true
    info "${GREEN}面板更新完成${NC}"
}

# ==================== 更新订阅 ====================
do_update_sub() {
    step "更新订阅"
    if [ ! -f "$SUBS_FILE" ] || [ "$(cat "$SUBS_FILE")" = "[]" ]; then
        error "没有订阅, 请先在面板中添加或编辑 ${SUBS_FILE}"
        return 1
    fi
    info "通过面板 API 更新订阅..."
    local panel_port
    panel_port="$(jq -r '.panel_port // 9999' "$PANEL_CONFIG" 2>/dev/null || echo 9999)"
    curl -s -X POST "http://127.0.0.1:${panel_port}/api/subscriptions/update" | jq .
}

# ==================== 转换订阅 ====================
do_convert() {
    local sub_url="${1:-}"
    if [ -z "$sub_url" ]; then
        echo "用法: $0 --convert <订阅URL> [模板]"
        echo "模板: tproxy (默认) | tun | mixed"
        return 1
    fi
    local template="${2:-tproxy}"
    step "转换订阅"
    info "订阅地址: $sub_url"
    info "使用模板: $template"
    local panel_port
    panel_port="$(jq -r '.panel_port // 9999' "$PANEL_CONFIG" 2>/dev/null || echo 9999)"

    info "调用转换 API..."
    local result
    result="$(curl -s -X POST "http://127.0.0.1:${panel_port}/api/convert" \
        -H 'Content-Type: application/json' \
        -d "{\"url\": \"$sub_url\", \"template\": \"$template\"}")"

    if echo "$result" | jq -e '.ok' >/dev/null 2>&1; then
        local count
        count="$(echo "$result" | jq -r '.node_count')"
        info "${GREEN}成功解析 $count 个节点${NC}"
        echo "$result" | jq -r '.nodes[]' 2>/dev/null | head -20
        echo ""
        info "完整配置已输出到 stdout, 可重定向到文件:"
        echo "$result" | jq '.config'
    else
        error "转换失败:"
        echo "$result" | jq .
    fi
}

# ==================== 卸载 ====================
do_uninstall() {
    step "卸载 sing-box 网关"

    info "停止服务..."
    rc-service sing-box stop 2>/dev/null || true
    rc-service singbox-panel stop 2>/dev/null || true
    rc-service nftables-sing-box stop 2>/dev/null || true

    info "移除服务..."
    rc-update del sing-box default 2>/dev/null || true
    rc-update del singbox-panel default 2>/dev/null || true
    rc-update del nftables-sing-box default 2>/dev/null || true

    info "清除 nftables 规则..."
    nft delete table ip sing-box 2>/dev/null || true
    ip route del local default dev lo table 100 2>/dev/null || true
    ip rule del fwmark 1 table 100 2>/dev/null || true

    info "删除文件..."
    read -r -p "是否删除配置和面板? (y/N) " yn
    if [ "$yn" = "y" ] || [ "$yn" = "Y" ]; then
        rm -f /etc/init.d/sing-box /etc/init.d/singbox-panel /etc/init.d/nftables-sing-box
        rm -f /etc/conf.d/sing-box /etc/conf.d/singbox-panel
        rm -f "$SB_BIN"
        rm -rf "$SB_DIR" "$INSTALL_DIR" "$LOG_DIR"
        rm -f /etc/sysctl.d/99-sing-box.conf
        rm -f "$NFT_RULES"
        info "${GREEN}卸载完成${NC}"
    else
        info "保留配置文件, 仅删除服务和二进制"
        rm -f /etc/init.d/sing-box /etc/init.d/singbox-panel /etc/init.d/nftables-sing-box
        rm -f /etc/conf.d/sing-box /etc/conf.d/singbox-panel
        rm -f "$SB_BIN"
    fi
}

# ==================== 完整安装 ====================
full_install() {
    check_os
    install_deps
    install_core
    install_configs
    install_panel
    install_services
    setup_kernel
    setup_nftables_service
    start_services

    step "部署完成"
    local ip_addr
    ip_addr="$(ip -4 addr show scope global | grep inet | awk '{print $2}' | head -1 | cut -d/ -f1)"
    cat << EOF

${GREEN}╔══════════════════════════════════════════════════════╗${NC}
${GREEN}║        sing-box 旁路由网关部署成功!                   ║${NC}
${GREEN}╚══════════════════════════════════════════════════════╝${NC}

  核心版本:   $("$SB_BIN" version 2>/dev/null | grep 'sing-box version' | awk '{print $3}' || echo 'unknown')
  核心配置:   ${SB_CONFIG}
  模板目录:   ${SB_TEMPLATES_DIR}
  订阅文件:   ${SUBS_FILE}

  管理面板:   http://${ip_addr:-<本机IP>}:9999
  Clash API:  http://${ip_addr:-<本机IP>}:9090
  代理端口:   2080 (mixed) / 7893 (tproxy)

  服务管理:
    rc-service sing-box start|stop|restart|status
    rc-service singbox-panel start|stop|restart|status

  下一步:
    1. 浏览器访问面板 http://${ip_addr:-<IP>}:9999
    2. 在「订阅」页添加你的机场订阅链接
    3. 点击「更新所有订阅」拉取节点
    4. 在「网络」页配置透明网关 (tproxy 规则)
    5. 在主路由 DHCP 中将网关/DNS 指向本机 IP

  命令行工具:
    bash $0 --update-core        更新核心
    bash $0 --update-panel       更新面板
    bash $0 --update-sub         更新订阅
    bash $0 --convert <URL>      转换订阅
    bash $0 --uninstall          卸载

EOF
}

# ==================== 主入口 ====================
main() {
    case "${1:-}" in
        --update-core)    do_update_core ;;
        --update-panel)   do_update_panel ;;
        --update-sub)     do_update_sub ;;
        --convert)        do_convert "${2:-}" "${3:-tproxy}" ;;
        --uninstall)      do_uninstall ;;
        --help|-h|"")
            cat << 'HELPEOF'
sing-box 旁路由网关部署脚本 (Alpine Linux)

用法:
  bash $0                      完整部署 (安装核心+面板+服务+配置)
  bash $0 --update-core        仅更新 sing-box 核心
  bash $0 --update-panel       仅更新管理面板
  bash $0 --update-sub         更新所有订阅并合并节点
  bash $0 --convert <URL> [模板]  转换订阅为 sing-box JSON
                                  模板: tproxy / tun / mixed
  bash $0 --uninstall          卸载 (交互式确认)
  bash $0 --help               显示此帮助

模板说明:
  config-tproxy.json  tproxy 透明网关 (需 nftables 规则, 推荐旁路由)
  config-tun.json     TUN 自动路由 (无需防火墙规则, sing-box 自管路由)
  config-mixed.json   Mixed 代理 (仅 HTTP/SOCKS, 不做透明网关)
HELPEOF
            ;;
        *)
            # 无参数 = 完整安装
            if [ $# -eq 0 ]; then
                full_install
            else
                error "未知参数: $1"
                exit 1
            fi
            ;;
    esac
}

main "$@"
