#!/bin/sh
# 检查 deb 的 maintainer scripts（DEBIAN/ 下除 control 等以外的脚本）里
# 有没有会打断 dpkg configure 阶段的重启动作。
#
# 用法: check_scripts.sh <DEBIAN 目录>
# 返回 0 = 干净；1 = 发现危险动作
#
# 为什么有这个检查：postinst 里 respring / killall SpringBoard 会把前台的
# Sileo 一起杀掉，dpkg 停在 configure 一半，用户下次打开 Sileo 就是「dpkg 被中断」。
# roothide 下 SIGKILL 还容易把 RootHide 注入链打死，进而崩溃循环、被迫硬重启。
dir="$1"
if [ ! -d "$dir" ]; then
    echo "FAIL: 目录不存在: $dir"
    exit 1
fi

bad=0
found=0
for f in "$dir"/*; do
    [ -f "$f" ] || continue
    b=$(basename "$f")
    case "$b" in
        control|md5sums|conffiles|triggers|shlibs) continue ;;
    esac
    found=$((found + 1))
    if grep -qE 'killall|sbreload|respring|launchctl|SpringBoard' "$f"; then
        echo "FAIL: $b 里出现重启桌面动作，会打断 dpkg 的 configure 阶段"
        grep -nE 'killall|sbreload|respring|launchctl|SpringBoard' "$f" | sed 's/^/      /'
        bad=1
    else
        echo "OK: $b 无重启动作"
    fi
done

if [ "$found" -eq 0 ]; then
    echo "OK: 包里没有任何 maintainer script"
fi
[ "$bad" -eq 0 ] || exit 1
exit 0
