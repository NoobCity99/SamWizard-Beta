#!/usr/bin/env bash
set -Eeuo pipefail

APP_DIR="${SAMWIZARD_APP_DIR:-/opt/samwizard}"
ENV_DIR="${SAMWIZARD_ENV_DIR:-/etc/samwizard}"
STATE_DIR="${SAMWIZARD_STATE_DIR:-/var/lib/samwizard}"
# Default env file: /etc/samwizard/samwizard.env
ENV_FILE="${SAMWIZARD_ENV_FILE:-${ENV_DIR}/samwizard.env}"
SERVICE_FILE="/etc/systemd/system/samwizard.service"
APP_URL="${SAMWIZARD_APP_URL:-https://github.com/NoobCity99/SamWizard-Beta/releases/latest/download/samwizard-app.tar.gz}"

HOST="${SAMWIZARD_HOST:-0.0.0.0}"
PORT="${SAMWIZARD_PORT:-8080}"
BETA="${SAMWIZARD_BETA:-1}"
BETA_LABEL="${SAMWIZARD_BETA_LABEL:-BETA}"

say() {
  printf '%s\n' "$*"
}

fail() {
  say "SamWizard Beta installer stopped: $*" >&2
  exit 1
}

require_root() {
  if [ "${EUID}" -ne 0 ]; then
    fail "run this installer with sudo, for example: sudo bash SamWizardBeta.sh"
  fi
}

load_os_release() {
  if [ ! -r /etc/os-release ]; then
    fail "this installer needs Ubuntu Server or compatible Ubuntu Linux."
  fi
  # shellcheck disable=SC1091
  . /etc/os-release
  case "${ID:-} ${ID_LIKE:-}" in
    *ubuntu*|*debian*) ;;
    *) fail "this system does not look like Ubuntu. Found ID=${ID:-unknown}." ;;
  esac
}

require_systemd() {
  if ! command -v systemctl >/dev/null 2>&1; then
    fail "systemctl was not found. SamWizard expects Ubuntu Server with systemd."
  fi
  if [ ! -d /run/systemd/system ]; then
    fail "systemd does not appear to be running. Use a real Ubuntu Server install."
  fi
}

install_prerequisites() {
  say "Installing required system tools..."
  export DEBIAN_FRONTEND=noninteractive
  apt-get update
  apt-get install -y curl ca-certificates python3 python3-venv python3-pip tar iproute2 ufw ntfs-3g exfatprogs
  if ! apt-get install -y hfsplus hfsprogs; then
    say "Optional Mac HFS tools were not installed. NTFS and exFAT support are still installed."
  fi
}

download_and_extract_app() {
  say "Downloading SamWizard Beta..."
  temp_dir="$(mktemp -d)"
  trap 'rm -rf "${temp_dir}"' EXIT
  bundle="${temp_dir}/samwizard-app.tar.gz"
  curl -fL "${APP_URL}" -o "${bundle}"

  mkdir -p "${APP_DIR}"
  rm -rf "${APP_DIR}/app" "${APP_DIR}/requirements.txt" "${APP_DIR}/README.md" "${APP_DIR}/VERSION"
  tar -xzf "${bundle}" -C "${APP_DIR}"
}

install_python_requirements() {
  say "Preparing Python environment..."
  python3 -m venv "${APP_DIR}/venv"
  "${APP_DIR}/venv/bin/python" -m pip install --upgrade pip
  "${APP_DIR}/venv/bin/python" -m pip install -r "${APP_DIR}/requirements.txt"
}

write_environment_file() {
  say "Writing service settings..."
  mkdir -p "${ENV_DIR}"
  mkdir -p "${STATE_DIR}"
  chmod 700 "${ENV_DIR}"
  chmod 700 "${STATE_DIR}"
  if [ -r "${ENV_FILE}" ] && grep -q '^SAMWIZARD_SECRET_KEY=' "${ENV_FILE}"; then
    secret_key="$(grep '^SAMWIZARD_SECRET_KEY=' "${ENV_FILE}" | tail -n 1 | cut -d= -f2-)"
  else
    secret_key="$(python3 -c 'import secrets; print(secrets.token_urlsafe(48))')"
  fi

  cat > "${ENV_FILE}" <<EOF
SAMWIZARD_SECRET_KEY=${secret_key}
SAMWIZARD_HOST=${HOST}
SAMWIZARD_PORT=${PORT}
SAMWIZARD_STATE_DIR=${STATE_DIR}
SAMWIZARD_BETA=${BETA}
SAMWIZARD_BETA_LABEL=${BETA_LABEL}
EOF
  chmod 600 "${ENV_FILE}"
}

write_service_file() {
  say "Creating systemd service..."
  cat > "${SERVICE_FILE}" <<EOF
[Unit]
Description=SamWizard
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=${APP_DIR}
EnvironmentFile=${ENV_FILE}
ExecStart=${APP_DIR}/venv/bin/python -m uvicorn app.main:app --host \${SAMWIZARD_HOST} --port \${SAMWIZARD_PORT}
Restart=on-failure
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF
}

start_service() {
  say "Starting SamWizard..."
  systemctl daemon-reload
  systemctl enable samwizard
  if ! systemctl restart samwizard; then
    systemctl status samwizard --no-pager || true
    fail "the service did not start. Review the status output above."
  fi
}

detect_local_ip() {
  ip -4 route get 1.1.1.1 2>/dev/null | awk '{for (i=1; i<=NF; i++) if ($i == "src") {print $(i+1); exit}}'
}

print_firewall_note() {
  if command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -qi '^Status: active'; then
    say "Note: UFW appears active. If the page does not open from Windows, allow port ${PORT}/tcp."
    say "Example: sudo ufw allow ${PORT}/tcp"
  fi
}

main() {
  require_root
  load_os_release
  require_systemd
  install_prerequisites
  download_and_extract_app
  install_python_requirements
  write_environment_file
  write_service_file
  start_service

  server_ip="$(detect_local_ip)"
  if [ -z "${server_ip}" ]; then
    server_ip="$(hostname -I 2>/dev/null | awk '{print $1}')"
  fi

  say ""
  say "SamWizard Beta is ready."
  if [ -n "${server_ip}" ]; then
    say "Open this from your Windows computer:"
    say "http://${server_ip}:${PORT}"
    say "Behind the scenes log:"
    say "http://${server_ip}:${PORT}/logs"
  else
    say "Open this server in a browser on port ${PORT}."
  fi
  print_firewall_note
}

main "$@"
