#!/bin/bash
set -e

# 清理残留锁文件并启动虚拟显示器
rm -f /tmp/.X99-lock
Xvfb :99 -screen 0 1280x800x24 &
export DISPLAY=:99

# 软链数据文件到工作目录
for f in .env accounts.json state.json; do
    if [ -f "/app/data/$f" ]; then
        ln -sf "/app/data/$f" "/app/$f"
    elif [ -f "/app/$f" ]; then
        cp "/app/$f" "/app/data/$f"
        ln -sf "/app/data/$f" "/app/$f"
    fi
done

# 软链目录
for d in auths screenshots; do
    mkdir -p "/app/data/$d"
    if [ ! -L "/app/$d" ]; then
        rm -rf "/app/$d"
        ln -sf "/app/data/$d" "/app/$d"
    fi
done

# 执行命令
exec uv run autoteam "$@"
