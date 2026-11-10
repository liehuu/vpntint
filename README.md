# VpnTint

VPN 连接时把状态栏的 **Wi-Fi 图标 / 蜂窝信号 / 4G-5G 字样** 染成橙色，断开后恢复系统默认颜色。

- 适用：iOS 15 ~ 17（含 iOS 17 + RootHide / roothide bootstrap）
- 注入目标：仅 `com.apple.springboard`（iOS 13 起应用内状态栏也由 SpringBoard 渲染，无需注入 App）
- 无依赖库、无设置面板，配置走一个 plist

## 安装

| 越狱形态 | 用哪个包 |
|---|---|
| Dopamine / palera1n rootless（有 `/var/jb`） | `vpntint-rootless.deb` |
| RootHide / roothide bootstrap（无 `/var/jb`） | `vpntint-roothide.deb` |

用 Sileo / Zebra / Filza 安装，**装完手动点一次 respring**。
包里故意不带 postinst —— 原因见下面「为什么包里不带 postinst」。

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

装 1.0.0 后遇到的，按这个顺序处理（roothide / RootHide，NewTerm 里 `su` 到 root，默认密码 `alpine`）：

```sh
# 1. 看有没有残留的 dpkg 进程，有就先等它自己结束
ps -A | grep dpkg

# 2. 把没走完的 configure 收尾（Sileo 里那个「修复」按钮本质就是这条）
dpkg --configure -a

# 3. 如果报 lock 被占用：确认第 1 步没有 dpkg 进程后再删锁
rm -f /var/lib/dpkg/lock /var/lib/dpkg/lock-frontend

# 4. 如果 vpntint 卡在 half-configured，强制移除
dpkg --remove --force-remove-reinstreq com.liehuu.vpntint

# 5. 再装 1.0.1（这一版没有任何 postinst，不会再碰 SpringBoard）
```

在 NewTerm 这类已经注入越狱环境的 App 里，`/var/lib/dpkg` 会被自动重定向到 jbroot 内的真实路径，
不用手写 `.jbroot-<HASH>` 长路径。若 `dpkg` 命令找不到，用 `$(jbroot)/usr/bin/dpkg` 代替。

## 工作原理（1.0.2 重写）

### 为什么前两版完全没效果

1.0.0 / 1.0.1 装上后状态栏毫无变化，日志里也只有一行「已加载」。原因有两条，都是致命的：

**① hook 目标类选错了。** iOS 17 起状态栏迁到私有框架 `StatusStatusUI.framework`，
类名从 `_UIStatusBar*` 换成 `STUIStatusBar*`。旧版 hook 的 `_UIStatusBarItemView`
在 iOS 17 上**根本不会被实例化** —— 没有对象，hook 就不会被调用，一个触发点都没有。

| iOS 16 及更早（UIKitCore） | iOS 17+（StatusStatusUI） |
|---|---|
| `_UIStatusBarItemView` | `STUIStatusBar` / `STUIStatusBarSignalView` / `STUIStatusBarStringView` |
| `_UIStatusBarData.vpnEntry` | `STStatusBarData.vpnEntry` |

**② 染色方式选错了。** iOS 17 的信号视图是**自绘**的，颜色来自
`activeColor` / `inactiveColor` 两个属性。旧版对它们写 `tintColor` 完全无效 ——
那两个属性不是 `UIView.tintColor`，写它等于什么都没做。

这类错误的共同特征：**编译能过、包能装、日志也不报错**，只有实机看效果才发现。
所以 1.0.2 起加了两道闸门（见下面「构建」）。

### 现在的机制

1. **状态判定（双源）**
   - 主源：hook `STUIStatusBar` 的 `_updateWithAggregatedData:` 与
     `_updateWithData:completionHandler:`，读本轮数据里的 `vpnEntry.enabled`。
     这是系统自己算好的值，最准，随状态栏刷新即时更新。
   - 兜底：每秒读一次 `CFNetworkCopySystemProxySettings()` 的 `__SCOPED__`，出现
     `utun* / ipsec* / ppp* / tun* / tap* / wg*` 即视为已连接。只在主源从未命中时启用
     （万一私有类再次改名）。纯用户态 API，不需要 entitlement。
   - 注意：iCloud 专用代理（Private Relay）也可能建 `utun`，会被误判。遇到就把接口名
     填进 `IgnoreInterfaces`（常见 `utun0`、`utun1`），或关掉 Private Relay。
2. **上色**
   - Wi-Fi 图标 / 蜂窝信号格：hook `STUIStatusBarSignalView` 及其**全部子类**的
     `setActiveColor:` / `setInactiveColor:`，改传入的颜色。
   - `4G / 5G / LTE` 字样：hook `STUIStatusBarStringView` 的 `applyStyleAttributes:`
     （改出参副本）与 `setText:`（改 `textColor`），并按文本白名单过滤，
     运营商名 / 通知文字不动。
   - 子类 override 后基类 hook 拦不到，所以按「基类 + objc_copyClassList 遍历子类」逐个补，
     原 IMP 存静态数组链式调回。
3. **保留 alpha**（这条是从别人实测里学到的，很容易踩）
   `colorWithAlphaComponent:` 是把 alpha **替换**成给定值，不是叠加。系统给未激活信号格的
   alpha 接近 1，压到 0.2 后：深色壁纸 → 近乎全黑，浅色壁纸 → 近乎全白，两种背景下都
   「看不见」，表现为图标整块消失。所以只换 RGB，alpha 沿用原色。
4. **启动闸门 500ms**
   respring 后主屏状态栏实例在启动瞬间建成，此刻介入会把它建成残缺状态，且 iOS 17 之后
   不会重建。闸门内所有 hook 只透传不写色，开闸后对闸门期间见过的视图补染一次。
5. **断开后主动写回原色**
   染色是「一次性下发」：断开 VPN 不改变信号格数 / 强度 / 网络类型文字，系统不会重发样式，
   上一轮的橙色就留在屏幕上。所以账本里记下系统原色，检测到「染色 → 断开」跳变时逐个写回。
   还带残留识别（读到的色若等于我们上次染的值，就不当系统色记账）与外观比对
   （深浅色切换后旧账作废，跳过该视图）。

## 配置（可选）

路径：`/var/mobile/Library/Preferences/com.liehuu.vpntint.plist`，改完保存即热生效（有 vnode 监听），
没有这个文件就走默认值。

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Enabled</key>          <true/>            <!-- 总开关 -->
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

写文件时注意属主，直接 Filza 建或者：

```sh
su mobile -c 'defaults write /var/mobile/Library/Preferences/com.liehuu.vpntint Enabled -bool YES'
```

## 排错

日志：`/var/mobile/Library/Logs/VpnTint.log`（写不进去会退到 `/tmp/VpnTint.log`）。

1.0.2 的启动行会把**每个 hook 点的安装情况**都打出来，这一行就能定位问题：

```
VpnTint 1.0.2 已加载 | host=SpringBoard | iOS17 | StatusStatusUI=1 | hook=12 | VPN=0(源=无)
iOS17 hook：data=1 string=3 signal=2（共 12 条）
闸门打开，当前 VPN=0
```

按这个顺序看：

| 看到什么 | 说明什么 |
|---|---|
| 日志里**没有**「已加载」 | 插件没被注入。查 `dpkg -l \| grep vpntint` 是否已装、filter plist 是否指向 `com.apple.springboard` |
| `hook=0` | 一条都没装上 —— 状态栏私有类名又变了，把日志发我 |
| `data=0` | VPN 状态主源没命中，此时只能靠代理设置兜底（`源=代理设置`） |
| `StatusStatusUI=0` | 私有框架 dlopen 失败，iOS 17 上 hook 基本不会命中 |
| 有 `data=1` 但 VPN 判定始终 0 | 状态栏拿到的 `vpnEntry.enabled` 就是 0，可能是你的 VPN App 不走系统 VPN 通道（见下） |
| 连接 VPN 后出现 `首次上色（active）：…` | 染色路径已打通，此时状态栏应该已经变色 |

**「没生效」和「崩了」怎么区分**：

- 崩了：SpringBoard 会 respring 或进安全模式，`/var/mobile/Library/Logs/CrashReporter/` 下出现
  `SpringBoard-*.ips`，里面有 `VpnTint.dylib` 的栈帧。所有 hook 都包了 `@try-@catch`，
  正常路径不该崩；真崩了把 ips 发我。
- 没生效：SpringBoard 一切正常，日志有「已加载」，但没有「首次上色」。

## 构建

GitHub Actions 自动出双架构包（arm64 + arm64e，分两次编译后 fat），产物在 Releases。

两道闸门（都在 CI 里跑，且每条判据都做过反向自检）：

- `tools/check_source.py` —— 源码级 **25 项**，钉住这次的教训：hook 目标必须是
  `STUIStatusBar*`、必须 hook `setActiveColor:` / `setInactiveColor:`、不得出现
  `colorWithAlphaComponent`、`VTKeepAlpha` 必须用原 alpha、闸门必须会开、断开必须还原、
  每个 hook 必须链式调回原实现。带 `--selftest`：逐一破坏 **6 条**关键机制
  （砍 STUIStatusBar / 砍 vpnEntry / 引入 colorWithAlphaComponent / 删 `gReady = 1` /
  断开不还原 / 换回 `@available`），确认每条判据**真的会失败**
  （不做这步，闸门可能就是永远通过的装饰）。
- `tools/verify_release.py` —— 产物级 **11 项**，直接读 deb 原始字节（自己解 ar + tar，
  不依赖 dpkg）：首成员 debian-binary、Version 与仓库根 `control` 一致、无任何
  maintainer script、dylib 与 plist 各 1 个、路径前缀与 Architecture 匹配
  （rootless 带 `var/jb/`，roothide 纯 `Library/`）、dylib 可执行位、filter plist 挂
  `com.apple.springboard`、arm64 + arm64e 双切片、7 个 hook 目标字符串齐全、
  且不含 `_UIStatusBarItemView` —— 防「源码改对了但产物没变」这类假修复。
  带 `--selftest`：对真实 deb 逐条投毒 **9 条**（另加「清空 dylib」共 10 条），
  先证明断言会失败，再判通过。
- `tools/check_scripts.sh` —— 独立的 maintainer script 扫描（自带反向自检）。

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

v1.0.2 两个包的 sha256（等于 GitHub Release 记录的 digest，已下载回来比过）：

| 包 | 大小 | sha256 |
|---|---|---|
| `vpntint-roothide.deb` | 22916 B | `96a508d7e960289b5256b62cf46214218c705e54b6cb08303ef06f0016ebf6d5` |
| `vpntint-rootless.deb` | 20620 B | `163c3f537a8da9133f5bf4e12595c1efaf9ecd05812e9bed7618a4def859cdb6` |

自己核一遍：设备上用 `sha256sum`，电脑上用 `shasum -a 256` 或
`certutil -hashfile <文件> SHA256`。

注意：CI 每次重编译的字节可能不同（tar 里带时间戳），所以**以 Release 上的文件为准**，
不要拿本地构建出来的包去对这张表。

## 已验证 / 未验证

对着产物验过的（`tools/verify_release.py`，读 deb 原始字节）：

- dylib 双切片：arm64 cpusubtype `0x00000000`，arm64e `0x00000002`（掩码 `0x00FFFFFF` 后比较），非单切片 thin
- `LC_BUILD_VERSION` platform=iOS / minos 15.0 / sdk 16.5，`LC_CODE_SIGNATURE` 存在
- dylib 内确实包含 `STUIStatusBar` / `STUIStatusBarSignalView` / `setActiveColor:` /
  `setInactiveColor:` / `vpnEntry` / `StatusStatusUI` 等 hook 目标，且不含 `_UIStatusBarItemView`
- Filter 只含 `com.apple.springboard`；roothide 包路径为 `Library/...`（无 `/var/jb`），rootless 为 `var/jb/...`
- 包内无 `postinst` / 任何 maintainer script（`tools/check_scripts.sh`，自带反向自检）
- Release 资产匿名下载回来比 sha256，与 GitHub 记录的 digest 一致

**没有实机，所以下面几条只能装上后看日志确认**：

- iOS 17 上 `STUIStatusBarSignalView` 的实际子类名与数量 —— 判据是启动行里
  `signal=` 的数字（预期 ≥2：Wi-Fi 与蜂窝各一），以及连接后出现 `首次上色（active）：<类名>`
- `vpnEntry.enabled` 是否会随你的 VPN App 变化。若恒为 0，说明该 App 没用
  `NEPacketTunnelProvider`（比如只是装了个描述文件做分流），这种情况只能换判定方式
- iCloud 专用代理（Private Relay）会不会被代理设置兜底误判成 VPN

