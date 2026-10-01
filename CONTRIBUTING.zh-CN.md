# 为得心 Hands 做贡献 · [English](CONTRIBUTING.md)

感谢参与！得心 Hands 在别人真实的桌面上操作，所以所有改动都用同一个标准衡量：它能不能让规划器在桌面上做得更多、更可靠，同时不碰任务之外的任何东西？组织层面的通用规范（[deskmind-ai/.github](https://github.com/deskmind-ai/.github)）在这里同样适用，包括[行为准则](https://github.com/deskmind-ai/.github/blob/main/CODE_OF_CONDUCT.md)。

## 环境

```bash
git clone https://github.com/deskmind-ai/bench && git clone https://github.com/deskmind-ai/hands
cd hands
python -m venv .venv && . .venv/bin/activate
pip install -e ../bench -e ".[render]"     # 任务格式和评分器来自 deskmind-bench
```

然后是 CI 在 Linux 上跑的三项检查（`.github/workflows/evals.yml`），提 PR 前三项都必须通过：

```bash
python -m unittest discover -s tests -v                        # 契约测试，毫秒级
deskmind-hands verify-tasks --set smoke                        # 任务自检：每个 oracle 能通过，什么都不做则失败
deskmind-hands run --set smoke --adapter oracle --repeats 1 --gate 100 --no-screenshots   # smoke：端到端跑通整个循环
```

**测试只用模拟桌面。** 测试在模拟桌面上配合 `oracle` 或 `null` 适配器运行：不需要 Mac、权限、模型或密钥。不要添加需要 `--driver peekaboo`、真实应用、网络或 API key 的测试；环境里只要出现模型密钥，CI 就会失败。`deskmind-hands doctor` 会列出你的机器能跑哪些层级。

## 两个驱动，一个接口

两者都实现 `deskmind_hands/drivers/base.py` 里的 `Driver`：`observe()` 返回元素列表，`execute()` 执行一个绑定在最新观察上的动作，`state()` 提供给评分器。

- **`drivers/mock.py`，与平台无关。** 在真实的工作目录上模拟一个文件窗口和一个编辑器。动作会真的改动文件，所以同一个评分器既能给模拟运行打分，也能给真实运行打分。测试都写在这里。
- **`drivers/peekaboo.py`，仅限 macOS。** 通过 [Peekaboo](https://github.com/steipete/Peekaboo) 读取辅助功能树和窗口截图，并在后台把输入发给目标窗口。它需要「屏幕录制」和「辅助功能」权限，所以任何测试和 CI 都不使用它。这里的改动在 Mac 上手动验证，PR 里写明所用的 macOS 和 Peekaboo 版本。

驱动做不到的事要如实报告（`ExecResult(unsupported=True)`），不要假装做了。写入后无法回读确认的，报告为 `indeterminate`，而不是成功。

## 投影层

在访达和文本编辑里，`peekaboo.py` 会往观察结果里添加**合成**控件：每个文件一个「把「文件」移动到 ▾」下拉框、新建文件夹的名称框和按钮、保存与另存为、撤销（`_project_finder`、`_project_textedit`）。它们的 id 以 `syn:` 开头，带 `synthetic=True`，由 `_execute_projected` 通过应用的脚本接口执行，每次使用都计入 `state()`（`projected_actions`）。`HANDS_PROJECTION=0` 可以关掉投影层。

投影的规则：只提供工作区内的目标位置；脚本调用之后核实效果；标签简短，用规划器训练时的语言；合成控件绝不能做到真实界面做不到的事。

## 适配一个新应用

1. **先摸底。** 用 `tools/ax_survey.py` 看它的辅助功能树暴露了什么。如果树本身够用，规划器可能不需要新代码就能应付，先用一个任务验证。
2. **补上缺的部分。** 在 `observe()` 里为该 bundle id 加一个分支，写一个 `_project_<app>()` 返回 `syn:<app>:...` 元素，再在 `_execute_projected` 里处理这些 id。用应用的脚本接口实现，并且限制在工作区内。
3. **应用词表不进代码。** 纯图标按钮的名称、只能靠视觉识别的控件、后台可用的快捷键、聊天应用唯一允许的会话，都放在 `~/.config/deskmind/apps.yaml`，由 `deskmind_hands/apps.py` 读取（见 [docs/apps-config.zh-CN.md](docs/apps-config.zh-CN.md)）。任何与某个用户的应用、语言环境或账号绑定的东西都不提交。
4. **用模拟桌面测试。** 把投影逻辑拆成不依赖应用也能测的形式：单元测试给它一个工作区和一组 `Element`，检查生成的合成控件及其效果。模拟桌面能表达的行为，就加进 `drivers/mock.py`，让 smoke 任务集覆盖到。
5. **加任务。** 在 `tasks/` 下建一个任务集，带 fixture 和效果 oracle，并通过 `verify-tasks`。新应用的评测任务提交到 [deskmind-bench](https://github.com/deskmind-ai/bench)。

其他平台的驱动（Linux AT-SPI、Windows UI Automation）是一个实现 `Driver` 的新类。先做只读：`observe()` 产出同样结构的 `Element`，`execute()` 一律返回 `unsupported`。

## 沙箱规则

任务和测试绝不碰用户的真实文件。每次运行都在从 fixture 解包出来的 `runs/<run>/ws` 里进行，`env/workspace.py` 会拒绝运行根目录之外的任何路径。任务的 `stage` 和 `oracle_effect` 命令只用 `$WS`，不用 `~` 或绝对路径。测试使用临时目录。任何代码都不发送全局按键，不在目标窗口之外点击；运行结束时恢复用户的剪贴板。

## 提交 PR

- **一个 PR 只做一件事**，简单说明改了什么、怎么验证的。
- **改 harness 要附证据。** 改变规划器看到的内容或动作的执行方式，就改变了 harness 版本：改动前后的 bench 成绩不可比。贴出改动前后的 `deskmind-bench run` 或 `deskmind-bench score` 结果，并说明版本会变，由维护者在 deskmind-bench 里升版本号。
- **保持规划器接口稳定。** `systemone` 适配器用 `POST /v1/systemone` 发送 `{state, questions}`、读取 `{answers}`，扩展时要保持兼容。
- **代码风格：** 跟周围代码保持一致，函数小，注释写「为什么」；用 bundle id 而不是应用名；坐标只在 `geometry.py` 里转换，动作只来自 `actions.py`；核心部分不引入新依赖（只用 PyYAML）。
- **检查清单：**
  - [ ] 上面三项检查在模拟桌面上通过；
  - [ ] 没有测试需要 Mac、真实应用、网络或密钥；
  - [ ] diff 里没有应用专属词表、个人路径、账号名或你自己应用的截图；
  - [ ] 涉及真实桌面的改动：描述里写明 macOS 和 Peekaboo 版本，以及改动前后的数字；
  - [ ] 中英文档同步更新（`docs/*.md` 和 `docs/*.zh-CN.md`）。

## 反馈问题

- **Bug：** 附上命令、驱动、任务 id，以及该次运行的 `run.json` 和 `trace.jsonl`（去掉个人信息）。截图只在只包含沙箱窗口时才附。
- **安全问题**，包括 Hands 越出任务或沙箱范围执行操作：不要公开提 issue，见 [SECURITY.md](https://github.com/deskmind-ai/.github/blob/main/SECURITY.md)。

提交贡献即表示你同意贡献内容按本仓库的 Apache-2.0 许可发布。
