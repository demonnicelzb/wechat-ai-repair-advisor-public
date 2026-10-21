# 微信 AI 维修顾问（WeChat AI Repair Advisor）

在 Windows 微信客户端上自动接待客户咨询的 AI 助手：**不是一问一答的复读机**，而是像维修店师傅一样
先问症状 → 一次只给一步 → 等客户反馈 → 再推进下一步，直到问题定位。

> ⚠️ 本项目基于本机微信数据库读取 + UI 自动化实现，**仅限个人学习与自用**。
> 这类自动化不符合《微信软件许可及服务协议》，存在账号被限制的风险，请自行评估。
> 请勿在未经对方同意的情况下用于群发、营销或骚扰。

## 它解决什么问题

普通的"微信 AI 自动回复"往往把当前这一条消息丢给大模型，模型看不到上文，只能干巴巴回一句。
本项目把**本次运行期间的聊天记录**一起交给模型，并约束它按排查流程推进：

```
客户 > 电脑出了点问题
助手 > 什么问题？
客户 > 屏幕黑了，但是机箱是亮的
助手 > 先看下键盘上大小写灯还亮不亮？
助手 > 亮着说明主机没死机。
客户 > 灯亮着
助手 > 那按 Ctrl+Alt+Del 看能不能出界面。
```

## 主要特性

| 特性 | 说明 |
| --- | --- |
| 多轮上下文 | 每轮带上双方本次运行后的聊天记录，模型记得上一轮问了什么 |
| 问答推进式排查 | 一次只让客户做一个动作，等反馈再走下一步 |
| 连续多条消息 | 模型用 `\|\|` 分条，程序拆成 2~3 条微信先后发出，像真人补一句 |
| 消息去重 | 按微信库 `local_id` 去重，不漏也不重，不会回复自己发的消息 |
| 连发合并 | 客户连续发多条时合并成一次输入，只回一次 |
| 转人工 | 命中"转人工 / 找老板 / 投诉"等关键词自动暂停，交给真人 |
| 一键暂停 | 目录里放个 `PAUSE` 文件即暂停，删掉恢复 |
| 进度持久化 | `state.json` 记录处理到哪条消息，重启不重复回复 |
| 安全演练 | `--dry-run` 只打印不发送；`--selftest` 离线自测（不连微信、不调 API） |
| 日志 | `logs/reply_日期.log` 记录每轮客户说了什么、回了什么、失败原因 |

## 依赖与环境

- Windows 10 / 11（依赖 Win32 API 与微信客户端）
- Python 3.9+
- 微信 **4.x** 桌面客户端（已登录，且窗口可见，不能缩到托盘）
- [wechatauto-replica](https://github.com/fanyuantaier/wechatauto-replica)（Apache-2.0）：读取微信本地数据库 + UIA/OCR 发送
- [openai](https://pypi.org/project/openai/) Python SDK + 任意 OpenAI 兼容接口（默认 DeepSeek）

```bash
pip install -r requirements.txt
```

> 本项目**不附带**任何第三方二进制（`WeChatUIA-Tool.exe`、Python/CMake 安装包、离线 wheel 等）。
> `wechatauto-replica` 及其需要的 UIA 驱动请按上游仓库文档自行安装。

## 快速开始

```bat
:: 1) 离线自测：不连微信、不花钱，27 项检查
启动微信助手.bat --selftest

:: 2) 命令行预演：用同一个"大脑"聊天，调语气用（会消耗少量 API）
python 对话预演.py

:: 3) 看有哪些会话名可以填
启动微信助手.bat --list

:: 4) 预览模式：真连微信、真读消息，但只打印不发送
启动微信助手.bat --dry-run

:: 5) 正式运行
启动微信助手.bat
```

## 配置

### 1. API Key

复制 `.env.example` 为 `.env`，填入你的 key：

```
DEEPSEEK_API_KEY=sk-xxxxxxxx
```

优先级：环境变量 `DEEPSEEK_API_KEY` > `config.json` 的 `api_key` > `.env`
> Windows 下若前两者都没有，还会兜底读**用户环境变量**（`HKCU\Environment`），
> 所以用 `setx DEEPSEEK_API_KEY sk-xxx` 设过 key 之后，即便当前终端是老窗口也能识别。

**不想把 key 写在项目文件里**（推荐），可以设成用户环境变量：

```powershell
[Environment]::SetEnvironmentVariable('DEEPSEEK_API_KEY','sk-xxxxxxxx','User')
```

> 注意：环境变量优先级最高。改了 key 记得一并更新，否则 `.env` 里的新 key 会被旧的盖掉；
> 想清掉环境变量里的：`[Environment]::SetEnvironmentVariable('DEEPSEEK_API_KEY',$null,'User')`

### 2. 回复对象

```json
"targets": ["客户A"]
```

可写多个：`"targets": ["客户A", "客户B"]`。
名字必须是微信里显示的**备注或昵称**，且和对方有过聊天记录。用 `--list` 查看可用名字。

也可以不改配置、临时指定：

```bat
启动微信助手.bat --targets "客户A"
启动微信助手.bat --targets "客户A,客户B"
```

### 3. 常用开关（config.json）

| 字段 | 说明 | 默认 |
| --- | --- | --- |
| `targets` | 要自动回复的联系人 | `["客户A"]` |
| `model` / `base_url` | 模型与接口地址 | `deepseek-chat` / DeepSeek |
| `history_scope` | 上下文范围：`session` 只带本次运行后的消息；`recent` 也带旧消息 | `session` |
| `max_reply_chars` / `max_chunks` | 单条字数上限 / 一轮最多几条 | `60` / `3` |
| `multi_bubble` / `bubble_interval` | 是否允许连发多条 / 两条间隔秒数 | `true` / `1.2` |
| `debounce_seconds` | 客户连发时等多久合并 | `1.5` |
| `handoff_keywords` / `pause_minutes` | 触发转人工的词 / 暂停时长 | 转人工等 / `30` |
| `persona_extra` | 追加人设要求（店名、上门范围等） | 空 |

想改说话风格或排查思路，改 `auto_reply_v2.py` 顶部的 `SYSTEM_PROMPT`。

## 目录结构

```
auto_reply_v2.py        主程序（单文件，约 1300 行）
对话预演.py              命令行预演，调语气用
config.json             配置
启动微信助手.bat          启动脚本（Windows）
docs/升级说明.md         设计说明与踩坑记录
state.json             自动生成：处理进度（不要提交）
logs/                  自动生成：运行日志（不要提交）
```

## 实现要点（踩过的坑）

1. **不要相信库里的 `is_self`**：`wechatauto-replica` 用 `sender_id == 2` 判断"是不是自己"，
   但消息表实际用的是 `Name2Id` 表的 rowid，不同机器上编号不同（本机自己是 3、对方是 73），
   结果机器人会把自己刚发出去的消息当成客户消息，跟自己无限对聊。
   本项目直接查会话消息库的 `Name2Id` 定位自己的 sender id，并用"最近发送过的文本"兜底。
2. **上下文要有边界**：把历史全塞给模型，客户只说句 `hello` 它也会接着上周的话题聊。
   默认只带本次运行之后的消息。
3. **去重按 `local_id`**：按内容哈希去重会吞掉客户重复说的"好的/是的"。
4. **发送失败要回读数据库确认**，否则可能重复发或漏发。

详见 [docs/升级说明.md](docs/升级说明.md)。

## 致谢与许可

- 微信读写能力来自 [wechatauto-replica](https://github.com/fanyuantaier/wechatauto-replica)（Apache-2.0），
  它是 [wxauto](https://github.com/cluic/wxauto) 的复刻版。本项目通过 pip 依赖使用，未修改其源码。
- 本项目自身的代码以 [MIT License](LICENSE) 发布。
- 使用本项目的全部风险（包括但不限于账号限制、数据安全、回复内容）由使用者自行承担。
