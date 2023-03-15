import logging
fmt = "%(asctime)s %(levelname)s %(name)s :%(message)s"
logging.basicConfig(level=logging.INFO, format=fmt)

from typing import List, Dict
from util import get_history_identifier, get_user_identifier, calculate_num_tokens, calculate_num_tokens_by_prompt, say_ts

import re
import json
import openai
import os
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

from dotenv import load_dotenv
load_dotenv()

openai.organization = os.getenv("ORGANAZTION_ID")
openai.api_key = os.getenv("OPENAI_API_KEY")

# ボットトークンとソケットモードハンドラーを使ってアプリを初期化
app = App(token=os.getenv("SLACK_BOT_TOKEN"))

# 現在使用中のユーザーのセット、複数リクエストを受けると履歴が壊れることがあるので、一つのユーザーに対しては一つのリクエストしか受け付けないようにする
using_user_set = set()  
# key: historyIdetifier value: historyArray ex. [{"role": "user", "content": prompt}]
history_dict : Dict[str, List[Dict[str, str]]] = {}

MAX_TOKEN_SIZE = 4096  # トークンの最大サイズ
COMPLETION_MAX_TOKEN_SIZE = 1024  # ChatCompletionの出力の最大トークンサイズ
INPUT_MAX_TOKEN_SIZE = MAX_TOKEN_SIZE - COMPLETION_MAX_TOKEN_SIZE  # ChatCompletionの入力に使うトークンサイズ

pages = json.load(open("./text.json", encoding="utf8"))["pages"]
titles = (p["title"] for p in pages)

def append_history_and_size_fix(message, role, content):
    """
    会話のヒストリーを追加して、トークンのサイズがINPUT_MAX_TOKEN_SIZEを超えたら古いものを削除する
    """
    history_idetifier = get_history_identifier(
                message["team"], message["channel"], message["user"])

    # ヒストリーを取得
    history_array: List[Dict[str, str]] = []
    if history_idetifier in history_dict.keys():
        history_array = history_dict[history_idetifier]
    history_array.append({"role": role, "content": content})

    # トークンのサイズがINPUT_MAX_TOKEN_SIZEを超えたら古いものを削除
    while calculate_num_tokens(history_array) > INPUT_MAX_TOKEN_SIZE:
        history_array = history_array[1:]
    history_dict[history_idetifier] = history_array # ヒストリーを更新
    

@app.message(re.compile(r"^!il-s$"))
def message_start(client, message, say, context, logger):
    try:
        if message["user"] in using_user_set:
            say_ts(client, message, f"<@{message['user']}> さんは既に対話学習を開始されています。")
        else:
            using_user_set.add(message["user"])

            message_il_start = f"<@{message['user']}> さんの対話学習を開始しました。以下の学習内容から演習したい内容を選択してください。\n"
            for title in enumerate(titles):
                message_il_start += f"- {title}\n"

            logger.info(message_il_start)
            say_ts(client, message, message_il_start)
            append_history_and_size_fix(message, "agent", message_il_start)

    except Exception as e:
        logger.error(e)
        say_ts(client, message, f"エラーが発生しました。やり方を変えて再度試してみてください。 Error: {e}")


@app.message(re.compile(r"^!il-f$"))
def message_finish(client, message, say, context, logger):
    try:
        if message["user"] in using_user_set:
            using_user_set.remove(message["user"]) # ユーザーを解放
            logger.info(f"<@{message['user']}> さんの対話学習を終了しました。")
            say_ts(client, message, f"<@{message['user']}> さんの対話学習を終了しました。")
        else:
            say_ts(client, message, f"<@{message['user']}> さんは対話学習を開始していません")
    except Exception as e:
        logger.error(e)
        say_ts(client, message, f"エラーが発生しました。やり方を変えて再度試してみてください。 Error: {e}")

@app.message(re.compile(r"^!il ((.|\s)*)$"))
def message_gpt(client, message, say, context, logger):
    try:
        if message["user"] in using_user_set:
            # TODO 最初の回答に答える

            say_ts(client, message, f"<@{message['user']}> さんの返答に対応中なのでお待ちください。")
        else:
            using_user_set.add(message["user"])
            using_team = message["team"]
            using_channel = message["channel"]
            history_idetifier = get_history_identifier(
                using_team, using_channel, message["user"])
            user_identifier = get_user_identifier(using_team, message["user"])

            prompt = context["matches"][0]

            # ヒストリーを取得
            history_array: List[Dict[str, str]] = []
            if history_idetifier in history_dict.keys():
                history_array = history_dict[history_idetifier]
            history_array.append({"role": "user", "content": prompt})

            # トークンのサイズがINPUT_MAX_TOKEN_SIZEを超えたら古いものを削除
            while calculate_num_tokens(history_array) > INPUT_MAX_TOKEN_SIZE:
                history_array = history_array[1:]

            # 単一の発言でMAX_TOKEN_SIZEを超えたら、対応できない
            if(len(history_array) == 0):
                messege_out_of_token_size = f"発言内容のトークン数が{INPUT_MAX_TOKEN_SIZE}を超えて、{calculate_num_tokens_by_prompt(prompt)}であったため、対応できませんでした。"
                say_ts(client, message, messege_out_of_token_size)
                logger.info(messege_out_of_token_size)
                using_user_set.remove(message["user"]) # ユーザーを解放
                return
            
            say_ts(client, message, f"<@{message['user']}> さんの以下の発言に対応中（履歴数: {len(history_array)} 、トークン数: {calculate_num_tokens(history_array)}）\n```\n{prompt}\n```")

            # ChatCompletionを呼び出す
            logger.info(f"user: {message['user']}, prompt: {prompt}")
            response = openai.ChatCompletion.create(
                model="gpt-3.5-turbo",
                messages=history_array,
                top_p=1,
                n=1,
                max_tokens=COMPLETION_MAX_TOKEN_SIZE,
                temperature=1,  # 生成する応答の多様性
                presence_penalty=0,
                frequency_penalty=0,
                logit_bias={},
                user=user_identifier
            )
            logger.debug(response)

            # ヒストリーを新たに追加
            new_response_message = response["choices"][0]["message"]
            history_array.append(new_response_message)

            # トークンのサイズがINPUT_MAX_TOKEN_SIZEを超えたら古いものを削除
            while calculate_num_tokens(history_array) > INPUT_MAX_TOKEN_SIZE:
                history_array = history_array[1:]
            history_dict[history_idetifier] = history_array # ヒストリーを更新

            say_ts(client, message, new_response_message["content"])
            logger.info(f"user: {message['user']}, content: {new_response_message['content']}")

            using_user_set.remove(message["user"]) # ユーザーを解放
    except Exception as e:
        using_user_set.remove(message["user"]) # ユーザーを解放
        logger.error(e)
        say_ts(client, message, f"エラーが発生しました。やり方を変えて再度試してみてください。 Error: {e}")

        # エラーを発生させた人の会話の履歴をリセットをする
        history_idetifier = get_history_identifier(
            message["team"], message["channel"], message["user"])
        history_dict[history_idetifier] = []

@app.message("hello")
def message_hello(message, say):
    say(f"こんにちは <@{message['user']}> さん！")
    say_ts(client, message, f"こんにちは <@{message['user']}> さん！")

@app.message(re.compile(r"^!il-help$"))
def message_help(client, message, say, context, logger):
    say_ts(client, message, f"`!il-s` 対話学習をスタートします。\n" +
        "`!il [回答内容]` 問われた問題に対して回答します。\n" +
        "`!il-f` 対話学習を終了します。\n")

@app.event("message")
def handle_message_events(body, logger):
    logger.debug(body)

# アプリを起動
if __name__ == "__main__":
    SocketModeHandler(app, os.getenv("SLACK_APP_TOKEN")).start()