#!/usr/bin/env bash
# harden-flow.sh — one-shot security hardening for the Mise host ("flow").
#
# Run ON the server, as root:
#     cd /opt/mise && sudo bash ops/harden-flow.sh
#
# What it does (idempotent — safe to re-run):
#   1. Backs up every config it touches to /root/hardening-backup-<timestamp>/
#   2. Installs the repo's mise.service (uvicorn binds 127.0.0.1 — origin only
#      reachable through the Cloudflare tunnel, not direct-to-IP)
#   3. UFW: default-deny inbound; allow loopback, tailnet (SSH + tailnet-only
#      services), and docker-bridge access to ollama :11434
#   4. Disables nginx (serves only the default page; tunnel handles all web traffic)
#   5. sshd: key-only auth, no root login, tight auth limits (drop-in conf)
#   6. fail2ban with an sshd jail
#   7. Locks down /opt/mise/data permissions (client data)
#   8. unattended-upgrades: auto-remove unused kernels/deps
#   9. Prints a verification summary (ports, firewall, fail2ban)
#
# It does NOT: touch cloudflared, the DB, /opt/mise/.env, docker, or odysseus.
# Your current SSH session survives: it arrives over tailscale0, which stays allowed.

set -euo pipefail

if [[ $EUID -ne 0 ]]; then
    echo "run as root: sudo bash $0" >&2
    exit 1
fi

BACKUP_DIR="/root/hardening-backup-$(date +%Y%m%d-%H%M%S)"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MISE_ENV="/opt/mise/.env"

step() { printf '\n\033[1m== %s ==\033[0m\n' "$*"; }

# ── 1. Backups ──────────────────────────────────────────────────────────────
step "Backing up current configs to $BACKUP_DIR"
mkdir -p "$BACKUP_DIR"
for f in /etc/systemd/system/mise.service /etc/ssh/sshd_config \
         /etc/nginx/sites-enabled/default /etc/ufw/user.rules; do
    [[ -f $f ]] && cp -a "$f" "$BACKUP_DIR/"
done
ss -tlnp > "$BACKUP_DIR/ss-before.txt" || true

# ── 2. mise.service: bind uvicorn to loopback ───────────────────────────────
step "Installing mise.service (uvicorn --host 127.0.0.1)"
install -m 0644 "$REPO_ROOT/mise.service" /etc/systemd/system/mise.service
systemctl daemon-reload
systemctl restart mise
sleep 2
systemctl is-active --quiet mise || { echo "mise failed to start — check: journalctl -u mise -n 50" >&2; exit 1; }
curl -fsS -o /dev/null http://127.0.0.1:8400/healthz \
    && echo "mise healthy on 127.0.0.1:8400" \
    || { echo "healthz failed after restart" >&2; exit 1; }

# Guard: per-IP controls (rate limit, PIN lockout) rely on uvicorn trusting
# forwarded headers from loopback only. Broad FORWARDED_ALLOW_IPS would make
# them spoofable.
if grep -qE '^FORWARDED_ALLOW_IPS=(\*|0\.0\.0\.0)' "$MISE_ENV" 2>/dev/null; then
    echo "WARNING: FORWARDED_ALLOW_IPS in $MISE_ENV is broadly scoped — set it to 127.0.0.1,::1" >&2
fi

# ── 3. UFW ──────────────────────────────────────────────────────────────────
step "Configuring UFW (default-deny inbound; loopback + tailnet + docker→ollama)"
ufw default deny incoming
ufw default allow outgoing
ufw allow in on lo
# Tailnet: SSH administration and any tailnet-only services (ollama, odysseus).
ufw allow in on tailscale0
# Docker containers reach host ollama via bridge gateways (172.16/12 pool).
ufw allow from 172.16.0.0/12 to any port 11434 proto tcp
ufw logging low
ufw --force enable
ufw status verbose

# ── 4. nginx off (tunnel serves all web traffic; :80 was default page only) ──
step "Disabling nginx"
if systemctl is-active --quiet nginx; then
    systemctl disable --now nginx
    echo "nginx disabled"
else
    echo "nginx already inactive"
fi

# ── 5. sshd hardening (key-only; existing sessions are not dropped) ─────────
step "Hardening sshd"
install -m 0644 /dev/stdin /etc/ssh/sshd_config.d/60-hardening.conf <<'EOF'
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin no
MaxAuthTries 3
LoginGraceTime 30
X11Forwarding no
EOF
sshd -t
systemctl reload ssh
echo "sshd reloaded — VERIFY you can open a NEW ssh session before logging out"

# ── 6. fail2ban ─────────────────────────────────────────────────────────────
step "Installing/enabling fail2ban (sshd jail)"
if ! dpkg -s fail2ban >/dev/null 2>&1; then
    apt-get update -qq
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq fail2ban
fi
install -m 0644 /dev/stdin /etc/fail2ban/jail.d/sshd-hardening.conf <<'EOF'
[sshd]
enabled = true
backend = systemd
maxretry = 5
findtime = 10m
bantime = 1h
EOF
systemctl enable --now fail2ban
systemctl restart fail2ban
fail2ban-client status sshd

# ── 7. Client-data permissions ──────────────────────────────────────────────
step "Locking down /opt/mise/data permissions"
chown -R mise:mise /opt/mise/data
chmod 750 /opt/mise/data
find /opt/mise/data -type d -exec chmod 750 {} +
find /opt/mise/data -type f \( -name '*.db' -o -name '*.db-*' -o -name '*.gz' \) -exec chmod 640 {} +

# ── 8. unattended-upgrades: prune unused kernels/deps ───────────────────────
step "Enabling auto-removal of unused kernels/dependencies"
CONF=/etc/apt/apt.conf.d/50unattended-upgrades
if [[ -f $CONF ]]; then
    sed -i 's|^//Unattended-Upgrade::Remove-Unused-Kernel-Packages "true";|Unattended-Upgrade::Remove-Unused-Kernel-Packages "true";|' "$CONF"
    sed -i 's|^//Unattended-Upgrade::Remove-Unused-Dependencies "false";|Unattended-Upgrade::Remove-Unused-Dependencies "true";|' "$CONF"
fi

# ── 9. Verification ─────────────────────────────────────────────────────────
step "Verification"
echo "-- listening sockets (after) --"
ss -tlnp
echo
echo "-- expectation: :8400 on 127.0.0.1 · no nginx :80 · :22 reachable via tailnet only (UFW) --"
echo
echo "-- tunnel path --"
curl -fsS -o /dev/null -w "https://kleephotography.com -> %{http_code}\n" https://kleephotography.com/ || true
echo
echo "Backups: $BACKUP_DIR"
echo "Manual step remaining: Cloudflare dashboard (WAF, rate limits, Access for plutus.*) — see PR notes."
