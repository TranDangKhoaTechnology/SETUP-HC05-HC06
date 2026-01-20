#!/usr/bin/env bash
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

BIN="$DIR/hc-setup-wizard"
if [[ ! -x "$BIN" && -x "${BIN}.exe" ]]; then
  BIN="${BIN}.exe"
fi
if [[ ! -x "$BIN" && -x "$DIR/hc_setup_wizard" ]]; then
  BIN="$DIR/hc_setup_wizard"
fi
if [[ ! -x "$BIN" && -x "$DIR/hc_setup_wizard.exe" ]]; then
  BIN="$DIR/hc_setup_wizard.exe"
fi

if [[ ! -x "$BIN" ]]; then
  echo "Could not find hc_setup_wizard binary next to this script."
  echo "Expected at: $BIN"
  exit 1
fi

echo "=== HC-05 / HC-06 setup (portable) ==="
read -rp "Serial port (e.g. /dev/ttyUSB0): " PORT
if [[ -z "$PORT" ]]; then
  echo "Port is required."
  exit 1
fi

read -rp "Module type [auto/hc05/hc06, default auto]: " MODULE
MODULE=${MODULE:-auto}

read -rp "Device name (optional): " NAME
read -rp "PIN (4 digits, optional): " PIN

read -rp "Data-mode baud [default 9600]: " BAUD
BAUD=${BAUD:-9600}

read -rp "HC-05 role [slave/master, default slave]: " ROLE
ROLE=${ROLE:-slave}

cmd=( "$BIN" --port "$PORT" --baud "$BAUD" --role "$ROLE" --module "$MODULE" )
[[ -n "$NAME" ]] && cmd+=( --name "$NAME" )
[[ -n "$PIN" ]] && cmd+=( --pin "$PIN" )

echo "Running: ${cmd[*]}"
exec "${cmd[@]}"
