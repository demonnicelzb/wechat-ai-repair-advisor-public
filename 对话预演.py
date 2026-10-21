# -*- coding: utf-8 -*-
"""对话预演 —— 不连微信，只在命令行里跟同一个"维修师傅"聊天

用途：调语气、调人设的时候用它，比在微信里试快得多，也不怕回错客户。
它和 auto_reply_v2.py 用的是同一套系统提示词、同一套多轮上下文逻辑、同一个模型。

用法：
    python 对话预演.py
    然后像客户一样打字，回车发送；输入 exit 退出。
    想看模型在第 3 轮是否记得第 1 轮，就照着真实客户那样一句一句回。
"""

import os
import sys

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

from auto_reply_v2 import Advisor, Logger, load_config  # noqa: E402


def main():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    cfg = load_config(os.path.join(BASE_DIR, "config.json"))
    if not cfg["api_key"]:
        print("还没配置 API Key：在 .env 里写 DEEPSEEK_API_KEY=sk-xxx，或填 config.json 的 api_key")
        return 2

    try:
        from openai import OpenAI
    except Exception as exc:
        print("没装 openai：python -m pip install openai（%s）" % exc)
        return 2

    client = OpenAI(api_key=cfg["api_key"], base_url=cfg["base_url"], timeout=60, max_retries=2)
    advisor = Advisor(client, cfg, Logger(path=None, verbose=False))

    print("=" * 60)
    print("对话预演（模型 %s，单条限 %d 字）" % (cfg["model"], cfg["max_reply_chars"]))
    print("你现在扮演客户，随便打；输入 exit 退出")
    print("=" * 60)
    print("师傅> 你好，电脑有什么问题？")

    history = []
    while True:
        try:
            line = input("客户> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("")
            break
        if not line:
            continue
        if line.lower() in ("exit", "quit", "退出", "q"):
            break
        try:
            reply = advisor.reply(history, line)
        except Exception as exc:
            print("调用模型失败：%s" % exc)
            break
        if not reply:
            print("师傅> （模型没返回内容，可能网络或余额问题）")
            continue
        print("师傅> %s" % reply)
        history.append({"role": "user", "content": line})
        history.append({"role": "assistant", "content": reply})
    return 0


if __name__ == "__main__":
    sys.exit(main())
