# -*- coding: utf-8 -*-
"""微信 AI 维修顾问 v2 —— 多轮对话版

和旧版 auto_reply.py 的区别（也是旧版"你说一句我回一句"的原因）：

1. 旧版每次只把【当前这一条】消息发给模型，模型看不到上文，所以只会干巴巴答一句。
   v2 每轮都会把【双方最近 N 条聊天记录】一起发给模型，它记得上一轮问了什么、
   客户回答了什么，才能像真师傅一样一步步往下排查。
2. 旧版按 (内容, 是否自己) 去重 —— 客户第二次说"好的/是的"会被当成重复消息吞掉，
   而且冷却期内的消息被跳过就永远不回了。v2 按微信数据库里的 local_id 去重，
   不漏也不重。
3. 旧版发送失败后消息仍被标记为已处理（或反之），容易死循环/漏答；v2 有回读确认、
   重试、失败告警，且不会无限重答。
4. 旧版重启后靠"预加载历史"防重复，但重启期间的新消息会丢；v2 把进度写进 state.json。
5. 新增：客户连发多条合并成一次回复、长回复自动分段、转人工关键词自动暂停、
   PAUSE 文件一键暂停、--dry-run 只预览不发、--selftest 离线自测。

用法：
    python auto_reply_v2.py                 # 正式运行
    python auto_reply_v2.py --dry-run       # 只打印将要发送的内容，不真发
    python auto_reply_v2.py --once          # 只扫一轮就退出（调试用）
    python auto_reply_v2.py --selftest      # 离线自测：不连微信、不调 API

配置：同目录 config.json（第一次运行会自动生成模板）+ .env / 环境变量 DEEPSEEK_API_KEY
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
import time
import traceback

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
ENV_PATH = os.path.join(BASE_DIR, ".env")
LOG_DIR = os.path.join(BASE_DIR, "logs")
PAUSE_FLAG = os.path.join(BASE_DIR, "PAUSE")

# ===================== 默认配置（改 config.json 即可，不用动这里）=====================
DEFAULT_CONFIG = {
    "_readme": "改完保存即可，不用重启微信。api_key 也可以放到 .env 里的 DEEPSEEK_API_KEY。",
    "api_key": "",
    "base_url": "https://api.deepseek.com/v1/",
    "model": "deepseek-chat",
    # 要自动接待的联系人（微信里显示的名字/备注，可以写多个）
    "targets": ["客户A"],
    # 轮询间隔（秒）
    "poll_interval": 1.0,
    # 客户连发多条时，等这么久看看还有没有后续，合并成一次回复
    "debounce_seconds": 1.5,
    # 每轮带给模型的历史消息条数 / 字符上限
    "history_limit": 30,
    "history_char_budget": 5000,
    # 上下文范围：session = 只带"本次启动之后"的聊天（旧话题不掺和，推荐）
    #             recent = 带最近 N 条（含启动前的旧消息）
    "history_scope": "session",
    # 单条回复最大字数、一轮最多分几条发（别刷屏）
    "max_reply_chars": 60,
    "max_chunks": 3,
    # 允许像真人一样连着发 1~2 条（模型用 || 分条）
    "multi_bubble": True,
    # 两条之间间隔几秒（模拟打字）
    "bubble_interval": 1.2,
    "temperature": 0.6,
    "max_tokens": 300,
    # 命中这些词就暂停自动回复，交给真人
    "handoff_keywords": ["转人工", "转接人工", "找老板", "人工客服", "我要投诉"],
    "pause_minutes": 30,
    # 你在 config 里追加的额外人设要求（可留空）
    "persona_extra": "",
    "dry_run": False,
}
# ==================================================================================

# 给模型的上下文提示：说明聊天记录里谁是谁
CONTEXT_HINT = (
    "（下面是这个客户和你的真实微信聊天记录，越靠后越新：user 是客户说的话，"
    "assistant 是你之前回的话。请接着往下聊，不要解释你在做什么。）"
)

SYSTEM_PROMPT = """你是「阿梁」，一家电脑维修店的技术员。老板让你在微信上替客户看电脑问题，你在跟客户一对一聊天。

# 怎么聊（最重要）
1. 你是"问答推进式"排查：先问清症状，再一次只让客户做一个动作，等他反馈了再决定下一步。绝对不要一次列一堆可能原因和步骤。
2. 每轮只做一件事：要么问一个关键问题，要么给一步操作，可以带一句判断。
3. 客户反馈后，先用一句人话下结论（"那显示器基本没事"），再给下一步。
4. 已经确认做过的事不要再问。客户说"重启过了"，就别再让他重启。
5. 信息不够就继续问，别硬猜：什么时候开始的、之前装过什么/动过什么、有没有报错原文、指示灯和风扇的情况、能不能进安全模式。
6. 涉及拆机、断电、数据安全一定要提醒：先关机拔电源、别带电插拔、笔记本先拔电池。客户明显不会拆，就直接让他送修或说可以视频教他。
7. 修好了就收尾："那就先这么用，还有问题随时喊我"，不要继续啰嗦。
8. 客户只回"嗯/好/行/是的/试了"这种，就当他照做了，直接推进下一步，别问"请问您试了吗"。
9. 客户问"你是 AI 吗"：别承认，也别拿腔调解释，用店里师傅的口吻带过（"想啥呢，我在店里修电脑的"），然后把话拉回问题。
10. 客户爆粗口：别对骂也别道歉半天，一句话带过，继续聊正事。

# 可以连续发两条（像真人补一句）
- 需要分两句说的时候，用 || 隔开，程序会把它们分成两条微信发出去。
- 例：那说明主机没坏，是显示那边的事。||你先换根信号线试试。
- 一般只在"一句判断 + 一句下一步"时发两条，别为了发两条而硬拆；每条不超过 40 字。

# 说话风格
- 微信口吻，口语短句，一条不超过 60 个字。
- 不用"首先/其次/综上所述"，不用编号、列表、markdown 符号，不用书名号式的术语堆砌。
- 不刷屏、不重复客户的话、不客套、不问"还有什么可以帮您"。

# 参考节奏（客户说电脑黑屏、机箱还亮）
客户：电脑出了点问题
你：什么问题？
客户：屏幕黑了，但机箱是亮的
你：先看下键盘上大小写灯还亮不亮？||亮着说明主机没死机。
客户：灯亮着
你：那按 Ctrl+Alt+Del 看能不能出界面。
客户：按了没反应
你：行，长按电源键 5 秒强制关机，再开机看看。
客户：重启了还是黑屏
你：那把主机断电，打开侧板，内存条拔下来重新插紧再试。

# 边界
- 只聊电脑/手机/网络这类数码维修。客户闲聊就简短接一句，然后把话头拉回他的电脑问题。
- 不确定就说不确定，不要编造硬件型号、价格、上门时间，不要承诺一定修好。
- 客户要转人工、要投诉、要退款：只回"我帮你转给老板，稍等"。
"""

NON_TEXT_PLACEHOLDER = {
    "image": "（客户发来一张图片）",
    "voice": "（客户发来一条语音，你听不到内容）",
    "video": "（客户发来一段视频）",
    "file": "（客户发来一个文件）",
    "link": "（客户发来一个链接）",
    "location": "（客户发来一个位置）",
    "emotion": "（客户发来一个表情）",
    "personal_card": "（客户发来一张名片）",
    "other": "（客户发来一条非文字消息）",
}

# ---------- 日志 ----------


class Logger:
    """控制台 + 文件双写。文件按天切，方便回头查聊天记录。"""

    def __init__(self, path=None, verbose=True):
        self.path = path
        self.verbose = verbose
        self._fh = None

    def _write(self, level, msg):
        line = "[%s] [%s] %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), level, msg)
        if self.verbose:
            try:
                print(line, flush=True)
            except Exception:
                pass
        if not self.path:
            return
        try:
            if self._fh is None:
                os.makedirs(os.path.dirname(self.path), exist_ok=True)
                self._fh = open(self.path, "a", encoding="utf-8")
            self._fh.write(line + "\n")
            self._fh.flush()
        except Exception:
            pass

    def info(self, msg):
        self._write("INFO", msg)

    def warn(self, msg):
        self._write("WARN", msg)

    def error(self, msg):
        self._write("ERROR", msg)

    def close(self):
        try:
            if self._fh:
                self._fh.close()
        except Exception:
            pass
        self._fh = None


def make_logger(dry_run=False):
    name = "dry_run_%s.log" % time.strftime("%Y%m%d") if dry_run else "reply_%s.log" % time.strftime("%Y%m%d")
    return Logger(os.path.join(LOG_DIR, name))


# ---------- 配置 ----------


def read_dotenv(path):
    """极简 .env 解析：KEY=VALUE，忽略 # 注释。"""
    result = {}
    try:
        if not os.path.exists(path):
            return result
        with open(path, "r", encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                result[key.strip()] = value.strip().strip('"').strip("'")
    except Exception:
        pass
    return result


def _key_from_windows_registry():
    """从用户环境变量（注册表 HKCU\\Environment）兜底读 key。

    刚用 setx / SetEnvironmentVariable 设完环境变量时，已经打开的终端进程
    不会更新自己的环境块，这里补一条注册表兜底，避免"明明设了却说没 key"。
    """
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as reg_key:
            value, _ = winreg.QueryValueEx(reg_key, "DEEPSEEK_API_KEY")
        return str(value or "").strip()
    except Exception:
        return ""


def load_config(path=CONFIG_PATH):

    cfg = dict(DEFAULT_CONFIG)
    user_cfg = {}
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                user_cfg = loaded
        except Exception as exc:
            print("[WARN] 读取 config.json 失败，用默认配置：%s" % exc)
    for key in DEFAULT_CONFIG:
        if key in user_cfg and user_cfg[key] not in (None, ""):
            cfg[key] = user_cfg[key]
    # api_key 优先顺序：环境变量 > config.json > .env
    env_file = read_dotenv(ENV_PATH)
    key = (
        os.environ.get("DEEPSEEK_API_KEY")
        or cfg.get("api_key")
        or env_file.get("DEEPSEEK_API_KEY")
        or _key_from_windows_registry()
        or ""
    )
    cfg["api_key"] = str(key).strip()
    # 规整类型，避免手写 json 写错导致运行时报错
    cfg["targets"] = [str(t).strip() for t in (cfg.get("targets") or []) if str(t).strip()]
    for key in ("poll_interval", "debounce_seconds", "temperature"):
        try:
            cfg[key] = float(cfg[key])
        except (TypeError, ValueError):
            cfg[key] = float(DEFAULT_CONFIG[key])
    for key in ("history_limit", "history_char_budget", "max_reply_chars", "max_chunks",
                "max_tokens", "pause_minutes"):
        try:
            cfg[key] = int(cfg[key])
        except (TypeError, ValueError):
            cfg[key] = int(DEFAULT_CONFIG[key])
    cfg["max_reply_chars"] = max(10, cfg["max_reply_chars"])
    cfg["max_chunks"] = max(1, cfg["max_chunks"])
    cfg["dry_run"] = bool(cfg.get("dry_run"))
    return cfg


def write_default_config(path=CONFIG_PATH):
    """第一次运行时生成配置模板，方便直接改。"""
    if os.path.exists(path):
        return
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(DEFAULT_CONFIG, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


# ---------- 状态持久化 ----------


class StateStore:
    """记录每条消息是否处理过 + 哪些会话暂停了。写盘用临时文件替换，防止写坏。"""

    def __init__(self, path, max_ids=800):
        self.path = path
        self.max_ids = max_ids
        self.data = {"processed": {}, "paused": {}}
        self._sets = {}
        self.dirty = False
        self.load()

    def load(self):
        try:
            if os.path.exists(self.path):
                with open(self.path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    self.data["processed"] = data.get("processed") or {}
                    self.data["paused"] = data.get("paused") or {}
        except Exception:
            self.data = {"processed": {}, "paused": {}}

    def save(self, force=False):
        if not self.dirty and not force:
            return
        try:
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)
            self.dirty = False
        except Exception:
            pass

    def _idset(self, target):
        idset = self._sets.get(target)
        if idset is None:
            idset = set()
            for raw in self.data["processed"].get(target, []):
                try:
                    idset.add(int(raw))
                except (TypeError, ValueError):
                    continue
            self._sets[target] = idset
        return idset

    def is_processed(self, target, msg_id):
        if msg_id is None:
            return True
        try:
            msg_id = int(msg_id)
        except (TypeError, ValueError):
            return True
        return msg_id in self._idset(target)

    def mark_processed(self, target, ids):
        idset = self._idset(target)
        changed = False
        for raw in ids:
            try:
                mid = int(raw)
            except (TypeError, ValueError):
                continue
            if mid not in idset:
                idset.add(mid)
                changed = True
        if changed:
            self.data["processed"][target] = list(idset)[-self.max_ids:]
            self.dirty = True

    def pause(self, target, minutes, reason=""):
        self.data["paused"][target] = {
            "until": time.time() + max(1, int(minutes)) * 60,
            "reason": reason,
        }
        self.dirty = True

    def resume(self, target):
        if self.data["paused"].pop(target, None) is not None:
            self.dirty = True

    def paused_remaining(self, target):
        item = self.data["paused"].get(target)
        if not item:
            return 0
        remain = float(item.get("until", 0)) - time.time()
        if remain <= 0:
            self.data["paused"].pop(target, None)
            self.dirty = True
            return 0
        return int(remain)


# ---------- 消息工具 ----------


def msg_local_id(msg):
    """取消息在微信库里的唯一 id，用于去重。"""
    for attr in ("local_id", "id"):
        val = getattr(msg, attr, None)
        if val is None:
            continue
        try:
            return int(str(val).replace("db-", "").strip())
        except (TypeError, ValueError):
            continue
    return None


def msg_text(msg):
    """把一条微信消息转成给模型看的文本；非文字消息给个占位描述。"""
    mtype = str(getattr(msg, "type", "text") or "text")
    content = getattr(msg, "content", "") or ""
    if isinstance(content, bytes):
        try:
            content = content.decode("utf-8", errors="ignore")
        except Exception:
            content = ""
    content = str(content).strip()
    if mtype in ("text", "quote"):
        return content
    placeholder = NON_TEXT_PLACEHOLDER.get(mtype, NON_TEXT_PLACEHOLDER["other"])
    # 表情消息如果本来就是 "[动画表情]" 这类，不占一行上下文
    if mtype == "emotion" and content.startswith("["):
        return ""
    return placeholder


def msg_is_system(msg):
    """判断是不是系统消息（入群提示、撤回提示等，不该回复）。

    注意：wechatauto 的 _db_row_to_message 会把 attr 统一改成 self/friend，
    所以不能只看 is_system，要把 type == 'base'（SystemMessage 的 type）
    也一起当成系统消息。
    """
    if getattr(msg, "is_system", False):
        return True
    if str(getattr(msg, "type", "") or "") == "base":
        return True
    content = str(getattr(msg, "content", "") or "")
    for marker in ("撤回了一条消息", "以上是打招呼的内容", "你已添加了", "你们已成为好友"):
        if marker in content:
            return True
    return False


def resolve_self_sender_id(db, chat_wxid, self_wxid):
    """查出「自己」在这个会话消息库里的 sender id。

    背景（这个坑很关键）：wechatauto 判断"是不是自己发的"用的是 ``sender_id == 2``，
    但它读的是 message_resource.db 里的 SenderName2Id 表；实际消息表里的
    ``real_sender_id`` 对应的是同一个消息库里的 ``Name2Id`` 表。这两张表的编号
    在很多机器上并不一致 —— 比如本机 Name2Id 里自己是 3、对方是 73，
    于是机器人会把自己刚发出去的话当成客户消息，然后跟自己无限对聊。

    这里直接查消息库自己的 Name2Id：user_name == 自己的 wxid 的那一行 rowid，
    就是 real_sender_id 里表示"我"的值。查不到返回 None（调用方退回旧逻辑）。
    """
    if db is None or not chat_wxid or not self_wxid:
        return None
    try:
        found = db._msg_conn(chat_wxid)
        if not found:
            return None
        conn, _table = found
        try:
            for row in conn.execute("SELECT rowid, user_name FROM Name2Id"):
                if len(row) >= 2 and row[1] == self_wxid:
                    return int(row[0])
        finally:
            try:
                conn.close()
            except Exception:
                pass
    except Exception:
        return None
    return None


def history_from_messages(msgs, exclude_ids, history_limit, char_budget, is_self_func=None):
    """数据库返回的是「最新在前」，这里转成按时间正序的 role 对话列表。

    is_self_func: 判断一条消息是不是自己发的；不传就用库自带的 msg.is_self
    （注意库自带的判断在部分机器上不可靠，见 resolve_self_sender_id 的说明）。
    """
    if is_self_func is None:
        def is_self_func(m):
            return bool(getattr(m, "is_self", False))

    seen = set()
    turns = []
    for msg in reversed(list(msgs or [])):
        try:
            if msg_is_system(msg):
                continue
            mid = msg_local_id(msg)
            if mid is not None:
                if exclude_ids and mid in exclude_ids:
                    continue
                if mid in seen:
                    continue
                seen.add(mid)
            text = msg_text(msg)
            if not text:
                continue
            role = "assistant" if is_self_func(msg) else "user"
            turns.append({"role": role, "content": text})
        except Exception:
            continue
    if history_limit > 0:
        turns = turns[-history_limit:]
    # 字符预算：从最新往前留，超出就砍最早的那几条
    total = 0
    kept = []
    for turn in reversed(turns):
        total += len(turn["content"])
        if total > char_budget and kept:
            break
        kept.append(turn)
    kept.reverse()
    return kept


def clean_reply(text):
    """洗掉模型偶尔漏出来的 markdown / 前缀 / 空行。"""
    text = str(text or "").strip()
    if not text:
        return ""
    text = re.sub(r"^```[\s\S]*?```\s*$", "", text)
    text = re.sub(r"^\s*(你|我|阿梁|助理|助手)\s*[:：]\s*", "", text)
    text = text.replace("**", "").replace("__", "").replace("`", "")
    text = re.sub(r"^\s*[-*•·]\s+", "", text, flags=re.M)
    text = re.sub(r"^\s*\d+\s*[.、)）]\s+", "", text, flags=re.M)
    text = re.sub(r"\n{2,}", "\n", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return text.strip()


SENTENCE_RE = re.compile(r"[^。！？!?；;\n]+[。！？!?；;]?")


def _split_sentences(text):
    return [part.strip() for part in SENTENCE_RE.findall(text or "") if part.strip()]


def _pack_sentences(sentences, max_chars):
    """把句子按字数上限贪心拼成若干条。"""
    chunks = []
    current = ""
    for sentence in sentences:
        sentence = sentence.strip()
        if not sentence:
            continue
        if not current:
            current = sentence
        elif len(current) + len(sentence) <= max_chars:
            current += sentence
        else:
            chunks.append(current)
            current = sentence
    if current:
        chunks.append(current)
    # 单句仍然超长就硬切
    out = []
    for chunk in chunks:
        while len(chunk) > max_chars:
            out.append(chunk[:max_chars])
            chunk = chunk[max_chars:]
        if chunk:
            out.append(chunk)
    return out


def split_chunks(text, max_chars, max_chunks, natural_split=True):
    """把模型的一段回复切成要发的 1~N 条微信。

    两条路径：
    1. 模型主动用 ``||`` 或换行分条（"一句判断 || 一句下一步"）—— 主路径；
    2. 没分隔符时按句子打包；如果本来能塞进一条、但确实是两句像样的话，
       就自然拆成两条（真人常常连着说两句）。
    """
    text = str(text or "").strip()
    if not text:
        return []

    explicit = [part.strip() for part in re.split(r"\|\||\n+", text) if part.strip()]
    if len(explicit) > 1:
        out = []
        for part in explicit:
            out.extend(_pack_sentences(_split_sentences(part), max_chars))
        return out[:max_chunks]

    sentences = _split_sentences(text)
    if not sentences:
        return [text[:max_chars]][:max_chunks]

    if len(text) <= max_chars:
        # 两句以上、且不算太短 -> 拆两条，更像真人在连着说
        if natural_split and len(sentences) >= 2 and len(text) >= 24:
            split_at = max(1, len(sentences) // 2)
            first = "".join(sentences[:split_at]).strip()
            second = "".join(sentences[split_at:]).strip()
            if first and second:
                return [first, second][:max_chunks]
        return [text][:max_chunks]

    return _pack_sentences(sentences, max_chars)[:max_chunks]


# ---------- 对话引擎 ----------


def build_system_prompt(cfg):
    extra = str(cfg.get("persona_extra") or "").strip()
    if extra:
        return SYSTEM_PROMPT + "\n# 老板补充要求\n" + extra + "\n"
    return SYSTEM_PROMPT


class Advisor:
    """负责「聊天记录 + 新消息 -> 一条回复」。模型调用失败会把异常抛给调用方。"""

    def __init__(self, client, cfg, log):
        self.client = client
        self.cfg = cfg
        self.log = log
        self.system_prompt = build_system_prompt(cfg)

    def build_messages(self, history, user_text):
        # 合成一条 system：部分兼容接口只认第一条 system
        messages = [{"role": "system", "content": self.system_prompt + "\n\n" + CONTEXT_HINT}]
        messages.extend(history or [])
        messages.append({"role": "user", "content": user_text})
        return messages

    def reply(self, history, user_text):
        messages = self.build_messages(history, user_text)
        resp = self.client.chat.completions.create(
            model=self.cfg["model"],
            messages=messages,
            temperature=float(self.cfg["temperature"]),
            max_tokens=int(self.cfg["max_tokens"]),
            timeout=60,
        )
        raw = ""
        try:
            raw = resp.choices[0].message.content or ""
        except Exception:
            raw = ""
        reply = clean_reply(raw)
        if not reply:
            self.log.warn("模型返回了空内容（可能被限流），本条跳过")
        return reply


# ---------- 机器人主循环 ----------


class Bot:
    def __init__(self, cfg, log, state, wc, advisor, dry_run=False):
        self.cfg = cfg
        self.log = log
        self.state = state
        self.wc = wc
        self.advisor = advisor
        self.dry_run = dry_run
        self._chats = {}
        self._fail_counts = {}
        self._last_error_at = {}
        # 自己在各会话消息库里的 sender id（用来区分"这条是我发的"）
        self._self_sender_ids = {}
        self._self_id_retry_at = {}
        # 最近发出去的文本，作为兜底识别（万一 sender id 查不到）
        self._sent_texts = []
        # 启动那一刻已存在的消息 id：历史上下文不带上它们（只要新消息之后的事）
        self._baseline_ids = {}

    # -- 基础 ---------------------------------------------------------
    def _chat(self, target):
        chat = self._chats.get(target)
        if chat is None:
            chat = self.wc.GetSubWindow(target)
            if chat is None:
                raise RuntimeError(
                    "找不到聊天对象「%s」：targets 里要写微信里的备注或昵称，"
                    "并且要和对方有过聊天记录（库里能查到）" % target
                )
            self._chats[target] = chat
        return chat

    def _messages(self, target):
        chat = self._chat(target)
        return list(chat.GetAllMessage() or [])

    # -- 判断一条消息是不是自己发的 ------------------------------------
    def self_sender_id(self, target):
        """自己在本地消息库里的 sender id；查不到返回 None（60 秒后重试）。"""
        sid = self._self_sender_ids.get(target)
        if sid is not None:
            return sid
        now = time.time()
        if now - self._self_id_retry_at.get(target, 0) < 60:
            return None
        self._self_id_retry_at[target] = now
        sid = None
        try:
            db = getattr(self.wc, "_db", None)
            chat = self._chat(target)
            chat_wxid = getattr(chat, "_wxid", None)
            self_wxid = db.get_self_info().get("username") if db is not None else None
            sid = resolve_self_sender_id(db, chat_wxid, self_wxid)
        except Exception:
            sid = None
        if sid is not None:
            self._self_sender_ids[target] = sid
            self.log.info("[%s] 自己的 sender id = %s（用于区分自己发的消息）" % (target, sid))
        else:
            self.log.warn("[%s] 没查到自己的 sender id，退回库自带判断（可能误认自己的消息）" % target)
        return sid

    def _is_self_msg(self, target, msg):
        if getattr(msg, "is_self", False):
            return True
        sid = self.self_sender_id(target)
        if sid is not None:
            sender = getattr(msg, "wxid", None)
            try:
                if sender is not None and int(sender) == int(sid):
                    return True
            except (TypeError, ValueError):
                pass
        # 兜底：最近 2 分钟内自己发过一模一样的文本，那就是自己发的
        text = str(getattr(msg, "content", "") or "").strip()
        if text:
            now = time.time()
            for sent_text, sent_at in self._sent_texts:
                if now - sent_at < 120 and sent_text == text:
                    return True
        return False

    def _remember_sent(self, text):
        self._sent_texts.append((str(text).strip(), time.time()))
        if len(self._sent_texts) > 20:
            self._sent_texts = self._sent_texts[-20:]

    # -- 启动基线：不回复程序启动前就存在的旧消息 ----------------------
    def baseline(self, target):
        try:
            msgs = self._messages(target)
        except Exception as exc:
            self.log.warn("[%s] 刷新聊天对象失败（稍后会自动重试）：%s" % (target, exc))
            self._chats.pop(target, None)
            return
        ids = [msg_local_id(m) for m in msgs]
        self.state.mark_processed(target, [i for i in ids if i is not None])
        # 记下启动那一刻的消息，之后拼上下文时不带上它们（旧话题不掺和）
        self._baseline_ids[target] = set(i for i in ids if i is not None)
        self.log.info("[%s] 已建立基线：忽略 %d 条历史消息" % (target, len(ids)))
        if not ids:
            self.log.warn(
                "[%s] 读不到该会话的消息：确认 targets 里写的是微信备注/昵称，"
                "并且微信数据库可读（微信已登录）" % target
            )

    def baseline_all(self):
        for target in self.cfg["targets"]:
            self.baseline(target)

    # -- 收集新消息 ----------------------------------------------------
    def collect_new(self, target, seen_ids):
        """返回按时间正序的新消息；已处理过/自己发的/系统消息直接跳过。"""
        msgs = self._messages(target)
        rows = []
        for msg in reversed(msgs):
            mid = msg_local_id(msg)
            if mid is None or mid in seen_ids:
                continue
            seen_ids.add(mid)
            # state.json 里已经处理过的（含重启前处理过的）一律跳过，
            # 否则同一条消息会被反复回复 —— 这是最致命的刷屏 bug。
            if self.state.is_processed(target, mid):
                continue
            if self._is_self_msg(target, msg) or msg_is_system(msg):
                # 自己（或老板手动）回的消息、系统消息：只更新进度，不触发回复
                self.state.mark_processed(target, [mid])
                continue
            rows.append(msg)
        return rows

    def drain(self, target):
        """把当前所有待处理消息标成已处理并把文字拼出来，不回复。

        用在「暂停/人工接管」期间：老板已经在人工回了，机器人不要回头补答，
        但进度得跟上，否则恢复后会把旧消息一股脑全回了。
        """
        seen = set()
        rows = self.collect_new(target, seen)
        ids = [msg_local_id(m) for m in rows if msg_local_id(m) is not None]
        self.state.mark_processed(target, ids)
        return "\n".join(t for t in (msg_text(m) for m in rows) if t).strip()

    # -- 发送 ----------------------------------------------------------
    def _already_sent(self, target, text):
        """发送返回失败时回读数据库，确认是不是其实已经发出去了，避免重复。"""
        try:
            msgs = self._messages(target)
        except Exception:
            return False
        target_text = text.strip()
        for msg in msgs[:6]:
            if not self._is_self_msg(target, msg):
                continue
            if str(getattr(msg, "content", "") or "").strip() == target_text:
                mid = msg_local_id(msg)
                if mid is not None:
                    self.state.mark_processed(target, [mid])
                return True
        return False

    def send_text(self, target, text, retries=1):
        if self.dry_run:
            self.log.info("[DRY-RUN] 发给 %s：%s" % (target, text))
            return True
        last_err = ""
        for attempt in range(retries + 1):
            try:
                resp = self.wc.SendMsg(text, who=target)
                if resp:
                    self._remember_sent(text)
                    return True
                try:
                    last_err = str(resp.get("message", resp))
                except Exception:
                    last_err = str(resp)
            except Exception as exc:
                last_err = str(exc)
            self.log.warn("[%s] 发送失败（第 %d 次）：%s" % (target, attempt + 1, last_err))
            time.sleep(1.5)
            if self._already_sent(target, text):
                self.log.info("[%s] 回读数据库确认消息已发出，不重发" % target)
                return True
        return False

    def send_chunks(self, target, chunks):
        if not chunks:
            return False
        for index, chunk in enumerate(chunks):
            if not self.send_text(target, chunk):
                self.log.error("[%s] 这条没发出去，请手动补发：%s" % (target, chunk))
                return False
            if index < len(chunks) - 1:
                time.sleep(float(self.cfg.get("bubble_interval", 1.2)))
        return True

    # -- 单次处理 ------------------------------------------------------
    def handle(self, target):
        """处理一个会话的新消息。返回处理的轮数（0 表示没新消息）。"""
        # 暂停中（转人工/老板接管）：只推进进度，不回复
        remain = self.state.paused_remaining(target)
        if remain > 0:
            self.drain(target)
            return 0

        seen = set()
        rows = self.collect_new(target, seen)
        if not rows:
            return 0
        # 防抖：客户经常连发几条，等一下把后续一起收进来，只回一次
        debounce = float(self.cfg["debounce_seconds"])
        if debounce > 0:
            time.sleep(debounce)
            rows.extend(self.collect_new(target, seen))

        ids = [msg_local_id(m) for m in rows if msg_local_id(m) is not None]
        text = "\n".join(t for t in (msg_text(m) for m in rows) if t).strip()
        if not text:
            # 只有表情/空消息，不回复
            self.state.mark_processed(target, ids)
            return 0

        preview = text.replace("\n", " / ")
        self.log.info("[%s] 客户：%s" % (target, preview))

        # 转人工
        handoff = [k for k in (self.cfg.get("handoff_keywords") or []) if k and k in text]
        if handoff:
            minutes = int(self.cfg["pause_minutes"])
            self.state.pause(target, minutes, "命中关键词：%s" % "/".join(handoff))
            self.state.mark_processed(target, ids)
            self.log.info("[%s] 命中转人工关键词，自动回复暂停 %d 分钟，请老板自己接" % (target, minutes))
            return 0

        history = self.load_history(target, set(ids))

        try:
            reply = self.advisor.reply(history, text)
        except Exception as exc:
            failed = self._fail_counts.get(target, 0) + 1
            self._fail_counts[target] = failed
            self.log.error("[%s] 调模型失败（第 %d 次）：%s" % (target, failed, exc))
            if failed >= 3:
                self.log.error("[%s] 连续 3 次失败，先跳过这条，检查 API Key / 网络 / 余额" % target)
                self.state.mark_processed(target, ids)
                self._fail_counts[target] = 0
            return 0
        self._fail_counts[target] = 0

        chunks = split_chunks(
            reply,
            int(self.cfg["max_reply_chars"]),
            int(self.cfg["max_chunks"]),
            natural_split=bool(self.cfg.get("multi_bubble", True)),
        )
        if not chunks:
            self.state.mark_processed(target, ids)
            return 0

        self.log.info("[%s] 回复：%s" % (target, " || ".join(chunks)))
        self.send_chunks(target, chunks)
        # 无论发送是否成功都标记已处理：发送模块内部已重试并告警，
        # 不标记会导致同一条消息被无限重答、刷屏。
        self.state.mark_processed(target, ids)
        return 1

    def load_history(self, target, exclude_ids):
        try:
            msgs = self._messages(target)
        except Exception as exc:
            self.log.warn("[%s] 读聊天记录失败，本轮只带当前消息：%s" % (target, exc))
            self._chats.pop(target, None)
            return []
        excluded = set(exclude_ids or ())
        # 默认只带"本次启动之后"的聊天：客户之前问过的事、老话题都不掺和，
        # 避免像"客户只说了句 hello，它却接着上周的电脑问题往下聊"。
        if str(self.cfg.get("history_scope") or "session") != "recent":
            excluded |= self._baseline_ids.get(target, set())
        return history_from_messages(
            msgs,
            excluded,
            int(self.cfg["history_limit"]),
            int(self.cfg["history_char_budget"]),
            is_self_func=lambda m: self._is_self_msg(target, m),
        )

    # -- 主循环 --------------------------------------------------------
    def run_once(self):
        if os.path.exists(PAUSE_FLAG):
            return 0
        total = 0
        for target in self.cfg["targets"]:
            try:
                total += self.handle(target)
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                # 出错别每秒钟刷屏：同一目标 60 秒内只报一次
                now = time.time()
                if now - self._last_error_at.get(target, 0) > 60:
                    self._last_error_at[target] = now
                    self.log.error("[%s] 处理异常（稍后自动重试）：%s" % (target, exc))
                    self.log.error(traceback.format_exc())
                self._chats.pop(target, None)
        self.state.save()
        return total

    def run(self):
        interval = max(0.3, float(self.cfg["poll_interval"]))
        self.log.info("开始接待：%s（模型 %s，%s）" % (
            "、".join(self.cfg["targets"]), self.cfg["model"],
            "预览模式，不发送" if self.dry_run else "正式模式"))
        self.log.info("提示：同目录放一个 PAUSE 文件可随时暂停；Ctrl+C 退出")
        while True:
            if os.path.exists(PAUSE_FLAG):
                time.sleep(max(2.0, interval * 3))
                continue
            self.run_once()
            time.sleep(interval)


# ---------- wechatauto 库的小补丁 ----------


def patch_wechatauto():
    """修 wechatauto 1.2.1 的一个 bug：guia.py 用了 threading 却没 import，
    导致日志里一直刷「布局校准失败：name 'threading' is not defined」。
    这里在运行时补进去，不改 site-packages 里的文件。"""
    try:
        import threading
        import wechatauto.guia as guia_module
        if not hasattr(guia_module, "threading"):
            guia_module.threading = threading
            return True
    except Exception:
        pass
    return False


# ---------- 自测（不连微信、不调 API）----------


class _FakeMessage:
    def __init__(self, local_id, content, is_self=False, mtype="text", sender=None):
        self.local_id = local_id
        self.content = content
        self.type = mtype
        self.attr = "self" if is_self else "friend"
        if sender is not None:
            self.wxid = sender
        else:
            self.wxid = 2 if is_self else 73

    @property
    def is_self(self):
        return self.attr == "self"

    @property
    def is_system(self):
        return self.attr == "system"


class _FakeChat:
    def __init__(self, name, wxid="wxid_customer"):
        self.who = name
        self._wxid = wxid
        self._rows = []
        self._next_id = 1

    def push(self, content, is_self=False, mtype="text", sender=None):
        msg = _FakeMessage(self._next_id, content, is_self, mtype, sender)
        self._next_id += 1
        self._rows.append(msg)
        return msg

    def GetAllMessage(self):
        return list(reversed(self._rows))

    def SendMsg(self, text, who=None):
        self.push(text, is_self=True)
        return _FakeResponse()


class _FakeConn:
    """假的消息库连接：Name2Id 里 rowid=2 是"自己"。"""

    @staticmethod
    def execute(sql, *args):
        if "Name2Id" in sql:
            return [(1, "wxid_other"), (2, "wxid_self")]
        return []

    def close(self):
        pass


class _FakeDB:
    def get_self_info(self):
        return {"username": "wxid_self", "nick_name": "L"}

    def _msg_conn(self, wxid):
        return (_FakeConn(), "Msg_fake")


class _FakeResponse(dict):
    def __init__(self, ok=True, message="成功"):
        super().__init__(status="成功" if ok else "失败", message=message, data=None)

    @property
    def is_success(self):
        return self.get("status") == "成功"

    def __bool__(self):
        return self.is_success


class _FakeWeChat:
    def __init__(self):
        self.chats = {}
        self._db = _FakeDB()

    def add_chat(self, name):
        chat = _FakeChat(name)
        self.chats[name] = chat
        return chat

    def GetSubWindow(self, name):
        return self.chats.get(name)

    def SendMsg(self, text, who=None):
        chat = self.chats.get(who)
        if chat is None:
            return _FakeResponse(False, "找不到会话")
        return chat.SendMsg(text, who)


class _FakeClient:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []
        self.chat = self
        self.completions = self

    def create(self, **kwargs):
        self.calls.append(kwargs)
        content = self.replies.pop(0) if self.replies else "好的"
        message = type("Msg", (), {"content": content})()
        choice = type("Choice", (), {"message": message})()
        return type("Resp", (), {"choices": [choice]})()


def run_selftest():
    _init_stdout()
    print("=" * 60)
    print("离线自测：不连微信、不调 API，只验证对话流程")
    print("=" * 60)
    passed = 0
    failed = 0

    def check(name, condition, detail=""):
        nonlocal passed, failed
        if condition:
            passed += 1
            print("  [PASS] %s" % name)
        else:
            failed += 1
            print("  [FAIL] %s %s" % (name, detail))

    cfg = dict(DEFAULT_CONFIG)
    cfg.update({
        "targets": ["测试客户"],
        "debounce_seconds": 0,
        "poll_interval": 0,
        "history_limit": 30,
        "max_reply_chars": 60,
        "max_chunks": 2,
    })
    log = Logger(path=None, verbose=False)
    tmp_dir = tempfile.mkdtemp(prefix="wxbot_selftest_")
    state = StateStore(os.path.join(tmp_dir, "state.json"))
    wc = _FakeWeChat()
    chat = wc.add_chat("测试客户")
    client = _FakeClient([
        "什么问题？",
        "先看下键盘大小写灯亮不亮？亮着说明主机没死机。",
        "那把主机断电，内存条拔下来重新插紧再试。",
        "风扇响一般是积灰或者轴承老了，先断电清个灰试试。",
        "还是黑屏的话，先换根信号线试试。",
    ])
    advisor = Advisor(client, cfg, log)
    # 用假微信跑，所以 dry_run 关掉：这样机器人"发出去"的回复会进假聊天记录，
    # 才能验证下一轮是否把上一轮的回复也带给了模型。
    bot = Bot(cfg, log, state, wc, advisor, dry_run=False)

    # 1) 启动基线：旧消息不回复
    chat.push("在吗")                    # 历史旧消息
    chat.push("我是老客户")                # 历史旧消息
    bot.baseline("测试客户")
    check("启动时旧消息不回复", bot.handle("测试客户") == 0)

    # 2) 第一轮
    chat.push("电脑出了点问题")
    check("新消息触发一次回复", bot.handle("测试客户") == 1)
    check("模型拿到正确的新消息", client.calls[-1]["messages"][-1]["content"] == "电脑出了点问题")
    check("系统提示词已带上", client.calls[-1]["messages"][0]["role"] == "system")

    # 3) 第二轮：必须带上上一轮的问答（这就是多轮的关键）
    chat.push("屏幕黑了，但机箱是亮的")
    bot.handle("测试客户")
    sent = client.calls[-1]["messages"]
    roles = [m["role"] for m in sent]
    contents = [m["content"] for m in sent]
    check("第二轮把上一轮客户的话带给了模型", "电脑出了点问题" in contents)
    check("第二轮把上一轮自己的回复也带给了模型", "什么问题？" in contents)
    check("聊天记录按 user/assistant 交替", roles[0] == "system" and roles[1:] == ["user", "assistant", "user"],
          str(roles))
    check("本轮新消息没有重复出现", contents.count("屏幕黑了，但机箱是亮的") == 1)

    # 4) 连发多条合并成一次回复
    calls_before = len(client.calls)
    chat.push("重启了")
    chat.push("还是黑屏")
    bot.handle("测试客户")
    check("连发两条只回一次", len(client.calls) - calls_before == 1)
    merged = client.calls[-1]["messages"][-1]["content"]
    check("连发内容被合并", "重启了" in merged and "还是黑屏" in merged)

    # 5) 没有新消息时不调用模型
    calls_before = len(client.calls)
    check("无新消息不回复", bot.handle("测试客户") == 0 and len(client.calls) == calls_before)

    # 6) 自己发的消息不触发回复
    chat.push("这是老板手动回的", is_self=True)
    check("自己发的消息不触发", bot.handle("测试客户") == 0)

    # 6.1) 本机真实的坑：库把自己的消息错认成客户的（sender id 其实就是我）
    check("能查到自己的 sender id", bot.self_sender_id("测试客户") == 2)
    calls_before = len(client.calls)
    chat.push("我这边刚发出去的话", is_self=False, sender=2)
    check("库认错时也能认出自己的消息",
          bot.handle("测试客户") == 0 and len(client.calls) == calls_before)

    # 6.2) 上下文只含"本次启动之后"的消息，旧话题不掺和
    chat.push("我电脑风扇很响")
    bot.handle("测试客户")
    contents = [m["content"] for m in client.calls[-1]["messages"]]
    check("上下文不含启动前的旧消息", "在吗" not in contents and "我是老客户" not in contents)
    check("上下文含启动后的新消息", "我电脑风扇很响" in contents)

    # 7) 转人工关键词 -> 暂停
    chat.push("我要转人工")
    bot.handle("测试客户")
    check("转人工后自动暂停", state.paused_remaining("测试客户") > 0)
    calls_before = len(client.calls)
    paused_msg = chat.push("老板在吗")
    bot.handle("测试客户")
    check("暂停期间不再自动回复", len(client.calls) == calls_before)
    check("暂停期间消息不积压", state.is_processed("测试客户", paused_msg.local_id))
    state.resume("测试客户")

    # 8) 分段 / 连续发多条
    chunks = split_chunks("第一句话。第二句话。第三句话。", 8, 2)
    check("长回复会分段", len(chunks) == 2 and all(len(c) <= 8 for c in chunks))
    check("空回复不分段", split_chunks("", 60, 3) == [])
    check("模型用 || 分条就发成两条",
          split_chunks("能啊。||你问吧。", 60, 3) == ["能啊。", "你问吧。"])
    two = split_chunks("那说明主机没坏，是显示器那边的事。你先换根信号线试试看。", 60, 3)
    check("两句像样的话会拆成两条", len(two) == 2, str(two))
    one = split_chunks("那就先这么用，还有问题随时喊我。", 60, 3)
    check("只有一句话不会被硬拆", len(one) == 1, str(one))

    # 9) 系统消息、非文字消息
    calls_before = len(client.calls)
    chat.push("你已添加了测试客户，现在可以开始聊天了", mtype="base")
    bot.handle("测试客户")
    check("系统消息不回复", len(client.calls) == calls_before)

    chat.push("", mtype="image")
    bot.handle("测试客户")
    check("图片消息转成占位描述", "图片" in client.calls[-1]["messages"][-1]["content"])

    # 10) 状态持久化：重新加载后仍记得已处理
    state.save(force=True)
    reloaded = StateStore(os.path.join(tmp_dir, "state.json"))
    check("进度已落盘", reloaded.is_processed("测试客户", 3))

    # 清理临时文件
    try:
        for name in os.listdir(tmp_dir):
            os.remove(os.path.join(tmp_dir, name))
        os.rmdir(tmp_dir)
    except Exception:
        pass

    print("-" * 60)
    print("自测结果：%d 通过，%d 失败" % (passed, failed))
    if failed == 0:
        print("全部通过，可以放心运行：python auto_reply_v2.py --dry-run")
        return 0
    print("有失败项，先把上面的 FAIL 发给开发者看。")
    return 1


# ---------- 入口 ----------


def _init_stdout():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def print_key_help(cfg_path):
    print("")
    print("=" * 60)
    print("还没配置 DeepSeek API Key，先选一种方式：")
    print("")
    print("  方式一（推荐）：新建 %s 文件，写一行：" % ENV_PATH)
    print("      DEEPSEEK_API_KEY=sk-你的key")
    print("")
    print("  方式二：编辑 %s，把 \"api_key\" 填上" % cfg_path)
    print("")
    print("  方式三：设置系统环境变量 DEEPSEEK_API_KEY")
    print("")
    print("  （旧版 auto_reply.py 里有你原来的 key，复制过来即可）")
    print("=" * 60)


def split_targets(text):
    """把 "张三,李四" / "张三，李四" 拆成列表。"""
    parts = re.split(r"[,，;；]+", str(text or ""))
    return [p.strip() for p in parts if p.strip()]


def print_recent_chats(wc, limit=30):
    """打印最近的会话名字，方便照着填 targets。"""
    print("=" * 64)
    print("最近的会话（targets 里就填这些名字，必须是微信里显示的备注/昵称）")
    print("=" * 64)
    try:
        sessions = wc.GetSession() or []
    except Exception as exc:
        print("读取会话列表失败：%s" % exc)
        return 1
    if not sessions:
        print("（没读到会话，确认微信已登录、且和对方有聊天记录）")
        return 0
    print("%-22s %-8s %s" % ("名字", "未读", "最近一条"))
    print("-" * 64)
    for item in sessions[:limit]:
        name = getattr(item, "name", "") or getattr(item, "username", "") or ""
        unread = getattr(item, "unread", 0)
        summary = str(getattr(item, "summary", "") or "").replace("\n", " ")[:26]
        print("%-22s %-8s %s" % (name, unread, summary))
    print("")
    print("用法（不改配置文件，临时指定）：")
    print('    启动微信助手.bat --targets "这里填名字"')
    print('    多个：启动微信助手.bat --targets "张三,李四"')
    return 0


def main(argv=None):
    _init_stdout()
    parser = argparse.ArgumentParser(description="微信 AI 维修顾问 v2（多轮对话版）")
    parser.add_argument("--dry-run", action="store_true", help="只打印将要发送的内容，不真的发")
    parser.add_argument("--selftest", action="store_true", help="离线自测，不连微信不调 API")
    parser.add_argument("--once", action="store_true", help="只扫一轮就退出")
    parser.add_argument("--config", default=CONFIG_PATH, help="配置文件路径")
    parser.add_argument("--targets", default="", help="临时指定回复对象，多个用逗号隔开（不改配置文件）")
    parser.add_argument("--list", action="store_true", help="列出最近的会话名字，照着填 targets")
    args = parser.parse_args(argv)

    if args.selftest:
        return run_selftest()

    os.makedirs(LOG_DIR, exist_ok=True)
    write_default_config(args.config)
    cfg = load_config(args.config)
    if args.targets.strip():
        cfg["targets"] = split_targets(args.targets)
    dry_run = bool(args.dry_run or cfg.get("dry_run"))
    log = make_logger(dry_run)
    if args.targets.strip():
        log.info("本次回复对象（命令行指定，不写入配置）：%s" % "、".join(cfg["targets"]))

    if patch_wechatauto():
        log.info("已修补 wechatauto 的 threading 缺失问题（布局校准不再报错）")

    if args.list:
        try:
            from wechatauto import WeChat
        except Exception as exc:
            print("[ERROR] 依赖没装好：%s" % exc)
            return 2
        try:
            wc = WeChat()
        except Exception as exc:
            log.error("连接微信失败：%s" % exc)
            return 3
        log.info("微信已连接，当前登录：%s" % getattr(wc, "nickname", "未知"))
        return print_recent_chats(wc)

    if not cfg["targets"]:
        print("[ERROR] 没指定回复对象：改 config.json 的 targets，或用 --targets \"名字\"")
        print("提示：先运行  启动微信助手.bat --list  看看有哪些会话名可以填")
        return 2
    if not cfg["api_key"]:
        print_key_help(args.config)
        return 2

    try:
        from openai import OpenAI
        from wechatauto import WeChat
    except Exception as exc:
        print("[ERROR] 依赖没装好：%s" % exc)
        print("请先运行：python -m pip install openai wechatauto")
        return 2

    client = OpenAI(api_key=cfg["api_key"], base_url=cfg["base_url"], timeout=60, max_retries=2)
    log.info("正在连接微信，请保证微信已登录且窗口没被最小化…")
    try:
        wc = WeChat()
    except Exception as exc:
        log.error("连接微信失败：%s" % exc)
        return 3

    advisor = Advisor(client, cfg, log)
    state_path = os.path.join(BASE_DIR, "state_dryrun.json" if dry_run else "state.json")
    state = StateStore(state_path)
    bot = Bot(cfg, log, state, wc, advisor, dry_run=dry_run)

    log.info("微信已连接，当前登录：%s" % getattr(wc, "nickname", "未知"))
    bot.baseline_all()
    try:
        if args.once:
            bot.run_once()
        else:
            bot.run()
    except KeyboardInterrupt:
        log.info("收到 Ctrl+C，退出")
    finally:
        state.save(force=True)
        log.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
