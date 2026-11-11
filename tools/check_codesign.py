#!/usr/bin/env python3
"""校验 Mach-O 内嵌代码签名是否**真的覆盖当前字节**。

为什么需要这个（2026-10-21 实机崩溃日志给出的线索）：
  SpringBoard 崩溃栈是
      strlen <- objc::StringHashTable::tryGetIndex <- _dyld_get_objc_selector
      <- __sel_registerName <- map_images_nolock <- map_images
      <- notifyLoad <- dlopen_from <- libinjector injection_init
  异常是 EXC_BAD_ACCESS / SIGKILL - CODESIGNING / "Invalid Page"，
  故障地址落在被 dlopen 的那个 tweak dylib 的 __TEXT 里。

  含义：内核在分页校验时认为该 dylib 的 __TEXT 页与代码签名不符，
  于是直接 SIGKILL 整个进程（SpringBoard → 安全模式）。
  这个杀发生在 map_images 阶段 —— 比 dylib 自己的任何 initializer 都早，
  所以插件**一行代码都没执行，日志一条都不会有**。
  「装了没反应 + 零日志 + 进安全模式」正是这个机制的指纹。

  ldid 签名后如果又改过文件字节（改架构、lipo 后重签、二次 patch），
  CodeDirectory 里的页哈希就对不上。这个脚本逐页重算哈希来证实/排除。

用法:
  check_codesign.py <deb 或 dylib> [...]
  check_codesign.py --selftest <deb>   反向自检：改一个字节必须被抓住，改回必须复绿
"""
import io
import os
import struct
import sys
import tempfile

# ---------------------------------------------------------------- deb 解析
def ar_members(data):
    if not data.startswith(b"!<arch>\n"):
        raise SystemExit("不是 ar 归档")
    off, out = 8, []
    while off + 60 <= len(data):
        hdr = data[off:off + 60]
        name = hdr[0:16].decode("ascii", "replace").strip()
        size = int(hdr[48:58].decode("ascii").strip() or "0")
        body = data[off + 60:off + 60 + size]
        out.append((name.rstrip("/"), body))
        off += 60 + size + (size & 1)
    return out


def open_tar(body):
    """解 data.tar。直接复用 verify_release 的实现，避免第二套解压逻辑再次踩同一个坑。"""
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import verify_release

    return verify_release.open_tar(body)


def dylib_from(path):
    """从 deb 或裸 dylib 取出 (名字, 字节)。"""
    data = open(path, "rb").read()
    if data.startswith(b"!<arch>\n"):
        for name, body in ar_members(data):
            if name.startswith("data.tar"):
                tf = open_tar(body)
                for m in tf.getmembers():
                    if m.isfile() and m.name.endswith(".dylib"):
                        return os.path.basename(m.name), tf.extractfile(m).read()
        raise SystemExit("deb 里没有 dylib: " + path)
    return os.path.basename(path), data


# ---------------------------------------------------------------- Mach-O
MH_MAGIC_64 = 0xFEEDFACF
FAT_MAGIC = 0xCAFEBABE
LC_CODE_SIGNATURE = 0x1D
CSMAGIC_EMBEDDED_SIGNATURE = 0xFADE0CC0
CSMAGIC_CODEDIRECTORY = 0xFADE0C02
HASH_NAMES = {1: "sha1", 2: "sha256", 3: "sha256-truncated", 4: "sha384"}

CS_FLAGS = [
    (0x00000001, "valid"), (0x00000002, "adhoc"), (0x00000004, "get-task-allow"),
    (0x00000100, "installer"), (0x00000200, "hard"), (0x00000400, "kill"),
    (0x00002000, "restrict"), (0x00004000, "enforcement"), (0x00010000, "runtime"),
    (0x00020000, "linker-signed"), (0x10000000, "library-validation"),
]


def slices(data):
    """返回 [(cputype, cpusubtype, 切片字节)]。"""
    if len(data) < 8:
        return []
    magic = struct.unpack(">I", data[:4])[0]
    if magic == FAT_MAGIC:
        n = struct.unpack(">I", data[4:8])[0]
        out = []
        for i in range(n):
            base = 8 + i * 20
            cpu, sub, off, size, _al = struct.unpack(">IIIII", data[base:base + 20])
            out.append((cpu, sub, data[off:off + size]))
        return out
    if struct.unpack("<I", data[:4])[0] == MH_MAGIC_64:
        return [(struct.unpack("<I", data[4:8])[0], struct.unpack("<I", data[8:12])[0], data)]
    return []


def find_code_signature(sl):
    """返回 (dataoff, datasize) 或 None。"""
    ncmds = struct.unpack("<I", sl[16:20])[0]
    off = 32
    for _ in range(ncmds):
        cmd, cmdsize = struct.unpack("<II", sl[off:off + 8])
        if cmd == LC_CODE_SIGNATURE:
            dataoff, datasize = struct.unpack("<II", sl[off + 8:off + 16])
            return dataoff, datasize
        if cmdsize <= 0:
            break
        off += cmdsize
    return None


def parse_superblob(blob, want_magic=CSMAGIC_CODEDIRECTORY):
    """在 SuperBlob 里找出 CodeDirectory 子 blob 的字节。

    踩过的坑：SuperBlob 的索引项是 {slotType, offset}，slotType 是
    CSSLOT_CODEDIRECTORY(=0) 这类**槽位号**，不是子 blob 的 magic。
    第一版直接拿 slotType 跟 magic 比，于是「签名明明在」却被判成
    「找不到 CodeDirectory」—— 判据自己错了，比漏报更误导。
    正解：按 offset 取出子 blob，再读它自己的 magic。
    """
    magic, length, count = struct.unpack(">III", blob[:12])
    if magic != CSMAGIC_EMBEDDED_SIGNATURE:
        if magic == want_magic:
            return blob
        return None
    for i in range(count):
        b = 12 + i * 8
        _slot, off = struct.unpack(">II", blob[b:b + 8])
        if off + 8 > len(blob):
            continue
        sub = blob[off:]
        sub_magic = struct.unpack(">I", sub[:4])[0]
        if sub_magic == want_magic:
            return sub
    return None


def verify_slice(sl):
    """校验一个切片的页哈希。返回 (ok, 说明, 细节 dict)。"""
    import hashlib

    info = {"slicesize": len(sl)}
    cs = find_code_signature(sl)
    if not cs:
        return False, "没有 LC_CODE_SIGNATURE（未签名）", info
    dataoff, datasize = cs
    info["sigsize"] = datasize
    if dataoff + datasize > len(sl):
        return False, "签名超出切片范围（dataoff+size > size）", info
    cd = parse_superblob(sl[dataoff:dataoff + datasize])
    if not cd:
        return False, "签名里找不到 CodeDirectory", info

    # CodeDirectory 头：9 个 u32 + hashSize/hashType/platform/pageSize 四个 u8 = 40 字节。
    # pageSize 是**指数**（cd[39]），不是字节数，别直接当大小用。
    (magic, length, version, flags, hashOffset, identOffset, nSpecial,
     nCode, codeLimit, hashSize, hashType,
     _platform, pageExp) = struct.unpack(">IIIIIIIIIBBBB", cd[:40])
    pageSize = (1 << pageExp) if pageExp < 32 else 0
    ident = cd[identOffset:cd.find(b"\x00", identOffset)].decode("utf-8", "replace") if identOffset else "?"
    info.update({
        "ident": ident, "flags": flags, "nCode": nCode, "codeLimit": codeLimit,
        "hashSize": hashSize, "hashType": hashType, "pageSize": pageSize,
        "version": hex(version),
    })
    info["flags_text"] = [n for bit, n in CS_FLAGS if flags & bit] or ["(none)"]

    hname = HASH_NAMES.get(hashType)
    if not hname or hashType not in (1, 2, 3):
        return False, "不认识的 hashType=%d" % hashType, info

    if codeLimit > len(sl):
        return False, "codeLimit(%d) > 切片大小(%d)" % (codeLimit, len(sl)), info
    if not pageSize:
        return False, "pageSize 非法", info

    bad = []
    covered = min(codeLimit, len(sl))
    for i in range(nCode):
        start = i * pageSize
        end = min(start + pageSize, covered)
        if start >= covered:
            bad.append((i, "超出 codeLimit"))
            continue
        h = hashlib.new(hname, sl[start:end]).digest()[:hashSize]
        slot = hashOffset + i * hashSize
        want = cd[slot:slot + hashSize]
        if h != want:
            bad.append((i, "页%d 哈希不符 expected=%s got=%s"
                        % (i, want.hex()[:16], h.hex()[:16])))
    info["bad_pages"] = len(bad)
    if bad:
        return False, "有 %d 页哈希不符（签名不覆盖当前字节）" % len(bad), dict(info, first_bad=bad[:3])
    return True, "签名有效（%d 页全部匹配）" % nCode, info


def check_one(path):
    name, data = dylib_from(path)
    sls = slices(data)
    print("=" * 70)
    print("文件: %s | dylib=%s | %d bytes | 切片数=%d"
          % (os.path.basename(path), name, len(data), len(sls)))
    ok_all = True
    if not sls:
        print("  FAIL 解析不出任何 Mach-O 切片")
        return False
    for cpu, sub, sl in sls:
        arch = "arm64e" if (sub & 0x00FFFFFF) == 2 else ("arm64" if (sub & 0x00FFFFFF) == 0 else "cpu0x%x/sub0x%x" % (cpu, sub))
        ok, msg, info = verify_slice(sl)
        print("  [%s] %s" % (arch, msg))
        for k in ("ident", "flags", "flags_text", "codeLimit", "nCode", "pageSize", "hashSize", "sigsize"):
            if k in info:
                print("        %-12s %s" % (k, info[k]))
        if "first_bad" in info:
            for pg, why in info["first_bad"]:
                print("        BAD %s" % why)
        if not ok:
            ok_all = False
    return ok_all


def selftest(path):
    """反向自检：改一个字节必须被抓住；改回必须复绿。

    不这么做的话，「签名有效」可能只是因为它根本没算哈希。
    """
    name, data = dylib_from(path)
    sls = slices(data)
    if not sls:
        print("FAIL 无法解析切片")
        return 1
    cpu, sub, sl = sls[0]
    ok0, msg0, _ = verify_slice(sl)
    print("基线: %s -> %s" % ("OK" if ok0 else "FAIL", msg0))

    # 只在 __TEXT 覆盖范围内改字节（跳过签名本身）
    cs = find_code_signature(sl)
    limit = cs[0] if cs else len(sl)
    poisoned = bytearray(sl)
    target = min(4096, limit - 1)     # 第 0 页里、签名之前
    poisoned[target] ^= 0xFF
    ok1, msg1, _ = verify_slice(bytes(poisoned))
    print("投毒(翻转第 %d 字节): %s -> %s" % (target, "OK" if ok1 else "FAIL", msg1))

    ok2, msg2, _ = verify_slice(sl)
    print("还原: %s -> %s" % ("OK" if ok2 else "FAIL", msg2))

    good = ok0 and (not ok1) and ok2
    print()
    print("OK：反向自检有效（改字节被抓住，还原复绿）" if good
          else "FAIL：反向自检无效（判据是装饰或基线本来就坏）")
    return 0 if good else 1


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not args:
        raise SystemExit(__doc__)
    if "--selftest" in sys.argv:
        return selftest(args[0])
    bad = [p for p in args if not check_one(p)]
    print()
    if bad:
        print("FAIL：%d 个文件的代码签名无效 -> %s" % (len(bad), bad))
        return 1
    print("OK：全部文件代码签名有效")
    return 0


if __name__ == "__main__":
    sys.exit(main())