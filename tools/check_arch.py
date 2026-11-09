#!/usr/bin/env python3
"""直接解析 Mach-O/fat 头校验切片，不依赖 lipo（Linux 上不一定有）。

判据（硬性）：
  - 必须是 fat 文件，且同时含 arm64 与 arm64e 两个切片
  - arm64  : cputype 0x0100000c, cpusubtype & 0x00FFFFFF == 0
  - arm64e : cputype 0x0100000c, cpusubtype & 0x00FFFFFF == 2   （实测 0x80000002，需掩码后比）
读原始 bytes，全程不做文本清洗。
"""
import struct
import sys

CPU_ARM64 = 0x0100000C
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
        raise SystemExit("用法: check_arch.py <macho>")
    path = sys.argv[1]
    sl, total = slices(path)
    print("文件: %s (%d bytes, %d 切片)" % (path, total, len(sl)))
    got = set()
    for cpu, sub, off, sz in sl:
        eff = sub & 0x00FFFFFF
        name = "arm64e" if (cpu == CPU_ARM64 and eff == 2) else ("arm64" if (cpu == CPU_ARM64 and eff == 0) else "other")
        print("  cputype=0x%08x cpusubtype=0x%08x(有效 0x%08x) off=%d size=%d -> %s"
              % (cpu & 0xFFFFFFFF, sub & 0xFFFFFFFF, eff, off, sz, name))
        got.add((cpu, eff))

    ok = True
    for cpu, eff, label in ((CPU_ARM64, 0, "arm64"), (CPU_ARM64, 2, "arm64e")):
        if (cpu, eff) in got:
            print("OK  存在 %s 切片" % label)
        else:
            print("FAIL 缺少 %s 切片" % label)
            ok = False
    if not ok:
        sys.exit(1)
    print("OK: 双切片齐全")
    return 0


if __name__ == "__main__":
    sys.exit(main())
