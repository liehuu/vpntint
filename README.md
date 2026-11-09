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

## 工作原理

1. **状态判定**：每秒读一次 `CFNetworkCopySystemProxySettings()` 的 `__SCOPED__`，
   出现 `utun* / ipsec* / ppp* / tun* / tap* / wg*` 接口即视为 VPN 已连接。
   纯用户态 API，不需要任何 entitlement。
   - 注意：iCloud 专用代理（Private Relay）也可能建 `utun`，会被误判成 VPN。
     如果遇到，把 `IgnoreInterfaces` 里填上对应接口名（常见 `utun0`、`utun1`）即可，或直接关掉 Private Relay。
2. **上色**：hook `_UIStatusBarItemView` 的 `applyStyleAttributes:`（框架每次刷新状态栏都经过它），
   对 Wi-Fi / 蜂窝条目树内的 `UIImageView`（转 template + tintColor）和 `UILabel`（textColor）写色；
   子类若自己 override 了该方法，用 runtime 遍历所有子类补 hook，并记录替换前的有效 IMP 链式调回。
3. **还原**：首次上色前把原始 `textColor / tintColor / image` 存进关联对象，VPN 断开时逐项还原，
   不靠猜「默认是黑还是白」。
4. 每秒做一次幂等重刷（值相同不 set），压住框架自己的颜色重置。

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
    <key>TintSignalBars</key>   <true/>            <!-- 蜂窝信号格子是否也染 -->
    <key>TintTypeText</key>     <true/>            <!-- 4G/5G 文字是否也染 -->
    <key>PollInterval</key>     <real>1.0</real>   <!-- VPN 检测间隔，最小 0.3 -->
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
启动时无条件记一行：版本、宿主进程、基类是否存在、补 hook 的子类数量、当前 VPN 判定。
VPN 切换时记「已连接 / 已断开」。首次上色命中会记出被染的视图类名 —— 这一行没出现，说明 hook 没命中，
需要按日志里的「补hook子类=0」继续查（多半是私有类改名）。

- 装完没生效：先确认 `dpkg -l | grep vpntint` 有记录，再看日志有没有那行「已加载」。
- 日志有「已加载」但没「首次上色」：VPN 判定没触发，或 hook 没命中，把系统版本和日志发我。

## 构建

GitHub Actions 自动出双架构包（arm64 + arm64e，分两次编译后 fat），产物在 Releases。
本地构建：

```sh
export THEOS=~/theos
make package FINALPACKAGE=1 THEOS_PACKAGE_SCHEME=rootless   # iphoneos-arm64
make package FINALPACKAGE=1 THEOS_PACKAGE_SCHEME=roothide   # iphoneos-arm64e
```

## 已验证 / 未验证

对着产物验过的（`tools/check_arch.py`、`tools/verify_deb.py`，读原始字节）：

- dylib 双切片：arm64 cpusubtype `0x00000000`，arm64e `0x00000002`（掩码 `0x00FFFFFF` 后比较），非单切片 thin
- `LC_BUILD_VERSION` platform=iOS / minos 15.0 / sdk 16.5，`LC_CODE_SIGNATURE` 存在
- 已链接 `CFNetwork.framework`，`CFNetworkCopySystemProxySettings` 不是悬空符号
- Filter 只含 `com.apple.springboard`；roothide 包路径为 `Library/...`（无 `/var/jb`），rootless 为 `var/jb/...`
- 包内无 `postinst` / 任何 maintainer script（`tools/check_scripts.sh`，自带反向自检）
- Release 资产匿名下载回来比 sha256，与 GitHub 记录的 digest 一致

**没有实机，所以下面两条只能装上后看日志确认**：

- iOS 17 上状态栏私有视图的实际命中情况 —— 判据是日志里出现 `首次上色命中：<类名>`；
  没出现但日志有「已加载」，说明这条 hook 链全没命中，需要按日志里的 `补hook子类=` 数字继续查
- iCloud 专用代理（Private Relay）会不会被误判成 VPN

