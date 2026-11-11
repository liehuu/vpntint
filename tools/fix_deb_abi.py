#!/usr/bin/env python3
"""在 deb 层面修 arm64e 的 ptrauth ABI 标志，然后重新签名、重新打包、复核。

为什么不改 Makefile 里的编译 flag：
  本机无法验证 `-fptrauth-abi-version=0` 之类 flag 在 CI 工具链上是否存在。
  加了若不支持会让构建直接失败；而且「编译器默认行为」本来就不可控。
  所以改成构建后**确定性**处理：改 4 个字节 → 重签 → 逐项复核。
  若某天工具链本身就产出 caps=0x80，本脚本是 no-op（会打印「无需修改」）。

顺序必须是「改字节 → 重签」：
  实测补 caps 后 CodeDirectory 第 0 页哈希立刻失效
  （expected=bf4207… got=b51ea7…）。旧顺序（先签后改）会被内核
  以 SIGKILL - CODESIGNING 杀掉，正是「装上进安全模式」的机制。

用法:
  fix_deb_abi.py <deb> [<deb> ...] --ldid /path/to/ldid
  fix_deb_abi.py <deb> --check-only
"""
import os
import shutil
import struct
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import verify_release  # noqa: E402

CPU_ARM64 = 0x0100000C
CPU_SUBTYPE_PTRAUTH_ABI = 0x80


def arm64e_offsets(d):
    """返回 [(fat_arch cpusubtype 偏移或 None, mach_header cpusubtype 偏移)]。"""
    magic = struct.unpack(">I", d[:4])[0]
    out = []
    if magic == 0xCAFEBABE:
        n = struct.unpack(">I", d[4:8])[0]
        off = 8
        for _ in range(n):
            cpu, sub, o, _sz, _al = struct.unpack(">IIIII", d[off:off + 20])
            if cpu == CPU_ARM64 and (sub & 0x00FFFFFF) == 2:
                out.append((off + 4, o + 8))
            off += 20
    elif magic == 0xCAFEBABF:
        n = struct.unpack(">I", d[4:8])[0]
        off = 8
        for _ in range(n):
            cpu, sub, o, _sz, _al, _r = struct.unpack(">IIQQII", d[off:off + 32])
            if cpu == CPU_ARM64 and (sub & 0x00FFFFFF) == 2:
                out.append((off + 4, o + 8))
            off += 32
    elif struct.unpack("<I", d[:4])[0] == 0xFEEDFACF:
        cpu, sub = struct.unpack("<II", d[4:12])
        if cpu == CPU_ARM64 and (sub & 0x00FFFFFF) == 2:
            out.append((None, 8))
    return out


def caps_of(d, hdr_off):
    return (struct.unpack("<I", d[hdr_off:hdr_off + 4])[0] >> 24) & 0xFF


def patch_bytes(d):
    """返回 (新字节, 改动列表)。若已合规则返回 (d, [])。"""
    buf = bytearray(d)
    changes = []
    for fat_off, hdr_off in arm64e_offsets(d):
        if caps_of(d, hdr_off) & CPU_SUBTYPE_PTRAUTH_ABI:
            continue
        old = struct.unpack("<I", buf[hdr_off:hdr_off + 4])[0]
        struct.pack_into("<I", buf, hdr_off, old | 0x80000000)
        changes.append("mach_header@%d 0x%08x->0x%08x"
                       % (hdr_off, old, old | 0x80000000))
        if fat_off is not None:
            fo = struct.unpack(">I", buf[fat_off:fat_off + 4])[0]
            struct.pack_into(">I", buf, fat_off, fo | 0x80000000)
            changes.append("fat_arch@%d 0x%08x->0x%08x"
                           % (fat_off, fo, fo | 0x80000000))
    return bytes(buf), changes


def repack(workdir, out_deb):
    """把解开后的目录树重新打成 deb。优先保持原 ar 成员顺序与压缩格式。"""
    ctl = os.path.join(workdir, "DEBIAN")
    if not os.path.isdir(ctl):
        raise SystemExit("解包后没有 DEBIAN 目录，无法重新打包")
    for name in os.listdir(ctl):                    # 清掉旧的签名/校验残留
        if name in ("md5sums",):
            os.remove(os.path.join(ctl, name))
    # dpkg-deb 会自动补 md5sums；用 zstd 压 data.tar 以贴近原包体积
    cmd = ["dpkg-deb", "--root-owner-group", "-Z", "zstd", "-b", workdir, out_deb]
    r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    if r.returncode != 0:
        # 老 dpkg 不支持 zstd，退回默认压缩
        cmd = ["dpkg-deb", "--root-owner-group", "-b", workdir, out_deb]
        r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    if r.returncode != 0:
        raise SystemExit("dpkg-deb -b 失败: %s"
                         % r.stdout.decode(errors="replace")[:400])


def process(deb, ldid=None, check_only=False):
    print("=" * 72)
    print(deb)
    raw = open(deb, "rb").read()
    members = verify_release.ar_members(raw)
    dat = [b for n, b in members if n.startswith("data.tar")]
    if not dat:
        print("  FAIL：没有 data.tar")
        return 1
    tf = verify_release.open_tar(dat[0])
    dylibs = [m for m in tf.getmembers()
              if m.isfile() and m.name.endswith(".dylib")]
    if len(dylibs) != 1:
        print("  FAIL：dylib 数量 %d != 1" % len(dylibs))
        return 1
    mem = dylibs[0]
    data = tf.extractfile(mem).read()
    spots = arm64e_offsets(data)
    if not spots:
        print("  没有 arm64e 切片，跳过")
        return 0
    need = [s for s in spots if not (caps_of(data, s[1]) & CPU_SUBTYPE_PTRAUTH_ABI)]
    for _f, h in spots:
        print("  arm64e mach_header@%d cpusubtype=0x%08x caps=0x%02x"
              % (h, struct.unpack("<I", data[h:h + 4])[0], caps_of(data, h)))
    if not need:
        print("  OK：caps 已含 0x80，无需修改（本步 no-op）")
        return 0
    if check_only:
        print("  FAIL：缺 CPU_SUBTYPE_PTRAUTH_ABI，--check-only 不修")
        return 1
    if not ldid:
        print("  FAIL：必须指定 --ldid。补完 caps 必须重签，否则页哈希过期")
        return 1

    new, changes = patch_bytes(data)
    for c in changes:
        print("    " + c)

    work = tempfile.mkdtemp(prefix="vtfix-")
    try:
        ex = subprocess.run(["dpkg-deb", "-R", deb, work],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        if ex.returncode != 0:
            print("  FAIL：dpkg-deb -R 失败 %s"
                  % ex.stdout.decode(errors="replace")[:300])
            return 1
        # 找到解包后的真实 dylib 路径并写回补丁
        rel = verify_release.norm(mem.name)
        target = os.path.join(work, *rel.split("/"))
        if not os.path.isfile(target):
            print("  FAIL：解包后找不到 %s" % target)
            return 1
        with open(target, "wb") as f:
            f.write(new)
        if os.path.exists(target + ".orig"):
            os.remove(target + ".orig")

        r = subprocess.run([ldid, "-S", target],
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        print("  ldid -S rc=%d %s"
              % (r.returncode, r.stdout.decode(errors="replace").strip()[:160]))
        if r.returncode != 0:
            print("  FAIL：重新签名失败")
            return 1

        # 复核 1：签名必须覆盖新字节
        cd = subprocess.run([sys.executable,
                             os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                          "check_codesign.py"), target],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        if cd.returncode != 0:
            print("  FAIL：重签后签名仍无效")
            print(cd.stdout.decode(errors="replace")[-600:])
            return 1
        print("  OK：重签后代码签名有效")

        # 复核 2：ABI 标志必须在
        after = open(target, "rb").read()
        bad = [h for _f, h in arm64e_offsets(after)
               if not (caps_of(after, h) & CPU_SUBTYPE_PTRAUTH_ABI)]
        if bad:
            print("  FAIL：补完复核仍缺 0x80")
            return 1
        print("  OK：arm64e 已带 ptrauth ABI 标志")

        repack(work, deb)
        print("  已重新打包:", deb)
    finally:
        shutil.rmtree(work, ignore_errors=True)

    # 复核 3：重新打包后的包必须整体通过产物门禁的 ABI 那一条
    ctx = verify_release.parse_deb(deb)
    fails = [m for cid, m in verify_release.run_checks(ctx, verbose=False)
             if cid == "arm64e-abi"]
    if fails:
        print("  FAIL：重打包后 arm64e-abi 仍失败：%s" % fails[0])
        return 1
    print("  OK：重打包后 arm64e-abi 断言通过")
    return 0


def main():
    paths, ldid, check_only = [], None, "--check-only" in sys.argv
    i = 1
    while i < len(sys.argv):
        a = sys.argv[i]
        if a == "--ldid":
            ldid = sys.argv[i + 1]
            i += 2
        elif a.startswith("--"):
            i += 1
        else:
            paths.append(a)
            i += 1
    if not paths:
        raise SystemExit(__doc__)
    rc = 0
    for p in paths:
        rc |= process(p, ldid=ldid, check_only=check_only)
    print()
    print("OK：全部处理完成" if rc == 0 else "FAIL：有 %d 个包处理失败" % rc)
    return rc


if __name__ == "__main__":
    sys.exit(main())