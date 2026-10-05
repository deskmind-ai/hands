<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="assets/brand/banner-dark.svg">
    <img src="assets/brand/banner-light.svg" alt="DeskMind 得心 — 得心，应手。" width="720">
  </picture>
</p>

<p align="center">
  <img src="assets/brand/hands-lockup-light.svg" height="40" alt="DeskMind Hands"><br>
  <b>DeskMind Hands</b> · 在后台操作真实的 macOS 桌面，不抢你的焦点。<br>
  <a href="README.md">English</a> · <a href="docs/architecture.zh-CN.md">架构</a> ·
  <a href="docs/data.zh-CN.md">训练数据</a> · <a href="docs/apps-config.zh-CN.md">应用配置</a>
</p>

> 本仓库属于 **[DeskMind](https://github.com/deskmind-ai/deskmind)**：App、演示和其他组件都从那里开始。
>
> 问题和 issue 请提到 [deskmind-ai/deskmind/issues](https://github.com/deskmind-ai/deskmind/issues)，PR 仍然提到本仓库。

---

**DeskMind · 得心** —— *得心，应手。* 一组开源项目，让智能体在你自己的电脑上**看**屏幕、**想**下一步、**做**
出操作，全部在本地完成。

这个仓库是 **Hands（手）**：把规划模型的决定变成 Mac 上的一次操作，并把发生的一切记录下来。Brain 的训练数据也从这里采集。

| | 仓库 | 作用 |
|---|---|---|
| 👁 | [deskmind-ai/eyes](https://github.com/deskmind-ai/eyes) | 在截图上找到目标 |
| 🧠 | [deskmind-ai/brain](https://github.com/deskmind-ai/brain) | 决定下一步，并给出校准过的置信度 |
| ✋ | **deskmind-ai/hands** | 操作真实的 macOS 桌面 |
| 📐 | [deskmind-ai/bench](https://github.com/deskmind-ai/bench) | 沙箱桌面任务与评分器，用来复现我们的数字 |

## 它做什么

- **在后台工作。** 操作通过辅助功能（Accessibility）接口和 [Peekaboo](https://github.com/steipete/Peekaboo)
  的按窗口投递送到目标窗口，不经过全局键盘和鼠标。任务运行时你可以继续在自己的应用里打字。极少数只能在前台完成的步骤，
  除非你明确允许，否则一律拒绝；每一次前台操作都会记进运行记录。
- **投影层让访达和文本编辑变得可控。** 在访达里移动文件是“选中、⌘C、导航、⌥⌘V”，四个决定，规划模型很容易走丢。
  Hands 把它呈现成每个文件一个合成控件：*移动「a.log」到 ▾*，选项就是所有合法的目的地。新建文件夹是一个名称输入框加一个按钮；
  另存为是名称、位置、按钮；撤销是一个按钮。每个合成控件都有标记，通过应用的脚本接口执行，并单独计数。
  设置 `HANDS_PROJECTION=0` 可以关掉它，在原始辅助功能树上衡量规划模型。
- **类型化问题，而不是自由文本。** `systemone` 适配器使用 `POST /v1/systemone`：发送页面状态和一组类型化问题
  （*下一步是什么操作？*、*CLICK 点哪个元素？*、*输入什么值？*、*目标是否已达成？*），每个问题都有枚举好的选项，
  返回每个选项的概率。规划模型从不自己写动作，所以没有任何解析。可以指向本机的
  [DeskMind Brain](https://github.com/deskmind-ai/brain)，或任何同样 API 的服务。
- **用 oracle 生成 DAgger 标签。** 每个生成的训练任务都带有效果 oracle，即能产生正确终态的 shell 命令。
  `tools/oracle_label.py` 把“还剩什么没做”换算成学生模型在任意状态下的正确答案，整个过程不需要模型。
  oracle 定位不了的状态交给人工（见 [docs/data.zh-CN.md](docs/data.zh-CN.md)）。
- **评测先行。** 每个任务都是带声明式评分器的 YAML。`verify-tasks` 要求 oracle 必须通过、什么都不做的对照必须失败，
  任务才算数。评分器和 diag 测试集放在 [deskmind-bench](https://github.com/deskmind-ai/bench)，那里是计分的依据。

## 快速上手（模拟桌面，不需要任何权限）

模拟桌面是真实文件系统加一个模拟的访达和编辑器，在任何系统上都能跑，包括 Linux CI。
deskmind-bench 必须克隆在 hands 旁边（在 hands 目录下即 `git clone https://github.com/deskmind-ai/bench ../bench`）：
`pip install -e ../bench` 和 `uv sync` 都从那里使用它。

```bash
git clone https://github.com/deskmind-ai/bench && git clone https://github.com/deskmind-ai/hands
cd hands
python -m venv .venv && . .venv/bin/activate
pip install -e ../bench -e ".[render]"          # 或者：uv sync（使用 ../bench）

python -m unittest discover -s tests              # 契约测试：坐标、动作、评分器、沙箱
deskmind-hands doctor                             # 这台机器能跑什么，其余为什么不能跑
deskmind-hands verify-tasks --set smoke           # 每个任务：oracle 通过，什么都不做则失败
deskmind-hands run --set smoke --adapter oracle --repeats 1 --gate 100 --no-screenshots
```

本机 8793 端口跑着 Brain 服务时（见 Brain 的 README），同一组 smoke 任务可以换成真正的规划模型：

```bash
deskmind-hands run --set smoke --adapter systemone --systemone-url http://127.0.0.1:8793 --model brain-4b
```

## 真实桌面

真实运行需要 macOS、`PATH` 上有 Peekaboo（需要上游 4.7.0 或更新版本：这是第一个 MCP `see` 会返回元素表的正式版；在 macOS 27、简体中文系统语言下测试），并给 Peekaboo
的宿主应用授予“屏幕录制”和“辅助功能”权限。额外依赖用 `pip install -e ".[macos]"` 安装。

```bash
HANDS_PEEKABOO_TRANSPORT=mcp deskmind-hands run --set train --task T-newfolder-01000 \
    --driver peekaboo --adapter systemone --systemone-url http://127.0.0.1:8793 --model brain-4b
```

每次运行都在 `runs/<run>/ws` 下的沙箱里进行，从 fixture 解包而来。结束时会关掉自己打开的窗口，并恢复你的剪贴板。
要跑 diag 基准并和参考数字对比，请用 [deskmind-bench](https://github.com/deskmind-ai/bench)。

## 通用模式

`deskmind-hands do` 在真实桌面上执行你用自己的话描述的目标：没有 fixture，也没有评分器。用 `名称=bundle id`
列出目标可以使用的应用；`--in` 把文件操作限制在一个文件夹内，结束后输出变更报告。

```bash
HANDS_PEEKABOO_TRANSPORT=mcp deskmind-hands do "<目标>" --apps Safari=com.apple.Safari,TextEdit=com.apple.TextEdit \
    --adapter systemone --systemone-url http://127.0.0.1:8793 --model brain-4b
```

开始前会先请你确认（`--yes` 跳过）；`--ask stdin` 让规划模型可以向你提问；`--foreground-ok` 允许列出的应用在不接受后台输入时短暂切到前台。

## Gym

`tools/gym` 是几个小型沙箱网页应用（`mail`、`music`、`settings`，以及横跨其中两个的 `mailmusic`），每个任务由种子生成，
并配有读取应用真实状态的 oracle。本地服务器把页面交给 GymHost（一个只包着单个网页、没有浏览器外壳的窗口）显示，
不会碰到任何真实应用或用户数据。运行器让规划模型在其中执行任务，写出由 oracle 标注的 DAgger 数据行。

```bash
tools/gym/host/build.sh                           # 构建 tools/gym/host/build/GymHost.app（需要 swiftc）
HANDS_PEEKABOO_TRANSPORT=mcp python -m tools.gym.run --app music --seeds 1-20 \
    --url http://127.0.0.1:8793 --model brain-4b --out data/gym/music.rows.jsonl
```

`--summary` 每个任务写一行（是否通过、问过的审批、最终状态）；`--trap` 和 `--beta` 控制执行一个看似合理的错误步骤、
或执行 oracle 自己那一步的频率。

## 目录

| 路径 | 内容 |
|---|---|
| `deskmind_hands/drivers/peekaboo.py` | 真实桌面驱动及其投影层 |
| `deskmind_hands/drivers/mock.py` | 确定性的模拟桌面 |
| `deskmind_hands/adapters/systemone.py` | 类型化问题的规划客户端（`/v1/systemone`） |
| `deskmind_hands/adapters/` | 另有 `oracle`/`null`（免费）、`anthropic`、`openai`、`mlx`、`hybrid` |
| `deskmind_hands/runtime/loop.py` | 智能体主循环：预算、取消、过期绑定、目标跟踪 |
| `deskmind_hands/geometry.py`、`actions.py` | 唯一做坐标换算的地方；唯一的动作词表 |
| `deskmind_hands/apps.py` | 从本机配置读取的各应用词表（默认为空） |
| `tasks/smoke`、`tasks/train`、`tasks/holdout` | 6 个模拟任务；200 个生成的训练任务（10 类）；42 个留出任务：每类的未见表述，外加两类组合任务 |
| `tools/` | 任务生成、状态采集、oracle 标注、人工标注包、gym |

## 应用相关的知识只留在你的机器上

有些应用需要辅助功能树里没有的信息：纯图标按钮的名字、完全没有辅助功能树的应用有哪些控件、只允许在某一个会话里使用的聊天应用。
这些都不随代码发布。它们从 `~/.config/deskmind/apps.yaml`（或 `$DESKMIND_APPS`）读取；没有这个文件时，所有应用一视同仁。
通用机制都保留：用本地模型给未命名按钮定位命名、用设备端 OCR 生成视觉元素、有保护的前台闪点。
见 [docs/apps-config.zh-CN.md](docs/apps-config.zh-CN.md)。

## 实话实说

- 真实运行**只支持 macOS**，并且只在一台简体中文系统语言的机器上测过。合成控件的标签是中文，规划模型就是在这些标签上训练的。
- 有投影的应用是**访达和文本编辑**。其他应用（包括 Safari）里，规划模型看到的是原始辅助功能树。
- **有些步骤需要前台。** 如果某个应用完全不接受后台输入，除非你允许对该应用做一次短暂的前台闪点，否则 Hands 会拒绝这一步。

## 许可

Apache-2.0，见 `LICENSE` 和 `NOTICE`。DeskMind 名称、得心、标志和小方不在代码许可范围内：可以用来指代本项目，
但不能修改后使用，也不能用来暗示背书。
