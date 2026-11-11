#!/usr/bin/env python3
"""直接解析 Mach-O/fat 头校验切片，不依赖 lipo（Linux 上不一定有）。

判据（硬性）：
  - 必须是 fat 文件，且同时含 arm64 与 arm64e 两个切片
      （或用 --allow-arm64-only 允许只发 arm64 单切片）
  - arm64  : cputype 0x0100000c, cpusubtype & 0x00FFFFFF == 0
  - arm64e : cputype 0x0100000c, cpusubtype & 0x00FFFFFF == 2
  - **arm64e 的 caps（高 8 位）必须含 0x80 = CPU_SUBTYPE_PTRAUTH_ABI**

为什么最后一条是硬性判据（2026-11-11 加）：
  v1.0.3 的 arm64e 切片实测 cpusubtype=0x00000002，caps=0x00。
  这是「老式、无 ptrauth ABI 版本」的 arm64e。LLVM 作者原话：
    "Modern arm64e requires versioned binaries: a binary with 0x00 caps in
     cpusubtype is now rejected by the linker and everything after."
  theos 的 clang-11 早于 ptrauth ABI 版本化（llvm#104650, 2024），
  所以它生成的 arm64e 天然带 caps=0x00。
  社区已有同症状案例（WatchPair11）：ElleKit 加载老式 arm64e 切片后
  SpringBoard 在构造函数第一条常量字符串上 EXC_BAD_ACCESS，最终以
  「只发 arm64 单切片」修好。
  注意：掩码 0x00FFFFFF 只用于**识别架构**，绝不能用来判 flags ——
  旧版本这个脚本掩完就不看 caps 了，等于把要查的那一位筛掉了。
"""
import struct
import sys

CPU_ARM64 = 0x0100000C
# arm64e 的 cpusubtype 高 8 位是 capability 字段。0x80 = CPU_SUBTYPE_PTRAUTH_ABI，
# 表示「带 ptrauth ABI 版本的二进制」。0x00 表示老式无版本 —— 会被拒绝。
CPU_SUBTYPE_PTRAUTH_ABI = 0x80
CPU_SUBTYPE_LIB64 = 0x80        # 同一个 bit 的另一个叫法（LiveContainer/Feather 用这个名）
FAT_MAGIC = 0xCAFEBABE
FAT_MAGIC_64 = 0xCAFEBABF
MH_MAGIC_64 = 0xFEEDFACF


def slices(path):
    data = open(path, "rb").read()
    if len(data) < 8:
        raise SystemExit("文件过小: %d bytes" % len(data))
    magic = struct.unpack(">I", data[:4])[0]
    out = []
    if magic in (FAT_MAGIC, FAT_MAGIC_64):
        n = struct.unpack(">I", data[4:8])[0]
        if n == 0 or n > 16:
            raise SystemExit("fat 头异常: n=%d" % n)
        off = 8
        for _ in range(n):
            if magic == FAT_MAGIC:
                cpu, sub, o, sz, _al = struct.unpack(">iiIII", data[off:off + 20])
                off += 20
            else:
                cpu, sub, o, sz, _al, _r = struct.unpack(">iiQQII", data[off:off + 32])
                off += 32
            out.append((cpu, sub, o, sz))
    elif struct.unpack("<I", data[:4])[0] == MH_MAGIC_64:
        cpu, sub = struct.unpack("<ii", data[4:12])
        out.append((cpu, sub, 0, len(data)))
        print("注意：这是单切片 thin 文件，不是 fat")
    else:
        raise SystemExit("不是 Mach-O: magic=%08x" % magic)
    return out, len(data)


def main():
    if len(sys.argv) < 2:
        raise SystemExit("用法: check_arch.py [--allow-arm64-only] <macho>")
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    allow64only = "--allow-arm64-only" in sys.argv
    if not args:
        raise SystemExit("用法: check_arch.py [--allow-arm64-only] <macho>")
    path = args[0]
    sl, total = slices(path)
    print("文件: %s (%d bytes, %d 切片)" % (path, total, len(sl)))
    got = set()
    abi_bad = []
    for cpu, sub, off, sz in sl:
        eff = sub & 0x00FFFFFF          # 只用于识别架构
        caps = (sub >> 24) & 0xFF        # 单独取出来判 flags
        name = "arm64e" if (cpu == CPU_ARM64 and eff == 2) else ("arm64" if (cpu == CPU_ARM64 and eff == 0) else "other")
        print("  cputype=0x%08x cpusubtype=0x%08x(有效 0x%08x, caps 0x%02x) off=%d size=%d -> %s"
              % (cpu & 0xFFFFFFFF, sub & 0xFFFFFFFF, eff, caps, off, sz, name))
        got.add((cpu, eff))
        if name == "arm64e" and not (caps & CPU_SUBTYPE_PTRAUTH_ABI):
            print("    !! arm64e caps=0x%02x 缺 CPU_SUBTYPE_PTRAUTH_ABI(0x80)："
                  "老式无版本 arm64e，现代注入器/内核会拒绝" % caps)
            abi_bad.append(sub & 0xFFFFFFFF)

    ok = True
    need = ((CPU_ARM64, 0, "arm64"),) if allow64only else (
        (CPU_ARM64, 0, "arm64"), (CPU_ARM64, 2, "arm64e"))
    for cpu, eff, label in need:
        if (cpu, eff) in got:
            print("OK  存在 %s 切片" % label)
        else:
            print("FAIL 缺少 %s 切片" % label)
            ok = False
    for sub in abi_bad:
        print("FAIL arm64e 切片 cpusubtype=0x%08x 缺 ptrauth ABI 标志" % sub)
        ok = False
    if not ok:
        return 1
    print("OK: 切片齐全且 arm64e 带 ptrauth ABI 标志")
    return 0


if __name__ == "__main__":
    sys.exit(main())
