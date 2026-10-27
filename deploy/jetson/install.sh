#!/usr/bin/env bash
# Run ON the Jetson once, after `make jetson-deploy` has synced and built:
#   bash ~/ecu-telemetry/deploy/jetson/install.sh
# Installs the agent as a user service that starts at boot and restarts on failure.
set -euo pipefail
ROOT=~/ecu-telemetry
[ -x "$ROOT/firmware/build/ecu_agent" ] || { echo "build first: make jetson-deploy (from the Mac)"; exit 1; }
[ -f "$ROOT/agent.env" ] || cp "$ROOT/deploy/jetson/agent.env" "$ROOT/agent.env"
mkdir -p ~/.config/systemd/user
cp "$ROOT/deploy/jetson/ecu-agent.service" ~/.config/systemd/user/ecu-agent.service
systemctl --user daemon-reload
systemctl --user enable --now ecu-agent.service
sleep 3
systemctl --user --no-pager status ecu-agent | head -5
echo "logs: journalctl --user -u ecu-agent -f"
