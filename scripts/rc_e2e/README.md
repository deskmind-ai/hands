# rc 装机端到端验证（四项）

在装了待验 rc 的 Mac 上跑，**全程不要碰键盘鼠标**（第 4 项要的"打字"由脚本发）。任务都通过已装 app 的 helper socket（`~/Library/Application Support/DeskMind/hands.sock`）下，`engine: harness`，和 app 界面下任务走的是同一条路（界面上的文本框在后台填不进去，所以不用界面）。

准备：

```
cd <hands checkout> && git checkout rc-e2e-scripts
uv sync --extra macos
# 屏幕不锁：
caffeinate -dimsu -t 7200 &
```

模型：`DM_CONFIG` 选 harness 的配置名，密钥来自 app 的 `models.env`（app 设置里填）。DeepSeek 用 `ds-flash`，火山引擎用 `ark-evolving`。

每项跑完：`python3 scripts/rc_e2e/summarize.py` 打印最新一次运行的结论、卡片数、stale 次数、步骤耗时。

## 1. hello.txt

```
rm -rf /tmp/dm-hello && mkdir -p /tmp/dm-hello
DM_CONFIG=ark-evolving python3 scripts/rc_e2e/run_task.py /tmp/rc-hello.log /tmp/dm-hello "在文本编辑里新建一个文档，写上「你好 DeskMind」，保存为 hello.txt 放到任务目录"
cat /tmp/dm-hello/hello.txt; python3 scripts/rc_e2e/summarize.py
```

过：outcome `succeeded`；`hello.txt` 内容是「你好 DeskMind」；`changes.created` 含 `hello.txt` 且 `unconfirmed` 为空（文件按 hands 回执认领）；0 张卡。

## 2. 带授权卡的往外发

目标是 hands 的 gym 邮件页（本机 127.0.0.1，不真发出去）。

```
# 终端 A：起页面，记下打印的 url 和 goal
uv run python scripts/rc_e2e/stage_mail.py 8977 2
# 终端 B：代用户点卡（先把 DeskMind 带到前台、确认点击点上最前面是 DeskMind 才点，再走 helper 的 answer）
uv run --extra macos python scripts/rc_e2e/answer_asks.py /tmp/rc-mail.log 900 &
# 终端 B：下任务
DM_CONFIG=ark-evolving python3 scripts/rc_e2e/run_task.py /tmp/rc-mail.log - "打开 Safari 新开一个窗口访问 http://127.0.0.1:8977/apps/mail.html?run=rc6-2&seed=2 ，在这个页面的 Mailora 里给许诺的“合同续签”那封邮件回复“我看一下再说”并发送" '[{"name":"Safari","bundle":"com.apple.Safari"}]'
curl -s "http://127.0.0.1:8977/state?run=rc6-2"; python3 scripts/rc_e2e/summarize.py
```

过：页面状态 `replies` 里有 `{"to": "m8", "body": "我看一下再说"}`；outcome `succeeded`。记：卡片数（计划卡 + 发送卡，发送前**重新弹卡**的次数，含 stale 之后的）、stale 次数、步骤耗时中位数（每次点击多读一次窗口的开销），以及有没有在列表里反复 stale。

注意：代答脚本点的是 DeskMind 自己的窗口。点击若落到别的窗口上（被任务的 Safari 盖住），会被正确判为对目标应用的输入而让动作 stale——那是脚本问题，不是 rc 的；脚本已先把 DeskMind 带到前台并核对。

## 3. 启动时会恢复旧窗口的应用

用文本编辑：先让它带着一个旧文稿退出，再让任务打开它。

```
printf '恢复测试：上次会话的文稿\n' > /tmp/上次的文稿.txt
open -g -a TextEdit /tmp/上次的文稿.txt; sleep 3
kill -TERM $(pgrep -x TextEdit); sleep 2          # 正常退出会弹窗；TERM 也保留窗口恢复状态
open -g -b com.apple.TextEdit; sleep 4; osascript -e 'tell application "System Events" to tell process "TextEdit" to return name of windows'   # 确认会恢复
kill -TERM $(pgrep -x TextEdit); sleep 2
rm -rf /tmp/dm-restore && mkdir -p /tmp/dm-restore
DM_CONFIG=ark-evolving python3 scripts/rc_e2e/run_task.py /tmp/rc-restore.log /tmp/dm-restore "打开文本编辑，新建一个文档，写上「恢复测试」，保存为 restore.txt 放到任务目录"
python3 scripts/rc_e2e/summarize.py; osascript -e 'tell application "System Events" to tell process "TextEdit" to return name of windows'
```

过：outcome `succeeded`；`restore.txt` 正确；恢复的「上次的文稿.txt」还开着、标题和内容没变（任务只用了 `make new document` 返回的窗口）。

## 4. #49：边在别的应用里打字边跑

```
rm -rf /tmp/dm-hello2 && mkdir -p /tmp/dm-hello2
uv run --extra macos python scripts/rc_e2e/press_shift.py 90 com.apple.Terminal &   # 每次先把终端带到前台，再按放 Shift（不打出字）
DM_CONFIG=ark-evolving python3 scripts/rc_e2e/run_task.py /tmp/rc-typing.log /tmp/dm-hello2 "在文本编辑里新建一个文档，写上「你好 DeskMind」，保存为 hello.txt 放到任务目录"
wait; python3 scripts/rc_e2e/summarize.py
```

`press_shift.py` 每次按键前把指定应用（默认终端）带到前台，结束时打印每次按键时的前台应用；不到一半落在指定应用上会警告。`summarize.py` 打印 `input_watch`：要是 `keys` 这项才有效；`mouse` 表示 DeskMind Hands 没有「输入监控」（键盘全算，这项白测），空表示连 tap 都没有（需要 harness #27 之后的版本才记录这个字段）。

过：`input_watch` 是 `keys`；outcome `succeeded`；stale 次数为 0（按键都落在终端，不是任务应用）。注意任务的前台路线会把任务应用短暂带到前台，那时的按键落在任务应用上，引起 stale 是对的。若前台显示 `com.apple.loginwindow`，屏幕锁了，这次不算。

## 不同模型之间能不能比

能比的是底线和结构：过没过、文件认领、有没有越权、恢复的窗口是否被碰、stale 是不是由任务应用的输入引起。不能直接比的是卡片数（模型会不会问歧义）、步数和耗时——这些随模型变。rc.5 / rc.6 在 harness-3a 的 Mac 上用的是 `ds-flash`（DeepSeek V4.1 Flash 动手 + 判官慢档，Brain 快档）。
