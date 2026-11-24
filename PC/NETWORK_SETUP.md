# 连接前的网络配置说明（IPv6 文件传输工具）

> 项目总览、安装与功能说明见 [../README.md](../README.md)，手机版使用说明见 [../mobile/README.md](../mobile/README.md)。
>
> 本工具**只使用 IPv6**。两台设备之间必须能互相路由到对方的 IPv6 地址，并且要在**服务器那一端**放行 TCP 端口（默认 `9000`）。
>
> **同一 Wi-Fi 下最简单**，通常只需要放行 Windows 防火墙；跨网络（公网直连）还需要配置光猫/路由器的 IPv6 防火墙。

---

## 0. 三分钟检查清单

连接前逐项确认：

- [ ] 服务器端选了「作为服务器」，并点了「启动监听」，状态显示"监听中"
- [ ] 客户端填的是**服务器的 IPv6 地址**和**相同端口**
- [ ] 两台设备都有 IPv6 地址（不是只有 IPv4）
- [ ] 手机连的是同一个 Wi-Fi，没有走移动数据
- [ ] 光猫/路由器没有开启「AP 隔离 / 访客网络」
- [ ] Windows 防火墙已允许（首次监听时弹窗点"允许"）
- [ ] 手机端 App / Pydroid 保持在前台
- [ ] 如果是链路本地地址（`fe80::`），客户端填了正确的接口索引
- [ ] 服务器设置了连接密码时，客户端密码一致（或已导入服务器分享的连接串）

---

## 1. 先分清三种 IPv6 地址

| 类型 | 例子 | 能否跨网段 | 说明 |
|---|---|---|---|
| **全局单播地址** | `2409:8a20:...`、`240e:...`、`2408:...` | 可以 | 运营商分配，公网可路由；但默认被光猫 IPv6 防火墙挡住入站 |
| **链路本地地址** | `fe80::1869:f071:e2f4:794c%7` | 不可以 | 只在同一网段有效；必须带 `%接口索引`（Windows 的 `%7`） |
| **临时地址** | `2409:...:44a1:...` | 可以 | 隐私扩展地址，会变化；临时用可以，长期连接建议用稳定地址 |
| 回环地址 | `::1` | 不可以 | 只代表"本机自己"，填给别的设备一定连不上 |

**不要填这些地址：**

- `::1`；
- 虚拟网卡（VMware `VMnet1/8`、LetsTAP、蓝牙网络连接）上的 `fe80::…`；
- 已经显示"已弃用（Deprecated）"的地址。

> 服务器有多块网卡时，客户端要填**和客户端在同一网段**的那块网卡的地址（通常是 WLAN 的地址）。

---

## 2. 谁当服务器？

- **服务器**：监听端口，等待别人连进来；只有服务器需要"放行入站"。
- **客户端**：主动去连服务器，不需要放行入站。

两种拓扑都可以：

```
拓扑 A（推荐）：电脑当服务器 ← 手机当客户端
拓扑 B：        手机当服务器 ← 电脑当客户端
```

服务器可以同时接多个客户端；服务器也可以把文件推送给客户端。

---

## 3. 检查两端有没有 IPv6

### 3.1 Windows

打开命令提示符，运行：

```powershell
ipconfig
```

在 `无线局域网适配器 WLAN` 下应看到类似：

```
IPv6 地址 . . . . . . . . . . . . : 2409:xxxx:xxxx:xxxx:xxxx:xxxx:xxxx:xxxx
临时 IPv6 地址. . . . . . . . . . : 2409:xxxx:...
本地链接 IPv6 地址. . . . . . . . : fe80::xxxx%7
默认网关. . . . . . . . . . . . . : fe80::xxxx%7
```

**如果没有 IPv6 地址：**

1. `Win + R` → 输入 `ncpa.cpl` → 回车；
2. 右键「WLAN」→「属性」；
3. 勾选 **Internet 协议版本 6 (TCP/IPv6)** → 确定；
4. 断开 Wi-Fi 再重连，或重启电脑。

或者用管理员 PowerShell：

```powershell
Enable-NetAdapterBinding -Name "WLAN" -ComponentID ms_tcpip6
```

### 3.2 Android 手机

设置 → WLAN → 点当前 Wi-Fi → 查看「IP 地址」，应包含 IPv6 地址（`2409:...` 或 `fe80::...`）。

如果没有 IPv6：说明路由器/光猫没有下发 IPv6，继续看下一节。

### 3.3 光猫 / 路由器

登录管理页（常见 `192.168.1.1`，账号密码在设备背面），检查：

1. **状态 / 网络信息**：WAN 是否有 IPv6 地址（`240e`/`2409`/`2408` 开头）和 IPv6 前缀；
2. **网络设置 / 宽带设置**：上网那条连接是否启用了 IPv6；
3. **LAN / IPv6 设置**：是否启用 **RA（路由通告）/ SLAAC** 和 **DHCPv6**，让局域网设备能拿到 IPv6 地址；
4. 如果是光猫后面再接自己的路由器：路由器也要开 IPv6（Native / DHCPv6，LAN 开 RA/SLAAC），或把光猫改桥接、由路由器 PPPoE 拨号并开启 IPv6。

> 以中国电信天翼网关（如创维 SK-D840N）为例，常见路径是「状态 → 网络信息」看 IPv6，「网络 → 网络设置 / 宽带设置」配 WAN 的 IPv6，「网络 → 用户侧管理 → IPv6 设置」开 LAN 侧分配。不同固件菜单名会有差异；普通账号看不到时，需要超级管理员账号或联系运营商开通 IPv6 双栈。
>
> 光猫的 IPv6 设置项一般对应：
> - WAN 获取方式：**自动 / DHCPv6**（PPPoE 宽带选"复用 IPv4 账号"）；
> - 前缀：**DHCPv6-PD / 自动**；
> - LAN 分配：**RA + SLAAC 开启**，DHCPv6 服务器自动。

---

## 4. 选对地址再填

| 场景 | 填什么 |
|---|---|
| 手机和电脑在同一个 Wi-Fi | 优先填电脑的**全局地址**（`2409:...`）；也可用 `fe80::...%7` + 接口索引 7 |
| 手机用移动数据、跨网络访问 | 必须填**全局地址**，并且光猫已放行入站（见第 6 节） |
| 服务器有多块网卡 | 填和客户端同网段的那块网卡的地址 |
| 不确定接口索引 | Windows `ipconfig` 里 `%` 后面的数字；Android 可在 Pydroid 里运行 `print(socket.if_nametoindex("wlan0"))` |

**接口索引什么时候填？**

- 全局地址（`2409`/`240e`/`2408` 开头）：留空；
- 链路本地地址（`fe80::` 开头）：必须填，如 Windows 的 `%7` 就填 `7`。

### 4.1 连接密码与配置串（软件功能）

- **服务器**可以设置连接密码（留空 = 不验证）；客户端必须填写相同密码。
- 密码不会以明文发送：客户端用服务器下发的随机数做 HMAC-SHA256 挑战-响应，服务器比对结果。
- 服务器启动监听后，点地址栏右侧「分享配置」，会生成一段 `FT6.` 开头的**连接配置串**，其中包含 **IPv6 地址 + 端口 + 连接密码**。
- 客户端点「导入配置」，粘贴这段字符串（剪贴板里已有则自动读取），程序会自动填好地址、端口、密码，再点「连接」即可。
- 手机读不到剪贴板时：可以让服务器把配置串发到「聊天」里，手机再点「导入配置」，程序会从聊天记录里查找；也可以在弹窗里长按输入框手动粘贴，或点「从文件导入」。
- 手机的「本机 IPv6 地址」偶尔会读不到（Android 10+ 对 `/proc` 有限制）；程序会尝试系统接口、`ip` 命令和路由探测，仍为空时可在「分享配置」里手动输入系统 WLAN 详情中的 IPv6 地址。
- 配置串等同于密码（只是 Base64 编码，不是加密），请私发；改了密码或端口后旧配置串失效，需要重新生成。
- 命令行同样支持：`file_server.py --password 密码`、`file_client.py --password 密码 ...`。

---

## 5. 防火墙

### 5.1 Windows 防火墙

第一次点「启动监听」时，Windows 会弹窗询问是否允许 Python/程序访问网络，**必须点"允许"**（专用和公用网络都勾上更稳妥）。

如果当时点了"取消"，手动放行（管理员 PowerShell）：

```powershell
New-NetFirewallRule -DisplayName "IPv6文件传输9000" -Direction Inbound -Protocol TCP -LocalPort 9000 -Action Allow -Profile Any
```

打包成 exe 后，按程序放行：

```powershell
New-NetFirewallRule -DisplayName "IPv6文件传输程序" -Direction Inbound -Program "D:\路径\file_gui.exe" -Action Allow -Profile Any
```

删除规则（不再需要时）：

```powershell
Remove-NetFirewallRule -DisplayName "IPv6文件传输9000"
```

另外注意 Windows 的**网络位置**：如果当前 Wi-Fi 被识别为"公用网络"，默认拦截会更严格；建议在「设置 → 网络和 Internet → WLAN → 网络配置文件」里改成"专用"。

### 5.2 光猫 / 路由器防火墙

- **同一个 Wi-Fi 内**：手机和电脑的流量走光猫的局域网侧，**不经过 WAN 防火墙**，所以一般不需要改光猫防火墙，只要 Windows 放行即可。
- **公网直连**：需要在光猫里放行 IPv6 入站（TCP 9000），或降低 IPv6 防火墙级别。很多运营商光猫只提供"高/中/低/关"，没有按端口放行；也可能被运营商限制。
- **不要**为了让局域网能连就关闭路由器防火墙——局域网互访本来就不经过它。

### 5.3 Android

Android 一般没有针对 App 的入站防火墙；但如果手机当服务器，需要保持 Pydroid 在前台，避免系统省电策略把进程杀掉。

---

## 6. 同一 Wi-Fi 与公网直连的区别

```
同一 Wi-Fi：手机 → 光猫 Wi-Fi/LAN → 电脑
            （不经过光猫 WAN 防火墙）

公网直连：  手机(4G/5G) → 运营商 → 互联网 → 光猫 WAN → 光猫 IPv6 防火墙 → 电脑
            （默认在这里被挡）
```

| | 同一 Wi-Fi | 公网直连 |
|---|---|---|
| 需要 IPv6 | 是（链路本地也行） | 是（必须是全局地址） |
| 光猫防火墙 | 不涉及 | 需要放行入站 |
| Windows 防火墙 | 需要放行 | 需要放行 |
| 地址稳定性 | 相对稳定 | 前缀可能变化，需要 DDNS 或重新填 |
| 速度 | 局域网速度，最快 | 受运营商上行/下行限制 |
| 安全性 | 较高（仅局域网） | 低（明文协议，见第 8 节） |
| 适用场景 | 家里/办公室日常 | 在外面访问家里电脑 |

### 为什么"手机和电脑要在一个 Wi-Fi 下"？

因为家用光猫同时充当路由器 + 防火墙：

- **同一 Wi-Fi**：手机和电脑都在光猫的 LAN 侧，互访流量在局域网内部完成，不经过 WAN 防火墙，所以能通；
- **手机用移动数据**：流量从互联网进入光猫 WAN 口，默认被 IPv6 防火墙丢弃，所以超时；
- 这不是 App 的限制——把光猫的 IPv6 入站放行后，手机用移动数据也能直连（但不建议把明文协议直接暴露到公网）。

**关键概念：IPv6 只是取消了 NAT，并没有取消防火墙。**

有全球地址只代表"理论上可路由"，能不能进来还取决于：

1. 光猫/路由器的 IPv6 防火墙是否允许入站；
2. Windows 防火墙是否放行；
3. 运营商是否封禁该端口；
4. 地址是否仍然有效（前缀可能变化）。

---

## 7. 连接前/连接后的验证

### 7.1 电脑端

```powershell
# 看 IPv6 地址
ipconfig

# 看端口是否在监听（应出现 [::]:9000 LISTENING）
netstat -ano | findstr :9000

# 本机通过全局地址自测（把地址换成你的）
```

Pydroid 或本机 Python：

```python
import socket
s = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
s.settimeout(5)
try:
    s.connect(("2409:xxxx:xxxx:xxxx:xxxx:xxxx:xxxx:xxxx", 9000))
    print("TCP 通了")
except Exception as e:
    print("失败：", repr(e))
```

### 7.2 手机端

在 Pydroid 3 里运行上面的 TCP 测试代码，把地址换成电脑的全局地址。

- **通了** → App 里填同样地址即可；
- **超时** → 防火墙、AP 隔离，或不在同一网络；
- **连接被拒绝** → 电脑没监听，或端口不对；
- **网络不可达** → 手机没有 IPv6，或不在同一 Wi-Fi。

### 7.3 验证 IPv6 互联网

电脑或手机浏览器打开 <https://test-ipv6.com/>，能拿到 10/10 说明公网 IPv6 正常。

> 注意：test-ipv6 通过只能说明"能上 IPv6 互联网"，不能说明"别人能主动连进来"（入站仍可能被防火墙挡）。

---

## 8. 常见问题排查

| 现象 | 最可能的原因 | 处理 |
|---|---|---|
| 连接超时 | Windows/光猫防火墙拦截；手机和电脑不在同一网络；AP 隔离 | 放行 TCP 9000；确认同一 SSID；关闭 AP 隔离/访客网络 |
| 连接被拒绝 | 电脑没在"监听"；端口不一致；服务器其实是客户端模式 | 电脑点「启动监听」；核对端口；确认角色 |
| 网络不可达 | 手机没有 IPv6；手机走了移动数据；地址填错（`::1`、虚拟网卡） | 看手机 Wi-Fi 详情；关移动数据；填 WLAN 的全局地址 |
| 能连上但传输 0% / 中断 | 防火墙只放行了部分连接；Wi-Fi 不稳定；Pydroid 被切后台 | 保持前台；看日志；重试（支持 .part 续传） |
| 昨天能连，今天不行 | 家宽 IPv6 前缀变化，电脑地址变了 | `ipconfig` 重新看地址；长期用需要 DDNS |
| 手机 Wi-Fi 有 IPv6 但仍超时 | 光猫开了 AP 隔离；Windows 网络位置是"公用"且规则只对专用；规则没生效 | 关 AP 隔离；网络改专用；用 `-Profile Any` 重建规则 |
| 手机用移动数据连不上 | 光猫 IPv6 防火墙拒绝入站（默认） | 需要放行入站，见第 6 节；或改用同 Wi-Fi |
| 中文显示方块（手机） | Tk 环境缺少中文字体 | 「连接」页底部更换界面字体 |

---

## 9. 公网直连的安全提醒

本工具已支持**连接密码**（HMAC-SHA256 挑战-响应，密码不在网络上明文传输），但**文件内容与聊天仍是明文**：

- 知道地址、端口和密码的人可以连接、上传/下载文件、发消息；
- 配置串里包含密码，请私发，不要发公开群聊；
- 公网 IPv6 地址是全局可见的，长期开放有风险；
- 建议只在可信网络使用，测试完点「停止监听」；
- 确实需要公网访问时，优先用 **Tailscale / ZeroTier** 等组网工具（不用改光猫防火墙，设备之间像在同一内网）；
- 或者再加**传输加密/VPN**后再对公网开放。

---

## 10. 一个成功配置的例子（示例，非真实地址）

电脑（Windows，同一 Wi-Fi）：

```
WLAN IPv6 地址 : 2409:xxxx:xxxx:xxxx:xxxx:xxxx:xxxx:1234
默认网关       : fe80::xxxx%7
监听端口       : 9000
```

手机（同一 Wi-Fi）：

```
服务器地址 : 2409:xxxx:xxxx:xxxx:xxxx:xxxx:xxxx:1234
端口       : 9000
接口索引   : 留空
```

结果：连接成功，文件传输与 SHA-256 校验正常。

---

## 11. 公网直连：在光猫上放行 IPv6 入站

> 适用于"手机用蜂窝网络直接连家里电脑"的场景。
> **只建议临时或受控使用**：本工具传输内容仍是明文，只有连接密码；长期对公网开放有风险（见第 9 节）。

### 11.1 先决条件

- 电脑的 WLAN 已经有**全局 IPv6**（`2409` / `240e` / `2408` 开头）；
- 电脑作为服务器，已经点「启动监听」；
- 知道光猫的管理员账号（部分设置普通账号看不到）；
- 手机有蜂窝 IPv6，能临时关 Wi-Fi 测试。

### 11.2 Windows 侧

1. 放行 TCP 9000（管理员 PowerShell）：

   ```powershell
   New-NetFirewallRule -DisplayName "IPv6文件传输9000" -Direction Inbound -Protocol TCP -LocalPort 9000 -Action Allow -Profile Any
   ```

2. 确认监听正常：

   ```powershell
   netstat -ano | findstr :9000
   ```

   应看到 `[::]:9000 LISTENING`。

3. 记下电脑当前的全局 IPv6（`ipconfig` 的 WLAN 段）。家宽前缀可能会变，变了就要重新填/重新生成配置串。

### 11.3 光猫侧（通用步骤）

不同品牌/固件的菜单名不同，但逻辑一致：

1. 浏览器打开光猫管理页（常见 `192.168.1.1`），登录。
2. 先看「状态 / 网络信息 / 网络侧信息」：确认 WAN 有 IPv6 地址和前缀；没有的话先按第 3 节开启 IPv6。
3. 用管理员账号进入「安全 / 防火墙 / IPv6 防火墙」：
   - 如果有**入站规则 / 端口放行 / IPv6 防火墙规则**：新增一条
     - 协议：TCP
     - 端口：9000（或你改过的端口）
     - 目的地址：电脑的 IPv6 地址（或选择该电脑对应的 LAN 设备）
     - 动作：允许
   - 如果只有「高 / 中 / 低 / 关闭」这种总开关，没有按端口放行：
     - 只能**临时**调到"低"或"关闭"来测试；
     - 测试完务必恢复，或者改用第 12 节的 Tailscale 方案；
     - 不建议长期关闭整台光猫的防火墙。
4. 保存并重启光猫，等 1～2 分钟。
5. 如果光猫后面还接了自己的路由器：
   - 路由器也要放行 TCP 9000（或关闭其 IPv6 防火墙对应限制）；
   - 或者把光猫改成桥接，由自己的路由器 PPPoE 拨号并管理 IPv6 防火墙；
   - 两层防火墙任意一层拦截都会超时。

### 11.4 SK-D840N / 天翼网关（创维光猫）常见情况

如果你家的光猫是 `192.168.1.1`、页面标题类似 `SK-D840N`（运营商定制的创维光猫），菜单可能被精简：

- 普通账号一般是 `useradmin`（密码在光猫背面），**通常看不到 IPv6 防火墙的完整设置**；
- 超级管理员账号/密码由装维掌握（不同地区不同），可向装维索取或让运营商远程开启；
- 常见路径：
  - `状态 → 网络信息 / 网络侧信息`：查看 WAN IPv6 地址和前缀；
  - `网络 → 网络设置 / 宽带设置`：IPv6 启用、DHCPv6、PPPoE 复用、DHCPv6-PD；
  - `安全 / 防火墙 → IPv6 防火墙`：高 / 中 / 低 / 关闭；
- 部分 SK-D840N 固件**没有"按端口放行"**，只能调整总开关；这种情况下不建议长期关防火墙，优先用 Tailscale；
- 如果找不到 IPv6 防火墙，打运营商客服（电信 10000）说明"需要开放 IPv6 入站端口 9000"，让装维处理；
- 也可以让装维把光猫改桥接，用自己的路由器拨号并在路由器里做 IPv6 防火墙放行。

### 11.5 验证公网直连

手机**关闭 Wi-Fi、只开蜂窝数据**，在 Pydroid 里运行：

```python
import socket
s = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
s.settimeout(5)
try:
    s.connect(("电脑的全局IPv6地址", 9000))
    print("公网 TCP 通了")
except Exception as e:
    print("失败：", repr(e))
```

- 通了 → 手机 App 里填同一个全局地址；
- 超时 → 光猫防火墙/运营商拦截；
- 拒绝 → 电脑没监听或端口不对；
- 不可达 → 手机没有蜂窝 IPv6，或地址填错。

### 11.6 风险提醒

- 公网 IPv6 地址可以被扫描到，**密码认证不能替代加密**；
- 建议改非默认端口、使用强密码、只在需要时启动监听，用完立即停止；
- 家宽 IPv6 前缀会变化，长期使用需要 DDNS 或每次重新生成配置串；
- 更安全的替代方案是下面的 Tailscale。

---

## 12. 推荐方案：Tailscale（不开放光猫端口）

Tailscale 会给每台设备分配一个**私有 IPv6 地址**（`fd7a:115c:a1e0:...`），设备之间通过加密隧道直连。手机用蜂窝网络也能访问家里电脑，**不需要公网 IPv6、不需要光猫放行、不需要同一 Wi-Fi**。

### 12.1 准备

1. 电脑安装 Tailscale（Windows 安装包），登录账号；
2. 手机安装 **Tailscale App**（注意不是 Pydroid，是 Tailscale 官方应用），登录**同一个账号**；
3. 手机打开 Tailscale 的 VPN 开关，状态显示 Connected；
4. 电脑保持 Tailscale 运行（托盘图标在线）。

免费个人计划一般够用；多台设备要在同一 tailnet（同一账号）里。

### 12.2 获取电脑的 Tailscale IPv6 地址

Windows PowerShell：

```powershell
tailscale ip -6
# 如果 tailscale 不在 PATH：
& "C:\Program Files\Tailscale\tailscale.exe" ip -6
```

会输出类似：

```
fd7a:115c:a1e0:ab12:3456:7890:abcd:ef01
```

也可以用：

```powershell
tailscale status
```

查看在线设备和地址。没有输出时，先执行 `tailscale up` 登录，或打开 Tailscale 托盘图标确认在线。

手机端在 Tailscale App 的「此设备 / This device」里也能看到本机 Tailscale IPv6。

### 12.3 用 Tailscale 地址连接

**电脑当服务器：**

1. 电脑照常「作为服务器」→「启动监听」（监听 `::`，包含 Tailscale 接口）；
2. 手机客户端「服务器地址」填电脑的 Tailscale IPv6（`fd7a:...`）；
3. 端口填 9000，密码填电脑设置的连接密码；
4. 连接即可，手机用蜂窝数据/任意 Wi-Fi 都可以。

**手机当服务器：**

1. 手机启动监听；
2. 电脑客户端填手机的 Tailscale IPv6；
3. 手机上 Pydroid 要保持前台，Tailscale 保持连接（省电策略可能杀后台）。

**Windows 防火墙：**

- 首次监听时允许弹窗；
- 或者沿用 11.2 的 TCP 9000 放行规则（`-Profile Any`）；
- Tailscale 的虚拟网卡通常被 Windows 识别为专用网络，若仍有问题，确认规则对 Tailscale 接口也生效。

**App 地址列表：**

- 本机 IPv6 列表里可能出现 `fd7a:...` 的 ULA 地址（Tailscale 接口）；
- 服务器「分享配置」时可以选中它再生成配置串；
- 如果没有自动列出，手动输入 Tailscale IPv6 也可以。

**Tailscale ACL：**

- 默认允许同一 tailnet 的所有设备互通；
- 如果你改过 ACL，需要允许对应设备访问 TCP 9000；
- 不需要在光猫或路由器上做任何端口映射。

### 12.4 验证

```powershell
# 电脑上查看 Tailscale 状态/地址
tailscale status
tailscale ip -6
```

```python
# 手机 Pydroid：测试 Tailscale IPv6 的 TCP 连通性
import socket
s = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
s.settimeout(5)
try:
    s.connect(("fd7a:115c:a1e0:xxxx:xxxx:xxxx:xxxx:xxxx", 9000))
    print("Tailscale TCP 通了")
except Exception as e:
    print("失败：", repr(e))
```

- 通 → App 填同样地址；
- 超时/不可达 → Tailscale 未连接、地址填错、Windows 防火墙或 ACL 拦截；
- 手机先确认 Tailscale App 显示 Connected，再测试。

### 12.5 注意事项

- Tailscale 的 IPv6 地址是**固定的私有地址**，不会像家宽前缀那样变化，适合长期使用；
- Tailscale 负责传输加密，但本工具的文件内容/聊天在隧道内仍是明文协议，连接密码仍然要设置；
- 手机用蜂窝时 Tailscale 会走移动数据；传输大文件注意流量；
- Android 省电策略可能杀掉 Tailscale 或 Pydroid，传输时保持前台、必要时把两者加入"不受限制"；
- 免费计划有设备和用户数量限制，具体以 Tailscale 官网为准。

---

## 13. 两种方式怎么选

| | 光猫放行 IPv6 入站 | Tailscale |
|---|---|---|
| 需要公网 IPv6 | 是 | 不需要 |
| 需要改光猫/路由器 | 是，且部分光猫不支持按端口放行 | 不需要 |
| 手机蜂窝可用 | 可以（前提是入站放行成功） | 可以 |
| 地址稳定性 | 家宽前缀可能变化 | 固定私有 IPv6 |
| 加密 | 无（只有密码认证） | Tailscale 隧道加密 |
| 安全风险 | 公网暴露，较高 | 仅 tailnet 内可见，较低 |
| 推荐场景 | 临时测试、有公网 IPv6 且会配置防火墙 | 日常远程访问（推荐） |

**结论：** 能在同一 Wi-Fi 下用就同 Wi-Fi；需要外网访问优先用 Tailscale；确实要公网直连再按第 11 节放行，并做好安全措施。

---

## 附录：命令速查

```powershell
# 查看本机 IPv6 地址
ipconfig

# 查看 IPv6 接口列表（WLAN 应出现在列表里）
netsh interface ipv6 show interface

# 查看 9000 端口监听
netstat -ano | findstr :9000

# 启用 WLAN 的 IPv6（管理员）
Enable-NetAdapterBinding -Name "WLAN" -ComponentID ms_tcpip6

# 放行 TCP 9000（管理员）
New-NetFirewallRule -DisplayName "IPv6文件传输9000" -Direction Inbound -Protocol TCP -LocalPort 9000 -Action Allow -Profile Any

# 删除放行规则
Remove-NetFirewallRule -DisplayName "IPv6文件传输9000"

# Tailscale：查看本机 Tailscale IPv6 / 在线设备
tailscale ip -6
tailscale status
```

```python
# Pydroid / Python：TCP 连通性测试
import socket
s = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
s.settimeout(5)
try:
    s.connect(("服务器的IPv6地址", 9000))
    print("TCP 通了")
except Exception as e:
    print("失败：", repr(e))
```

```python
# Android/Pydroid：查看 wlan0 的接口索引（链路本地地址需要）
import socket
print(socket.if_nametoindex("wlan0"))
```
