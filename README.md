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

用 Sileo / Zebra / Filza 安装，postinst 会自动 respring；没自动重启就手动 respring 一次。

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
- `postinst` 权限 0755、shebang 为 LF（无 CRLF）
- Release 资产匿名下载回来比 sha256，与 GitHub 记录的 digest 一致

**没有实机，所以下面两条只能装上后看日志确认**：

- iOS 17 上状态栏私有视图的实际命中情况 —— 判据是日志里出现 `首次上色命中：<类名>`；
  没出现但日志有「已加载」，说明这条 hook 链全没命中，需要按日志里的 `补hook子类=` 数字继续查
- iCloud 专用代理（Private Relay）会不会被误判成 VPN

