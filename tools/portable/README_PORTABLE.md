# Portable auto-setup

No Python install needed. Steps:

1) Download the asset for your OS from **Releases** (tag `v*`) and unzip.
2) The folder contains:
   - GUI: `hc-setup-gui` (or `.exe`)
   - CLI: `hc-setup-wizard` (or `_` legacy name) plus runner scripts `run_windows.bat`, `run_macos_linux.sh`
3) Put the module in AT mode (HC-05: hold KEY/EN HIGH before power; HC-06: usually fine when not paired).
4) Connect your USB-UART (cross RX/TX, shared GND, respect voltage levels).
5) Recommended: run the GUI first:
   - Windows/macOS/Linux: double-click `hc-setup-gui` (`hc-setup-gui.exe` on Windows). It has dropdown port picker, detect, run setup, live log, và tab "Pair master/slave" để auto pair một master + một slave (hỗ trợ one-port swap).
6) Prefer CLI?
   - Windows: double-click `run_windows.bat`
   - macOS/Linux: `chmod +x run_macos_linux.sh hc-setup-wizard hc-setup-gui` then `./run_macos_linux.sh`
   - CLI accepts the same flags as the Python version (`--help`, `--list-ports`, `--detect-only`, etc.).

Notes:
- After changing baud, reopen the port at the new speed to keep issuing AT commands.
- Some HC-06 firmwares use different BAUD mappings; if `AT+BAUDx` fails, try another common baud or configure manually.
