# VpnTint

VPN 连接时把状态栏的 **Wi-Fi 图标 / 蜂窝信号 / 4G-5G 字样** 染成橙色，断开后恢复系统默认颜色。

- 适用：iOS 15 ~ 17（含 iOS 17 + RootHide / roothide bootstrap）
- 注入目标：仅 `com.apple.springboard`（iOS 13 起应用内状态栏也由 SpringBoard 渲染，无需注入 App）
- 无依赖库、无设置面板，配置走一个 plist

> **⚠️ 请勿再使用 v1.0.2 / v1.0.3。** 两版都会让 SpringBoard 进安全模式。
> 当前版本 **1.0.4**：修掉了 1.0.3 仍然崩的两个静态层缺陷（arm64e 老式 ABI、日志不可写）。

## 安装

| 越狱形态 | 用哪个包 |
|---|---|
| Dopamine / palera1n rootless（有 `/var/jb`） | `vpntint-rootless.deb` |
| RootHide / roothide bootstrap（无 `/var/jb`） | `vpntint-roothide.deb` |

用 Sileo / Zebra / Filza 安装，**装完手动点一次 respring**。
包里故意不带 postinst —— 原因见下面「为什么包里不带 postinst」。

## 装完先看日志（1.0.4 起真正可读）

1.0.3 的「找不到 `VpnTint.log`」是个假线索：旧版只往 `/var/mobile/Library/Logs/`
和 `/tmp/` 写，SpringBoard 沙箱两个都拒绝，而且写失败完全静默。
更糟的是 `NSLog` 兜底被放在「文件是否可用」判断**之后** —— 文件写不了，系统日志也一起哑掉。
所以**「没有日志」这件事，本身就说明不了插件有没有被加载**。

1.0.4 改成：

- 优先 `NSHomeDirectory()/Documents/VpnTint.log`（进程自己的沙箱一定可写），
  再依次试 `/var/mobile/Library/Logs/`、`/var/mobile/Documents/`、`/var/mobile/Media/`、
  `/tmp/`、`/var/tmp/`；每个候选都**真的试开一次写句柄**，`fileExists` 不算数；
- `NSLog` 提到判断之前，系统日志永远有；
- 启动打 `[0/7]` … `[7/7]` 分步点。

判读方式：

| 日志内容 | 含义 |
|---|---|
| 有 `[0/7]`，无 `[7/7]` | 已被加载，卡在中间某步 —— 按最后一条编号定位 |
| 一条都没有 | 根本没被加载，回到注入/签名层 |
| 有 `[7/7]` 但不上色 | 功能层问题 |

## 如果之前装过 1.0.2 / 1.0.3 / 已经进了安全模式

安全模式 = SpringBoard 崩溃后注入器把 tweak 全部禁用了，不是「插件没生效」。按顺序处理：

1. 在安全模式里用 Sileo 卸载 `com.liehuu.vpntint`；卸不掉就在 NewTerm（`su` root，默认 `alpine`）里禁用两个文件后重启桌面：

   ```sh
   D="$(jbroot)/Library/MobileSubstrate/DynamicLibraries"
   mv "$D/VpnTint.dylib" "$D/VpnTint.dylib.disabled"
   mv "$D/VpnTint.plist" "$D/VpnTint.plist.disabled"
   killall -9 SpringBoard
   ```

   roothide 下 `$(jbroot)` 由注入环境提供；不要手写 `.jbroot-<HASH>` 路径。
2. 正常进桌面后装 1.0.4，**手动 respring**。
3. 若仍崩，不用卸载也能自证：把 `HookLevel` 设成 `0` 再 respring（见「配置」）。
   这一档连私有框架都不加载、一条 hook 都不装，插件等于完全不存在。
4. 请把这两份东西发回来定位：
   - `/var/mobile/Library/Logs/CrashReporter/` 里的 `SpringBoard-*.ips`
   - `VpnTint.log`（先找 SpringBoard 沙箱的 Documents 目录）

## 为什么包里不带 postinst（1.0.1 起）

1.0.0 的 `postinst` 里有一句 `killall -9 SpringBoard`，装完确实自动重启了桌面，代价是：

1. dpkg 在 **configure 阶段同步执行** postinst，这时 Sileo 正在前台持有这次安装会话；
2. `killall -9` 是 SIGKILL 硬杀 SpringBoard，Sileo 作为 SpringBoard 托管的 App 被一起杀掉，
   安装会话被掐断，dpkg 停在「已解包、未配置」；
3. roothide / RootHide 下 SpringBoard 被 -9 后要重新注入 jailbreak 环境，这一步不干净就是
   SpringBoard 崩溃循环 → 只能硬重启，dpkg 数据库彻底停在 configure 中间；
4. 下次打开 Sileo，检测到 dpkg 状态不一致 → 报「dpkg 被中断」。

所以 1.0.1 起：

- 包内**不带任何 maintainer script**，respring 交给包管理器或用户；
- CI 加了硬断言 `tools/check_scripts.sh`：maintainer script 里出现
  `killall / sbreload / respring / launchctl / SpringBoard` 直接构建失败；
- 这个断言有**反向自检**（先喂一个含 `killall -9 SpringBoard` 的假 postinst，必须被拦下才算检查有效），
  不会退化成永远通过的装饰；
- 另外显式断言包里不存在 `postinst`。

## 已经踩到「dpkg 被中断」怎么恢复

装 1.0.0 后遇到的，按这个顺序处理（NewTerm 里 `su` 到 root）：

```sh
# 1. 看有没有残留的 dpkg 进程，有就先等它自己结束
ps -A | grep dpkg

# 2. 把没走完的 configure 收尾（Sileo 里那个「修复」按钮本质就是这条）
dpkg --configure -a

# 3. 如果报 lock 被占用：确认第 1 步没有 dpkg 进程后再删锁
rm -f /var/lib/dpkg/lock /var/lib/dpkg/lock-frontend

# 4. 如果 vpntint 卡在 half-configured，强制移除
dpkg --remove --force-remove-reinstreq com.liehuu.vpntint

# 5. 再装当前版本（1.0.1 起任何版本都没有 postinst，不会再碰 SpringBoard）
```

在 NewTerm 这类已经注入越狱环境的 App 里，`/var/lib/dpkg` 会被自动重定向到 jbroot 内的真实路径。
若 `dpkg` 命令找不到，用 `$(jbroot)/usr/bin/dpkg` 代替。

## 版本历史与三类根因

| 版本 | 实机表现 | 根因 |
|---|---|---|
| 1.0.0 / 1.0.1 | 装完**完全没效果** | hook 目标类错（iOS 17 已迁到 `StatusStatusUI` / `STUIStatusBar*`）；染色方式错（信号视图自绘，写 `tintColor` 无效） |
| 1.0.1 起 | 「dpkg 被中断」修复 | 1.0.0 的 postinst 里 `killall -9 SpringBoard` 打断 dpkg configure |
| 1.0.2 | **装完进安全模式** | hook 框架的继承链递归（见下） |
| 1.0.3 | 修了递归，但**实机仍然进安全模式，且无日志、无新 ips** | arm64e 切片用的是老式 ABI（caps=0x00）；日志路径在 SpringBoard 沙箱不可写且失败静默（见下） |
| **1.0.4** | 修上面两个静态层缺陷；染色逻辑沿用 | CI 里补 `CPU_SUBTYPE_PTRAUTH_ABI` 后重签 + 日志多候选试写 + `NSLog` 提前 + `[0/7]…[7/7]` 打点 |

### 1.0.3 为什么「装了没反应、也没崩」

三个症状同时成立——进安全模式、找不到日志、**没有新的 `SpringBoard-*.ips`**——
指向的不是运行时崩溃，而是**加载期就被拒**。静态层查出两个具体缺陷：

**（1）arm64e 切片是老式 ABI（头号嫌疑）**

实测 1.0.3 的 arm64e 切片 `cpusubtype = 0x00000002`，高 8 位 caps = `0x00`，
即缺 `CPU_SUBTYPE_PTRAUTH_ABI(0x80)`。这是 iOS 12/13 时代的老 arm64e ABI。
LLVM 作者（llvm-project#104650）原话：

> Modern arm64e requires versioned binaries: a binary with 0x00 caps in
> cpusubtype is now rejected by the linker and everything after.

dyld 在 arm64e 进程里会**优先挑 arm64e 切片**，于是加载的就是这一片。
这与「装上进安全模式 + 一行代码没跑 + 零日志」完全吻合。

修法有个必须守住的顺序：补 caps 会改 4 个字节，而它就落在**被签名的 `__TEXT` 第 0 页**里
（实测 `__TEXT` 覆盖文件偏移 0..49152，caps 在偏移 8）。
所以补完页哈希立刻失效（实测 `pages[0]` 从 `bf4207…` 变成 `b51ea7…`），
**必须先补字节、再 `ldid -S` 重签**。反过来做会被内核以
`SIGKILL - CODESIGNING` 杀掉，正是崩溃日志里那个 `termination.namespace=CODESIGNING`。

**（2）日志路径在沙箱里根本写不进去**

见上面「装完先看日志」。旧实现只有两个候选路径，且 `NSLog` 被条件挡在后面。

这两条以前没被抓住，是因为门禁自己把要查的那一位掩掉了：
`check_arch.py` 和 `verify_release.py` 都用 `sub & 0x00FFFFFF` 比 subtype，
**caps 正好在被掩掉的 8 位里**，于是「arm64e 存在」永远为真、
「这个 arm64e 是不是现代 ABI」从来没被检查过。1.0.4 单独取 caps 判 flags，
并加了反向投毒证明这条断言真会失败。

### 1.0.2 为什么会崩（这次必须记住）

这是 SpringBoard 崩溃，不是「没生效」。`@try/@catch` 拦不住 `SIGSEGV` 和栈溢出。

1. **继承链递归（主因）**：同一个替换实现被装到「基类 + 全部子类」，而原 IMP 是按
   `object_getClass(self)` 沿继承链查的。子类 override 调 `[super setActiveColor:]` 时
   `self` 仍是子类实例，会查回子类自己的 orig → 无限递归 → 栈溢出。
2. **constructor 阶段全量遍历类表**：`objc_copyClassList` + 对大量私有类调
   `class_getInstanceMethod`，可能触发 `+initialize`，宿主尚未就绪就崩。
3. **补染 / 还原直取替换后的 IMP**：`methodForSelector` 拿到的是我们换进去的实现，
   直接调用会二次进入 hook。

1.0.3 的结构性修复：

- 每个 `(类, SEL)` 一个**独立 trampoline**，原 IMP 精确绑到自己的槽位；调回原实现只从
  槽位取 `orig`，**绝不按 `self` 的类查表**。
- **记录与替换分两阶段**：先把所有系统原始 IMP 记完，再统一 `class_replaceMethod`。
  若边记录边替换，两个被 hook 的类存在继承关系时，后装的会把 orig 记成前一个 trampoline。
- 只 hook 精确命名的具体类，不做全量类表遍历；基类兜底与具体类**互斥**，不同时装。
- 初始化从 `constructor` 延迟到主队列；补染 / 还原改走 `objc_msgSend`。
- 递归深度保险；染色逻辑异常时**原样透传**，绝不把系统这次状态栏更新吞掉。
- `HookLevel=0` 时连私有框架都不 dlopen，提供不用卸载就能自证的降级路径。

## 工作原理（1.0.3）

### 入口与染色（1.0.2 已修对，1.0.3 沿用）

iOS 17 起状态栏迁到私有框架 `StatusStatusUI.framework`，类名从 `_UIStatusBar*` 换成
`STUIStatusBar*`；且信号视图是**自绘**的，颜色来自 `activeColor` / `inactiveColor`，
写 `tintColor` 完全无效。

| | iOS 16 及更早（UIKitCore） | iOS 17+（StatusStatusUI） |
|---|---|---|
| 数据 | `_UIStatusBarData.vpnEntry` | `STStatusBarData.vpnEntry` |
| 视图 | `_UIStatusBar*View` | `STUIStatusBar*View` |

启动时 `dlopen` 该私有框架，确保 SpringBoard 早期 `objc_getClass` 能取到类。
1.0.3 只 hook 精确类名：`STUIStatusBar`、`STUIStatusBarStringView`、
`STUIStatusBarWifiSignalView`、`STUIStatusBarCellularSignalView`；
具体子类一个都取不到时才退到 `STUIStatusBarSignalView` 基类，且**不与子类同时装**。

### 五条机制

1. **状态判定（双源）**
   - 主源：hook `STUIStatusBar` 的 `_updateWithAggregatedData:` 与
     `_updateWithData:completionHandler:`，读本轮数据里的 `vpnEntry.enabled`。
   - 兜底：每秒读一次 `CFNetworkCopySystemProxySettings()` 的 `__SCOPED__`，出现
     `utun* / ipsec* / ppp* / tun* / tap* / wg*` 即视为已连接。只在主源从未命中时启用。
     纯用户态 API，不需要 entitlement。
   - 注意：iCloud 专用代理（Private Relay）也可能建 `utun`，会被误判。遇到就把接口名
     填进 `IgnoreInterfaces`（常见 `utun0`、`utun1`），或关掉 Private Relay。
2. **上色**
   - Wi-Fi 图标 / 蜂窝信号格：`setActiveColor:` / `setInactiveColor:` 改传入的颜色，
     以及 `applyStyleAttributes:` 改出参副本里的 `imageTintColor` / `imageDimmedTintColor`。
   - `4G / 5G / LTE` 字样：`applyStyleAttributes:` 改出参副本的 `textColor`，
     `setText:` 之后补写 `textColor`，按文本白名单过滤，运营商名 / 通知文字不动。
   - 每个 hook 点**必然且只调一次**系统原实现；算入参出错就原样透传。
3. **保留 alpha**
   `colorWithAlphaComponent:` 是把 alpha **替换**成给定值，不是叠加。系统给未激活信号格的
   alpha 接近 1，压到 0.2 后：深色壁纸 → 近乎全黑，浅色壁纸 → 近乎全白，两种背景下都
   「看不见」。所以只换 RGB，alpha 沿用原色。
4. **启动闸门 500ms**
   respring 后主屏状态栏实例在启动瞬间建成，此刻介入会把它建成残缺状态，且 iOS 17 之后
   不会重建。闸门内只记录视图、不写颜色，开闸后补染一次。
5. **断开后主动写回原色**
   染色是「一次性下发」：断开 VPN 不改变信号格数 / 强度 / 网络类型文字，系统不会重发样式，
   上一轮的橙色就留在屏幕上。所以账本里记下系统原色，检测到「染色 → 断开」跳变时逐个写回。
   还带残留识别与深浅色外观比对。

## 配置（可选）

路径：`/var/mobile/Library/Preferences/com.liehuu.vpntint.plist`。
除 `HookLevel` 外大多可热生效（有 vnode 监听）；**改 `HookLevel` 必须 respring**，
因为 hook 只在启动时安装。没有这个文件就走默认值。

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Enabled</key>          <true/>            <!-- 总开关 -->
    <key>HookLevel</key>        <integer>3</integer> <!-- 0/1/2/3，见下；改完要 respring -->
    <key>ColorHex</key>         <string>FF9500</string>  <!-- 目标色，支持 RRGGBB / AARRGGBB -->
    <key>TintSignalBars</key>   <true/>            <!-- Wi-Fi / 信号格是否也染 -->
    <key>TintTypeText</key>     <true/>            <!-- 4G/5G 文字是否也染 -->
    <key>CFFallback</key>       <true/>            <!-- 状态栏数据拿不到时用系统代理设置兜底 -->
    <key>PollInterval</key>     <real>1.0</real>   <!-- 兜底轮询间隔，最小 0.3 -->
    <key>IgnoreInterfaces</key>                    <!-- 不计入 VPN 的接口名前缀 -->
        <array><string>utun0</string></array>
</dict>
</plist>
```

`HookLevel` 是这次专门加的**分级定位开关**，出问题时按 3 → 2 → 1 → 0 逐档降，不用卸载：

| 值 | 装什么 | 用途 |
|---|---|---|
| `0` | 什么都不装，连 `dlopen` 都不做 | 确认崩溃是否来自本插件（最安全） |
| `1` | 仅 `STUIStatusBar` 数据层 | 只验 VPN 判定 |
| `2` | + `STUIStatusBarStringView` 文字层 | 只验 4G/5G 字样 |
| `3` | + 信号层（默认） | 完整功能 |

写文件时注意属主，直接 Filza 建或者：

```sh
su mobile -c 'defaults write /var/mobile/Library/Preferences/com.liehuu.vpntint HookLevel -int 0'
```

## 排错

日志：`/var/mobile/Library/Logs/VpnTint.log`（写不进去会退到 `/tmp/VpnTint.log`）。

1.0.3 的启动行长这样（`level=` 与 `hook=` 是关键）：

```
VpnTint 1.0.3 已加载 | host=SpringBoard | iOS17 | StatusStatusUI=1 | level=3 | hook=8 | VPN=0(源=无)
iOS17 hook：level=3 已装=8 条
闸门打开，当前 VPN=0
状态栏数据 hook 命中（vpnEntry=1）
首次上色（active）：STUIStatusBarWifiSignalView
```

`HookLevel=0` 时只有一行：

```
VpnTint 1.0.3：HookLevel=0，本轮不加载框架、不装任何 hook
```

按这个顺序看：

| 看到什么 | 说明什么 |
|---|---|
| 日志里**没有**「已加载」 | 插件没被注入。查 `dpkg -l \| grep vpntint`、filter plist 是否指向 `com.apple.springboard` |
| `hook=0` | 一条都没装上 —— 状态栏私有类名又变了，把日志发回来 |
| `StatusStatusUI=0` | 私有框架 dlopen 失败，iOS 17 上 hook 基本不会命中 |
| 没有「状态栏数据 hook 命中」 | VPN 状态主源没命中，只能靠代理设置兜底（`源=代理设置`） |
| 数据 hook 命中但 `vpnEntry` 恒为 0 | 你的 VPN App 可能没走系统 VPN 通道（没用 `NEPacketTunnelProvider`） |
| 连接 VPN 后出现 `首次上色（active）：…` | 染色路径已打通，此时状态栏应该已经变色 |

**「没生效」和「崩了」怎么区分**：

- 崩了：SpringBoard respring 或进安全模式，`/var/mobile/Library/Logs/CrashReporter/` 下出现
  `SpringBoard-*.ips`，栈帧里有 `VpnTint.dylib`。**先把 `HookLevel` 设 0 再 respring**，
  能正常进桌面就说明确实来自本插件；然后把 ips 和日志一起发回来。
  1.0.2 已经证明「所有 hook 都包了 @try-@catch」并不等于不会崩 —— 栈溢出和段错误抓不到。
- 没生效：SpringBoard 一切正常，日志有「已加载」，但没有「首次上色」。

## 构建

GitHub Actions 自动出双架构包（arm64 + arm64e，分两次编译后 fat），产物在 Releases。

两道闸门（都在 CI 里跑，且每条判据都做过反向自检）：

- `tools/check_source.py` —— 源码级 **52 项**。除 1.0.2 那批（hook 目标、染色方式、
  保 alpha、闸门、断开还原、双源 VPN、禁 `@available`）外，1.0.3 钉住了崩溃防护
  （槽位精确绑定 `orig`、`VTCallOrig` 不许按 `object_getClass` 查表、禁
  `objc_copyClassList` / `VTFindOrig` / `__thread`、延迟到主队列、24 个 trampoline
  编号一致、递归深度保险、两阶段安装、`prepare` 异常不吞原实现、`HookLevel=0`
  无害降级），1.0.4 又钉住观测本身：`[0/7]` 打点、`0..7` 分步齐全、`NSLog` 兜底、
  **`NSLog` 必须早于 `if (!gLogPath)` 的守卫**、`NSHomeDirectory()` + 试写探针。
  带 `--selftest`：逐一破坏 **31 条**关键机制，确认每条判据**真的会失败**。
- `tools/verify_release.py` —— 产物级 **12 项**，直接读 deb 原始字节（自己解 ar + tar，
  不依赖 dpkg）：首成员 debian-binary、Version 与仓库根 `control` 一致、无任何
  maintainer script、dylib 与 plist 各 1 个、路径前缀与 Architecture 匹配
  （rootless 带 `var/jb/`，roothide 纯 `Library/`）、dylib 可执行位、filter plist 挂
  `com.apple.springboard`、arm64 + arm64e 双切片、**arm64e 带 ptrauth ABI 标志**、
  7 个 hook 目标字符串齐全、且不含 `_UIStatusBarItemView` —— 防「源码改对了但产物没变」。
  带 `--selftest`：对真实 deb 逐条投毒 **10 条 + 2 条反向**（含「把 caps 补成 0x80
  必须变绿」），先证明断言会失败、再证明它不是永假，最后才判通过。
- `tools/check_scripts.sh` —— 独立的 maintainer script 扫描（自带反向自检）。
- `tools/check_codesign.py` —— 逐页重算 CodeDirectory 的 sha256，证明签名**真的覆盖
  当前字节**。带 `--selftest`：翻转一个字节必须被抓到，还原必须复绿。
- `tools/check_load.py` —— 加载期静态风险：依赖库是否全为系统库、未定义符号是否
  含系统不提供的那类（`___isOSVersionAtLeast`）、arm64e 的 ptrauth ABI。
- `tools/fix_deb_abi.py` —— 在 deb 层补 `CPU_SUBTYPE_PTRAUTH_ABI` 并重新签名。
  运行顺序不可反：补字节会让页哈希失效，必须「先补后签」。

CI 第 7 步专门处理 arm64e ABI：先 `--check-only` 证明「修之前确实缺标志」，
再补 + 重签，最后用 `check_codesign.py` 复核签名覆盖新字节。
若某天工具链本身就产出现代 ABI，这一步会自动变成 no-op（并打印说明）。

`data.tar.zst` 的解压是三级兜底：内置 `compression.zstd`（Python 3.14+）→
`zstandard` 模块（必须用 `stream_reader`，dpkg 的帧不带 content size）→ `zstd` 命令。
三条路都失败时会把各自的原因一起打出来，不会把「库缺失」误报成「包坏了」。

本地构建：

```sh
export THEOS=~/theos
make package FINALPACKAGE=1 THEOS_PACKAGE_SCHEME=rootless   # iphoneos-arm64
make package FINALPACKAGE=1 THEOS_PACKAGE_SCHEME=roothide   # iphoneos-arm64e
python3 tools/check_source.py --selftest && python3 tools/check_source.py
```

## 下载校验

以 GitHub Release 上的文件为准：CI 每次重编译的字节可能不同（tar 里带时间戳），
**不要拿本地构建出来的包去对哈希**。已做「Release 原文件 / Release 记录的 digest /
重新下载的文件（`?x=<ts>` 破 CDN）」三端一致比对，两个包又各跑了
`verify_release.py` 的 11 项断言 + 10 条投毒，全过。

Release：https://github.com/liehuu/vpntint/releases/tag/v1.0.3

| 包 | 大小 | sha256 |
|---|---|---|
| `vpntint-roothide.deb` | 21882 B | `ba64bb5ec1f21b6ee529a66477075575a333b4a57307a4b5e563a8e6e39b637c` |
| `vpntint-rootless.deb` | 19674 B | `8b4c02016b597fce17ab02ee271bbcf4fad6a0c6cb26b01d220139c59208dc2c` |

自己核一遍：设备上用 `sha256sum`，电脑上用 `shasum -a 256` 或
`certutil -hashfile <文件> SHA256`。

## 已验证 / 未验证

对着产物验过的（`tools/verify_release.py`，读 deb 原始字节）：

- dylib 双切片：arm64 cpusubtype `0x00000000`，arm64e `0x80000002`（掩码 `0x00FFFFFF` 后为 `2`），非单切片 thin
- `LC_BUILD_VERSION` platform=iOS / minos 15.0，`LC_CODE_SIGNATURE` 存在
- dylib 内确实包含 `STUIStatusBar` / `STUIStatusBarSignalView` / `setActiveColor:` /
  `setInactiveColor:` / `vpnEntry` / `StatusStatusUI` 等 hook 目标，且不含 `_UIStatusBarItemView`
- Filter 只含 `com.apple.springboard`；roothide 包路径为 `Library/...`（无 `/var/jb`），rootless 为 `var/jb/...`
- 包内无 `postinst` / 任何 maintainer script（`tools/check_scripts.sh`，自带反向自检）
- 源码闸门 47 项 + 26 条投毒、产物闸门 11 项 + 10 条投毒

**没有实机，所以下面几条只能装上后看日志确认**：

- 崩溃是否真的消除 —— 判据是 respring 后能正常进桌面、日志有 `已加载` 且没有 `SpringBoard-*.ips`
- iOS 17 上信号视图的精确类名是否命中 —— 判据是 `hook=` 数字，以及连接后出现
  `首次上色（active）：<类名>`
- `vpnEntry.enabled` 是否会随你的 VPN App 变化。若恒为 0，说明该 App 没用
  `NEPacketTunnelProvider`（比如只是装了个描述文件做分流），这种情况只能换判定方式
- iCloud 专用代理（Private Relay）会不会被代理设置兜底误判成 VPN
