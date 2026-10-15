#!/bin/zsh -f
set -e
harbor_script_dir="${0:A:h}"
harbor_resources="$harbor_script_dir"
if [[ ! -f "$harbor_resources/engine/main.py" ]]; then
  harbor_resources="$harbor_script_dir/Codex Harbor.app/Contents/Resources"
fi
if [[ ! -f "$harbor_resources/engine/main.py" ]]; then
  print -u2 '找不到 Codex Harbor 恢复组件，请从应用随附的恢复脚本启动。'
  exit 1
fi
for harbor_python in /opt/homebrew/bin/python3 /usr/local/bin/python3 /usr/bin/python3; do
  if [[ -x "$harbor_python" ]] && "$harbor_python" -I -B -S -c 'import sys; sys.exit(0 if sys.version_info >= (3,11) else 1)'; then
    exec "$harbor_python" -I -B "$harbor_resources/engine/main.py" --restore-macos
  fi
done
print -u2 '需要 Python 3.11 或更高版本。请安装后重试。'
exit 1
