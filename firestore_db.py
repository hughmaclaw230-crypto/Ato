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
                cred_dict = json.loads(cred_json)
                cred = credentials.Certificate(cred_dict)
                firebase_admin.initialize_app(cred)
                log.info("🔥 Firebase 已透過 JSON 憑證初始化")
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
