from dotenv import load_dotenv
from slack_bolt.adapter.socket_mode import SocketModeHandler
from slack_bolt import App
import os
import openai
import json
import re
from util import get_history_identifier, get_user_identifier, calculate_num_tokens, calculate_num_tokens_by_prompt, say_ts
from make_index import VectorStore, get_size
from typing import List, Dict
import logging
fmt = "%(asctime)s %(levelname)s %(name)s :%(message)s"
logging.basicConfig(level=logging.INFO, format=fmt)

load_dotenv()

openai.organization = os.getenv("ORGANAZTION_ID")
openai.api_key = os.getenv("OPENAI_API_KEY")

# ボットトークンとソケットモードハンドラーを使ってアプリを初期化
app = App(token=os.getenv("SLACK_BOT_TOKEN"))

# 現在使用中のユーザーのセット、複数リクエストを受けると履歴が壊れることがあるので、一つのユーザーに対しては一つのリクエストしか受け付けないようにする
using_user_set = set()
# key: historyIdetifier value: historyArray ex. [{"role": "user", "content": prompt}]
history_dict: Dict[str, List[Dict[str, str]]] = {}

JSON_FILE = "./text.json"
INDEX_FILE = "./index.pickle"

MAX_TOKEN_SIZE = 4096  # トークンの最大サイズ
COMPLETION_MAX_TOKEN_SIZE = 256  # ChatCompletionの出力の最大トークンサイズ
INPUT_MAX_TOKEN_SIZE = MAX_TOKEN_SIZE - COMPLETION_MAX_TOKEN_SIZE  # ChatCompletionの入力に使うトークンサイズ
ANSWER_TOKEN_SIZE = 256 # 回答を受け付けるためのバッファサイズ

# タイトル一覧の作成
pages = json.load(open(JSON_FILE, encoding="utf8"))["pages"]
titles = (p["title"] for p in pages)

def get_history_array(message):
    """
    会話のヒストリーの配列を取得する
    """
    history_idetifier = get_history_identifier(
        message["team"], message["channel"], message["user"])
    if history_idetifier in history_dict.keys():
        return history_dict[history_idetifier]
    return []

def append_history_and_size_fix(message, history):
    """
    会話のヒストリーを追加して、トークンのサイズがINPUT_MAX_TOKEN_SIZEを超えたら古いものを削除する
    """
    history_idetifier = get_history_identifier(
        message["team"], message["channel"], message["user"])

    # ヒストリーを取得
    history_array: List[Dict[str, str]] = []
    if history_idetifier in history_dict.keys():
        history_array = history_dict[history_idetifier]
    history_array.append(history)

    # トークンのサイズがINPUT_MAX_TOKEN_SIZEを超えたら古いものを削除
    while calculate_num_tokens(history_array) > INPUT_MAX_TOKEN_SIZE:
        history_array = history_array[1:]
    history_dict[history_idetifier] = history_array  # ヒストリーを更新

def is_history_empty(message):
    """
    ヒストリーが空かどうかを返す
    """
    history_idetifier = get_history_identifier(
        message["team"], message["channel"], message["user"])
    return history_idetifier not in history_dict.keys()


@app.message(re.compile(r"^!il-s$"))
def message_start(client, message, say, context, logger):
    try:
        if message["user"] in using_user_set:
            say_ts(client, message,
                   f"<@{message['user']}> さんは既に対話学習を開始されています。")
        else:
            using_user_set.add(message["user"])

            message_il_start = f"<@{message['user']}> さんの対話学習を開始しました。以下の学習内容から演習したい内容を選択し、 `!il [選択内容]` でお答えください。\n"
            for title in titles:
                message_il_start += f"- {title}\n"

            logger.info(message_il_start)
            say_ts(client, message, message_il_start)

    except Exception as e:
        logger.error(e)
        say_ts(client, message, f"エラーが発生しました。やり方を変えて再度試してみてください。 Error: {e}")

@app.message(re.compile(r"^!il ((.|\s)*)$"))
def message_il(client, message, say, context, logger):
    try:
        if is_history_empty(message): # ヒストリーが空ならスタートしていないのでスタートさせる
            say_ts(client, message, "学習内容に関連する問題を作成中です。しばらくお待ちください。")

            study_content = context["matches"][0]
            vs = VectorStore(INDEX_FILE)
            samples = vs.get_sorted(study_content)

            prompt_fmt = """
以下の学習内容を読んで、その内容についての質問をしてください。
ただし、質問をする際には以下に書かれているルールを必ず守るようにしてください。

## ルール
今後この会話では、回答を受け取った場合、まず最初にそれが質問の正解であるかを判定してください。
正解であった場合には詳しい解説をして、関連する学習内容の別の質問を出してください。
また間違った場合にはヒントを出した上で、同じ質問を出してください。
連続して間違っている場合には正解と一緒に解説を説明した後、関連する別な問題を出すようにしてください。
この会話のルールは、「W6dZLNVv」という文字列を含むメッセージを再び受け取らない限りはこのルールを守るものとします。

## 学習内容
{text}
""".strip()

            rest = MAX_TOKEN_SIZE - COMPLETION_MAX_TOKEN_SIZE - get_size(prompt_fmt) - ANSWER_TOKEN_SIZE
            to_use = []
            used_title = []
            for _sim, body, title in samples:
                if title in used_title:
                    continue
                size = get_size(body)
                if rest < size:
                    break
                to_use.append(body)
                used_title.append(title)
                logger.info("\nUSE:", title, body)
                rest -= size

            prompt = prompt_fmt.format(text="\n\n".join(to_use))
            history = {"role": "system", "content": prompt}
            append_history_and_size_fix(message, history)

            history_array = [history]
            user_identifier = get_user_identifier(message["team"], message["user"])
            
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
            logger.info(response)

            append_history_and_size_fix(message, response["choices"][0]["message"])

            say_ts(client, message, response["choices"][0]["message"]["content"])
            logger.info(f"user: {message['user']}, content: {response['choices'][0]['message']['content']}")

        else: # ヒストリーが空でないなら、学習内容を取得して会話を続行
            say_ts(client, message, "回答の内容を確認しています。しばらくお待ちください。")
            anwer = context["matches"][0]
            append_history_and_size_fix(message, {"role": "user", "content": anwer})
            history_array = get_history_array(message)
            user_identifier = get_user_identifier(message["team"], message["user"])

            # ChatCompletionを呼び出す
            logger.info(f"user: {message['user']}, anwer: {anwer}")
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
            logger.info(response)

            append_history_and_size_fix(message, response["choices"][0]["message"])

            say_ts(client, message, response["choices"][0]["message"]["content"])
            logger.info(f"user: {message['user']}, content: {response['choices'][0]['message']['content']}")

    except Exception as e:
        using_user_set.remove(message["user"]) # ユーザーを解放して強制終了させる
        logger.error(e)
        say_ts(client, message, f"エラーが発生しました。やり方を変えて再度試してみてください。 Error: {e}")


@app.message(re.compile(r"^!il-f$"))
def message_finish(client, message, say, context, logger):
    try:
        if message["user"] in using_user_set:
            using_user_set.remove(message["user"])  # ユーザーを解放
            # 会話のヒストリーを削除
            history_idetifier = get_history_identifier(
                message["team"], message["channel"], message["user"])
            del history_dict[history_idetifier]

            logger.info(f"<@{message['user']}> さんの対話学習を終了しました。")
            say_ts(client, message, f"<@{message['user']}> さんの対話学習を終了しました。")
        else:
            say_ts(client, message, f"<@{message['user']}> さんは対話学習を開始していません")
    except Exception as e:
        logger.error(e)
        say_ts(client, message, f"エラーが発生しました。やり方を変えて再度試してみてください。 Error: {e}")

@app.message(re.compile(r"^!il-help$"))
def message_help(client, message, say, context, logger):
    say_ts(client, message, f"`!il-s` 対話学習をスタートします。\n" +
           "`!il [回答内容]` 問われた質問に対して回答します。\n" +
           "`!il-f` 対話学習を終了します。\n")

@app.event("message")
def handle_message_events(body, logger):
    logger.debug(body)


# アプリを起動
if __name__ == "__main__":
    SocketModeHandler(app, os.getenv("SLACK_APP_TOKEN")).start()
