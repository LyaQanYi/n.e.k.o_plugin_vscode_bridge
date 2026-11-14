# n.e.k.o_plugin_vscode_bridge

N.E.K.O. 的 VSCode 桥接插件。装好之后，你可以直接在 N.E.K.O. 里让它读取和修改 VSCode 当前工作区的代码。

真正的插件根是 [`vscode_bridge/`](./vscode_bridge)。在 N.E.K.O. Plugin Manager 的 **Development** 页面加载这个子目录，不要加载仓库根目录。

- `PROTOCOL.md` — 与 [neko-vscode-extension](https://github.com/LyaQanYi/neko-vscode-extension) 共用的 WebSocket 协议
- `protocol-version.json` — 协议版本和最低版本
- `vscode_bridge/` — `plugin.toml`、Python 桥接、Hosted UI、i18n、文档、测试
- `scripts/check-hosted-ui.sh` — 在 N.E.K.O. 仓库内用临时副本跑官方 Hosted UI 检查
- `scripts/package-plugin.sh` — 生成 `dist/vscode_bridge.neko-plugin`

## 安装

从 [Releases](https://github.com/LyaQanYi/n.e.k.o_plugin_vscode_bridge/releases) 下载 `vscode_bridge.neko-plugin`，在 N.E.K.O. **Plugin Center** 导入即可。

还需要安装 VSCode 端的扩展：[neko-vscode-extension](https://github.com/LyaQanYi/neko-vscode-extension)。

## 开发加载

```text
Plugin Manager → Development → Load
<本仓库路径>/vscode_bridge
```

Validate → Save → Start。Development 插件不会随 N.E.K.O. 自动启动，重启后需手动 Start。

## 打包

需要一个 N.E.K.O. 源码检出，用环境变量 `NEKO_REPO` 指向它：

```bash
export NEKO_REPO=/path/to/N.E.K.O
./scripts/package-plugin.sh
```

等价于：

```bash
cd "$NEKO_REPO"
uv run neko-plugin build "$OLDPWD/vscode_bridge" -o "$OLDPWD/dist/vscode_bridge.neko-plugin"
```

然后把 `.neko-plugin` 导入 N.E.K.O. Plugin Center。

## 许可

MIT
