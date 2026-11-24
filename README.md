# IPv6 文件传输工具（PC + 手机版）

基于 **IPv6 TCP** 的点对点文件传输 + 文字消息工具。电脑端用 Tkinter，手机端在 **Pydroid 3** 里运行 Tkinter，两端使用同一套协议，可以互相传文件、发消息；不需要云盘或中转服务器。

> 连接前请先阅读网络配置说明：[PC/NETWORK_SETUP.md](PC/NETWORK_SETUP.md)（手机版同一份：[mobile/NETWORK_SETUP.md](mobile/NETWORK_SETUP.md)）。

---

## 功能概览

- **服务器 / 客户端双角色**：一端启动监听，另一端主动连接；服务器可同时连接多个客户端。
- **双向文件传输**：
  - 客户端上传文件到服务器；
  - 服务器把任意本地文件推送给选中客户端或全部客户端；
  - 手机版同样支持发送和接收。
- **传输可靠性**：
  - 进度条、百分比、实时速度、剩余时间；
  - 双端 SHA-256 校验并显示「一致 / 不一致」；
  - 断线后从 `.part` 续传；
  - 同名文件自动重命名，防止路径穿越。
- **文字消息**：独立长连接，传大文件时仍可聊天；服务器支持群发/定向。
- **连接密码**：HMAC-SHA256 挑战-响应，密码不在网络上明文传输。
- **连接配置串**：服务器生成 `FT6.` 开头的字符串（含 IPv6 地址、端口、密码），客户端粘贴即自动填好。
- **客户端别名**：服务器可给客户端起名（如“我的手机”），列表、聊天、传输记录都会显示别名。
- **传输记录**：显示方向、对端（谁传给谁）、文件名、大小、状态、校验结果；长内容支持横向滚动；手机端点记录可看完整文件名/路径。
- **手机版**：竖屏四页签，内置文件浏览器，接收目录可设置，地址列表/记录表可横向滚动，支持字体切换。

---

## 目录结构

```text
.
├── README.md                  ← 本文件（项目总览）
├── PC/                        ← 电脑版
│   ├── main.py                ← 图形界面入口（服务器 + 客户端）
│   ├── file_core.py           ← 协议与传输内核
│   ├── file_server.py         ← 命令行服务器（文件传输，无 GUI/聊天）
│   ├── file_client.py         ← 命令行客户端（put/get）
│   ├── NETWORK_SETUP.md       ← 网络配置说明（必读）
│   └── app_icon_256.ico       ← 程序图标（如存在）
└── mobile/                    ← 手机版（Pydroid 3）
    ├── main.py                ← 手机版图形界面入口
    ├── file_core.py           ← 与电脑版同步的传输内核
    ├── README.md              ← 手机版安装与使用说明
    └── NETWORK_SETUP.md       ← 网络配置说明
```

> `file_core.py` 必须和同目录的 `main.py` 一起使用，不能只拷一个文件。
> `file_gui_config.json`、`mobile_config.json` 是运行时生成的配置文件（保存端口、目录、密码、别名等），不是程序必需文件。

---

## 快速开始

### 电脑版

要求：Windows 10/11，Python 3.8+（安装时自带 Tkinter），无需安装第三方库。

```powershell
cd PC
python main.py
```

1. 选择「作为服务器」→ 填端口（默认 9000）→ 设置连接密码（可留空）→「启动监听」；
2. 点「分享配置」复制 `FT6.` 连接串发给客户端；
3. 客户端选择「作为客户端」→ 点「导入配置」粘贴连接串 →「连接」；
4. 在「发送」页添加文件 →「开始发送 / 开始上传」。

命令行方式（文件传输）：

```powershell
# 服务器
python file_server.py --port 9000 --dir received --password 1234

# 客户端上传
python file_client.py --host 2409:xxxx::1 --port 9000 --password 1234 put .\a.jpg

# 客户端下载
python file_client.py --host 2409:xxxx::1 --port 9000 --password 1234 get a.jpg --out-dir downloads
```

> 命令行服务器只支持文件传输，不支持文字消息和服务器推送；需要完整功能请用图形界面。

### 手机版（Pydroid 3）

1. 把整个 `mobile` 文件夹复制到手机，例如：
   ```text
   /storage/emulated/0/Documents/Ipv6Transfer/mobile/
   ```
2. 安装 **Pydroid 3**；首次运行如提示安装 Tkinter 组件，按提示安装；
3. Pydroid 打开 `mobile/main.py` → 运行；
4. 允许 Pydroid 访问存储。

详细步骤见 [mobile/README.md](mobile/README.md)。

### 首次连接推荐流程

1. 服务器端设置连接密码并启动监听；
2. 点「分享配置」复制连接串；
3. 客户端点「导入配置」粘贴，自动填入地址/端口/密码；
4. 点「连接」；
5. 服务器在线列表里可以给客户端设置别名，方便识别。

---

## 网络要求

- 本工具**只使用 IPv6**，不支持 IPv4-only 网络；
- 两端必须有可互通的 IPv6 地址；
- 同一 Wi-Fi 下最简单（局域网直连，不经过光猫 WAN 防火墙）；
- 跨网络/蜂窝访问需要光猫放行入站，或使用 Tailscale（推荐）；
- 不要填 `::1`、虚拟网卡/蓝牙的 `fe80::`；链路本地地址需要正确的接口索引。

详细内容（地址类型、Windows/手机/光猫设置、防火墙、公网直连、Tailscale、排错）：

- [PC/NETWORK_SETUP.md](PC/NETWORK_SETUP.md)
- [mobile/NETWORK_SETUP.md](mobile/NETWORK_SETUP.md)

---

## 连接密码与配置串

- 服务器设置密码后，客户端必须填写相同密码；
- 密码通过 HMAC-SHA256 挑战-响应验证，不会在连接中明文发送；
- 服务器「分享配置」生成 `FT6.` 开头的配置串，包含 **IPv6 地址 + 端口 + 密码**；
- 客户端「导入配置」可自动读取剪贴板，或手动粘贴/从文件导入；
- 配置串等同于密码，只是 Base64 编码，不是加密，请私发；
- 修改密码或端口后旧配置串失效，需要重新生成。

---

## 安全说明

- **文件内容和聊天仍是明文传输**，连接密码只解决“谁能连”；
- 本机配置文件（`file_gui_config.json` / `mobile_config.json`）会保存密码和别名，注意保护；
- 不要在不可信的公网长期开放监听；
- 公网访问优先用 **Tailscale / ZeroTier** 等加密组网，或自行加 VPN；
- 配置串包含密码，不要发到公开群聊；
- 传输完成后建议停止监听。

---

## 常见问题

| 现象 | 处理 |
|---|---|
| 手机一运行就闪退 | 确认 `main.py` 和 `file_core.py` 是同一版本；删除 `__pycache__`；彻底关闭 Pydroid 再运行；仍不行则在 Pydroid 终端执行 `python main.py` 查看 traceback |
| 报 `No module named tkinter` | Pydroid 3 未安装 Tkinter 组件，在 Pydroid 的 Pip/组件里安装 |
| 连接超时 | 确认服务器已「启动监听」；Windows 防火墙放行 TCP 9000；手机/电脑在同一 Wi-Fi；光猫未拦截 |
| 连接被拒绝 | 端口不一致，或服务器其实是客户端模式（客户端不监听） |
| 提示连接密码错误 | 检查密码，或让服务器重新「分享配置」后导入 |
| 本机 IPv6 地址为空 | 确认系统 WLAN 真的有 IPv6；手机可在分享配置时手动输入地址；电脑检查网卡是否启用 IPv6 |
| 只有 `fe80::` 地址 | 链路本地地址仅同网段可用，客户端需填接口索引 |
| 中文显示方块 | 电脑检查中文字体；手机在「连接」页底部切换界面字体 |
| 长文件名显示不全 | 电脑/手机表格可横向滚动；手机点记录可看完整文件名/路径 |
| 手机导入配置读不到剪贴板 | 在弹窗里长按粘贴、从文件导入，或让服务器把配置串发到聊天后再导入 |

---

## 相关文档

| 文档 | 内容 |
|---|---|
| [PC/NETWORK_SETUP.md](PC/NETWORK_SETUP.md) | IPv6 地址、Windows/光猫设置、防火墙、公网放行、Tailscale |
| [mobile/NETWORK_SETUP.md](mobile/NETWORK_SETUP.md) | 同一份网络配置说明（手机版） |
| [mobile/README.md](mobile/README.md) | Pydroid 3 安装、手机版使用、常见问题 |

---

## 打包（可选）

电脑版可用 PyInstaller 打包成 exe（在 `PC` 目录执行）：

```powershell
pyinstaller --noconfirm --clean --onefile --windowed --name IPv6Transfer --icon app_icon_256.ico main.py
```

- 有 `app_icon.ico`（多尺寸）时把 `--icon` 换成它；
- 打包后 `file_core.py` 会由 PyInstaller 自动分析，不需要手动复制；
- 若使用 `.spec` 文件，在 `EXE(...)` 中加 `icon='app_icon_256.ico'`；
- 手机版不需要打包，直接用 Pydroid 3 运行 `mobile/main.py`。
