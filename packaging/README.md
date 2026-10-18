# 打包成安装包

普通用户不需要装 Python：把这里生成的安装包发给他们就行。设置、设备授权和微信登录都保存在用户自己的数据目录里，升级或重装 App 不会丢：

- macOS：`~/Library/Application Support/HomeAgent`
- Windows：`%APPDATA%\HomeAgent`

## macOS（.dmg）

```bash
cd packaging
./build_mac.sh          # 生成 dist/家庭管家.dmg
```

在 Apple Silicon 的 Mac 上打出来的包只能在 Apple Silicon 上运行；要给 Intel Mac 用，需要在 Intel Mac 上再打一次。

没有 Apple 开发者证书时，App 只做了本地签名。别人第一次打开会提示“无法验证开发者”，需要在 Finder 里**右键 → 打开**，或者到“系统设置 → 隐私与安全性”里点“仍要打开”。要去掉这个提示，需要 Apple 开发者账号（99 美元/年）做签名和公证。

第一次扫描设备时，系统会询问“家庭管家”是否可以访问本地网络，要点**允许**。

## Windows（.exe）

只能在 Windows 电脑上打包：

1. 安装 Python 3.10 或更高版本（python.org，安装时勾选 “Add python.exe to PATH”）。
2. 安装 [Inno Setup 6](https://jrsoftware.org/isdl.php)（用来生成双击安装的 exe，可选）。
3. 把整个 home-agent 文件夹拷到 Windows 上，双击 `packaging\build_windows.bat`。

结果：`packaging\dist\家庭管家-安装程序.exe`。没装 Inno Setup 时只生成 `dist\HomeAgent\HomeAgent.exe`，把整个文件夹拷给别人也能用。

没有代码签名证书时，Windows SmartScreen 会提示“已保护你的电脑”，点“更多信息 → 仍要运行”即可。第一次扫描时防火墙可能弹窗，要点**允许访问**。
