#!/usr/bin/env python3
"""校验 deb：解析 ar -> 控制/数据 tar -> 检查文件模式、control 内容、dylib 切片。"""
import hashlib
import io
import lzma
import os
import sys
import tarfile


def ar_members(data):
    if not data.startswith(b"!<arch>\n"):
        raise SystemExit("不是 ar 归档")
    off = 8
    out = []
    while off + 60 <= len(data):
        hdr = data[off:off + 60]
        name = hdr[0:16].decode().strip()
        size = int(hdr[48:58].decode().strip())
        body = data[off + 60:off + 60 + size]
        out.append((name, body))
        off += 60 + size + (size & 1)
    return out


def open_tar(body):
    if body[:6] == b"\xfd7zXZ\x00":
        return tarfile.open(fileobj=io.BytesIO(lzma.decompress(body)), mode="r:")
    if body[:2] == b"\x1f\x8b":
        return tarfile.open(fileobj=io.BytesIO(body), mode="r:gz")
    if body[:1] == b"\x5d":                       # .tar.lzma（legacy alone 格式）
        return tarfile.open(fileobj=io.BytesIO(lzma.decompress(body, format=lzma.FORMAT_ALONE)), mode="r:")
    if body[:4] == b"\x28\xb5\x2f\xfd":          # zstd，theos 新版默认用它
        try:
            from compression import zstd          # Python 3.14+
            plain = zstd.decompress(body)
        except ImportError:
            import zstandard                      # pip 包
            plain = zstandard.ZstdDecompressor().decompress(body)
        return tarfile.open(fileobj=io.BytesIO(plain), mode="r:")
    return tarfile.open(fileobj=io.BytesIO(body), mode="r:")


path = sys.argv[1]
raw = open(path, "rb").read()
print("文件:", path, len(raw), "bytes")
print("sha256:", hashlib.sha256(raw).hexdigest())

members = ar_members(raw)
print("ar 成员:", [n for n, _ in members])
assert members[0][0].startswith("debian-binary"), "首成员必须是 debian-binary"
print("debian-binary:", members[0][1])

ctl_body = [b for n, b in members if n.startswith("control.tar")][0]
dat_body = [b for n, b in members if n.startswith("data.tar")][0]

ctl = open_tar(ctl_body)
names = ctl.getnames()
print("控制成员:", names)
assert "./control" in names or "control" in names, "缺 control"
cf = ctl.extractfile("./control" if "./control" in names else "control").read().decode()
print("--- control ---")
print(cf.strip())
arch = [l for l in cf.splitlines() if l.startswith("Architecture:")][0].split(":", 1)[1].strip()
print("Architecture =", arch)

pi = "./postinst" if "./postinst" in names else ("postinst" if "postinst" in names else None)
assert pi, "缺 postinst"
info = ctl.getmember(pi)
print("postinst mode = 0o%o (可执行=%s)" % (info.mode, bool(info.mode & 0o111)))
assert info.mode & 0o111, "postinst 没有可执行位"
body = ctl.extractfile(pi).read()
print("postinst 首行:", body.splitlines()[0])
assert body.startswith(b"#!/bin/sh\n"), "postinst shebang 或换行符不对（应为 LF）"
assert b"\r\n" not in body, "postinst 含 CRLF，设备上会执行失败"

dat = open_tar(dat_body)


def norm(n):
    while n.startswith("./"):
        n = n[2:]
    return n.lstrip("/")


dnames = [(norm(m.name), m.mode, m.name) for m in dat.getmembers() if m.isfile()]
print("--- 包内文件 ---")
for n, m, _raw in dnames:
    print("  0o%o %s" % (m, n))

dylib = [n for n, _, _r in dnames if n.endswith(".dylib")]
plist = [n for n, _, _r in dnames if n.endswith(".plist")]
assert len(dylib) == 1 and len(plist) == 1, "dylib/plist 数量不对"

if arch == "iphoneos-arm64":
    for n in (dylib[0], plist[0]):
        assert n.startswith("var/jb/"), "%s 应位于 var/jb 下（rootless）" % n
else:
    for n in (dylib[0], plist[0]):
        assert not n.startswith("var/jb/"), "%s 不应带 /var/jb 前缀（roothide 是纯 /Library）" % n

os.makedirs("/tmp/debx", exist_ok=True)
out = "/tmp/debx/VpnTint.dylib"
with open(out, "wb") as f:
    raw_name = [r for n, _m, r in dnames if n == dylib[0]][0]
    f.write(dat.extractfile(raw_name).read())
print("已导出 dylib ->", out)
print("OK: 包结构校验通过")
