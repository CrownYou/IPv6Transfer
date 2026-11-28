# IPv6 文件传输工具（电脑版 + 安卓版）

基于 **IPv6 TCP** 的点对点文件传输 / 文字消息工具。不需要云盘、账号或中转服务器：
一端监听，另一端连过来，就能双向传文件、发消息、互相推送。

同一套协议（`PROTO_VERSION = 1`）有两条实现，**可以任意互相通信**：

| 实现 | 位置 | 技术栈 | 形态 |
|---|---|---|---|
| **电脑版** | [`PC/`](PC) | Python 3.8+ / Tkinter，仅标准库 | 桌面图形界面 + 命令行服务器 / 客户端 |
| **安卓版** | [`mobile/`](mobile) | C# / .NET 10 / .NET MAUI | 手机 App（APK），竖屏四页签 |

> 手机装 [`mobile/dist/com.ipv6transfer.mobile-1.0.1.apk`](mobile/dist) 即可使用；
> 电脑直接 `python PC/main.py` 启动图形界面。
>
> 连接前请务必先读 **[PC/NETWORK_SETUP.md](PC/NETWORK_SETUP.md)**（IPv6 地址类型、防火墙、光猫、Tailscale、排错）。

---

## 目录

- [功能概览](#功能概览)
- [三端能力对照](#三端能力对照)
- [目录结构](#目录结构)
- [快速开始](#快速开始)
- [连接密码与配置串](#连接密码与配置串)
- [协议实现](#协议实现)
- [配置文件](#配置文件)
- [网络要求与排错](#网络要求与排错)
- [安全说明](#安全说明)
- [构建与打包](#构建与打包)
- [测试与验证](#测试与验证)
- [常见问题](#常见问题)
- [文档索引](#文档索引)

---

## 功能概览

- **服务器 / 客户端双角色**：任何一端都能当服务器；服务器可同时服务多个客户端。
- **双向文件传输**：客户端上传 → 服务器落盘；服务器把任意本地文件**推送**给选中客户端或全部客户端。
- **可靠传输**：
  - 1 MiB 分块流式收发，内存占用恒定；
  - 进度条 + 百分比 + 实时速率 + 剩余时间；
  - 双端 **SHA-256** 校验，界面显示「一致 / 不一致」；不一致自动从头重传一次；
  - 中断保留 `<名字>.part`，重连即**断点续传**；
  - 同名文件自动加时间戳，绝不覆盖；文件名净化 + 目录越界防护。
- **文字消息**：独立长连接（与文件传输连接分开），传大文件时仍能聊天；服务器支持群发 / 定向。
- **系统通知**：服务器收完文件自动告知客户端；客户端收到推送并校验后回执。
- **连接密码**：HMAC-SHA256 挑战-响应，密码不以明文出现在网络上。
- **连接配置串**：服务器生成 `FT6.` 开头的字符串（地址 + 端口 + 密码），客户端粘贴即自动填好。
- **客户端别名**：服务器可给客户端起名（如「我的手机」），列表、聊天、传输记录都会显示。
- **传输记录**：方向、对端、大小、状态、校验结论，点选可看完整 SHA-256 与文件名 / 路径。

---

## 三端能力对照

| 能力 | 电脑版 GUI<br>`PC/main.py` | 电脑版 CLI<br>`file_server.py` / `file_client.py` | 安卓版 App<br>`mobile/` |
|---|:--:|:--:|:--:|
| 当服务器监听 / 多客户端 | ✅ | ✅（`--max-clients`） | ✅ |
| 当客户端连接 | ✅ | ✅ | ✅ |
| 上传文件 | ✅ | ✅ `put` | ✅ |
| 服务器推送文件 | ✅ | ❌ | ✅ |
| 主动拉取服务器文件 | ❌（靠推送） | ✅ `get` / `list` | ❌（靠推送） |
| 文字消息（群发 / 定向） | ✅ | ❌ | ✅ |
| 系统通知 | ✅ | ❌ | ✅ |
| 连接密码 | ✅ | ✅ `--password` | ✅ |
| `FT6.` 配置串分享 / 导入 | ✅ | ❌ | ✅ |
| 客户端别名 | ✅ | ❌ | ✅ |
| 续传 / SHA-256 / 同名改名 | ✅ | ✅ | ✅ |
| 界面 | 桌面 Tkinter（两页签 + 日志） | 纯命令行 | 竖屏四页签 |

> **「仅文件模式」**：`file_server.py` 是纯文件服务器，会拒绝文字通道请求。
> 这时客户端（电脑 GUI 或手机 App）会提示并显示「已连接（仅文件）」——可以传文件，但不能聊天。
> 需要完整功能请让**图形界面当服务器**。

---

## 目录结构

```text
.
├── README.md                    ← 本文件（项目总览）
│
├── PC/                          ← 电脑版（Python 3.8+，仅标准库）
│   ├── main.py                  ← 图形界面入口（服务器 + 客户端，文件传输 + 文字消息）
│   ├── file_core.py             ← 协议与传输内核（CLI、GUI、手机端测试共用）
│   ├── file_server.py           ← 命令行服务器（文件传输）
│   ├── file_client.py           ← 命令行客户端（put / get / list）
│   ├── NETWORK_SETUP.md         ← 网络配置说明（必读）
│   ├── app_icon_256.ico         ← 程序图标（PyInstaller 打包用）
│   ├── file_gui_config.json     ← GUI 运行时配置（自动生成，含密码/别名，注意保护）
│   └── received/                ← 默认接收目录（示例中有一个已接收文件）
│
└── mobile/                      ← 安卓版（C# / .NET MAUI）
    ├── README.md                ← 安卓版详细说明（构建、界面、差异、验证）
    ├── Ipv6Transfer.slnx        ← 解决方案
    ├── Ipv6Transfer.Core/       ← 协议内核（C#，net10.0 类库，零 UI 依赖）
    ├── Ipv6Transfer.Mobile/     ← MAUI App（Views / ViewModels / Services / Platforms）
    ├── tests/                   ← 与电脑版 Python 内核的协议互通测试
    ├── docs/screenshots/        ← 模拟器实拍截图（含应用图标）
    ├── dist/                    ← 可直接安装的 APK
    └── app_icon_256.ico         ← 图标源文件
```

> 两个 `app_icon_256.ico` 是同一个图标（电脑版打包与手机版启动图/应用图标都用它）。

---

## 快速开始

### 电脑版图形界面

要求：Windows 10/11，Python 3.8+（自带 Tkinter），**不需要安装任何第三方库**。

```powershell
cd PC
python main.py
```

也可以直接指定启动参数：

```powershell
python main.py --role server --port 9000 --dir received
python main.py --role client --host 2409:xxxx::1 --port 9000
```

界面分左右两块：左侧是「连接」面板（角色、端口、接收目录、密码、本机 IPv6 地址、分享/导入配置），
右侧是「文件传输」「文字消息」两个页签，底部是运行日志。

### 电脑版命令行

```powershell
# 服务器：监听 [::]:9000，落盘到 ./received
python file_server.py
python file_server.py --port 9100 --dir D:\share --password 1234
python file_server.py --ipv6-only --max-clients 4      # 只接受纯 IPv6 对端

# 客户端：上传 / 下载 / 列目录
python file_client.py --host 2409:xxxx::1 put a.bin photo.zip
python file_client.py --host 2409:xxxx::1 list
python file_client.py --host 2409:xxxx::1 get report.pdf --out-dir .\dl
python file_client.py --host fe80::abcd --zone 12 put data.bin   # 链路本地地址
```

`file_client.py` 其他常用参数：`--password`、`--limit-rate`（限速）、`--no-resume`（不续传）、`--quiet`。

> 命令行服务器只支持文件传输；需要文字消息 / 推送请用 GUI 或手机 App。

### 安卓版

**方式一：直接装 APK（推荐）**

```powershell
adb install mobile\dist\com.ipv6transfer.mobile-1.0.1.apk
```

首次启动若提示「未获得公共存储权限」，点「授权」→ 打开「所有文件访问权限」→ 返回 App
（不授权也能用，但文件会存在应用私有目录，其它 App / 文件管理器看不到）。

**方式二：自己构建**（需要 .NET 10 SDK + Android SDK + JDK 21，详见 [mobile/README.md](mobile/README.md)）

```powershell
cd mobile
dotnet build Ipv6Transfer.Mobile\Ipv6Transfer.Mobile.csproj -f net10.0-android -c Release
# 产物：Ipv6Transfer.Mobile\bin\Release\net10.0-android\com.ipv6transfer.mobile-Signed.apk
```

### 首次连接推荐流程

1. 服务器端（电脑或手机）设置**连接密码**，点「启动监听」；
2. 点「分享配置」生成 `FT6.` 连接串，发给客户端（微信/QQ 私发即可）；
3. 客户端点「导入配置」：自动读剪贴板，或从文件导入，或从聊天记录里找；
4. 地址 / 端口 / 密码自动填好 → 点「连接」；
5. 服务器在「在线客户端」里给客户端设置**别名**，之后列表、聊天、记录都显示别名；
6. 「发送」页添加文件 → 「开始上传 / 开始发送」。

---

## 连接密码与配置串

- 服务器设置密码后，客户端必须填相同密码，否则在握手阶段被拒。
- 密码校验是 **HMAC-SHA256(challenge)** 挑战-响应：服务器发随机 nonce，客户端回摘要，
  **密码本身不经过网络**；也不保存到传输日志里。
- 「分享配置」生成的字符串形如（下面是真实可解析的示例：`2409:a20::1` / 9000 / 密码 `1234`）：

  ```text
  FT6.eyJ2IjoxLCJob3N0IjoiMjQwOTphMjA6OjEiLCJwb3J0Ijo5MDAwLCJwYXNzd29yZCI6IjEyMzQifQ.ff83ca37
  ```

  内容 = `{地址, 端口, 密码}` 的 Base64 + 8 位 SHA-256 校验和，**只编码不加密，等同于密码**，请私发。
- 改了密码或端口后旧配置串立即失效，需要重新「分享配置」。
- 命令行也支持密码：`file_server.py --password 你的密码`、`file_client.py --password 你的密码 ...`。

---

## 协议实现

两条实现（`PC/file_core.py` 与 `mobile/Ipv6Transfer.Core`）逐字节兼容，均已通过互通测试。

**帧格式**

- 控制消息：一行 UTF-8 JSON（紧凑分隔符），以 `\n` 结尾，单条上限 64 KiB；
- 文件内容：紧随控制消息之后，按声明的长度直接发送裸字节，1 MiB 为一块；
- 单文件上限 1 TiB，文件名上限 200 字节。

**控制命令**

| 命令 | 方向 | 作用 |
|---|---|---|
| `hello` | 客户端 → 服务器 | 版本、角色（`transfer` / `control`）、`client_id`、`app` |
| `auth` | 客户端 → 服务器 | 回应 nonce 的 HMAC-SHA256 摘要 |
| `put` / `done` | 客户端 → 服务器 | 上传：声明名字/大小/是否续传 → 传数据 → 交 SHA-256 |
| `get` / `done` | 客户端 → 服务器 | 下载：声明名字/偏移（或推送 token）→ 收数据 → 回校验结果 |
| `list` | 客户端 → 服务器 | 列服务器接收目录（CLI 在用） |
| `say` | 双向（文字通道） | 聊天消息 |
| `notice` | 双向（文字通道） | 系统通知（灰色显示） |
| `push` | 服务器 → 客户端 | 通知客户端来下载某文件（可带 token 指向任意本地文件） |
| `ping` / `bye` | 双向 | 心跳（30 s）与正常关闭 |

**可靠性设计**

- 接收中的文件写成 `<名字>.part`，校验通过才改名；
- 续传偏移由服务端（上传）或本地分片大小（下载）决定；
- 校验不一致时服务端回 `restart: true`，客户端自动从 0 重传一次；
- 同名落盘自动加 `_YYYYmmdd-HHMMSS` 后缀；文件名只取最后一段并清洗，无法写出接收目录。

---

## 配置文件

两个程序都会自动生成 JSON 配置（**含密码与别名，注意保护**）：

| 文件 | 位置 | 键 |
|---|---|---|
| `file_gui_config.json` | `PC/` 目录（不可写时退到用户主目录） | `role` `port` `host` `zone` `recv_dir` `client_recv_dir` `password` `client_aliases` `geometry` |
| `mobile_config.json` | App 私有目录（`/data/data/com.ipv6transfer.mobile/files/`） | 同上，去掉 `geometry` |

两个文件的键名一致，**可以互相拷用**（例如把电脑上的配置复制到手机）。
它们不是程序必需文件，删掉即恢复默认（端口 9000、接收目录 `received` / `downloads`）。

---

## 网络要求与排错

本工具**只使用 IPv6**（`::1` 只用于同机测试）。要点：

- 两端都要有可互通的 IPv6 地址；
- **同一 Wi-Fi 最简单**（局域网直连，不经过光猫 WAN 防火墙），通常只需放行 Windows 防火墙；
- 跨网络 / 蜂窝访问：光猫放行 IPv6 入站 + 防火墙放行端口（有风险），或优先用 **Tailscale**（推荐，见文档第 12 节）；
- 不要填 `::1`、虚拟网卡 / 蓝牙的 `fe80::`；链路本地地址必须带**接口索引**（如 `fe80::abcd%12`）；
- 手机连 Wi-Fi 时不要同时开着移动数据，也不要连「访客网络」（AP 隔离会挡内网互访）。

连接前逐项确认（完整版见 [PC/NETWORK_SETUP.md](PC/NETWORK_SETUP.md) 第 0 节）：

- [ ] 服务器已「启动监听」，状态显示「监听中」
- [ ] 客户端填的是服务器的 IPv6 地址 + 相同端口
- [ ] 两端都有 IPv6（不是只有 IPv4）
- [ ] Windows 防火墙首次监听时点了「允许」
- [ ] 手机 App 保持在前台
- [ ] 链路本地地址填了接口索引
- [ ] 设置了密码时，两端一致（或已导入连接串）

---

## 安全说明

- **文件内容和聊天是明文传输**；连接密码只解决「谁能连」，不提供加密。
- `FT6.` 配置串等同于密码，不要发到公开群聊。
- 公网使用优先 **Tailscale / ZeroTier** 等加密组网，或自建 VPN。
- 配置文件里存有密码，仓库里的 `PC/file_gui_config.json` 只是你本机的运行结果，**不要把含真实密码的配置提交到公开仓库**。
- 传输完成后建议「停止监听」；不要在不可信网络长期开放端口。

---

## 构建与打包

### 电脑版（可选，PyInstaller 打 exe）

```powershell
cd PC
pyinstaller --noconfirm --clean --onefile --windowed --name IPv6Transfer --icon app_icon_256.ico main.py
```

- `file_core.py` 会被 PyInstaller 自动分析，不需要手动复制；
- 用 `.spec` 时在 `EXE(...)` 里加 `icon='app_icon_256.ico'`。

### 安卓版

```powershell
$env:JAVA_HOME='C:\Program Files\Android\openjdk\jdk-21.0.8'
$env:ANDROID_HOME='C:\Program Files (x86)\Android\android-sdk'
$env:ANDROID_SDK_ROOT=$env:ANDROID_HOME

cd mobile
dotnet build Ipv6Transfer.Mobile\Ipv6Transfer.Mobile.csproj -f net10.0-android -c Release
```

> Debug 包默认走 Fast Deployment，直接 `adb install` 会闪退；
> 用 adb 安装 Debug 包时加 `-p:EmbedAssembliesIntoApk=true -p:AndroidEnableFastDeployment=false`，
> 或直接用 `mobile/dist` 里的 Release APK。

---

## 测试与验证

### 双端互通测试（C# 手机版 ↔ Python 电脑版内核）

```powershell
cd mobile\tests
pwsh -File .\run-interop.ps1
```

脚本用 `interop_peer.py`（直接 import `PC/file_core.py`）当 Python 对端，覆盖 **25 项断言**：

- 连接串：双向解析、两端生成逐字节一致；
- 上传 / 下载：C# ↔ Python 四种组合，文件 SHA-256 一致；
- 续传：半截 `.part` → `resume_from=500000` → 最终哈希一致；
- 密码：正确密码通过、错误密码被拒（双向）；
- 文字通道：聊天、系统通知、推送消息（双向）；
- 服务器推送 + `token` 下载（双向）。

最近一次运行：**全部 25 项 PASS**。

### 安卓端到端

见 [mobile/README.md](mobile/README.md) 第 6 节：在 Pixel 7 / API 36 模拟器上验证了安装启动、
服务器监听与密码认证、接收上传、对外下载、推送自动下载、内置文件浏览器上传、别名、配置持久化，
Debug 与 Release 包都跑过一遍；截图在 [`mobile/docs/screenshots/`](mobile/docs/screenshots)。

---

## 常见问题

| 现象 | 处理 |
|---|---|
| 连不上 / 一直「连接中…」 | 确认服务器已启动监听；地址是**服务器**的 IPv6；同一 Wi-Fi；防火墙已放行；手机没走流量 |
| 「连接密码错误」 | 两端密码不一致，或服务器改过密码 —— 重新「分享配置」再导入 |
| 提示「对端不支持文字消息 / 仅文件模式」 | 对端是 `file_server.py` 纯文件服务器；改用 GUI 或手机 App 当服务器 |
| 本机 IPv6 地址为空 | 系统确实没启用 IPv6；手机可在「分享配置」时手动输入地址 |
| 只有 `fe80::` 地址 | 链路本地地址仅同网段可用，客户端要填「接口索引」 |
| 连接超时但 ping 通 | 服务器防火墙没放行端口；或光猫 IPv6 防火墙挡了入站 |
| 手机一运行就闪退（Python 版 / Pydroid） | 确认 `main.py` 与 `file_core.py` 同版本、删掉 `__pycache__`、彻底关掉 Pydroid 再开；终端里 `python main.py` 看 traceback |
| 报 `No module named tkinter` | Pydroid 3 未安装 Tkinter 组件 |
| 中文显示方块（Pydroid） | 在「连接」页底部切换界面字体；C# 版直接用系统字体，无此问题 |
| 手机文件存在哪 | App 内「接收」页顶部显示接收目录，点「复制路径」到文件管理器粘贴 |
| 传输中断 | `.part` 会保留，重发同一文件自动续传 |
| 手机安装后图标/界面是旧的 | 卸载旧版再装（部分启动器/ROM 会缓存） |

---

## 文档索引

| 文档 | 内容 |
|---|---|
| [PC/NETWORK_SETUP.md](PC/NETWORK_SETUP.md) | IPv6 地址类型、Windows/光猫/手机设置、防火墙、公网直连、Tailscale、命令速查（**连接前必读**） |
| [mobile/README.md](mobile/README.md) | 安卓版构建与安装、界面说明、与 Python 版的差异、完整验证记录 |
| [mobile/docs/screenshots/](mobile/docs/screenshots) | 四个页签、传输记录、各类弹窗、应用图标（模拟器实拍） |
| [mobile/tests/](mobile/tests) | 协议互通测试工具（`run-interop.ps1`、`interop_peer.py`、C# 对端工具） |

---

## 备注

- 协议版本固定 `PROTO_VERSION = 1`；改动协议需同步改 `PC/file_core.py` 与 `mobile/Ipv6Transfer.Core` 并重跑互通测试。
- 电脑版为 Python 标准库实现，无第三方依赖；安卓版为 .NET 10 + MAUI，只依赖 MAUI 官方包。
- 图标：`app_icon_256.ico`（电脑版见 `PC/app_icon_256.ico`，安卓自适应图标由它生成）。
