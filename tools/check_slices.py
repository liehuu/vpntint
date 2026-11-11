#!/usr/bin/env python3
"""确认 dylib 的切片策略符合 1.0.4 的约定，并**显式证明**没有老 ABI 的 arm64e。

为什么单独成文件而不写在 CI 的 run 里：
  workflow 里的 `run: |` 是 YAML 块标量，嵌在里面的 heredoc 内容必须跟住
  同一缩进层级。写顶格会让 YAML 解析直接失败，而**失败的 workflow 不会
  产生任何 job** —— API 上看到的是 total_count=0、日志下载 404，
  很容易被误读成「CI 挂了」而不是「YAML 写坏了」。抽成文件就没有这个雷。

判据：
  1. 必须有 arm64 切片（cputype 0x0100000c，有效 subtype 0）
  2. 不允许存在缺 PTRAUTH_ABI(0x80) 的 arm64e 切片
     —— 1.0.4 主动不发 arm64e；万一哪天 ARCHS 被改回去，这里当场拦下。
  3. 打印 arm64e 切片数量：0 是 1.0.4 的预期形态，非 0 只作提示（只要合规）。

用法:
  check_slices.py <macho> [<macho> ...]
"""
import struct
import sys

CPU_ARM64 = 0x0100000C
PTRAUTH_ABI = 0x80
FAT_MAGIC = 0xCAFEBABE
FAT_MAGIC_64 = 0xCAFEBABF
MH_MAGIC_64 = 0xFEEDFACF


def slices(data):
    """[(cputype, 有效 subtype, caps, 是否 fat)]"""
    be = struct.unpack(">I", data[:4])[0]
    le = struct.unpack("<I", data[:4])[0]
    out = []
    if be in (FAT_MAGIC, FAT_MAGIC_64):
        n = struct.unpack(">I", data[4:8])[0]
        if n == 0 or n > 32:
            raise SystemExit("fat 头异常: n=%d" % n)
        off = 8
        for _ in range(n):
            if be == FAT_MAGIC:
                cpu, sub = struct.unpack(">iI", data[off:off + 8])
                off += 20
            else:
                cpu, sub = struct.unpack(">iI", data[off:off + 8])
                off += 32
            out.append((cpu & 0xFFFFFFFF, sub & 0x00FFFFFF,
                        (sub >> 24) & 0xFF, True))
    elif le == MH_MAGIC_64:
        cpu, sub = struct.unpack("<iI", data[4:12])
        out.append((cpu & 0xFFFFFFFF, sub & 0x00FFFFFF, (sub >> 24) & 0xFF, False))
    else:
        raise SystemExit("既不是 fat 也不是 Mach-O 64：magic=%08x" % be)
    return out


def check(path):
    data = open(path, "rb").read()
    sl = slices(data)
    print("  文件: %s (%d bytes, %d 切片)" % (path, len(data), len(sl)))
    for cpu, eff, caps, _fat in sl:
        name = ("arm64e" if eff == 2 else "arm64") if cpu == CPU_ARM64 else "other"
        print("    cputype=0x%08x 有效=%d caps=0x%02x -> %s" % (cpu, eff, caps, name))

    n_arm64 = sum(1 for c, e, _, _ in sl if c == CPU_ARM64 and e == 0)
    if n_arm64 == 0:
        print("  FAIL：缺 arm64 切片")
        return 1

    bad = [(c, e, cp) for c, e, cp, _ in sl
           if c == CPU_ARM64 and e == 2 and not (cp & PTRAUTH_ABI)]
    if bad:
        print("  FAIL：存在老 ABI 的 arm64e 切片 %r（缺 0x80）" % (bad,))
        return 1

    n_e = sum(1 for c, e, _, _ in sl if c == CPU_ARM64 and e == 2)
    if n_e == 0:
        print("  OK：arm64 单切片，无 arm64e（1.0.4 的预期形态）")
    else:
        print("  OK：arm64 存在；arm64e 切片 %d 个且均带 0x80（非 1.0.4 预期，但合规）" % n_e)
    return 0


def main():
    paths = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not paths:
        raise SystemExit(__doc__)
    rc = 0
    for p in paths:
        try:
            rc |= check(p)
        except SystemExit as e:
            print("  FAIL：%s" % e)
            rc |= 1
    if rc == 0:
        print("OK：切片策略符合预期")
    return rc


if __name__ == "__main__":
    sys.exit(main())