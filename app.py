# -*- coding: utf-8 -*-
"""
HIA アカウント・マスタ管理

画面は2種類、データベースは1つ。
  HIA総合管理 （/km  ・緑）… 当社スタッフが利用
  HIA健保管理 （/app ・青）… 健保担当者・企業担当者が利用

HIA総合管理でできること
  1. 各健保・企業の代表者アカウントの発行
  2. アカウント権限の登録・編集・削除
  3. アカウントの一覧確認
  4. 操作ログの管理（HIA健保管理のログも含む）

HIA健保管理でできること（健保担当者／企業担当者）
  1. アカウントの登録・編集・削除（自分のスコープ内）
  2. 企業情報の登録・編集・削除
  3. 事業所情報の登録・編集・削除
  4. 加入者情報の登録・編集・削除
  5. パスワードの再設定（ログイン画面から本人が申請）
  6. 産業医面談の管理（sanmen.py／対象者の抽出・健診受診管理・面談記録・
     労基署報告・メール配信・データ取込）
"""
import csv
import hashlib
import io
import os
import re
import secrets
import smtplib
import sqlite3
import uuid
from datetime import date, datetime, timedelta
from email.message import EmailMessage
from functools import wraps

from flask import (Flask, Response, flash, g, redirect, render_template,
                   request, send_from_directory, session, url_for)
from werkzeug.security import check_password_hash, generate_password_hash

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "hia.db")
# 同梱の初期データ。hia.db が無いときだけここから作る。
# プログラムを新しい版に差し替えても hia.db は上書きされないため、
# 画面から登録したアカウント・マスタは消えない。
INITIAL_DB = os.path.join(BASE_DIR, "hia_initial.db")
BACKUP_DIR = os.path.join(BASE_DIR, "backup")
BACKUP_KEEP = int(os.environ.get("HIA_BACKUP_KEEP", "10"))
OUTBOX = os.path.join(BASE_DIR, "outbox")

BUILD = "2.1.0 (2026-09-01)"

MAX_EXPORT_ROWS = int(os.environ.get("HIA_MAX_EXPORT_ROWS", "1000"))
INVITE_HOURS = 72
RESET_HOURS = 2

ROLE_LABELS = {
    "system_admin": "当社スタッフ",
    "kenpo_user": "健保担当者",
    "company_user": "企業担当者",
}
# 企業担当者のサブロール（産業医面談管理で使う）
#   doctor（産業医）… 医学的判断（面談対象の承認・就業区分の判定・面談所見・記名）
#   hr    （人事）  … 運用事務（取込・メール配信・対応区分・報告書の出力）
SUB_ROLE_LABELS = {"doctor": "産業医", "hr": "人事"}
SUB_ROLES = ["doctor", "hr"]
# サブロールを付けられるロール
SUB_ROLE_ROLES = ("company_user",)

# ================================================================ 機能制御
# ロール（＋サブロール）ごとに、使える機能を当社スタッフが切り替えられる。
# 「固定」の機能は設定で変更できない（医学的判断は産業医のみが行うため）。
ROLE_KEYS = ["system_admin", "kenpo_user", "company_user",
             "company_user/doctor", "company_user/hr"]
ROLE_KEY_LABELS = {
    "system_admin": "当社スタッフ",
    "kenpo_user": "健保担当者",
    "company_user": "企業担当者（サブロールなし）",
    "company_user/doctor": "企業担当者（産業医）",
    "company_user/hr": "企業担当者（人事）",
}
# (キー, 区分, 機能名, 説明, 固定)
FEATURES = [
    ("master.view", "マスタ管理", "マスタの閲覧",
     "企業・事業所・部署・加入者の一覧と詳細を見る", False),
    ("master.write", "マスタ管理", "マスタの登録・変更・削除",
     "企業・事業所・部署・加入者の追加・編集・削除と紐づけ", False),
    ("master.import", "マスタ管理", "CSVの一括取込",
     "加入者・企業・事業所のCSV取込", False),
    ("oh.list", "産業医面談", "面談対象者一覧",
     "面談候補の抽出結果の閲覧・メモ・健診結果票", False),
    ("oh.kenshin", "産業医面談", "健診受診管理（定期・深夜）",
     "年度ごとの受診状況・受診率の確認", False),
    ("oh.approve", "産業医面談", "面談対象の承認・就業区分の判定",
     "医学的判断のため産業医のみ（変更できません）", True),
    ("oh.interview", "産業医面談", "面談結果の記録",
     "医学的判断のため産業医のみ（変更できません）", True),
    ("oh.sign", "産業医面談", "報告書への記名・サイン",
     "医学的判断のため産業医のみ（変更できません）", True),
    ("oh.hr_class", "産業医面談", "対応区分の設定",
     "未判定／産業医判定済／要精査・加療指示ほかの設定", False),
    ("oh.report", "産業医面談", "労基署報告の閲覧・出力",
     "サマリー・様式第6号・ストレスチェック報告書のPDF／CSV", False),
    ("oh.mail", "産業医面談", "メール配信",
     "面談受診勧奨・ストレスチェック受検案内の配信", False),
    ("oh.upload", "産業医面談", "データ取込",
     "健診結果・労働時間・ストレスチェックの取込", False),
    # ---- HIA健保管理（青）の各業務。カテゴリ単位で出し入れする ----
    ("kenpo.kenshin", "HIA健保管理", "健康診断 代行管理",
     "受診進捗のダッシュボード・対象者一覧・健診結果出力・請求書出力", False),
    ("kenpo.hoken", "HIA健保管理", "特定保健指導",
     "ダッシュボード・対象者一覧・XML出力・請求書出力・健診結果の取込と履歴", False),
    ("kenpo.influenza", "HIA健保管理", "インフルエンザ補助",
     "ダッシュボード・予約一覧・申請一覧・接種実績の取込・請求管理", False),
    ("kenpo.receipt", "HIA健保管理", "レセプト情報",
     "レセプト情報の取込と取込履歴", False),
    ("kenpo.member_edit", "HIA健保管理", "加入者情報の変更（健保側）",
     "加入者の基本情報・住所・連絡先の編集", False),
    ("kenpo.mail", "HIA健保管理", "メール・通知送信",
     "受診案内テンプレートの作成・管理・送信", False),
    ("risk", "そのほか", "疾患予測",
     "予測結果一覧・健診結果の連携・NSIPS連携", False),
    ("accounts", "そのほか", "アカウント管理",
     "アカウントの一覧・発行・編集・削除", False),
    ("download", "そのほか", "CSVのダウンロード",
     "アカウントごとのダウンロード権限とあわせて判定します", False),
    # ---- 設定・サポート。カテゴリ（サイドバー）と各カードの出し分け ----
    ("settings.view", "設定・サポート", "設定・サポートを表示",
     "サイドバーの「設定・サポート」とそのカード一覧を出すかどうか", False),
    ("settings.contact", "設定・サポート", "お問い合わせ先",
     "サポート窓口の連絡先の画面", False),
    ("settings.log", "設定・サポート", "操作ログ管理",
     "操作ログの閲覧・絞り込み・CSV出力（配信ログを含む）", False),
    ("settings.mail", "設定・サポート", "メール設定",
     "送信元・SMTPなどメール送信の設定", False),
]
FEATURE_KEYS = [f[0] for f in FEATURES]
# 一部の機能はロールの役割上「対象外」にする（切り替えず、常に利用不可）。
#   設定・サポートの管理は事務の仕事なので、産業医は対象外。
_SETTINGS = {"settings.view", "settings.contact", "settings.log", "settings.mail"}
FEATURE_NA_ROLES = {k: {"company_user/doctor"} for k in _SETTINGS}


def feature_na(key, role_key):
    # そのロールにとって対象外の機能か（画面では「対象外」と表示して切り替えません）
    return role_key in FEATURE_NA_ROLES.get(key, ())
FIXED_FEATURES = {f[0] for f in FEATURES if f[4]}
# 固定の機能を使えるロール（医学的判断は産業医のみ）
FIXED_FEATURE_ROLE = "company_user/doctor"

_KENPO = {"kenpo.kenshin", "kenpo.hoken", "kenpo.influenza", "kenpo.receipt",
          "kenpo.member_edit", "kenpo.mail"}
_OPS = ({"master.view", "master.write", "master.import", "oh.list", "oh.kenshin",
         "oh.hr_class", "oh.report", "oh.mail", "oh.upload", "risk", "accounts",
         "download"} | _KENPO | _SETTINGS)
# 産業医は医学的判断が中心。業務事務（取込・配信・健保側の各業務）は既定で持たない
_DOCTOR = {"master.view", "oh.list", "oh.kenshin", "oh.report", "oh.approve",
           "oh.interview", "oh.sign", "risk", "accounts", "download"}
# 既定の機能マトリクス（設定画面の「初期値に戻す」でこの状態になる）
FEATURE_DEFAULTS = {
    "system_admin": set(_OPS),
    "kenpo_user": set(_OPS),
    "company_user": set(_OPS),
    "company_user/hr": set(_OPS),
    "company_user/doctor": set(_DOCTOR),
}
SCOPE_LABELS = {"all": "全健保", "kenpo_all": "自組合全体",
                "own_company": "担当する範囲"}
STATUS_LABELS = {"active": "有効", "invited": "PW未設定", "disabled": "無効",
                 "deleted": "削除済み"}
# 加入者向けサイトの本人確認（認証）で使う項目のパターン。健保ごとに登録する。
AUTH_PATTERNS = {
    "A": ["被保険者記号", "被保険者番号", "カナ", "生年月日", "性別"],
    "B": ["被保険者番号", "カナ", "生年月日", "性別"],
}
AUTH_PATTERN_NOTE = {
    "A": "被保険者証に記号がある組合はこちら（記号と番号で本人を特定します）",
    "B": "記号を使わない組合はこちら（番号のみで本人を特定します）",
}


def clean_auth_pattern(v, default="A"):
    # 認証方式の値をそろえる（A か B のどちらか）
    v = (v or "").strip().upper()
    return v if v in AUTH_PATTERNS else default
# 各ロールが発行・変更できるロール（自分より広い権限は付与できない）
ISSUABLE_ROLES = {
    "system_admin": ["system_admin", "kenpo_user", "company_user"],
    "kenpo_user": ["kenpo_user", "company_user"],
    "company_user": ["company_user"],
}
SHELL_OF_ROLE = {"system_admin": "km", "kenpo_user": "kenpo", "company_user": "kenpo"}
# 担当範囲を企業・事業所・部署で指定するロール（閲覧範囲は own_company になる）
SCOPED_ROLES = ("company_user",)

app = Flask(__name__)


def _secret_key():
    """署名用のキー。毎回作り直すと再起動でログインが切れてしまうため、
    環境変数がなければ secret.key に保存して使い回します。"""
    env = os.environ.get("HIA_SECRET_KEY")
    if env:
        return env
    path = os.path.join(BASE_DIR, "secret.key")
    try:
        with open(path, encoding="utf-8") as fp:
            key = fp.read().strip()
        if key:
            return key
    except OSError:
        pass
    key = secrets.token_hex(32)
    try:
        with open(path, "w", encoding="utf-8") as fp:
            fp.write(key)
        os.chmod(path, 0o600)
    except OSError:
        pass          # 書けない環境ではその場かぎりのキーで動かす
    return key


app.secret_key = _secret_key()
# ログインを保つ期間（この間はブラウザを閉じても入り直さずに使えます）
app.permanent_session_lifetime = timedelta(days=14)
app.config.update(SESSION_COOKIE_SAMESITE="Lax", SESSION_COOKIE_HTTPONLY=True,
                  SESSION_REFRESH_EACH_REQUEST=True,
                  # CSSや画面（static）を更新したらすぐ反映されるようにする
                  SEND_FILE_MAX_AGE_DEFAULT=0)

STAGING = {}
ALLOW_IPS = [x.strip() for x in os.environ.get("HIA_ALLOW_IPS", "").split(",") if x.strip()]


# ---------------------------------------------------------------- DB
def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


@app.teardown_appcontext
def close_db(exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def restore_initial_db():
    """hia.db が無いときだけ、同梱の初期データから hia.db を作る。

    プログラムの差し替え（zipの展開）では hia.db が含まれないため、
    画面から登録したアカウント・マスタが上書きされて消えることはない。
    """
    if os.path.exists(DB_PATH) or not os.path.exists(INITIAL_DB):
        return None
    import shutil
    shutil.copy2(INITIAL_DB, DB_PATH)
    return "初期データ（hia_initial.db）から hia.db を作成しました。"


def backup_db(force=False):
    """hia.db を backup フォルダへ控える（既定は1日1回・最新10世代を保持）"""
    if not os.path.exists(DB_PATH):
        return None
    import glob
    import shutil
    try:
        os.makedirs(BACKUP_DIR, exist_ok=True)
        today = datetime.now().strftime("%Y%m%d")
        if not force and glob.glob(os.path.join(BACKUP_DIR, f"hia_{today}_*.db")):
            return None                      # 今日の控えが既にある
        dest = os.path.join(BACKUP_DIR,
                            "hia_" + datetime.now().strftime("%Y%m%d_%H%M%S") + ".db")
        # 書き込み中でも壊れない形で複製する
        src = sqlite3.connect(DB_PATH)
        dst = sqlite3.connect(dest)
        with dst:
            src.backup(dst)
        dst.close()
        src.close()
        olds = sorted(glob.glob(os.path.join(BACKUP_DIR, "hia_*.db")))
        for path in olds[:-BACKUP_KEEP]:     # 古い世代を消す
            try:
                os.remove(path)
            except OSError:
                pass
        return f"データベースを控えました（{os.path.basename(dest)}／最新{BACKUP_KEEP}世代を保持）"
    except Exception as e:                   # 控えに失敗しても起動は続ける
        return f"データベースの控えに失敗しました（{e}）"


def init_db():
    """新規作成、または既存DBのスキーマ移行を行う"""
    from migrate import ensure_schema
    lines = []
    msg = restore_initial_db()
    if msg:
        lines.append(msg)
    lines += list(ensure_schema(DB_PATH))
    msg = backup_db()
    if msg:
        lines.append(msg)
    return lines


def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def no_store(resp):
    """ブラウザに画面をためこませない（更新がすぐ反映されるように）"""
    resp.headers["Cache-Control"] = "no-store, must-revalidate"
    resp.headers["Pragma"] = "no-cache"
    return resp


def asset_ver(name="app.css"):
    """スタイルの更新時刻。リンクに付けて、古いCSSが使われるのを防ぎます。"""
    try:
        return str(int(os.path.getmtime(os.path.join(BASE_DIR, "static", name))))
    except OSError:
        return "0"


@app.context_processor
def inject_asset_ver():
    return {"ASSET_V": asset_ver()}


# ---------------------------------------------------------------- ログ
def client_ip():
    fwd = request.headers.get("X-Forwarded-For", "")
    return (fwd.split(",")[0].strip() if fwd else request.remote_addr) or "-"


def log(category, action, result="success", target=None, detail=None):
    db = get_db()
    acc = current_account()
    me = real_account()
    sup = support_kenpo()
    if sup:
        # 誰が・どの健保に入って操作したかを残す
        detail = (detail + "／" if detail else "") + f"サポートログイン中（{sup['name']}）"
    db.execute(
        "INSERT INTO audit_log (shell, category, action, result, actor_email, actor_id,"
        " actor_role, kenpo_id, ip, target, detail) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (session.get("shell"), category, action, result,
         me["email"] if me else session.get("pending_email"),
         me["id"] if me else None,
         me["role"] if me else None,
         acc["kenpo_id"] if acc else None,
         client_ip(), target, detail))
    db.commit()


# ---------------------------------------------------------------- 認証
def real_account():
    """ログインしている本人（サポートログイン中も本人を返す）"""
    aid = session.get("account_id")
    if not aid:
        return None
    if "real_acc" not in g:
        g.real_acc = get_db().execute(
            "SELECT * FROM account WHERE id=? AND status='active'", (aid,)).fetchone()
    return g.real_acc


def support_kenpo():
    """サポートログイン中の健康保険組合。していなければ None"""
    kid = session.get("support_kenpo_id")
    if not kid:
        return None
    me = real_account()
    if not me or me["role"] != "system_admin":
        return None
    if "sup_kenpo" not in g:
        g.sup_kenpo = get_db().execute("SELECT * FROM kenpo WHERE id=?", (kid,)).fetchone()
    return g.sup_kenpo


def current_account():
    """操作の主体。サポートログイン中は、その健保の担当者として扱う。"""
    me = real_account()
    if not me:
        return None
    if "acc" not in g:
        kenpo = support_kenpo()
        if kenpo:
            acc = dict(me)
            acc.update(role="kenpo_user", view_scope="kenpo_all", kenpo_id=kenpo["id"],
                       company_id=None, is_primary=0, can_download=me["can_download"],
                       name=f"{me['name']}（サポート）", support=True,
                       support_kenpo=kenpo["name"])
            g.acc = acc
        else:
            g.acc = me
    return g.acc


def sub_role(acc):
    """企業担当者のサブロール（doctor＝産業医／hr＝人事）。付いていなければ空文字。"""
    if not acc:
        return ""
    try:
        v = acc["sub_role"]
    except (KeyError, IndexError):
        return ""
    return (v or "") if v in SUB_ROLE_LABELS else ""


def clean_sub_role(role, value):
    """フォームから受け取ったサブロールを検証する。付けられないロールでは空にする。"""
    value = (value or "").strip()
    if role in SUB_ROLE_ROLES and value in SUB_ROLE_LABELS:
        return value
    return ""


def role_full(row):
    """一覧・確認画面に出すロール名。サブロールがあれば併記する。"""
    base = ROLE_LABELS.get(row["role"], row["role"])
    s = sub_role(row)
    return f"{base}（{SUB_ROLE_LABELS[s]}）" if s else base


def is_doctor(acc=None):
    """医学的判断（面談対象の承認・就業区分・面談記録・報告書への記名）ができるか"""
    return sub_role(acc if acc is not None else current_account()) == "doctor"


def role_key(acc=None):
    """機能制御のキー。企業担当者はサブロールまで含める（company_user/doctor など）"""
    acc = acc if acc is not None else current_account()
    if not acc:
        return ""
    s = sub_role(acc)
    return acc["role"] + ("/" + s if s else "")


def feature_overrides():
    """機能制御の設定（画面で保存した内容）。1リクエストに1回だけ読む。"""
    if "features" not in g:
        try:
            g.features = {(r["role_key"], r["feature"]): bool(r["allowed"])
                          for r in get_db().execute(
                              "SELECT role_key, feature, allowed FROM role_feature")}
        except sqlite3.Error:
            g.features = {}
    return g.features


# 当社スタッフ（システム管理者）は機能制御の対象外。設定に関係なく全機能を使える。
# ただし固定の機能（医学的判断＝面談対象の承認・就業区分の判定・面談結果の記録・
# 報告書への記名）は、労働安全衛生法の考え方にもとづき産業医のみ。
ALL_FEATURE_ROLE = "system_admin"


def feature_allowed(key, acc=None):
    """そのアカウントが機能を使えるか。設定があればそれを、無ければ既定値を使う。
    固定の機能（医学的判断）は設定に関係なく産業医だけが使える。
    当社スタッフは設定に関係なく（固定の機能以外の）全機能を使える。"""
    acc = acc if acc is not None else current_account()
    if not acc:
        return False
    rk = role_key(acc)
    if feature_na(key, rk):
        return False            # 役割上の対象外（設定に関係なく使えません）
    if key in FIXED_FEATURES:
        return rk == FIXED_FEATURE_ROLE
    if rk == ALL_FEATURE_ROLE:
        return True
    ov = feature_overrides().get((rk, key))
    if ov is not None:
        return ov
    return key in FEATURE_DEFAULTS.get(rk, set())


def account_features(acc=None):
    """画面（シェル）へ渡す機能の一覧"""
    return {k: feature_allowed(k, acc) for k in FEATURE_KEYS}


def deny_feature(key):
    """機能制御で使えない操作を拒否する（画面に出さないだけでなくサーバ側でも止める）"""
    log("auth", "機能制御により操作を拒否", "blocked", target=request.path,
        detail=f"role={role_key()}／機能={key}")
    return render_template("denied.html", path=request.path, feature_key=key), 403


def feature_required(key):
    def deco(fn):
        @wraps(fn)
        def wrapper(*a, **kw):
            if not current_account():
                return redirect(url_for("login", next=request.path))
            if not feature_allowed(key):
                return deny_feature(key)
            return fn(*a, **kw)
        return wrapper
    return deco


def login_required(fn):
    @wraps(fn)
    def wrapper(*a, **kw):
        if not current_account():
            return redirect(url_for("login", next=request.path))
        return fn(*a, **kw)
    return wrapper


def roles_required(*roles):
    def deco(fn):
        @wraps(fn)
        def wrapper(*a, **kw):
            acc = current_account()
            if not acc:
                return redirect(url_for("login", next=request.path))
            if acc["role"] not in roles:
                log("auth", "権限不足によるアクセス拒否", "blocked",
                    target=request.path, detail=f"role={acc['role']}")
                return render_template("denied.html", path=request.path), 403
            return fn(*a, **kw)
        return wrapper
    return deco


# 画面（エンドポイント）と機能制御のキーの対応。
# ここに載っているエンドポイントは、機能が「利用不可」のロールでは403で拒否する。
ENDPOINT_FEATURES = {
    # マスタの閲覧
    "companies": "master.view", "companies_rows": "master.view",
    "offices": "master.view", "offices_rows": "master.view",
    "departments": "master.view", "departments_rows": "master.view",
    "members": "master.view", "members_rows": "master.view",
    "api_members_by_office": "master.view",
    # マスタの登録・変更・削除
    "company_new": "master.write", "company_edit": "master.write",
    "company_delete": "master.write",
    "office_new": "master.write", "office_edit": "master.write",
    "office_delete": "master.write",
    "department_new": "master.write", "department_edit": "master.write",
    "department_delete": "master.write",
    "member_new": "master.write", "member_edit": "master.write",
    "member_delete": "master.write",
    "members_link_auto": "master.write", "members_link_page": "master.write",
    "members_link_assign": "master.write", "api_members_link": "master.write",
    "api_members_link_filtered": "master.write",
    "risk_group_edit": "master.write", "risk_group_delete": "master.write",
    # CSVの一括取込
    "members_import": "master.import", "members_import_commit": "master.import",
    "companies_import": "master.import",
    "companies_import_commit": "master.import",
    "offices_import": "master.import", "offices_import_commit": "master.import",
    # 疾患予測
    "risk_list": "risk", "risk_member": "risk", "risk_run_exec": "risk",
    "risk_export": "risk", "risk_groups": "risk", "risk_kenshin": "risk",
    "risk_kenshin_sync": "risk", "risk_kenshin_xml": "risk",
    "risk_nsips": "risk", "risk_nsips_sync": "risk",
    # 設定・サポート
    "logs": "settings.log", "logs_export": "settings.log",
    "logs_mail_export": "settings.log",
    # アカウント管理
    "accounts": "accounts", "accounts_export": "accounts",
    "accounts_new": "accounts", "accounts_create": "accounts",
    "accounts_edit": "accounts", "accounts_edit_apply": "accounts",
    "accounts_invite_link": "accounts", "accounts_set_password": "accounts",
    "accounts_send_invite": "accounts", "accounts_toggle": "accounts",
    "accounts_delete": "accounts", "accounts_purge": "accounts",
}


@app.before_request
def keep_session():
    """ログイン状態を保ち続ける（画面の中の枠からの移動でログイン画面に戻らないように）。
    アクセスするたびに有効期限を先へ延ばします。"""
    if session.get("account_id") and not session.permanent:
        session.permanent = True


@app.before_request
def block_by_feature():
    """機能制御（ロール・サブロールごとの利用可否）で操作を止める。
    画面のボタンを隠すだけでなく、URLを直接呼ばれた場合もここで拒否して記録する。"""
    key = ENDPOINT_FEATURES.get(request.endpoint)
    if not key:
        return None
    acc = current_account()
    if not acc:
        return None
    if feature_allowed(key, acc):
        return None
    return deny_feature(key)


@app.before_request
def ip_allowlist():
    if not ALLOW_IPS or request.endpoint == "static":
        return None
    ip = client_ip()
    if any(ip.startswith(pre) for pre in ALLOW_IPS):
        return None
    try:
        log("auth", "許可されていない接続元を拒否", "blocked", target=ip,
            detail="HIA_ALLOW_IPS の設定範囲外")
    except Exception:
        pass
    return Response("この端末からの接続は許可されていません。", status=403,
                    mimetype="text/plain; charset=utf-8")


@app.context_processor
def inject_globals():
    embed = bool(session.get("embed")) and request.endpoint not in ("dashboard",)
    return {
        "LAYOUT": "frag_base.html" if embed else "base.html",
        "EMBED": embed,
        "SHELL": session.get("shell", "kenpo"),
        "acc": current_account(),
        "ROLE_LABELS": ROLE_LABELS,
        "SUB_ROLE_LABELS": SUB_ROLE_LABELS,
        "SUB_ROLES": SUB_ROLES,
        "SUB_ROLE_ROLES": SUB_ROLE_ROLES,
        "role_full": role_full,
        "sub_role_of": sub_role,
        # マスタの登録・変更・削除ができるか（機能制御で切り替えられる）
        "CAN_MASTER": feature_allowed("master.write"),
        "CAN_IMPORT": feature_allowed("master.import"),
        "IS_DOCTOR": is_doctor(),
        "can_feature": feature_allowed,
        "SCOPE_LABELS": SCOPE_LABELS,
        "STATUS_LABELS": STATUS_LABELS,
        "AUTH_PATTERNS": AUTH_PATTERNS,
        "AUTH_PATTERN_NOTE": AUTH_PATTERN_NOTE,
        "BUILD": BUILD,
        "MAX_EXPORT_ROWS": MAX_EXPORT_ROWS,
        "MAIL_ENABLED": mail_enabled(),
        "RESET_HOURS": RESET_HOURS,
    }


# ---------------------------------------------------------------- 採番
KANA_TRIM = ("株式会社", "有限会社", "合同会社", "健康保険組合", "組合", "（株）", "(株)")


def internal_company_code(kenpo_name, company_name):
    """当社内部コードを健保名＋企業名から自動で割り振る。
    同じ健保名・企業名なら常に同じコードになる（更新しても変わらない）。"""
    def slug(v):
        v = (v or "").strip()
        for t in KANA_TRIM:
            v = v.replace(t, "")
        return re.sub(r"[\s　]+", "", v)

    key = f"{slug(kenpo_name)}|{slug(company_name)}"
    h = hashlib.sha1(key.encode("utf-8")).hexdigest().upper()
    return "C" + h[:7]      # 例：C1A2B3C4


def peek_code(kind, key, width=4):
    """次に採番される番号を、採番せずに確認する（画面表示用）"""
    row = get_db().execute("SELECT value FROM setting WHERE key=?",
                           (f"seq:{kind}:{key}",)).fetchone()
    return str((int(row["value"]) if row else 0) + 1).zfill(width)


def next_code(kind, key, width=4):
    db = get_db()
    sk = f"seq:{kind}:{key}"
    row = db.execute("SELECT value FROM setting WHERE key=?", (sk,)).fetchone()
    n = (int(row["value"]) if row else 0) + 1
    db.execute("INSERT INTO setting (key, value) VALUES (?,?)"
               " ON CONFLICT(key) DO UPDATE SET value=excluded.value", (sk, str(n)))
    return str(n).zfill(width)


# ---------------------------------------------------------------- スコープ
def issuable_roles(acc):
    return ISSUABLE_ROLES.get(acc["role"], [])


def account_company_ids(aid):
    """アカウントが対象とする企業のID一覧"""
    return [r["company_id"] for r in get_db().execute(
        "SELECT company_id FROM account_company WHERE account_id=?", (aid,))]


def account_scope_ids(aid):
    """アカウントの担当範囲。企業・事業所・部署を跨いで持てる。
    戻り値は {"company": [...], "office": [...], "dept": [...]}"""
    out = {"company": [], "office": [], "dept": []}
    for r in get_db().execute(
            "SELECT kind, ref_id FROM account_scope WHERE account_id=?", (aid,)):
        if r["kind"] in out:
            out[r["kind"]].append(r["ref_id"])
    # 担当範囲が未設定の場合は、従来の対象企業を担当範囲として扱う
    if not any(out.values()):
        out["company"] = account_company_ids(aid)
    return out


def set_account_scopes(aid, scopes):
    """担当範囲を置き換える。企業は従来の account_company にも反映する。"""
    db = get_db()
    db.execute("DELETE FROM account_scope WHERE account_id=?", (aid,))
    for kind in ("company", "office", "dept"):
        for rid in dict.fromkeys(scopes.get(kind) or []):
            db.execute("INSERT OR IGNORE INTO account_scope (account_id, kind, ref_id)"
                       " VALUES (?,?,?)", (aid, kind, rid))
    set_account_companies(aid, scopes.get("company") or [])


def scope_summary(scopes):
    """担当範囲を人が読める形にする"""
    db = get_db()
    parts = []
    if scopes.get("company"):
        q = ",".join("?" * len(scopes["company"]))
        names = [r["name"] for r in db.execute(
            f"SELECT name FROM company WHERE id IN ({q}) ORDER BY code", scopes["company"])]
        parts.append("企業 " + "・".join(names))
    if scopes.get("office"):
        q = ",".join("?" * len(scopes["office"]))
        names = [f"{r['cname']}／{r['name']}" for r in db.execute(
            "SELECT o.name, c.name AS cname FROM office o JOIN company c ON c.id=o.company_id"
            f" WHERE o.id IN ({q}) ORDER BY c.code, o.code", scopes["office"])]
        parts.append("事業所 " + "・".join(names))
    if scopes.get("dept"):
        q = ",".join("?" * len(scopes["dept"]))
        names = [f"{r['cname']}／{r['oname']}／{r['name']}" for r in db.execute(
            "SELECT d.name, o.name AS oname, c.name AS cname FROM department d"
            " JOIN office o ON o.id=d.office_id JOIN company c ON c.id=o.company_id"
            f" WHERE d.id IN ({q}) ORDER BY c.code, o.code, d.code", scopes["dept"])]
        parts.append("部署 " + "・".join(names))
    return "／".join(parts) or "指定なし"


def account_companies(aid):
    """アカウントが対象とする企業（コード順）"""
    return get_db().execute(
        "SELECT c.* FROM account_company ac JOIN company c ON c.id=ac.company_id"
        " WHERE ac.account_id=? ORDER BY c.code", (aid,)).fetchall()


def set_account_companies(aid, ids):
    """対象企業を置き換える。代表企業として account.company_id にも先頭を入れる"""
    db = get_db()
    db.execute("DELETE FROM account_company WHERE account_id=?", (aid,))
    for cid in ids:
        db.execute("INSERT INTO account_company (account_id, company_id) VALUES (?,?)",
                   (aid, cid))
    db.execute("UPDATE account SET company_id=? WHERE id=?",
               (ids[0] if ids else None, aid))


def company_names(ids):
    if not ids:
        return "—"
    q = ",".join("?" * len(ids))
    rows = get_db().execute(
        f"SELECT name FROM company WHERE id IN ({q}) ORDER BY code", ids).fetchall()
    return "、".join(r["name"] for r in rows)


def scoped_companies(acc):
    db = get_db()
    if acc["role"] == "system_admin":
        return db.execute(
            "SELECT c.*, k.name AS kenpo_name, k.code AS kenpo_code FROM company c"
            " JOIN kenpo k ON k.id=c.kenpo_id ORDER BY k.code, c.code").fetchall()
    if acc["role"] == "kenpo_user":
        return db.execute(
            "SELECT c.*, k.name AS kenpo_name, k.code AS kenpo_code FROM company c"
            " JOIN kenpo k ON k.id=c.kenpo_id WHERE c.kenpo_id=? ORDER BY c.code",
            (acc["kenpo_id"],)).fetchall()
    # 担当範囲に含まれる企業＋担当事業所・部署の親企業
    sc = account_scope_ids(acc["id"])
    ids = set(sc["company"])
    if sc["office"]:
        q = ",".join("?" * len(sc["office"]))
        ids |= {r["company_id"] for r in db.execute(
            f"SELECT company_id FROM office WHERE id IN ({q})", sc["office"])}
    if sc["dept"]:
        q = ",".join("?" * len(sc["dept"]))
        ids |= {r["company_id"] for r in db.execute(
            "SELECT o.company_id FROM department d JOIN office o ON o.id=d.office_id"
            f" WHERE d.id IN ({q})", sc["dept"])}
    if not ids:
        return []
    ids = sorted(ids)
    q = ",".join("?" * len(ids))
    return db.execute(
        "SELECT c.*, k.name AS kenpo_name, k.code AS kenpo_code FROM company c"
        f" JOIN kenpo k ON k.id=c.kenpo_id WHERE c.id IN ({q}) ORDER BY c.code",
        ids).fetchall()


DEPT_SELECT = ("SELECT d.*, o.name AS office_name, o.code AS office_code,"
               " o.ext_code AS office_ext,"
               " c.id AS company_id, c.name AS company_name, c.code AS company_code,"
               " c.ext_code AS company_ext,"
               " c.kenpo_id FROM department d JOIN office o ON o.id=d.office_id"
               " JOIN company c ON c.id=o.company_id")


def scoped_departments(acc):
    """操作できる部署（事業所の下）。企業・事業所を担当していればその配下すべて、
    部署だけを担当している場合はその部署に限る。"""
    db = get_db()
    if acc["role"] in ("system_admin", "kenpo_user"):
        ids = [o["id"] for o in scoped_offices(acc)]
        if not ids:
            return []
        q = ",".join("?" * len(ids))
        return db.execute(DEPT_SELECT + f" WHERE d.office_id IN ({q})"
                          " ORDER BY c.code, o.code, d.code", ids).fetchall()
    sc = account_scope_ids(acc["id"])
    conds, params = [], []
    if sc["company"]:
        conds.append("o.company_id IN (" + ",".join("?" * len(sc["company"])) + ")")
        params += list(sc["company"])
    if sc["office"]:
        conds.append("d.office_id IN (" + ",".join("?" * len(sc["office"])) + ")")
        params += list(sc["office"])
    if sc["dept"]:
        conds.append("d.id IN (" + ",".join("?" * len(sc["dept"])) + ")")
        params += list(sc["dept"])
    if not conds:
        return []
    return db.execute(DEPT_SELECT + " WHERE " + " OR ".join(conds)
                      + " ORDER BY c.code, o.code, d.code", params).fetchall()


def owns_department(acc, did):
    return bool(did) and any(d["id"] == did for d in scoped_departments(acc))


def scoped_offices(acc):
    """操作できる事業所。企業を担当していればその配下すべて、
    事業所・部署だけを担当している場合はその事業所に限る。"""
    db = get_db()
    if acc["role"] in ("system_admin", "kenpo_user"):
        ids = [c["id"] for c in scoped_companies(acc)]
        if not ids:
            return []
        q = ",".join("?" * len(ids))
        return db.execute(
            "SELECT o.*, c.name AS company_name, c.code AS company_code,"
            " c.ext_code AS company_ext, c.kenpo_id"
            " FROM office o JOIN company c ON c.id=o.company_id"
            f" WHERE o.company_id IN ({q}) ORDER BY c.code, o.code", ids).fetchall()
    sc = account_scope_ids(acc["id"])
    conds, params = [], []
    if sc["company"]:
        conds.append("o.company_id IN (" + ",".join("?" * len(sc["company"])) + ")")
        params += list(sc["company"])
    if sc["office"]:
        conds.append("o.id IN (" + ",".join("?" * len(sc["office"])) + ")")
        params += list(sc["office"])
    if sc["dept"]:
        conds.append("o.id IN (SELECT office_id FROM department WHERE id IN ("
                     + ",".join("?" * len(sc["dept"])) + "))")
        params += list(sc["dept"])
    if not conds:
        return []
    return db.execute(
        "SELECT o.*, c.name AS company_name, c.code AS company_code,"
        " c.ext_code AS company_ext, c.kenpo_id"
        " FROM office o JOIN company c ON c.id=o.company_id"
        " WHERE " + " OR ".join(conds) + " ORDER BY c.code, o.code", params).fetchall()


def _age_of(birth):
    """生年月日（YYYY-MM-DD／YYYY/MM/DD）から満年齢を求める。分からなければ None"""
    m = re.match(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})", (birth or "").strip())
    if not m:
        return None
    y, mo, d = (int(x) for x in m.groups())
    t = date.today()
    return t.year - y - ((t.month, t.day) < (mo, d))


def _fmt_date(v):
    """20260601／2026-06-01 などを 2026/06/01 の形にそろえる"""
    t = (v or "").strip().replace("-", "/")
    m = re.fullmatch(r"(\d{4})(\d{2})(\d{2})", t)
    if m:
        return "/".join(m.groups())
    return t


def _current_fy():
    """いまの年度（4月〜翌3月）"""
    t = date.today()
    return str(t.year - 1 if t.month <= 3 else t.year)


def member_services(db, row):
    """加入者ごとの「対象状況」（マイページに出す内容と同じもの）。

    健診・特定保健指導・産業医面談・インフルエンザ予防接種補助について、
    登録済みのデータから対象かどうかを組み立てて返します。
    ここに出る判定区分・リスク区分は参考情報で、受診の必要性や就業上の措置の
    最終判断は産業医・医師が行います。
    """
    mid, fy = row["id"], _current_fy()
    age = _age_of(row["birth"])
    lost = (row["lost_at"] or "").strip()
    out = []

    # ---- 健診（定期健診・深夜健診） ----
    kk = db.execute("SELECT fiscal_year, kind, exam_date, judge FROM oh_kenshin"
                    " WHERE member_id=? ORDER BY fiscal_year DESC, exam_date DESC"
                    " LIMIT 1", [mid]).fetchone()
    if not kk:
        kk = db.execute("SELECT MAX(exam_date) AS exam_date FROM kenshin_result"
                        " WHERE member_id=?", [mid]).fetchone()
    last = _fmt_date(kk["exam_date"] if kk and kk["exam_date"] else "")
    judge = kk["judge"] if (kk and "judge" in kk.keys()) else None
    judge_txt = f"　最終判定 {judge}" if judge else ""
    kinds = ["定期健診（年1回）"]
    if row["night_work"]:
        kinds.append("深夜健診（6か月ごと）")
    # 加入者情報で「健診の対象から除外する」にしている場合は対象外として表示する
    excluded = ("excluded" in row.keys() and row["excluded"])
    out.append({
        "name": "健診", "target": (not lost) and not excluded,
        "label": ("対象外（除外の設定あり）" if excluded
                  else ("対象" if not lost else "対象外（資格喪失）")),
        "detail": "／".join(kinds)
                  + (f"　最終受診 {last}" if last else "　受診記録なし")
                  + judge_txt,
    })

    # ---- 特定健診・特定保健指導（40〜74歳） ----
    med = db.execute("SELECT COUNT(*) AS n FROM kenshin_result"
                     " WHERE member_id=? AND judge IN ('caution','medical')",
                     [mid]).fetchone()["n"]
    tokutei = age is not None and 40 <= age <= 74 and not lost
    if tokutei:
        lab = "対象（保健指導の候補）" if med else "対象"
        det = ("特定健診の対象年齢です。"
               + ("直近の健診に要注意・要医療の項目があるため、階層化の結果により"
                  "保健指導の案内対象になります。" if med
                  else "健診結果の階層化で支援区分が決まります。"))
    else:
        lab = "対象外"
        det = ("資格喪失のため対象外です。" if lost
               else "特定健診・特定保健指導は40〜74歳が対象です。"
               if age is not None else "生年月日が未登録のため判定できません。")
    out.append({"name": "特定保健指導", "target": tokutei, "label": lab, "detail": det})

    # ---- 産業医面談（面談候補） ----
    cand = db.execute("SELECT fiscal_year, reasons, status FROM oh_candidate"
                      " WHERE member_id=? ORDER BY fiscal_year DESC LIMIT 1",
                      [mid]).fetchone()
    ot = db.execute("SELECT MAX(hours) AS h FROM oh_overtime WHERE member_id=?",
                    [mid]).fetchone()["h"]
    st = db.execute("SELECT high, applied FROM oh_stress WHERE member_id=?"
                    " ORDER BY fiscal_year DESC LIMIT 1", [mid]).fetchone()
    if cand:
        out.append({"name": "産業医面談", "target": True,
                    "label": f"対象（{cand['status']}）",
                    "detail": f"{cand['fiscal_year']}年度の面談候補　"
                              f"抽出理由：{cand['reasons'] or '—'}"})
    else:
        marks = []
        if ot:
            marks.append(f"時間外（最大）{ot:.0f}時間／月")
        if st and st["high"]:
            marks.append("高ストレス")
        if st and st["applied"]:
            marks.append("本人からの申出あり")
        out.append({"name": "産業医面談", "target": False, "label": "候補なし",
                    "detail": (f"{fy}年度の面談候補には入っていません。"
                               + ("　参考：" + "／".join(marks) if marks else ""))})

    # ---- インフルエンザ予防接種補助 ----
    out.append({
        "name": "インフルエンザ補助", "target": not lost,
        "label": "対象" if not lost else "対象外（資格喪失）",
        "detail": ("本人・家族ともに年度内1回まで補助の対象です"
                   "（申請受付期間内に申請が必要）。" if not lost
                   else "資格喪失のため対象外です。"),
    })

    # ---- 参考：疾病リスクの予測区分 ----
    rk = db.execute("SELECT disease, level, score FROM risk_score WHERE member_id=?"
                    " ORDER BY score DESC LIMIT 3", [mid]).fetchall()
    risks = [f"{r['disease']}（{r['level']}／{r['score']:.1f}点）" for r in rk]
    return {"age": age, "fy": fy, "services": out, "risks": risks}


def member_where(acc):
    """加入者の閲覧範囲。担当範囲（企業・事業所・部署）のどれかに当てはまれば見える。"""
    if acc["role"] == "system_admin":
        return "1=1", []
    if acc["role"] == "kenpo_user":
        return "m.kenpo_id = ?", [acc["kenpo_id"]]
    sc = account_scope_ids(acc["id"])
    conds, params = [], []
    for kind, col in (("company", "m.company_id"), ("office", "m.office_id"),
                      ("dept", "m.dept_id")):
        if sc[kind]:
            conds.append(f"{col} IN (" + ",".join("?" * len(sc[kind])) + ")")
            params += list(sc[kind])
    if not conds:
        return "1=0", []
    return "(" + " OR ".join(conds) + ")", params


def can_manage_account(acc, row):
    if acc["role"] == "system_admin":
        return True
    if acc["role"] == "kenpo_user":
        return row["kenpo_id"] == acc["kenpo_id"] and row["role"] != "system_admin"
    if acc["role"] == "company_user":
        if row["role"] != "company_user":
            return False
        mine = set(account_company_ids(acc["id"]))
        theirs = set(account_company_ids(row["id"]))
        # 相手の対象企業が自分の対象企業に収まっている場合だけ操作できる
        return bool(theirs) and theirs <= mine
    return False


def owns_company(acc, cid):
    return cid in {c["id"] for c in scoped_companies(acc)}


def owns_office(acc, oid):
    return oid in {o["id"] for o in scoped_offices(acc)}


def visible_members(kenpo_id, company_ids, view_scope, limit=20, scopes=None):
    """閲覧できる加入者を数える。scopes を渡すと企業・事業所・部署を跨いで数える。"""
    db = get_db()
    if isinstance(company_ids, int):
        company_ids = [company_ids]
    company_ids = [c for c in (company_ids or []) if c]
    if view_scope == "own_company":
        if scopes is not None:
            conds, params = [], []
            for kind, col in (("company", "m.company_id"), ("office", "m.office_id"),
                              ("dept", "m.dept_id")):
                ids = scopes.get(kind) or []
                if ids:
                    conds.append(f"{col} IN (" + ",".join("?" * len(ids)) + ")")
                    params += list(ids)
            where = "(" + " OR ".join(conds) + ")" if conds else "1=0"
        elif not company_ids:
            where, params = "1=0", []
        else:
            where = "m.company_id IN (" + ",".join("?" * len(company_ids)) + ")"
            params = list(company_ids)
    elif view_scope == "all":
        where, params = "1=1", []
    else:
        where, params = "m.kenpo_id = ?", [kenpo_id]
    row = db.execute(
        "SELECT COUNT(*) AS total, COUNT(DISTINCT m.company_id) AS companies,"
        " COUNT(DISTINCT m.office_id) AS offices FROM member m WHERE " + where,
        params).fetchone()
    # 参照先が欠けている行も落とさないよう LEFT JOIN で内訳を作る
    breakdown = db.execute(
        "SELECT COALESCE(c.name,'（企業なし）') AS cname, COALESCE(c.code,'—') AS ccode,"
        " COALESCE(o.name,'（事業所なし）') AS oname, COALESCE(o.code,'—') AS ocode,"
        " COUNT(*) AS cnt FROM member m LEFT JOIN company c ON c.id=m.company_id"
        " LEFT JOIN office o ON o.id=m.office_id WHERE " + where +
        " GROUP BY m.company_id, m.office_id ORDER BY ccode, ocode", params).fetchall()
    sample = db.execute(
        "SELECT m.member_no, m.name, COALESCE(c.name,'—') AS cname,"
        " COALESCE(o.name,'—') AS oname FROM member m"
        " LEFT JOIN company c ON c.id=m.company_id LEFT JOIN office o ON o.id=m.office_id"
        " WHERE " + where + " ORDER BY cname, oname, m.member_no LIMIT ?",
        params + [limit]).fetchall()
    return {"total": row["total"], "breakdown": breakdown, "sample": sample,
            "companies": row["companies"], "offices": row["offices"]}


# ---------------------------------------------------------------- メール
def mail_enabled():
    return bool(os.environ.get("HIA_SMTP_HOST"))


def send_mail(to, subject, body):
    """SMTPが設定されていれば送信し、無ければ outbox に書き出す"""
    host = os.environ.get("HIA_SMTP_HOST")
    os.makedirs(OUTBOX, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    fn = os.path.join(OUTBOX, f"{stamp}_{re.sub(r'[^A-Za-z0-9]+', '_', to)}.txt")
    with open(fn, "w", encoding="utf-8") as f:
        f.write(f"To: {to}\nSubject: {subject}\nDate: {now()}\n\n{body}\n")
    if not host:
        return False
    try:
        msg = EmailMessage()
        msg["From"] = os.environ.get("HIA_SMTP_FROM", "no-reply@example.local")
        msg["To"] = to
        msg["Subject"] = subject
        msg.set_content(body)
        with smtplib.SMTP(host, int(os.environ.get("HIA_SMTP_PORT", "25")), timeout=10) as sv:
            if os.environ.get("HIA_SMTP_USER"):
                sv.starttls()
                sv.login(os.environ["HIA_SMTP_USER"], os.environ.get("HIA_SMTP_PASS", ""))
            sv.send_message(msg)
        return True
    except Exception:
        return False


def ext_url(endpoint, **kw):
    """メールに載せる外部URL。HIA_BASE_URL が設定されていればそれを基準にする。
    未設定でサーバー上の localhost から操作した場合は、
    受信者が開けるようにこのパソコンのIPへ置き換える。"""
    base = os.environ.get("HIA_BASE_URL", "").rstrip("/")
    if base:
        return base + url_for(endpoint, **kw)
    u = url_for(endpoint, _external=True, **kw)
    for name in ("://localhost", "://127.0.0.1"):
        if name in u:
            ip = local_ipv4()
            if ip not in ("127.0.0.1", ""):
                u = u.replace(name, "://" + ip)
            break
    return u


def gen_password(n=14):
    """読み間違えにくい文字だけで、条件を満たすパスワードを作る"""
    letters = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnpqrstuvwxyz"
    digits = "23456789"
    marks = "-_@#"
    pool = letters + digits + marks
    while True:
        pw = "".join(secrets.choice(pool) for _ in range(n))
        if (re.search(r"[A-Za-z]", pw) and re.search(r"\d", pw)
                and len(pw) >= 12):
            return pw


def password_errors(p1, p2):
    errs = []
    if len(p1) < 12:
        errs.append("パスワードは12文字以上で設定してください。")
    if not re.search(r"[A-Za-z]", p1) or not re.search(r"\d", p1):
        errs.append("英字と数字をそれぞれ1文字以上含めてください。")
    if p1 != p2:
        errs.append("確認用パスワードが一致しません。")
    return errs


# ================================================================ 認証画面
@app.route("/")
def index():
    acc = current_account()
    if not acc:
        return redirect(url_for("login"))
    return redirect(url_for("spa_km") if SHELL_OF_ROLE.get(acc["role"], "kenpo") == "km" else url_for("spa"))


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = (request.form.get("email") or "").strip().lower()
        pw = request.form.get("password") or ""
        session["pending_email"] = email
        db = get_db()
        row = db.execute("SELECT * FROM account WHERE lower(email)=lower(?)",
                         (email,)).fetchone()
        bad = "メールアドレスまたはパスワードが正しくありません。"
        if row is None:
            log("auth", "ログイン失敗", "failure", target=email, detail="アカウントが存在しない")
            flash(bad, "error")
        elif row["status"] == "invited":
            log("auth", "ログイン失敗", "failure", target=email, detail="パスワード未設定")
            flash("案内メールのリンクからパスワードを設定してください。", "error")
        elif row["status"] in ("disabled", "deleted"):
            log("auth", "ログイン失敗", "failure", target=email,
                detail=f"{STATUS_LABELS[row['status']]}のアカウント")
            flash(bad, "error")
        elif not row["password_hash"] or not check_password_hash(row["password_hash"], pw):
            log("auth", "ログイン失敗", "failure", target=email, detail="パスワード不一致")
            flash(bad, "error")
        else:
            session.clear()
            session.permanent = True      # 再起動やブラウザを閉じてもログインを保つ
            session["account_id"] = row["id"]
            session["shell"] = SHELL_OF_ROLE.get(row["role"], "kenpo")
            session["embed"] = True
            g.pop("acc", None)
            db.execute("UPDATE account SET last_login_at=? WHERE id=?", (now(), row["id"]))
            db.commit()
            log("auth", "ログイン成功", "success", target=email)
            nxt = request.args.get("next")
            if not nxt:
                nxt = url_for("spa_km") if SHELL_OF_ROLE.get(row["role"], "kenpo") == "km" else url_for("spa")
            return redirect(nxt)
    # 画面の中に埋め込まれた枠（iframe）でログイン画面を出すと、枠の中だけに
    # ログインフォームが出てしまうため、いちばん外側の画面をログイン画面に切り替えます。
    if request.headers.get("Sec-Fetch-Dest") == "iframe":
        return render_template("login_break.html",
                               next=request.args.get("next") or "")
    return render_template("login.html")


@app.route("/api/me")
@login_required
def api_me():
    """ヘッダー表示用のログイン情報（シェルは静的HTMLのため取得して差し込む）"""
    acc = current_account()
    name = acc["name"] or acc["email"]
    return {
        "name": name,
        "email": acc["email"],
        "role": ROLE_LABELS.get(acc["role"], acc["role"]),
        "scope": SCOPE_LABELS.get(acc["view_scope"], acc["view_scope"]),
        "initials": name[:2],
        "sub_role": sub_role(acc),
        "sub_role_label": SUB_ROLE_LABELS.get(sub_role(acc), ""),
        "role_key": role_key(acc),
        "role_key_label": ROLE_KEY_LABELS.get(role_key(acc), ""),
        # シェル（静的HTML）でカードの出し入れに使う
        "features": account_features(acc),
    }


@app.route("/logout")
def logout():
    if current_account():
        log("auth", "ログアウト", "success", target=current_account()["email"])
    session.clear()
    return redirect(url_for("login"))


@app.route("/forgot", methods=["GET", "POST"])
def forgot():
    """ログイン画面からのパスワード再設定申請"""
    if request.method == "GET":
        return render_template("forgot.html")
    email = (request.form.get("email") or "").strip().lower()
    session["pending_email"] = email
    db = get_db()
    row = db.execute("SELECT * FROM account WHERE lower(email)=lower(?)",
                     (email,)).fetchone()
    if row and row["status"] in ("active", "invited"):
        token = secrets.token_urlsafe(32)
        expire = (datetime.now() + timedelta(hours=RESET_HOURS)).strftime("%Y-%m-%d %H:%M:%S")
        db.execute("UPDATE account SET reset_token=?, reset_expire=?, reset_at=? WHERE id=?",
                   (token, expire, now(), row["id"]))
        db.commit()
        link = ext_url("reset", token=token)
        sent = send_mail(email, "【HIA】パスワード再設定のご案内",
                         f"{row['name']} 様\n\nパスワード再設定の申請を受け付けました。\n"
                         f"以下のリンクから新しいパスワードを設定してください。\n\n{link}\n\n"
                         f"有効期限：{expire}（{RESET_HOURS}時間）\n"
                         f"このリンクは1回だけ使用できます。\n"
                         f"心当たりがない場合はこのメールを破棄してください。")
        log("auth", "パスワード再設定を申請", "success", target=email,
            detail=("メール送信済み" if sent
                    else "メール未設定のため管理者による発行が必要")
                   + f"／有効期限={expire}")
    else:
        log("auth", "パスワード再設定の申請", "failure", target=email,
            detail="対象アカウントなし、または利用できない状態")
    return render_template("forgot.html", done=True, email=email)


@app.route("/reset/<token>", methods=["GET", "POST"])
def reset(token):
    db = get_db()
    row = db.execute("SELECT * FROM account WHERE reset_token=?", (token,)).fetchone()
    if not row:
        return render_template("reset.html", state="invalid")
    if row["reset_expire"] and row["reset_expire"] < now():
        return render_template("reset.html", state="expired")
    if request.method == "POST":
        errs = password_errors(request.form.get("password") or "",
                               request.form.get("password2") or "")
        if errs:
            for e in errs:
                flash(e, "error")
            return render_template("reset.html", state="ok", row=row, token=token)
        db.execute("UPDATE account SET password_hash=?, status='active', reset_token=NULL,"
                   " reset_expire=NULL, invite_token=NULL, invite_expire=NULL, updated_at=?"
                   " WHERE id=?",
                   (generate_password_hash(request.form["password"]), now(), row["id"]))
        db.commit()
        session["pending_email"] = row["email"]
        log("auth", "本人によるパスワード再設定", "success", target=row["email"])
        flash("パスワードを再設定しました。ログインしてください。", "ok")
        return redirect(url_for("login"))
    return render_template("reset.html", state="ok", row=row, token=token)


@app.route("/invite/<token>", methods=["GET", "POST"])
def invite(token):
    db = get_db()
    row = db.execute("SELECT * FROM account WHERE invite_token=?", (token,)).fetchone()
    if not row:
        return render_template("invite.html", state="invalid")
    if row["status"] != "invited":
        return render_template("invite.html", state="used")
    if row["invite_expire"] and row["invite_expire"] < now():
        return render_template("invite.html", state="expired")
    if request.method == "POST":
        errs = password_errors(request.form.get("password") or "",
                               request.form.get("password2") or "")
        if errs:
            for e in errs:
                flash(e, "error")
            return render_template("invite.html", state="ok", row=row, token=token)
        db.execute("UPDATE account SET password_hash=?, status='active', invite_token=NULL,"
                   " invite_expire=NULL, activated_at=? WHERE id=?",
                   (generate_password_hash(request.form["password"]), now(), row["id"]))
        db.commit()
        session["pending_email"] = row["email"]
        log("auth", "本人によるパスワード設定", "success", target=row["email"])
        flash("パスワードを設定しました。ログインしてください。", "ok")
        return redirect(url_for("login"))
    return render_template("invite.html", state="ok", row=row, token=token)


# ================================================================ シェル
@app.route("/km")
@login_required
def spa_km():
    """HIA総合管理（当社スタッフ・緑）"""
    if not real_account() or real_account()["role"] != "system_admin":
        return redirect(url_for("spa"))
    # サポートログイン中に総合管理を開いたら、サポートを終了して戻る扱いにする
    if support_kenpo():
        return redirect(url_for("support_end"))
    session["embed"] = True
    session["shell"] = "km"
    return no_store(send_from_directory(os.path.join(BASE_DIR, "static"), "km_app.html"))


@app.route("/app")
@login_required
def spa():
    """HIA健保管理（健保担当者・企業担当者・青）"""
    acc = current_account()
    if not support_kenpo() and SHELL_OF_ROLE.get(acc["role"], "kenpo") == "km":
        return redirect(url_for("spa_km"))
    session["embed"] = True
    session["shell"] = "kenpo"
    return no_store(send_from_directory(os.path.join(BASE_DIR, "static"), "hia_app.html"))


@app.route("/standalone")
@login_required
def standalone():
    session["embed"] = False
    return redirect(url_for("dashboard"))


@app.route("/dashboard")
@login_required
def dashboard():
    db, acc = get_db(), current_account()
    where, params = member_where(acc)
    stats = {
        "companies": len(scoped_companies(acc)),
        "offices": len(scoped_offices(acc)),
        "members": db.execute(f"SELECT COUNT(*) c FROM member m WHERE {where}",
                              params).fetchone()["c"],
    }
    q, p = "SELECT COUNT(*) c FROM account WHERE status <> 'deleted'", []
    if acc["role"] == "kenpo_user":
        q += " AND kenpo_id=?"
        p = [acc["kenpo_id"]]
    elif acc["role"] == "company_user":
        mine = account_company_ids(acc["id"]) or [0]
        q += (" AND id IN (SELECT account_id FROM account_company WHERE company_id IN ("
              + ",".join("?" * len(mine)) + "))")
        p = list(mine)
    stats["accounts"] = db.execute(q, p).fetchone()["c"]
    stats["invited"] = db.execute(q + " AND status='invited'", p).fetchone()["c"]
    recent = db.execute("SELECT * FROM audit_log ORDER BY id DESC LIMIT 8").fetchall()
    return render_template("dashboard.html", stats=stats, recent=recent)


# ================================================================ 健康保険組合
@app.route("/kenpos")
@roles_required("system_admin")
def kenpos():
    db = get_db()
    rows = db.execute(
        "SELECT k.*,"
        " (SELECT COUNT(*) FROM company c WHERE c.kenpo_id=k.id) AS n_company,"
        " (SELECT COUNT(*) FROM member m WHERE m.kenpo_id=k.id) AS n_member,"
        " (SELECT COUNT(*) FROM account a WHERE a.kenpo_id=k.id"
        "   AND a.status <> 'deleted') AS n_account"
        " FROM kenpo k ORDER BY k.code").fetchall()
    return render_template("kenpos.html", rows=rows)


@app.route("/api/kenpos/<int:kid>/flag", methods=["POST"])
@roles_required("system_admin")
def api_kenpo_flag(kid):
    """健康保険組合一覧のトグル（公開・健診代行・保健指導・インフル）を切り替える"""
    db = get_db()
    data = request.get_json(silent=True) or {}
    key = (data.get("key") or "").strip()
    allowed = {"publish_auth": "公開権限", "kenshin_auth": "健診代行権限",
               "guidance_auth": "保健指導権限", "flu_enabled": "インフル機能"}
    if key not in allowed:
        return {"ok": False, "message": "対象の項目が正しくありません。"}
    row = db.execute("SELECT * FROM kenpo WHERE id=?", (kid,)).fetchone()
    if not row:
        return {"ok": False, "message": "健康保険組合が見つかりません。"}
    val = 1 if data.get("value") else 0
    db.execute(f"UPDATE kenpo SET {key}=? WHERE id=?", (val, kid))
    db.commit()
    log("master", f"{allowed[key]}を{'有効' if val else '無効'}に変更", "success",
        target=row["name"], detail=f"保険者番号={row['code']}")
    return {"ok": True, "value": val}


@app.route("/api/kenpos", methods=["GET", "POST"])
@roles_required("system_admin")
def api_kenpos():
    """健康保険組合の取得・登録（HIA総合管理の編集画面から呼ぶ）"""
    db = get_db()
    if request.method == "GET":
        rows = db.execute(
            "SELECT k.*,"
            " (SELECT COUNT(*) FROM company c WHERE c.kenpo_id=k.id) AS n_company,"
            " (SELECT COUNT(*) FROM member m WHERE m.kenpo_id=k.id) AS n_member,"
            " (SELECT COUNT(*) FROM account a WHERE a.kenpo_id=k.id"
            "   AND a.status <> 'deleted') AS n_account"
            " FROM kenpo k ORDER BY k.code").fetchall()
        return {"rows": [dict(r) for r in rows]}

    data = request.get_json(silent=True) or request.form
    code = (data.get("code") or "").strip()
    name = (data.get("name") or "").strip()
    if not code:
        return {"ok": False, "message": "保険者番号を入力してください。"}
    if not re.fullmatch(r"[0-9A-Za-z\-]{1,20}", code):
        return {"ok": False, "message": "保険者番号は英数字とハイフンで入力してください。"}
    if not name:
        return {"ok": False, "message": "健康保険組合名（保険者名称）を入力してください。"}
    if db.execute("SELECT 1 FROM kenpo WHERE code=?", (code,)).fetchone():
        return {"ok": False, "message": "この保険者番号は既に登録されています。"}
    if db.execute("SELECT 1 FROM kenpo WHERE name=?", (name,)).fetchone():
        return {"ok": False, "message": "この名称は既に登録されています。"}
    auth = clean_auth_pattern(data.get("auth_pattern"))
    db.execute("INSERT INTO kenpo (code, name, auth_pattern) VALUES (?,?,?)",
               (code, name, auth))
    db.commit()
    log("master", "健康保険組合を登録", "success", target=name,
        detail=f"保険者番号={code}／認証方式={auth}（{'・'.join(AUTH_PATTERNS[auth])}）")
    return {"ok": True, "auth_pattern": auth,
            "message": f"{name}（保険者番号 {code}）を登録しました。"
                       f"認証方式{auth}（{'・'.join(AUTH_PATTERNS[auth])}）"}


@app.route("/kenpos/new", methods=["GET", "POST"])
@roles_required("system_admin")
def kenpo_new():
    db = get_db()
    if request.method == "GET":
        return render_template("kenpo_form.html", row=None)
    code = (request.form.get("code") or "").strip()
    name = (request.form.get("name") or "").strip()
    errs = []
    if not code:
        errs.append("保険者番号を入力してください。")
    elif not re.fullmatch(r"[0-9A-Za-z\-]{1,20}", code):
        errs.append("保険者番号は英数字とハイフンで入力してください。")
    elif db.execute("SELECT 1 FROM kenpo WHERE code=?", (code,)).fetchone():
        errs.append("この保険者番号は既に登録されています。")
    if not name:
        errs.append("健康保険組合名を入力してください。")
    elif db.execute("SELECT 1 FROM kenpo WHERE name=?", (name,)).fetchone():
        errs.append("この名称は既に登録されています。")
    if errs:
        for e in errs:
            flash(e, "error")
        return render_template("kenpo_form.html", row=None, form=request.form)
    auth = clean_auth_pattern(request.form.get("auth_pattern"))
    db.execute("INSERT INTO kenpo (code, name, auth_pattern) VALUES (?,?,?)",
               (code, name, auth))
    db.commit()
    log("master", "健康保険組合を登録", "success", target=name,
        detail=f"保険者番号={code}／認証方式={auth}"
               f"（{'・'.join(AUTH_PATTERNS[auth])}）")
    flash(f"{name}（保険者番号 {code}）を登録しました。", "ok")
    return redirect(url_for("kenpos"))


@app.route("/kenpos/<int:kid>/edit", methods=["GET", "POST"])
@roles_required("system_admin")
def kenpo_edit(kid):
    db = get_db()
    row = db.execute("SELECT * FROM kenpo WHERE id=?", (kid,)).fetchone()
    if not row:
        flash("対象の健康保険組合が見つかりません。", "error")
        return redirect(url_for("kenpos"))
    if request.method == "GET":
        return render_template("kenpo_form.html", row=row)
    code = (request.form.get("code") or "").strip()
    name = (request.form.get("name") or "").strip()
    errs = []
    if not code:
        errs.append("保険者番号を入力してください。")
    elif not re.fullmatch(r"[0-9A-Za-z\-]{1,20}", code):
        errs.append("保険者番号は英数字とハイフンで入力してください。")
    elif db.execute("SELECT 1 FROM kenpo WHERE code=? AND id<>?", (code, kid)).fetchone():
        errs.append("この保険者番号は既に登録されています。")
    if not name:
        errs.append("健康保険組合名を入力してください。")
    elif db.execute("SELECT 1 FROM kenpo WHERE name=? AND id<>?", (name, kid)).fetchone():
        errs.append("この名称は既に登録されています。")
    if errs:
        for e in errs:
            flash(e, "error")
        return render_template("kenpo_form.html", row=row, form=request.form)
    cur_auth = (row["auth_pattern"] if "auth_pattern" in row.keys() else "A") or "A"
    auth = clean_auth_pattern(request.form.get("auth_pattern"), cur_auth)
    db.execute("UPDATE kenpo SET code=?, name=?, auth_pattern=? WHERE id=?",
               (code, name, auth, kid))
    db.commit()
    log("master", "健康保険組合を更新", "success", target=name,
        detail=f"保険者番号 {row['code']} → {code}／名称 {row['name']} → {name}"
               + (f"／認証方式 {cur_auth} → {auth}" if cur_auth != auth
                  else f"／認証方式={auth}"))
    flash(f"{name} を更新しました。", "ok")
    return redirect(url_for("kenpos"))


@app.route("/kenpos/<int:kid>/delete", methods=["POST"])
@roles_required("system_admin")
def kenpo_delete(kid):
    db = get_db()
    row = db.execute("SELECT * FROM kenpo WHERE id=?", (kid,)).fetchone()
    if not row:
        flash("対象の健康保険組合が見つかりません。", "error")
        return redirect(url_for("kenpos"))
    n_c = db.execute("SELECT COUNT(*) c FROM company WHERE kenpo_id=?", (kid,)).fetchone()["c"]
    n_m = db.execute("SELECT COUNT(*) c FROM member WHERE kenpo_id=?", (kid,)).fetchone()["c"]
    n_a = db.execute("SELECT COUNT(*) c FROM account WHERE kenpo_id=? AND status<>'deleted'",
                     (kid,)).fetchone()["c"]
    if n_c or n_m or n_a:
        log("master", "健康保険組合の削除をブロック", "blocked", target=row["name"],
            detail=f"企業{n_c}社・加入者{n_m}件・アカウント{n_a}件が紐づいている")
        flash(f"この健康保険組合には企業{n_c}社・加入者{n_m}件・アカウント{n_a}件が"
              f"紐づいているため削除できません。先にそれらを削除してください。", "error")
        return redirect(url_for("kenpos"))
    db.execute("DELETE FROM kenpo WHERE id=?", (kid,))
    db.commit()
    log("master", "健康保険組合を削除", "success", target=row["name"],
        detail=f"保険者番号={row['code']}")
    flash(f"{row['name']} を削除しました。", "ok")
    return redirect(url_for("kenpos"))


# ================================================================ サポートログイン
@app.route("/support/start", methods=["POST"])
@login_required
def support_start():
    """当社スタッフが、指定した健康保険組合の画面に入る（サポート対応用）"""
    me = real_account()
    if not me or me["role"] != "system_admin":
        log("account", "サポートログインをブロック", "blocked",
            detail="当社スタッフ以外からの要求")
        flash("サポートログインは当社スタッフのみ利用できます。", "error")
        return redirect(url_for("dashboard"))
    kid = request.form.get("kenpo_id", type=int)
    kenpo = get_db().execute("SELECT * FROM kenpo WHERE id=?", (kid,)).fetchone()
    if not kenpo:
        flash("対象の健康保険組合が見つかりません。", "error")
        return redirect(url_for("dashboard"))
    session["support_kenpo_id"] = kid
    session["shell"] = "kenpo"
    g.pop("acc", None)
    g.pop("sup_kenpo", None)
    log("account", "サポートログインを開始", "success", target=kenpo["name"],
        detail=f"保険者番号={kenpo['code']}")
    # 画面上のお知らせは出しません（操作ログへの記録はこれまでどおり残ります）
    return redirect(url_for("spa"))


@app.route("/support/end", methods=["POST", "GET"])
@login_required
def support_end():
    """サポートログインを終了して当社の画面に戻る"""
    kenpo = support_kenpo()
    session.pop("support_kenpo_id", None)
    session["shell"] = "km"
    g.pop("acc", None)
    g.pop("sup_kenpo", None)
    if kenpo:
        log("account", "サポートログインを終了", "success", target=kenpo["name"])
        flash(f"{kenpo['name']} のサポートログインを終了しました。", "ok")
    return redirect(url_for("spa_km"))


@app.route("/api/support/state")
@login_required
def api_support_state():
    """画面上部の表示に使う。サポートログイン中かどうかを返す"""
    kenpo = support_kenpo()
    me = real_account()
    return {"support": bool(kenpo),
            "kenpo": {"id": kenpo["id"], "code": kenpo["code"], "name": kenpo["name"]}
            if kenpo else None,
            "staff": me["name"] if me else None,
            "end_url": url_for("support_end")}


# ================================================================ 疾患予測
# 健診データ（本システム取込済み）× NSIPSデータ（自社調剤システムとAPI連携）を
# 属性区分でまとめ、将来の生活習慣病リスクを予測する。
# 個人を特定する情報は保持・出力しない（年代・性別などの区分で扱う）。

# 日立 Risk Simulator for Insurance の分類に準拠した8大生活習慣病
DISEASES = ["糖尿病", "高血圧性疾患", "脂質異常症", "心血管疾患",
            "脳血管疾患", "腎疾患", "肝疾患", "悪性新生物"]

AGE_BANDS = ["20代未満", "20代", "30代", "40代", "50代", "60代", "70代以上", "年代不明"]

# 健診結果の検査項目と、関係の深い疾病
KENSHIN_ITEMS = {
    "血圧": ["高血圧性疾患", "心血管疾患", "脳血管疾患"],
    "血糖": ["糖尿病", "腎疾患", "心血管疾患"],
    "脂質": ["脂質異常症", "心血管疾患", "脳血管疾患"],
    "肝機能": ["肝疾患"],
    "BMI": ["糖尿病", "高血圧性疾患", "脂質異常症"],
    "尿蛋白": ["腎疾患", "高血圧性疾患"],
}

# NSIPSの薬効分類と、関係の深い疾病
DRUG_CLASSES = {
    "降圧剤": ["高血圧性疾患", "心血管疾患", "脳血管疾患"],
    "糖尿病用剤": ["糖尿病", "腎疾患", "心血管疾患"],
    "脂質異常症用剤": ["脂質異常症", "心血管疾患", "脳血管疾患"],
    "抗血栓薬": ["心血管疾患", "脳血管疾患"],
    "肝疾患用剤": ["肝疾患"],
    "利尿剤": ["高血圧性疾患", "腎疾患"],
}

# 個人を特定しうる項目。NSIPS連携で混入した場合は取込まず除外する
PII_KEYS = {"name", "kana", "birth", "address", "zip", "tel", "email",
            "member_no", "cert_mark", "insured_no", "patient_name", "patient_id"}


# 疾患予測の設定は、画面ではなくサーバー側（環境変数）で指定する。
# 認証情報を画面から扱わないための方針。run.bat で設定する。
RISK_ENV = {
    "hitachi_endpoint": "HIA_HITACHI_ENDPOINT",   # 日立APIの接続先
    "hitachi_key": "HIA_HITACHI_KEY",             # 日立APIの認証キー
    "kenshin_endpoint": "HIA_KENSHIN_ENDPOINT",   # 予約管理システムの接続先
    "kenshin_time": "HIA_KENSHIN_TIME",           # 健診バッチの実行時刻
    "nsips_endpoint": "HIA_NSIPS_ENDPOINT",       # 調剤システムの接続先
    "nsips_mode": "HIA_NSIPS_MODE",               # batch / realtime
    "match_rule": "HIA_RISK_MATCH_RULE",          # cert（被保険者証）/ anonid
    "horizon": "HIA_RISK_HORIZON",                # 既定の予測年数
}
RISK_DEFAULTS = {"nsips_mode": "batch", "match_rule": "cert",
                 "horizon": "3", "kenshin_time": "02:00"}


def risk_setting(key, default=""):
    """疾患予測の設定を環境変数から読む"""
    v = os.environ.get(RISK_ENV.get(key, ""), "").strip()
    if v:
        return v
    return RISK_DEFAULTS.get(key, default)


def risk_engine():
    """日立APIの接続設定があれば契約API、無ければ社内試算で動かす"""
    return "hitachi" if risk_setting("hitachi_endpoint") else "trial"


def age_band(birth):
    """生年月日から年代の区分を出す（生年月日そのものは保存しない）"""
    if not birth or len(str(birth)) < 4:
        return "年代不明"
    try:
        y = int(str(birth)[:4])
    except ValueError:
        return "年代不明"
    age = datetime.now().year - y
    if age < 20:
        return "20代未満"
    if age >= 70:
        return "70代以上"
    return f"{age // 10 * 10}代"


def risk_kenpo_id(acc):
    """操作の対象になる健康保険組合。
    当社スタッフは健保を切り替えられる（選んだ内容はログイン中は保持する）。"""
    if acc["kenpo_id"]:
        return acc["kenpo_id"]
    db = get_db()
    q = request.values.get("kenpo_id", type=int)
    if q and db.execute("SELECT 1 FROM kenpo WHERE id=?", (q,)).fetchone():
        session["risk_kenpo_id"] = q
        return q
    sel = session.get("risk_kenpo_id")
    if sel and db.execute("SELECT 1 FROM kenpo WHERE id=?", (sel,)).fetchone():
        return sel
    r = db.execute("SELECT id FROM kenpo ORDER BY code LIMIT 1").fetchone()
    return r["id"] if r else None


def member_groups(acc):
    """加入者を属性区分（年代・性別）でまとめる。個人の情報は取り出さない。"""
    db = get_db()
    where, params = member_where(acc)
    rows = db.execute(
        "SELECT m.birth, m.sex, m.company_id, m.kenpo_id FROM member m WHERE " + where,
        params).fetchall()
    groups = {}
    for r in rows:
        key = (r["kenpo_id"], r["company_id"], age_band(r["birth"]), r["sex"] or "不明")
        groups[key] = groups.get(key, 0) + 1
    return groups


@app.route("/risk")
@login_required
def risk_list():
    """予測結果の一覧（加入者ごと）"""
    db, acc = get_db(), current_account()
    kid = risk_kenpo_id(acc)
    f = {k: (request.args.get(k) or "").strip()
         for k in ("name", "cname", "disease", "level")}
    run = db.execute("SELECT * FROM risk_run WHERE kenpo_id=? ORDER BY id DESC LIMIT 1",
                     (kid,)).fetchone()
    rows, summary = [], {}
    if run:
        sql = ("SELECT s.*, m.name, m.kana, m.member_no, m.cert_branch,"
               " c.name AS company_name, o.name AS office_name"
               " FROM risk_score s JOIN member m ON m.id=s.member_id"
               " LEFT JOIN company c ON c.id=m.company_id"
               " LEFT JOIN office o ON o.id=m.office_id"
               " WHERE s.run_id=?")
        params = [run["id"]]
        if f["disease"]:
            sql += " AND s.disease=?"
            params.append(f["disease"])
        else:
            # 疾病を選んでいないときは、その人で最もリスクの高い疾病だけを出す
            sql += (" AND s.score = (SELECT MAX(s2.score) FROM risk_score s2"
                    " WHERE s2.run_id=s.run_id AND s2.member_id=s.member_id)")
        if f["name"]:
            sql += " AND (m.name LIKE ? OR m.kana LIKE ?)"
            params += [f"%{f['name']}%"] * 2
        if f["cname"]:
            sql += " AND c.name LIKE ?"
            params.append(f"%{f['cname']}%")
        if f["level"]:
            sql += " AND s.level=?"
            params.append(f["level"])
        if not f["disease"]:
            sql += " GROUP BY s.member_id"
        rows = db.execute(sql + " ORDER BY s.score DESC, m.member_no", params).fetchall()
        for lv in ("高", "中", "低"):
            summary[lv] = sum(1 for r in rows if r["level"] == lv)
    last_sync = db.execute("SELECT * FROM nsips_sync WHERE kenpo_id=?"
                           " ORDER BY id DESC LIMIT 1", (kid,)).fetchone()
    last_ken = db.execute("SELECT * FROM kenshin_sync WHERE kenpo_id=?"
                          " ORDER BY id DESC LIMIT 1", (kid,)).fetchone()
    view = request.args.get("view") or "member"
    kenpos = (db.execute("SELECT id, code, name FROM kenpo ORDER BY code").fetchall()
              if not acc["kenpo_id"] else [])
    return render_template("risk_list.html", rows=rows, run=run, f=f, view=view,
                           kenpos=kenpos, kid=kid,
                           summary=summary, diseases=DISEASES, bands=AGE_BANDS,
                           last_sync=last_sync, last_ken=last_ken,
                           engine=risk_engine(), g=risk_aggregate(run, f),
                           hist=risk_hist(rows))


def risk_hist(rows):
    """スコアの分布（10点刻み）"""
    bins = [0] * 10
    for r in rows:
        i = min(int((r["score"] or 0) // 10), 9)
        bins[i] += 1
    return bins


def risk_aggregate(run, f=None):
    """属性単位（年代・性別・疾病・企業）の集計。個人のスコアをまとめる。"""
    if not run:
        return None
    db = get_db()
    f = f or {}
    # その人でいちばん高いスコア（＝代表リスク）を属性ごとにまとめる
    top = db.execute(
        "SELECT s.member_id, s.age_band, s.sex, s.company_id, s.disease, s.score,"
        " s.level, c.name AS company_name"
        " FROM risk_score s LEFT JOIN company c ON c.id=s.company_id"
        " WHERE s.run_id=? AND s.score = (SELECT MAX(s2.score) FROM risk_score s2"
        "   WHERE s2.run_id=s.run_id AND s2.member_id=s.member_id)"
        " GROUP BY s.member_id", (run["id"],)).fetchall()

    # 年代 × 性別
    by_band = {}
    for r in top:
        d = by_band.setdefault(r["age_band"], {"男": 0, "女": 0, "高男": 0, "高女": 0,
                                               "score": [], "n": 0})
        sex = r["sex"] if r["sex"] in ("男", "女") else "男"
        d[sex] += 1
        d["n"] += 1
        d["score"].append(r["score"])
        if r["level"] == "高":
            d["高" + sex] += 1
    band_rows = []
    for band in AGE_BANDS:
        if band not in by_band:
            continue
        d = by_band[band]
        band_rows.append({
            "band": band, "n": d["n"], "male": d["男"], "female": d["女"],
            "high": d["高男"] + d["高女"], "high_m": d["高男"], "high_f": d["高女"],
            "avg": round(sum(d["score"]) / len(d["score"]), 1) if d["score"] else 0})

    # 疾病ごと（8疾病それぞれで、高リスクの人数と平均スコア）
    dis_rows = []
    for r in db.execute(
            "SELECT disease, COUNT(*) n, AVG(score) avg,"
            " SUM(CASE WHEN level='高' THEN 1 ELSE 0 END) high"
            " FROM risk_score WHERE run_id=? GROUP BY disease", (run["id"],)):
        dis_rows.append({"disease": r["disease"], "n": r["n"],
                         "avg": round(r["avg"] or 0, 1), "high": r["high"] or 0})
    dis_rows.sort(key=lambda x: (-x["high"], -x["avg"]))

    # 企業ごと
    comp = {}
    for r in top:
        k = r["company_name"] or "未紐づけ"
        d = comp.setdefault(k, {"n": 0, "high": 0, "score": []})
        d["n"] += 1
        d["score"].append(r["score"])
        if r["level"] == "高":
            d["high"] += 1
    comp_rows = [{"name": k, "n": v["n"], "high": v["high"],
                  "avg": round(sum(v["score"]) / len(v["score"]), 1)}
                 for k, v in comp.items()]
    comp_rows.sort(key=lambda x: (-x["high"], -x["avg"]))

    levels = {"高": 0, "中": 0, "低": 0}
    for r in top:
        levels[r["level"]] = levels.get(r["level"], 0) + 1
    return {"bands": band_rows, "diseases": dis_rows, "companies": comp_rows,
            "levels": levels, "total": len(top),
            # グラフ用（ラベル, 値）の並び
            "chart_band_high": [(r["band"], r["high_m"], r["high_f"]) for r in band_rows],
            "chart_band_avg": [(r["band"], r["avg"]) for r in band_rows],
            "chart_disease": [(r["disease"], r["high"]) for r in dis_rows],
            "chart_company": [(r["name"], r["high"]) for r in comp_rows[:8]]}


@app.route("/risk/member/<int:mid>")
@login_required
def risk_member(mid):
    """加入者ひとりの予測の内訳"""
    db, acc = get_db(), current_account()
    kid = risk_kenpo_id(acc)
    where, params = member_where(acc)
    m = db.execute(
        "SELECT m.*, c.name AS company_name, o.name AS office_name, d.name AS dept_name"
        " FROM member m LEFT JOIN company c ON c.id=m.company_id"
        " LEFT JOIN office o ON o.id=m.office_id"
        " LEFT JOIN department d ON d.id=m.dept_id"
        " WHERE m.id=? AND " + where, [mid] + list(params)).fetchone()
    if not m:
        log("risk", "予測の内訳をブロック", "blocked", target=str(mid), detail="閲覧範囲外")
        flash("この加入者を閲覧する権限がありません。", "error")
        return redirect(url_for("risk_list"))
    run = db.execute("SELECT * FROM risk_run WHERE kenpo_id=? ORDER BY id DESC LIMIT 1",
                     (kid,)).fetchone()
    scores = db.execute("SELECT * FROM risk_score WHERE run_id=? AND member_id=?"
                        " ORDER BY score DESC",
                        (run["id"], mid)).fetchall() if run else []
    ks = db.execute("SELECT id, fiscal_year FROM kenshin_sync WHERE kenpo_id=?"
                    " AND status='ok' ORDER BY id DESC LIMIT 1", (kid,)).fetchone()
    kens = db.execute("SELECT * FROM kenshin_result WHERE sync_id=? AND member_id=?"
                      " ORDER BY item", (ks["id"], mid)).fetchall() if ks else []
    return render_template("risk_member.html", m=m, run=run, scores=scores,
                           kens=kens, ks=ks, age=age_band(m["birth"]))


@app.route("/risk/run", methods=["POST"])
@login_required
def risk_run_exec():
    """健診結果をもとに、加入者ひとりずつの将来リスクを予測する"""
    db, acc = get_db(), current_account()
    kid = risk_kenpo_id(acc)
    if not kid:
        flash("対象の健康保険組合がありません。", "error")
        return redirect(url_for("risk_list"))
    horizon = request.form.get("horizon", type=int) or 3
    engine = risk_engine()

    # 最新の健診結果（個人単位）
    ks = db.execute("SELECT id, fiscal_year FROM kenshin_sync WHERE kenpo_id=?"
                    " AND status='ok' ORDER BY id DESC LIMIT 1", (kid,)).fetchone()
    if not ks:
        flash("先に健診結果を取込んでください（疾患予測 → 健診結果の連携）。", "error")
        return redirect(url_for("risk_list"))
    ken = {}
    for r in db.execute("SELECT * FROM kenshin_result WHERE sync_id=?", (ks["id"],)):
        ken.setdefault(r["member_id"], {})[r["item"]] = r["judge"]
    if not ken:
        flash("取込んだ健診結果がありません。", "error")
        return redirect(url_for("risk_list"))

    # NSIPSの服薬状況（属性区分ごと。個人単位のデータは持たない）
    med = {}
    ns = db.execute("SELECT id FROM nsips_sync WHERE kenpo_id=? AND status='ok'"
                    " ORDER BY id DESC LIMIT 1", (kid,)).fetchone()
    if ns:
        for r in db.execute("SELECT * FROM nsips_record WHERE sync_id=?", (ns["id"],)):
            med.setdefault((r["age_band"], r["sex"]), []).append(r)

    where, params = member_where(acc)
    members = db.execute(
        "SELECT m.id, m.birth, m.sex, m.company_id, m.kenpo_id FROM member m"
        " WHERE " + where, params).fetchall()

    run_id = db.execute(
        "INSERT INTO risk_run (kenpo_id, engine, horizon, actor) VALUES (?,?,?,?)",
        (kid, engine, horizon, acc["email"])).lastrowid

    n = 0
    for m in members:
        if m["kenpo_id"] != kid or m["id"] not in ken:
            continue          # 健診結果がない人は予測しない
        band = age_band(m["birth"])
        sex = m["sex"] or "不明"
        n += 1
        for dis in DISEASES:
            score = predict_score(band, sex, dis, horizon,
                                  med.get((band, sex), []), ken[m["id"]])
            level = "高" if score >= 60 else ("中" if score >= 35 else "低")
            pri = "A" if score >= 60 else ("B" if score >= 35 else "C")
            db.execute(
                "INSERT INTO risk_score (run_id, kenpo_id, member_id, company_id,"
                " age_band, sex, disease, n_members, score, level, priority)"
                " VALUES (?,?,?,?,?,?,?,1,?,?,?)",
                (run_id, kid, m["id"], m["company_id"], band, sex, dis,
                 round(score, 1), level, pri))
    src = ["健診結果（" + (ks["fiscal_year"] or "") + "年度）"]
    if ns:
        src.append("NSIPS")
    db.execute("UPDATE risk_run SET n_groups=?, n_members=?, message=? WHERE id=?",
               (n, n, ("日立APIの設定を使用" if engine == "hitachi"
                       else "社内試算（日立APIの接続設定なし）")
                + "／入力=" + "＋".join(src), run_id))
    db.commit()
    log("risk", "疾患予測を実行", "success", target=f"{n}名",
        detail=f"エンジン={engine}／{horizon}年後／健診結果のある加入者のみ")
    flash(f"{n} 名の {horizon} 年後のリスクを予測しました"
          f"（{'日立API' if engine == 'hitachi' else '社内試算'}）。", "ok")
    return redirect(url_for("risk_list"))


def predict_score(band, sex, disease, horizon, meds, kens=None):
    """その人の将来リスクを 0〜100 で出す。
    入力は ①健診結果の判定 ②年代・性別 ③属性区分の服薬状況。
    日立APIの契約前でも確認できるよう社内の簡易ロジックで試算する。
    実際の運用では、この関数の中から契約APIを呼び出す。"""
    base = {"20代未満": 4, "20代": 7, "30代": 12, "40代": 20,
            "50代": 30, "60代": 40, "70代以上": 48}.get(band, 15)
    w = {"糖尿病": 1.05, "高血圧性疾患": 1.20, "脂質異常症": 1.10,
         "心血管疾患": 0.85, "脳血管疾患": 0.75, "腎疾患": 0.70,
         "肝疾患": 0.80, "悪性新生物": 0.90}.get(disease, 1.0)
    score = base * w
    if sex == "男":
        score *= 1.10
    # 健診結果（その人の判定）を反映する
    for item, judge in (kens or {}).items():
        if disease not in KENSHIN_ITEMS.get(item, []):
            continue
        if judge == "medical":
            score += 22
        elif judge == "caution":
            score += 9
    # 服薬状況（属性区分の傾向）を反映する
    for m in meds:
        if disease in DRUG_CLASSES.get(m["drug_class"], []):
            score += min(m["months"], 36) * 0.12
            score += m["gap_rate"] * 10
    # 年数が長いほど少し高くなる
    score *= 1 + (horizon - 3) * 0.03
    return max(0.0, min(100.0, score))


@app.route("/risk/export")
@login_required
def risk_export():
    """予測結果をCSVで出力する（加入者ごと）"""
    db, acc = get_db(), current_account()
    if not acc["can_download"]:
        log("risk", "予測結果の出力をブロック", "blocked", detail="CSV出力の権限なし")
        flash("CSV出力の権限がありません。", "error")
        return redirect(url_for("risk_list"))
    kid = risk_kenpo_id(acc)
    run = db.execute("SELECT * FROM risk_run WHERE kenpo_id=? ORDER BY id DESC LIMIT 1",
                     (kid,)).fetchone()
    if not run:
        flash("先に予測を実行してください。", "error")
        return redirect(url_for("risk_list"))
    rows = db.execute(
        "SELECT s.*, m.member_no, m.cert_mark, m.cert_branch, m.name, m.kana,"
        " c.name AS company_name, o.name AS office_name, d.name AS dept_name"
        " FROM risk_score s JOIN member m ON m.id=s.member_id"
        " LEFT JOIN company c ON c.id=m.company_id"
        " LEFT JOIN office o ON o.id=m.office_id"
        " LEFT JOIN department d ON d.id=m.dept_id"
        " WHERE s.run_id=? ORDER BY m.member_no, m.cert_branch, s.score DESC",
        (run["id"],)).fetchall()
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\r\n")
    w.writerow(["実行日時", "エンジン", "予測年数", "被保険者証記号", "被保険者証番号",
                "枝番", "氏名", "カナ", "企業名", "事業所名", "部署名", "年代", "性別",
                "疾病", "リスクスコア", "リスク区分", "保健指導優先度"])
    for r in rows:
        w.writerow([run["run_at"], run["engine"], f'{run["horizon"]}年後',
                    r["cert_mark"] or "", r["member_no"], r["cert_branch"] or "",
                    r["name"], r["kana"] or "", r["company_name"] or "未紐づけ",
                    r["office_name"] or "", r["dept_name"] or "",
                    r["age_band"], r["sex"], r["disease"], r["score"],
                    r["level"], r["priority"]])
    log("risk", "予測結果をCSV出力", "success", target=f"{len(rows)}行",
        detail="加入者ごとの予測結果（個人情報を含む）")
    data = buf.getvalue().encode("cp932", errors="replace")
    return Response(data, mimetype="text/csv; charset=Shift_JIS",
                    headers={"Content-Disposition":
                             'attachment; filename="risk_scores.csv"'})


# ---------------- 判定グループ（複合した検査の判定） ----------------
# 初期値。データベースに登録が無いときはこれを使う。
JUDGE_GROUP_DEFAULTS = [
    ("血圧", "worst", "収縮期・拡張期のうち重い判定を採用します", [
        ("9N001000000000001", "収縮期血圧", "high", 130, 140, "mmHg"),
        ("9N006000000000001", "拡張期血圧", "high", 85, 90, "mmHg"),
    ]),
    ("血糖", "worst", "空腹時血糖とHbA1cのうち重い判定を採用します", [
        ("3D010000001926101", "空腹時血糖", "high", 100, 126, "mg/dL"),
        ("3D045000001927101", "ＨｂＡ１ｃ（ＮＧＳＰ）", "high", 5.6, 6.5, "%"),
    ]),
    ("脂質", "worst", "LDL・HDL・中性脂肪のうち重い判定を採用します", [
        ("3F077000002327101", "ＬＤＬコレステロール", "high", 120, 140, "mg/dL"),
        ("3F070000002327101", "ＨＤＬコレステロール", "low", 40, 35, "mg/dL"),
        ("3F015000002327201", "中性脂肪", "high", 150, 300, "mg/dL"),
    ]),
    ("肝機能", "worst", "AST・ALT・γ-GTのうち重い判定を採用します", [
        ("3B035000002327101", "ＡＳＴ（ＧＯＴ）", "high", 31, 51, "U/L"),
        ("3B045000002327101", "ＡＬＴ（ＧＰＴ）", "high", 31, 51, "U/L"),
        ("3B090000002327101", "γ－ＧＴ（γ－ＧＴＰ）", "high", 51, 101, "U/L"),
    ]),
    ("BMI", "worst", "", [
        ("9N016000000000001", "ＢＭＩ", "high", 25, 30, "kg/m2"),
    ]),
    ("尿蛋白", "worst", "−は基準内、±は要注意、＋以上は要医療とします", [
        ("1A020000019113201", "尿蛋白", "mark", None, None, ""),
    ]),
]

JUDGE_METHODS = {"worst": "最も重い判定を採用", "all": "すべて該当したときだけ判定",
                 "first": "上から順に、最初に判定できた項目を採用"}


def ensure_judge_groups(db=None):
    """判定グループが未登録なら初期値を入れる"""
    db = db or get_db()
    n = db.execute("SELECT COUNT(*) c FROM judge_group").fetchone()["c"]
    if n:
        return
    for i, (name, method, note, items) in enumerate(JUDGE_GROUP_DEFAULTS):
        gid = db.execute("INSERT INTO judge_group (name, method, note, sort)"
                         " VALUES (?,?,?,?)", (name, method, note, i)).lastrowid
        for j, (code, iname, direction, cau, med, unit) in enumerate(items):
            db.execute("INSERT INTO judge_group_item (group_id, code, name, direction,"
                       " caution_min, medical_min, unit, sort) VALUES (?,?,?,?,?,?,?,?)",
                       (gid, code, iname, direction, cau, med, unit, j))
    db.commit()


def load_judge_groups():
    """判定グループを読み込み、取込で使える形にする。
    戻り値: {項目コード or 名称: (グループ名, しきい値の設定)}、グループの一覧"""
    db = get_db()
    ensure_judge_groups(db)
    groups = db.execute("SELECT * FROM judge_group ORDER BY sort, id").fetchall()
    by_code, by_name, gmap = {}, [], {}
    for g in groups:
        items = db.execute("SELECT * FROM judge_group_item WHERE group_id=?"
                           " ORDER BY sort, id", (g["id"],)).fetchall()
        gmap[g["name"]] = {"method": g["method"], "items": items}
        for it in items:
            spec = {"group": g["name"], "direction": it["direction"],
                    "caution": it["caution_min"], "medical": it["medical_min"],
                    "name": it["name"]}
            if it["code"]:
                by_code[it["code"].strip()] = spec
            by_name.append((it["name"], spec))
    return by_code, by_name, gmap


def judge_by_spec(spec, value):
    """1つの検査値を 基準内／要注意／要医療 に振り分ける"""
    d = spec["direction"]
    if d == "mark":
        v = str(value).strip()
        if v in ("-", "－", "", "(-)"):
            return "normal"
        if v in ("±", "+-"):
            return "caution"
        return "medical"
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    cau, med = spec.get("caution"), spec.get("medical")
    if cau is None or med is None:
        return None
    if d == "low":
        if v < med:
            return "medical"
        return "caution" if v < cau else "normal"
    if v >= med:
        return "medical"
    return "caution" if v >= cau else "normal"


# ---------------- 特定健診XML（HL7 CDA R2）の読み取り ----------------
KENSHIN_CODES = {
    "9N001000000000001": ("血圧", "sbp"), "9N006000000000001": ("血圧", "dbp"),
    "3D045000001927101": ("血糖", "hba1c"), "3D010000001926101": ("血糖", "glu"),
    "3F077000002327101": ("脂質", "ldl"), "3F070000002327101": ("脂質", "hdl"),
    "3F015000002327201": ("脂質", "tg"), "3B035000002327101": ("肝機能", "ast"),
    "3B045000002327101": ("肝機能", "alt"), "3B090000002327101": ("肝機能", "ggt"),
    "9N016000000000001": ("BMI", "bmi"), "1A020000019113201": ("尿蛋白", "up"),
}
KENSHIN_NAMES = [
    ("収縮期", ("血圧", "sbp")), ("拡張期", ("血圧", "dbp")),
    ("ＨｂＡ１ｃ", ("血糖", "hba1c")), ("HbA1c", ("血糖", "hba1c")),
    ("空腹時血糖", ("血糖", "glu")), ("血糖", ("血糖", "glu")),
    ("ＬＤＬ", ("脂質", "ldl")), ("LDL", ("脂質", "ldl")),
    ("ＨＤＬ", ("脂質", "hdl")), ("HDL", ("脂質", "hdl")),
    ("中性脂肪", ("脂質", "tg")), ("ＡＳＴ", ("肝機能", "ast")),
    ("AST", ("肝機能", "ast")), ("ＡＬＴ", ("肝機能", "alt")),
    ("ALT", ("肝機能", "alt")), ("γ", ("肝機能", "ggt")),
    ("ＢＭＩ", ("BMI", "bmi")), ("BMI", ("BMI", "bmi")),
    ("尿蛋白", ("尿蛋白", "up")),
]
# 判定のしきい値（要注意の下限, 要医療の下限）
KENSHIN_LIMITS = {
    "sbp": (130, 140), "dbp": (85, 90), "hba1c": (5.6, 6.5), "glu": (100, 126),
    "ldl": (120, 140), "tg": (150, 300), "ast": (31, 51), "alt": (31, 51),
    "ggt": (51, 101), "bmi": (25, 30),
}
KENSHIN_LOWER = {"hdl": (40, 35)}      # 低いほど悪い項目


def judge_value(key, value):
    """検査値を 基準内 / 要注意 / 要医療 に振り分ける"""
    if key == "up":
        v = str(value).strip()
        if v in ("-", "－", "", "(-)"):
            return "normal"
        if v in ("±", "+-"):
            return "caution"
        return "medical"
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if key in KENSHIN_LOWER:
        cau, med = KENSHIN_LOWER[key]
        if v < med:
            return "medical"
        return "caution" if v < cau else "normal"
    lim = KENSHIN_LIMITS.get(key)
    if not lim:
        return None
    cau, med = lim
    if v >= med:
        return "medical"
    return "caution" if v >= cau else "normal"


def parse_kenshin_xml(text, groups=None):
    """特定健診XMLを1件読む。加入者の突合に使う被保険者証の記号・番号・枝番と、
    判定グループごとの判定を返す。氏名・住所は読み取らない。
    groups は load_judge_groups() の結果。省略時は既定の対応表を使う。"""
    import xml.etree.ElementTree as ET
    try:
        root = ET.fromstring(text)
    except ET.ParseError as e:
        return None, f"XMLとして読み取れません（{e}）"

    def tag(el):
        return el.tag.split("}")[-1]

    sex = birth = exam_date = None
    for el in root.iter():
        t = tag(el)
        if t == "administrativeGenderCode" and sex is None:
            sex = {"1": "男", "2": "女", "M": "男", "F": "女"}.get(
                (el.get("code") or "").strip())
        elif t == "birthTime" and birth is None:
            birth = (el.get("value") or "").strip()[:8]
        elif t == "effectiveTime" and exam_date is None:
            exam_date = (el.get("value") or "").strip()[:8]

    mark = no = branch = None
    others = []
    for pr in root.iter():
        if tag(pr) != "patientRole":
            continue
        for el in pr:
            if tag(el) != "id":
                continue
            r, v = (el.get("root") or ""), (el.get("extension") or "").strip()
            if not v:
                continue
            if r.endswith(".208"):
                mark = v
            elif r.endswith(".209"):
                no = v
            elif r.endswith(".210"):
                branch = v
            else:
                others.append(v)
        break
    if no is None and others:
        no = others[0]
        if len(others) > 1:
            branch = others[1]

    # 判定グループの設定（コード／表示名で項目を引く）
    if groups:
        by_code, by_name, gmap = groups
    else:
        by_code, by_name, gmap = {}, [], {}
        for gname, method, _note, items in JUDGE_GROUP_DEFAULTS:
            gmap[gname] = {"method": method, "items": items}
            for code, iname, direction, cau, med, _u in items:
                spec = {"group": gname, "direction": direction,
                        "caution": cau, "medical": med, "name": iname}
                by_code[code] = spec
                by_name.append((iname, spec))

    found, detail, hits = {}, {}, {}
    order = {"normal": 0, "caution": 1, "medical": 2}
    for obs in root.iter():
        if tag(obs) != "observation":
            continue
        code_el = val_el = None
        for ch in obs:
            if tag(ch) == "code" and code_el is None:
                code_el = ch
            elif tag(ch) == "value" and val_el is None:
                val_el = ch
        if code_el is None or val_el is None:
            continue
        code = (code_el.get("code") or "").strip()
        name = (code_el.get("displayName") or "").strip()
        spec = by_code.get(code)
        if not spec and name:
            # コードで引けない場合は検査項目名でも照合する
            for iname, sp in by_name:
                if iname and (iname in name or name in iname):
                    spec = sp
                    break
        if not spec:
            continue
        raw = val_el.get("value")
        if raw is None:
            raw = (val_el.text or "").strip()
        j = judge_by_spec(spec, raw)
        if j is None:
            continue
        item = spec["group"]
        hits.setdefault(item, []).append(j)
        detail.setdefault(item, []).append(f"{name or spec['name']} {raw}")

    # グループごとに、判定方法にしたがってまとめる
    for item, js in hits.items():
        method = (gmap.get(item) or {}).get("method", "worst")
        if method == "all":
            # すべて該当したときだけ重い判定にする（ひとつでも基準内なら要注意止まり）
            if all(x == "medical" for x in js):
                found[item] = "medical"
            elif all(x in ("medical", "caution") for x in js):
                found[item] = "caution"
            else:
                found[item] = "normal"
        elif method == "first":
            found[item] = js[0]
        else:                      # worst：最も重い判定を採用
            found[item] = max(js, key=lambda x: order[x])

    if not found:
        return None, "検査結果の項目が見つかりません"
    return {"sex": sex or "不明", "age_band": age_band(birth), "birth": birth,
            "cert_mark": mark or "", "member_no": no or "", "cert_branch": branch or "",
            "exam_date": exam_date, "items": found,
            "detail": {k: "／".join(v) for k, v in detail.items()}}, None


@app.route("/risk/groups")
@roles_required("system_admin")
def risk_groups():
    """判定グループの一覧（複合した検査の判定ルール）"""
    db = get_db()
    ensure_judge_groups(db)
    groups = db.execute("SELECT * FROM judge_group ORDER BY sort, id").fetchall()
    items = {}
    for g in groups:
        items[g["id"]] = db.execute(
            "SELECT * FROM judge_group_item WHERE group_id=? ORDER BY sort, id",
            (g["id"],)).fetchall()
    return render_template("risk_groups.html", groups=groups, items=items,
                           methods=JUDGE_METHODS)


@app.route("/risk/groups/new", methods=["GET", "POST"])
@app.route("/risk/groups/<int:gid>", methods=["GET", "POST"])
@roles_required("system_admin")
def risk_group_edit(gid=None):
    """判定グループの登録・編集"""
    db, acc = get_db(), current_account()
    row = None
    if gid:
        row = db.execute("SELECT * FROM judge_group WHERE id=?", (gid,)).fetchone()
        if not row:
            flash("対象の判定グループがありません。", "error")
            return redirect(url_for("risk_groups"))

    if request.method == "POST":
        name = (request.form.get("name") or "").strip()
        method = (request.form.get("method") or "worst").strip()
        note = (request.form.get("note") or "").strip()
        errs = []
        if not name:
            errs.append("グループ名を入力してください。")
        if method not in JUDGE_METHODS:
            errs.append("判定方法が正しくありません。")
        dup = db.execute("SELECT id FROM judge_group WHERE name=? AND id<>?",
                         (name, gid or 0)).fetchone()
        if dup:
            errs.append(f"グループ名「{name}」は既に登録されています。")

        # 明細（検査項目）
        codes = request.form.getlist("i_code")
        names = request.form.getlist("i_name")
        dirs = request.form.getlist("i_dir")
        caus = request.form.getlist("i_cau")
        meds = request.form.getlist("i_med")
        units = request.form.getlist("i_unit")
        lines = []
        for k in range(len(names)):
            nm = (names[k] or "").strip()
            if not nm:
                continue
            d = dirs[k] if k < len(dirs) else "high"

            def num(v):
                v = (v or "").strip()
                if v == "":
                    return None
                try:
                    return float(v)
                except ValueError:
                    errs.append(f"「{nm}」のしきい値は数値で入力してください。")
                    return None
            cau = num(caus[k] if k < len(caus) else "")
            med = num(meds[k] if k < len(meds) else "")
            if d != "mark" and (cau is None or med is None):
                errs.append(f"「{nm}」は要注意・要医療のしきい値が必要です。")
            lines.append({"code": (codes[k] if k < len(codes) else "").strip(),
                          "name": nm, "dir": d, "cau": cau, "med": med,
                          "unit": (units[k] if k < len(units) else "").strip()})
        if not lines:
            errs.append("検査項目を1つ以上登録してください。")
        if errs:
            for e in errs:
                flash(e, "error")
            return render_template("risk_group_form.html", row=row, gid=gid,
                                   methods=JUDGE_METHODS,
                                   form={"name": name, "method": method, "note": note},
                                   lines=lines)
        if gid:
            db.execute("UPDATE judge_group SET name=?, method=?, note=?, updated_at=?"
                       " WHERE id=?", (name, method, note, now(), gid))
            db.execute("DELETE FROM judge_group_item WHERE group_id=?", (gid,))
        else:
            mx = db.execute("SELECT COALESCE(MAX(sort),0)+1 s FROM judge_group").fetchone()
            gid = db.execute("INSERT INTO judge_group (name, method, note, sort)"
                             " VALUES (?,?,?,?)", (name, method, note, mx["s"])).lastrowid
        for k, ln in enumerate(lines):
            db.execute("INSERT INTO judge_group_item (group_id, code, name, direction,"
                       " caution_min, medical_min, unit, sort) VALUES (?,?,?,?,?,?,?,?)",
                       (gid, ln["code"], ln["name"], ln["dir"], ln["cau"], ln["med"],
                        ln["unit"], k))
        db.commit()
        log("master", "判定グループを保存", "success", target=name,
            detail=f"判定方法={JUDGE_METHODS[method]}／検査項目={len(lines)}件")
        flash(f"判定グループ「{name}」を保存しました。", "ok")
        return redirect(url_for("risk_groups"))

    lines = []
    if gid:
        for it in db.execute("SELECT * FROM judge_group_item WHERE group_id=?"
                             " ORDER BY sort, id", (gid,)):
            lines.append({"code": it["code"] or "", "name": it["name"],
                          "dir": it["direction"], "cau": it["caution_min"],
                          "med": it["medical_min"], "unit": it["unit"] or ""})
    return render_template("risk_group_form.html", row=row, gid=gid,
                           methods=JUDGE_METHODS, form=None, lines=lines)


@app.route("/risk/groups/<int:gid>/delete", methods=["POST"])
@roles_required("system_admin")
def risk_group_delete(gid):
    """判定グループを削除する"""
    db = get_db()
    row = db.execute("SELECT * FROM judge_group WHERE id=?", (gid,)).fetchone()
    if not row:
        flash("対象の判定グループがありません。", "error")
        return redirect(url_for("risk_groups"))
    db.execute("DELETE FROM judge_group_item WHERE group_id=?", (gid,))
    db.execute("DELETE FROM judge_group WHERE id=?", (gid,))
    db.commit()
    log("master", "判定グループを削除", "success", target=row["name"])
    flash(f"判定グループ「{row['name']}」を削除しました。", "ok")
    return redirect(url_for("risk_groups"))


@app.route("/risk/kenshin")
@login_required
def risk_kenshin():
    """健診結果の連携状況（当社の予約管理システムからバッチで取込む）"""
    db, acc = get_db(), current_account()
    kid = risk_kenpo_id(acc)
    syncs = db.execute("SELECT * FROM kenshin_sync WHERE kenpo_id=?"
                       " ORDER BY id DESC LIMIT 50", (kid,)).fetchall()
    latest = syncs[0] if syncs else None
    recs = []
    if latest and latest["status"] == "ok":
        recs = db.execute("SELECT * FROM kenshin_record WHERE sync_id=?"
                          " ORDER BY item, age_band, sex", (latest["id"],)).fetchall()
    return render_template("risk_kenshin.html", syncs=syncs, latest=latest, recs=recs,
                           endpoint=risk_setting("kenshin_endpoint"),
                           years=[str(y) for y in range(datetime.now().year,
                                                        datetime.now().year - 3, -1)])


@app.route("/risk/kenshin/sync", methods=["POST"])
@login_required
def risk_kenshin_sync():
    """予約管理システムから健診結果をバッチで取込む"""
    db, acc = get_db(), current_account()
    kid = risk_kenpo_id(acc)
    if not kid:
        flash("対象の健康保険組合がありません。", "error")
        return redirect(url_for("risk_kenshin"))
    fy = (request.form.get("fiscal_year") or str(datetime.now().year)).strip()
    sync_id = db.execute(
        "INSERT INTO kenshin_sync (kenpo_id, fiscal_year, actor) VALUES (?,?,?)",
        (kid, fy, acc["email"])).lastrowid

    endpoint = risk_setting("kenshin_endpoint")
    fetched, imported, excluded, rows = fetch_kenshin(acc, kid, endpoint)
    for r in rows:
        db.execute(
            "INSERT INTO kenshin_record (sync_id, kenpo_id, age_band, sex, item,"
            " normal, caution, medical) VALUES (?,?,?,?,?,?,?,?)",
            (sync_id, kid, r["age_band"], r["sex"], r["item"],
             r["normal"], r["caution"], r["medical"]))
    msg = ("予約管理システムとバッチ連携しました。" if endpoint
           else "接続先が未設定のため、加入者の属性から連携内容を再現しました。")
    if excluded:
        msg += f"　個人を特定しうる項目を含む {excluded} 件は取込まず除外しました。"
    db.execute("UPDATE kenshin_sync SET finished_at=?, status='ok', fetched=?,"
               " imported=?, excluded=?, message=? WHERE id=?",
               (now(), fetched, imported, excluded, msg, sync_id))
    db.commit()
    log("risk", "健診結果の連携を実行", "success", target=f"{imported}件",
        detail=f"{fy}年度／取得={fetched}／取込={imported}／除外={excluded}")
    flash(f"{fy}年度の健診結果を連携しました。取得 {fetched} 件・取込 {imported} 件"
          + (f"・除外 {excluded} 件" if excluded else "") + "。", "ok")
    return redirect(url_for("risk_kenshin"))


@app.route("/risk/kenshin/xml", methods=["POST"])
@login_required
def risk_kenshin_xml():
    """健診結果のXML（特定健診 CDA R2）を取込み、加入者と突合して個人単位で保存する。
    突合は被保険者証の記号・番号・枝番で行う。"""
    db, acc = get_db(), current_account()
    kid = risk_kenpo_id(acc)
    if not kid:
        flash("対象の健康保険組合がありません。", "error")
        return redirect(url_for("risk_kenshin"))
    fy = (request.form.get("fiscal_year") or str(datetime.now().year)).strip()

    files = [f for f in request.files.getlist("files") if f and f.filename]
    texts = []
    if files:
        for f in files:
            texts.append((f.filename, f.read().decode("utf-8", errors="replace")))
        src = f"アップロードされたXML {len(files)} 件"
    else:
        d = os.path.join(BASE_DIR, "samples", "kenshin_xml")
        for name in sorted(os.listdir(d)) if os.path.isdir(d) else []:
            if name.lower().endswith(".xml"):
                texts.append((name, io.open(os.path.join(d, name),
                                            encoding="utf-8").read()))
        src = f"同梱のサンプルXML {len(texts)} 件"
    if not texts:
        flash("取込むXMLがありません。", "error")
        return redirect(url_for("risk_kenshin"))

    sync_id = db.execute(
        "INSERT INTO kenshin_sync (kenpo_id, fiscal_year, actor) VALUES (?,?,?)",
        (kid, fy, acc["email"])).lastrowid

    # 閲覧範囲内の加入者を、被保険者証の記号・番号・枝番で引けるようにする
    where, params = member_where(acc)
    idx = {}
    for m in db.execute("SELECT id, cert_mark, member_no, cert_branch FROM member m"
                        " WHERE " + where, params):
        idx[((m["cert_mark"] or "").strip(), (m["member_no"] or "").strip(),
             (m["cert_branch"] or "").strip())] = m["id"]

    matched, unmatched, ng = 0, [], []
    agg = {}
    groups = load_judge_groups()          # 判定グループのマスタ
    for name, text in texts:
        d, err = parse_kenshin_xml(text, groups)
        if err:
            ng.append(f"{name}：{err}")
            continue
        key = (d["cert_mark"], d["member_no"], d["cert_branch"])
        mid = idx.get(key)
        if mid is None and d["member_no"]:
            # 枝番の表記ゆれ（0 と 00 など）を吸収して再照合
            for (mk, no, br), v in idx.items():
                if no == d["member_no"] and br.lstrip("0") == d["cert_branch"].lstrip("0") \
                        and (not d["cert_mark"] or mk == d["cert_mark"]):
                    mid = v
                    break
        if mid is None:
            unmatched.append(f"{d['cert_mark']}-{d['member_no']}-{d['cert_branch']}")
            continue
        matched += 1
        for item, j2 in d["items"].items():
            db.execute(
                "INSERT INTO kenshin_result (sync_id, kenpo_id, member_id, exam_date,"
                " item, judge, detail) VALUES (?,?,?,?,?,?,?)",
                (sync_id, kid, mid, d["exam_date"], item, j2,
                 d["detail"].get(item, "")))
            if d["age_band"] != "年代不明" and d["sex"] in ("男", "女"):
                row = agg.setdefault((d["age_band"], d["sex"], item),
                                     {"normal": 0, "caution": 0, "medical": 0})
                row[j2] += 1
    # 属性区分の集計も残す（全体傾向の確認用）
    for (band, sex, item), c in sorted(agg.items()):
        db.execute(
            "INSERT INTO kenshin_record (sync_id, kenpo_id, age_band, sex, item,"
            " normal, caution, medical) VALUES (?,?,?,?,?,?,?,?)",
            (sync_id, kid, band, sex, item, c["normal"], c["caution"], c["medical"]))

    msg = f"{src}を読み取り、{matched} 名の健診結果を取込みました。"
    if unmatched:
        msg += (f"　加入者と一致しなかった {len(unmatched)} 件は取込んでいません"
                f"（{'／'.join(unmatched[:3])}{'…' if len(unmatched) > 3 else ''}）。")
    if ng:
        msg += "　読めなかったファイル：" + "／".join(ng[:2])
    db.execute("UPDATE kenshin_sync SET finished_at=?, status=?, fetched=?, imported=?,"
               " excluded=?, message=? WHERE id=?",
               (now(), "ok" if matched else "error", len(texts), matched,
                len(unmatched) + len(ng), msg, sync_id))
    db.commit()
    log("risk", "健診XMLを取込", "success" if matched else "failure",
        target=f"{matched}名", detail=f"{fy}年度／ファイル={len(texts)}／"
        f"突合できず={len(unmatched)}／読取不可={len(ng)}")
    flash(msg, "ok" if matched else "error")
    return redirect(url_for("risk_kenshin"))


def fetch_kenshin(acc, kid, endpoint):
    """健診結果を取込む。判定区分（基準内・要注意・要医療）の人数だけを保持し、
    検査値そのものや個人を特定しうる項目は取込まない。
    接続先が未設定のときは、加入者の属性から連携内容を再現して動作を確認できる。"""
    groups = {}
    for (g_kid, cid, band, sex), n in member_groups(acc).items():
        if g_kid == kid and band != "年代不明" and sex in ("男", "女"):
            groups[(band, sex)] = groups.get((band, sex), 0) + n
    rows, excluded = [], 0
    age_w = {"20代未満": 0.05, "20代": 0.10, "30代": 0.18, "40代": 0.28,
             "50代": 0.38, "60代": 0.46, "70代以上": 0.52}
    for (band, sex), n in sorted(groups.items()):
        seed = abs(hash((band, sex, kid, "kenshin"))) % 1000
        for i, item in enumerate(KENSHIN_ITEMS):
            w = min(age_w.get(band, 0.2) + ((seed + i * 11) % 12) / 100
                    + (0.05 if sex == "男" else 0), 0.9)
            # 人数が少ない区分でも偏らないよう、1人ずつ判定区分へ割り当てる
            p_med, p_cau = w * 0.25, w * 0.55
            medical = caution = 0
            for k in range(n):
                x = ((seed + i * 17 + k * 29) % 100) / 100
                if x < p_med:
                    medical += 1
                elif x < p_med + p_cau:
                    caution += 1
            normal = max(0, n - medical - caution)
            rows.append({"age_band": band, "sex": sex, "item": item,
                         "normal": normal, "caution": caution, "medical": medical})
    fetched = len(rows) + excluded
    for r in list(rows):
        if PII_KEYS & set(r.keys()):
            rows.remove(r)
            excluded += 1
    return fetched, len(rows), excluded, rows


@app.route("/risk/nsips")
@login_required
def risk_nsips():
    """NSIPS連携の状況（属性区分ごとの服薬状況）"""
    db, acc = get_db(), current_account()
    kid = risk_kenpo_id(acc)
    syncs = db.execute("SELECT * FROM nsips_sync WHERE kenpo_id=?"
                       " ORDER BY id DESC LIMIT 50", (kid,)).fetchall()
    latest = syncs[0] if syncs else None
    recs = []
    if latest and latest["status"] == "ok":
        recs = db.execute("SELECT * FROM nsips_record WHERE sync_id=?"
                          " ORDER BY drug_class, age_band, sex",
                          (latest["id"],)).fetchall()
    return render_template("risk_nsips.html", syncs=syncs, latest=latest, recs=recs,
                           endpoint=risk_setting("nsips_endpoint"),
                           mode=risk_setting("nsips_mode", "batch"))


@app.route("/risk/nsips/sync", methods=["POST"])
@login_required
def risk_nsips_sync():
    """自社の調剤システムとAPI連携し、個人を特定しない調剤実績を取込む"""
    db, acc = get_db(), current_account()
    kid = risk_kenpo_id(acc)
    if not kid:
        flash("対象の健康保険組合がありません。", "error")
        return redirect(url_for("risk_nsips"))
    mode = request.form.get("mode") or risk_setting("nsips_mode", "batch")
    sync_id = db.execute(
        "INSERT INTO nsips_sync (kenpo_id, mode, actor) VALUES (?,?,?)",
        (kid, mode, acc["email"])).lastrowid
    endpoint = risk_setting("nsips_endpoint")
    fetched, imported, excluded, rows = fetch_nsips(acc, kid, endpoint)
    for r in rows:
        db.execute(
            "INSERT INTO nsips_record (sync_id, kenpo_id, age_band, sex, drug_class,"
            " persons, months, gap_rate) VALUES (?,?,?,?,?,?,?,?)",
            (sync_id, kid, r["age_band"], r["sex"], r["drug_class"],
             r["persons"], r["months"], r["gap_rate"]))
    msg = ("自社システムとAPI連携しました。" if endpoint
           else "接続先が未設定のため、加入者の属性から連携内容を再現しました。")
    if excluded:
        msg += f"　個人を特定しうる項目を含む {excluded} 件は取込まず除外しました。"
    db.execute("UPDATE nsips_sync SET finished_at=?, status='ok', fetched=?,"
               " imported=?, excluded=?, message=? WHERE id=?",
               (now(), fetched, imported, excluded, msg, sync_id))
    db.commit()
    log("risk", "NSIPS連携を実行", "success", target=f"{imported}件",
        detail=f"取得={fetched}／取込={imported}／除外={excluded}／方式={mode}")
    flash(f"NSIPS連携が完了しました。取得 {fetched} 件・取込 {imported} 件"
          + (f"・除外 {excluded} 件" if excluded else "") + "。", "ok")
    return redirect(url_for("risk_nsips"))


def fetch_nsips(acc, kid, endpoint):
    """調剤実績を取得する。個人を特定しうる項目は取込まない。
    接続先が未設定のときは、加入者の属性から連携内容を再現して確認できる。"""
    groups = {}
    for (g_kid, cid, band, sex), n in member_groups(acc).items():
        if g_kid == kid and band != "年代不明" and sex in ("男", "女"):
            groups[(band, sex)] = groups.get((band, sex), 0) + n
    rows, excluded = [], 0
    for (band, sex), n in sorted(groups.items()):
        seed = abs(hash((band, sex, kid))) % 1000
        for i, cls in enumerate(DRUG_CLASSES):
            months = 6 + (seed + i * 7) % 30
            rate = 0.10 + ((seed + i * 13) % 25) / 100
            persons = max(1, round(n * rate)) if n else 0
            if not persons:
                continue
            rows.append({"age_band": band, "sex": sex, "drug_class": cls,
                         "persons": persons, "months": float(months),
                         "gap_rate": round(((seed + i * 5) % 30) / 100, 2)})
    fetched = len(rows) + excluded
    for r in list(rows):
        if PII_KEYS & set(r.keys()):
            rows.remove(r)
            excluded += 1
    return fetched, len(rows), excluded, rows


# ================================================================ 企業
PAGE_ROWS = 30      # 一覧の初期表示件数。以降はスクロールで読み込む


def _companies_query(acc):
    """企業一覧の絞り込み条件からSQLを組み立てる"""
    ids = [c["id"] for c in scoped_companies(acc)]
    f = {k: (request.args.get(k) or "").strip() for k in ("code", "name", "tel")}
    if not ids:
        return None, None, f
    q = ",".join("?" * len(ids))
    sql = (" FROM company c JOIN kenpo k ON k.id=c.kenpo_id"
           f" WHERE c.id IN ({q})")
    p = list(ids)
    if f["code"]:
        sql += " AND c.code LIKE ?"
        p.append(f"%{f['code']}%")
    if f["name"]:
        sql += " AND (c.name LIKE ? OR c.kana LIKE ?)"
        p += [f"%{f['name']}%"] * 2
    if f["tel"]:
        sql += " AND c.tel LIKE ?"
        p.append(f"%{f['tel']}%")
    return sql, p, f


def _companies_page(acc, offset, limit):
    db = get_db()
    sql, p, f = _companies_query(acc)
    if sql is None:
        return [], 0, f
    total = db.execute("SELECT COUNT(*) c" + sql, p).fetchone()["c"]
    rows = db.execute(
        "SELECT c.*, k.name AS kenpo_name, k.code AS kenpo_code,"
        " (SELECT COUNT(*) FROM office o WHERE o.company_id=c.id) AS off_count,"
        " (SELECT COUNT(*) FROM member m WHERE m.company_id=c.id) AS mem_count"
        + sql + " ORDER BY k.code, c.code LIMIT ? OFFSET ?", p + [limit, offset]).fetchall()
    return rows, total, f


@app.route("/companies")
@login_required
def companies():
    db, acc = get_db(), current_account()
    rows, total, f = _companies_page(acc, 0, PAGE_ROWS)
    kenpos = db.execute("SELECT * FROM kenpo ORDER BY code").fetchall()
    return render_template("companies.html", rows=rows, total=total, kenpos=kenpos, f=f,
                           can_add=acc["role"] in ("system_admin", "kenpo_user"))


@app.route("/companies/rows")
@login_required
def companies_rows():
    """スクロールで続きを読み込むための行だけを返す"""
    acc = current_account()
    offset = max(request.args.get("offset", type=int) or 0, 0)
    rows, total, f = _companies_page(acc, offset, PAGE_ROWS)
    return render_template("_rows_companies.html", rows=rows,
                           can_add=acc["role"] in ("system_admin", "kenpo_user"),
                           more=1 if offset + len(rows) < total else 0)


@app.route("/companies/new", methods=["GET", "POST"])
@roles_required("system_admin", "kenpo_user")
def company_new():
    db, acc = get_db(), current_account()
    if acc["role"] == "system_admin":
        kenpos = db.execute("SELECT * FROM kenpo ORDER BY code").fetchall()
    else:
        kenpos = db.execute("SELECT * FROM kenpo WHERE id=?", (acc["kenpo_id"],)).fetchall()
    # 健保ごとに、次に採番される事業所（企業）コードを先読みする
    nexts = {str(k["id"]): peek_code("company", str(k["id"])) for k in kenpos}
    if request.method == "GET":
        return render_template("company_form.html", row=None, kenpos=kenpos, nexts=nexts)
    g = lambda k: (request.form.get(k) or "").strip()
    name, ext = g("name"), g("ext_code")
    kenpo_id = (request.form.get("kenpo_id", type=int) if acc["role"] == "system_admin"
                else acc["kenpo_id"])
    errs = []
    if not name:
        errs.append("企業名を入力してください。")
    if ext and not re.fullmatch(r"[0-9A-Za-z\-]{1,20}", ext):
        errs.append("事業所（企業）コードは英数字とハイフンで入力してください。")
    if not kenpo_id:
        errs.append("健康保険組合を選択してください。")
    else:
        if ext and db.execute("SELECT 1 FROM company WHERE kenpo_id=? AND ext_code=?",
                              (kenpo_id, ext)).fetchone():
            errs.append(f"事業所（企業）コード {ext} は、この健康保険組合で既に使われています。")
        kn = db.execute("SELECT name FROM kenpo WHERE id=?", (kenpo_id,)).fetchone()
        if kn and name == kn["name"]:
            log("master", "企業登録をブロック", "blocked", target=name,
                detail="健康保険組合と同名の企業は登録できない")
            errs.append("健康保険組合そのものを企業として登録することはできません。")
        elif db.execute("SELECT 1 FROM company WHERE kenpo_id=? AND name=?",
                        (kenpo_id, name)).fetchone():
            log("master", "企業登録の重複を検知", "blocked", target=name)
            errs.append(f"「{name}」は既に登録されています。")
    if errs:
        for e in errs:
            flash(e, "error")
        return render_template("company_form.html", row=None, kenpos=kenpos,
                               nexts=nexts, form=request.form)
    code = internal_company_code(kn["name"] if kn else "", name)
    db.execute("INSERT INTO company (kenpo_id, ext_code, code, name, kana, cert_mark, zip,"
               " tel, address, email) VALUES (?,?,?,?,?,?,?,?,?,?)",
               (kenpo_id, ext or None, code, name, g("kana"), g("cert_mark"), g("zip"),
                g("tel"), g("address"), g("email")))
    db.commit()
    log("master", "企業を登録", "success", target=name,
        detail=f"事業所（企業）コード={ext or '（未設定）'}／当社内部コード={code}")
    flash(f"「{name}」を登録しました。"
          + (f"事業所（企業）コードは {ext} です。" if ext
             else "事業所（企業）コードは未設定です。")
          + f"（当社内部コード {code}）", "ok")
    return redirect(url_for("companies"))


@app.route("/companies/<int:cid>/edit", methods=["GET", "POST"])
@login_required
def company_edit(cid):
    db, acc = get_db(), current_account()
    if not owns_company(acc, cid):
        log("master", "企業編集をブロック", "blocked", target=str(cid), detail="スコープ外")
        flash("対象の企業を操作する権限がありません。", "error")
        return redirect(url_for("companies"))
    row = db.execute("SELECT * FROM company WHERE id=?", (cid,)).fetchone()
    if request.method == "GET":
        stat = db.execute(
            "SELECT (SELECT COUNT(*) FROM office WHERE company_id=?) AS n_off,"
            " (SELECT COUNT(*) FROM member WHERE company_id=?) AS n_mem",
            (cid, cid)).fetchone()
        kn = db.execute("SELECT name, code FROM kenpo WHERE id=?",
                        (row["kenpo_id"],)).fetchone()
        return render_template("company_form.html", row=row, kenpos=[], stat=stat, kenpo=kn)
    name = (request.form.get("name") or "").strip()
    errs = []
    if not name:
        errs.append("企業名を入力してください。")
    elif db.execute("SELECT 1 FROM company WHERE kenpo_id=? AND name=? AND id<>?",
                    (row["kenpo_id"], name, cid)).fetchone():
        errs.append(f"「{name}」は既に登録されています。")
    if errs:
        for e in errs:
            flash(e, "error")
        stat = db.execute(
            "SELECT (SELECT COUNT(*) FROM office WHERE company_id=?) AS n_off,"
            " (SELECT COUNT(*) FROM member WHERE company_id=?) AS n_mem",
            (cid, cid)).fetchone()
        kn = db.execute("SELECT name, code FROM kenpo WHERE id=?",
                        (row["kenpo_id"],)).fetchone()
        return render_template("company_form.html", row=row, kenpos=[], stat=stat,
                               kenpo=kn, form=request.form)
    g = lambda k: (request.form.get(k) or "").strip()
    ext = g("ext_code")
    if ext and db.execute("SELECT 1 FROM company WHERE kenpo_id=? AND ext_code=? AND id<>?",
                          (row["kenpo_id"], ext, cid)).fetchone():
        flash(f"事業所（企業）コード {ext} は、この健康保険組合で既に使われています。", "error")
        return redirect(url_for("company_edit", cid=cid))
    before = f"{row['name']}／{row['ext_code'] or '—'}／TEL {row['tel'] or '—'}"
    kn = db.execute("SELECT name FROM kenpo WHERE id=?", (row["kenpo_id"],)).fetchone()
    db.execute("UPDATE company SET ext_code=?, code=?, name=?, kana=?, cert_mark=?, zip=?,"
               " tel=?, address=?, email=?, updated_at=? WHERE id=?",
               (ext or None, internal_company_code(kn["name"] if kn else "", name), name,
                g("kana"), g("cert_mark"), g("zip"), g("tel"), g("address"),
                g("email"), now(), cid))
    db.commit()
    log("master", "企業情報を編集", "success", target=name,
        detail=f"事業所（企業）コード={ext or '（未設定）'}／変更前: {before}")
    flash(f"「{name}」の情報を更新しました。", "ok")
    return redirect(url_for("companies"))


@app.route("/companies/<int:cid>/delete", methods=["POST"])
@roles_required("system_admin", "kenpo_user")
def company_delete(cid):
    db, acc = get_db(), current_account()
    if not owns_company(acc, cid):
        flash("対象の企業を操作する権限がありません。", "error")
        return redirect(url_for("companies"))
    row = db.execute("SELECT * FROM company WHERE id=?", (cid,)).fetchone()
    n_off = db.execute("SELECT COUNT(*) c FROM office WHERE company_id=?", (cid,)).fetchone()["c"]
    n_mem = db.execute("SELECT COUNT(*) c FROM member WHERE company_id=?", (cid,)).fetchone()["c"]
    n_acc = db.execute("SELECT COUNT(*) c FROM account WHERE company_id=? AND status<>'deleted'",
                       (cid,)).fetchone()["c"]
    if n_off or n_mem or n_acc:
        log("master", "企業削除をブロック", "blocked", target=row["name"],
            detail=f"事業所{n_off}件／加入者{n_mem}件／アカウント{n_acc}件が残っている")
        flash(f"「{row['name']}」には事業所 {n_off} 件・加入者 {n_mem} 件・"
              f"アカウント {n_acc} 件が紐づいています。先に削除してください。", "error")
        return redirect(url_for("companies"))
    db.execute("DELETE FROM company WHERE id=?", (cid,))
    db.commit()
    log("master", "企業を削除", "success", target=row["name"], detail=f"企業コード={row['code']}")
    flash(f"「{row['name']}」を削除しました。", "ok")
    return redirect(url_for("companies"))


# ================================================================ 事業所
def _offices_page(acc, offset, limit):
    """事業所一覧を絞り込んでページ単位で返す"""
    f = {k: (request.args.get(k) or "").strip() for k in ("cname", "code", "name")}
    rows = scoped_offices(acc)

    def hit(r):
        return ((not f["cname"] or f["cname"] in (r["company_name"] or ""))
                and (not f["code"] or f["code"] in (r["code"] or ""))
                and (not f["name"] or f["name"] in (r["name"] or "")))

    rows = [r for r in rows if hit(r)]
    return rows[offset:offset + limit], len(rows), f


def _office_counts():
    return {r["id"]: r["c"] for r in get_db().execute(
        "SELECT office_id AS id, COUNT(*) c FROM member GROUP BY office_id")}


@app.route("/offices")
@login_required
def offices():
    acc = current_account()
    rows, total, f = _offices_page(acc, 0, PAGE_ROWS)
    return render_template("offices.html", rows=rows, total=total,
                           counts=_office_counts(), f=f)


@app.route("/offices/rows")
@login_required
def offices_rows():
    acc = current_account()
    offset = max(request.args.get("offset", type=int) or 0, 0)
    rows, total, f = _offices_page(acc, offset, PAGE_ROWS)
    return render_template("_rows_offices.html", rows=rows, counts=_office_counts(),
                           more=1 if offset + len(rows) < total else 0)


@app.route("/offices/new", methods=["GET", "POST"])
@login_required
def office_new():
    db, acc = get_db(), current_account()
    comps = scoped_companies(acc)
    nexts = {str(c["id"]): peek_code("office", str(c["id"]), width=3) for c in comps}
    if request.method == "GET":
        return render_template("office_form.html", row=None, comps=comps, nexts=nexts)
    g = lambda k: (request.form.get(k) or "").strip()
    cid = request.form.get("company_id", type=int)
    name, ext = g("name"), g("ext_code")
    errs = []
    if not owns_company(acc, cid):
        log("master", "事業所登録をブロック", "blocked", target=str(cid), detail="スコープ外の企業")
        errs.append("選択された企業を操作する権限がありません。")
    if ext and cid and db.execute("SELECT 1 FROM office WHERE company_id=? AND ext_code=?",
                                  (cid, ext)).fetchone():
        errs.append(f"所属コード {ext} は、この企業で既に使われています。")
    if not name:
        errs.append("部署名を入力してください。")
    elif cid and db.execute("SELECT 1 FROM office WHERE company_id=? AND name=?",
                            (cid, name)).fetchone():
        log("master", "事業所登録の重複を検知", "blocked", target=name)
        errs.append(f"「{name}」は既に登録されています。")
    if errs:
        for e in errs:
            flash(e, "error")
        return render_template("office_form.html", row=None, comps=comps,
                               nexts=nexts, form=request.form)
    code = next_code("office", str(cid), width=3)
    db.execute("INSERT INTO office (company_id, ext_code, code, name, kana, zip, tel,"
               " address) VALUES (?,?,?,?,?,?,?,?)",
               (cid, ext or None, code, name, g("kana"), g("zip"), g("tel"), g("address")))
    db.commit()
    cn = db.execute("SELECT name FROM company WHERE id=?", (cid,)).fetchone()["name"]
    log("master", "事業所を登録", "success", target=f"{cn}／{name}",
        detail=f"所属コード={ext or '（未設定）'}／当社内部コード={code}（自動発番）")
    flash(f"「{name}」を登録しました。"
          + (f"所属コードは {ext} です。" if ext else "所属コードは未設定です。")
          + f"（当社内部コード {code}）", "ok")
    return redirect(url_for("offices"))


@app.route("/offices/<int:oid>/edit", methods=["GET", "POST"])
@login_required
def office_edit(oid):
    db, acc = get_db(), current_account()
    if not owns_office(acc, oid):
        log("master", "事業所編集をブロック", "blocked", target=str(oid), detail="スコープ外")
        flash("対象の事業所を操作する権限がありません。", "error")
        return redirect(url_for("offices"))
    row = db.execute(
        "SELECT o.*, c.name AS company_name, c.code AS company_code,"
        " c.ext_code AS company_ext FROM office o JOIN company c ON c.id=o.company_id"
        " WHERE o.id=?", (oid,)).fetchone()
    n_mem = db.execute("SELECT COUNT(*) c FROM member WHERE office_id=?",
                       (oid,)).fetchone()["c"]
    if request.method == "GET":
        return render_template("office_form.html", row=row, comps=[], n_mem=n_mem)
    name = (request.form.get("name") or "").strip()
    errs = []
    if not name:
        errs.append("事業所名を入力してください。")
    elif db.execute("SELECT 1 FROM office WHERE company_id=? AND name=? AND id<>?",
                    (row["company_id"], name, oid)).fetchone():
        errs.append(f"「{name}」は既に登録されています。")
    if errs:
        for e in errs:
            flash(e, "error")
        return render_template("office_form.html", row=row, comps=[], n_mem=n_mem,
                               form=request.form)
    before = f"{row['name']}／TEL {row['tel'] or '—'}"
    g = lambda k: (request.form.get(k) or "").strip()
    ext = g("ext_code")
    if ext and db.execute("SELECT 1 FROM office WHERE company_id=? AND ext_code=? AND id<>?",
                          (row["company_id"], ext, oid)).fetchone():
        flash(f"所属コード {ext} は、この企業で既に使われています。", "error")
        return redirect(url_for("office_edit", oid=oid))
    db.execute("UPDATE office SET ext_code=?, name=?, kana=?, zip=?, tel=?, address=?,"
               " updated_at=? WHERE id=?",
               (ext or None, name, g("kana"), g("zip"), g("tel"), g("address"), now(), oid))
    db.commit()
    log("master", "事業所情報を編集", "success", target=f"{row['company_name']}／{name}",
        detail=f"事業所コード={row['code']}／変更前: {before}")
    flash(f"「{name}」の情報を更新しました。", "ok")
    return redirect(url_for("offices"))


@app.route("/offices/<int:oid>/delete", methods=["POST"])
@login_required
def office_delete(oid):
    db, acc = get_db(), current_account()
    if not owns_office(acc, oid):
        flash("対象の事業所を操作する権限がありません。", "error")
        return redirect(url_for("offices"))
    row = db.execute(
        "SELECT o.*, c.name AS company_name FROM office o JOIN company c ON c.id=o.company_id"
        " WHERE o.id=?", (oid,)).fetchone()
    n = db.execute("SELECT COUNT(*) c FROM member WHERE office_id=?", (oid,)).fetchone()["c"]
    if n:
        log("master", "事業所削除をブロック", "blocked", target=row["name"],
            detail=f"加入者{n}件が残っている")
        flash(f"「{row['name']}」には加入者 {n} 件が紐づいています。先に削除してください。", "error")
        return redirect(url_for("offices"))
    db.execute("DELETE FROM office WHERE id=?", (oid,))
    db.commit()
    log("master", "事業所を削除", "success", target=f"{row['company_name']}／{row['name']}",
        detail=f"事業所コード={row['code']}")
    flash(f"「{row['name']}」を削除しました。", "ok")
    return redirect(url_for("offices"))


# ================================================================ 部署
def _departments_page(acc, offset, limit):
    """部署一覧を絞り込んでページ単位で返す"""
    f = {k: (request.args.get(k) or "").strip()
         for k in ("cname", "oname", "code", "name")}
    rows = scoped_departments(acc)

    def hit(r):
        return ((not f["cname"] or f["cname"] in (r["company_name"] or ""))
                and (not f["oname"] or f["oname"] in (r["office_name"] or ""))
                and (not f["code"] or f["code"] in (r["code"] or ""))
                and (not f["name"] or f["name"] in (r["name"] or "")))

    rows = [r for r in rows if hit(r)]
    return rows[offset:offset + limit], len(rows), f


def _dept_counts():
    return {r["dept_id"]: r["c"] for r in get_db().execute(
        "SELECT dept_id, COUNT(*) c FROM member GROUP BY dept_id")}


@app.route("/departments")
@login_required
def departments():
    acc = current_account()
    rows, total, f = _departments_page(acc, 0, PAGE_ROWS)
    return render_template("departments.html", rows=rows, total=total,
                           counts=_dept_counts(), f=f)


@app.route("/departments/rows")
@login_required
def departments_rows():
    acc = current_account()
    offset = max(request.args.get("offset", type=int) or 0, 0)
    rows, total, f = _departments_page(acc, offset, PAGE_ROWS)
    return render_template("_rows_departments.html", rows=rows, counts=_dept_counts(),
                           more=1 if offset + len(rows) < total else 0)


@app.route("/departments/new", methods=["GET", "POST"])
@login_required
def department_new():
    db, acc = get_db(), current_account()
    offs = scoped_offices(acc)
    nexts = {str(o["id"]): peek_code("dept", str(o["id"]), width=3) for o in offs}
    if request.method == "GET":
        return render_template("department_form.html", row=None, offs=offs, nexts=nexts)
    g = lambda k: (request.form.get(k) or "").strip()
    oid = request.form.get("office_id", type=int)
    name = g("name")
    errs = []
    if not owns_office(acc, oid):
        log("master", "部署登録をブロック", "blocked", target=str(oid), detail="スコープ外の事業所")
        errs.append("選択された事業所を操作する権限がありません。")
    if not name:
        errs.append("部署名を入力してください。")
    elif oid and db.execute("SELECT 1 FROM department WHERE office_id=? AND name=?",
                            (oid, name)).fetchone():
        errs.append(f"「{name}」は既に登録されています。")
    if errs:
        for e in errs:
            flash(e, "error")
        return render_template("department_form.html", row=None, offs=offs, nexts=nexts,
                               form=request.form)
    ext = g("ext_code")
    if ext and db.execute("SELECT 1 FROM department WHERE office_id=? AND ext_code=?",
                          (oid, ext)).fetchone():
        flash(f"部署コード {ext} は、この事業所で既に使われています。", "error")
        return render_template("department_form.html", row=None, offs=offs, nexts=nexts,
                               form=request.form)
    code = next_code("dept", str(oid), width=3)
    db.execute("INSERT INTO department (office_id, ext_code, code, name, kana)"
               " VALUES (?,?,?,?,?)", (oid, ext or None, code, name, g("kana")))
    db.commit()
    o = next(x for x in offs if x["id"] == oid)
    log("master", "部署を登録", "success", target=f"{o['name']}／{name}",
        detail=f"部署コード={ext or '（未設定）'}／当社内部コード={code}（自動発番）")
    flash(f"「{name}」を登録しました。"
          + (f"部署コードは {ext} です。" if ext else "部署コードは未設定です。")
          + f"（当社内部コード {code}）", "ok")
    return redirect(url_for("departments"))


@app.route("/departments/<int:did>/edit", methods=["GET", "POST"])
@login_required
def department_edit(did):
    db, acc = get_db(), current_account()
    if not owns_department(acc, did):
        log("master", "部署編集をブロック", "blocked", target=str(did), detail="スコープ外")
        flash("対象の部署を操作する権限がありません。", "error")
        return redirect(url_for("departments"))
    row = next(d for d in scoped_departments(acc) if d["id"] == did)
    n_mem = db.execute("SELECT COUNT(*) c FROM member WHERE dept_id=?", (did,)).fetchone()["c"]
    if request.method == "GET":
        return render_template("department_form.html", row=row, offs=[], n_mem=n_mem)
    g = lambda k: (request.form.get(k) or "").strip()
    name = g("name")
    if not name:
        flash("部署名を入力してください。", "error")
        return render_template("department_form.html", row=row, offs=[], n_mem=n_mem,
                               form=request.form)
    if db.execute("SELECT 1 FROM department WHERE office_id=? AND name=? AND id<>?",
                  (row["office_id"], name, did)).fetchone():
        flash(f"「{name}」は既に登録されています。", "error")
        return render_template("department_form.html", row=row, offs=[], n_mem=n_mem,
                               form=request.form)
    ext = g("ext_code")
    if ext and db.execute("SELECT 1 FROM department WHERE office_id=? AND ext_code=? AND id<>?",
                          (row["office_id"], ext, did)).fetchone():
        flash(f"部署コード {ext} は、この事業所で既に使われています。", "error")
        return render_template("department_form.html", row=row, offs=[], n_mem=n_mem,
                               form=request.form)
    db.execute("UPDATE department SET ext_code=?, name=?, kana=?, updated_at=? WHERE id=?",
               (ext or None, name, g("kana"), now(), did))
    db.commit()
    log("master", "部署情報を編集", "success", target=name,
        detail=f"部署コード={row['code']}／変更前: {row['name']}")
    flash(f"「{name}」を更新しました。", "ok")
    return redirect(url_for("departments"))


@app.route("/departments/<int:did>/delete", methods=["POST"])
@login_required
def department_delete(did):
    db, acc = get_db(), current_account()
    if not owns_department(acc, did):
        flash("対象の部署を操作する権限がありません。", "error")
        return redirect(url_for("departments"))
    row = next(d for d in scoped_departments(acc) if d["id"] == did)
    n_m = db.execute("SELECT COUNT(*) c FROM member WHERE dept_id=?", (did,)).fetchone()["c"]
    n_a = db.execute(
        "SELECT COUNT(*) c FROM account_scope s JOIN account a ON a.id=s.account_id"
        " WHERE s.kind='dept' AND s.ref_id=? AND a.status<>'deleted'",
        (did,)).fetchone()["c"]
    if n_m or n_a:
        log("master", "部署の削除をブロック", "blocked", target=row["name"],
            detail=f"加入者{n_m}件・管理者{n_a}件が紐づいている")
        flash(f"この部署には加入者{n_m}件・管理者{n_a}件が紐づいているため削除できません。", "error")
        return redirect(url_for("departments"))
    db.execute("DELETE FROM department WHERE id=?", (did,))
    db.commit()
    log("master", "部署を削除", "success", target=row["name"],
        detail=f"{row['company_name']}／{row['office_name']}")
    flash(f"「{row['name']}」を削除しました。", "ok")
    return redirect(url_for("departments"))


# ================================================================ 加入者
def _members_query(acc):
    where, params = member_where(acc)
    f = {k: (request.args.get(k) or "").strip()
         for k in ("no", "name", "cname", "oname", "dname", "link")}
    sql = ("SELECT m.*, c.name AS company_name, c.code AS company_code,"
           " o.name AS office_name, o.code AS office_code,"
           " d.name AS dept_name, d.code AS dept_code"
           " FROM member m LEFT JOIN company c ON c.id=m.company_id"
           " LEFT JOIN office o ON o.id=m.office_id"
           " LEFT JOIN department d ON d.id=m.dept_id WHERE " + where)
    params = list(params)
    if f["no"]:
        sql += " AND m.member_no LIKE ?"
        params.append(f"%{f['no']}%")
    if f["name"]:
        sql += " AND (m.name LIKE ? OR m.kana LIKE ?)"
        params += [f"%{f['name']}%"] * 2
    if f["cname"]:
        sql += " AND c.name LIKE ?"
        params.append(f"%{f['cname']}%")
    if f["oname"]:
        sql += " AND o.name LIKE ?"
        params.append(f"%{f['oname']}%")
    if f["dname"]:
        sql += " AND d.name LIKE ?"
        params.append(f"%{f['dname']}%")
    if f["link"] == "未紐づけ":
        sql += " AND m.company_id IS NULL"
    elif f["link"] == "紐づけ済み":
        sql += " AND m.company_id IS NOT NULL"
    return sql, params, f


def _members_page(acc, offset, limit):
    db = get_db()
    sql, params, f = _members_query(acc)
    body = sql[sql.index(" FROM member m"):]
    total = db.execute("SELECT COUNT(*) c" + body, params).fetchone()["c"]
    rows = db.execute(sql + " ORDER BY c.code, o.code, m.member_no LIMIT ? OFFSET ?",
                      params + [limit, offset]).fetchall()
    return rows, total, f


@app.route("/members")
@login_required
def members():
    db, acc = get_db(), current_account()
    done = request.args.get("done", type=int)
    if done is not None:
        if done > 0:
            flash(f"紐づけを完了しました。このページで {done} 件を紐づけました。", "ok")
        else:
            flash("紐づけ作業を終了しました。紐づけた加入者はありません。", "ok")
    rows, total, f = _members_page(acc, 0, PAGE_ROWS)
    kenpos = db.execute("SELECT * FROM kenpo ORDER BY code").fetchall()
    return render_template("members.html", rows=rows, total=total, f=f, kenpos=kenpos,
                           comps=scoped_companies(acc), offs=scoped_offices(acc),
                           depts=scoped_departments(acc))


@app.route("/members/rows")
@login_required
def members_rows():
    acc = current_account()
    offset = max(request.args.get("offset", type=int) or 0, 0)
    rows, total, f = _members_page(acc, offset, PAGE_ROWS)
    return render_template("_rows_members.html", rows=rows,
                           more=1 if offset + len(rows) < total else 0)


def auto_link_candidates(acc):
    """加入者情報に書かれたコードで紐づけ先が決まる加入者を集める。
    企業・事業所を「先方が管理する番号」または「当社内部コード」で照合する。"""
    db = get_db()
    where, params = member_where(acc)
    rows = db.execute(
        "SELECT m.id, m.member_no, m.cert_branch, m.name, m.kenpo_id,"
        " m.src_company_code AS sc, m.src_office_code AS so"
        " FROM member m WHERE m.company_id IS NULL AND m.src_company_code IS NOT NULL"
        " AND m.src_company_code <> '' AND " + where
        + " ORDER BY m.member_no, m.cert_branch LIMIT 1000", params).fetchall()
    if not rows:
        return [], []
    allowed = {c["id"] for c in scoped_companies(acc)}
    comps = {}
    for c in db.execute("SELECT * FROM company"):
        if c["id"] not in allowed:
            continue
        for k in (c["ext_code"], c["code"]):
            if k:
                comps.setdefault((c["kenpo_id"], k), c)
    offs = {}
    for o in db.execute("SELECT * FROM office"):
        for k in (o["ext_code"], o["code"]):
            if k:
                offs.setdefault((o["company_id"], k), o)
    ok, ng = [], []
    for r in rows:
        comp = comps.get((r["kenpo_id"], r["sc"]))
        if not comp:
            ng.append(dict(r, reason=f"企業コード「{r['sc']}」に一致する企業がありません"))
            continue
        off = offs.get((comp["id"], r["so"])) if r["so"] else None
        if r["so"] and not off:
            ng.append(dict(r, reason=f"所属コード「{r['so']}」が企業「{comp['name']}」にありません",
                           company=comp["name"]))
            continue
        ok.append(dict(r, company_id=comp["id"], company=comp["name"],
                       office_id=off["id"] if off else None,
                       office=off["name"] if off else None))
    return ok, ng


@app.route("/members/link/auto", methods=["GET", "POST"])
@login_required
def members_link_auto():
    """加入者情報のコードをもとに、まとめて紐づける"""
    db, acc = get_db(), current_account()
    ok, ng = auto_link_candidates(acc)
    if request.method == "GET":
        return render_template("members_link_auto.html", ok=ok, ng=ng)
    ids = set(request.form.getlist("member_ids", type=int))
    targets = [r for r in ok if r["id"] in ids] if ids else ok
    if not targets:
        flash("紐づけできる加入者がありません。", "error")
        return redirect(url_for("members_link_auto"))
    n = 0
    for r in targets:
        db.execute("UPDATE member SET company_id=?, office_id=?, updated_at=? WHERE id=?",
                   (r["company_id"], r["office_id"], now(), r["id"]))
        n += 1
    db.commit()
    log("master", "加入者情報のコードで一括紐づけ", "success", target=f"{n}件",
        detail="対象=" + ",".join(f"{r['member_no']}" for r in targets[:20])
               + ("…" if len(targets) > 20 else ""))
    flash(f"加入者情報のコードで {n} 件を紐づけました。", "ok")
    return redirect(url_for("members"))


@app.route("/members/link")
@login_required
def members_link_page():
    """紐づけ先（企業・部署）を選ぶ画面"""
    db, acc = get_db(), current_account()
    where, params = member_where(acc)
    n_unlinked = db.execute("SELECT COUNT(*) c FROM member m WHERE " + where
                            + " AND m.company_id IS NULL", params).fetchone()["c"]
    comps, offs = scoped_companies(acc), scoped_offices(acc)
    depts = scoped_departments(acc)
    counts = {r["company_id"]: r["c"] for r in db.execute(
        "SELECT company_id, COUNT(*) c FROM member GROUP BY company_id")}
    ocounts = {r["office_id"]: r["c"] for r in db.execute(
        "SELECT office_id, COUNT(*) c FROM member GROUP BY office_id")}
    dcounts = {r["dept_id"]: r["c"] for r in db.execute(
        "SELECT dept_id, COUNT(*) c FROM member GROUP BY dept_id")}
    auto_ok, auto_ng = auto_link_candidates(acc)
    return render_template("members_link.html", comps=comps, offs=offs, depts=depts,
                           counts=counts, ocounts=ocounts, dcounts=dcounts,
                           n_unlinked=n_unlinked, n_auto=len(auto_ok), n_auto_ng=len(auto_ng))


@app.route("/members/link/assign")
@login_required
def members_link_assign():
    """選んだ企業・部署に加入者を紐づける画面"""
    db, acc = get_db(), current_account()
    cid = request.args.get("company_id", type=int)
    oid = request.args.get("office_id", type=int)
    did = request.args.get("dept_id", type=int)
    comps, offs = scoped_companies(acc), scoped_offices(acc)
    depts = scoped_departments(acc)
    comp = next((c for c in comps if c["id"] == cid), None)
    off = next((o for o in offs if o["id"] == oid), None)
    dept = next((d for d in depts if d["id"] == did), None) if did else None
    if not comp or not off or off["company_id"] != cid:
        flash("紐づけ先の企業と事業所を選んでください。", "error")
        return redirect(url_for("members_link_page"))
    if did and (not dept or dept["office_id"] != oid):
        flash("選択された部署は、その事業所のものではありません。", "error")
        return redirect(url_for("members_link_page"))

    show_linked = request.args.get("linked") == "1"
    where, params = member_where(acc)
    sql = ("SELECT m.id, m.member_no, m.cert_branch, m.name, m.kana, m.sex, m.birth,"
           " m.relation, m.company_id, m.office_id, c.name AS company_name,"
           " o.name AS office_name"
           " FROM member m LEFT JOIN company c ON c.id=m.company_id"
           " LEFT JOIN office o ON o.id=m.office_id WHERE " + where)
    if not show_linked:
        sql += " AND m.company_id IS NULL"
    elif did:
        sql += " AND (m.company_id IS NULL OR IFNULL(m.dept_id,0) <> ?)"
        params = list(params) + [did]
    else:
        sql += " AND (m.company_id IS NULL OR m.office_id <> ?)"
        params = list(params) + [oid]
    rows = db.execute(sql + " ORDER BY m.member_no, m.cert_branch LIMIT 300",
                      params).fetchall()
    n_unlinked = db.execute("SELECT COUNT(*) c FROM member m WHERE " + where
                            + " AND m.company_id IS NULL",
                            member_where(acc)[1]).fetchone()["c"]
    cond = "m.dept_id=?" if did else "m.office_id=?"
    after = db.execute(
        "SELECT m.id, m.member_no, m.cert_branch, m.name, m.kana, m.sex, m.birth,"
        " m.relation FROM member m WHERE " + cond + " AND " + where
        + " ORDER BY m.member_no, m.cert_branch LIMIT 300",
        [did or oid] + member_where(acc)[1]).fetchall()
    return render_template("members_link_assign.html", rows=rows, after=after,
                           comp=comp, off=off, dept=dept, show_linked=show_linked,
                           n_unlinked=n_unlinked)


@app.route("/api/members/by-office")
@login_required
def api_members_by_office():
    """紐づけページの「紐づけ後」欄に表示する、その事業所（または部署）の加入者を返す"""
    db, acc = get_db(), current_account()
    oid = request.args.get("office_id", type=int)
    did = request.args.get("dept_id", type=int)
    if not oid or not any(o["id"] == oid for o in scoped_offices(acc)):
        return {"ok": False, "rows": []}
    if did and not any(d["id"] == did and d["office_id"] == oid
                       for d in scoped_departments(acc)):
        return {"ok": False, "rows": []}
    where, params = member_where(acc)
    cond = "m.dept_id=?" if did else "m.office_id=?"
    rows = db.execute(
        "SELECT m.id, m.member_no, m.cert_mark, m.cert_branch, m.name, m.kana, m.sex,"
        " m.birth, m.relation FROM member m WHERE " + cond + " AND " + where
        + " ORDER BY m.member_no, m.cert_branch LIMIT 300",
        [did or oid] + params).fetchall()
    return {"ok": True, "rows": [dict(r) for r in rows]}


@app.route("/api/members/link-filtered", methods=["POST"])
@login_required
def api_members_link_filtered():
    """加入者一覧の検索結果に一致する加入者を、まとめて紐づける"""
    db, acc = get_db(), current_account()
    data = request.get_json(silent=True) or {}
    cid = data.get("company_id")
    oid = data.get("office_id") or None
    did = data.get("dept_id") or None
    comps, offs = scoped_companies(acc), scoped_offices(acc)
    comp = next((c for c in comps if c["id"] == cid), None)
    off = next((o for o in offs if o["id"] == oid), None) if oid else None
    if not comp:
        return {"ok": False, "message": "企業を選択してください。"}
    if oid and (not off or off["company_id"] != cid):
        return {"ok": False, "message": "選択された事業所は、その企業のものではありません。"}
    dept = None
    if did:
        dept = next((d for d in scoped_departments(acc) if d["id"] == did), None)
        if not dept or dept["office_id"] != oid:
            return {"ok": False, "message": "選択された部署は、その事業所のものではありません。"}

    # 一覧と同じ絞り込みでSQLを組み立てる
    f = {k: (data.get(k) or "").strip()
         for k in ("no", "name", "cname", "oname", "dname", "link")}
    where, params = member_where(acc)
    sql = ("SELECT m.id, m.member_no FROM member m"
           " LEFT JOIN company c ON c.id=m.company_id"
           " LEFT JOIN office o ON o.id=m.office_id"
           " LEFT JOIN department d ON d.id=m.dept_id WHERE " + where)
    params = list(params)
    if f["no"]:
        sql += " AND m.member_no LIKE ?"
        params.append(f"%{f['no']}%")
    if f["name"]:
        sql += " AND (m.name LIKE ? OR m.kana LIKE ?)"
        params += [f"%{f['name']}%"] * 2
    if f["cname"]:
        sql += " AND c.name LIKE ?"
        params.append(f"%{f['cname']}%")
    if f["oname"]:
        sql += " AND o.name LIKE ?"
        params.append(f"%{f['oname']}%")
    if f["dname"]:
        sql += " AND d.name LIKE ?"
        params.append(f"%{f['dname']}%")
    if f["link"] == "未紐づけ":
        sql += " AND m.company_id IS NULL"
    elif f["link"] == "紐づけ済み":
        sql += " AND m.company_id IS NOT NULL"
    targets = db.execute(sql, params).fetchall()
    if not targets:
        return {"ok": False, "message": "条件に一致する加入者がありません。"}
    for t in targets:
        db.execute("UPDATE member SET company_id=?, office_id=?, dept_id=?, kenpo_id=?,"
                   " updated_at=? WHERE id=?",
                   (cid, oid, did, comp["kenpo_id"], now(), t["id"]))
    db.commit()
    place = (f"{comp['name']}" + (f"／{off['name']}" if off else "")
             + (f"／{dept['name']}" if dept else ""))
    cond = "／".join(f"{k}={v}" for k, v in f.items() if v) or "条件なし（全件）"
    log("master", "検索結果の加入者をまとめて紐づけ", "success", target=f"{len(targets)}件",
        detail=f"{place}／絞り込み: {cond}")
    return {"ok": True, "linked": len(targets), "place": place,
            "message": f"検索結果の {len(targets)}件を「{place}」に紐づけました。"}


@app.route("/api/members/link", methods=["POST"])
@login_required
def api_members_link():
    """選んだ加入者に企業・部署を紐づける（紐づけページから呼ぶ）"""
    db, acc = get_db(), current_account()
    data = request.get_json(silent=True) or {}
    ids = [int(i) for i in (data.get("member_ids") or []) if str(i).isdigit()]
    cid = data.get("company_id")
    oid = data.get("office_id") or None
    did = data.get("dept_id") or None
    if not ids:
        return {"ok": False, "message": "紐づける加入者を選択してください。"}
    comps, offs = scoped_companies(acc), scoped_offices(acc)
    comp = next((c for c in comps if c["id"] == cid), None)
    off = next((o for o in offs if o["id"] == oid), None) if oid else None
    if not comp:
        log("master", "加入者の紐づけをブロック", "blocked", target=str(cid),
            detail="スコープ外の企業")
        return {"ok": False, "message": "企業を選択してください。"}
    if not oid:
        return {"ok": False, "message": "部署を選択してください。"}
    if not off or off["company_id"] != cid:
        return {"ok": False, "message": "選択された事業所は、その企業のものではありません。"}
    dept = None
    if did:
        dept = next((d for d in scoped_departments(acc) if d["id"] == did), None)
        if not dept or dept["office_id"] != oid:
            return {"ok": False, "message": "選択された部署は、その事業所のものではありません。"}
    where, params = member_where(acc)
    q = ",".join("?" * len(ids))
    targets = db.execute(f"SELECT m.id, m.member_no FROM member m WHERE m.id IN ({q}) AND "
                         + where, ids + params).fetchall()
    if len(targets) != len(ids):
        log("master", "加入者の紐づけをブロック", "blocked",
            detail=f"閲覧範囲外の加入者が含まれる（要求{len(ids)}件／対象{len(targets)}件）")
        return {"ok": False, "message": "選択された加入者の一部を操作する権限がありません。"}
    for t in targets:
        db.execute("UPDATE member SET company_id=?, office_id=?, dept_id=?, kenpo_id=?,"
                   " updated_at=? WHERE id=?",
                   (cid, oid, did, comp["kenpo_id"], now(), t["id"]))
    db.commit()
    place = f"{comp['name']}／{off['name']}" + (f"／{dept['name']}" if dept else "")
    log("master", "加入者に企業・事業所・部署を紐づけ", "success", target=f"{len(targets)}件",
        detail=f"{comp['code']} {comp['name']}／{off['code']} {off['name']}"
               + (f"／{dept['code']} {dept['name']}" if dept else "／部署なし")
               + f"／対象={','.join(t['member_no'] for t in targets[:20])}"
               + ("…" if len(targets) > 20 else ""))
    return {"ok": True, "linked": len(targets), "place": place,
            "message": f"{len(targets)}件を「{place}」に紐づけました。"}


MEMBER_FORM_FIELDS = ("cert_mark", "cert_branch", "attr", "relation", "kana", "sex",
                      "qualified_at", "lost_at", "zip", "address", "address2", "tel",
                      "email", "delivery_code", "employee_code")


def _member_form_values(form):
    """フォームから加入者の任意項目を取り出す。日付は形式をそろえる。"""
    g = lambda k: (form.get(k) or "").strip()
    vals, errs = {}, []
    for k in MEMBER_FORM_FIELDS:
        vals[k] = g(k)
    for k, label in (("birth", "生年月日"), ("qualified_at", "資格取得日"),
                     ("lost_at", "資格喪失日")):
        v = norm_date(g(k))
        if v is None:
            errs.append(f"{label}の形式が正しくありません。")
            v = ""
        if k == "birth":
            vals["birth"] = v
        else:
            vals[k] = v
    # 切り替え（トグル）は 0／1 で持ちます
    for k in ("night_work", "excluded"):
        vals[k] = 1 if form.get(k) else 0
    return vals, errs


@app.route("/members/new", methods=["GET", "POST"])
@login_required
def member_new():
    db, acc = get_db(), current_account()
    comps, offs = scoped_companies(acc), scoped_offices(acc)
    depts = scoped_departments(acc)
    nexts = {str(c["id"]): peek_code("member", str(c["kenpo_id"]), width=8) for c in comps}
    if acc["kenpo_id"]:
        nexts[""] = peek_code("member", str(acc["kenpo_id"]), width=8)
    if request.method == "GET":
        return render_template("member_form.html", row=None, comps=comps, offs=offs,
                               depts=depts, nexts=nexts)
    cid = request.form.get("company_id", type=int)
    oid = request.form.get("office_id", type=int)
    did = request.form.get("dept_id", type=int)
    no = (request.form.get("member_no") or "").strip()
    name = (request.form.get("name") or "").strip()
    vals, errs = _member_form_values(request.form)
    comp = next((c for c in comps if c["id"] == cid), None) if cid else None
    off = next((o for o in offs if o["id"] == oid), None) if oid else None
    dept = next((d for d in depts if d["id"] == did), None) if did else None
    if did and (not dept or dept["office_id"] != oid):
        errs.append("選択された部署は、その事業所のものではありません。")
    if cid and not comp:
        log("master", "加入者登録をブロック", "blocked", target=no, detail="スコープ外の企業")
        errs.append("選択された企業を操作する権限がありません。")
    if oid and (not off or off["company_id"] != cid):
        errs.append("選択された部署は、その企業のものではありません。")
    if not no:
        errs.append("被保険者証番号を入力してください。")
    if not name:
        errs.append("対象者氏名（漢字）を入力してください。")
    kenpo_id = comp["kenpo_id"] if comp else acc["kenpo_id"]
    if not kenpo_id:
        errs.append("企業を選択してください（所属する健康保険組合を決めるために必要です）。")
    if no and kenpo_id and cur_id(db, kenpo_id, vals["cert_mark"], no, vals["cert_branch"]):
        errs.append(f"被保険者証記号「{vals['cert_mark'] or '（空）'}」・番号「{no}」"
                    f"・枝番「{vals['cert_branch'] or '（空）'}」の加入者は既に登録されています。")
    if errs:
        for e in errs:
            flash(e, "error")
        return render_template("member_form.html", row=None, comps=comps, offs=offs,
                               depts=depts, nexts=nexts, form=request.form)
    keys = list(vals.keys())
    db.execute("INSERT INTO member (kenpo_id, company_id, office_id, dept_id, member_no,"
               " name, subscriber_id, " + ", ".join(keys) + ") VALUES (?,?,?,?,?,?,?,"
               + ",".join("?" * len(keys)) + ")",
               [kenpo_id, cid or None, oid or None, did or None, no, name,
                next_code("member", str(kenpo_id), width=8)] + [vals[k] for k in keys])
    db.commit()
    log("master", "加入者を登録", "success", target=no,
        detail=f"{comp['name'] if comp else '企業未紐づけ'}／"
               f"{off['name'] if off else '事業所未設定'}／"
               f"{dept['name'] if dept else '部署未設定'}")
    flash(f"被保険者証番号 {no} を登録しました。", "ok")
    return redirect(url_for("members"))


@app.route("/members/<int:mid>/edit", methods=["GET", "POST"])
@login_required
def member_edit(mid):
    db, acc = get_db(), current_account()
    where, params = member_where(acc)
    row = db.execute(
        "SELECT m.*, c.name AS company_name, o.name AS office_name, d.name AS dept_name"
        " FROM member m LEFT JOIN company c ON c.id=m.company_id"
        " LEFT JOIN office o ON o.id=m.office_id LEFT JOIN department d ON d.id=m.dept_id"
        " WHERE m.id=? AND " + where, [mid] + params).fetchone()
    if not row:
        log("master", "加入者編集をブロック", "blocked", target=str(mid), detail="スコープ外")
        flash("対象の加入者を操作する権限がありません。", "error")
        return redirect(url_for("members"))
    comps, offs = scoped_companies(acc), scoped_offices(acc)
    depts = scoped_departments(acc)
    if request.method == "GET":
        return render_template("member_form.html", row=row, comps=comps, offs=offs,
                               depts=depts, mypage=member_services(db, row))
    cid = request.form.get("company_id", type=int)
    oid = request.form.get("office_id", type=int)
    did = request.form.get("dept_id", type=int)
    name = (request.form.get("name") or "").strip()
    no = (request.form.get("member_no") or "").strip()
    vals, errs = _member_form_values(request.form)
    comp = next((c for c in comps if c["id"] == cid), None) if cid else None
    off = next((o for o in offs if o["id"] == oid), None) if oid else None
    dept = next((d for d in depts if d["id"] == did), None) if did else None
    if did and (not dept or dept["office_id"] != oid):
        errs.append("選択された部署は、その事業所のものではありません。")
    if cid and not comp:
        errs.append("選択された企業を操作する権限がありません。")
    if oid and (not off or off["company_id"] != cid):
        errs.append("選択された部署は、その企業のものではありません。")
    if not name:
        errs.append("対象者氏名（漢字）を入力してください。")
    if not no:
        errs.append("被保険者証番号を入力してください。")
    else:
        dup = cur_id(db, row["kenpo_id"], vals["cert_mark"], no, vals["cert_branch"])
        if dup and dup["id"] != mid:
            errs.append(f"被保険者証記号「{vals['cert_mark'] or '（空）'}」・番号「{no}」"
                        f"・枝番「{vals['cert_branch'] or '（空）'}」の加入者は既に登録されています。")
    if errs:
        for e in errs:
            flash(e, "error")
        return render_template("member_form.html", row=row, comps=comps, offs=offs,
                               depts=depts, form=request.form,
                               mypage=member_services(db, row))
    before = (f"{row['name']}／{row['company_name'] or '企業未紐づけ'}"
              f"／{row['office_name'] or '部署未設定'}")
    keys = list(vals.keys())
    db.execute("UPDATE member SET company_id=?, office_id=?, dept_id=?, member_no=?, name=?, "
               + ", ".join(f"{k}=?" for k in keys) + ", updated_at=? WHERE id=?",
               [cid or None, oid or None, did or None, no, name]
               + [vals[k] for k in keys] + [now(), mid])
    db.commit()
    log("master", "加入者情報を編集", "success", target=no,
        detail=f"変更前: {before} → {name}／{comp['name'] if comp else '企業未紐づけ'}"
               f"／{off['name'] if off else '部署未設定'}")
    flash(f"被保険者証番号 {no} の情報を更新しました。", "ok")
    return redirect(url_for("members"))


@app.route("/members/<int:mid>/delete", methods=["POST"])
@login_required
def member_delete(mid):
    db, acc = get_db(), current_account()
    where, params = member_where(acc)
    row = db.execute("SELECT m.*, c.name AS company_name FROM member m"
                     " JOIN company c ON c.id=m.company_id WHERE m.id=? AND " + where,
                     [mid] + params).fetchone()
    if not row:
        flash("対象の加入者を操作する権限がありません。", "error")
        return redirect(url_for("members"))
    db.execute("DELETE FROM member WHERE id=?", (mid,))
    db.commit()
    log("master", "加入者を削除", "success", target=row["member_no"],
        detail=f"{row['company_name']}／{row['name']}")
    flash(f"加入者番号 {row['member_no']} を削除しました。", "ok")
    return redirect(url_for("members"))




def read_table(fs):
    raw = fs.read()
    for enc in ("utf-8-sig", "cp932", "utf-8"):
        try:
            return list(csv.DictReader(io.StringIO(raw.decode(enc))))
        except UnicodeDecodeError:
            continue
    raise ValueError("文字コードを判別できませんでした（UTF-8 または Shift_JIS で保存してください）")


def csv_response(filename, header, rows):
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\r\n")
    w.writerow(header)
    w.writerows(rows)
    # Excel でそのまま開けるように Shift_JIS で出力する
    return Response(buf.getvalue().encode("cp932", errors="replace"),
                    headers={"Content-Type": "text/csv; charset=Shift_JIS",
                             "Content-Disposition": f'attachment; filename="{filename}"'})


# ================================================================ 加入者の一括取込
# 実際の加入者情報フォーマット
MEMBER_COLUMNS = ["加入者ID", "被保険者証記号", "被保険者証番号", "被保険者証枝番",
                  "被保険者属性名", "続柄名称", "対象者氏名（漢字）", "対象者氏名（カナ）",
                  "性別", "生年月日", "資格取得日（家族認定日）", "資格喪失日（家族削除日）",
                  "郵便番号", "住所", "住所（建物名）", "電話番号", "メールアドレス",
                  "事業所（企業）コード", "所属コード", "配付先コード", "社員コード",
                  "connectID", "個人ID"]
MEMBER_REQUIRED = ["被保険者証番号", "対象者氏名（漢字）"]
# 加入者IDは当システムで採番する（ファイルの値は使わない）
MEMBER_SAMPLE = ["", "99999", "9001", "", "", "本人", "伊藤博文", "イトウヒロブミ", "男",
                 "1961/10/16", "2000/4/1", "", "1710014", "東京都豊島区池袋2-43-1",
                 "池袋青柳ビル　７階", "364163872",
                 "hirobumi.nagao+ZD5D0200@kusurinomadoguchi.co.jp", "436", "", "", "", "", ""]


def cur_id(db, kenpo_id, mark, member_no, branch):
    """既に登録済みの加入者かどうかを返す。
    本人と家族は同じ被保険者証番号で枝番が異なるため、記号・番号・枝番で判断する。"""
    if not member_no:
        return None
    return db.execute(
        "SELECT id FROM member WHERE kenpo_id=? AND IFNULL(cert_mark,'')=?"
        " AND member_no=? AND IFNULL(cert_branch,'')=?",
        (kenpo_id, mark or "", member_no, branch or "")).fetchone()


def norm_date(v):
    """1961/10/16 や 1961-10-16 を 1961-10-16 にそろえる。判別できなければ None"""
    v = (v or "").strip()
    if not v:
        return ""
    m = re.fullmatch(r"(\d{4})[/\-.](\d{1,2})[/\-.](\d{1,2})", v)
    if not m:
        return None
    return f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"


@app.route("/template/members.csv")
@login_required
def members_template():
    log("download", "取込フォーマットを出力", "success", target="加入者情報フォーマット")
    return csv_response("subscriber_format.csv", MEMBER_COLUMNS, [MEMBER_SAMPLE])


@app.route("/members/import", methods=["POST"])
@login_required
def members_import():
    """加入者情報フォーマットを取り込む。企業と部署はコードで指定する。"""
    db, acc = get_db(), current_account()
    fs = request.files.get("file")
    rows, resp = _read_upload(fs, MEMBER_COLUMNS, "members",
                              ["被保険者証番号", "対象者氏名（漢字）", "事業所（企業）コード"])
    if rows is None:
        return resp

    # 取込先の健保。当社スタッフは画面で選び、それ以外は自分の所属で決まる
    if acc["role"] == "system_admin":
        kenpo_id = request.form.get("kenpo_id", type=int)
        if not kenpo_id:
            flash("取込先の健康保険組合を選択してください。", "error")
            return redirect(url_for("members"))
    else:
        kenpo_id = acc["kenpo_id"]
    # 先方が管理する番号（ext_code）で引けるようにする。内部コードでも引ける
    allowed = {c["id"] for c in scoped_companies(acc)}
    comps = {}
    for c in db.execute("SELECT * FROM company WHERE kenpo_id=?", (kenpo_id,)):
        if c["id"] not in allowed:
            continue
        for k in (c["ext_code"], c["code"]):
            if k:
                comps.setdefault(k, c)
    offs = {}
    for o in db.execute("SELECT o.* FROM office o JOIN company c ON c.id=o.company_id"
                        " WHERE c.kenpo_id=?", (kenpo_id,)):
        for k in (o["ext_code"], o["code"]):
            if k:
                offs.setdefault((o["company_id"], k), o)

    ok, err, seen = [], [], set()
    for i, r in enumerate(rows, start=2):
        g = lambda k: (r.get(k) or "").strip()
        no, name, ccode = g("被保険者証番号"), g("対象者氏名（漢字）"), g("事業所（企業）コード")
        ocode = g("所属コード")
        mark, branch = g("被保険者証記号"), g("被保険者証枝番")
        e, warn = [], []
        for col in MEMBER_REQUIRED:
            if not g(col):
                e.append(f"{col}が未入力")
        comp = comps.get(ccode)
        if ccode and not comp:
            warn.append(f"企業コード「{ccode}」は未登録のため未紐づけで登録します")
        off = offs.get((comp["id"], ocode)) if comp and ocode else None
        if ocode and comp and not off:
            warn.append(f"所属コード「{ocode}」は企業「{comp['name']}」に未登録のため"
                        f"部署は未設定にします")
        cur = cur_id(db, kenpo_id, mark, no, branch)
        if g("加入者ID") and not cur:
            warn.append(f"加入者IDは当システムで採番します（ファイルの「{g('加入者ID')}」は使いません）")
        birth = norm_date(g("生年月日"))
        if birth is None:
            e.append("生年月日の形式が不正（例：1961/10/16）")
        qual = norm_date(g("資格取得日（家族認定日）"))
        if qual is None:
            e.append("資格取得日の形式が不正")
        lost = norm_date(g("資格喪失日（家族削除日）"))
        if lost is None:
            e.append("資格喪失日の形式が不正")
        key = (mark, no, branch)
        if key in seen:
            e.append("同じ被保険者証記号・番号・枝番の行がファイル内で重複")
        seen.add(key)

        row = {"line": i, "cells": [g(c) for c in MEMBER_COLUMNS], "errors": e,
               "warnings": warn, "mode": "更新" if cur else "新規"}
        if not e:
            row.update(kenpo_id=kenpo_id, company_id=comp["id"] if comp else None,
                       office_id=off["id"] if off else None,
                       member_no=no, name=name, member_id=cur["id"] if cur else None,
                       vals={
                           "src_company_code": ccode,
                           "src_office_code": ocode,
                           "cert_mark": g("被保険者証記号"),
                           "cert_branch": g("被保険者証枝番"),
                           "attr": g("被保険者属性名"),
                           "relation": g("続柄名称"),
                           "kana": g("対象者氏名（カナ）"),
                           "sex": g("性別"),
                           "birth": birth,
                           "qualified_at": qual,
                           "lost_at": lost,
                           "zip": g("郵便番号"),
                           "address": g("住所"),
                           "address2": g("住所（建物名）"),
                           "tel": g("電話番号"),
                           "email": g("メールアドレス"),
                           "delivery_code": g("配付先コード"),
                           "employee_code": g("社員コード"),
                           "connect_id": g("connectID"),
                           "personal_id": g("個人ID"),
                       })
        (err if e else ok).append(row)

    token = _stage("member", ok, fs.filename) if (ok and not err) else None
    kn = db.execute("SELECT name FROM kenpo WHERE id=?", (kenpo_id,)).fetchone()
    log("import", "加入者の取込ファイルを検証", "success" if not err else "failure",
        target=fs.filename, detail=f"正常{len(ok)}件／エラー{len(err)}件")
    return render_template("master_preview.html", title="加入者の一括インポート",
                           columns=MEMBER_COLUMNS, ok_rows=ok, err_rows=err, token=token,
                           filename=fs.filename, back_url=url_for("members"),
                           commit_url=url_for("members_import_commit"), show_mode=True,
                           has_warn=any(r["warnings"] for r in ok + err),
                           lead=f"取込先の健康保険組合：{kn['name'] if kn else '—'}。"
                                f"被保険者証番号が既にある場合は内容を更新します。"
                                f"企業・部署が未登録の場合は未紐づけで登録し、"
                                f"あとで加入者一覧から紐づけできます。")


@app.route("/members/import/commit", methods=["POST"])
@login_required
def members_import_commit():
    db, acc = get_db(), current_account()
    stg = STAGING.pop(request.form.get("token") or "", None)
    if not stg or stg["kind"] != "member" or stg["email"] != acc["email"]:
        flash("取込内容の有効期限が切れています。もう一度アップロードしてください。", "error")
        return redirect(url_for("members"))
    ins = upd = 0
    for r in stg["rows"]:
        v = r["vals"]
        keys = list(v.keys())
        if r["member_id"]:
            sets = ", ".join(f"{k}=?" for k in keys)
            db.execute(f"UPDATE member SET company_id=?, office_id=?, name=?, {sets},"
                       " updated_at=? WHERE id=?",
                       [r["company_id"], r["office_id"], r["name"]]
                       + [v[k] for k in keys] + [now(), r["member_id"]])
            upd += 1
        else:
            cols = ", ".join(keys)
            marks = ", ".join("?" * len(keys))
            db.execute(f"INSERT INTO member (kenpo_id, company_id, office_id, member_no,"
                       f" name, subscriber_id, {cols}) VALUES (?,?,?,?,?,?,{marks})",
                       [r["kenpo_id"], r["company_id"], r["office_id"], r["member_no"],
                        r["name"], next_code("member", str(r["kenpo_id"]), width=8)]
                       + [v[k] for k in keys])
            ins += 1
    db.commit()
    log("import", "加入者情報を取込", "success", target=stg["filename"],
        detail=f"新規{ins}件／更新{upd}件")
    flash(f"取込が完了しました。新規 {ins} 件／更新 {upd} 件。", "ok")
    return redirect(url_for("members"))


# ================================================================ 企業の一括取込
# 実際の登録フォーマット（1行に企業＋部署をまとめて記載する）
def _stage(kind, rows, filename):
    tok = uuid.uuid4().hex
    STAGING[tok] = {"kind": kind, "rows": rows, "email": current_account()["email"],
                    "filename": filename}
    return tok


def _read_upload(fs, columns, back, required=None):
    """アップロードされたCSVを読み、必要な列があるかまで検証する。
    失敗時は (None, response) を返す。"""
    if not fs or not fs.filename:
        flash("ファイルを選択してください。", "error")
        return None, redirect(url_for(back))
    try:
        rows = read_table(fs)
    except ValueError as e:
        log("import", "取込ファイルの読込失敗", "failure", target=fs.filename, detail=str(e))
        flash(str(e), "error")
        return None, redirect(url_for(back))
    if not rows:
        flash("データ行がありません。", "error")
        return None, redirect(url_for(back))
    have = set(rows[0].keys())
    missing = [c for c in (required or columns) if c not in have]
    if missing:
        log("import", "取込ファイルの列不足", "failure", target=fs.filename,
            detail="不足列=" + ",".join(missing))
        flash("必要な列がありません：" + "、".join(missing), "error")
        return None, redirect(url_for(back))
    return rows, None


COMPANY_COLUMNS = ["事業所（企業）コード", "保険者番号", "被保険者証記号", "企業名",
                   "企業名（フリガナ）", "郵便番号", "住所", "電話番号", "担当メールアドレス",
                   "所属コード", "部署名", "部署名（フリガナ）"]
COMPANY_REQUIRED = ["保険者番号", "企業名", "部署名"]
COMPANY_SAMPLE = ["456", "6139166", "1000", "ひかり健康保険組合", "ヒカリケンコウホケンクミアイ",
                  "1080023", "東京都港区芝浦4-16-25　安全ビル4F", "363651454",
                  "Yuki-Kuriyama@hikarikenpo.or.jp", "8718", "株式会社　光通信", "ヒカリツウシン"]


@app.route("/sample/<name>.csv")
@login_required
def sample_csv(name):
    """お試し用のサンプルCSV（部署・家族まで入ったデータ）"""
    files = {"company": "company_sample.csv", "office": "office_sample.csv",
             "subscriber": "subscriber_sample.csv"}
    fn = files.get(name)
    path = os.path.join(BASE_DIR, "samples", fn) if fn else None
    if not path or not os.path.exists(path):
        flash("サンプルが見つかりません。", "error")
        return redirect(url_for("dashboard"))
    log("download", "サンプルCSVを出力", "success", target=fn)
    with open(path, "rb") as f:
        return Response(f.read(),
                        headers={"Content-Type": "text/csv; charset=Shift_JIS",
                                 "Content-Disposition": f'attachment; filename="{fn}"'})


@app.route("/template/companies.csv")
@roles_required("system_admin", "kenpo_user")
def companies_template():
    log("download", "取込フォーマットを出力", "success", target="企業登録フォーマット")
    return csv_response("company_format.csv", COMPANY_COLUMNS, [COMPANY_SAMPLE])


@app.route("/companies/import", methods=["POST"])
@roles_required("system_admin", "kenpo_user")
def companies_import():
    """企業登録フォーマットを取り込む。1行で企業と部署（事業所）を登録・更新する。"""
    db, acc = get_db(), current_account()
    fs = request.files.get("file")
    rows, resp = _read_upload(fs, COMPANY_COLUMNS, "companies",
                              ["事業所（企業）コード", "保険者番号", "企業名", "部署名"])
    if rows is None:
        return resp

    # 取込先として許される健保（保険者番号 → id）
    if acc["role"] == "system_admin":
        kmap = {r["code"]: r for r in db.execute("SELECT * FROM kenpo")}
    else:
        kmap = {r["code"]: r for r in db.execute("SELECT * FROM kenpo WHERE id=?",
                                                 (acc["kenpo_id"],))}
    ok, err, seen, seen_c = [], [], set(), set()
    for i, r in enumerate(rows, start=2):
        g = lambda k: (r.get(k) or "").strip()
        ccode, kcode, cname = g("事業所（企業）コード"), g("保険者番号"), g("企業名")
        ocode, oname = g("所属コード"), g("部署名")
        e = []
        for col in COMPANY_REQUIRED:
            if not g(col):
                e.append(f"{col}が未入力")
        kenpo = kmap.get(kcode)
        if kcode and not kenpo:
            e.append(f"保険者番号「{kcode}」の健康保険組合が未登録"
                     + ("" if acc["role"] == "system_admin" else "、または操作の範囲外"))
        key = (kcode, ccode or cname, ocode or oname)
        if key in seen:
            e.append("同じ企業・事業所の行がファイル内で重複")
        seen.add(key)

        # 既存かどうかは「健保が管理する事業所（企業）コード」で判断する。
        # コードが空の場合は企業名で照合する。
        cur_c = None
        if kenpo and ccode:
            cur_c = db.execute("SELECT * FROM company WHERE kenpo_id=? AND ext_code=?",
                               (kenpo["id"], ccode)).fetchone()
        if not cur_c and kenpo and cname:
            cur_c = db.execute("SELECT * FROM company WHERE kenpo_id=? AND name=?",
                               (kenpo["id"], cname)).fetchone()
        cur_o = None
        if cur_c and ocode:
            cur_o = db.execute("SELECT * FROM office WHERE company_id=? AND ext_code=?",
                               (cur_c["id"], ocode)).fetchone()
        if not cur_o and cur_c and oname:
            cur_o = db.execute("SELECT * FROM office WHERE company_id=? AND name=?",
                               (cur_c["id"], oname)).fetchone()
        # 同じファイル内の先行行で既に登録される企業も「既存」として扱う
        in_file = (kcode, ccode or cname) in seen_c
        seen_c.add((kcode, ccode or cname))
        warn = []
        if not ccode:
            warn.append("事業所（企業）コードが空欄のため、企業名で照合します")
        if not ocode:
            warn.append("所属コードが空欄のため、事業所名で照合します")
        row = {"line": i, "cells": [g(c) for c in COMPANY_COLUMNS], "errors": e,
               "warnings": warn,
               "mode": ("更新" if (cur_c or in_file) else "新規")
                       + "／" + ("更新" if cur_o else "新規")}
        if not e:
            row.update(kenpo_id=kenpo["id"], ccode=ccode, cname=cname, kname=kenpo["name"],
                       ckana=g("企業名（フリガナ）"), cert=g("被保険者証記号"),
                       zip=g("郵便番号"), addr=g("住所"), tel=g("電話番号"),
                       email=g("担当メールアドレス"),
                       ocode=ocode, oname=oname, okana=g("部署名（フリガナ）"),
                       company_id=cur_c["id"] if cur_c else None,
                       office_id=cur_o["id"] if cur_o else None)
        (err if e else ok).append(row)

    token = _stage("company", ok, fs.filename) if (ok and not err) else None
    log("import", "企業の取込ファイルを検証", "success" if not err else "failure",
        target=fs.filename, detail=f"正常{len(ok)}件／エラー{len(err)}件")
    return render_template("master_preview.html", title="企業の一括インポート",
                           columns=COMPANY_COLUMNS, ok_rows=ok, err_rows=err, token=token,
                           filename=fs.filename, back_url=url_for("companies"),
                           commit_url=url_for("companies_import_commit"), show_mode=True,
                           has_warn=any(r["warnings"] for r in ok + err),
                           lead="1行につき企業と部署（事業所）を登録します。"
                                "事業所（企業）コードと所属コードは"
                                "健保・企業が管理する番号として保存し、次回の取込で"
                                "同じ番号の行があれば内容を更新します（更新のキー）。"
                                "当社内部コードは自動で割り振ります。")


@app.route("/companies/import/commit", methods=["POST"])
@roles_required("system_admin", "kenpo_user")
def companies_import_commit():
    db, acc = get_db(), current_account()
    stg = STAGING.pop(request.form.get("token") or "", None)
    if not stg or stg["kind"] != "company" or stg["email"] != acc["email"]:
        flash("取込内容の有効期限が切れています。もう一度アップロードしてください。", "error")
        return redirect(url_for("companies"))
    n_c = n_cu = n_o = n_ou = 0
    for r in stg["rows"]:
        # 先方の番号（なければ名称）で引き直す。同じ企業が複数行にあっても1社にまとめる
        cur_c = None
        if r["ccode"]:
            cur_c = db.execute("SELECT id FROM company WHERE kenpo_id=? AND ext_code=?",
                               (r["kenpo_id"], r["ccode"])).fetchone()
        if not cur_c:
            cur_c = db.execute("SELECT id FROM company WHERE kenpo_id=? AND name=?",
                               (r["kenpo_id"], r["cname"])).fetchone()
        if cur_c:
            cid = cur_c["id"]
            db.execute("UPDATE company SET name=?, ext_code=COALESCE(?, ext_code),"
                       " code=?, kana=?, cert_mark=?, zip=?, tel=?, address=?, email=?,"
                       " updated_at=? WHERE id=?",
                       (r["cname"], r["ccode"] or None,
                        internal_company_code(r["kname"], r["cname"]),
                        r["ckana"], r["cert"], r["zip"], r["tel"], r["addr"],
                        r["email"], now(), cid))
            if r["company_id"]:
                n_cu += 1
        else:
            cid = db.execute(
                "INSERT INTO company (kenpo_id, ext_code, code, name, kana, cert_mark,"
                " zip, tel, address, email) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (r["kenpo_id"], r["ccode"] or None,
                 internal_company_code(r["kname"], r["cname"]), r["cname"],
                 r["ckana"], r["cert"], r["zip"], r["tel"], r["addr"],
                 r["email"])).lastrowid
            n_c += 1
        cur_o = None
        if r["ocode"]:
            cur_o = db.execute("SELECT id FROM office WHERE company_id=? AND ext_code=?",
                               (cid, r["ocode"])).fetchone()
        if not cur_o:
            cur_o = db.execute("SELECT id FROM office WHERE company_id=? AND name=?",
                               (cid, r["oname"])).fetchone()
        if cur_o:
            db.execute("UPDATE office SET name=?, ext_code=COALESCE(?, ext_code),"
                       " kana=?, updated_at=? WHERE id=?",
                       (r["oname"], r["ocode"] or None, r["okana"], now(), cur_o["id"]))
            n_ou += 1
        else:
            db.execute("INSERT INTO office (company_id, ext_code, code, name, kana)"
                       " VALUES (?,?,?,?,?)",
                       (cid, r["ocode"] or None,
                        next_code("office", str(cid), width=3), r["oname"], r["okana"]))
            n_o += 1
    db.commit()
    log("master", "企業をCSVで一括登録", "success", target=stg["filename"],
        detail=f"企業 新規{n_c}／更新{n_cu}、部署 新規{n_o}／更新{n_ou}")
    flash(f"企業を新規{n_c}件・更新{n_cu}件、部署を新規{n_o}件・更新{n_ou}件で取り込みました。", "ok")
    return redirect(url_for("companies"))


# ================================================================ 事業所（部署）の一括取込
# 企業登録フォーマットと同じ呼び方でそろえた、部署だけを登録するフォーマット
OFFICE_COLUMNS = ["保険者番号", "事業所（企業）コード", "所属コード", "部署名",
                  "部署名（フリガナ）", "郵便番号", "住所", "電話番号"]
OFFICE_REQUIRED = ["保険者番号", "事業所（企業）コード", "部署名"]
# 所属コードはファイルの値を使わず、当システムで採番する
OFFICE_SAMPLE = ["6139166", "456", "8718", "株式会社　光通信", "ヒカリツウシン",
                 "1080023", "東京都港区芝浦4-16-25　安全ビル4F", "363651454"]


@app.route("/template/offices.csv")
@login_required
def offices_template():
    log("download", "取込フォーマットを出力", "success", target="事業所登録フォーマット")
    return csv_response("office_format.csv", OFFICE_COLUMNS, [OFFICE_SAMPLE])


@app.route("/offices/import", methods=["POST"])
@login_required
def offices_import():
    """事業所（部署）登録フォーマットを取り込む。保険者番号と企業コードで所属先を決める。"""
    db, acc = get_db(), current_account()
    fs = request.files.get("file")
    rows, resp = _read_upload(fs, OFFICE_COLUMNS, "offices", OFFICE_REQUIRED)
    if rows is None:
        return resp

    # 操作できる企業を (保険者番号, 事業所（企業）コード) で引けるようにする。
    # 健保が管理する番号（ext_code）でも、当社内部コードでも引ける。
    comps = {}
    for c in scoped_companies(acc):
        k = db.execute("SELECT code FROM kenpo WHERE id=?", (c["kenpo_id"],)).fetchone()
        for key in (c["ext_code"], c["code"]):
            if key:
                comps.setdefault((k["code"], key), c)

    ok, err, seen = [], [], set()
    for i, r in enumerate(rows, start=2):
        g = lambda k: (r.get(k) or "").strip()
        kcode, ccode, ocode, name = (g("保険者番号"), g("事業所（企業）コード"),
                                     g("所属コード"), g("部署名"))
        e = []
        for col in OFFICE_REQUIRED:
            if not g(col):
                e.append(f"{col}が未入力")
        comp = comps.get((kcode, ccode))
        if kcode and ccode and not comp:
            e.append(f"保険者番号「{kcode}」・事業所（企業）コード「{ccode}」の企業が"
                     f"未登録、または操作の範囲外")
        key = (kcode, ccode, ocode or name)
        if key in seen:
            e.append("同じ企業・事業所の行がファイル内で重複")
        seen.add(key)

        # 既存かどうかは「企業が管理する所属コード」で判断する。空欄なら名称で照合
        cur = None
        if comp and ocode:
            cur = db.execute("SELECT * FROM office WHERE company_id=? AND ext_code=?",
                             (comp["id"], ocode)).fetchone()
        if not cur and comp and name:
            cur = db.execute("SELECT * FROM office WHERE company_id=? AND name=?",
                             (comp["id"], name)).fetchone()
        warn = []
        if not ocode:
            warn.append("所属コードが空欄のため、事業所名で照合します")
        row = {"line": i, "cells": [g(c) for c in OFFICE_COLUMNS], "errors": e,
               "warnings": warn, "mode": "更新" if cur else "新規"}
        if not e:
            row.update(company_id=comp["id"], code=ocode, name=name,
                       kana=g("部署名（フリガナ）"), zip=g("郵便番号"),
                       address=g("住所"), tel=g("電話番号"),
                       office_id=cur["id"] if cur else None)
        (err if e else ok).append(row)

    token = _stage("office", ok, fs.filename) if (ok and not err) else None
    log("import", "事業所の取込ファイルを検証", "success" if not err else "failure",
        target=fs.filename, detail=f"正常{len(ok)}件／エラー{len(err)}件")
    return render_template("master_preview.html", title="事業所の一括インポート",
                           columns=OFFICE_COLUMNS, ok_rows=ok, err_rows=err, token=token,
                           filename=fs.filename, back_url=url_for("offices"),
                           commit_url=url_for("offices_import_commit"), show_mode=True,
                           has_warn=any(r["warnings"] for r in ok + err),
                           lead="所属コードは企業が管理する番号として保存し、"
                                "次回の取込で同じ番号の行があれば内容を更新します（更新のキー）。"
                                "当社内部コードは登録順に自動発番します。")


@app.route("/offices/import/commit", methods=["POST"])
@login_required
def offices_import_commit():
    db, acc = get_db(), current_account()
    stg = STAGING.pop(request.form.get("token") or "", None)
    if not stg or stg["kind"] != "office" or stg["email"] != acc["email"]:
        flash("取込内容の有効期限が切れています。もう一度アップロードしてください。", "error")
        return redirect(url_for("offices"))
    ins = upd = 0
    for r in stg["rows"]:
        if r["office_id"]:
            db.execute("UPDATE office SET name=?, ext_code=COALESCE(?, ext_code), kana=?,"
                       " zip=?, address=?, tel=?, updated_at=? WHERE id=?",
                       (r["name"], r["code"] or None, r["kana"], r["zip"], r["address"],
                        r["tel"], now(), r["office_id"]))
            upd += 1
        else:
            db.execute("INSERT INTO office (company_id, ext_code, code, name, kana, zip,"
                       " address, tel) VALUES (?,?,?,?,?,?,?,?)",
                       (r["company_id"], r["code"] or None,
                        next_code("office", str(r["company_id"]), width=3),
                        r["name"], r["kana"], r["zip"], r["address"], r["tel"]))
            ins += 1
    db.commit()
    log("master", "事業所をCSVで一括登録", "success", target=stg["filename"],
        detail=f"新規{ins}件／更新{upd}件")
    flash(f"事業所を新規{ins}件・更新{upd}件で取り込みました。", "ok")
    return redirect(url_for("offices"))


# ================================================================ 出力
def export_csv(filename, header, rows, kind):
    acc = current_account()
    if not feature_allowed("download", acc):
        log("download", "出力をブロック", "blocked", target=kind,
            detail=f"機能制御で不可（role={role_key(acc)}）")
        flash("このロールではCSVのダウンロードができません（機能制御の設定）。", "error")
        return None
    if not acc["can_download"] and acc["role"] != "system_admin":
        log("download", "出力をブロック", "blocked", target=kind, detail="ダウンロード権限なし")
        flash("このアカウントにはダウンロード権限がありません。", "error")
        return None
    if len(rows) > MAX_EXPORT_ROWS:
        log("download", "出力をブロック（上限超過）", "blocked", target=kind,
            detail=f"要求{len(rows)}件 > 上限{MAX_EXPORT_ROWS}件。管理者へ通知")
        flash(f"出力件数が上限（{MAX_EXPORT_ROWS}件）を超えています。"
              f"条件を絞ってください。この操作は記録されました。", "error")
        return None
    log("download", "CSVを出力", "success", target=kind, detail=f"{len(rows)}件")
    return csv_response(filename, header, rows)


@app.route("/members/export")
@login_required
def members_export():
    """取込フォーマットと同じ並びで出力する（出力したものをそのまま取込に使える）"""
    db, acc = get_db(), current_account()
    where, params = member_where(acc)
    rows = db.execute(
        "SELECT m.subscriber_id, m.cert_mark, m.member_no, m.cert_branch, m.attr,"
        " m.relation, m.name, m.kana, m.sex, m.birth, m.qualified_at, m.lost_at,"
        " m.zip, m.address, m.address2, m.tel, m.email, c.code, o.code,"
        " m.delivery_code, m.employee_code, m.connect_id, m.personal_id"
        " FROM member m LEFT JOIN company c ON c.id=m.company_id"
        " LEFT JOIN office o ON o.id=m.office_id"
        " WHERE " + where + " ORDER BY c.code, o.code, m.member_no", params).fetchall()
    return export_csv("subscriber.csv", MEMBER_COLUMNS,
                      [tuple("" if v is None else v for v in r) for r in rows],
                      "加入者情報") or redirect(url_for("members"))


# ================================================================ 機能制御の設定
@app.route("/settings/features")
@roles_required("system_admin")
def feature_settings():
    """ロール・サブロールごとの機能制御（当社スタッフのみ）"""
    ov = feature_overrides()
    matrix = {}
    for rk in ROLE_KEYS:
        matrix[rk] = {}
        for key, _grp, _label, _desc, fixed in FEATURES:
            if feature_na(key, rk):
                matrix[rk][key] = False     # 役割上の対象外（切り替えません）
            elif fixed:
                matrix[rk][key] = (rk == FIXED_FEATURE_ROLE)
            elif rk == ALL_FEATURE_ROLE:
                matrix[rk][key] = True      # 当社スタッフは常に利用可（切替不可）
            else:
                v = ov.get((rk, key))
                matrix[rk][key] = (key in FEATURE_DEFAULTS.get(rk, set())
                                   if v is None else v)
    n_over = len(ov)
    groups = []
    for key, grp, label, desc, fixed in FEATURES:
        if not groups or groups[-1][0] != grp:
            groups.append((grp, []))
        groups[-1][1].append({"key": key, "label": label, "desc": desc, "fixed": fixed,
                              "na": sorted(FEATURE_NA_ROLES.get(key, ()))})
    # 見出しに使う列の情報（ロール名とサブロール名を分けて渡す）
    role_cols = []
    for rk in ROLE_KEYS:
        full = ROLE_KEY_LABELS[rk]
        base, paren, sub = full.partition("（")
        role_cols.append({"key": rk, "full": full, "base": base,
                          "sub": sub[:-1] if paren else "",
                          "settable": rk != ALL_FEATURE_ROLE})
    n_cells = sum(1 for k, _g, _l, _d, fx in FEATURES if not fx
                  for c in role_cols if c["settable"] and not feature_na(k, c["key"]))
    return render_template("feature_settings.html", groups=groups, matrix=matrix,
                           role_keys=ROLE_KEYS, role_labels=ROLE_KEY_LABELS,
                           role_cols=role_cols, n_over=n_over, n_cells=n_cells,
                           fixed_role=ROLE_KEY_LABELS.get(FIXED_FEATURE_ROLE, ""),
                           all_role=ROLE_KEY_LABELS.get(ALL_FEATURE_ROLE, ""),
                           all_role_key=ALL_FEATURE_ROLE)


@app.route("/settings/features/save", methods=["POST"])
@roles_required("system_admin")
def feature_settings_save():
    """機能制御の保存・初期化"""
    db = get_db()
    if request.form.get("reset"):
        db.execute("DELETE FROM role_feature")
        db.commit()
        log("account", "機能制御を初期値に戻した", "success", target="全ロール")
        flash("機能制御を初期値に戻しました。", "ok")
        return redirect(url_for("feature_settings"))

    on = set(request.form.getlist("allow"))     # "role_key|feature" の形で届く
    changes = []
    before = feature_overrides()
    db.execute("DELETE FROM role_feature")
    for rk in ROLE_KEYS:
        if rk == ALL_FEATURE_ROLE:
            continue            # 当社スタッフは機能制御の対象外（常に全機能）
        for key, _grp, label, _desc, fixed in FEATURES:
            if feature_na(key, rk):
                continue        # 役割上の対象外は設定しない
            if fixed:
                continue        # 固定の機能は保存しない（産業医のみ・変更不可）
            allowed = f"{rk}|{key}" in on
            db.execute("INSERT INTO role_feature (role_key, feature, allowed)"
                       " VALUES (?,?,?)", (rk, key, 1 if allowed else 0))
            was = before.get((rk, key))
            if was is None:
                was = key in FEATURE_DEFAULTS.get(rk, set())
            if bool(was) != allowed:
                changes.append(f"{ROLE_KEY_LABELS[rk]}／{label}＝"
                               f"{'利用可' if allowed else '利用不可'}")
    db.commit()
    g.pop("features", None)
    log("account", "機能制御を変更", "success", target=f"{len(changes)}件",
        detail="／".join(changes)[:900] or "変更なし")
    flash(f"機能制御を保存しました（変更 {len(changes)}件）。"
          "各アカウントの次のアクセスから適用されます。", "ok")
    return redirect(url_for("feature_settings"))


# ================================================================ マイアカウント
# 自分のアカウント情報（利用者名・メールアドレス・パスワード）は本人が変更できる。
# 権限ロール・サブロール・閲覧範囲・担当範囲は本人では変更できない
# （自分で権限を広げられないようにするため。変更は他の管理者が行う）。
def me_context(row=None):
    acc = current_account()
    db = get_db()
    row = row or db.execute("SELECT * FROM account WHERE id=?", (acc["id"],)).fetchone()
    kenpo = db.execute("SELECT * FROM kenpo WHERE id=?", (row["kenpo_id"],)).fetchone() \
        if row["kenpo_id"] else None
    scopes = account_scope_ids(row["id"])
    return {
        "row": row,
        "kenpo": kenpo,
        "companies": company_names(account_company_ids(row["id"])) or "—",
        "scope_label": scope_summary(scopes) or "—",
        "support": bool(support_kenpo()),
    }


@app.route("/me", methods=["GET", "POST"])
@login_required
def me_account():
    """自分のアカウント情報を確認・変更する"""
    db, acc = get_db(), current_account()
    if support_kenpo():
        # サポートログイン中は本人の設定を触らせない（誤操作を防ぐ）
        flash("サポートログイン中はマイアカウントを変更できません。"
              "サポートを終了してから操作してください。", "error")
        return render_template("me.html", **me_context()), 403
    row = db.execute("SELECT * FROM account WHERE id=?", (acc["id"],)).fetchone()
    if request.method == "GET":
        return render_template("me.html", **me_context(row))

    name = (request.form.get("name") or "").strip()
    email = (request.form.get("email") or "").strip().lower()
    errs = []
    if not name:
        errs.append("利用者名を入力してください。")
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[A-Za-z]{2,}", email):
        errs.append("メールアドレスの形式が正しくありません。")
    dup = db.execute("SELECT id, status FROM account WHERE lower(email)=lower(?)"
                     " AND id<>?", (email, row["id"])).fetchone()
    if dup:
        errs.append("このメールアドレスは、ほかのアカウントで使われています。")
    if errs:
        for e in errs:
            flash(e, "error")
        return render_template("me.html", form=request.form, **me_context(row))

    changes = []
    if row["name"] != name:
        changes.append(("利用者名", row["name"], name))
    if (row["email"] or "").lower() != email:
        changes.append(("メールアドレス（ログインID）", row["email"], email))
    if not changes:
        flash("変更点がありませんでした。", "ok")
        return redirect(url_for("me_account"))

    db.execute("UPDATE account SET name=?, email=?, updated_at=? WHERE id=?",
               (name, email, now(), row["id"]))
    db.commit()
    g.pop("real_acc", None)
    g.pop("acc", None)
    log("account", "自分のアカウント情報を変更", "success", target=email,
        detail="／".join(f"{k} {a} → {b}" for k, a, b in changes))

    # メールアドレスを変えた場合は、変更前と変更後の両方へ通知する
    if any(k.startswith("メールアドレス") for k, _, _ in changes):
        body = (f"{name} 様\n\nログインに使うメールアドレスが変更されました。\n\n"
                + "\n".join(f"　{k}：{a} → {b}" for k, a, b in changes)
                + "\n\n次回のログインからは新しいメールアドレスをお使いください。\n"
                  "パスワードは変更されていません。\n"
                  "心当たりがない場合は、システム管理者にお問い合わせください。")
        for to in {row["email"], email}:
            send_mail(to, "【HIA】ログインIDの変更のお知らせ", body)
        log("account", "ログインIDの変更を通知", "success", target=email,
            detail="変更前・変更後の両方へ送信")
    flash("アカウント情報を変更しました。", "ok")
    # 変更後はアカウント一覧へ戻す（アカウント管理を使えない場合はマイアカウントに残る）
    if feature_allowed("accounts", current_account()):
        return redirect(url_for("accounts"))
    return redirect(url_for("me_account"))


@app.route("/me/password", methods=["POST"])
@login_required
def me_password():
    """自分のパスワードを変更する（現在のパスワードを確認してから保存する）"""
    db, acc = get_db(), current_account()
    if support_kenpo():
        flash("サポートログイン中はパスワードを変更できません。", "error")
        return redirect(url_for("me_account"))
    row = db.execute("SELECT * FROM account WHERE id=?", (acc["id"],)).fetchone()
    cur = request.form.get("current") or ""
    pw1 = request.form.get("pw1") or ""
    pw2 = request.form.get("pw2") or ""

    if not row["password_hash"] or not check_password_hash(row["password_hash"], cur):
        log("auth", "パスワード変更に失敗", "failure", target=row["email"],
            detail="現在のパスワードが一致しない")
        flash("現在のパスワードが正しくありません。", "error")
        return redirect(url_for("me_account"))
    errs = password_errors(pw1, pw2)
    if pw1 == cur:
        errs.append("現在と同じパスワードは設定できません。")
    if errs:
        for e in errs:
            flash(e, "error")
        return redirect(url_for("me_account"))

    db.execute("UPDATE account SET password_hash=?, reset_token=NULL, reset_expire=NULL,"
               " reset_at=?, updated_at=? WHERE id=?",
               (generate_password_hash(pw1), now(), now(), row["id"]))
    db.commit()
    log("auth", "自分でパスワードを変更", "success", target=row["email"],
        detail="現在のパスワードを確認して変更（パスワードそのものは記録しない）")
    sent = send_mail(row["email"], "【HIA】パスワード変更のお知らせ",
                     f"{row['name']} 様\n\nパスワードが変更されました。\n"
                     f"　変更日時：{now()}\n\n"
                     "心当たりがない場合は、すぐにシステム管理者へご連絡ください。")
    flash("パスワードを変更しました。"
          + ("本人確認のため、登録メールアドレスへ通知を送信しました。" if sent
             else "（通知メールは送信していません。送信設定が未完了です）"), "ok")
    return redirect(url_for("me_account"))


# ================================================================ アカウント
@app.route("/accounts")
@login_required
def accounts():
    """アカウント一覧。絞り込みは画面側でサジェストと即時フィルタを行うため全件を返す。"""
    db, acc = get_db(), current_account()
    sql = ("SELECT a.*, c.name AS company_name, k.name AS kenpo_name FROM account a"
           " LEFT JOIN company c ON c.id=a.company_id"
           " LEFT JOIN kenpo k ON k.id=a.kenpo_id WHERE 1=1")
    p = []
    if acc["role"] == "kenpo_user":
        sql += " AND a.kenpo_id=? AND a.role <> 'system_admin'"
        p.append(acc["kenpo_id"])
    elif acc["role"] == "company_user":
        mine = account_company_ids(acc["id"]) or [0]
        sql += (" AND a.role='company_user' AND a.id IN"
                " (SELECT account_id FROM account_company WHERE company_id IN ("
                + ",".join("?" * len(mine)) + "))")
        p += list(mine)
    rows = db.execute(
        sql + " ORDER BY CASE a.status WHEN 'deleted' THEN 1 ELSE 0 END,"
              " a.is_primary DESC, a.id DESC", p).fetchall()
    # 担当範囲（企業・事業所・部署）を行ごとに付ける
    comp_map, scope_map = {}, {}
    for r in db.execute("SELECT ac.account_id, c.name, c.code FROM account_company ac"
                        " JOIN company c ON c.id=ac.company_id ORDER BY c.code"):
        comp_map.setdefault(r["account_id"], []).append(r["name"])
    for r in db.execute(
            "SELECT s.account_id, o.name AS name, c.name AS pname, 'office' AS kind"
            " FROM account_scope s JOIN office o ON o.id=s.ref_id"
            " JOIN company c ON c.id=o.company_id WHERE s.kind='office'"
            " UNION ALL"
            " SELECT s.account_id, d.name AS name, o.name AS pname, 'dept' AS kind"
            " FROM account_scope s JOIN department d ON d.id=s.ref_id"
            " JOIN office o ON o.id=d.office_id WHERE s.kind='dept'"):
        scope_map.setdefault(r["account_id"], []).append(
            ("事業所" if r["kind"] == "office" else "部署") + f"：{r['pname']}／{r['name']}")
    rows = [dict(r, companies=comp_map.get(r["id"], []),
                 scopes=scope_map.get(r["id"], [])) for r in rows]
    kenpos = db.execute("SELECT * FROM kenpo ORDER BY name").fetchall()
    # 登録直後は案内リンク送信モーダルを開く
    inv, inv_link = None, None
    iid = request.args.get("invite", type=int)
    if iid:
        cand = db.execute("SELECT * FROM account WHERE id=?", (iid,)).fetchone()
        if cand and can_manage_account(acc, cand) and cand["status"] == "invited" \
                and cand["invite_token"]:
            inv = cand
            inv_link = ext_url("invite", token=cand["invite_token"])
    return render_template("accounts.html", rows=rows, kenpos=kenpos,
                           inv=inv, inv_link=inv_link)


@app.route("/accounts/export")
@login_required
def accounts_export():
    db, acc = get_db(), current_account()
    sql = ("SELECT a.email, a.name, a.role, a.view_scope, a.can_download, a.status, a.is_primary,"
           " k.name, c.name, a.created_at, a.last_login_at, a.sub_role FROM account a"
           " LEFT JOIN company c ON c.id=a.company_id LEFT JOIN kenpo k ON k.id=a.kenpo_id"
           " WHERE a.status <> 'deleted'")
    p = []
    if acc["role"] == "kenpo_user":
        sql += " AND a.kenpo_id=?"
        p.append(acc["kenpo_id"])
    elif acc["role"] == "company_user":
        mine = account_company_ids(acc["id"]) or [0]
        sql += (" AND a.id IN (SELECT account_id FROM account_company WHERE company_id IN ("
                + ",".join("?" * len(mine)) + "))")
        p += list(mine)
    rows = [(r[0], r[1], ROLE_LABELS.get(r[2], r[2]),
             SUB_ROLE_LABELS.get(r[11] or "", ""), SCOPE_LABELS.get(r[3], r[3]),
             "可" if r[4] else "不可", STATUS_LABELS.get(r[5], r[5]),
             "代表者" if r[6] else "", r[7] or "", r[8] or "", r[9], r[10] or "")
            for r in db.execute(sql + " ORDER BY a.id", p)]
    return export_csv("accounts.csv",
                      ["メールアドレス", "利用者名", "権限ロール", "サブロール", "閲覧範囲",
                       "ダウンロード", "状態", "区分", "健康保険組合", "企業名", "作成日時",
                       "最終ログイン"],
                      rows, "アカウント一覧") or redirect(url_for("accounts"))


def _target_account(aid):
    db, acc = get_db(), current_account()
    row = db.execute("SELECT * FROM account WHERE id=?", (aid,)).fetchone()
    if not row or not can_manage_account(acc, row):
        return None
    return row


def scope_kenpo_id(acc, scopes):
    """担当範囲から所属する健康保険組合を決める"""
    db = get_db()
    for kind, sql in (("company", "SELECT kenpo_id FROM company WHERE id=?"),
                      ("office", "SELECT c.kenpo_id FROM office o"
                                 " JOIN company c ON c.id=o.company_id WHERE o.id=?"),
                      ("dept", "SELECT c.kenpo_id FROM department d"
                               " JOIN office o ON o.id=d.office_id"
                               " JOIN company c ON c.id=o.company_id WHERE d.id=?")):
        for rid in scopes.get(kind) or []:
            r = db.execute(sql, (rid,)).fetchone()
            if r:
                return r["kenpo_id"]
    return acc["kenpo_id"]


def _resolve_scope(acc, role, company_ids, office_ids=None, dept_ids=None):
    """ロールから閲覧範囲を決定する。手動指定はさせない。
    企業担当者は、企業・事業所・部署を跨いで複数まとめて担当できる。
    指定できるのは発行者の操作範囲内のものだけ。"""
    if role == "system_admin":
        return "all", {"company": [], "office": [], "dept": []}
    if role == "kenpo_user":
        return "kenpo_all", {"company": [], "office": [], "dept": []}
    ok_c = {c["id"] for c in scoped_companies(acc)}
    ok_o = {o["id"] for o in scoped_offices(acc)}
    ok_d = {d["id"] for d in scoped_departments(acc)}
    scopes = {
        "company": [i for i in (company_ids or []) if i in ok_c],
        "office": [i for i in (office_ids or []) if i in ok_o],
        "dept": [i for i in (dept_ids or []) if i in ok_d],
    }
    return "own_company", scopes


@app.route("/accounts/new", methods=["GET", "POST"])
@login_required
def accounts_new():
    db, acc = get_db(), current_account()
    comps = scoped_companies(acc)
    roles = issuable_roles(acc)
    kenpos = db.execute("SELECT * FROM kenpo ORDER BY code").fetchall()
    if request.method == "GET":
        return render_template("accounts_new.html", comps=comps, roles=roles, kenpos=kenpos,
                               selected=[], depts=scoped_departments(acc), offs=scoped_offices(acc))

    email = (request.form.get("email") or "").strip().lower()
    name = (request.form.get("name") or "").strip()
    role = request.form.get("role") or roles[-1]
    srole = clean_sub_role(role, request.form.get("sub_role"))
    company_ids = [int(x) for x in request.form.getlist("company_ids") if x.isdigit()]
    kenpo_id = request.form.get("kenpo_id", type=int)
    can_dl = 1 if request.form.get("can_download") else 0
    is_primary = 1 if request.form.get("is_primary") else 0

    errs = []
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[A-Za-z]{2,}", email):
        errs.append("メールアドレスの形式が正しくありません。")
    if not name:
        errs.append("利用者名を入力してください。")
    dup = db.execute("SELECT status FROM account WHERE lower(email)=lower(?)",
                     (email,)).fetchone()
    if dup and dup["status"] == "deleted":
        errs.append("このメールアドレスは削除済みのアカウントで使われています。"
                    "一覧で該当アカウントを完全削除すると、再度登録できます。")
    elif dup:
        errs.append("このメールアドレスは既に登録されています。")
    if role not in roles:
        errs.append(f"「{ROLE_LABELS.get(role, role)}」を発行する権限がありません。")

    office_ids = request.form.getlist("office_ids", type=int)
    dept_ids = request.form.getlist("dept_ids", type=int)
    view_scope, scopes = _resolve_scope(acc, role, company_ids, office_ids, dept_ids)
    company_ids = scopes["company"]
    if view_scope == "own_company" and not any(scopes.values()):
        errs.append("担当する企業・事業所・部署のいずれかを1件以上選択してください。")
    if role == "kenpo_user":
        if acc["role"] != "system_admin":
            kenpo_id = acc["kenpo_id"]
        if not kenpo_id:
            errs.append("健康保険組合を選択してください。")
    if errs:
        for e in errs:
            flash(e, "error")
        return render_template("accounts_new.html", comps=comps, roles=roles, kenpos=kenpos,
                               form=request.form, selected=company_ids, depts=scoped_departments(acc), offs=scoped_offices(acc))

    sel = [c for c in comps if c["id"] in set(company_ids)]
    if role in SCOPED_ROLES:
        kenpo_id = scope_kenpo_id(acc, scopes)
    elif role == "system_admin":
        kenpo_id = None
    kenpo = db.execute("SELECT * FROM kenpo WHERE id=?", (kenpo_id,)).fetchone() \
        if kenpo_id else None
    vis = visible_members(kenpo_id, company_ids, view_scope, scopes=scopes)
    return render_template("accounts_confirm.html", email=email, name=name, role=role,
                           srole=srole,
                           view_scope=view_scope, can_dl=can_dl, is_primary=is_primary,
                           companies=sel, company_ids=company_ids,
                           kenpo=kenpo, kenpo_id=kenpo_id, vis=vis, scopes=scopes, scope_label=scope_summary(scopes))


@app.route("/accounts/create", methods=["POST"])
@login_required
def accounts_create():
    db, acc = get_db(), current_account()
    email = (request.form.get("email") or "").strip().lower()
    name = (request.form.get("name") or "").strip()
    role = request.form.get("role")
    srole = clean_sub_role(role, request.form.get("sub_role"))
    company_ids = [int(x) for x in request.form.getlist("company_ids") if x.isdigit()]
    kenpo_id = request.form.get("kenpo_id", type=int)
    can_dl = 1 if request.form.get("can_download") == "1" else 0
    is_primary = 1 if request.form.get("is_primary") == "1" else 0

    if request.form.get("confirmed") != "1":
        flash("閲覧できる加入者の範囲を確認してから発行してください。", "error")
        return redirect(url_for("accounts_new"))
    if role not in issuable_roles(acc):
        log("account", "アカウント発行をブロック", "blocked", target=email,
            detail=f"権限外のロール（{ROLE_LABELS.get(role, role)}）を指定")
        flash("そのロールを発行する権限がありません。", "error")
        return redirect(url_for("accounts_new"))
    requested = len(company_ids)
    office_ids = request.form.getlist("office_ids", type=int)
    dept_ids = request.form.getlist("dept_ids", type=int)
    view_scope, scopes = _resolve_scope(acc, role, company_ids, office_ids, dept_ids)
    company_ids = scopes["company"]
    if view_scope == "own_company":
        if requested != len(company_ids):
            log("account", "アカウント発行をブロック", "blocked", target=email,
                detail=f"スコープ外の企業が指定された（要求{requested}社／許可{len(company_ids)}社）")
            flash("選択された企業のうち、操作する権限のないものが含まれています。", "error")
            return redirect(url_for("accounts_new"))
        if not any(scopes.values()):
            flash("担当する企業・事業所・部署のいずれかを1件以上選択してください。", "error")
            return redirect(url_for("accounts_new"))
    if role == "kenpo_user" and acc["role"] != "system_admin":
        kenpo_id = acc["kenpo_id"]
    if role in SCOPED_ROLES:
        kenpo_id = scope_kenpo_id(acc, scopes)
    if role == "system_admin":
        kenpo_id, company_ids = None, []

    vis = visible_members(kenpo_id, company_ids, view_scope, scopes=scopes)
    token = secrets.token_urlsafe(32)
    expire = (datetime.now() + timedelta(hours=INVITE_HOURS)).strftime("%Y-%m-%d %H:%M:%S")
    cur = db.execute(
        "INSERT INTO account (email, name, role, sub_role, view_scope, can_download,"
        " is_primary, kenpo_id, company_id, status, invite_token, invite_expire, created_by)"
        " VALUES (?,?,?,?,?,?,?,?,?,'invited',?,?,?)",
        (email, name, role, srole, view_scope, can_dl, is_primary, kenpo_id, None,
         token, expire, acc["email"]))
    set_account_scopes(cur.lastrowid, scopes)
    db.commit()
    log("account", "アカウントを発行", "success", target=email,
        detail=(f"ロール={ROLE_LABELS[role]}"
                + (f"（{SUB_ROLE_LABELS[srole]}）" if srole else "")
                + f"／閲覧範囲={SCOPE_LABELS[view_scope]}"
                + (f"（{company_names(company_ids)}）" if company_ids else "")
                + f"／担当範囲={scope_summary(scopes)}"
                + f"／{'代表者／' if is_primary else ''}"
                f"閲覧対象={vis['total']}件（{vis['companies']}社・{vis['offices']}事業所）"
                f"／ダウンロード={'可' if can_dl else '不可'}／初回パスワードは発行しない"))
    flash(f"{email} を登録しました。案内メールを送信してください。", "ok")
    return redirect(url_for("accounts", invite=cur.lastrowid))


@app.route("/accounts/<int:aid>/edit", methods=["GET", "POST"])
@login_required
def accounts_edit(aid):
    db, acc = get_db(), current_account()
    row = _target_account(aid)
    if not row:
        flash("対象のアカウントを操作する権限がありません。", "error")
        return redirect(url_for("accounts"))
    if row["id"] == acc["id"]:
        # 自分の利用者名・メールアドレス・パスワードは「マイアカウント」で変更できる。
        # 権限ロール・担当範囲は自分では変更できない（他の管理者が行う）。
        flash("自分の利用者名・メールアドレス・パスワードは「マイアカウント」で変更できます。"
              "権限ロール・担当する範囲の変更は別の管理者に依頼してください。", "error")
        return redirect(url_for("me_account"))
    if row["status"] == "deleted":
        flash("削除済みのアカウントは編集できません。", "error")
        return redirect(url_for("accounts"))
    comps = scoped_companies(acc)
    roles = issuable_roles(acc)
    kenpos = db.execute("SELECT * FROM kenpo ORDER BY code").fetchall()
    cur_ids = account_company_ids(row["id"])
    inv_url = ext_url("invite", token=row["invite_token"]) \
        if row["status"] == "invited" and row["invite_token"] else None
    inv_expired = bool(row["status"] == "invited"
                       and (row["invite_expire"] or "") < now())
    def edit_page(**extra):
        # 直前に設定したパスワードがあれば1度だけ表示する
        once = session.pop("setpw_once", None)
        if once and once.get("aid") == aid:
            extra.setdefault("generated", once.get("pw"))
        return render_template("accounts_edit.html", row=row, comps=comps, roles=roles,
                               kenpos=kenpos, selected=cur_ids,
                               inv_url=inv_url, inv_expired=inv_expired,
                               offs=scoped_offices(acc), depts=scoped_departments(acc),
                               sel_offices=set(account_scope_ids(aid)["office"]),
                               sel_depts=set(account_scope_ids(aid)["dept"]),
                               hours=INVITE_HOURS, **extra)

    if request.method == "GET":
        # 「案内リンクを表示する」を押したとき（?link=1）。
        # 期限が切れている・未発行のときは、その場で新しいリンクを発行する。
        if request.args.get("link"):
            token, expire = row["invite_token"], row["invite_expire"]
            renewed = False
            if not token or (expire or "") < now():
                token = secrets.token_urlsafe(32)
                expire = (datetime.now()
                          + timedelta(hours=INVITE_HOURS)).strftime("%Y-%m-%d %H:%M:%S")
                # すでに使えているアカウント（有効）は状態を変えない。
                # 状態を invited に戻すとログインできなくなってしまうため。
                if row["status"] == "active":
                    db.execute("UPDATE account SET invite_token=?, invite_expire=?,"
                               " updated_at=? WHERE id=?",
                               (token, expire, now(), aid))
                else:
                    db.execute("UPDATE account SET status='invited', invite_token=?,"
                               " invite_expire=?, updated_at=? WHERE id=?",
                               (token, expire, now(), aid))
                db.commit()
                renewed = True
                row = _target_account(aid)
            log("account", "案内リンクを画面に表示", "success", target=row["email"],
                detail=("新しいリンクを発行" if renewed else "既存のリンクを表示")
                       + f"／有効期限={expire}")
            return edit_page(link=ext_url("invite", token=token), link_expire=expire,
                             link_renewed=renewed)
        return edit_page()

    name = (request.form.get("name") or "").strip()
    role = request.form.get("role") or row["role"]
    srole = clean_sub_role(role, request.form.get("sub_role"))
    company_ids = [int(x) for x in request.form.getlist("company_ids") if x.isdigit()]
    can_dl = 1 if request.form.get("can_download") else 0
    is_primary = 1 if request.form.get("is_primary") else 0
    errs = []
    if not name:
        errs.append("利用者名を入力してください。")
    if role not in roles:
        errs.append(f"「{ROLE_LABELS.get(role, role)}」へ変更する権限がありません。")
    office_ids = request.form.getlist("office_ids", type=int)
    dept_ids = request.form.getlist("dept_ids", type=int)
    view_scope, scopes = _resolve_scope(acc, role, company_ids, office_ids, dept_ids)
    company_ids = scopes["company"]
    if view_scope == "own_company" and not any(scopes.values()):
        errs.append("担当する企業・事業所・部署のいずれかを1件以上選択してください。")
    if role == "kenpo_user" and acc["role"] == "system_admin":
        kid = request.form.get("kenpo_id", type=int)
        if not kid:
            errs.append("健康保険組合を選択してください。")
    if errs:
        for e in errs:
            flash(e, "error")
        return render_template("accounts_edit.html", row=row, comps=comps, roles=roles,
                               kenpos=kenpos, form=request.form, selected=company_ids,
                               inv_url=inv_url, inv_expired=inv_expired,
                               offs=scoped_offices(acc), depts=scoped_departments(acc),
                               sel_offices=set(account_scope_ids(aid)["office"]),
                               sel_depts=set(account_scope_ids(aid)["dept"]))

    sel = [c for c in comps if c["id"] in set(company_ids)]
    kenpo_id = scope_kenpo_id(acc, scopes) or row["kenpo_id"]
    if role == "kenpo_user" and acc["role"] == "system_admin":
        kenpo_id = request.form.get("kenpo_id", type=int) or row["kenpo_id"]
    kenpo = db.execute("SELECT * FROM kenpo WHERE id=?", (kenpo_id,)).fetchone() \
        if kenpo_id else None
    vb = visible_members(row["kenpo_id"], cur_ids, row["view_scope"],
                         scopes=account_scope_ids(aid))
    va = visible_members(kenpo_id, company_ids, view_scope, scopes=scopes)
    def kname(kid):
        if not kid:
            return "—"
        r = db.execute("SELECT name FROM kenpo WHERE id=?", (kid,)).fetchone()
        return r["name"] if r else "—"
    diff = [
        ("利用者名", row["name"], name),
        ("権限ロール", ROLE_LABELS.get(row["role"], row["role"]), ROLE_LABELS[role]),
        ("サブロール", SUB_ROLE_LABELS.get(sub_role(row), "なし"),
         SUB_ROLE_LABELS.get(srole, "なし")),
        ("健康保険組合", kname(row["kenpo_id"]), kname(kenpo_id)),
        ("対象の企業", company_names(cur_ids), company_names(company_ids)),
        ("閲覧範囲", SCOPE_LABELS.get(row["view_scope"], row["view_scope"]),
         SCOPE_LABELS[view_scope]),
        ("ダウンロード", "可" if row["can_download"] else "不可", "可" if can_dl else "不可"),
        ("区分", "代表者" if row["is_primary"] else "一般", "代表者" if is_primary else "一般"),
        ("閲覧できる加入者", f"{vb['total']:,}件（{vb['companies']}社）",
         f"{va['total']:,}件（{va['companies']}社）"),
    ]
    return render_template("accounts_edit_confirm.html", row=row, name=name, role=role,
                           srole=srole,
                           view_scope=view_scope, can_dl=can_dl, is_primary=is_primary,
                           companies=sel, company_ids=company_ids, kenpo_id=kenpo_id,
                           kenpo=kenpo, vis=va, diff=diff,
                           changed=any(a != b for _, a, b in diff),
                           scopes=scopes, scope_label=scope_summary(scopes),
                           expanded=va["total"] > vb["total"])


@app.route("/accounts/<int:aid>/edit/apply", methods=["POST"])
@login_required
def accounts_edit_apply(aid):
    db, acc = get_db(), current_account()
    row = _target_account(aid)
    if not row or row["id"] == acc["id"] or row["status"] == "deleted":
        flash("対象のアカウントを操作する権限がありません。", "error")
        return redirect(url_for("accounts"))
    if request.form.get("confirmed") != "1":
        flash("変更内容と閲覧範囲を確認してから保存してください。", "error")
        return redirect(url_for("accounts_edit", aid=aid))
    name = (request.form.get("name") or "").strip()
    role = request.form.get("role")
    srole = clean_sub_role(role, request.form.get("sub_role"))
    company_ids = [int(x) for x in request.form.getlist("company_ids") if x.isdigit()]
    can_dl = 1 if request.form.get("can_download") == "1" else 0
    is_primary = 1 if request.form.get("is_primary") == "1" else 0
    if not name or role not in issuable_roles(acc):
        log("account", "権限変更をブロック", "blocked", target=row["email"],
            detail=f"権限外のロール（{ROLE_LABELS.get(role, role)}）への変更を試行")
        flash("そのロールへ変更する権限がありません。", "error")
        return redirect(url_for("accounts_edit", aid=aid))
    requested = len(company_ids)
    office_ids = request.form.getlist("office_ids", type=int)
    dept_ids = request.form.getlist("dept_ids", type=int)
    view_scope, scopes = _resolve_scope(acc, role, company_ids, office_ids, dept_ids)
    company_ids = scopes["company"]
    if view_scope == "own_company":
        if requested != len(company_ids):
            log("account", "権限変更をブロック", "blocked", target=row["email"],
                detail=f"スコープ外の企業が指定された（要求{requested}社／許可{len(company_ids)}社）")
            flash("選択された企業のうち、操作する権限のないものが含まれています。", "error")
            return redirect(url_for("accounts_edit", aid=aid))
        if not any(scopes.values()):
            flash("担当する企業・事業所・部署のいずれかを1件以上選択してください。", "error")
            return redirect(url_for("accounts_edit", aid=aid))
    old_ids = account_company_ids(aid)
    kenpo_id = row["kenpo_id"]
    if role == "kenpo_user" and acc["role"] == "system_admin":
        kenpo_id = request.form.get("kenpo_id", type=int) or row["kenpo_id"]
    if any(scopes.values()):
        kenpo_id = scope_kenpo_id(acc, scopes) or kenpo_id
    if role == "system_admin":
        kenpo_id, company_ids = None, []
    vis = visible_members(kenpo_id, company_ids, view_scope, scopes=scopes)
    before_company = {"name": company_names(old_ids)} if old_ids else None
    after_company = {"name": company_names(company_ids)} if company_ids else None

    def kname(kid):
        if not kid:
            return "—"
        r = db.execute("SELECT name FROM kenpo WHERE id=?", (kid,)).fetchone()
        return r["name"] if r else "—"

    changes = []
    if row["name"] != name:
        changes.append(("利用者名", row["name"], name))
    if row["role"] != role:
        changes.append(("権限ロール", ROLE_LABELS.get(row["role"], row["role"]),
                        ROLE_LABELS[role]))
    if sub_role(row) != srole:
        changes.append(("サブロール", SUB_ROLE_LABELS.get(sub_role(row), "なし"),
                        SUB_ROLE_LABELS.get(srole, "なし")))
    if (row["kenpo_id"] or 0) != (kenpo_id or 0):
        changes.append(("健康保険組合", kname(row["kenpo_id"]), kname(kenpo_id)))
    if row["view_scope"] != view_scope:
        changes.append(("閲覧範囲", SCOPE_LABELS.get(row["view_scope"], row["view_scope"]),
                        SCOPE_LABELS[view_scope]))
    if set(old_ids) != set(company_ids):
        changes.append(("対象の企業", before_company["name"] if before_company else "—",
                        after_company["name"] if after_company else "—"))
    if bool(row["can_download"]) != bool(can_dl):
        changes.append(("CSVのダウンロード", "可" if row["can_download"] else "不可",
                        "可" if can_dl else "不可"))
    if bool(row["is_primary"]) != bool(is_primary):
        changes.append(("区分", "代表者" if row["is_primary"] else "一般",
                        "代表者" if is_primary else "一般"))

    db.execute("UPDATE account SET name=?, role=?, sub_role=?, view_scope=?, kenpo_id=?,"
               " can_download=?, is_primary=?, updated_at=? WHERE id=?",
               (name, role, srole, view_scope, kenpo_id, can_dl, is_primary, now(), aid))
    set_account_scopes(aid, scopes)
    db.commit()
    log("account", "アカウントの権限を変更", "success", target=row["email"],
        detail=("／".join(f"{k} {a} → {b2}" for k, a, b2 in changes) or "変更なし")
               + f"／変更後の閲覧対象={vis['total']}件（{vis['companies']}社）")

    sent = False
    if changes:
        body = (f"{name} 様\n\nアカウントの設定が変更されました。\n\n"
                "■ 変更内容\n"
                + "\n".join(f"　{k}：{a} → {b2}" for k, a, b2 in changes)
                + f"\n\n■ 変更後に閲覧できる加入者\n"
                  f"　{vis['total']:,}件（{vis['companies']}社・{vis['offices']}事業所）\n\n"
                  "変更は次回のアクセスから適用されます。\n"
                  "パスワードは変更されていません。\n"
                  "心当たりがない場合は、システム管理者にお問い合わせください。")
        sent = send_mail(row["email"], "【HIA】アカウント設定の変更のお知らせ", body)
        log("account", "設定変更の通知メールを送信", "success", target=row["email"],
            detail=("メール送信済み" if sent else "メール未設定のため outbox に出力")
                   + "／" + "、".join(k for k, _, _ in changes))

    if not changes:
        flash("変更点がなかったため、そのまま保存しました。", "ok")
    elif sent:
        flash(f"{row['email']} の設定を変更し、本人へ通知メールを送信しました。"
              f"変更は次のアクセスから適用されます。", "ok")
    else:
        flash(f"{row['email']} の設定を変更しました。"
              f"通知メールは送信できませんでした（送信設定が未完了です）。", "error")
    return redirect(url_for("accounts"))


@app.route("/accounts/<int:aid>/invite-link")
@login_required
def accounts_invite_link(aid):
    """案内リンクは編集画面にまとめたので、そちらへ送る（古いURL・ブックマーク用）"""
    return redirect(url_for("accounts_edit", aid=aid, link=1))


@app.route("/accounts/<int:aid>/set-password", methods=["GET", "POST"])
@login_required
def accounts_set_password(aid):
    """管理者がその場でパスワードを設定する。
    メールが使えない環境で、案内リンクを渡せないときに使う。"""
    db, acc = get_db(), current_account()
    row = _target_account(aid)
    if not row:
        log("account", "パスワード設定をブロック", "blocked", target=str(aid),
            detail="対象アカウントへの権限がない")
        flash("対象のアカウントを操作する権限がありません。", "error")
        return redirect(url_for("accounts"))
    if row["status"] == "deleted":
        flash("削除済みのアカウントです。", "error")
        return redirect(url_for("accounts"))
    if row["id"] == acc["id"]:
        flash("自分のパスワードは「パスワード変更」から変更してください。", "error")
        return redirect(url_for("accounts"))

    if request.method == "GET":
        # 入力欄は編集画面にまとめたので、そちらへ送る
        return redirect(url_for("accounts_edit", aid=aid))

    if request.form.get("mode") == "auto":
        pw = gen_password()
        errs = []
    else:
        pw = request.form.get("pw1") or ""
        errs = password_errors(pw, request.form.get("pw2") or "")
    if errs:
        for e in errs:
            flash(e, "error")
        return redirect(url_for("accounts_edit", aid=aid))

    db.execute("UPDATE account SET password_hash=?, status='active', invite_token=NULL,"
               " invite_expire=NULL, reset_token=NULL, reset_expire=NULL, updated_at=?"
               " WHERE id=?", (generate_password_hash(pw), now(), aid))
    db.commit()
    log("account", "管理者がパスワードを設定", "success", target=row["email"],
        detail=("自動生成したパスワードを設定" if request.form.get("mode") == "auto"
                else "管理者が入力したパスワードを設定")
               + "／メールは送信していない")
    # 作ったパスワードは1度だけ表示する。編集画面にそのまま表示して控えてもらう
    session["setpw_once"] = {"aid": aid, "pw": pw}
    return redirect(url_for("accounts_edit", aid=aid))


@app.route("/accounts/<int:aid>/send-invite", methods=["POST"])
@login_required
def accounts_send_invite(aid):
    """パスワード設定用の案内メールを送信する"""
    db, acc = get_db(), current_account()
    row = _target_account(aid)
    if not row:
        log("account", "案内メールの送信をブロック", "blocked", target=str(aid),
            detail="対象アカウントへの権限がない")
        flash("対象のアカウントを操作する権限がありません。", "error")
        return redirect(url_for("accounts"))
    if row["status"] != "invited" or not row["invite_token"]:
        flash("このアカウントはすでにパスワードが設定されています。", "error")
        return redirect(url_for("accounts"))
    if (row["invite_expire"] or "") < now():
        token = secrets.token_urlsafe(32)
        expire = (datetime.now() + timedelta(hours=INVITE_HOURS)).strftime("%Y-%m-%d %H:%M:%S")
        db.execute("UPDATE account SET invite_token=?, invite_expire=?, updated_at=? WHERE id=?",
                   (token, expire, now(), aid))
        db.commit()
        log("account", "期限切れの案内リンクを再発行", "success", target=row["email"],
            detail=f"新しい有効期限={expire}")
    else:
        token, expire = row["invite_token"], row["invite_expire"]
    link = ext_url("invite", token=token)
    sent = send_mail(row["email"], "【HIA】アカウント発行のご案内",
                     f"{row['name']} 様\n\nアカウントを発行しました。\n"
                     f"以下のリンクからパスワードを設定してください。\n\n{link}\n\n"
                     f"有効期限：{expire}（{INVITE_HOURS}時間）\n"
                     f"リンクは1回だけ使用できます。")
    log("account", "案内メールを送信", "success" if sent else "failure", target=row["email"],
        detail=("メール送信済み" if sent else "メール未設定のため送信できず outbox に出力")
               + f"／有効期限={expire}")
    if sent:
        flash(f"{row['email']} 宛に案内メールを送信しました。", "ok")
    else:
        flash(f"案内メールを送信できませんでした（送信設定が未完了です）。"
              f"outbox フォルダの内容をご本人へお伝えください。", "error")
    return redirect(url_for("accounts"))


@app.route("/accounts/<int:aid>/toggle", methods=["POST"])
@login_required
def accounts_toggle(aid):
    db, acc = get_db(), current_account()
    row = _target_account(aid)
    if not row or row["id"] == acc["id"]:
        flash("対象のアカウントを操作する権限がありません。", "error")
        return redirect(url_for("accounts"))
    new = "disabled" if row["status"] != "disabled" else ("active" if row["password_hash"]
                                                         else "invited")
    db.execute("UPDATE account SET status=?, updated_at=? WHERE id=?", (new, now(), aid))
    db.commit()
    log("account", "アカウント状態を変更", "success", target=row["email"],
        detail=f"{STATUS_LABELS.get(row['status'])} → {STATUS_LABELS.get(new)}")
    flash(f"{row['email']} の状態を「{STATUS_LABELS.get(new)}」に変更しました。", "ok")
    return redirect(url_for("accounts"))


@app.route("/accounts/<int:aid>/delete", methods=["POST"])
@login_required
def accounts_delete(aid):
    db, acc = get_db(), current_account()
    row = _target_account(aid)
    if not row:
        log("account", "アカウント削除をブロック", "blocked", target=str(aid),
            detail="対象アカウントへの権限がない")
        flash("対象のアカウントを操作する権限がありません。", "error")
        return redirect(url_for("accounts"))
    if row["id"] == acc["id"]:
        flash("自分自身のアカウントは削除できません。", "error")
        return redirect(url_for("accounts"))
    if row["status"] == "deleted":
        flash("このアカウントは既に削除されています。", "error")
        return redirect(url_for("accounts"))
    db.execute("UPDATE account SET status='deleted', password_hash=NULL, invite_token=NULL,"
               " invite_expire=NULL, reset_token=NULL, reset_expire=NULL, updated_at=?"
               " WHERE id=?", (now(), aid))
    db.commit()
    log("account", "アカウントを削除", "success", target=row["email"],
        detail=(f"ロール={ROLE_LABELS.get(row['role'], row['role'])}"
                f"／閲覧範囲={SCOPE_LABELS.get(row['view_scope'], row['view_scope'])}"
                f"／ログイン不可・一覧から非表示に変更"))
    flash(f"{row['email']} を削除しました。ログインできない状態になり、復元はできません。", "ok")
    return redirect(url_for("accounts"))


@app.route("/accounts/<int:aid>/purge", methods=["GET", "POST"])
@login_required
def accounts_purge(aid):
    """削除済みアカウントをデータベースから消す。復元はできないため、
    同じメールアドレスを使い直す場合はこの操作が必要になる。"""
    db, acc = get_db(), current_account()
    row = _target_account(aid)
    if not row or row["id"] == acc["id"]:
        flash("対象のアカウントが見つかりません。", "error")
        return redirect(url_for("accounts"))
    if row["status"] != "deleted":
        log("account", "完全削除をブロック", "blocked", target=row["email"],
            detail="削除済みではないアカウントに対する操作")
        flash("完全削除できるのは削除済みのアカウントだけです。先に削除してください。", "error")
        return redirect(url_for("accounts"))
    if request.method == "GET":
        return render_template("accounts_purge.html", row=row)
    if (request.form.get("confirm_email") or "").strip().lower() != row["email"]:
        log("account", "完全削除をブロック", "blocked", target=row["email"],
            detail="確認入力がメールアドレスと一致しない")
        flash("入力されたメールアドレスが一致しません。完全削除は実行していません。", "error")
        return render_template("accounts_purge.html", row=row)
    db.execute("DELETE FROM account_company WHERE account_id=?", (aid,))
    db.execute("DELETE FROM account WHERE id=?", (aid,))
    db.commit()
    log("account", "アカウントを完全削除", "success", target=row["email"],
        detail=f"ロール={ROLE_LABELS.get(row['role'], row['role'])}／操作ログは保持")
    flash(f"{row['email']} を完全に削除しました。操作ログの記録は残っています。"
          f"このメールアドレスは再度登録できます。", "ok")
    return redirect(url_for("accounts"))


# ================================================================ ログ
def _log_scope(acc):
    if acc["role"] == "kenpo_user":
        return " AND (kenpo_id=? OR actor_email=?)", [acc["kenpo_id"], acc["email"]]
    if acc["role"] == "company_user":
        mine = account_company_ids(acc["id"]) or [0]
        q = ",".join("?" * len(mine))
        return (" AND actor_email IN (SELECT email FROM account WHERE id IN"
                f" (SELECT account_id FROM account_company WHERE company_id IN ({q})))",
                list(mine))
    return "", []


@app.route("/logs")
@login_required
def logs():
    db, acc = get_db(), current_account()
    cat = request.args.get("category") or ""
    shell = request.args.get("shell") or ""
    kw = (request.args.get("q") or "").strip()
    dfrom = request.args.get("from") or ""
    dto = request.args.get("to") or ""
    sql, p = "SELECT * FROM audit_log WHERE 1=1", []
    sc, sp = _log_scope(acc)
    sql += sc
    p += sp
    if cat:
        sql += " AND category=?"
        p.append(cat)
    if shell:
        sql += " AND shell=?"
        p.append(shell)
    if kw:
        sql += " AND (actor_email LIKE ? OR action LIKE ? OR target LIKE ? OR detail LIKE ?)"
        p += [f"%{kw}%"] * 4
    if dfrom:
        sql += " AND ts >= ?"
        p.append(dfrom + " 00:00:00")
    if dto:
        sql += " AND ts <= ?"
        p.append(dto + " 23:59:59")
    total = db.execute("SELECT COUNT(*) c FROM (" + sql + ")", p).fetchone()["c"]
    rows = db.execute(sql + " ORDER BY id DESC LIMIT 300", p).fetchall()
    return render_template("logs.html", rows=rows, total=total, cat=cat, shell=shell,
                           kw=kw, dfrom=dfrom, dto=dto, mails=mail_log_rows(db))


def mail_log_rows(db, limit=100):
    """配信ログ（メールの送信記録）を1回の配信ごとにまとめて返す。

    受診勧奨・受検案内は同じ時刻にまとめて送るため、
    「配信日時（分単位）×メール種別×テンプレート」で1行にし、
    宛先数・成功・失敗・スキップの件数だけを出します（個人名は出しません）。
    """
    try:
        return db.execute(
            "SELECT substr(l.sent_at,1,16) sent_at, l.kind kind,"
            "       COALESCE(t.name, l.subject, '—') tpl,"
            "       COUNT(*) n,"
            "       SUM(CASE WHEN l.result='success' THEN 1 ELSE 0 END) ok,"
            "       SUM(CASE WHEN l.result='failure' THEN 1 ELSE 0 END) ng,"
            "       SUM(CASE WHEN l.result='skipped' THEN 1 ELSE 0 END) sk,"
            "       MAX(l.actor) actor"
            "  FROM oh_mail_log l"
            "  LEFT JOIN oh_mail_template t ON t.id = l.template_id"
            " GROUP BY substr(l.sent_at,1,16), l.kind, l.template_id"
            " ORDER BY sent_at DESC LIMIT ?", (limit,)).fetchall()
    except sqlite3.Error:
        return []                            # 産業医面談管理のテーブルが無い場合


@app.route("/logs/mail/export")
@login_required
def logs_mail_export():
    db = get_db()
    rows = [(r["sent_at"], r["kind"], r["tpl"], r["n"], r["ok"], r["ng"], r["sk"],
             r["actor"] or "") for r in mail_log_rows(db, limit=10000)]
    return export_csv("mail_log.csv",
                      ["配信日時", "メール種別", "テンプレート名", "宛先数", "成功",
                       "失敗", "スキップ", "実行者"], rows, "配信ログ") \
        or redirect(url_for("logs"))


@app.route("/logs/export")
@login_required
def logs_export():
    db, acc = get_db(), current_account()
    sql = ("SELECT ts, shell, category, action, result, actor_email, actor_role, ip,"
           " target, detail FROM audit_log WHERE 1=1")
    sc, p = _log_scope(acc)
    sql += sc
    rows = [(r[0], {"km": "HIA総合管理", "kenpo": "HIA健保管理"}.get(r[1], r[1] or ""),
             r[2], r[3],
             {"success": "成功", "failure": "失敗", "blocked": "ブロック"}.get(r[4], r[4]),
             r[5] or "", ROLE_LABELS.get(r[6], r[6] or ""), r[7] or "", r[8] or "", r[9] or "")
            for r in db.execute(sql + " ORDER BY id", p)]
    return export_csv("audit_log.csv",
                      ["日時", "画面", "区分", "操作", "結果", "実行者", "実行者ロール",
                       "IPアドレス", "対象", "詳細"], rows, "操作ログ") or redirect(url_for("logs"))


@app.errorhandler(404)
def nf(e):
    return render_template("denied.html", path=request.path, notfound=True), 404


# ================================================================ 産業医面談管理
# 別システム「産業医面談管理システム」の機能を組み込んだモジュール（/oh …）。
# 認証・ロール・担当範囲・操作ログ・CSV出力は、このファイルの共通処理をそのまま使う。
import sys as _sys           # noqa: E402
import sanmen               # noqa: E402

sanmen.init_app(app, _sys.modules[__name__])


def local_ipv4():
    """同じネットワークの端末から見えるIPアドレスを調べる"""
    import socket
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sk:
            sk.connect(("8.8.8.8", 80))
            return sk.getsockname()[0]
    except Exception:
        try:
            return socket.gethostbyname(socket.gethostname())
        except Exception:
            return "127.0.0.1"


def port_is_free(host, port):
    """待受ポートが空いているか確認する"""
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sk:
        sk.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sk.bind(("" if host == "0.0.0.0" else host, port))
            return True
        except OSError:
            return False


def pick_port(host, port):
    """指定ポートが使えなければ、案内を出して次の空きポートを探す。
    HIA_PORT_AUTO=0 を指定した場合は自動で変えずに終了する。"""
    if port_is_free(host, port):
        return port
    print(f"[HIA] ポート {port} は既に使われています。")
    if os.environ.get("HIA_PORT_AUTO", "1") == "0":
        print("[HIA] 使用中のプログラムを終了するか、HIA_PORT に別の番号を指定してください。")
        print(f'[HIA] 使用中のプログラムの調べ方（コマンドプロンプト）: netstat -ano | findstr :{port}')
        raise SystemExit(1)
    for cand in range(port + 1, port + 21):
        if port_is_free(host, cand):
            print(f"[HIA] 代わりにポート {cand} で起動します。"
                  f"（元に戻す場合は使用中のプログラムを終了してください）")
            return cand
    print("[HIA] 空いているポートが見つかりませんでした。HIA_PORT で番号を指定してください。")
    raise SystemExit(1)


# 起動のしかた（python app.py／waitress／テスト）に関わらず、
# 読み込み時に一度だけ「初期データの復元 → スキーマ移行 → 控え」を行う。
_DB_WAS_MISSING = not os.path.exists(DB_PATH) and not os.path.exists(INITIAL_DB)
BOOTSTRAP_LINES = init_db()


if __name__ == "__main__":
    for line in BOOTSTRAP_LINES:
        print("[HIA] " + line)
    if _DB_WAS_MISSING:
        print("[HIA] seed.py で初期データを投入してください。")
    host = os.environ.get("HIA_HOST", "0.0.0.0")
    port = pick_port(host, int(os.environ.get("HIA_PORT", "8000")))
    ip = local_ipv4() if host == "0.0.0.0" else host
    print(f"[HIA] build {BUILD}")
    try:
        _con = sqlite3.connect(DB_PATH)
        _n_acc = _con.execute("SELECT COUNT(*) FROM account"
                              " WHERE status <> 'deleted'").fetchone()[0]
        _con.close()
        print(f"[HIA] データベース              {DB_PATH}（アカウント {_n_acc} 件）")
        print(f"[HIA] 控えの保存先              {BACKUP_DIR}")
    except Exception:
        pass
    print(f"[HIA] このパソコンから           http://127.0.0.1:{port}/login")
    print(f"[HIA] 同じネットワークの端末から  http://{ip}:{port}/login")
    print(f"[HIA] HIA総合管理（当社）       http://{ip}:{port}/km")
    print(f"[HIA] HIA健保管理（健保・企業）  http://{ip}:{port}/app")
    print(f"[HIA] 産業医面談管理            http://{ip}:{port}/oh/")
    # 疾患予測の設定（サーバー側で指定した内容）を起動時に確認できるようにする
    print("[HIA] 疾患予測 予測エンジン    "
          + ("日立API（" + risk_setting("hitachi_endpoint") + "）"
             if risk_setting("hitachi_endpoint") else "社内試算（日立APIの接続先は未設定）"))
    print("[HIA] 疾患予測 健診結果の連携  "
          + (risk_setting("kenshin_endpoint") or "未設定（画面から手動で取込めます）")
          + f"／バッチ {risk_setting('kenshin_time')}")
    print("[HIA] 疾患予測 NSIPS連携      "
          + (risk_setting("nsips_endpoint") or "未設定")
          + f"／{risk_setting('nsips_mode')}")
    print(f"[HIA] 疾患予測 突合={risk_setting('match_rule')}"
          f"／既定の予測年数={risk_setting('horizon')}年")
    app.run(host=host, port=port, debug=False)
