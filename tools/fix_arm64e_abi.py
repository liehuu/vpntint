#!/usr/bin/env python3
"""把 arm64e 切片的 cpusubtype 补上 CPU_SUBTYPE_PTRAUTH_ABI(0x80)，然后重新签名。

为什么需要（2026-11-11）：
  v1.0.3 的 arm64e 切片实测 cpusubtype = 0x00000002，caps = 0x00。
  caps 是 cpusubtype 的高 8 位（不是 subtype 的一部分）：
    0x80 = CPU_SUBTYPE_PTRAUTH_ABI  —— 声明「本二进制按 ptrauth ABI 版本化」
    0x00 = 老式无版本 arm64e        —— iOS 12/13 时代的 ABI
  LLVM 作者（llvm-project#104650）原话：
    "Modern arm64e requires versioned binaries: a binary with 0x00 caps in
     cpusubtype is now rejected by the linker and everything after."
  这与本插件实机症状吻合：装上即进安全模式、零日志、无新 SpringBoard ips。
  因为 dyld 会优先挑 arm64e 切片，而那一片声明的是老 ABI。

  ★ 补 caps 会改 4 个字节，CodeDirectory 的页哈希立刻失效
    （实测 pages[0] 从 bf4207... 变成 b51ea7...）。
    所以必须**补完再签名**，不能签名后补。本脚本的顺序就是：
      补 caps → ldid -S 重签 → 复核签名有效 → 复核 ABI 正确

  ★ 安全前提：只有当编译器本身是现代 ABI（clang major >= 12）时，
    「补 caps」才是把声明改成与代码一致，而不是撒谎。
    调用方必须显式传 --clang-major N，低于 12 直接拒绝。

用法:
  fix_arm64e_abi.py <dylib> --clang-major 16 [--ldid /path/to/ldid]
  fix_arm64e_abi.py <dylib> --check-only     只报告，不改文件
"""
import os
import struct
import subprocess
import sys

CPU_ARM64 = 0x0100000C
FAT_MAGIC = 0xCAFEBABE
FAT_MAGIC_64 = 0xCAFEBABF
MH_MAGIC_64 = 0xFEEDFACF
CPU_SUBTYPE_PTRAUTH_ABI = 0x80
MIN_CLANG_MAJOR = 12


def find_arm64e(d):
    """返回 [(fat_arch 表内 cpusubtype 的绝对偏移, mach_header 内 cpusubtype 的偏移)]。"""
    magic = struct.unpack(">I", d[:4])[0]
    out = []
    if magic == FAT_MAGIC:
        n = struct.unpack(">I", d[4:8])[0]
        off = 8
        for _ in range(n):
            cpu, sub, o, _sz, _al = struct.unpack(">IIIII", d[off:off + 20])
            if cpu == CPU_ARM64 and (sub & 0x00FFFFFF) == 2:
                out.append((off + 4, o + 8))
            off += 20
    elif magic == FAT_MAGIC_64:
        n = struct.unpack(">I", d[4:8])[0]
        off = 8
        for _ in range(n):
            cpu, sub, o, _sz, _al, _r = struct.unpack(">IIQQII", d[off:off + 32])
            if cpu == CPU_ARM64 and (sub & 0x00FFFFFF) == 2:
                out.append((off + 4, o + 8))
            off += 32
    elif struct.unpack("<I", d[:4])[0] == MH_MAGIC_64:
        cpu, sub = struct.unpack("<II", d[4:12])
        if cpu == CPU_ARM64 and (sub & 0x00FFFFFF) == 2:
            out.append((None, 8))
    return out


def report(path):
    d = open(path, "rb").read()
    spots = find_arm64e(d)
    if not spots:
        print("    没有 arm64e 切片，无需处理")
        return spots, d
    for fat_off, hdr_off in spots:
        hdr_sub = struct.unpack("<I", d[hdr_off:hdr_off + 4])[0]
        fat_sub = (struct.unpack(">I", d[fat_off:fat_off + 4])[0]
                   if fat_off is not None else None)
        print("    arm64e: mach_header=0x%08x%s caps=0x%02x"
              % (hdr_sub, "" if fat_sub is None else " fat_arch=0x%08x" % fat_sub,
                 (hdr_sub >> 24) & 0xFF))
    return spots, d


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    check_only = "--check-only" in sys.argv
    clang_major = None
    ldid = None
    for i, a in enumerate(sys.argv):
        if a == "--clang-major" and i + 1 < len(sys.argv):
            clang_major = int(sys.argv[i + 1])
        if a == "--ldid" and i + 1 < len(sys.argv):
            ldid = sys.argv[i + 1]
    if not args:
        raise SystemExit(__doc__)
    path = args[0]

    print("  文件:", path)
    spots, d = report(path)
    if not spots:
        return 0

    need = [s for s in spots
            if not ((struct.unpack("<I", d[s[1]:s[1] + 4])[0] >> 24) & 0xFF)
            & CPU_SUBTYPE_PTRAUTH_ABI]
    if not need:
        print("    OK：caps 已含 0x80，无需修改")
        return 0
    if check_only:
        print("    FAIL：缺 CPU_SUBTYPE_PTRAUTH_ABI（--check-only 不修）")
        return 1

    if clang_major is None:
        print("    FAIL：必须传 --clang-major N。低于 %d 时补 caps 会是撒谎，"
              "本脚本拒绝执行" % MIN_CLANG_MAJOR)
        return 1
    if clang_major < MIN_CLANG_MAJOR:
        print("    FAIL：clang major=%d < %d，老编译器产出的真是老 ABI，"
              "不能靠改 subtype 掩盖" % (clang_major, MIN_CLANG_MAJOR))
        return 1

    buf = bytearray(d)
    for fat_off, hdr_off in need:
        old = struct.unpack("<I", buf[hdr_off:hdr_off + 4])[0]
        new = old | 0x80000000
        struct.pack_into("<I", buf, hdr_off, new)
        print("    mach_header@%d: 0x%08x -> 0x%08x" % (hdr_off, old, new))
        if fat_off is not None:
            fo = struct.unpack(">I", buf[fat_off:fat_off + 4])[0]
            struct.pack_into(">I", buf, fat_off, fo | 0x80000000)
            print("    fat_arch@%d:    0x%08x -> 0x%08x"
                  % (fat_off, fo, fo | 0x80000000))
    exe = ldid or "ldid"
    try:
        probe = subprocess.run([exe, "-h"], stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT)
    except OSError as e:
        print("    FAIL：找不到 ldid（%r）。补完 caps 必须重签，"
              "否则签名页哈希过期，加载期会被 CODESIGNING 杀掉" % (e,))
        return 1
    if probe.returncode not in (0, 1):
        print("    !! ldid -h 返回 %d（不致命，继续尝试签名）" % probe.returncode)

    open(path, "wb").write(buf)
    print("    已写入补丁后的字节")
    r = subprocess.run([exe, "-S", path], stdout=subprocess.PIPE,
                       stderr=subprocess.STDOUT)
    print("    ldid -S rc=%d %s" % (r.returncode,
                                    r.stdout.decode(errors="replace").strip()[:200]))
    if r.returncode != 0:
        print("    FAIL：重新签名失败")
        return 1

    print("    复核 ABI：")
    _spots2, d2 = report(path)
    bad = [s for s in _spots2
           if not ((struct.unpack("<I", d2[s[1]:s[1] + 4])[0] >> 24) & 0xFF)
           & CPU_SUBTYPE_PTRAUTH_ABI]
    if bad:
        print("    FAIL：补完复核仍缺 0x80")
        return 1
    print("    OK：arm64e 已带 ptrauth ABI 标志，且已重新签名")
    return 0


if __name__ == "__main__":
    sys.exit(main())