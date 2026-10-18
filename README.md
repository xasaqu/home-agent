# 家庭管家 Home Agent

基于 [OpenAI Agents SDK](https://github.com/openai/openai-agents-python) 的个人智能家居 Agent，大模型使用 DeepSeek，可以在网页里操作，也可以通过微信 ClawBot 聊天。

## 功能

| 需求 | 实现 |
| --- | --- |
| 1. 主动发现局域网设备 | `discover_devices` 工具：mDNS、SSDP/UPnP、Yeelight 广播和 ARP 表，只扫描本机所在网段 |
| 2. 获取设备的查看/操作权限 | `request_device_access` / `get_device_status` / `control_device` 工具。权限分“无 / 查看 / 操作”三级，Agent 只能申请，批准由你在网页或微信里完成 |
| 3. 默认搭载 webaccess skill | `home_agent/skills/webaccess/`：SKILL.md（使用说明）加上 `web_search` / `web_fetch` 两个工具 |
| 4. HTML 操作界面 | 打开 `http://127.0.0.1:8765`，里面有对话、设备、微信三个面板 |
| 5. DeepSeek 作为大模型 | 通过 DeepSeek 的 OpenAI 兼容接口接入（`OpenAIChatCompletionsModel`） |
| 6. 微信 ClawBot | 直接实现腾讯 iLink Bot 协议：扫码绑定、长轮询收消息、发送回复和“正在输入”状态 |

## 普通用户：直接安装

- Mac：打开 `家庭管家.dmg`，把“家庭管家”拖进“应用程序”，然后双击打开。
- Windows：双击 `家庭管家-安装程序.exe`。

第一次打开会自动弹出网页向导：填 DeepSeek API Key → 扫码绑定微信（可跳过）→ 完成。之后管家在后台运行，菜单栏（Windows 是任务栏右下角）有一个蓝色小房子图标，点它就能打开页面。默认开机自动启动，可以在“设置”里关掉。

所有设置都保存在用户数据目录（Mac：`~/Library/Application Support/HomeAgent`，Windows：`%APPDATA%\HomeAgent`），重启电脑、升级 App 都不用重新设置。只有微信登录被微信服务器判定过期时，需要重新扫码，网页上会提示。

管家申请设备权限时会推送到微信，回复“同意”或“拒绝”即可。需要填密码的设备（比如路由器）要在网页里填写；如果在“设置”里打开了“手机访问”，微信推送里会带一个手机可以直接打开的链接。

打包方法见 [packaging/README.md](packaging/README.md)。

## 开发者：从源码运行

需要 Python 3.10 或更高版本，并且要在**家里局域网内的电脑**上运行，这样才能发现设备。

```bash
cd home-agent
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python -m home_agent.app           # 带菜单栏图标；也可以用 python -m home_agent 只跑服务
```

启动后会自动打开 http://127.0.0.1:8765 ，按向导填写 key 即可，不需要再改 .env（.env 仍然可用，优先级低于网页里的设置）。

- 想用手机在局域网里访问网页：在网页“设置”里打开“手机访问”并重启管家。手机访问必须带口令，口令已经包含在设置页和微信推送的链接里。
- macOS 首次扫描时系统可能会询问是否允许访问“本地网络”，需要点允许。
- Windows 防火墙需要允许 Python 接收 UDP 数据，否则 SSDP/mDNS 收不到回应。

## 设备接入

扫描到的设备默认是“未授权”。在 **设备** 面板点“授权…”，选择适配器和权限后保存。每种适配器都只使用厂商公开的本地 API：

| 适配器 | 需要你做的事 |
| --- | --- |
| `yeelight` | 在 Yeelight App 里为灯打开“局域网控制” |
| `hue` | 先按一下 Hue 桥上的圆形按钮，再在 30 秒内点“保存授权”，会自动完成配对 |
| `shelly` | 设备开启了登录认证时填写密码 |
| `homeassistant` | 填写 HA 的长期访问令牌。家里已有 Home Assistant 时推荐用这个，米家、涂鸦等设备都可以经 HA 统一控制 |
| `http` | 自己有 HTTP 接口的设备（ESPHome、自制设备），用 JSON 配置状态地址和动作 |
| `miio` | 米家 Wi-Fi 设备。填写设备 token（32 位十六进制，可用 Xiaomi Cloud Tokens Extractor 从自己的小米账号取出）。属性编号按型号在 home.miot-spec.com 查询 |
| `xiaomi_router` | 小米路由器。填写管理后台密码，授权“查看+操作”后点“同步路由器设备”，会把连在路由器上的设备全部加进列表 |

米家、涂鸦等云端设备没有统一的公开局域网接口，建议先接入 Home Assistant，再由本 Agent 通过 HA 控制。

Agent 发现自己没有权限时会提交申请，申请会出现在网页“设备”面板顶部。绑定了微信的话，也会推送到你的微信，回复 `/approve 编号` 即可批准。

## 微信 ClawBot

ClawBot 是微信官方推出的插件（“我 → 设置 → 插件 → ClawBot”），目前还在灰度，需要你的微信能看到这个入口。官方配套的是 OpenClaw 的插件 [`@tencent-weixin/openclaw-weixin`](https://www.npmjs.com/package/@tencent-weixin/openclaw-weixin)（MIT 协议）。本项目没有安装 OpenClaw，而是参照这个插件 2.4.10 版本的源码，用 Python 实现了同一套 iLink 协议（`home_agent/wechat/ilink.py`）。

绑定步骤：

1. 打开网页的 **微信** 面板，点“获取二维码”。
2. 用手机微信扫码，并在手机上确认。如果手机上显示一串数字，把它填进网页里提交。
3. 显示“微信已连接”后，在微信的 ClawBot 对话里直接和管家聊天。

默认只有扫码的那个微信号可以使用。要允许家人使用，可以在 `.env` 的 `WECHAT_ALLOW_FROM` 里加入对方的微信 user id（对方发消息后可以在日志里看到）。

微信里可以用这些指令：`/help`、`/requests`、`/approve 编号`、`/deny 编号`、`/reset`。

登录凭证保存在数据目录的 `wechat.json`。微信提示登录过期时，重新扫码即可。

## 目录结构

```
home_agent/
  agent.py              Agent 定义：DeepSeek 模型、系统提示、加载 skill
  server.py             FastAPI：网页、REST API、微信消息分发
  config.py             配置（读取 .env）
  tools/discovery.py    局域网发现
  tools/adapters.py     设备适配器
  tools/devices.py      设备注册表、权限、Agent 工具
  skills/               skill 目录，每个子目录包含 SKILL.md 和可选的 tools.py
  wechat/ilink.py       微信 ClawBot 协议客户端
  static/index.html     网页界面
app.py                桌面版入口：菜单栏图标、开机自启、自动打开浏览器
autostart.py          开机自启（macOS LaunchAgent / Windows 注册表）
packaging/            打包成 .dmg / .exe 的脚本
```

### 新增 skill

新建 `home_agent/skills/<名字>/SKILL.md`，开头写上 `name` 和 `description`，正文写使用说明；如果需要工具，再加一个 `tools.py` 导出 `TOOLS = [...]`。然后在 `.env` 里设置 `HOME_AGENT_SKILLS=webaccess,<名字>`。

Agents SDK 自带的 skills 功能依赖 OpenAI 托管的 shell 工具，用 DeepSeek 时无法使用，所以这里实现了一个兼容 SKILL.md 格式的轻量加载器。

## 安全设计

- API Key 和设备凭证只保存在本机的用户数据目录里（见上文）。
- Agent 不能给自己授权，所有权限都由你批准。设备操作会记录到数据目录 `devices.json` 的 `log` 里。
- `web_fetch` 拒绝访问局域网和本机地址，访问设备必须经过有权限检查的设备工具。
- 网页默认只监听 127.0.0.1。打开“手机访问”后才对局域网开放，而且除本机以外的访问都必须带口令。
- 数据目录里的 `devices.json` 有设备密码和 token。macOS / Linux 上程序会把它设成只有你自己能读（600 权限）；Windows 上这个设置不起作用，任何能登录这台电脑的账号都可能读到，请只在自己的 Windows 账号下运行，并且不要共享这个目录。
