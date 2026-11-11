#!/usr/bin/env python3
"""直接解析 Mach-O/fat 头校验切片，不依赖 lipo（Linux 上不一定有）。

判据（硬性）：
  - 必须含 arm64 切片（cputype 0x0100000c, cpusubtype & 0x00FFFFFF == 0）
  - **默认允许只发 arm64 单切片**：1.0.4 起 deliberately 只发 arm64，理由见下。
    传 --require-arm64e 才要求双切片。
  - 若存在 arm64e 切片：cpusubtype & 0x00FFFFFF == 2，**且 caps（高 8 位）
    必须含 0x80 = CPU_SUBTYPE_PTRAUTH_ABI**。这条永远硬性 —— 一旦哪天有人
    把 arm64e 加回来，缺 0x80 必须当场拦下。
  - 传 --allow-arm64-only 等价于默认（保留旧参数名，向后兼容）。

为什么默认改成单切片（2026-11-11）：
  1.0.3 的双切片里 arm64e cpusubtype=0x00000002（caps=0x00），缺
  CPU_SUBTYPE_PTRAUTH_ABI(0x80)。而 CI 链接器日志明确警告：
    ld: warning: object file ...o was built with an incompatible arm64e
                ABI compiler
  即**编译器产出的机器码本身就是老 ABI**，不是头字段漏标。
  所以「补 caps」是错修：标签改成新 ABI，代码里却没有对应的 ptrauth 签名
  指令，一旦和按新 ABI 编译的系统库交换函数指针就验签失败，崩点更隐蔽。
  只发 arm64 后，arm64e 设备上 dyld 走 arm64 兼容路径，整条 ptrauth 路径
  都不经过，问题从根上消失。社区同类症状（WatchPair11）也是这么修的。
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
        raise SystemExit("用法: check_arch.py [--require-arm64e] [--allow-arm64-only] <macho>")
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    # 1.0.4 起默认就是「只要 arm64」；--require-arm64e 才要求双切片。
    # --allow-arm64-only 保留为同义参数，避免老命令/文档失效。
    require_arm64e = "--require-arm64e" in sys.argv
    if not args:
        raise SystemExit("用法: check_arch.py [--require-arm64e] [--allow-arm64-only] <macho>")
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
    if (CPU_ARM64, 0) in got:
        print("OK  存在 arm64 切片")
    else:
        print("FAIL 缺少 arm64 切片")
        ok = False
    if require_arm64e:
        if (CPU_ARM64, 2) in got:
            print("OK  存在 arm64e 切片（--require-arm64e）")
        else:
            print("FAIL 指定了 --require-arm64e 但没有 arm64e 切片")
            ok = False
    else:
        if (CPU_ARM64, 2) in got:
            print("OK  存在 arm64e 切片（未要求，但带就必须合规）")
        else:
            print("OK  单切片 arm64（1.0.4 起默认方案，arm64e 设备走兼容加载）")
    for sub in abi_bad:
        print("FAIL arm64e 切片 cpusubtype=0x%08x 缺 ptrauth ABI 标志" % sub)
        ok = False
    if not ok:
        return 1
    if (CPU_ARM64, 2) in got:
        print("OK: 切片齐全且 arm64e 带 ptrauth ABI 标志")
    else:
        print("OK: 单切片 arm64，无 ptrauth ABI 风险")
    return 0


if __name__ == "__main__":
    sys.exit(main())
