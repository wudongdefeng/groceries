import asyncio
import base64
import json
import os
import re
import tempfile

import requests

from telethon import TelegramClient
from telethon.crypto import AuthKey
from telethon.sessions import MemorySession

# Telegram credentials must come from environment / TG_SESSION_ENV.
# Example:
# export TG_SESSION_ENV='{"TELEGRAM_DC_ID":"5","TELEGRAM_SERVER_ADDRESS":"91.108.56.104","TELEGRAM_PORT":"443","TELEGRAM_AUTH_KEY_HEX":"...","API_ID":4072867,"API_HASH":"..."}'

TARGET_CHAT_IDS = [
    1193345985,
    4376139249,
    1212178749,
    1335416484,
    1747293207,
    1440109119,
    1620853847,
    874231729,
    # 这里继续加
]

ALLOWED_EXTS = {".json", ".zip", ".py", ".js", ".apk"}
MAX_WEWORK_FILE_SIZE = 20 * 1024 * 1024  # 20MB


def get_telegram_credentials() -> tuple[int, str]:
    tg_session_env = os.getenv("TG_SESSION_ENV")
    if tg_session_env:
        try:
            data = json.loads(tg_session_env)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid TG_SESSION_ENV JSON: {exc}") from exc

        api_id = data.get("API_ID")
        api_hash = data.get("API_HASH")
        if api_id is not None and api_hash:
            return int(api_id), str(api_hash)

    env_api_id = os.getenv("API_ID")
    env_api_hash = os.getenv("API_HASH")
    if env_api_id and env_api_hash:
        return int(env_api_id), env_api_hash

    raise ValueError(
        "No Telegram API credentials found. Please set TG_SESSION_ENV with API_ID/API_HASH or export API_ID and API_HASH."
    )


def get_qywx_key() -> str:
    qywx_key = os.getenv("QYWX_KEY")
    if not qywx_key:
        raise ValueError("QYWX_KEY is not set")
    return qywx_key


def get_webhook_send_url() -> str:
    return f"https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key={get_qywx_key()}"


def get_webhook_upload_url() -> str:
    return f"https://qyapi.weixin.qq.com/cgi-bin/webhook/upload_media?key={get_qywx_key()}&type=file"


def restore_session_from_env(env_json: str) -> MemorySession:
    if not env_json:
        raise ValueError("TG_SESSION_ENV is empty")

    try:
        data = json.loads(env_json)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid TG_SESSION_ENV JSON: {exc}") from exc

    required_keys = [
        "TELEGRAM_DC_ID",
        "TELEGRAM_SERVER_ADDRESS",
        "TELEGRAM_PORT",
    ]
    missing = [k for k in required_keys if not data.get(k)]
    auth_key_hex = data.get("TELEGRAM_AUTH_KEY_HEX")
    auth_key_b64 = data.get("TELEGRAM_AUTH_KEY_B64")
    if missing or (not auth_key_hex and not auth_key_b64):
        raise ValueError(
            "TG_SESSION_ENV missing required Telegram session fields: "
            "TELEGRAM_DC_ID, TELEGRAM_SERVER_ADDRESS, TELEGRAM_PORT, TELEGRAM_AUTH_KEY_HEX or TELEGRAM_AUTH_KEY_B64"
        )

    dc_id = int(data["TELEGRAM_DC_ID"])
    server_address = data["TELEGRAM_SERVER_ADDRESS"]
    port = int(data["TELEGRAM_PORT"])

    if auth_key_b64:
        auth_key_bytes = base64.b64decode(auth_key_b64)
    else:
        auth_key_bytes = bytes.fromhex(auth_key_hex)

    session = MemorySession()
    session.set_dc(dc_id, server_address, port)
    session.auth_key = AuthKey(auth_key_bytes)

    if not hasattr(session.auth_key, "key"):
        raise RuntimeError("Failed to restore AuthKey from TG_SESSION_ENV")

    return session


def get_session_from_env_only() -> MemorySession:
    tg_session_env = os.getenv("TG_SESSION_ENV")
    if not tg_session_env:
        raise FileNotFoundError(
            "No Telegram session found. Please set TG_SESSION_ENV with the Telegram session data."
        )

    print("使用 TG_SESSION_ENV 读取 Telegram session")
    return restore_session_from_env(tg_session_env)


def get_filename_from_message(msg):
    file_obj = getattr(msg, "file", None)
    if file_obj:
        name = getattr(file_obj, "name", None)
        if name:
            return name

    doc = getattr(msg, "document", None)
    if doc:
        attrs = getattr(doc, "attributes", [])
        for attr in attrs:
            if hasattr(attr, "file_name"):
                return attr.file_name
        return None

    return None


def ext_from_name(name: str):
    if not name:
        return ""
    return os.path.splitext(name)[1].lower()


def is_allowed_file(msg):
    # 排除视频、图片、语音、贴纸
    if getattr(msg, "video", None):
        return False
    if getattr(msg, "photo", None):
        return False
    if getattr(msg, "voice", None):
        return False
    if getattr(msg, "sticker", None):
        return False

    name = get_filename_from_message(msg)
    if not name:
        return False
    return ext_from_name(name) in ALLOWED_EXTS


def safe_download_path(msg, idx: int):
    name = get_filename_from_message(msg)
    if not name:
        return None

    ext = ext_from_name(name)
    base = os.path.splitext(name)[0]
    safe_name = f"{base}_{idx}{ext}" if idx else name
    safe_name = safe_name.replace("/", "_").replace("\\", "_")

    path = os.path.join(tempfile.gettempdir(), safe_name)
    counter = 1
    while os.path.exists(path):
        path = os.path.join(tempfile.gettempdir(), f"{base}_{idx}_{counter}{ext}")
        counter += 1
    return path


async def resolve_chat_entity(client, chat_id):
    # 1) 优先直接解析
    try:
        entity = await client.get_entity(chat_id)
        if entity:
            return entity
    except Exception:
        pass

    # 2) 兜底：从当前 dialogs 找匹配 id
    async for dialog in client.iter_dialogs():
        entity = dialog.entity
        if getattr(entity, "id", None) == chat_id:
            return entity

    return None


async def download_one_file(client, msg, idx: int):
    if not is_allowed_file(msg):
        return None

    target = safe_download_path(msg, idx)
    if not target:
        return None

    try:
        result = await client.download_media(msg, file=target)
        if not result:
            return None
        if os.path.exists(result):
            return result
        if os.path.exists(target):
            return target
        return None
    except Exception as e:
        print("下载失败:", type(e).__name__, e)
        return None


def upload_file_to_wecom(file_path: str):
    size = os.path.getsize(file_path)
    print("文件大小:", size, "bytes")

    if size > MAX_WEWORK_FILE_SIZE:
        print(f"文件过大，跳过上传: {file_path}, size={size}")
        return None

    with open(file_path, "rb") as f:
        files = {"media": (os.path.basename(file_path), f, "application/octet-stream")}
        url = get_webhook_upload_url()
        r = requests.post(url, files=files, timeout=60)
        print("企业微信上传返回:", r.status_code, r.text)

        if r.status_code != 200:
            print("上传失败，跳过这个文件")
            return None

        try:
            data = r.json()
        except ValueError:
            print("响应不是 JSON，跳过上传")
            return None

        if "media_id" not in data:
            print(f"upload failed: {data}")
            return None

        return data["media_id"]


def send_file_to_wecom(media_id: str):
    payload = {"msgtype": "file", "file": {"media_id": media_id}}
    r = requests.post(get_webhook_send_url(), json=payload, timeout=30)
    print("企业微信发送返回:", r.status_code, r.text)
    r.raise_for_status()


def is_link_text(msg):
    text = (getattr(msg, "message", "") or "").strip()
    if not text:
        return False
    return bool(re.search(r'https?://[^\s]+', text))


def send_link_message(msg):
    text = (msg.message or "").strip()
    if not text:
        return

    matches = re.findall(r'https?://[^\s]+', text)
    if not matches:
        return

    content = (
        "## 文本链接转发\n"
        f"- 时间: {msg.date.strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"- 内容: {text}\n"
    )

    payload = {"msgtype": "markdown", "markdown": {"content": content}}
    r = requests.post(get_webhook_send_url(), json=payload, timeout=30)
    print("企业微信文本转发返回:", r.status_code, r.text)
    r.raise_for_status()


async def collect_recent_messages(client, chat_entity, limit=50):
    items = []
    async for msg in client.iter_messages(chat_entity, limit=limit):
        if is_link_text(msg):
            items.append(("link", msg))
        elif is_allowed_file(msg):
            items.append(("file", msg))
    return items


async def main():
    session = get_session_from_env_only()
    api_id, api_hash = get_telegram_credentials()
    client = TelegramClient(session, api_id, api_hash)

    try:
        await client.start()

        total_sent = 0

        for chat_id in TARGET_CHAT_IDS:
            entity = await resolve_chat_entity(client, chat_id)
            if entity is None:
                print(f"无法解析 chat_id={chat_id}，跳过")
                continue

            print(f"处理 chat_id={chat_id}, title={getattr(entity, 'title', 'UNKNOWN')}, type={type(entity).__name__}")

            items = await collect_recent_messages(client, entity, limit=200)
            print(f"chat_id={chat_id} 共找到 {len(items)} 条符合条件的消息")

            for kind, msg in items[:5]:
                if kind == "link":
                    send_link_message(msg)
                    total_sent += 1
                    continue

                if kind == "file":
                    local_path = await download_one_file(client, msg, total_sent + 1)
                    if not local_path:
                        print(f"跳过不可下载的对象，msg_id={msg.id}")
                        continue

                    media_id = upload_file_to_wecom(local_path)
                    if media_id is None:
                        print("跳过超大文件上传:", local_path)
                        try:
                            os.remove(local_path)
                        except Exception:
                            pass
                        continue

                    send_file_to_wecom(media_id)

                    file_name = get_filename_from_message(msg) or "unknown"
                    summary = (
                        "## 已推送文件\n"
                        f"- 文件名: {file_name}\n"
                        f"- 时间: {msg.date.strftime('%Y-%m-%d %H:%M:%S')}\n"
                        f"- 消息ID: {msg.id}"
                    )
                    requests.post(
                        get_webhook_send_url(),
                        json={"msgtype": "markdown", "markdown": {"content": summary}},
                        timeout=30,
                    )

                    try:
                        os.remove(local_path)
                    except Exception:
                        pass

                    total_sent += 1

    finally:
        await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
