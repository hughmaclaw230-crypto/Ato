#!/usr/bin/env python3
"""
Firestore 用戶資料庫模組
取代原本的 JSON 檔案儲存，使用 Google Firestore 持久化用戶資料
"""

import os
import json
import logging
import firebase_admin
from firebase_admin import credentials, firestore

log = logging.getLogger(__name__)

# ─── Firestore 初始化 ─────────────────────────────────

_db = None

USERS_COLLECTION = "users"


def _init_firestore():
    """初始化 Firebase / Firestore 連線"""
    global _db

    if _db is not None:
        return _db

    # 方式 1: 環境變數 FIREBASE_CREDENTIALS_JSON（JSON 字串）
    cred_json = os.environ.get("FIREBASE_CREDENTIALS_JSON", "")
    # 方式 2: 環境變數 GOOGLE_APPLICATION_CREDENTIALS（檔案路徑）
    cred_file = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "")

    try:
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
            # 嘗試 Application Default Credentials
            firebase_admin.initialize_app()
            log.info("🔥 Firebase 已透過預設憑證初始化")

        _db = firestore.client()
        log.info("✅ Firestore 連線成功")
        return _db

    except Exception as e:
        log.error(f"❌ Firestore 初始化失敗: {e}")
        raise


def get_db():
    """取得 Firestore client"""
    if _db is None:
        return _init_firestore()
    return _db


# ═══════════════════════════════════════════════════════════
#  用戶 CRUD 操作
# ═══════════════════════════════════════════════════════════

def get_user(user_id: str) -> dict | None:
    """從 Firestore 取得用戶資料"""
    try:
        doc = get_db().collection(USERS_COLLECTION).document(user_id).get()
        if doc.exists:
            return doc.to_dict()
        return None
    except Exception as e:
        log.error(f"Firestore get_user 失敗 ({user_id}): {e}")
        return None


def save_user(user_id: str, data: dict):
    """寫入/更新用戶資料到 Firestore"""
    try:
        get_db().collection(USERS_COLLECTION).document(user_id).set(data)
        log.debug(f"Firestore save_user: {user_id}")
    except Exception as e:
        log.error(f"Firestore save_user 失敗 ({user_id}): {e}")


def get_pending_users() -> list[tuple[str, dict]]:
    """取得所有待審核的用戶"""
    try:
        docs = (
            get_db()
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
    try:
        docs = get_db().collection(USERS_COLLECTION).stream()
        return [(doc.id, doc.to_dict()) for doc in docs]
    except Exception as e:
        log.error(f"Firestore get_all_users 失敗: {e}")
        return []
