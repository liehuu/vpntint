#!/usr/bin/env python3
"""【已停用 · 不要用于出包】在 deb 层面补 arm64e 的 ptrauth ABI 标志。

★★★ 2026-11-11 结论：这条路是错的，1.0.4 起已弃用。
    现在只发 arm64 单切片（Makefile 里 ARCHS = arm64），根本没有 arm64e 切片，
    所以本脚本在当前产物上是 no-op。保留它只为取证与历史对照。

为什么弃用（比 fix_arm64e_abi.py 的说明更完整）：
  1. CI 日志里链接器明确警告：
       ld: warning: object file ...Tweak.m.17ed827e.o was built with an
                   incompatible arm64e ABI compiler
     —— 编译器产出的**机器码**就是老 ABI，不是头字段漏标。
  2. 所以补 caps=0x80 不改任何一条指令，只是把标签改成「新 ABI」。
     这样的二进制在任何需要 ptrauth 签名的调用边界上都会失败，
     而且崩点比原来的加载期拒绝更晚、更隐蔽、更难定位。
  3. 正解：不产生 arm64e 切片。arm64e 设备上 dyld 走 arm64 兼容加载路径，
     完全不经过 ptrauth。状态栏染色是纯 UI 逻辑，无性能代价。

--check-only 保留：它只读，用来复核某个二进制缺不缺 0x80，仍有用。

残余价值：若将来换到真能产出 versioned arm64e 的工具链，本脚本的
「补字节 → 重签 → 逐页复核 → 重打包后逐文件比对」流程可直接复用。

原始说明：
  本机无法验证 `-fptrauth-abi-version=0` 之类 flag 在 CI 工具链上是否存在。
  所以改成构建后确定性处理：改 4 个字节 → 重签 → 逐项复核。

  顺序必须是「改字节 → 重签」：
  实测补 caps 后 CodeDirectory 第 0 页哈希立刻失效
  （expected=bf4207… got=b51ea7…）。旧顺序（先签后改）会被内核
  以 SIGKILL - CODESIGNING 杀掉。

用法:
  fix_deb_abi.py <deb> [<deb> ...] --check-only        只报告（推荐）
  fix_deb_abi.py <deb> --ldid /path/to/ldid --i-know-the-risk   实际修改
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


def orig_compressor(deb):
    """从原包里探测 data.tar 的压缩格式，重打包时优先沿用。

    为什么不统一用 zstd：dm.pl 给 rootless 包用的是 lzma、roothide 包才是 zstd。
    重打包时无条件改成别的格式，等于**顺手换掉了设备端的解压依赖** ——
    老 dpkg 不认新格式就装不上，而这个问题只在设备上暴露，本地与 CI 全绿也看不出来。
    压缩格式不是本次要改的东西，能保持就保持。
    """
    try:
        raw = open(deb, "rb").read()
        for name, _body in verify_release.ar_members(raw):
            if name.startswith("data.tar."):
                return name.split("data.tar.", 1)[1]
            if name == "data.tar":
                return "none"
    except Exception:
        pass
    return None


def tar_file_map(tf):
    """{归一化路径: sha256}，只统计普通文件。用于重打包前后逐文件比对。"""
    import hashlib
    m = {}
    for mem in tf.getmembers():
        if mem.isfile():
            m[verify_release.norm(mem.name)] = hashlib.sha256(
                tf.extractfile(mem).read()).hexdigest()
    return m


def repack(workdir, out_deb, comp=None):
    """把解开后的目录树重新打成 deb。优先沿用原压缩格式。

    dpkg-deb 对 lzma 的支持视版本而定（新版已弃用），所以按 comp -> xz -> zstd
    -> 默认 依次尝试。**格式变了不算失败**，因为真正判据在后面：
    重打包后解开，除被改的 dylib 外所有文件必须与原包逐字节一致。
    """
    ctl = os.path.join(workdir, "DEBIAN")
    if not os.path.isdir(ctl):
        raise SystemExit("解包后没有 DEBIAN 目录，无法重新打包")
    for name in os.listdir(ctl):                    # 清掉旧的签名/校验残留
        if name in ("md5sums",):
            os.remove(os.path.join(ctl, name))

    base = ["dpkg-deb", "--root-owner-group"]
    order = []
    for c in [comp, "xz", "zstd", "gzip", None]:
        if c not in order:
            order.append(c)

    r = None
    for c in order:
        cmd = base + (["-Z", c] if c else []) + ["-b", workdir, out_deb]
        r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        if r.returncode == 0:
            got = orig_compressor(out_deb)
            print("  重打包压缩: 请求 %s -> 实际 %s" % (c or "default", got))
            return got
    raise SystemExit("dpkg-deb -b 全部尝试失败: %s"
                     % r.stdout.decode(errors="replace")[:400])


def process(deb, ldid=None, check_only=False):
    print("=" * 72)
    print(deb)
    # 必须在覆盖原文件之前探测压缩格式，否则重打包时无从得知原来是 xz 还是 zstd
    comp = orig_compressor(deb)
    print("  原 data.tar 压缩格式: %s" % (comp or "未知"))
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

        got_comp = repack(work, deb, comp)
        print("  已重新打包:", deb)
    finally:
        shutil.rmtree(work, ignore_errors=True)

    # 复核 3：重打包后必须是「只多了 ABI 修复」的包，其余文件逐字节不变。
    # 这条是真正的判据 —— 压缩格式变了不算问题（设备端 dpkg 能解就行），
    # 但**内容**变了就是事故：dpkg-deb -b 可能改变文件权限/丢文件/换路径。
    ctx2 = verify_release.parse_deb(deb)
    old_files = tar_file_map(tf)
    new_tf = verify_release.open_tar(
        [b for n, b in verify_release.ar_members(open(deb, "rb").read())
         if n.startswith("data.tar")][0])
    new_files = tar_file_map(new_tf)

    missing = sorted(set(old_files) - set(new_files))
    added = sorted(set(new_files) - set(old_files))
    changed = sorted(k for k in set(old_files) & set(new_files)
                     if old_files[k] != new_files[k])
    if missing or added:
        print("  FAIL：重打包后文件集合变了 缺=%s 多=%s" % (missing, added))
        return 1
    # 唯一允许变的是那个 dylib（补了 caps + 重签）
    expect = [verify_release.norm(mem.name)]
    unexpected = [k for k in changed if k not in expect]
    if unexpected:
        print("  FAIL：重打包后非预期文件被改动: %s" % unexpected)
        return 1
    if changed != expect:
        print("  FAIL：dylib 未被改动（补丁没落盘？）changed=%s" % changed)
        return 1
    print("  OK：重打包后仅 dylib 变动，其余 %d 个文件逐字节一致"
          % (len(old_files) - 1))

    # 复核 4：重新打包后的包必须整体通过产物门禁的 ABI 那一条
    fails = [m for cid, m in verify_release.run_checks(ctx2, verbose=False)
             if cid == "arm64e-abi"]
    if fails:
        print("  FAIL：重打包后 arm64e-abi 仍失败：%s" % fails[0])
        return 1
    print("  OK：重打包后 arm64e-abi 断言通过（压缩 %s）" % got_comp)
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
    # ★ 2026-11-11：本脚本已停用。只读的 --check-only 放行，实际修改必须显式确认。
    #   不加这道闸，「跑一下修修看」会把老 ABI 的机器码标成新 ABI，
    #   崩溃点从加载期推迟到某个不确定的调用边界，反而更难查。
    if not check_only and "--i-know-the-risk" not in sys.argv:
        print("FAIL：fix_deb_abi.py 已于 1.0.4 停用，禁止直接执行修改。")
        print("      原因：CI 链接器警告编译器产出的是老 arm64e ABI；")
        print("      补 caps 只改标签不改代码，会让二进制声称新 ABI 却按老 ABI 跑。")
        print("      正解：Makefile 里 ARCHS = arm64（只发单切片），dyld 走兼容路径。")
        print("      取证可用：fix_deb_abi.py <deb> --check-only")
        return 1
    rc = 0
    for p in paths:
        rc |= process(p, ldid=ldid, check_only=check_only)
    print()
    print("OK：全部处理完成" if rc == 0 else "FAIL：有 %d 个包处理失败" % rc)
    return rc


if __name__ == "__main__":
    sys.exit(main())