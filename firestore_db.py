#!/usr/bin/env python3
"""
Firestore 用戶資料庫模組
取代原本的 JSON 檔案儲存，使用 Google Firestore 持久化用戶資料
包含完整的錯誤處理與降級保護
"""

import os
import json
import logging
import firebase_admin
from firebase_admin import credentials, firestore

log = logging.getLogger(__name__)

# ─── Firestore 初始化 ─────────────────────────────────

_db = None
_init_failed = False  # 初始化曾失敗 → 不再重試，避免反覆報錯

USERS_COLLECTION = "users"


def _sanitize_private_key(cred_dict: dict) -> dict:
    """
    清理 private_key 欄位：
    - 將 literal \\n 轉為真正的換行
    - 移除 -----END PRIVATE KEY-----\n 之後的多餘字元
    - 確保 PEM 格式正確
    """
    pk = cred_dict.get("private_key", "")
    if not pk:
        return cred_dict

    # 替換 literal \\n 為真正的換行符
    if "\\n" in pk and "\n" not in pk:
        pk = pk.replace("\\n", "\n")

    # 移除 END 標記後的多餘內容
    end_marker = "-----END PRIVATE KEY-----"
    end_idx = pk.find(end_marker)
    if end_idx >= 0:
        pk = pk[:end_idx + len(end_marker)] + "\n"

    # 確保開頭有 BEGIN 標記
    begin_marker = "-----BEGIN PRIVATE KEY-----"
    begin_idx = pk.find(begin_marker)
    if begin_idx > 0:
        pk = pk[begin_idx:]

    # 移除多餘空行但保留必要的換行
    lines = pk.split("\n")
    cleaned_lines = []
    for line in lines:
        stripped = line.strip()
        if stripped or line == "":  # 保留空行在 BEGIN/END 之後
            cleaned_lines.append(stripped if stripped else "")
    pk = "\n".join(cleaned_lines)

    # 確保結尾有換行
    if not pk.endswith("\n"):
        pk += "\n"

    cred_dict["private_key"] = pk
    log.debug(f"private_key 長度: {len(pk)}, 行數: {pk.count(chr(10))}")
    return cred_dict


def _parse_firebase_json(raw: str) -> dict:
    """
    解析 Firebase 憑證 JSON，處理常見的 Render 環境變數問題：
    1. 正常 JSON
    2. 被額外引號包裹的 JSON (Render UI 有時會加)
    3. 換行符被 literal \\n 取代
    4. 雙重轉義的 JSON
    5. Base64 編碼的 JSON
    """
    raw = raw.strip()
    if not raw:
        raise ValueError("FIREBASE_CREDENTIALS_JSON 為空")

    # 嘗試 1: 直接 parse
    try:
        d = json.loads(raw)
        if isinstance(d, dict) and "project_id" in d:
            log.info("🔥 憑證解析成功（直接 JSON）")
            return _sanitize_private_key(d)
    except json.JSONDecodeError:
        pass

    # 嘗試 2: 去掉外層引號 (Render 有時會多包一層)
    if (raw.startswith("'") and raw.endswith("'")) or \
       (raw.startswith('"') and raw.endswith('"')):
        try:
            inner = raw[1:-1]
            d = json.loads(inner)
            if isinstance(d, dict) and "project_id" in d:
                log.info("🔥 憑證解析成功（去除外層引號）")
                return _sanitize_private_key(d)
        except json.JSONDecodeError:
            pass

    # 嘗試 3: 替換 literal \\n 為真正的換行（private_key 常見問題）
    try:
        fixed = raw.replace("\\\\n", "\\n")
        d = json.loads(fixed)
        if isinstance(d, dict) and "project_id" in d:
            log.info("🔥 憑證解析成功（修復 escaped newlines）")
            return _sanitize_private_key(d)
    except json.JSONDecodeError:
        pass

    # 嘗試 4: 雙重 JSON 轉義（整個 JSON 被 json.dumps 過一次）
    try:
        unescaped = json.loads(raw)  # 第一層
        if isinstance(unescaped, str):
            d = json.loads(unescaped)  # 第二層
            if isinstance(d, dict) and "project_id" in d:
                log.info("🔥 憑證解析成功（雙重 JSON 轉義）")
                return _sanitize_private_key(d)
    except (json.JSONDecodeError, TypeError):
        pass

    # 嘗試 5: Base64 編碼
    try:
        import base64
        decoded = base64.b64decode(raw).decode("utf-8")
        d = json.loads(decoded)
        if isinstance(d, dict) and "project_id" in d:
            log.info("🔥 憑證解析成功（Base64）")
            return _sanitize_private_key(d)
    except Exception:
        pass

    # 全部失敗 — 輸出診斷訊息
    preview = raw[:80] + "..." if len(raw) > 80 else raw
    log.error(f"❌ 無法解析 FIREBASE_CREDENTIALS_JSON（長度={len(raw)}）")
    log.error(f"   前 80 字元: {preview}")
    log.error(f"   開頭字元: {repr(raw[:3])}  結尾字元: {repr(raw[-3:])}")
    raise ValueError(f"FIREBASE_CREDENTIALS_JSON 格式無效（長度={len(raw)}，開頭={repr(raw[:10])}）")


def _init_firestore():
    """初始化 Firebase / Firestore 連線（含重複初始化保護）"""
    global _db, _init_failed

    if _db is not None:
        return _db

    if _init_failed:
        return None  # 之前已經失敗過，不再重試

    # 方式 1: 環境變數 FIREBASE_CREDENTIALS_JSON（JSON 字串）
    cred_json = os.environ.get("FIREBASE_CREDENTIALS_JSON", "")
    # 方式 2: 環境變數 GOOGLE_APPLICATION_CREDENTIALS（檔案路徑）
    cred_file = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "")

    try:
        # 防止 firebase_admin 重複初始化
        try:
            existing_app = firebase_admin.get_app()
            log.info("🔥 Firebase app 已存在，直接使用")
        except ValueError:
            # 尚未初始化，正常建立
            if cred_json:
                cred_dict = _parse_firebase_json(cred_json)
                cred = credentials.Certificate(cred_dict)
                firebase_admin.initialize_app(cred)
                log.info(f"🔥 Firebase 已透過 JSON 憑證初始化 (project: {cred_dict.get('project_id', '?')})")
            elif cred_file:
                cred = credentials.Certificate(cred_file)
                firebase_admin.initialize_app(cred)
                log.info(f"🔥 Firebase 已透過憑證檔案初始化: {cred_file}")
            else:
                firebase_admin.initialize_app()
                log.info("🔥 Firebase 已透過預設憑證初始化")

        _db = firestore.client()
        log.info("✅ Firestore 連線成功")
        return _db

    except Exception as e:
        _init_failed = True  # 標記失敗，後續不再重試
        _db = None
        log.error(f"❌ Firestore 初始化失敗: {e}")
        raise


def get_db():
    """取得 Firestore client（若初始化曾失敗則回傳 None）"""
    if _db is None:
        if _init_failed:
            return None
        try:
            return _init_firestore()
        except Exception:
            return None
    return _db


def is_available() -> bool:
    """檢查 Firestore 是否可用"""
    return _db is not None and not _init_failed


# ═══════════════════════════════════════════════════════════
#  用戶 CRUD 操作（全部加入 Firestore 可用性檢查）
# ═══════════════════════════════════════════════════════════

def get_user(user_id: str) -> dict | None:
    """從 Firestore 取得用戶資料"""
    db = get_db()
    if db is None:
        log.warning(f"Firestore 不可用，無法查詢用戶 {user_id}")
        return None
    try:
        doc = db.collection(USERS_COLLECTION).document(user_id).get()
        if doc.exists:
            return doc.to_dict()
        return None
    except Exception as e:
        log.error(f"Firestore get_user 失敗 ({user_id}): {e}")
        return None


def save_user(user_id: str, data: dict) -> bool:
    """寫入/更新用戶資料到 Firestore，回傳是否成功"""
    db = get_db()
    if db is None:
        log.warning(f"Firestore 不可用，無法儲存用戶 {user_id}")
        return False
    try:
        db.collection(USERS_COLLECTION).document(user_id).set(data)
        log.debug(f"Firestore save_user: {user_id}")
        return True
    except Exception as e:
        log.error(f"Firestore save_user 失敗 ({user_id}): {e}")
        return False


def get_pending_users() -> list[tuple[str, dict]]:
    """取得所有待審核的用戶"""
    db = get_db()
    if db is None:
        return []
    try:
        docs = (
            db
            .collection(USERS_COLLECTION)
            .where("status", "==", "pending")
            .stream()
        )
        return [(doc.id, doc.to_dict()) for doc in docs]
    except Exception as e:
        log.error(f"Firestore get_pending_users 失敗: {e}")
        return []


def get_all_users() -> list[tuple[str, dict]]:
    """取得所有用戶"""
    db = get_db()
    if db is None:
        return []
    try:
        docs = db.collection(USERS_COLLECTION).stream()
        return [(doc.id, doc.to_dict()) for doc in docs]
    except Exception as e:
        log.error(f"Firestore get_all_users 失敗: {e}")
        return []
