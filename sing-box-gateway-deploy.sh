#!/usr/bin/env bash
# ============================================================
#  sing-box 旁路由网关自动部署脚本 (Alpine Linux)
#  ============================================================
#  功能:
#    1. 自动安装所有依赖 (nftables / iproute2 / python3 / flask ...)
#    2. 自动识别架构 (12+) + 按架构匹配下载 sing-box 核心
#    3. 自动下载/更新管理面板 (优先 GitHub 仓库, 回退本地)
#    4. 多仪表盘管理 (metacubexd / zashboard / yacd 本地托管)
#    5. 配置透明网关 (tproxy / tun 两种模式)
#    6. 一键更新: 核心 / 面板 / 订阅 / 订阅转换(含直接应用)
#    7. OpenRC 服务管理 (sing-box + panel)
#
#  用法:
#    sb                            # 交互式菜单 (快捷命令, 安装后可用)
#    bash sing-box-gateway-deploy.sh              # 交互式菜单 (默认)
#    bash sing-box-gateway-deploy.sh --install    # 完整部署 (非交互)
#    bash sing-box-gateway-deploy.sh --update-core     # 仅更新核心
#    bash sing-box-gateway-deploy.sh --update-panel    # 仅更新面板
#    bash sing-box-gateway-deploy.sh --update-dashboard <name>  # 安装/更新仪表盘
#    bash sing-box-gateway-deploy.sh --update-sub      # 更新订阅
#    bash sing-box-gateway-deploy.sh --convert <URL>  # 转换订阅(预览)
#    bash sing-box-gateway-deploy.sh --convert-apply <URL>  # 转换并应用
#    bash sing-box-gateway-deploy.sh --install-shortcut  # 安装 sb 快捷命令
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
# 面板代码来源 (改成你自己的仓库; 为空则只用本地 panel/app.py)
PANEL_REPO="Skycnhe/Alpine-Sing-box"
PANEL_BRANCH="Hk001"

# ==================== 检测架构 ====================
# 输出 goarch: amd64/arm64/armv7/armv6/armv5/386/mipsle/mips/mips64/s390x/riscv64/ppc64le
detect_arch() {
    local arch
    arch="$(uname -m)"
    case "$arch" in
        x86_64|amd64)          echo "amd64" ;;
        aarch64|arm64)        echo "arm64" ;;
        armv7l|armv7)         echo "armv7" ;;
        armv6l|armv6)         echo "armv6" ;;
        armv5tel|armv5)       echo "armv5" ;;
        armhf)                echo "armv7" ;;
        i386|i686)            echo "386" ;;
        mipsle|mipsel)        echo "mipsle" ;;
        mips)                echo "mips" ;;
        mips64|mips64el)      echo "mips64" ;;
        s390x)               echo "s390x" ;;
        riscv64)             echo "riscv64" ;;
        ppc64le|powerpc64le)  echo "ppc64le" ;;
        *) echo "amd64"; warn "未知架构 $arch, 默认使用 amd64" ;;
    esac
}

# ==================== 检测 libc (musl/glibc) ====================
# Alpine 用 musl; 选包时必须匹配对应 libc 变体, 否则二进制无法运行
detect_libc() {
    if ldd --version 2>&1 | head -1 | grep -qi musl; then
        echo "musl"
    elif ldd --version 2>&1 | head -1 | grep -qi 'glibc\|GNU\|2\.[0-9]'; then
        echo "glibc"
    elif grep -qi alpine /etc/os-release 2>/dev/null; then
        echo "musl"
    else
        echo "glibc"
    fi
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

# ==================== 获取最新发布信息 ====================
# 从 GitHub API 取最新 release; stdout 第一行 tag_name, 其余每行 "name<TAB>url"
get_latest_release() {
    local resp
    resp="$(curl -sL --connect-timeout 30 --max-time 60 "${SB_API}")"
    local tag
    tag="$(echo "$resp" | jq -r '.tag_name // empty' 2>/dev/null)"
    [ -z "$tag" ] && return 1
    echo "$tag"
    echo "$resp" | jq -r '.assets[] | .name + "\t" + .browser_download_url' 2>/dev/null
}

# ==================== 按架构+libc 选择核心下载资源 ====================
# $1 = goarch, $2 = libc(musl/glibc); 从 stdin 读取 "name<TAB>url" 列表, stdout 输出匹配 url
select_core_asset() {
    local goarch="$1" libc="${2:-}"
    local lines=() name url line
    # 一次性读入 stdin 到数组 (避免多个 while 循环互相消耗 stdin)
    while IFS=$'\t' read -r name url; do
        [ -n "$name" ] && lines+=("${name}"$'\t'"${url}")
    done
    # 1) 优先匹配对应 libc 变体 (musl/glibc): linux-${goarch}-${libc}
    if [ -n "$libc" ]; then
        for line in "${lines[@]}"; do
            name="${line%%$'\t'*}"; url="${line#*$'\t'}"
            echo "$name" | grep -qiE "linux[-_]${goarch}[-_]${libc}([.\\-]|$)" && { echo "$url"; return 0; }
        done
    fi
    # 2) 裸变体 (无 -glibc/-musl 后缀): linux-${goarch}.tar.gz
    for line in "${lines[@]}"; do
        name="${line%%$'\t'*}"; url="${line#*$'\t'}"
        echo "$name" | grep -qiE "linux[-_]${goarch}\." && { echo "$url"; return 0; }
    done
    # 3) 模糊兜底: 任意 linux-${goarch} 变体 (可能 libc 不符, 仅最后手段)
    for line in "${lines[@]}"; do
        name="${line%%$'\t'*}"; url="${line#*$'\t'}"
        echo "$name" | grep -qiE "linux[-_]${goarch}([.\\-]|$)" && { echo "$url"; return 0; }
    done
    return 1
}

# ==================== 下载安装 sing-box 核心 ====================
install_core() {
    step "安装 sing-box 核心"

    # 确保目录存在 (install_core 可能在 install_configs 之前独立调用)
    mkdir -p "$SB_DIR" "$LOG_DIR"

    local goarch libc
    goarch="$(detect_arch)"
    libc="$(detect_libc)"
    info "检测架构: ${goarch} | libc: ${libc} (关键词 linux-${goarch}-${libc})"

    # 获取最新发布
    info "获取最新版本..."
    local release_out tag assets
    if ! release_out="$(get_latest_release)"; then
        error "无法获取最新版本, 请检查网络或 GitHub API 限流"
        return 1
    fi
    tag="$(echo "$release_out" | head -1)"
    assets="$(echo "$release_out" | tail -n +2)"
    local ver="${tag#v}"
    info "最新版本: ${tag} (v${ver})"

    # 按架构+libc 匹配资源 URL
    local dl_url
    dl_url="$(echo "$assets" | select_core_asset "$goarch" "$libc")"
    if [ -z "$dl_url" ]; then
        warn "未找到匹配 ${goarch}/${libc} 的资源, 可用资源:"
        echo "$assets" | while IFS=$'\t' read -r name _; do [ -n "$name" ] && echo "    $name"; done
        error "无匹配预编译核心"
        return 1
    fi
    local dl_name; dl_name="$(basename "$dl_url")"
    info "匹配资源: ${dl_name} (${libc}版)"

    # 下载
    local tmp_file="/tmp/sing-box-${ver}.tar.gz"
    info "下载: ${dl_url}"
    if ! curl -L --fail --connect-timeout 30 --max-time 300 -o "$tmp_file" "$dl_url"; then
        error "下载失败"
        return 1
    fi

    # 解压
    local extract_dir="/tmp/sing-box-${ver}"
    rm -rf "$extract_dir"; mkdir -p "$extract_dir"
    tar xzf "$tmp_file" -C "$extract_dir" 2>/dev/null \
        || gzip -dc "$tmp_file" | tar x -C "$extract_dir"

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

    # 初始化配置
    local need_regen=false
    if [ ! -f "$SB_CONFIG" ]; then
        info "创建默认配置 (tproxy 模式)..."
        need_regen=true
    else
        # 检测旧格式: legacy DNS (address 字段), _comment 字段, DNS rcode 规则
        if grep -qE '"_comment"|"_usage"|"address".*dns-query|"address".*rcode|"rcode".*"(REFUSED|SUCCESS|success|refused)"|"independent_cache"' "$SB_CONFIG" 2>/dev/null; then
            warn "检测到旧格式配置 (legacy DNS / 注释字段 / rcode 规则), 自动迁移到新格式..."
            cp "$SB_CONFIG" "${SB_CONFIG_BACKUP}.old-format"
            need_regen=true
        else
            info "配置文件已存在, 保留现有配置"
            cp "$SB_CONFIG" "$SB_CONFIG_BACKUP"
        fi
    fi

    if [ "$need_regen" = true ]; then
        cp "${SB_TEMPLATES_DIR}/config-tproxy.json" "$SB_CONFIG"
        # 去掉所有 _ 开头的注释字段 (模板已无注释, 此为安全兜底)
        if command -v jq >/dev/null 2>&1; then
            jq 'with_entries(select(.key[0:1] != "_"))' "$SB_CONFIG" > "$SB_CONFIG.tmp" 2>/dev/null && mv "$SB_CONFIG.tmp" "$SB_CONFIG" || true
        elif command -v python3 >/dev/null 2>&1; then
            python3 -c "
import json
with open('$SB_CONFIG') as f: d=json.load(f)
d={k:v for k,v in d.items() if not k.startswith('_')}
with open('$SB_CONFIG','w') as f: json.dump(d,f,indent=2,ensure_ascii=False)
" 2>/dev/null || true
        fi
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

# ==================== 修复/迁移配置 ====================
fix_config() {
    step "检测并修复 sing-box 配置"

    mkdir -p "$SB_DIR" "$SB_TEMPLATES_DIR"

    # 确保模板存在
    if [ ! -f "${SB_TEMPLATES_DIR}/config-tproxy.json" ] && [ -d "${SCRIPT_DIR}/templates" ]; then
        info "安装模板文件..."
        cp -f "${SCRIPT_DIR}"/templates/config-*.json "$SB_TEMPLATES_DIR/"
    fi

    if [ ! -f "$SB_CONFIG" ]; then
        warn "config.json 不存在, 从 tproxy 模板创建..."
        cp "${SB_TEMPLATES_DIR}/config-tproxy.json" "$SB_CONFIG"
    fi

    local need_fix=false

    # 检测问题: _comment/_usage 字段, legacy DNS (address + dns-query/rcode), DNS rcode 规则
    if grep -qE '"_(comment|usage)"' "$SB_CONFIG" 2>/dev/null; then
        warn "检测到注释字段 (_comment/_usage) — sing-box 会拒绝未知字段"
        need_fix=true
    fi
    if grep -qE '"address"\s*:\s*"(https?://|rcode://)' "$SB_CONFIG" 2>/dev/null; then
        warn "检测到 legacy DNS 格式 (address + dns-query/rcode) — sing-box 1.14.0 已移除"
        need_fix=true
    fi
    if grep -qE '"rcode"\s*:\s*"(REFUSED|SUCCESS|success|refused)"' "$SB_CONFIG" 2>/dev/null; then
        warn "检测到 DNS 规则中的 rcode 字段 — 已改为 action: reject"
        need_fix=true
    fi
    if grep -q '"independent_cache"' "$SB_CONFIG" 2>/dev/null; then
        warn "检测到 independent_cache — sing-box 1.14.0 已移除"
        need_fix=true
    fi
    if grep -qE '"type"\s*:\s*"block"|"type"\s*:\s*"dns"' "$SB_CONFIG" 2>/dev/null; then
        warn "检测到 block/dns outbound — sing-box 1.14.0 已改为 route action"
        need_fix=true
    fi

    if [ "$need_fix" = false ]; then
        info "${GREEN}配置格式正常, 无需修复${NC}"
        # 仍然清理可能的 _ 字段 (安全兜底)
        if command -v jq >/dev/null 2>&1; then
            jq 'with_entries(select(.key[0:1] != "_"))' "$SB_CONFIG" > "$SB_CONFIG.tmp" 2>/dev/null && mv "$SB_CONFIG.tmp" "$SB_CONFIG"
        fi
        return 0
    fi

    # 备份旧配置
    local bak="${SB_CONFIG}.fix-bak.$(date +%s)"
    cp "$SB_CONFIG" "$bak"
    info "已备份旧配置到: ${bak}"

    # 从 tproxy 模板重新生成 (保留旧配置的出站节点)
    local tpl="${SB_TEMPLATES_DIR}/config-tproxy.json"
    if [ ! -f "$tpl" ]; then
        error "模板不存在, 无法修复. 请先运行: sb --install"
        return 1
    fi

    # 提取旧配置中的代理节点 (非 direct/block/dns/selector/urltest 的出站)
    local nodes_file="/tmp/sb-nodes-$$.json"
    if command -v jq >/dev/null 2>&1; then
        jq '[.outbounds[] | select(.type | test("direct|block|dns|selector|urltest|reject") | not)]' \
           "$SB_CONFIG" > "$nodes_file" 2>/dev/null || echo '[]' > "$nodes_file"
    else
        echo '[]' > "$nodes_file"
    fi

    # 用新模板 + 旧节点重建 (通过面板 API 或直接 jq 合并)
    if command -v python3 >/dev/null 2>&1; then
        python3 -c "
import json
with open('$tpl') as f: tpl_cfg = json.load(f)
tpl_cfg = {k:v for k,v in tpl_cfg.items() if not k.startswith('_')}
with open('$nodes_file') as f: nodes = json.load(f)
if nodes:
    # 重建出站: select + auto + 节点 + direct
    ob = [{'tag':'select','type':'selector','outbounds':[n['tag'] for n in nodes]+['direct'],'default':nodes[0]['tag']}]
    ob += nodes
    ob += [{'tag':'direct','type':'direct'}]
    tpl_cfg['outbounds'] = ob
    # select 引用加入路由
    if 'route' in tpl_cfg:
        tpl_cfg['route']['final'] = 'select'
with open('$SB_CONFIG','w') as f: json.dump(tpl_cfg,f,indent=2,ensure_ascii=False)
" 2>/dev/null
        info "${GREEN}配置已从新模板重建 (保留旧节点)${NC}"
    else
        # 无 python3: 直接用模板
        jq 'with_entries(select(.key[0:1] != "_"))' "$tpl" > "$SB_CONFIG" 2>/dev/null \
            || cp "$tpl" "$SB_CONFIG"
        info "${GREEN}配置已从新模板重建 (未保留旧节点, 无 python3)${NC}"
    fi

    rm -f "$nodes_file"

    # 校验
    if [ -x "$SB_BIN" ]; then
        info "校验配置..."
        if "$SB_BIN" check -c "$SB_CONFIG" 2>&1; then
            info "${GREEN}配置校验通过${NC}"
        else
            warn "配置校验仍有问题, 请检查日志"
            return 1
        fi
    fi

    info "${GREEN}配置修复完成${NC}"
    info "如需重启: rc-service sing-box restart"
}

# ==================== 下载/安装管理面板 ====================
# 面板来源: 优先从 GitHub 仓库下载最新 panel/app.py, 失败回退到本地
install_panel() {
    step "安装 Web 管理面板"

    mkdir -p "$INSTALL_DIR" "$PANEL_DIR"

    local panel_src_ok=no

    # 1) 尝试从 GitHub 仓库下载最新面板代码
    if [ -n "${PANEL_REPO:-}" ] && [ -n "${PANEL_BRANCH:-}" ]; then
        local raw_base="https://raw.githubusercontent.com/${PANEL_REPO}/${PANEL_BRANCH}/panel"
        info "从 GitHub 下载面板代码: ${PANEL_REPO}@${PANEL_BRANCH}"
        if curl -sL --fail --connect-timeout 20 --max-time 60 -o "$PANEL_APP.tmp" "${raw_base}/app.py"; then
            # 校验: 必须像 Python 文件 (排除 404 HTML 页面)
            if grep -qE '^(from |import |def |app\s*=|Flask\(|@app\.route)' "$PANEL_APP.tmp" 2>/dev/null; then
                mv "$PANEL_APP.tmp" "$PANEL_APP"
                chmod 644 "$PANEL_APP"
                info "${GREEN}面板代码已从 GitHub 更新${NC}"
                panel_src_ok=yes
            else
                warn "GitHub 返回内容不像 Python 文件 (路径不存在?), 回退本地"
                rm -f "$PANEL_APP.tmp"
            fi
        else
            warn "GitHub 下载失败 (仓库/分支/路径不存在?), 回退本地"
            rm -f "$PANEL_APP.tmp"
        fi
    fi

    # 2) 回退: 本地 panel/app.py
    if [ "$panel_src_ok" != "yes" ]; then
        if [ -f "${SCRIPT_DIR}/panel/app.py" ]; then
            info "使用本地面板代码..."
            cp -f "${SCRIPT_DIR}/panel/app.py" "$PANEL_APP"
            panel_src_ok=yes
        elif [ -f "$PANEL_APP" ]; then
            info "本地无源码, 保留已安装的面板"
            panel_src_ok=yes
        else
            error "找不到面板代码 (GitHub 和本地均无 panel/app.py)"
            return 1
        fi
    fi

    # 3) Python 虚拟环境 + 依赖 (pip 自动按架构选 wheel, 无需手动处理)
    if [ ! -d "$PANEL_VENV" ]; then
        info "创建 Python 虚拟环境..."
        python3 -m venv "$PANEL_VENV"
    fi
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

# ==================== 安装快捷命令 sb ====================
# 将部署脚本复制到 /opt/singbox-gateway/ 并创建 /usr/local/bin/sb 包装器
# 安装后用户可直接输入 sb 打开管理菜单, 无需记脚本路径
install_shortcut() {
    step "安装快捷命令 sb"

    local target_dir="/opt/singbox-gateway"
    local target_script="${target_dir}/sing-box-gateway-deploy.sh"
    local sb_link="/usr/local/bin/sb"

    mkdir -p "$target_dir"

    # 1) 复制部署脚本本身到安装目录 (使 sb 始终能找到它)
    local script_path
    script_path="${SCRIPT_DIR}/$(basename "$0")"
    [ -f "$script_path" ] || script_path="$0"
    if [ "$(readlink -f "$script_path" 2>/dev/null || echo "$script_path")" != "$(readlink -f "$target_script" 2>/dev/null || echo "$target_script")" ]; then
        info "复制部署脚本到 ${target_script}..."
    fi
    cp -f "$script_path" "$target_script" 2>/dev/null || cp -f "$0" "$target_script" 2>/dev/null || true
    chmod +x "$target_script"

    # 2) 复制模板目录 (供后续 sb --install 的 install_configs 使用)
    if [ -d "${SCRIPT_DIR}/templates" ] && [ "${SCRIPT_DIR}" != "${target_dir}" ]; then
        info "复制模板文件到 ${target_dir}/templates/..."
        cp -rf "${SCRIPT_DIR}/templates" "${target_dir}/"
    fi

    # 3) 复制面板源码 (供后续 sb --update-panel 的本地回退使用)
    if [ -f "${SCRIPT_DIR}/panel/app.py" ] && [ "${SCRIPT_DIR}" != "${target_dir}" ]; then
        mkdir -p "${target_dir}/panel"
        cp -f "${SCRIPT_DIR}/panel/app.py" "${target_dir}/panel/"
    fi

    # 4) 创建 sb 快捷命令
    info "创建快捷命令: sb → ${target_script}"
    cat > "$sb_link" << 'SHORTCUTEOF'
#!/bin/sh
# sb - sing-box 旁路由网关快捷命令
# 用法:
#   sb                 交互式菜单
#   sb --install       完整部署
#   sb --update-core   更新核心
#   sb --update-panel  更新面板
#   sb --convert-apply <URL>  转换并应用订阅
#   sb --help          查看帮助
exec bash /opt/singbox-gateway/sing-box-gateway-deploy.sh "$@"
SHORTCUTEOF
    chmod +x "$sb_link"

    # 5) 检查 PATH
    if echo "$PATH" | grep -q "/usr/local/bin"; then
        info "${GREEN}快捷命令已安装: 直接输入 sb 即可打开管理菜单${NC}"
    else
        warn "快捷命令已安装到 ${sb_link}, 但 /usr/local/bin 不在 PATH 中"
        warn "请手动添加: export PATH=\$PATH:/usr/local/bin"
    fi
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

# ==================== 仪表盘管理 (走面板 API) ====================
# 仪表盘由 Flask 面板本地托管, 故管理操作调用面板 API
# 可用仪表盘: metacubexd / zashboard / zashboard-lite / yacd
DASHBOARD_NAMES="metacubexd zashboard zashboard-lite yacd"

panel_api() {
    local panel_port
    panel_port="$(jq -r '.panel_port // 9999' "$PANEL_CONFIG" 2>/dev/null || echo 9999)"
    curl -s "http://127.0.0.1:${panel_port}$1"
}

panel_api_post() {
    local panel_port
    panel_port="$(jq -r '.panel_port // 9999' "$PANEL_CONFIG" 2>/dev/null || echo 9999)"
    curl -s -X POST "http://127.0.0.1:${panel_port}$1" \
        -H 'Content-Type: application/json' -d "$2"
}

panel_api_delete() {
    local panel_port
    panel_port="$(jq -r '.panel_port // 9999' "$PANEL_CONFIG" 2>/dev/null || echo 9999)"
    curl -s -X DELETE "http://127.0.0.1:${panel_port}$1"
}

# 检查面板是否运行
check_panel_running() {
    local panel_port
    panel_port="$(jq -r '.panel_port // 9999' "$PANEL_CONFIG" 2>/dev/null || echo 9999)"
    curl -s --max-time 3 "http://127.0.0.1:${panel_port}/api/status" >/dev/null 2>&1
}

# 列出仪表盘 (解析面板 API 返回, 显示表格)
list_dashboards_sh() {
    if ! check_panel_running; then
        warn "面板未运行, 无法查询仪表盘, 请先启动面板"
        return 1
    fi
    info "可用/已安装的仪表盘:"
    echo ""
    printf "  %-16s %-8s %-8s %s\n" "名称" "状态" "活动" "说明"
    printf "  %-16s %-8s %-8s %s\n" "----" "----" "----" "----"
    panel_api "/api/dashboards" | jq -r '.[] | "  \(.name)\t\(.installed|tostring)\t\(.active|tostring)\t\(.desc)"' 2>/dev/null | \
    while IFS=$'\t' read -r name installed active desc; do
        local st="未安装"; [ "$installed" = "true" ] && st="${GREEN}已安装${NC}"
        local ac=""; [ "$active" = "true" ] && ac="${GREEN}★${NC}"
        printf "  %-16s %-8s %-8s %s\n" "$name" "$st" "$ac" "$desc"
    done
    echo ""
}

# 安装仪表盘 (走面板 API)
install_dashboard_sh() {
    local name="$1"
    if [ -z "$name" ]; then
        error "未指定仪表盘名"
        return 1
    fi
    if ! check_panel_running; then
        error "面板未运行, 无法安装仪表盘; 请先执行完整安装或启动面板服务"
        return 1
    fi
    info "安装仪表盘: $name (从 GitHub 下载, 可能需要 1-2 分钟)..."
    local result
    result="$(panel_api_post "/api/dashboards/install" "{\"name\":\"$name\"}")"
    if echo "$result" | jq -e '.ok' >/dev/null 2>&1; then
        info "${GREEN}$(echo "$result" | jq -r '.message')${NC}"
    else
        error "$(echo "$result" | jq -r '.error // "安装失败"')"
    fi
}

# 切换活动仪表盘
activate_dashboard_sh() {
    local name="$1"
    if ! check_panel_running; then
        error "面板未运行"
        return 1
    fi
    local result
    result="$(panel_api_post "/api/dashboards/${name}/activate" '{}')"
    if echo "$result" | jq -e '.ok' >/dev/null 2>&1; then
        info "${GREEN}$(echo "$result" | jq -r '.message')${NC}"
    else
        error "$(echo "$result" | jq -r '.error')"
    fi
}

# 删除仪表盘
delete_dashboard_sh() {
    local name="$1"
    if ! check_panel_running; then
        error "面板未运行"
        return 1
    fi
    local result
    result="$(panel_api_delete "/api/dashboards/${name}")"
    if echo "$result" | jq -e '.ok' >/dev/null 2>&1; then
        info "${GREEN}$(echo "$result" | jq -r '.message')${NC}"
    else
        error "$(echo "$result" | jq -r '.error')"
    fi
}

# ---- 面板管理子菜单 ----
menu_panel_manage() {
    while true; do
        echo -e "\n${CYAN}── 面板管理 ──${NC}"
        echo "  Flask 管理面板 (端口 9999, 本工具):"
        echo "    u) 更新 Flask 面板代码 (从 GitHub/本地)"
        echo "  Clash API 仪表盘 (前端, 本地托管):"
        echo "    1) 安装 metacubexd   (官方, 推荐)"
        echo "    2) 安装 zashboard    (现代, 含字体)"
        echo "    3) 安装 zashboard-lite (精简)"
        echo "    4) 安装 yacd         (经典)"
        echo "    l) 列出所有仪表盘状态"
        echo "    s) 切换活动仪表盘"
        echo "    d) 删除仪表盘"
        echo "    o) 打开活动仪表盘 (浏览器)"
        echo "    0) 返回主菜单"
        read -r -p "选择 [0-4/u/l/s/d/o]: " c
        case "$c" in
            u|U) do_update_panel ;;
            1) install_dashboard_sh metacubexd ;;
            2) install_dashboard_sh zashboard ;;
            3) install_dashboard_sh zashboard-lite ;;
            4) install_dashboard_sh yacd ;;
            l|L) list_dashboards_sh ;;
            s|S)
                list_dashboards_sh
                read -r -p "切换到哪个仪表盘: " dn
                activate_dashboard_sh "$dn"
                ;;
            d|D)
                list_dashboards_sh
                read -r -p "删除哪个仪表盘: " dn
                read -r -p "确认删除 $dn? (y/N) " yn
                [ "$yn" = "y" ] && delete_dashboard_sh "$dn"
                ;;
            o|O)
                local ip_addr
                ip_addr="$(ip -4 addr show scope global 2>/dev/null | grep inet | awk '{print $2}' | head -1 | cut -d/ -f1)"
                info "在浏览器打开: http://${ip_addr:-<IP>}:9999/ui/"
                ;;
            0|'') return 0 ;;
            *) warn "无效选项" ;;
        esac
        read -r -p "按回车继续..." _
    done
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

# ==================== 转换并应用 (直接落地 config.json + 重启) ====================
do_convert_apply() {
    local sub_url="${1:-}"
    if [ -z "$sub_url" ]; then
        echo "用法: $0 --convert-apply <订阅URL> [模板]"
        echo "模板: tproxy (默认) | tun | mixed"
        return 1
    fi
    local template="${2:-tproxy}"
    step "转换订阅并应用"
    info "订阅地址: $sub_url"
    info "使用模板: $template"
    local panel_port
    panel_port="$(jq -r '.panel_port // 9999' "$PANEL_CONFIG" 2>/dev/null || echo 9999)"

    info "调用转换并应用 API (下载→解析→合并→校验→写入 config.json→重启)..."
    local result
    result="$(curl -s -X POST "http://127.0.0.1:${panel_port}/api/convert/apply" \
        -H 'Content-Type: application/json' \
        -d "{\"url\": \"$sub_url\", \"template\": \"$template\"}")"

    if echo "$result" | jq -e '.ok' >/dev/null 2>&1; then
        local count
        count="$(echo "$result" | jq -r '.node_count')"
        info "${GREEN}成功转换 $count 个节点并应用为 ${template} 模板${NC}"
        echo "$result" | jq -r '.nodes[]' 2>/dev/null | head -20
        echo ""
        echo "$result" | jq -r '.message'
    else
        error "转换并应用失败:"
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
        rm -f /usr/local/bin/sb
        rm -rf "$SB_DIR" "$INSTALL_DIR" "$LOG_DIR"
        rm -f /etc/sysctl.d/99-sing-box.conf
        rm -f "$NFT_RULES"
        info "${GREEN}卸载完成${NC}"
    else
        info "保留配置文件, 仅删除服务和二进制"
        rm -f /etc/init.d/sing-box /etc/init.d/singbox-panel /etc/init.d/nftables-sing-box
        rm -f /etc/conf.d/sing-box /etc/conf.d/singbox-panel
        rm -f "$SB_BIN"
        rm -f /usr/local/bin/sb
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
    install_shortcut

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
    sb                           快捷打开管理菜单 (推荐)
    sb --install                 完整部署
    sb --update-core              更新核心
    sb --update-panel             更新面板
    sb --update-sub               更新订阅
    sb --convert-apply <URL>      转换并应用订阅
    sb --uninstall                卸载
    bash $0                       (等价于 sb)

EOF
}

# ============================================================
#  交互式菜单
# ============================================================

# ---- 读取核心版本 ----
get_core_version() {
    if [ -x "$SB_BIN" ] && [ -f "$SB_VERSION_FILE" ]; then
        echo "v$(cat "$SB_VERSION_FILE")"
    elif [ -x "$SB_BIN" ]; then
        "$SB_BIN" version 2>/dev/null | grep -oiE 'sing-box version [^ ]+' | awk '{print $3}' | sed 's/^/v/' || echo "未知"
    else
        echo "未安装"
    fi
}

# ---- 服务状态字符串 ----
svc_status() {
    local svc="$1"
    if rc-service "$svc" status >/dev/null 2>&1; then
        echo -e "${GREEN}运行中${NC}"
    else
        echo -e "${RED}已停止${NC}"
    fi
}

# ---- 节点计数 ----
node_count() {
    if [ -f "$SB_CONFIG" ] && command -v jq >/dev/null 2>&1; then
        jq '[.outbounds[] | select(.type|test("shadowsocks|vmess|vless|trojan|hysteria2|tuic"))] | length' "$SB_CONFIG" 2>/dev/null || echo "0"
    else
        echo "0"
    fi
}

# ---- 状态头 ----
show_status_header() {
    local ip_addr
    ip_addr="$(ip -4 addr show scope global 2>/dev/null | grep inet | awk '{print $2}' | head -1 | cut -d/ -f1)"
    echo -e "${CYAN}╔══════════════════════════════════════════════════════════╗${NC}"
    echo -e "${CYAN}║        sing-box 旁路由网关管理 (Alpine Linux)            ║${NC}"
    echo -e "${CYAN}╚══════════════════════════════════════════════════════════╝${NC}"
    echo -e "  本机 IP:     ${ip_addr:-未知}"
    echo -e "  核心版本:   $(get_core_version)"
    echo -e "  sing-box:   $(svc_status sing-box)    面板: $(svc_status singbox-panel)"
    echo -e "  节点数量:    $(node_count)    面板地址: http://${ip_addr:-<IP>}:9999"
    echo ""
}

# ---- 服务控制子菜单 ----
menu_service_control() {
    while true; do
        echo -e "\n${CYAN}── 服务管理 ──${NC}"
        echo "  1) 启动 sing-box        2) 停止 sing-box        3) 重启 sing-box"
        echo "  4) 启动面板             5) 停止面板             6) 重启面板"
        echo "  7) 启动 nftables网关    8) 停止 nftables网关    9) 重启 nftables网关"
        echo "  s) 全部启动             t) 全部停止             r) 全部重启"
        echo "  0) 返回主菜单"
        read -r -p "选择 [0-9/s/t/r]: " c
        case "$c" in
            1) rc-service sing-box start 2>&1 || true ;;
            2) rc-service sing-box stop 2>&1 || true ;;
            3) rc-service sing-box restart 2>&1 || true ;;
            4) rc-service singbox-panel start 2>&1 || true ;;
            5) rc-service singbox-panel stop 2>&1 || true ;;
            6) rc-service singbox-panel restart 2>&1 || true ;;
            7) rc-service nftables-sing-box start 2>&1 || true ;;
            8) rc-service nftables-sing-box stop 2>&1 || true ;;
            9) rc-service nftables-sing-box restart 2>&1 || true ;;
            s|S) rc-service sing-box start 2>&1; rc-service singbox-panel start 2>&1; rc-service nftables-sing-box start 2>&1 || true ;;
            t|T) rc-service nftables-sing-box stop 2>&1; rc-service singbox-panel stop 2>&1; rc-service sing-box stop 2>&1 || true ;;
            r|R) rc-service sing-box restart 2>&1; rc-service singbox-panel restart 2>&1; rc-service nftables-sing-box restart 2>&1 || true ;;
            0|'') return 0 ;;
            *) warn "无效选项" ;;
        esac
        read -r -p "按回车继续..." _
    done
}

# ---- 转换订阅子菜单 ----
menu_convert() {
    while true; do
        echo -e "\n${CYAN}── 订阅转换 (Clash/V2Ray → sing-box) ──${NC}"
        echo "  1) 转换预览      (解析订阅, 显示节点和生成的 JSON)"
        echo "  2) 转换并应用    (写入 config.json + sing-box check + 重启, 一键生效)"
        echo "  3) 转换并下载    (输出 JSON 到文件)"
        echo "  0) 返回主菜单"
        read -r -p "选择 [0-3]: " c
        case "$c" in
            1|2|3)
                read -r -p "订阅URL: " su
                [ -z "$su" ] && { warn "URL 不能为空"; continue; }
                read -r -p "模板(tproxy/tun/mixed) [tproxy]: " tp
                tp="${tp:-tproxy}"
                case "$c" in
                    1) do_convert "$su" "$tp" ;;
                    2) do_convert_apply "$su" "$tp" ;;
                    3) local out="/tmp/sing-box-config-$$.json"
                       do_convert "$su" "$tp" | tail -n +1 > "$out" 2>/dev/null
                       # 上面 do_convert 有额外输出, 用 API 直接取
                       local pp; pp="$(jq -r '.panel_port // 9999' "$PANEL_CONFIG" 2>/dev/null || echo 9999)"
                       curl -s -X POST "http://127.0.0.1:${pp}/api/convert" \
                           -H 'Content-Type: application/json' \
                           -d "{\"url\": \"$su\", \"template\": \"$tp\"}" | jq '.config' > "$out" 2>/dev/null
                       info "${GREEN}配置已保存到 ${out}${NC}"
                       ;;
                esac
                ;;
            0|'') return 0 ;;
            *) warn "无效选项" ;;
        esac
        read -r -p "按回车继续..." _
    done
}

# ---- 网关模式子菜单 ----
menu_setup_gateway() {
    while true; do
        echo -e "\n${CYAN}── 配置透明网关 ──${NC}"
        echo "  1) tproxy 模式  (旁路由推荐, 需 nftables 规则 + 路由表)"
        echo "  2) TUN 模式     (sing-box 自管路由, 无需防火墙规则, 最简单)"
        echo "  3) Mixed 模式  (仅 HTTP/SOCKS5 代理, 不做透明网关)"
        echo "  4) 应用 tproxy nftables 规则 (加载 fwmark + 路由表 100)"
        echo "  5) 清除 tproxy nftables 规则"
        echo "  0) 返回主菜单"
        read -r -p "选择 [0-5]: " c
        case "$c" in
            1|2|3)
                local tpl
                case "$c" in 1) tpl="tproxy";; 2) tpl="tun";; 3) tpl="mixed";; esac
                if [ ! -f "${SB_TEMPLATES_DIR}/config-${tpl}.json" ]; then
                    error "模板不存在: config-${tpl}.json (先重新安装配置)"
                    continue
                fi
                info "切换到 ${tpl} 模式 (备份当前配置)"
                [ -f "$SB_CONFIG" ] && cp "$SB_CONFIG" "$SB_CONFIG_BACKUP"
                # 复制模板并去除所有 _ 开头字段 (模板本身已无注释, 此为安全兜底)
                if command -v jq >/dev/null 2>&1; then
                    jq 'with_entries(select(.key[0:1] != "_"))' \
                       "${SB_TEMPLATES_DIR}/config-${tpl}.json" > "$SB_CONFIG" 2>/dev/null \
                       || cp "${SB_TEMPLATES_DIR}/config-${tpl}.json" "$SB_CONFIG"
                else
                    cp "${SB_TEMPLATES_DIR}/config-${tpl}.json" "$SB_CONFIG"
                fi
                # 记录模式
                if [ -f "$PANEL_CONFIG" ]; then
                    jq --arg m "$tpl" '.gateway_mode=$m' "$PANEL_CONFIG" > "$PANEL_CONFIG.tmp" && mv "$PANEL_CONFIG.tmp" "$PANEL_CONFIG"
                fi
                info "${GREEN}已切换为 ${tpl} 模式, 正在重启 sing-box...${NC}"
                rc-service sing-box restart 2>&1 || warn "sing-box 重启失败, 请检查配置"
                if [ "$tpl" = "tproxy" ]; then
                    warn "tproxy 模式需执行选项 4 加载 nftables 规则才能生效"
                fi
                ;;
            4)
                if [ ! -f "$NFT_RULES" ]; then
                    error "nftables 规则文件不存在: $NFT_RULES"
                    continue
                fi
                info "加载 nftables 规则..."
                modprobe nft_tproxy 2>/dev/null || true
                modprobe xt_TPROXY 2>/dev/null || true
                nft -f "$NFT_RULES" 2>/dev/null || error "nft 加载失败"
                ip route add local default dev lo table 100 2>/dev/null || info "路由表 100 已存在"
                ip rule add fwmark 1 table 100 2>/dev/null || info "fwmark 规则已存在"
                info "${GREEN}tproxy 规则已加载${NC}"
                rc-service nftables-sing-box restart 2>/dev/null || true
                ;;
            5)
                info "清除 nftables 规则..."
                nft delete table ip sing-box 2>/dev/null || true
                ip route del local default dev lo table 100 2>/dev/null || true
                ip rule del fwmark 1 table 100 2>/dev/null || true
                info "${GREEN}已清除 tproxy 规则${NC}"
                ;;
            0|'') return 0 ;;
            *) warn "无效选项" ;;
        esac
        read -r -p "按回车继续..." _
    done
}

# ---- 显示当前配置信息 ----
show_info() {
    step "当前配置信息"
    local ip_addr
    ip_addr="$(ip -4 addr show scope global 2>/dev/null | grep inet | awk '{print $2}' | head -1 | cut -d/ -f1)"
    echo -e "  核心版本:    $(get_core_version)"
    echo -e "  核心二进制:  ${SB_BIN}"
    echo -e "  核心配置:    ${SB_CONFIG}"
    echo -e "  配置备份:    ${SB_CONFIG_BACKUP}"
    echo -e "  模板目录:    ${SB_TEMPLATES_DIR}"
    echo -e "  订阅文件:    ${SUBS_FILE}"
    echo -e "  面板配置:    ${PANEL_CONFIG}"
    echo -e "  面板代码:    ${PANEL_APP}"
    echo -e "  nft 规则:    ${NFT_RULES}"
    echo -e "  节点数量:    $(node_count)"
    echo ""
    echo -e "  服务状态:"
    echo -e "    sing-box:        $(svc_status sing-box)"
    echo -e "    singbox-panel:   $(svc_status singbox-panel)"
    echo -e "    nftables网关:    $(svc_status nftables-sing-box)"
    echo ""
    # sb 快捷命令状态
    if [ -x /usr/local/bin/sb ]; then
        echo -e "  快捷命令:    ${GREEN}sb 已安装${NC} (直接输入 sb 打开菜单)"
    else
        echo -e "  快捷命令:    ${YELLOW}sb 未安装${NC} (选菜单 12 或 --install-shortcut 安装)"
    fi
    echo ""
    echo -e "  访问地址:"
    echo -e "    管理面板:   http://${ip_addr:-<IP>}:9999"
    echo -e "    Clash API:  http://${ip_addr:-<IP>}:9090"
    echo ""
    if [ -f "$PANEL_CONFIG" ]; then
        echo -e "  面板配置项:"
        jq '.' "$PANEL_CONFIG" 2>/dev/null | sed 's/^/    /'
    fi
}

# ---- 查看实时日志 ----
show_logs() {
    echo -e "\n${CYAN}── 日志查看 (Ctrl+C 退出) ──${NC}"
    echo "  1) sing-box 日志     2) 面板日志     3) nftables 流量"
    echo "  0) 返回"
    read -r -p "选择 [0-3]: " c
    case "$c" in
        1) tail -n 100 -f "${LOG_DIR}/sing-box-stderr.log" 2>/dev/null || error "日志文件不存在" ;;
        2) tail -n 100 -f "${LOG_DIR}/panel-stderr.log" 2>/dev/null || error "日志文件不存在" ;;
        3) nft monitor 2>/dev/null || error "nft monitor 不可用" ;;
        0|'') return 0 ;;
    esac
}

# ---- 主菜单循环 ----
main_menu() {
    while true; do
        clear 2>/dev/null || true
        show_status_header
        echo -e "${CYAN}请选择操作:${NC}"
        echo "  1)  完整安装      (首次部署: 依赖+核心+面板+服务+配置+内核)"
        echo "  2)  仅安装/更新核心"
        echo "  3)  仅安装/更新面板"
        echo "  4)  更新所有订阅  (拉取+合并节点到 config.json)"
        echo "  5)  转换订阅      (Clash/V2Ray → sing-box, 预览/下载/应用)"
        echo "  6)  服务管理      (启动/停止/重启 各服务)"
        echo "  7)  配置透明网关  (切换 tproxy/TUN/Mixed + nftables 规则)"
        echo "  8)  面板管理      (Flask面板更新 + 仪表盘安装/切换/删除)"
        echo "  9)  查看配置信息"
        echo " 10)  查看日志"
        echo " 11)  卸载"
        echo " 12)  安装快捷命令 sb  (安装后直接输入 sb 打开菜单)"
        echo " 13)  修复/迁移配置  (旧格式DNS/_comment → 新格式)"
        echo "  0)  退出"
        echo ""
        read -r -p "请选择 [0-13]: " choice
        case "$choice" in
            1)  full_install ;;
            2)  do_update_core ;;
            3)  do_update_panel ;;
            4)  do_update_sub ;;
            5)  menu_convert ;;
            6)  menu_service_control ;;
            7)  menu_setup_gateway ;;
            8)  menu_panel_manage ;;
            9)  show_info ;;
            10) show_logs ;;
            11) do_uninstall ;;
            12) install_shortcut ;;
            13) fix_config ;;
            0|"") echo "再见"; exit 0 ;;
            *) warn "无效选项: $choice" ;;
        esac
        echo ""
        read -r -p "按回车返回菜单 (Ctrl+C 退出)..." _
    done
}

# ==================== 主入口 ====================
main() {
    case "${1:-}" in
        --menu|-m)       main_menu ;;
        --install|--full) full_install ;;
        --update-core)  do_update_core ;;
        --update-panel) do_update_panel ;;
        --update-dashboard) install_dashboard_sh "${2:-}" ;;
        --update-sub)   do_update_sub ;;
        --convert)      do_convert "${2:-}" "${3:-tproxy}" ;;
        --convert-apply) do_convert_apply "${2:-}" "${3:-tproxy}" ;;
        --install-shortcut) install_shortcut ;;
        --fix-config)   fix_config ;;
        --status)       show_info ;;
        --uninstall)    do_uninstall ;;
        --help|-h)
            cat << 'HELPEOF'
sing-box 旁路由网关部署脚本 (Alpine Linux)

快捷命令:
  sb                             安装后可直接使用 (等价于 bash 本脚本)

用法:
  sb                              交互式菜单 (推荐)
  sb --install                    完整部署
  sb --update-core                更新核心
  sb --convert-apply <URL>        转换并应用订阅
  sb --fix-config                 修复/迁移旧格式配置 (legacy DNS / _comment)
  sb --help                       查看帮助
  bash $0                         交互式菜单 (默认)
  bash $0 --menu                  交互式菜单
  bash $0 --install               完整部署 (非交互, 核心+面板+服务+配置)
  bash $0 --update-core           仅更新 sing-box 核心
  bash $0 --update-panel          仅更新管理面板
  bash $0 --update-dashboard <name>  安装/更新仪表盘 (metacubexd/zashboard/yacd)
  bash $0 --update-sub            更新所有订阅并合并节点
  bash $0 --convert <URL> [模板]  转换订阅为 sing-box JSON
  bash $0 --convert-apply <URL> [模板]  转换订阅并直接写入 config.json + 重启
                                  模板: tproxy / tun / mixed
  bash $0 --install-shortcut      安装 sb 快捷命令到 /usr/local/bin/sb
  bash $0 --fix-config            修复/迁移旧格式配置
  bash $0 --status                查看当前配置与状态
  bash $0 --uninstall             卸载 (交互式确认)
  bash $0 --help                  显示此帮助

菜单功能 (交互模式):
  1 完整安装  2 更新核心  3 更新面板  4 更新订阅  5 转换订阅(预览/应用)
  6 服务管理  7 配置网关  8 面板管理  9 查看信息  10 查看日志  11 卸载
  12 安装快捷命令 sb  13 修复/迁移配置

模板说明:
  config-tproxy.json  tproxy 透明网关 (需 nftables 规则, 推荐旁路由)
  config-tun.json     TUN 自动路由 (无需防火墙规则, sing-box 自管路由)
  config-mixed.json   Mixed 代理 (仅 HTTP/SOCKS, 不做透明网关)
HELPEOF
            ;;
        *)
            # 无参数 = 默认进入交互式菜单
            main_menu
            ;;
    esac
}

main "$@"
