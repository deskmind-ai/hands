# 应用配置 · [English](apps-config.md)

各应用相关的知识放在你本机的一个 YAML 文件里，由 `deskmind_hands/apps.py` 在每个进程启动时读取一次：

1. 如果设置了 `$DESKMIND_APPS`，读它指向的路径；
2. 否则读 `~/.config/deskmind/apps.yaml`。

所有键都是可选的，默认全部为空。没有这个文件时，所有应用一视同仁。

```yaml
# ~/.config/deskmind/apps.yaml

# 纯图标按钮（Electron 应用的工具栏在辅助功能树里只是一排“按钮”）。每个应用：规划模型看到的名字 -> 定位模型能在截图上找到的描述。
grounding:
  com.example.chat:
    发送: send message button in the message input toolbar
    表情: emoji (smiley face) button in the message input toolbar
    附件: attach / upload file (paperclip) button in the message input toolbar

# 辅助功能树藏在网页视图深处的应用：读得更深（深度 60，而不是 12）。
deep_ax: [com.example.chat]

# 完全没有可用辅助功能树的应用，用设备端 OCR 和定位出的图标来观察。
vision:
  com.example.player:
    vocab:
      搜索框: search input box at the top of the window
      播放/暂停: play / pause button in the player bar at the bottom
      下一首: next track button in the player bar at the bottom
    typable: [搜索框]            # 可以输入文字的元素
    home: 示例播放器              # 回到首页的标志的 OCR 文字；每次运行开始时点一下

# 允许智能体操作的唯一一个聊天应用，并且限定在一个会话里。屏幕上的其他内容（其他会话及其预览）在规划模型看到之前就被裁掉；
# 如果那个会话没有打开，运行直接停止。
chat:
  bundle: com.example.chat
  conversation: 示例群聊          # 会话标题栏里的准确标题
  placeholders: ["输入消息"]      # 空消息框读回来的占位文字

# 按应用显示名，列出可以在后台投递给它的快捷键。[] 表示一个都不投递，这样规划模型永远不会拿到一个会落到应用当前焦点上的快捷键。
chords:
  示例聊天: []
  示例播放器: []
```

## 相关开关

| 变量 | 作用 |
|---|---|
| `HANDS_GROUNDER_URL` | 本地定位服务（默认 `http://127.0.0.1:8010/ground`）；截图按路径传递，不离开本机 |
| `HANDS_FLASH_APPS` | 允许短暂前台闪点的 bundle id，逗号分隔（视觉应用） |
| `HANDS_CHAT_FLASH=1` | 允许对配置的聊天应用做闪点；每次运行最多发送一条消息 |
| `HANDS_PROJECTION=0` | 关闭投影层 |
| `HANDS_PEEKABOO_TRANSPORT` | `mcp`（每次运行一个会话，后台）或 `cli` |

定位服务接收 `{"image": <路径>, "size": [w, h], "queries": {名字: 描述}}`，返回 `{"points": {名字: [x, y] | null}}`，
坐标相对于图片（0–1）。任何能做到这一点的本地模型都可以；[DeskMind Eyes](https://github.com/deskmind-ai/eyes) 是我们自己的。

视觉应用的 OCR 使用设备端的 macOS Vision。先编译一次辅助程序：

```bash
swiftc -O tools/native/ocr.swift -o tools/native/ocr
```
