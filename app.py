# -*- coding: utf-8 -*-
"""
HIA アカウント・マスタ管理

画面は2種類、データベースは1つ。
  HIA総合管理 （/km  ・緑）… HIAスタッフが利用
  HIA健保管理 （/app ・青）… 健保担当者・企業担当者が利用

HIA総合管理でできること
  1. 各健保・企業のアカウントの発行
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
import json
import os
import re
from address_split import split_address
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
# 配布ZIPごとの番号（app.py・templates・static がそろっているかの確認用。8-70）。
# templates/_build.txt と static/build.txt にも同じ番号を入れて配布し、違っていれば起動時とログイン画面で知らせる
BUILD_ID = "20260925c"


def build_mismatch():
    """templates／static の版番号が app.py と違えば、その内訳（説明文）を返す。そろっていれば空文字"""
    bad = []
    for label, path in (("templates", os.path.join(BASE_DIR, "templates", "_build.txt")),
                        ("static", os.path.join(BASE_DIR, "static", "build.txt"))):
        try:
            with open(path, encoding="utf-8") as f:
                v = f.read().strip()
        except OSError:
            v = "（無し）"
        if v != BUILD_ID:
            bad.append(f"{label}={v}")
    if not bad:
        return ""
    return (f"プログラム（app.py）は {BUILD_ID} ですが、{'／'.join(bad)} です。"
            "配布ZIPの templates・static フォルダを丸ごと入れ替えてください（hia.db・secret.key は残します）。")

MAX_EXPORT_ROWS = int(os.environ.get("HIA_MAX_EXPORT_ROWS", "1000"))
INVITE_HOURS = 72
RESET_HOURS = 2

ROLE_LABELS = {
    "system_admin": "HIAスタッフ",
    "kenpo_user": "健保担当者",
    "company_user": "企業担当者",
    # 加入者ご本人のログイン。自分の健康情報の確認・写真の取込・
    # メールのやり取りだけを行い、ほかの加入者の情報は見られません。
    "member": "加入者本人",
}
# 企業担当者のサブロール（産業医面談管理で使う）
#   doctor（産業医）… 医学的判断（面談対象の承認・就業区分の判定・面談所見・記名）
#   hr    （人事）  … 運用事務（取込・メール配信・対応区分・報告書の出力）
#   nurse （保健師）… 面談・保健指導・受診勧奨の実施と、対応の記録
SUB_ROLE_LABELS = {"doctor": "産業医", "hr": "人事", "nurse": "保健師"}
SUB_ROLES = ["doctor", "hr", "nurse"]
# アカウントの登録・編集で、サブロールの下に出す説明
SUB_ROLE_DESC = {
    "doctor": "面談の承認・就業区分・面談記録・記名",
    "hr": "取込・メール配信・対応区分・出力",
    "nurse": "保健指導・対応区分・案内メール（判定は行いません）",
}
# サブロールを付けられるロール
SUB_ROLE_ROLES = ("company_user",)

# ================================================================ 機能制御
# ロール（＋サブロール）ごとに、使える機能をHIAスタッフが切り替えられる。
# 「固定」の機能は設定で変更できない（医学的判断は産業医のみが行うため）。
ROLE_KEYS = ["system_admin", "kenpo_user", "company_user",
             "company_user/doctor", "company_user/hr", "company_user/nurse",
             "member"]
ROLE_KEY_LABELS = {
    "system_admin": "HIAスタッフ",
    "kenpo_user": "健保担当者",
    "company_user": "企業担当者（サブロールなし）",
    "company_user/doctor": "企業担当者（産業医）",
    "company_user/hr": "企業担当者（人事）",
    "company_user/nurse": "企業担当者（保健師）",
    "member": "加入者本人",
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
    ("oh.dashboard", "産業医面談", "面談ダッシュボード",
     "面談の進み具合・抽出理由・ステータスの内訳の画面を表示", False),
    ("oh.kenshin", "産業医面談", "健診受診管理（定期・深夜）",
     "年度ごとの受診状況・受診率の確認", False),
    ("oh.approve", "産業医面談", "面談対象の承認・就業区分の判定",
     "医学的判断のため産業医のみ（変更できません）", True),
    ("oh.interview.view", "産業医面談", "面談結果入力の画面を表示",
     "面談結果入力の画面（カード・URL）を出すかどうか。記録できるのは産業医と保健師です", False),
    ("oh.interview", "産業医面談", "面談結果の記録",
     "産業医と保健師のみ（変更できません）。保健師が記録すると対応済みになります", True),
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
    # ---- 健康管理（加入者マスタとは別に、加入者一人ひとりの健康を管理する画面）----
    ("health.view", "健康管理", "加入者健康一覧・マイページ",
     "加入者ごとの健診・時間外・ストレス・面談をまとめて見る一覧と、"
     "加入者マイページ（メモの追記を含む）", False),
    ("health.mail", "健康管理", "加入者へのメール送信",
     "加入者健康一覧から、個人別・一括でメールを送る（テンプレートの編集を含む）", False),
    ("health.self", "健康管理", "本人のマイページ",
     "加入者ご本人が自分の健康情報を見る（内部メモは出しません）", False),
    ("health.self_upload", "健康管理", "本人による写真の取込",
     "加入者ご本人が、採血結果などの写真を自分のマイページから取り込む", False),
    ("health.self_mail", "健康管理", "メールのやり取り",
     "加入者ご本人が、届いたお知らせの履歴を見て問い合わせを送る", False),
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
    ("kenpo.mail", "HIA健保管理", "メール設定",
     "受診案内テンプレートの作成・管理・送信・リマインド（HIA健保管理）", False),
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
     "操作ログの閲覧・絞り込み・CSV出力（画面の表示・取込・出力の履歴）", False),
    ("settings.mail", "設定・サポート", "メール設定",
     "配信テンプレート・配信履歴・リマインド（HIA総合管理。HIA健保管理の「メール設定」と同じ画面）", False),
]
FEATURE_KEYS = [f[0] for f in FEATURES]
_SETTINGS = {"settings.view", "settings.contact", "settings.log", "settings.mail"}
# 担当者が既定で持つ健康管理の機能。「本人のマイページ」は加入者ご本人だけが使うため
# 担当者の既定には入れません（設定で個別に与えることはできます）
_HEALTH = {"health.view", "health.mail"}
# 産業医が使う画面は「面談対象者一覧」と「面談結果入力」だけ。
# 加入者本人が使えるのは自分のマイページ（確認・写真の取込・メールのやり取り）だけ。
# それ以外の機能はロールの役割上「対象外」にする（切り替えず、常に利用不可）。
# 産業医は面談の2画面に加えて、労基署報告（様式第6号・ストレスチェック結果等報告書）を
# 開いて内容を確認し、記名（サイン）します。出力・提出は人事が行います。
_DOCTOR_OK = {"oh.list", "oh.approve",
              "oh.interview.view", "oh.interview", "oh.sign", "oh.report"}
_MEMBER_OK = {"health.self", "health.self_upload", "health.self_mail"}
# 人事にとって対象外の機能。
# 「産業医面談 管理」のカテゴリ（面談ダッシュボード・面談対象者一覧・面談結果入力）は
# 産業医が使う画面なので、人事には出しません。
# 人事の運用（対応区分・メモ・一括処理・案内メール）は「健康管理」の
# 加入者健康一覧・加入者マイページで行います。
_HR_NA = {"oh.dashboard", "oh.list", "oh.interview.view"}
# 保健師は「健康管理」で保健指導・受診勧奨を行い、対応の記録を残します。
# 支援対象者・支援実績（対応の履歴）のCSV出力もできます（ロール別業務フロー 8）。
# 産業医面談 管理・マスタ・取込・健保側の画面は対象外です。
# 保健師は面談結果入力（保健指導の面談）も行い、記録すると対応済みになります。
_NURSE_OK = {"health.view", "health.mail", "oh.hr_class", "download",
             "oh.interview.view", "oh.interview"}
FEATURE_NA_ROLES = {}
for _f in FEATURES:
    _na = set()
    if _f[0] not in _DOCTOR_OK:
        _na.add("company_user/doctor")
    if _f[0] not in _MEMBER_OK:
        _na.add("member")
    if _f[0] not in _NURSE_OK:
        _na.add("company_user/nurse")
    if _f[0] in _HR_NA:
        _na.add("company_user/hr")
    if _na:
        FEATURE_NA_ROLES[_f[0]] = _na


def feature_na(key, role_key):
    # そのロールにとって対象外の機能か（画面では「対象外」と表示して切り替えません）
    return role_key in FEATURE_NA_ROLES.get(key, ())
FIXED_FEATURES = {f[0] for f in FEATURES if f[4]}
# 固定の機能を使えるロール（医学的判断は産業医のみ）
FIXED_FEATURE_ROLE = "company_user/doctor"
# 固定の機能のうち、産業医以外にも使えるもの（面談結果の記録は保健師も行う）
FIXED_FEATURE_ROLES = {"oh.interview": {"company_user/doctor", "company_user/nurse"}}


def fixed_roles(key):
    return FIXED_FEATURE_ROLES.get(key, {FIXED_FEATURE_ROLE})

_KENPO = {"kenpo.kenshin", "kenpo.hoken", "kenpo.influenza", "kenpo.receipt",
          "kenpo.member_edit", "kenpo.mail"}
_OPS = ({"master.view", "master.write", "master.import", "oh.list", "oh.kenshin",
         "oh.dashboard",
         "oh.interview.view", "oh.hr_class", "oh.report", "oh.mail", "oh.upload",
         "risk", "accounts", "download"} | _KENPO | _SETTINGS | _HEALTH)
# 産業医は「面談対象者一覧」と「面談結果入力」だけを使います（ほかは対象外）
_DOCTOR = set(_DOCTOR_OK)
# 既定の機能マトリクス（設定画面の「初期値に戻す」でこの状態になる）
FEATURE_DEFAULTS = {
    "system_admin": set(_OPS),
    "kenpo_user": set(_OPS),
    "company_user": set(_OPS),
    # 人事は面談結果入力の画面を出しません（対象外。記録は産業医が行います）
    "company_user/hr": set(_OPS) - _HR_NA,
    "company_user/doctor": set(_DOCTOR),
    # 保健師は健康管理（加入者健康一覧・マイページ・案内メール・健診結果の確認）だけ
    "company_user/nurse": set(_NURSE_OK),
    # 加入者本人は自分のマイページだけ
    "member": set(_MEMBER_OK),
}
SCOPE_LABELS = {"all": "全健保", "kenpo_all": "自組合全体",
                "own_company": "担当する範囲",
                # 加入者本人のログインは、自分の情報だけが見える
                "self": "ご本人のみ"}
STATUS_LABELS = {"active": "有効", "invited": "PW未設定", "disabled": "無効",
                 "deleted": "削除済み"}
# 「認証方式（A／B）」の登録は 8-54 で廃止しました（kenpo.auth_pattern 列は互換のため残しています）
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


# ---- 画面の表示倍率 ----------------------------------------------------
# 画面の寸法はブラウザを 90% で見たときに合わせて作ってあります。
# 100% のままでも同じ見え方になるよう、いちばん外側の文書に zoom を掛けます。
# 環境変数 HIA_UI_ZOOM で変えられます（例: 1 で等倍。画面テストは 1 で動かします）。
def ui_zoom():
    v = (os.environ.get("HIA_UI_ZOOM") or "0.9").strip()
    try:
        z = float(v)
    except ValueError:
        z = 0.9
    if not (0.5 <= z <= 1.5):
        z = 0.9
    return ("%g" % z)


@app.route("/ui/zoom.css")
def ui_zoom_css():
    """表示倍率のスタイル。枠（iframe）の中の画面には親の倍率がそのまま伝わるので、
    このスタイルはいちばん外側の文書だけが読み込みます（frag_base は自分が外側のときだけ）。
    印刷は用紙に合わせるため等倍に戻します。"""
    z = ui_zoom()
    css = ("html{zoom:%s;--ui-zoom:%s}\n@media print{html{zoom:1;--ui-zoom:1}}\n" % (z, z))
    resp = app.response_class(css, mimetype="text/css")
    resp.headers["Cache-Control"] = "no-store, must-revalidate"
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
        " actor_role, kenpo_id, ip, target, detail, path)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (session.get("shell"), category, action, result,
         me["email"] if me else session.get("pending_email"),
         me["id"] if me else None,
         me["role"] if me else None,
         acc["kenpo_id"] if acc else None,
         client_ip(), target, detail, request.path if request else None))
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
                       company_id=None, can_download=me["can_download"],
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


# 画面の色味（メインカラー）。産業医・人事は「健康」をイメージしやすい
# ブルーグリーン（青緑）にそろえます。ほかのロールは従来どおりです。
TONE_SUB_ROLES = ("doctor", "hr", "nurse")


def role_tone(acc=None):
    acc = acc if acc is not None else current_account()
    return "health" if acc and sub_role(acc) in TONE_SUB_ROLES else ""


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


# HIAスタッフ（システム管理者）は機能制御の対象外。設定に関係なく全機能を使える。
# ただし固定の機能（医学的判断＝面談対象の承認・就業区分の判定・面談結果の記録・
# 報告書への記名）は、労働安全衛生法の考え方にもとづき産業医のみ。
ALL_FEATURE_ROLE = "system_admin"


def can_download_now(acc=None):
    """CSVのダウンロードができるか。export_csv と同じ条件で判定する。
    画面側は、これが偽ならダウンロードのボタンを出さない。"""
    acc = acc if acc is not None else current_account()
    if not acc:
        return False
    if not feature_allowed("download", acc):
        return False
    return bool(acc["can_download"]) or acc["role"] == "system_admin"


def feature_allowed(key, acc=None):
    """そのアカウントが機能を使えるか。設定があればそれを、無ければ既定値を使う。
    固定の機能（医学的判断）は設定に関係なく産業医だけが使える。
    HIAスタッフは設定に関係なく（固定の機能以外の）全機能を使える。"""
    acc = acc if acc is not None else current_account()
    if not acc:
        return False
    rk = role_key(acc)
    if feature_na(key, rk):
        return False            # 役割上の対象外（設定に関係なく使えません）
    if key in FIXED_FEATURES:
        return rk in fixed_roles(key)
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
    "orgs": "master.view", "orgs_export": "master.view",
    "companies": "master.view", "offices": "master.view", "departments": "master.view",
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
    "members_link_page": "master.write",
    # 加入者情報のコードでまとめて紐づける画面。ほかの紐づけと同じ扱い（割当が漏れていた）
    "members_link_auto": "master.write",
    "members_link_assign": "master.write", "api_members_link": "master.write",
    "api_members_link_filtered": "master.write",
    "risk_group_edit": "master.write", "risk_group_delete": "master.write",
    # CSVの一括取込（アップロード → 確認 → 確定 の3段階。5様式共通）
    "import_upload": "master.import", "import_upload_bulk": "master.import",
    "import_receive": "master.import", "import_preview": "master.import",
    "import_commit": "master.import",
    "import_format": "master.import", "import_blank": "master.import",
    "criteria": "master.view", "criteria_edit": "master.write",
    "member_photos_page": "master.import", "member_photo_upload": "master.import",
    "member_photo_delete": "master.write",
    # 疾患予測
    "risk_list": "risk", "risk_members": "risk", "risk_member": "risk", "risk_run_exec": "risk",
    "risk_export": "risk", "risk_groups": "risk", "risk_kenshin": "risk",
    "risk_kenshin_sync": "risk", "risk_kenshin_xml": "risk",
    "risk_nsips": "risk", "risk_nsips_sync": "risk",
    # 設定・サポート
    "logs": "settings.log", "logs_export": "settings.log",
    # アカウント管理
    "accounts": "accounts", "accounts_rows": "accounts", "accounts_export": "accounts",
    "accounts_new": "accounts", "accounts_create": "accounts",
    "accounts_edit": "accounts", "accounts_edit_apply": "accounts",
    "accounts_invite_link": "accounts", "accounts_set_password": "accounts",
    "accounts_send_invite": "accounts", "accounts_toggle": "accounts",
    "accounts_delete": "accounts", "accounts_purge": "accounts",
}


# ---------------------------------------------------------------- 画面表示の記録
# 「どの画面を開いたか」を操作ログに残すための画面名（GETで表示できたときだけ記録）。
# 一覧の追加読み込み（*_rows）・API・CSV出力・ログイン画面は記録しません
# （CSV出力と取込は download／import として別に記録しています）。
VIEW_PAGES = {
    "spa": "HIA健保管理（トップ）", "spa_km": "HIA総合管理（トップ）",
    "dashboard": "ダッシュボード", "standalone": "画面一覧",
    "orgs": "企業・事業所・部署", "companies": "企業情報", "company_new": "企業の登録", "company_edit": "企業の編集",
    "offices": "事業所情報", "office_new": "事業所の登録", "office_edit": "事業所の編集",
    "departments": "部署情報", "department_new": "部署の登録",
    "department_edit": "部署の編集",
    "members": "加入者情報", "member_new": "加入者の登録", "member_edit": "加入者の編集",
    "members_link_page": "企業・部署の紐づけ", "member_photos_page": "写真の取込",
    "criteria": "判定マスタ", "criteria_edit": "判定マスタの編集",
    "kenpos": "健康保険組合情報", "kenpo_new": "健康保険組合の登録",
    "kenpo_edit": "健康保険組合の編集",
    "accounts": "アカウント一覧", "accounts_new": "アカウントの登録",
    "accounts_edit": "アカウントの編集", "me_account": "マイアカウント",
    "feature_settings": "機能制御", "logs": "操作ログ管理",
    "risk_list": "疾患予測", "risk_members": "疾患予測 対象者一覧", "risk_member": "疾患予測（加入者別）",
    "risk_groups": "リスクグループ", "risk_group_edit": "リスクグループの編集",
    "risk_kenshin": "健診データ連携", "risk_nsips": "NSIPSデータ連携",
    "oh.oh_dashboard": "面談ダッシュボード", "oh.oh_list": "面談対象者一覧",
    "oh.oh_member": "面談対象者の詳細", "oh.oh_interview": "面談結果入力",
    "oh.oh_kenshin": "健診結果", "oh.oh_mail": "メール設定",
    "oh.oh_questions": "ストレスチェック設問", "oh.oh_report": "帳票・報告書",
    "oh.oh_upload": "データ取込",
    "hm.hm_dash": "健康管理ダッシュボード",
    "hm.hm_list": "加入者健康一覧", "hm.hm_member": "加入者マイページ",
    "hm.hm_me": "健康マイページ（本人）",
}

# 同じ画面を続けて開いたとき（絞り込みのやり直しなど）は、この秒数までまとめて1件にします
VIEW_LOG_GAP = 60


@app.after_request
def log_view(resp):
    """どの画面を開いたかを操作ログに残す"""
    try:
        if request.method != "GET" or resp.status_code != 200:
            return resp
        name = VIEW_PAGES.get(request.endpoint)
        if not name or not session.get("account_id"):
            return resp
        db = get_db()
        me = real_account()
        last = db.execute(
            "SELECT ts FROM audit_log WHERE category='view' AND target=? AND actor_id=?"
            " ORDER BY id DESC LIMIT 1", (name, me["id"] if me else None)).fetchone()
        if last and last["ts"]:
            gap = db.execute("SELECT (julianday('now','localtime') - julianday(?))*86400 s",
                             (last["ts"],)).fetchone()["s"]
            if gap is not None and gap < VIEW_LOG_GAP:
                return resp
        # 絞り込みの語句は残しません（画面のURL＝ディレクトリだけを記録します）
        log("view", f"画面を表示：{name}", "success", target=name)
    except Exception:
        pass
    return resp


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


def current_shell():
    """画面の色味（青＝HIA健保管理／緑＝HIA総合管理）を、いま操作しているアカウントの状態から決める。
    サポートログイン中は必ず HIA健保管理（青）。当社スタッフ本来の画面は HIA総合管理（緑）。
    （以前はセッションに覚えた値を使っていたため、別のタブで総合管理を開くと
    健保管理の枠の中まで緑に変わることがあった）"""
    if support_kenpo():
        return "kenpo"
    me = real_account()
    if me:
        return SHELL_OF_ROLE.get(me["role"], "kenpo")
    return session.get("shell", "kenpo")


@app.context_processor
def inject_globals():
    embed = bool(session.get("embed")) and request.endpoint not in ("dashboard",)
    return {
        "LAYOUT": "frag_base.html" if embed else "base.html",
        "EMBED": embed,
        "SHELL": current_shell(),
        # 画面の色味（産業医・人事はブルーグリーン）
        "TONE": role_tone(),
        "acc": current_account(),
        "ROLE_LABELS": ROLE_LABELS,
        "SUB_ROLE_LABELS": SUB_ROLE_LABELS,
        "SUB_ROLES": SUB_ROLES,
        "SUB_ROLE_DESC": SUB_ROLE_DESC,
        "SUB_ROLE_ROLES": SUB_ROLE_ROLES,
        "role_full": role_full,
        "sub_role_of": sub_role,
        # マスタの登録・変更・削除ができるか（機能制御で切り替えられる）
        "CAN_MASTER": feature_allowed("master.write"),
        "CAN_IMPORT": feature_allowed("master.import"),
        # CSV出力ができるか（機能制御＋アカウントごとの許可）。押せないボタンを出さないため
        "CAN_DOWNLOAD": can_download_now(),
        # アカウント管理を開けるか（産業医・保健師・加入者本人は開けない）
        "CAN_ACCOUNTS": feature_allowed("accounts"),
        "IS_DOCTOR": is_doctor(),
        "can_feature": feature_allowed,
        "SCOPE_LABELS": SCOPE_LABELS,
        "STATUS_LABELS": STATUS_LABELS,
        "BUILD": BUILD,
        "BUILD_MISMATCH": build_mismatch(),
        "VIEW_LOG_GAP": VIEW_LOG_GAP,
        "MAX_EXPORT_ROWS": MAX_EXPORT_ROWS,
        "MAIL_ENABLED": mail_enabled(),
        "RESET_HOURS": RESET_HOURS,
    }


# ---------------------------------------------------------------- 採番
KANA_TRIM = ("株式会社", "有限会社", "合同会社", "健康保険組合", "組合", "（株）", "(株)")


def internal_company_code(kenpo_name, company_name):
    """内部コードを健保名＋企業名から自動で割り振る。
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


def set_account_scopes(aid, scopes, companies=None):
    """担当範囲を置き換える。企業は従来の account_company にも反映する。
    companies を渡すと account_company にはそちら（まるごと＋絞り込み対象の親企業）を入れる。
    渡さなければ従来どおり scopes["company"] を使う。"""
    db = get_db()
    db.execute("DELETE FROM account_scope WHERE account_id=?", (aid,))
    for kind in ("company", "office", "dept"):
        for rid in dict.fromkeys(scopes.get(kind) or []):
            db.execute("INSERT OR IGNORE INTO account_scope (account_id, kind, ref_id)"
                       " VALUES (?,?,?)", (aid, kind, rid))
    if companies is None:
        companies = scopes.get("company") or []
    set_account_companies(aid, list(dict.fromkeys(companies)))


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
            f" WHERE o.id IN ({q}) ORDER BY c.id, o.id", scopes["office"])]
        parts.append("事業所 " + "・".join(names))
    if scopes.get("dept"):
        q = ",".join("?" * len(scopes["dept"]))
        # 事業所のない部署（office_id が NULL）は事業所名の代わりに「事業所なし」と出す
        names = [f"{r['cname']}／{r['oname'] or '（事業所なし）'}／{r['name']}"
                 for r in db.execute(
            "SELECT d.name, o.name AS oname, c.name AS cname FROM department d"
            " JOIN company c ON c.id=d.company_id LEFT JOIN office o ON o.id=d.office_id"
            f" WHERE d.id IN ({q}) ORDER BY c.id, IFNULL(d.office_id, 0), d.id",
            scopes["dept"])]
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
            f"SELECT company_id FROM department WHERE id IN ({q})", sc["dept"])}
    if not ids:
        return []
    ids = sorted(ids)
    q = ",".join("?" * len(ids))
    return db.execute(
        "SELECT c.*, k.name AS kenpo_name, k.code AS kenpo_code FROM company c"
        f" JOIN kenpo k ON k.id=c.kenpo_id WHERE c.id IN ({q}) ORDER BY c.code",
        ids).fetchall()


# 部署は事業所なしでも登録できるため、企業は部署から直接たどり、
# 事業所は LEFT JOIN する（office_id が NULL の行＝事業所なしの部署）。
DEPT_SELECT = ("SELECT d.*, o.name AS office_name, o.ext_code AS office_ext,"
               " c.name AS company_name, c.ext_code AS company_ext, c.kenpo_id"
               " FROM department d JOIN company c ON c.id=d.company_id"
               " LEFT JOIN office o ON o.id=d.office_id")
DEPT_ORDER = " ORDER BY c.id, IFNULL(d.office_id, 0), d.id"


def scoped_departments(acc):
    """操作できる部署。企業・事業所を担当していればその企業・事業所の部署すべて、
    部署だけを担当している場合はその部署に限る。"""
    db = get_db()
    if acc["role"] in ("system_admin", "kenpo_user"):
        ids = [c["id"] for c in scoped_companies(acc)]
        if not ids:
            return []
        q = ",".join("?" * len(ids))
        return db.execute(DEPT_SELECT + f" WHERE d.company_id IN ({q})"
                          + DEPT_ORDER, ids).fetchall()
    sc = account_scope_ids(acc["id"])
    conds, params = [], []
    if sc["company"]:
        conds.append("d.company_id IN (" + ",".join("?" * len(sc["company"])) + ")")
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
                      + DEPT_ORDER, params).fetchall()


def owns_department(acc, did):
    return bool(did) and any(d["id"] == did for d in scoped_departments(acc))


def scoped_offices(acc):
    """操作できる事業所。企業を担当していればその企業の事業所すべて、
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
            f" WHERE o.company_id IN ({q}) ORDER BY c.id, o.id", ids).fetchall()
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
        " WHERE " + " OR ".join(conds) + " ORDER BY c.id, o.id", params).fetchall()


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


def _fy_options(n=4):
    """年度の選択肢（今年度から過去n年分）"""
    now_fy = int(_current_fy())
    return [str(now_fy - i) for i in range(n)]


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


def account_member_id(acc):
    """加入者本人のアカウントに紐づく加入者のID（本人以外は None）"""
    if not acc or acc["role"] != "member":
        return None
    try:
        mid = acc["member_id"]
    except (KeyError, IndexError):
        mid = None
    if mid:
        return mid
    # 紐づけが無い場合はメールアドレスの一致で本人を探す（移行時の保険）
    if not acc["email"]:
        return None
    row = get_db().execute(
        "SELECT id FROM member WHERE lower(IFNULL(email,''))=lower(?)"
        " ORDER BY id LIMIT 1", (acc["email"],)).fetchone()
    return row["id"] if row else None


def member_where(acc):
    """加入者の閲覧範囲。担当範囲（企業・事業所・部署）のどれかに当てはまれば見える。"""
    if acc["role"] == "system_admin":
        return "1=1", []
    if acc["role"] == "member":
        # 加入者本人は自分の1件だけ
        mid = account_member_id(acc)
        return ("m.id = ?", [mid]) if mid else ("1=0", [])
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
        # シェル（静的HTML）でメインカラーを切り替えるために渡す
        "tone": role_tone(acc),
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
    """HIA総合管理（HIAスタッフ・緑）"""
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
    consent, err = clean_consent_text(data.get("consent_text"))
    if err:
        return {"ok": False, "message": err}
    sp, err = clean_site_periods(data)
    if err:
        return {"ok": False, "message": err}
    db.execute("INSERT INTO kenpo (code, name, consent_text, kenshin_site_start, kenshin_site_end,"
               " flu_site_start, flu_site_end) VALUES (?,?,?,?,?,?,?)",
               (code, name, consent, sp["kenshin_site_start"], sp["kenshin_site_end"],
                sp["flu_site_start"], sp["flu_site_end"]))
    kid = db.execute("SELECT id FROM kenpo WHERE code=?", (code,)).fetchone()["id"]
    db.commit()
    log("master", "健康保険組合を登録", "success", target=name,
        detail=f"保険者番号={code}"
               + (f"／同意文 {len(consent_items(consent))}項目" if consent else "／同意文なし"))
    return {"ok": True, "id": kid,
            "message": f"{name}（保険者番号 {code}）を登録しました。"
                       + ("同意文を登録しました" if consent else "")}


SITE_PERIOD_KEYS = ("kenshin_site_start", "kenshin_site_end", "flu_site_start", "flu_site_end")


def clean_site_periods(data, row=None):
    """健診代行・インフル補助の「サイト公開期間」（開始日・終了日）を取り出してそろえる。
    送られてこなかった項目は今の値を保つ。返り値は (値の辞書, エラー文)"""
    out, errs = {}, []
    for k in SITE_PERIOD_KEYS:
        if k in data:
            v = norm_date((data.get(k) or "").strip()) if (data.get(k) or "").strip() else ""
            if v:
                try:
                    datetime.strptime(v, "%Y-%m-%d")
                except ValueError:
                    v = None
            if v is None:
                errs.append("サイト公開期間の日付の形式が正しくありません。")
                v = ""
        else:
            v = (row[k] if row is not None and k in row.keys() and row[k] else "")
        out[k] = v
    for a, b, label in (("kenshin_site_start", "kenshin_site_end", "健診代行"),
                        ("flu_site_start", "flu_site_end", "インフル補助")):
        if out[a] and out[b] and out[b] < out[a]:
            errs.append(f"{label}のサイト公開期間は、終了日を開始日以降にしてください。")
    return out, (errs[0] if errs else "")


# クローズサイトに表示する同意文：本文・同意必須 の項目の並びを JSON で保存する
CONSENT_MAX_ITEMS = 20
CONSENT_BODY_MAX = 256


def clean_consent_text(v):
    """同意文をそろえる。(保存する文字列, エラー文) を返す。

    v は 項目の配列（JSON）か、その JSON 文字列。本文が空の項目は除く。
    項目が無ければ '' を返す（クローズサイトには表示しない）。
    """
    if v is None:
        return "", ""
    if isinstance(v, str):
        t = v.strip()
        if not t:
            return "", ""
        try:
            v = json.loads(t)
        except ValueError:
            # 旧形式（本文だけの文字列）は 1 項目として扱う
            v = [{"body": t, "required": True}]
    if not isinstance(v, list):
        return "", "同意文の形式が正しくありません。"
    out = []
    for it in v:
        if not isinstance(it, dict):
            continue
        body = str(it.get("body") or "").replace("\r\n", "\n").replace("\r", "\n").strip()
        if not body:
            continue
        if len(body) > CONSENT_BODY_MAX:
            return "", f"同意文の本文は{CONSENT_BODY_MAX:,}文字以内で入力してください（現在 {len(body):,}文字）。"
        out.append({"body": body, "required": bool(it.get("required", True))})
    if len(out) > CONSENT_MAX_ITEMS:
        return "", f"同意文は{CONSENT_MAX_ITEMS}項目までです（現在 {len(out)}項目）。"
    return (json.dumps(out, ensure_ascii=False) if out else ""), ""


def consent_items(text):
    """保存した同意文（JSON）を項目の配列に戻す"""
    s_, _ = clean_consent_text(text)
    return json.loads(s_) if s_ else []


@app.route("/api/kenpos/<int:kid>", methods=["GET", "POST"])
@roles_required("system_admin")
def api_kenpo_one(kid):
    """健康保険組合 1件の取得・更新（HIA総合管理の編集画面から呼ぶ）

    更新できるのは 保険者番号・保険者名称・クローズサイトに表示する同意文。
    一覧のトグル（権限）は /api/kenpos/<id>/flag で切り替える。
    """
    db = get_db()
    row = db.execute("SELECT * FROM kenpo WHERE id=?", (kid,)).fetchone()
    if not row:
        return {"ok": False, "message": "健康保険組合が見つかりません。"}, 404
    if request.method == "GET":
        return {"ok": True, "row": dict(row)}
    data = request.get_json(silent=True) or request.form
    code = (data.get("code") or row["code"] or "").strip()
    name = (data.get("name") or "").strip()
    if not code:
        return {"ok": False, "message": "保険者番号を入力してください。"}
    if not re.fullmatch(r"[0-9A-Za-z\-]{1,20}", code):
        return {"ok": False, "message": "保険者番号は英数字とハイフンで入力してください。"}
    if not name:
        return {"ok": False, "message": "健康保険組合名（保険者名称）を入力してください。"}
    if db.execute("SELECT 1 FROM kenpo WHERE code=? AND id<>?", (code, kid)).fetchone():
        return {"ok": False, "message": "この保険者番号は既に登録されています。"}
    if db.execute("SELECT 1 FROM kenpo WHERE name=? AND id<>?", (name, kid)).fetchone():
        return {"ok": False, "message": "この名称は既に登録されています。"}
    cur_consent = row["consent_text"] if "consent_text" in row.keys() else ""
    if "consent_text" in data:
        consent, err = clean_consent_text(data.get("consent_text"))
        if err:
            return {"ok": False, "message": err}
    else:
        consent = cur_consent or ""
    sp, err = clean_site_periods(data, row)
    if err:
        return {"ok": False, "message": err}
    db.execute("UPDATE kenpo SET code=?, name=?, consent_text=?, kenshin_site_start=?,"
               " kenshin_site_end=?, flu_site_start=?, flu_site_end=? WHERE id=?",
               (code, name, consent, sp["kenshin_site_start"], sp["kenshin_site_end"],
                sp["flu_site_start"], sp["flu_site_end"], kid))
    db.commit()
    changes = []
    if code != row["code"]:
        changes.append(f"保険者番号 {row['code']} → {code}")
    if name != row["name"]:
        changes.append(f"名称 {row['name']} → {name}")
    if consent != (cur_consent or ""):
        changes.append("同意文を" + ("削除（表示しない）" if not consent
                                  else f"更新（{len(consent_items(consent))}項目）"))
    for a, b, label in (("kenshin_site_start", "kenshin_site_end", "健診代行"),
                        ("flu_site_start", "flu_site_end", "インフル補助")):
        before = f"{(row[a] if a in row.keys() else '') or '—'}〜{(row[b] if b in row.keys() else '') or '—'}"
        after = f"{sp[a] or '—'}〜{sp[b] or '—'}"
        if before != after:
            changes.append(f"{label}のサイト公開期間 {before} → {after}")
    log("master", "健康保険組合を更新", "success", target=name,
        detail=("／".join(changes) if changes else f"保険者番号={code}（変更なし）"))
    return {"ok": True, "row": dict(db.execute("SELECT * FROM kenpo WHERE id=?", (kid,)).fetchone()),
            "message": f"{name} を更新しました。" + ("" if changes else "（変更はありません）")}


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
    db.execute("INSERT INTO kenpo (code, name) VALUES (?,?)", (code, name))
    db.commit()
    log("master", "健康保険組合を登録", "success", target=name, detail=f"保険者番号={code}")
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
    db.execute("UPDATE kenpo SET code=?, name=? WHERE id=?", (code, name, kid))
    db.commit()
    log("master", "健康保険組合を更新", "success", target=name,
        detail=f"保険者番号 {row['code']} → {code}／名称 {row['name']} → {name}")
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
    """HIAスタッフが、指定した健康保険組合の画面に入る（サポート対応用）"""
    me = real_account()
    if not me or me["role"] != "system_admin":
        log("account", "サポートログインをブロック", "blocked",
            detail="HIAスタッフ以外からの要求")
        flash("サポートログインはHIAスタッフのみ利用できます。", "error")
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
    """サポートログインを終了してHIA総合管理の画面に戻る"""
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
PII_KEYS = {"name", "kana", "birth", "address", "pref", "city", "zip", "tel", "email",
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
    HIAスタッフは健保を切り替えられる（選んだ内容はログイン中は保持する）。"""
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
    """疾患予測ダッシュボード（リスク区分・年代・性別・疾病・企業ごとの集計）。
    8-63 で検索条件と加入者ごとの一覧、予測の実行ボタンを外し、集計だけの画面にした。
    加入者ごとの結果はマイページ（疾患予測タブ）で、予測の実行は POST /risk/run（自動連携側から呼ぶ）"""
    db, acc = get_db(), current_account()
    kid = risk_kenpo_id(acc)
    run = db.execute("SELECT * FROM risk_run WHERE kenpo_id=? ORDER BY id DESC LIMIT 1",
                     (kid,)).fetchone()
    last_sync = db.execute("SELECT * FROM nsips_sync WHERE kenpo_id=?"
                           " ORDER BY id DESC LIMIT 1", (kid,)).fetchone()
    last_ken = db.execute("SELECT * FROM kenshin_sync WHERE kenpo_id=?"
                          " ORDER BY id DESC LIMIT 1", (kid,)).fetchone()
    kenpos = (db.execute("SELECT id, code, name FROM kenpo ORDER BY code").fetchall()
              if not acc["kenpo_id"] else [])
    return render_template("risk_list.html", run=run, kenpos=kenpos, kid=kid,
                           diseases=DISEASES, bands=AGE_BANDS,
                           last_sync=last_sync, last_ken=last_ken,
                           engine=risk_engine(), g=risk_aggregate(run))


@app.route("/risk/list")
@login_required
def risk_members():
    """疾患予測 対象者一覧（加入者ごとの予測結果。ダッシュボードのカードから開く。8-71）"""
    db, acc = get_db(), current_account()
    kid = risk_kenpo_id(acc)
    f = {k: (request.args.get(k) or "").strip()
         for k in ("name", "cname", "disease", "level")}
    run = db.execute("SELECT * FROM risk_run WHERE kenpo_id=? ORDER BY id DESC LIMIT 1",
                     (kid,)).fetchone()
    rows = []
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
    kenpos = (db.execute("SELECT id, code, name FROM kenpo ORDER BY code").fetchall()
              if not acc["kenpo_id"] else [])
    return render_template("risk_members.html", rows=rows, run=run, f=f, kenpos=kenpos, kid=kid,
                           diseases=DISEASES)


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

    # 年代 × 性別（リスク区分ごとの人数も持つ。8-63 の横積み上げグラフ用）
    by_band = {}
    for r in top:
        d = by_band.setdefault(r["age_band"], {"男": 0, "女": 0, "高男": 0, "高女": 0,
                                               "score": [], "n": 0,
                                               "lv": {"男": {"高": 0, "中": 0, "低": 0},
                                                      "女": {"高": 0, "中": 0, "低": 0}}})
        sex = r["sex"] if r["sex"] in ("男", "女") else "男"
        d[sex] += 1
        d["n"] += 1
        d["score"].append(r["score"])
        if r["level"] == "高":
            d["高" + sex] += 1
        if r["level"] in d["lv"][sex]:
            d["lv"][sex][r["level"]] += 1
    band_rows = []
    for band in AGE_BANDS:
        if band not in by_band:
            continue
        d = by_band[band]
        band_rows.append({
            "band": band, "n": d["n"], "male": d["男"], "female": d["女"],
            "high": d["高男"] + d["高女"], "high_m": d["高男"], "high_f": d["高女"],
            "lv": d["lv"],
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
            "hist": risk_hist(top),        # 予測した加入者全体のスコア分布（10点刻み）
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
    # 予測の内訳は加入者マイページの「疾患予測」タブで見せる（一覧の「詳細表示」と同じ行き先）
    return redirect(url_for("hm.hm_member", mid=mid, tab="risk"))


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
        flash("健診結果がまだ連携されていません。健診システムからの自動連携をお待ちください。", "error")
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
    """健診結果の連携状況（予約管理システムからバッチで取込む）"""
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


# 企業・事業所・部署のマスタは HIA健保管理（青）だけの機能（8-64 で総合管理のマスタ管理から外した）。
# 当社スタッフが直接開いても HIA健保管理のトンマナ（青・「詳細表示」）で出す
ORG_SHELL = {"SHELL": "kenpo"}


def _back_url(default_endpoint):
    """戻り先のURL（削除の確認画面の「戻る」に使う）。next があればそこへ。"""
    nxt = request.form.get("next") or request.args.get("next") or ""
    if nxt.startswith("/") and not nxt.startswith("//"):
        return nxt
    return url_for(default_endpoint)


def _back_to(default_endpoint):
    """削除などのあとの戻り先。企業の詳細表示から操作したときは next（同一サイト内のパス）に戻る"""
    nxt = request.form.get("next") or request.args.get("next") or ""
    if nxt.startswith("/") and not nxt.startswith("//"):
        return redirect(nxt)
    return redirect(url_for(default_endpoint))


def _company_children(cid):
    """企業の詳細表示（編集画面）に出す、この企業の事業所・部署と加入者数。8-59"""
    db = get_db()
    offs = db.execute("SELECT * FROM office WHERE company_id=? ORDER BY code", (cid,)).fetchall()
    depts_by_office, depts_direct = {}, []
    for d in db.execute("SELECT * FROM department WHERE company_id=? ORDER BY office_id, code",
                        (cid,)):
        if d["office_id"]:
            depts_by_office.setdefault(d["office_id"], []).append(d)
        else:
            depts_direct.append(d)      # 事業所を使わない企業の部署（企業の直下）
    return dict(offs=offs, depts_by_office=depts_by_office, depts_direct=depts_direct,
                mem_by_office=_office_counts(), mem_by_dept=_dept_counts())


def _save_company_children(cid, form):
    """企業の詳細表示で入力された事業所・部署の行を登録・更新する（8-59）。
    行は off_* / dep_* の配列で届く。新しい事業所は off_key（n1, n2…）で識別し、
    同じ画面で追加した部署の dep_office にはそのキーが入る。エラー時は ValueError。"""
    db = get_db()
    g = lambda vals, i: (vals[i] if i < len(vals) else "").strip()
    off_ids, off_keys = form.getlist("off_id"), form.getlist("off_key")
    off_ext, off_name = form.getlist("off_ext"), form.getlist("off_name")
    off_addr, off_tel = form.getlist("off_address"), form.getlist("off_tel")
    cur_offs = {o["id"]: o for o in db.execute("SELECT * FROM office WHERE company_id=?", (cid,))}
    key_to_id, seen_names, seen_ext, changes = {}, set(), set(), []
    for i in range(len(off_ids)):
        oid = int(off_ids[i]) if off_ids[i].isdigit() else None
        ext, name, addr, tel = g(off_ext, i), g(off_name, i), g(off_addr, i), g(off_tel, i)
        if oid is None and not (ext or name or addr or tel):
            continue                                     # 空の追加行は無視
        if not name:
            raise ValueError("事業所名が空の行があります。事業所名を入力してください。")
        if ext and not re.fullmatch(r"[0-9A-Za-z\-]{1,20}", ext):
            raise ValueError(f"事業所コード {ext} は英数字とハイフンで入力してください。")
        if name in seen_names:
            raise ValueError(f"事業所「{name}」が重複しています。")
        if ext and ext in seen_ext:
            raise ValueError(f"事業所コード {ext} が重複しています。")
        seen_names.add(name)
        if ext:
            seen_ext.add(ext)
        if oid is not None:
            cur = cur_offs.get(oid)
            if cur is None:
                raise ValueError("この企業に属していない事業所が含まれています。")
            if (cur["ext_code"] or "", cur["name"], cur["address"] or "", cur["tel"] or "") \
                    != (ext, name, addr, tel):
                db.execute("UPDATE office SET ext_code=?, name=?, address=?, tel=?, updated_at=?"
                           " WHERE id=?", (ext or None, name, addr, tel, now(), oid))
                changes.append(f"事業所を更新：{cur['name']}→{name}")
            key_to_id[g(off_keys, i) or f"o{oid}"] = oid
        else:
            code = next_code("office", str(cid), width=3)
            cur_ = db.execute("INSERT INTO office (company_id, ext_code, code, name, kana, zip,"
                              " tel, address) VALUES (?,?,?,?,?,?,?,?)",
                              (cid, ext or None, code, name, "", "", tel, addr))
            key_to_id[g(off_keys, i)] = cur_.lastrowid
            changes.append(f"事業所を追加：{name}（{code}）")
    # 既存の事業所どうしの重複（送られてこなかった行も含めて確認）
    for o in cur_offs.values():
        if o["id"] not in key_to_id.values() and o["name"] in seen_names:
            raise ValueError(f"事業所「{o['name']}」は既に登録されています。")

    dep_ids, dep_off = form.getlist("dep_id"), form.getlist("dep_office")
    dep_ext, dep_name = form.getlist("dep_ext"), form.getlist("dep_name")
    # 事業所なしの部署（office_id が NULL）も含める。JOIN だと落ちる
    cur_depts = {d["id"]: d for d in db.execute(
        "SELECT * FROM department WHERE company_id=?", (cid,))}
    valid_offs = set(cur_offs) | set(key_to_id.values())
    seen_d = set()
    for i in range(len(dep_ids)):
        did = int(dep_ids[i]) if dep_ids[i].isdigit() else None
        ref, ext, name = g(dep_off, i), g(dep_ext, i), g(dep_name, i)
        if did is None and not (ext or name):
            continue
        if not name:
            raise ValueError("部署名が空の行があります。部署名を入力してください。")
        if ext and not re.fullmatch(r"[0-9A-Za-z\-]{1,20}", ext):
            raise ValueError(f"部署コード {ext} は英数字とハイフンで入力してください。")
        # "company" は「（事業所なし）」＝企業の直下。それ以外は既存の事業所IDか、
        # この画面で追加したばかりの事業所のキー（n1, n2…）。
        if ref == "company":
            oid = None
        else:
            oid = int(ref) if ref.isdigit() else key_to_id.get(ref)
            if oid not in valid_offs:
                raise ValueError(f"部署「{name}」の事業所が選ばれていません"
                                 f"（事業所を使わない場合は「（事業所なし）」を選びます）。")
        if (oid, name) in seen_d:
            raise ValueError(f"部署「{name}」が同じ{" 事業所の中" if oid else "企業の直下"}で重複しています。")
        seen_d.add((oid, name))
        if did is not None:
            cur = cur_depts.get(did)
            if cur is None:
                raise ValueError("この企業に属していない部署が含まれています。")
            if (cur["ext_code"] or "", cur["name"]) != (ext, name):
                db.execute("UPDATE department SET ext_code=?, name=?, updated_at=? WHERE id=?",
                           (ext or None, name, now(), did))
                changes.append(f"部署を更新：{cur['name']}→{name}")
        else:
            if db.execute("SELECT 1 FROM department WHERE company_id=? AND name=?"
                          " AND IFNULL(office_id, 0)=?", (cid, name, oid or 0)).fetchone():
                raise ValueError(f"部署「{name}」は既に登録されています。")
            code = next_code("dept", str(oid) if oid else f"c{cid}", width=3)
            # department.company_id は必須（事業所なしの部署も持てるため）
            db.execute("INSERT INTO department (company_id, office_id, ext_code, code,"
                       " name, kana) VALUES (?,?,?,?,?,?)",
                       (cid, oid, ext or None, code, name, ""))
            changes.append(f"部署を追加：{name}（{code}）")
    for d in cur_depts.values():
        if d["id"] not in {int(x) for x in dep_ids if x.isdigit()} and (d["office_id"], d["name"]) in seen_d:
            raise ValueError(f"部署「{d['name']}」は既に登録されています。")
    return changes


@app.route("/orgs")
@login_required
def orgs():
    """企業・事業所・部署のマスタを1つの一覧で（企業 → 事業所 → 部署 の階層）。8-58"""
    db, acc = get_db(), current_account()
    comps = scoped_companies(acc)
    offs = scoped_offices(acc)
    depts = scoped_departments(acc)
    offs_by_company, depts_by_office = {}, {}
    for o in offs:
        offs_by_company.setdefault(o["company_id"], []).append(o)
    depts_no_office = {}
    for d in depts:
        if d["office_id"]:
            depts_by_office.setdefault(d["office_id"], []).append(d)
        else:
            # 事業所を使わない企業の部署（企業の直下）。事業所の行が無いので企業の直後に出す
            depts_no_office.setdefault(d["company_id"], []).append(d)
    mem_by_company = {r["id"]: r["c"] for r in db.execute(
        "SELECT company_id AS id, COUNT(*) c FROM member WHERE company_id IS NOT NULL GROUP BY company_id")}
    # 企業の登録・削除は HIAスタッフと健保担当者だけ（企業担当者は編集のみ）
    return render_template("orgs.html", **ORG_SHELL, comps=comps, offs=offs, depts=depts,
                           offs_by_company=offs_by_company, depts_by_office=depts_by_office,
                           depts_no_office=depts_no_office,
                           mem_by_company=mem_by_company, mem_by_office=_office_counts(),
                           mem_by_dept=_dept_counts(),
                           can_add=acc["role"] in ("system_admin", "kenpo_user"))


@app.route("/companies")
@login_required
def companies():
    """旧・企業情報一覧。企業・事業所・部署の一覧（/orgs）に統合（8-58）"""
    return redirect(url_for("orgs"))


@app.route("/companies/new", methods=["GET", "POST"])
@roles_required("system_admin", "kenpo_user")
def company_new():
    db, acc = get_db(), current_account()
    if acc["role"] == "system_admin":
        kenpos = db.execute("SELECT * FROM kenpo ORDER BY code").fetchall()
    else:
        kenpos = db.execute("SELECT * FROM kenpo WHERE id=?", (acc["kenpo_id"],)).fetchall()
    # 健保ごとに、次に採番される企業コードを先読みする
    nexts = {str(k["id"]): peek_code("company", str(k["id"])) for k in kenpos}
    empty = dict(offs=[], depts_by_office={}, mem_by_office={}, mem_by_dept={})
    if request.method == "GET":
        return render_template("company_form.html", **ORG_SHELL, row=None, kenpos=kenpos, nexts=nexts,
                               **empty)
    g = lambda k: (request.form.get(k) or "").strip()
    name, ext = g("name"), g("ext_code")
    kenpo_id = (request.form.get("kenpo_id", type=int) if acc["role"] == "system_admin"
                else acc["kenpo_id"])
    errs = []
    if not name:
        errs.append("企業名を入力してください。")
    if ext and not re.fullmatch(r"[0-9A-Za-z\-]{1,20}", ext):
        errs.append("企業コードは英数字とハイフンで入力してください。")
    if not kenpo_id:
        errs.append("健康保険組合を選択してください。")
    else:
        if ext and db.execute("SELECT 1 FROM company WHERE kenpo_id=? AND ext_code=?",
                              (kenpo_id, ext)).fetchone():
            errs.append(f"企業コード {ext} は、この健康保険組合で既に使われています。")
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
        return render_template("company_form.html", **ORG_SHELL, row=None, kenpos=kenpos,
                               nexts=nexts, form=request.form, **empty)
    code = internal_company_code(kn["name"] if kn else "", name)
    cur = db.execute("INSERT INTO company (kenpo_id, ext_code, code, name, kana, cert_mark, zip,"
                     " tel, address, email) VALUES (?,?,?,?,?,?,?,?,?,?)",
                     (kenpo_id, ext or None, code, name, g("kana"), g("cert_mark"), g("zip"),
                      g("tel"), g("address"), g("email")))
    new_cid = cur.lastrowid
    # 事業所・部署も一緒に登録（登録画面は編集画面と同じ構成。8-67）。駄目なら企業も登録しない
    try:
        changes = _save_company_children(new_cid, request.form)
    except ValueError as e:
        db.rollback()
        flash(str(e), "error")
        return render_template("company_form.html", **ORG_SHELL, row=None, kenpos=kenpos,
                               nexts=nexts, form=request.form, **empty)
    db.commit()
    cid = db.execute("SELECT id FROM company WHERE kenpo_id=? AND name=?"
                     " ORDER BY id DESC LIMIT 1", (kenpo_id, name)).fetchone()["id"]
    log("master", "企業を登録", "success", target=name,
        detail=f"企業コード={ext or '（未設定）'}／企業ID={cid}"
        + ("／" + "、".join(changes) if changes else ""))
    flash(f"「{name}」を登録しました。"
          + (f"企業コードは {ext} です。" if ext
             else "企業コードは未設定です。")
          + f"（企業ID {cid}）"
          + (f"事業所 {len(changes)} 件も登録しました。" if changes else ""), "ok")
    return _back_to("orgs")


@app.route("/companies/<int:cid>/edit", methods=["GET", "POST"])
@login_required
def company_edit(cid):
    db, acc = get_db(), current_account()
    if not owns_company(acc, cid):
        log("master", "企業編集をブロック", "blocked", target=str(cid), detail="スコープ外")
        flash("対象の企業を操作する権限がありません。", "error")
        return _back_to("orgs")
    row = db.execute("SELECT * FROM company WHERE id=?", (cid,)).fetchone()
    if request.method == "GET":
        stat = db.execute(
            "SELECT (SELECT COUNT(*) FROM office WHERE company_id=?) AS n_off,"
            " (SELECT COUNT(*) FROM member WHERE company_id=?) AS n_mem",
            (cid, cid)).fetchone()
        kn = db.execute("SELECT name, code FROM kenpo WHERE id=?",
                        (row["kenpo_id"],)).fetchone()
        return render_template("company_form.html", **ORG_SHELL, row=row, kenpos=[], stat=stat, kenpo=kn,
                               **_company_children(cid))
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
        return render_template("company_form.html", **ORG_SHELL, row=row, kenpos=[], stat=stat,
                               kenpo=kn, form=request.form, **_company_children(cid))
    g = lambda k: (request.form.get(k) or "").strip()
    ext = g("ext_code")
    if ext and db.execute("SELECT 1 FROM company WHERE kenpo_id=? AND ext_code=? AND id<>?",
                          (row["kenpo_id"], ext, cid)).fetchone():
        flash(f"企業コード {ext} は、この健康保険組合で既に使われています。", "error")
        return redirect(url_for("company_edit", cid=cid))
    before = f"{row['name']}／{row['ext_code'] or '—'}／TEL {row['tel'] or '—'}"
    kn = db.execute("SELECT name FROM kenpo WHERE id=?", (row["kenpo_id"],)).fetchone()
    # 事業所・部署の行（企業の詳細表示から入力。8-59）を先に検証し、駄目なら企業情報も保存しない
    try:
        changes = _save_company_children(cid, request.form)
    except ValueError as e:
        db.rollback()
        log("master", "企業情報の編集をブロック", "blocked", target=name, detail=str(e))
        flash(str(e), "error")
        stat = db.execute(
            "SELECT (SELECT COUNT(*) FROM office WHERE company_id=?) AS n_off,"
            " (SELECT COUNT(*) FROM member WHERE company_id=?) AS n_mem",
            (cid, cid)).fetchone()
        return render_template("company_form.html", **ORG_SHELL, row=row, kenpos=[], stat=stat,
                               kenpo=kn, form=request.form, **_company_children(cid))
    db.execute("UPDATE company SET ext_code=?, code=?, name=?, kana=?, cert_mark=?, zip=?,"
               " tel=?, address=?, email=?, updated_at=? WHERE id=?",
               (ext or None, internal_company_code(kn["name"] if kn else "", name), name,
                g("kana"), g("cert_mark"), g("zip"), g("tel"), g("address"),
                g("email"), now(), cid))
    db.commit()
    log("master", "企業情報を編集", "success", target=name,
        detail=f"企業コード={ext or '（未設定）'}／変更前: {before}"
        + ("／" + "、".join(changes) if changes else ""))
    flash(f"「{name}」の情報を更新しました。"
          + (f"（事業所 {len(changes)} 件を反映）" if changes else ""), "ok")
    return _back_to("orgs")


def _count(sql, *p):
    return get_db().execute(sql, p).fetchone()[0]


def _account_scope_count(kind, rid):
    """担当範囲としてこの企業・事業所・部署を指しているアカウントの数"""
    return _count("SELECT COUNT(*) FROM account_scope s JOIN account a ON a.id=s.account_id"
                  " WHERE s.kind=? AND s.ref_id=? AND a.status<>'deleted'", kind, rid)


@app.route("/companies/<int:cid>/delete", methods=["GET", "POST"])
@roles_required("system_admin", "kenpo_user")
def company_delete(cid):
    """企業の削除。GET は確認画面、POST は実行。
    この企業の事業所・部署・加入者・アカウントが1件でも残っていれば削除できない。"""
    db, acc = get_db(), current_account()
    if not owns_company(acc, cid):
        flash("対象の企業を操作する権限がありません。", "error")
        return _back_to("orgs")
    row = db.execute("SELECT * FROM company WHERE id=?", (cid,)).fetchone()
    if not row:
        flash("対象の企業が見つかりません。", "error")
        return _back_to("orgs")
    blockers = [
        ("事業所", _count("SELECT COUNT(*) FROM office WHERE company_id=?", cid),
         "事業所を削除してください（事業所の企業は変更できません）"),
        ("部署", _count("SELECT COUNT(*) FROM department WHERE company_id=?", cid),
         "部署を削除してください（部署の企業は変更できません）"),
        ("加入者", _count("SELECT COUNT(*) FROM member WHERE company_id=?", cid),
         "加入者一覧の「選択したN件を紐づける」または「企業・部署の紐づけ」で所属先を付け替えるか、未紐づけに戻してください"),
        ("担当するアカウント",
         _count("SELECT COUNT(*) FROM account WHERE company_id=? AND status<>'deleted'", cid)
         + _account_scope_count("company", cid),
         "アカウント管理で担当範囲からこの企業を外してください"),
    ]
    blocked = any(n for _, n, _ in blockers)
    if request.method == "GET":
        return render_template(
            "delete_confirm.html", what="企業", blockers=blockers,
            subject={"name": row["name"], "sub": row["kana"] or "", "id_label": "企業ID",
                     "id": cid}, notes=[], alt=None, back_url=_back_url("orgs"))
    if blocked:
        log("master", "企業削除をブロック", "blocked", target=row["name"],
            detail="／".join(f"{l}{n}件" for l, n, _ in blockers if n) + "が残っている")
        flash(f"「{row['name']}」には紐づいているデータがあるため削除できません。", "error")
        return redirect(url_for("company_delete", cid=cid))
    db.execute("DELETE FROM company WHERE id=?", (cid,))
    db.commit()
    log("master", "企業を削除", "success", target=row["name"], detail=f"企業ID={cid}")
    flash(f"「{row['name']}」を削除しました。", "ok")
    return _back_to("orgs")


# ================================================================ 事業所
def _office_counts():
    return {r["id"]: r["c"] for r in get_db().execute(
        "SELECT office_id AS id, COUNT(*) c FROM member GROUP BY office_id")}


@app.route("/offices")
@login_required
def offices():
    """旧・事業所情報一覧。企業・事業所・部署の一覧（/orgs）に統合（8-58）"""
    return redirect(url_for("orgs"))


@app.route("/offices/new", methods=["GET", "POST"])
@login_required
def office_new():
    db, acc = get_db(), current_account()
    comps = scoped_companies(acc)
    nexts = {str(c["id"]): peek_code("office", str(c["id"]), width=3) for c in comps}
    if request.method == "GET":
        # 一覧の「事業所を追加」から来たときは、その企業を選んだ状態で開く
        pre = request.args if request.args.get("company_id") else None
        return render_template("office_form.html", **ORG_SHELL, row=None, comps=comps, nexts=nexts, form=pre)
    g = lambda k: (request.form.get(k) or "").strip()
    cid = request.form.get("company_id", type=int)
    name, ext = g("name"), g("ext_code")
    errs = []
    if not owns_company(acc, cid):
        log("master", "事業所登録をブロック", "blocked", target=str(cid), detail="スコープ外の企業")
        errs.append("選択された企業を操作する権限がありません。")
    if ext and cid and db.execute("SELECT 1 FROM office WHERE company_id=? AND ext_code=?",
                                  (cid, ext)).fetchone():
        errs.append(f"事業所コード {ext} は、この企業で既に使われています。")
    if not name:
        errs.append("事業所名を入力してください。")
    elif cid and db.execute("SELECT 1 FROM office WHERE company_id=? AND name=?",
                            (cid, name)).fetchone():
        log("master", "事業所登録の重複を検知", "blocked", target=name)
        errs.append(f"「{name}」は既に登録されています。")
    if errs:
        for e in errs:
            flash(e, "error")
        return render_template("office_form.html", **ORG_SHELL, row=None, comps=comps,
                               nexts=nexts, form=request.form)
    code = next_code("office", str(cid), width=3)
    # 所在地・連絡先の欄は 8-72 で画面から外した（届けば保存、無ければ空）
    db.execute("INSERT INTO office (company_id, ext_code, code, name, kana, zip, tel,"
               " address) VALUES (?,?,?,?,?,?,?,?)",
               (cid, ext or None, code, name, g("kana"), g("zip"), g("tel"), g("address")))
    db.commit()
    cn = db.execute("SELECT name FROM company WHERE id=?", (cid,)).fetchone()["name"]
    oid = db.execute("SELECT id FROM office WHERE company_id=? AND name=?"
                     " ORDER BY id DESC LIMIT 1", (cid, name)).fetchone()["id"]
    log("master", "事業所を登録", "success", target=f"{cn}／{name}",
        detail=f"事業所コード={ext or '（未設定）'}／事業所ID={oid}")
    flash(f"「{name}」を登録しました。"
          + (f"事業所コードは {ext} です。" if ext else "事業所コードは未設定です。")
          + f"（事業所ID {oid}）", "ok")
    return _back_to("orgs")


@app.route("/offices/<int:oid>/edit", methods=["GET", "POST"])
@login_required
def office_edit(oid):
    db, acc = get_db(), current_account()
    if not owns_office(acc, oid):
        log("master", "事業所編集をブロック", "blocked", target=str(oid), detail="スコープ外")
        flash("対象の事業所を操作する権限がありません。", "error")
        return _back_to("orgs")
    row = db.execute(
        "SELECT o.*, c.name AS company_name, c.code AS company_code,"
        " c.ext_code AS company_ext FROM office o JOIN company c ON c.id=o.company_id"
        " WHERE o.id=?", (oid,)).fetchone()
    n_mem = db.execute("SELECT COUNT(*) c FROM member WHERE office_id=?",
                       (oid,)).fetchone()["c"]

    def _children():
        """この事業所の部署（編集画面の「部署」カード。8-72）"""
        return dict(depts=db.execute("SELECT * FROM department WHERE office_id=? ORDER BY code",
                                     (oid,)).fetchall(), mem_by_dept=_dept_counts())
    if request.method == "GET":
        return render_template("office_form.html", **ORG_SHELL, row=row, comps=[], n_mem=n_mem,
                               **_children())
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
        return render_template("office_form.html", **ORG_SHELL, row=row, comps=[], n_mem=n_mem,
                               form=request.form, **_children())
    before = f"{row['name']}／TEL {row['tel'] or '—'}"
    g = lambda k: (request.form.get(k) or "").strip()
    ext = g("ext_code")
    if ext and db.execute("SELECT 1 FROM office WHERE company_id=? AND ext_code=? AND id<>?",
                          (row["company_id"], ext, oid)).fetchone():
        flash(f"事業所コード {ext} は、この企業で既に使われています。", "error")
        return redirect(url_for("office_edit", oid=oid))
    # 部署の行（8-72）。駄目なら事業所も保存しない。所在地・連絡先は画面に無いので今の値を保つ
    try:
        changes = _save_company_children(row["company_id"], request.form)
    except ValueError as e:
        db.rollback()
        flash(str(e), "error")
        return render_template("office_form.html", **ORG_SHELL, row=row, comps=[], n_mem=n_mem,
                               form=request.form, **_children())
    keep = lambda k: g(k) if k in request.form else (row[k] or "")
    db.execute("UPDATE office SET ext_code=?, name=?, kana=?, zip=?, tel=?, address=?,"
               " updated_at=? WHERE id=?",
               (ext or None, name, g("kana"), keep("zip"), keep("tel"), keep("address"), now(), oid))
    db.commit()
    log("master", "事業所情報を編集", "success", target=f"{row['company_name']}／{name}",
        detail=f"事業所コード={row['code']}／変更前: {before}"
        + ("／" + "、".join(changes) if changes else ""))
    flash(f"「{name}」の情報を更新しました。"
          + (f"（部署 {len(changes)} 件を反映）" if changes else ""), "ok")
    return _back_to("orgs")


@app.route("/offices/<int:oid>/delete", methods=["GET", "POST"])
@login_required
def office_delete(oid):
    """事業所の削除。GET は確認画面、POST は実行。
    この事業所の部署・加入者は自動では「事業所なし」に付け替えない（黙って付け替わるのを防ぐ）。
    残っている間は削除できず、付け替えてから削除する。"""
    db, acc = get_db(), current_account()
    if not owns_office(acc, oid):
        flash("対象の事業所を操作する権限がありません。", "error")
        return _back_to("orgs")
    row = db.execute(
        "SELECT o.*, c.name AS company_name FROM office o JOIN company c ON c.id=o.company_id"
        " WHERE o.id=?", (oid,)).fetchone()
    if not row:
        flash("対象の事業所が見つかりません。", "error")
        return _back_to("orgs")
    blockers = [
        ("部署", _count("SELECT COUNT(*) FROM department WHERE office_id=?", oid),
         "部署を削除してください（部署の事業所は変更できません）"),
        ("加入者", _count("SELECT COUNT(*) FROM member WHERE office_id=?", oid),
         "加入者一覧の「選択したN件を紐づける」または「企業・部署の紐づけ」で所属先を付け替えてください"),
        ("担当するアカウント", _account_scope_count("office", oid),
         "アカウント管理で担当範囲からこの事業所を外してください"),
    ]
    blocked = any(n for _, n, _ in blockers)
    if request.method == "GET":
        return render_template(
            "delete_confirm.html", what="事業所", blockers=blockers,
            subject={"name": row["name"], "sub": row["company_name"],
                     "id_label": "事業所ID", "id": oid},
            notes=["この事業所の部署・加入者を<b>自動で「事業所なし」に付け替えることはしません</b>。"
                   "黙って付け替わるのを防ぐため、先に付け替えてから削除してください。"],
            alt=None, back_url=_back_url("orgs"))
    if blocked:
        log("master", "事業所削除をブロック", "blocked", target=row["name"],
            detail="／".join(f"{l}{n}件" for l, n, _ in blockers if n) + "が残っている")
        flash(f"「{row['name']}」には紐づいているデータがあるため削除できません。", "error")
        return redirect(url_for("office_delete", oid=oid))
    db.execute("DELETE FROM office WHERE id=?", (oid,))
    db.commit()
    log("master", "事業所を削除", "success", target=f"{row['company_name']}／{row['name']}",
        detail=f"事業所ID={oid}")
    flash(f"「{row['name']}」を削除しました。", "ok")
    return _back_to("orgs")


# ================================================================ 部署
def _dept_counts():
    return {r["dept_id"]: r["c"] for r in get_db().execute(
        "SELECT dept_id, COUNT(*) c FROM member GROUP BY dept_id")}


@app.route("/departments")
@login_required
def departments():
    """旧・部署情報一覧。企業・事業所・部署の一覧（/orgs）に統合（8-58）"""
    return redirect(url_for("orgs"))


def _dept_under(did, dept, cid, oid):
    """加入者の所属に選んだ部署を検証する。部署は事業所を選ばなくても選べる。
    事業所を選んでいなければ、その部署の事業所を所属にする。戻り値は (事業所ID, エラー文)"""
    if not did:
        return oid, None
    if not dept or dept["company_id"] != cid:
        return oid, "選択された部署は、その企業のものではありません。"
    if oid and dept["office_id"] != oid:
        return oid, "選択された部署は、その事業所のものではありません。"
    return (oid or dept["office_id"]), None


def _dept_place(cid, oid):
    """部署の企業・事業所を「企業名／事業所名」または「企業名（事業所なし）」で返す"""
    db = get_db()
    c = db.execute("SELECT name FROM company WHERE id=?", (cid,)).fetchone()
    cn = c["name"] if c else "—"
    if not oid:
        return f"{cn}（事業所なし）"
    o = db.execute("SELECT name FROM office WHERE id=?", (oid,)).fetchone()
    return f"{cn}／{o['name'] if o else '—'}"


def _dept_parent(acc, form):
    """部署の親を入力から決める。戻り値は (企業ID, 事業所IDまたはNone, エラーの一覧)。

    事業所は任意のため、「（事業所なし）」と「事業所」のどちらでもよい。
    画面は2つのプルダウンで選ばせる。「事業所」の値は `company`（事業所なし）か
    `o<事業所ID>`（CSV取込の確認画面と同じ形にそろえている）。"""
    cid = form.get("company_id", type=int)
    sel = (form.get("parent") or "").strip()
    errs = []
    if not owns_company(acc, cid):
        errs.append("選択された企業を操作する権限がありません。")
        return None, None, errs
    if sel == "company":
        return cid, None, errs
    if not (sel.startswith("o") and sel[1:].isdigit()):
        errs.append("事業所を選択してください（事業所を使わない場合は「（事業所なし）」を選びます）。")
        return cid, None, errs
    oid = int(sel[1:])
    if not owns_office(acc, oid):
        errs.append("選択された事業所を操作する権限がありません。")
        return cid, None, errs
    row = get_db().execute("SELECT company_id FROM office WHERE id=?", (oid,)).fetchone()
    if not row or row["company_id"] != cid:
        errs.append("選択された事業所は、その企業のものではありません。")
        return cid, None, errs
    return cid, oid, errs


def _dept_same_name(cid, oid, name, did=None):
    """同じ親に同名の部署がすでにあるか。部署名は一意にしないため、登録は止めずに注意を出す"""
    sql = ("SELECT COUNT(*) c FROM department WHERE company_id=? AND name=?"
           " AND IFNULL(office_id, 0)=?")
    p = [cid, name, oid or 0]
    if did:
        sql += " AND id<>?"
        p.append(did)
    return get_db().execute(sql, p).fetchone()["c"]


@app.route("/departments/new", methods=["GET", "POST"])
@login_required
def department_new():
    db, acc = get_db(), current_account()
    comps, offs = scoped_companies(acc), scoped_offices(acc)
    if request.method == "GET":
        # 「部署を追加」から来たときは、その企業・事業所を選んだ状態で開く
        # （企業の詳細表示からは company_id と parent=company が付く）
        pre = request.args if any(request.args.get(k)
                                  for k in ("office_id", "company_id", "parent")) else None
        return render_template("department_form.html", **ORG_SHELL, row=None, comps=comps, offs=offs, form=pre)
    g = lambda k: (request.form.get(k) or "").strip()
    name, ext = g("name"), g("ext_code")
    cid, oid, errs = _dept_parent(acc, request.form)
    if errs:
        log("master", "部署登録をブロック", "blocked", target=name, detail="／".join(errs))
    if not name:
        errs.append("部署名を入力してください。")
    # 部署コードは一意にしない（取込では複数に一致した行をエラーにして扱う）。
    # ここでは登録を止めず、重複していることだけ知らせる
    dup_code = bool(cid and ext and db.execute(
        "SELECT 1 FROM department WHERE company_id=? AND ext_code=?",
        (cid, ext)).fetchone())
    if errs:
        for e in errs:
            flash(e, "error")
        return render_template("department_form.html", **ORG_SHELL, row=None, comps=comps, offs=offs,
                               form=request.form)
    dup = _dept_same_name(cid, oid, name)
    code = next_code("dept", str(oid or f"c{cid}"), width=3)
    did = db.execute(
        "INSERT INTO department (company_id, office_id, ext_code, code, name, kana)"
        " VALUES (?,?,?,?,?,?)",
        (cid, oid, ext or None, code, name, g("kana"))).lastrowid
    db.commit()
    where = _dept_place(cid, oid)
    log("master", "部署を登録", "success", target=f"{where}／{name}",
        detail=f"部署コード={ext or '（未設定）'}／部署ID={did}"
               + (f"／同名の部署が既に{dup}件あります" if dup else ""))
    flash(f"「{name}」を {where} に登録しました。"
          + (f"部署コードは {ext} です。" if ext else "部署コードは未設定です。")
          + f"（部署ID {did}）", "ok")
    if dup:
        flash(f"{where} には同じ名前の部署が既に {dup} 件あります。"
              f"部署名は重複できるため登録しましたが、取り違えにご注意ください。", "error")
    if dup_code:
        flash(f"部署コード {ext} は、この企業の別の部署でも使われています。"
              f"取込のときに照合できず、その行はエラーになります。", "error")
    return _back_to("orgs")


@app.route("/departments/<int:did>/edit", methods=["GET", "POST"])
@login_required
def department_edit(did):
    db, acc = get_db(), current_account()
    if not owns_department(acc, did):
        log("master", "部署編集をブロック", "blocked", target=str(did), detail="スコープ外")
        flash("対象の部署を操作する権限がありません。", "error")
        return _back_to("orgs")
    row = next(d for d in scoped_departments(acc) if d["id"] == did)
    n_mem = db.execute("SELECT COUNT(*) c FROM member WHERE dept_id=?", (did,)).fetchone()["c"]
    if request.method == "GET":
        return render_template("department_form.html", **ORG_SHELL, row=row, comps=[], offs=[], n_mem=n_mem)
    g = lambda k: (request.form.get(k) or "").strip()
    name = g("name")
    if not name:
        flash("部署名を入力してください。", "error")
        return render_template("department_form.html", **ORG_SHELL, row=row, comps=[], offs=[], n_mem=n_mem,
                               form=request.form)
    ext = g("ext_code")
    dup_code = bool(ext and db.execute(
        "SELECT 1 FROM department WHERE company_id=? AND ext_code=? AND id<>?",
        (row["company_id"], ext, did)).fetchone())
    dup = _dept_same_name(row["company_id"], row["office_id"], name, did)
    db.execute("UPDATE department SET ext_code=?, name=?, kana=?, updated_at=? WHERE id=?",
               (ext or None, name, g("kana"), now(), did))
    db.commit()
    log("master", "部署情報を編集", "success", target=name,
        detail=f"部署ID={did}／変更前: {row['name']}"
               + (f"／同名の部署が既に{dup}件あります" if dup else ""))
    flash(f"「{name}」を更新しました。", "ok")
    if dup:
        flash(f"同じ企業・事業所に同じ名前の部署が {dup} 件あります。取り違えにご注意ください。", "error")
    if dup_code:
        flash(f"部署コード {ext} は、この企業の別の部署でも使われています。"
              f"取込のときに照合できず、その行はエラーになります。", "error")
    return _back_to("orgs")


@app.route("/departments/<int:did>/delete", methods=["GET", "POST"])
@login_required
def department_delete(did):
    """部署の削除。GET は確認画面、POST は実行"""
    db, acc = get_db(), current_account()
    if not owns_department(acc, did):
        flash("対象の部署を操作する権限がありません。", "error")
        return _back_to("orgs")
    row = next((d for d in scoped_departments(acc) if d["id"] == did), None)
    if not row:
        flash("対象の部署が見つかりません。", "error")
        return _back_to("orgs")
    place = _dept_place(row["company_id"], row["office_id"])
    blockers = [
        ("加入者", _count("SELECT COUNT(*) FROM member WHERE dept_id=?", did),
         "加入者一覧の「選択したN件を紐づける」または「企業・部署の紐づけ」で所属先を付け替えてください"),
        ("担当するアカウント", _account_scope_count("dept", did),
         "アカウント管理で担当範囲からこの部署を外してください"),
    ]
    blocked = any(n for _, n, _ in blockers)
    if request.method == "GET":
        return render_template(
            "delete_confirm.html", what="部署", blockers=blockers,
            subject={"name": row["name"], "sub": place, "id_label": "部署ID", "id": did},
            notes=[], alt=None, back_url=_back_url("orgs"))
    if blocked:
        log("master", "部署の削除をブロック", "blocked", target=row["name"],
            detail="／".join(f"{l}{n}件" for l, n, _ in blockers if n) + "が残っている")
        flash(f"「{row['name']}」には紐づいているデータがあるため削除できません。", "error")
        return redirect(url_for("department_delete", did=did))
    db.execute("DELETE FROM department WHERE id=?", (did,))
    db.commit()
    log("master", "部署を削除", "success", target=row["name"], detail=place)
    flash(f"「{row['name']}」を削除しました。", "ok")
    return _back_to("orgs")


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
    企業・事業所を「健保・企業が管理する番号（コード）」または「HIAの通し番号（ID）」で照合する。"""
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
            ng.append(dict(r, reason=f"事業所コード「{r['so']}」が企業「{comp['name']}」にありません",
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
    return render_template("members_link.html", comps=comps, offs=offs, depts=depts,
                           counts=counts, ocounts=ocounts, dcounts=dcounts,
                           n_unlinked=n_unlinked)


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
    dept = next((d for d in scoped_departments(acc) if d["id"] == did), None) if did else None
    oid, derr = _dept_under(did, dept, cid, oid)
    if derr:
        return {"ok": False, "message": derr}
    off = next((o for o in offs if o["id"] == oid), None) if oid else None

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
    if oid and (not off or off["company_id"] != cid):
        return {"ok": False, "message": "選択された事業所は、その企業のものではありません。"}
    dept = next((d for d in scoped_departments(acc) if d["id"] == did), None) if did else None
    oid, derr = _dept_under(did, dept, cid, oid)
    if derr:
        return {"ok": False, "message": derr}
    off = next((o for o in offs if o["id"] == oid), None) if oid else None
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
    place = (comp['name'] + (f"／{off['name']}" if off else "")
             + (f"／{dept['name']}" if dept else ""))
    log("master", "加入者に企業・事業所・部署を紐づけ", "success", target=f"{len(targets)}件",
        detail=f"{comp['code']} {comp['name']}"
               + (f"／{off['code']} {off['name']}" if off else "／事業所なし")
               + (f"／{dept['code']} {dept['name']}" if dept else "／部署なし")
               + f"／対象={','.join(t['member_no'] for t in targets[:20])}"
               + ("…" if len(targets) > 20 else ""))
    return {"ok": True, "linked": len(targets), "place": place,
            "message": f"{len(targets)}件を「{place}」に紐づけました。"}


MEMBER_FORM_FIELDS = ("cert_mark", "cert_branch", "attr", "relation", "kana", "sex",
                      "qualified_at", "lost_at", "zip", "pref", "city", "address", "address2", "tel",
                      "email", "billing_code", "employee_code", "kenpo_member_id",
                      "memo")


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
    oid, derr = _dept_under(did, dept, cid, oid)
    if derr:
        errs.append(derr)
    off = next((o for o in offs if o["id"] == oid), None) if oid else None
    if cid and not comp:
        log("master", "加入者登録をブロック", "blocked", target=no, detail="スコープ外の企業")
        errs.append("選択された企業を操作する権限がありません。")
    if oid and (not off or off["company_id"] != cid):
        errs.append("選択された事業所は、その企業のものではありません。")
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
                               depts=depts, mypage=member_services(db, row),
                               photos=photos_of(db, mid), PHOTO_KINDS=PHOTO_KINDS)
    cid = request.form.get("company_id", type=int)
    oid = request.form.get("office_id", type=int)
    did = request.form.get("dept_id", type=int)
    name = (request.form.get("name") or "").strip()
    no = (request.form.get("member_no") or "").strip()
    vals, errs = _member_form_values(request.form)
    comp = next((c for c in comps if c["id"] == cid), None) if cid else None
    off = next((o for o in offs if o["id"] == oid), None) if oid else None
    dept = next((d for d in depts if d["id"] == did), None) if did else None
    oid, derr = _dept_under(did, dept, cid, oid)
    if derr:
        errs.append(derr)
    off = next((o for o in offs if o["id"] == oid), None) if oid else None
    if cid and not comp:
        errs.append("選択された企業を操作する権限がありません。")
    if oid and (not off or off["company_id"] != cid):
        errs.append("選択された事業所は、その企業のものではありません。")
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
                               mypage=member_services(db, row),
                               photos=photos_of(db, mid), PHOTO_KINDS=PHOTO_KINDS)
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


@app.route("/members/<int:mid>/delete", methods=["GET", "POST"])
@login_required
def member_delete(mid):
    """加入者の削除。GET は確認画面、POST は実行。

    退職・脱退は削除ではなく「資格喪失日」を入れる（健診の履歴などを残すため）。
    削除は登録の誤りを取り消すときだけ。健診結果・面談の記録などが残っていれば削除できない。"""
    db, acc = get_db(), current_account()
    where, params = member_where(acc)
    # 企業に紐づいていない加入者も削除の対象になる（LEFT JOIN）
    row = db.execute("SELECT m.*, c.name AS company_name FROM member m"
                     " LEFT JOIN company c ON c.id=m.company_id WHERE m.id=? AND " + where,
                     [mid] + params).fetchone()
    if not row:
        flash("対象の加入者を操作する権限がありません。", "error")
        return redirect(url_for("members"))
    blockers = [
        ("健診結果", _count("SELECT COUNT(*) FROM kenshin_result WHERE member_id=?", mid)
                    + _count("SELECT COUNT(*) FROM oh_kenshin WHERE member_id=?", mid),
         "健診結果がある加入者は削除できません。退職・脱退なら資格喪失日を入れてください"),
        ("疾患予測の結果", _count("SELECT COUNT(*) FROM risk_score WHERE member_id=?", mid),
         "予測結果がある加入者は削除できません。退職・脱退なら資格喪失日を入れてください"),
        ("産業医面談の記録",
         sum(_count(f"SELECT COUNT(*) FROM {t} WHERE member_id=?", mid)
             for t in ("oh_interview", "oh_candidate", "oh_memo", "oh_stress",
                       "oh_overtime")),
         "面談の記録がある加入者は削除できません。退職・脱退なら資格喪失日を入れてください"),
    ]
    blocked = any(n for _, n, _ in blockers)
    if request.method == "GET":
        return render_template(
            "delete_confirm.html", what="加入者", blockers=blockers,
            subject={"name": row["name"],
                     "sub": f"被保険者証番号 {row['member_no']}"
                            + (f"／{row['company_name']}" if row["company_name"]
                               else "／未紐づけ"),
                     "id_label": "加入者ID", "id": mid},
            notes=["<b>退職・脱退は削除ではありません。</b>加入者の編集画面で"
                   "<b>資格喪失日（家族は削除日）</b>を入れてください。"
                   "健診の履歴などはそのまま残り、以後の対象者からは外れます。",
                   "削除は<b>登録そのものが誤りだったとき</b>（二重登録・別人の取り込みなど）"
                   "に使います。削除すると元に戻せません。"],
            alt={"url": url_for("member_edit", mid=mid), "label": "資格喪失日を入れる（編集へ）"},
            back_url=url_for("members"))
    if blocked:
        log("master", "加入者の削除をブロック", "blocked", target=row["member_no"],
            detail="／".join(f"{l}{n}件" for l, n, _ in blockers if n) + "が残っている")
        flash("紐づいているデータがあるため削除できません。", "error")
        return redirect(url_for("member_delete", mid=mid))
    db.execute("DELETE FROM member WHERE id=?", (mid,))
    db.commit()
    log("master", "加入者を削除", "success", target=row["member_no"],
        detail=f"{row['company_name'] or '未紐づけ'}／{row['name']}")
    flash(f"被保険者証番号 {row['member_no']}（{row['name']}）を削除しました。", "ok")
    return redirect(url_for("members"))




# ================================================================ 判定マスタ
# 検査項目ごとの判定基準（判定区分・性別・下限値・上限値）を登録します。
# 基準は「健保共通」と「企業ごと」の2段で持ち、企業ごとの設定があればそれを使います。
JUDGE_CODES = [("A", "A：異常なし"), ("B", "B：軽度異常"), ("C", "C：要再検査・生活改善"),
               ("D", "D：要精密検査・治療"), ("E", "E：治療中")]
JUDGE_SEXES = ["共通", "男性", "女性"]


# 区分番号（特定健診XML 健診項目コード表の区分。HIA総合管理の検査マスタと同じ定義）
JUDGE_SECTIONS = [
    ("01", "受診情報"), ("02", "基本情報・診察"), ("03", "身体計測"), ("04", "血圧"),
    ("05", "血中脂質検査"), ("06", "肝機能検査"), ("07", "血糖検査"),
    ("08", "尿・腎機能検査"), ("09", "血液学的検査"), ("10", "心電図検査"),
    ("11", "眼底検査"), ("12", "その他の検査"), ("13", "医師の判断"),
    ("14", "問診（質問票）"), ("15", "メタボリックシンドローム判定"), ("16", "保健指導"),
]
SECTION_NAME = dict(JUDGE_SECTIONS)


def judge_items(db, sec="", q="", code=""):
    """検査項目マスタ（区分番号・項目名・コードで絞り込める）"""
    sql, p = "SELECT * FROM judge_item WHERE 1=1", []
    if sec:
        sql += " AND sec_no=?"
        p.append(sec)
    if q:
        sql += " AND name LIKE ?"
        p.append(f"%{q}%")
    if code:
        sql += " AND code LIKE ?"
        p.append(f"%{code}%")
    return db.execute(sql + " ORDER BY sort, id", p).fetchall()


def _criteria_scope(acc, cid):
    """判定基準の対象（健保・企業）を決める。企業を選ばなければ健保共通"""
    if acc["role"] == "system_admin":
        kid = acc["kenpo_id"]
    else:
        kid = acc["kenpo_id"]
    return kid, (cid or None)


def criteria_rows(db, kid, cid, fy, item_id=None):
    sql = ("SELECT c.*, i.name AS item_name, i.unit AS item_unit FROM judge_criteria c"
           " JOIN judge_item i ON i.id=c.item_id"
           " WHERE c.kenpo_id=? AND c.fiscal_year=? AND c.company_id IS ?")
    p = [kid, fy, cid]
    if item_id:
        sql += " AND c.item_id=?"
        p.append(item_id)
    return db.execute(sql + " ORDER BY i.sort, c.sort, c.id", p).fetchall()


def norm_item_name(v):
    """検査項目名を突き合わせやすい形にそろえる（全角・半角・空白の違いを無視する）"""
    import unicodedata
    t = unicodedata.normalize("NFKC", (v or "").strip()).lower()
    return re.sub(r"[\s　・]", "", t)


# 取込CSVの列名 → 判定マスタの検査項目名（呼び方の違いを吸収する）
ITEM_ALIASES = {
    "収縮期血圧": "収縮期血圧(その他)", "拡張期血圧": "拡張期血圧(その他)",
    "hba1c": "HbA1c(NGSP値)", "中性脂肪": "空腹時中性脂肪(トリグリセリド)",
    "血糖": "空腹時血糖", "ldl": "LDLコレステロール", "hdl": "HDLコレステロール",
    "ast": "AST(GOT)", "alt": "ALT(GPT)", "γ-gt": "γ-GT(γ-GTP)", "γ-gtp": "γ-GT(γ-GTP)",
}


def judge_item_by_name(db, name):
    """検査項目名（取込CSVの列名など）から判定マスタの項目を引く"""
    if not hasattr(g, "_jitems"):
        g._jitems = {norm_item_name(r["name"]): r
                     for r in db.execute("SELECT * FROM judge_item")}
    key = norm_item_name(name)
    row = g._jitems.get(key)
    if row:
        return row
    alias = ITEM_ALIASES.get(key)
    return g._jitems.get(norm_item_name(alias)) if alias else None


def judge_defaults_of(db, code):
    """HIA総合管理の判定マスタに入っている既定の基準"""
    return db.execute("SELECT * FROM judge_default WHERE item_code=?"
                      " ORDER BY sort, id", (code,)).fetchall()


def reflect_judge_defaults(db, kenpo_id, fy, item_ids=None):
    """健保共通の基準が無い項目に、HIA総合管理の判定マスタの内容を反映する。

    「HIA健保管理に設定が無い場合は、HIA総合管理の判定マスタの情報を反映する」ための処理です。
    反映するのは健保共通の基準で、企業ごとの基準は上書きしません。
    戻り値は (反映した項目数, 反映した件数)。
    """
    items = db.execute("SELECT * FROM judge_item ORDER BY sort, id").fetchall()
    if item_ids is not None:
        keep = set(item_ids)
        items = [i for i in items if i["id"] in keep]
    n_item = n_row = 0
    for it in items:
        has = db.execute("SELECT COUNT(*) c FROM judge_criteria WHERE kenpo_id=?"
                         " AND company_id IS NULL AND item_id=? AND fiscal_year=?",
                         (kenpo_id, it["id"], fy)).fetchone()["c"]
        if has:
            continue
        rows = judge_defaults_of(db, it["code"])
        if not rows:
            continue
        for r in rows:
            db.execute("INSERT INTO judge_criteria (kenpo_id, company_id, item_id,"
                       " fiscal_year, judge, sex, lo, hi, sort, updated_at)"
                       " VALUES (?, NULL, ?,?,?,?,?,?,?,?)",
                       (kenpo_id, it["id"], fy, r["judge"], r["sex"], r["lo"], r["hi"],
                        r["sort"], now()))
            n_row += 1
        n_item += 1
    if n_item:
        db.commit()
        log("master", "判定マスタを反映（HIA総合管理 → HIA健保管理）", "success",
            target=f"{fy}年度", detail=f"{n_item}項目・{n_row}件を健保共通として登録")
    return n_item, n_row


def criteria_map(db, kenpo_id, company_id, fy):
    """判定に使う基準を項目IDごとに返す（企業ごとの基準があれば健保共通より優先）"""
    sql = ("SELECT * FROM judge_criteria WHERE kenpo_id=? AND fiscal_year=?"
           " AND company_id IS ? ORDER BY sort, id")
    out = {}
    for r in db.execute(sql, (kenpo_id, fy, None)):     # 健保共通
        out.setdefault(r["item_id"], []).append(r)
    if company_id:
        mine = {}
        for r in db.execute(sql, (kenpo_id, fy, company_id)):
            mine.setdefault(r["item_id"], []).append(r)
        out.update(mine)                                # 企業ごとの基準で置き換える
    return out


def judge_by_rules(rules, value, sex=""):
    """検査値を判定基準に当てて A〜E を求める（該当が無ければ None）"""
    try:
        v = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    sx = "男性" if str(sex).startswith("男") else ("女性" if str(sex).startswith("女") else "")
    best = None
    for r in rules:
        rsex = r["sex"] or "共通"
        if rsex != "共通" and rsex != sx:
            continue
        lo, hi = r["lo"], r["hi"]
        try:
            if lo not in (None, "") and v < float(lo):
                continue
            # 上限値は「以下」（hi と同じ値もその判定に含めます）
            if hi not in (None, "") and v > float(hi):
                continue
        except ValueError:
            continue
        # 性別を指定した基準を優先する
        if best is None or (rsex != "共通" and (best[1] == "共通")):
            best = (r["judge"], rsex)
    return best[0] if best else None


@app.route("/criteria")
@login_required
def criteria():
    """判定マスタ（企業ごと／健保共通の判定基準の一覧）"""
    db, acc = get_db(), current_account()
    comps = scoped_companies(acc)
    cid = request.args.get("company_id", type=int)
    if cid and not owns_company(acc, cid):
        flash("選択された企業を操作する権限がありません。", "error")
        return redirect(url_for("criteria"))
    fy = (request.args.get("fy") or str(_current_fy())).strip()
    sec = (request.args.get("sec") or "").strip()
    q = (request.args.get("q") or "").strip()
    code = (request.args.get("code") or "").strip()
    kid, cid = _criteria_scope(acc, cid)
    # 健保共通の基準が無い項目には、既定値（日本人間ドック学会の判定区分）を入れておきます
    if kid:
        reflect_judge_defaults(db, kid, fy)
    items = judge_items(db, sec, q, code)
    mine = {}
    for r in criteria_rows(db, kid, cid, fy):
        mine.setdefault(r["item_id"], []).append(r)
    common = {}
    if cid:
        for r in criteria_rows(db, kid, None, fy):
            common.setdefault(r["item_id"], []).append(r)
    n_all = db.execute("SELECT COUNT(*) c FROM judge_item").fetchone()["c"]
    return render_template("criteria.html", items=items, comps=comps, cid=cid, fy=fy,
                           mine=mine, common=common, years=_fy_options(),
                           sections=JUDGE_SECTIONS, SECTION_NAME=SECTION_NAME,
                           sec=sec, q=q, code=code, n_all=n_all)


@app.route("/criteria/<int:item_id>", methods=["GET", "POST"])
@login_required
def criteria_edit(item_id):
    """判定マスタ 編集（検査項目1つ分の判定基準）"""
    db, acc = get_db(), current_account()
    comps = scoped_companies(acc)
    src = request.form if request.method == "POST" else request.args
    cid = src.get("company_id", type=int)
    if cid and not owns_company(acc, cid):
        flash("選択された企業を操作する権限がありません。", "error")
        return redirect(url_for("criteria"))
    fy = (src.get("fy") or str(_current_fy())).strip()
    kid, cid = _criteria_scope(acc, cid)
    item = db.execute("SELECT * FROM judge_item WHERE id=?", (item_id,)).fetchone()
    if not item:
        flash("検査項目が見つかりません。", "error")
        return redirect(url_for("criteria"))
    if request.method == "GET":
        return render_template("criteria_edit.html", item=item, comps=comps, cid=cid,
                               fy=fy, rows=criteria_rows(db, kid, cid, fy, item_id),
                               common=criteria_rows(db, kid, None, fy, item_id) if cid
                               else [], JUDGE_CODES=JUDGE_CODES,
                               JUDGE_SEXES=JUDGE_SEXES, years=_fy_options(),
                               SECTION_NAME=SECTION_NAME)
    # 保存（画面の行をそのまま入れ替える）
    judges = request.form.getlist("judge")
    sexes = request.form.getlist("sex")
    los = request.form.getlist("lo")
    his = request.form.getlist("hi")
    errs, keep = [], []
    for i, j in enumerate(judges):
        j = (j or "").strip().upper()
        if j not in [c for c, _ in JUDGE_CODES]:
            continue
        sx = (sexes[i] if i < len(sexes) else "共通") or "共通"
        lo = (los[i] if i < len(los) else "").strip()
        hi = (his[i] if i < len(his) else "").strip()
        for v, label in ((lo, "下限値"), (hi, "上限値")):
            if v:
                try:
                    float(v)
                except ValueError:
                    errs.append(f"{i + 1}行目の{label}は数値で入力してください（{v}）。")
        if lo and hi:
            try:
                if float(lo) > float(hi):
                    errs.append(f"{i + 1}行目は下限値が上限値より大きくなっています。")
            except ValueError:
                pass
        keep.append((j, sx if sx in JUDGE_SEXES else "共通", lo or None, hi or None))
    if errs:
        for e in errs:
            flash(e, "error")
        return render_template("criteria_edit.html", item=item, comps=comps, cid=cid,
                               fy=fy, rows=criteria_rows(db, kid, cid, fy, item_id),
                               common=criteria_rows(db, kid, None, fy, item_id) if cid
                               else [], JUDGE_CODES=JUDGE_CODES,
                               JUDGE_SEXES=JUDGE_SEXES, years=_fy_options(),
                               SECTION_NAME=SECTION_NAME)
    db.execute("DELETE FROM judge_criteria WHERE kenpo_id=? AND company_id IS ?"
               " AND item_id=? AND fiscal_year=?", (kid, cid, item_id, fy))
    for n, (j, sx, lo, hi) in enumerate(keep, start=1):
        db.execute("INSERT INTO judge_criteria (kenpo_id, company_id, item_id,"
                   " fiscal_year, judge, sex, lo, hi, sort, updated_at)"
                   " VALUES (?,?,?,?,?,?,?,?,?,?)",
                   (kid, cid, item_id, fy, j, sx, lo, hi, n * 10, now()))
    db.commit()
    place = next((c["name"] for c in comps if c["id"] == cid), None) if cid else "健保共通"
    log("master", "判定マスタを更新", "success", target=f"{item['name']}（{fy}年度）",
        detail=f"対象={place}／{len(keep)}件を登録")
    flash(f"{item['name']}の判定基準（{place}・{fy}年度）を{len(keep)}件で更新しました。", "ok")
    return redirect(url_for("criteria", company_id=cid, fy=fy))


# ================================================================ 加入者ごとの写真
# 採血結果などの写真を「個人ごと」に取り込み、加入者マスタ（加入者の編集画面）で見ます。
# ファイル本体は uploads/member_photos/<加入者ID>/ に置き、DBには場所と情報だけ持ちます。
PHOTO_DIR = os.path.join(BASE_DIR, "uploads", "member_photos")
PHOTO_TYPES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
               ".webp": "image/webp", ".gif": "image/gif", ".heic": "image/heic",
               ".pdf": "application/pdf"}
MAX_PHOTO_MB = int(os.environ.get("HIA_MAX_PHOTO_MB", "10"))
PHOTO_KINDS = ["採血結果", "健診結果票", "問診票", "同意書", "その他"]


def member_in_scope(db, acc, mid):
    """権限の範囲にある加入者を1件返す（範囲外なら None）"""
    where, params = member_where(acc)
    return db.execute(
        "SELECT m.*, c.name AS company_name, o.name AS office_name, d.name AS dept_name"
        " FROM member m LEFT JOIN company c ON c.id=m.company_id"
        " LEFT JOIN office o ON o.id=m.office_id LEFT JOIN department d ON d.id=m.dept_id"
        " WHERE m.id=? AND " + where, [mid] + params).fetchone()


def photos_of(db, mid):
    return db.execute("SELECT * FROM member_photo WHERE member_id=?"
                      " ORDER BY id DESC", (mid,)).fetchall()


def photo_counts(db, ids):
    """加入者ごとの写真の枚数"""
    if not ids:
        return {}
    q = ",".join("?" * len(ids))
    return {r["member_id"]: r["n"] for r in db.execute(
        f"SELECT member_id, COUNT(*) n FROM member_photo WHERE member_id IN ({q})"
        " GROUP BY member_id", list(ids))}


def _photo_path(mid, filename):
    return os.path.join(PHOTO_DIR, str(mid), filename)


@app.route("/members/photos")
@login_required
def member_photos_page():
    """写真の取込（個人ごと）。加入者を探して選び、写真を取り込みます。"""
    db, acc = get_db(), current_account()
    q = (request.args.get("q") or "").strip()
    mid = request.args.get("mid", type=int)
    where, params = member_where(acc)
    rows, sel, photos = [], None, []
    if q:
        like = f"%{q}%"
        rows = db.execute(
            "SELECT m.id, m.name, m.kana, m.subscriber_id, m.employee_code, m.member_no,"
            " c.name AS company_name, o.name AS office_name"
            " FROM member m LEFT JOIN company c ON c.id=m.company_id"
            " LEFT JOIN office o ON o.id=m.office_id"
            " WHERE " + where + " AND (m.subscriber_id LIKE ? OR m.employee_code LIKE ?"
            " OR m.member_no LIKE ? OR m.name LIKE ? OR m.kana LIKE ?)"
            " ORDER BY m.subscriber_id, m.member_no LIMIT 50",
            params + [like] * 5).fetchall()
    if mid:
        sel = member_in_scope(db, acc, mid)
        if not sel:
            flash("対象の加入者を操作する権限がありません。", "error")
            return redirect(url_for("member_photos_page"))
        photos = photos_of(db, mid)
    n_all = db.execute("SELECT COUNT(*) c FROM member_photo p JOIN member m"
                       " ON m.id=p.member_id WHERE " + where, params).fetchone()["c"]
    return render_template("member_photos.html", q=q, rows=rows, sel=sel, photos=photos,
                           counts=photo_counts(db, [r["id"] for r in rows]),
                           kinds=PHOTO_KINDS, max_mb=MAX_PHOTO_MB,
                           exts=sorted(PHOTO_TYPES), n_all=n_all)


@app.route("/members/<int:mid>/photos", methods=["POST"])
@login_required
def member_photo_upload(mid):
    """写真を取り込む（1人に何枚でも）"""
    return save_member_photos(mid)


def save_member_photos(mid, back=None, by_self=False):
    """写真の取込の本体。担当者の画面と、加入者ご本人のマイページで共通に使う。

    by_self=True は加入者ご本人が自分の写真を取り込む場合（操作ログにその旨を残す）。
    """
    db, acc = get_db(), current_account()
    row = member_in_scope(db, acc, mid)
    if not row:
        log("import", "写真の取込をブロック", "blocked", target=str(mid), detail="スコープ外")
        flash("対象の加入者を操作する権限がありません。", "error")
        return redirect(back or url_for("member_photos_page"))
    files = [f for f in request.files.getlist("photos") if f and f.filename]
    kind = (request.form.get("kind") or "").strip()
    taken_on = _fmt_date(request.form.get("taken_on") or "")
    note = (request.form.get("note") or "").strip()
    back = back or request.form.get("back") or url_for("member_photos_page", mid=mid)
    if not files:
        flash("取り込む写真を選択してください。", "error")
        return redirect(back)
    os.makedirs(_photo_path(mid, ""), exist_ok=True)
    ok, ng = 0, []
    for fs in files:
        ext = os.path.splitext(fs.filename)[1].lower()
        if ext not in PHOTO_TYPES:
            ng.append(f"{fs.filename}（対応していない形式）")
            continue
        data = fs.read()
        if len(data) > MAX_PHOTO_MB * 1024 * 1024:
            ng.append(f"{fs.filename}（{MAX_PHOTO_MB}MBを超えています）")
            continue
        name = uuid.uuid4().hex + ext
        with open(_photo_path(mid, name), "wb") as out:
            out.write(data)
        db.execute("INSERT INTO member_photo (member_id, kind, filename, orig_name, mime,"
                   " bytes, taken_on, note, uploaded_by) VALUES (?,?,?,?,?,?,?,?,?)",
                   (mid, kind or None, name, fs.filename, PHOTO_TYPES[ext], len(data),
                    taken_on or None, note or None, acc["email"]))
        ok += 1
    db.commit()
    log("import", "加入者の写真を取込", "success" if ok else "failure",
        target=row["subscriber_id"] or row["member_no"],
        detail=f"{ok}件を登録／区分={kind or '未設定'}"
               + ("／ご本人による取込" if by_self else "")
               + (f"／取込できなかったファイル{len(ng)}件" if ng else ""))
    if ok:
        flash(f"写真を{ok}件取り込みました。", "ok")
    for m in ng:
        flash("取り込めませんでした：" + m, "error")
    return redirect(back)


@app.route("/members/<int:mid>/photos/<int:pid>")
@login_required
def member_photo_file(mid, pid):
    """写真そのものを返す（権限の範囲の加入者だけ）"""
    db, acc = get_db(), current_account()
    if not member_in_scope(db, acc, mid):
        log("view", "写真の閲覧をブロック", "blocked", target=str(mid), detail="スコープ外")
        return Response("この写真を見る権限がありません。", status=403,
                        mimetype="text/plain; charset=utf-8")
    p = db.execute("SELECT * FROM member_photo WHERE id=? AND member_id=?",
                   (pid, mid)).fetchone()
    path = _photo_path(mid, p["filename"]) if p else None
    if not path or not os.path.exists(path):
        return Response("写真が見つかりません。", status=404,
                        mimetype="text/plain; charset=utf-8")
    with open(path, "rb") as f:
        return Response(f.read(), headers={
            "Content-Type": p["mime"] or "application/octet-stream",
            "Content-Disposition": "inline",
            "Cache-Control": "private, max-age=0, no-store"})


@app.route("/members/<int:mid>/photos/<int:pid>/delete", methods=["POST"])
@login_required
def member_photo_delete(mid, pid):
    db, acc = get_db(), current_account()
    row = member_in_scope(db, acc, mid)
    if not row:
        flash("対象の加入者を操作する権限がありません。", "error")
        return redirect(url_for("member_photos_page"))
    p = db.execute("SELECT * FROM member_photo WHERE id=? AND member_id=?",
                   (pid, mid)).fetchone()
    if p:
        path = _photo_path(mid, p["filename"])
        if os.path.exists(path):
            os.remove(path)
        db.execute("DELETE FROM member_photo WHERE id=?", (pid,))
        db.commit()
        log("master", "加入者の写真を削除", "success",
            target=row["subscriber_id"] or row["member_no"],
            detail=f"{p['kind'] or '区分未設定'}／{p['orig_name'] or p['filename']}")
        flash("写真を削除しました。", "ok")
    return redirect(request.form.get("back") or url_for("member_photos_page", mid=mid))


# 取込ファイルの見出しの旧名称（そのまま取り込めるようにする）
HEADER_ALIASES = {"事業所（企業）コード": "企業コード", "所属コード": "事業所コード"}


def read_table(fs):
    raw = fs.read()
    for enc in ("utf-8-sig", "cp932", "utf-8"):
        try:
            rows = list(csv.DictReader(io.StringIO(raw.decode(enc))))
        except UnicodeDecodeError:
            continue
        for r in rows:
            for old, new in HEADER_ALIASES.items():
                if old in r and new not in r:
                    r[new] = r.pop(old)
        return rows
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


# ================================================================ CSV取込（5様式共通）
# 列定義は CSVフォーマット.xlsx（版0.23）のとおり。
#   ・呼び名は「ID」＝HIAが採番する通し番号／「コード」＝健保・企業が管理する番号
#   ・階層別の3様式と加入者情報は接頭辞を付けない（現行の列名をそのまま使う）
#   ・一括取込だけは1行に企業・事業所・部署が並ぶため、階層名を接頭辞として付ける
#     （代表者名・郵便番号・住所・電話番号・担当メールアドレスが企業と事業所で衝突するため）
#   ・取込先の健保はCSVの列では指定せず、画面に出す（保険者番号の列は置かない）
#   ・事業所・部署の上位は行ごとに列で指定し、空欄の行だけ確認画面で選ぶ
COMPANY_COLUMNS = ["企業ID", "企業コード", "企業名", "企業名（フリガナ）", "被保険者証記号",
                   "代表者名", "郵便番号", "住所", "電話番号", "担当メールアドレス"]
OFFICE_COLUMNS = ["企業ID", "企業コード", "事業所ID", "事業所コード", "事業所名",
                  "事業所名（フリガナ）", "被保険者証記号", "代表者名", "郵便番号",
                  "住所", "電話番号", "担当メールアドレス"]
DEPT_COLUMNS = ["企業ID", "企業コード", "事業所ID", "事業所コード",
                "部署ID", "部署コード", "部署名", "部署名（フリガナ）"]
BULK_COLUMNS = ["企業ID", "企業コード", "企業名", "企業名（フリガナ）", "企業被保険者証記号",
                "企業代表者名", "企業郵便番号", "企業住所", "企業電話番号",
                "企業担当メールアドレス",
                "事業所ID", "事業所コード", "事業所名", "事業所名（フリガナ）",
                "事業所被保険者証記号", "事業所代表者名", "事業所郵便番号", "事業所住所",
                "事業所電話番号", "事業所担当メールアドレス",
                "部署ID", "部署コード", "部署名", "部署名（フリガナ）"]
MEMBER_COLUMNS = ["被保険者証記号", "被保険者証番号", "被保険者証枝番", "続柄名称",
                  "対象者氏名（漢字）", "対象者氏名（カナ）", "性別", "生年月日",
                  "資格取得日（家族認定日）", "資格喪失日（家族削除日）", "被保険者属性名",
                  "郵便番号", "住所", "住所（建物名）", "電話番号", "メールアドレス",
                  "社員コード", "配付先コード", "connectID",
                  "インフルエンザ予防接種対象", "健診対象",
                  "企業ID", "企業コード", "事業所ID", "事業所コード", "部署ID", "部署コード"]

# 「必要な列を確認する」に出す定義。(列名, 必須か, 型・桁, 説明)
# CSVフォーマット.xlsx の各シートを写したもの。型・桁は取込の検証にもそのまま使う。
_ID = "HIAが採番する通し番号です。様式をダウンロードすると埋まって出てきます。照合キー②に使います。"
_CODE = "健保・企業が管理する番号です。空欄でも構いません。入力されていればIDより優先して照合します。照合キー①に使います。"
_UP = ("<b>上位の指定に使います。</b>ID・コードのどちらか一方が入力されていれば足ります。"
       "両方とも空欄の行は、確認画面で選んでいただきます。"
       "両方に入力があり指す先が異なる場合はエラーになります")
COMPANY_HELP = [
    ("企業ID", "任意", "数字", _ID),
    ("企業コード", "任意", "文字 100", _CODE),
    ("企業名", "必須", "文字 1〜255", "企業の名称です。照合キー③の一部として使います"),
    ("企業名（フリガナ）", "任意", "文字 1〜255", "照合キー③の一部として使います"),
    ("被保険者証記号", "必須", "文字 20", "企業の被保険者証記号です。照合キー③の一部として使います"),
    ("代表者名", "任意", "文字 1〜100", "—"),
    ("郵便番号", "任意", "数字7桁", "ハイフンなしでご入力ください"),
    ("住所", "任意", "文字 1〜255", "都道府県・市区町村・番地以下に分けず、1列でご入力ください"),
    ("電話番号", "任意", "数字10〜11桁", "数字のみでご入力ください。ハイフンは使用できません"),
    ("担当メールアドレス", "任意", "文字 1〜255", "この企業の窓口として使用します。通知の送信には使用しません"),
]
OFFICE_HELP = [
    ("企業ID", "任意", "数字", _UP),
    ("企業コード", "任意", "文字 100", _UP),
    ("事業所ID", "任意", "数字", _ID),
    ("事業所コード", "任意", "文字 100", _CODE),
    ("事業所名", "必須", "文字 1〜255",
     "事業所の名称です。照合キー③として使います（上位の企業の中から探します）"),
    ("事業所名（フリガナ）", "任意", "文字 1〜255", "—"),
    ("被保険者証記号", "任意", "文字 20",
     "事業所の被保険者証記号です。<b>健診データ連携のファイル名に使用します。</b>"
     "初期値は企業の値を引き継ぎ、以後は事業所の値を優先します"),
    ("代表者名", "任意", "文字 1〜100", "—"),
    ("郵便番号", "任意", "数字7桁", "ハイフンなしでご入力ください"),
    ("住所", "任意", "文字 1〜255", "企業と同じく1列でご入力ください"),
    ("電話番号", "任意", "数字10〜11桁", "数字のみでご入力ください。ハイフンは使用できません"),
    ("担当メールアドレス", "任意", "文字 1〜255", "—"),
]
DEPT_HELP = [
    ("企業ID", "任意", "数字", _UP),
    ("企業コード", "任意", "文字 100", _UP),
    ("事業所ID", "任意", "数字",
     _UP + "。両方とも空欄の行は「（事業所なし）」か「事業所」を確認画面で選んでいただきます"
           "（空欄を事業所なしとは判断しません）"),
    ("事業所コード", "任意", "文字 100",
     _UP + "。両方とも空欄の行は「（事業所なし）」か「事業所」を確認画面で選んでいただきます"
           "（空欄を事業所なしとは判断しません）"),
    ("部署ID", "任意", "数字", _ID),
    ("部署コード", "任意", "文字 100", _CODE),
    ("部署名", "必須", "文字 1〜255",
     "部署の名称です。照合キー③として使います（上位の中から探します）。部署名は一意ではないため、"
     "IDもコードも空欄で同じ名称が複数ある場合は、新規登録として扱います"),
    ("部署名（フリガナ）", "任意", "文字 1〜255", "—"),
]
_BULK_NOTE = "｜この様式だけ、階層名を接頭辞として付けています（企業と事業所で列名が重複するためです）"
BULK_HELP = [
    ("企業ID", "任意", "数字", _ID),
    ("企業コード", "任意", "文字 100", _CODE),
    ("企業名", "必須", "文字 1〜255", "企業の名称です。照合キー③の一部として使います"),
    ("企業名（フリガナ）", "任意", "文字 1〜255", "照合キー③の一部として使います"),
    ("企業被保険者証記号", "必須", "文字 20", "照合キー③の一部として使います" + _BULK_NOTE),
    ("企業代表者名", "任意", "文字 1〜100", "—" + _BULK_NOTE),
    ("企業郵便番号", "任意", "数字7桁", "ハイフンなしでご入力ください" + _BULK_NOTE),
    ("企業住所", "任意", "文字 1〜255", "1列でご入力ください" + _BULK_NOTE),
    ("企業電話番号", "任意", "数字10〜11桁", "数字のみでご入力ください" + _BULK_NOTE),
    ("企業担当メールアドレス", "任意", "文字 1〜255", "—" + _BULK_NOTE),
    ("事業所ID", "任意", "数字", _ID),
    ("事業所コード", "任意", "文字 100", _CODE),
    ("事業所名", "任意", "文字 1〜255",
     "<b>空欄の行は事業所を作成しません</b>（その行の部署は事業所なしで登録されます）。照合キー③として使います"),
    ("事業所名（フリガナ）", "任意", "文字 1〜255", "—"),
    ("事業所被保険者証記号", "任意", "文字 20",
     "健診データ連携のファイル名に使用します" + _BULK_NOTE),
    ("事業所代表者名", "任意", "文字 1〜100", "—" + _BULK_NOTE),
    ("事業所郵便番号", "任意", "数字7桁", "ハイフンなしでご入力ください" + _BULK_NOTE),
    ("事業所住所", "任意", "文字 1〜255", "1列でご入力ください" + _BULK_NOTE),
    ("事業所電話番号", "任意", "数字10〜11桁", "数字のみでご入力ください" + _BULK_NOTE),
    ("事業所担当メールアドレス", "任意", "文字 1〜255", "—" + _BULK_NOTE),
    ("部署ID", "任意", "数字", _ID),
    ("部署コード", "任意", "文字 100", _CODE),
    ("部署名", "任意", "文字 1〜255",
     "<b>空欄の行は部署を作成しません</b>（企業・事業所のみの登録になります）。照合キー③として使います"),
    ("部署名（フリガナ）", "任意", "文字 1〜255", "—"),
]
_BELONG = ("所属先の指定です。すべて任意項目のため、指定がなければ未紐づけのまま登録します。"
           "下位の項目を入力すると、上位は自動的に補います（部署→事業所→企業の順）。"
           "同じ階層でIDとコードの両方を入力し、指す先が異なる場合はエラーになります")
MEMBER_HELP = [
    ("被保険者証記号", "必須", "文字 20", "本人・家族の識別に使用します。更新キーの一部です"),
    ("被保険者証番号", "必須", "数字 1〜20桁", "更新キーの一部です"),
    ("被保険者証枝番", "任意", "数字2桁", "本人と家族は「番号＋枝番」で区別します"),
    ("続柄名称", "必須", "本人／妻／夫／長男 …",
     "本人か家族かをこの値で判定します。<b>値の内容自体は検証しません</b>"
     "（家族側の記載方法が健保ごとに異なるためです）。前後の空白は削除して取り扱います"),
    ("対象者氏名（漢字）", "必須", "文字 1〜100", "—"),
    ("対象者氏名（カナ）", "必須", "文字 1〜100", "—"),
    ("性別", "必須", "男／女", "—"),
    ("生年月日", "必須", "YYYY-MM-DD", "／（スラッシュ）区切りにも対応し、自動的に正規化します"),
    ("資格取得日（家族認定日）", "必須", "YYYY-MM-DD", "家族の場合は認定日をご入力ください"),
    ("資格喪失日（家族削除日）", "任意", "YYYY-MM-DD", "家族の場合は削除日をご入力ください"),
    ("被保険者属性名", "任意", "一般／任意継続／特例退職", "—"),
    ("郵便番号", "任意", "数字7桁", "ハイフンなしでご入力ください"),
    ("住所", "任意", "文字 1〜200", "—"),
    ("住所（建物名）", "任意", "文字 1〜200", "住所とは別の列にご入力ください"),
    ("電話番号", "任意", "数字10〜11桁", "数字のみでご入力ください。ハイフンは使用できません"),
    ("メールアドレス", "任意", "文字 1〜100", "—"),
    ("社員コード", "任意", "文字 1〜40", "健診・保健指導のCSV出力で使用しています"),
    ("配付先コード", "任意", "文字 100",
     "健保が業務で使用している番号です。用途は健保ごとに異なるため、中立な現行の名称のままとしています"),
    ("connectID", "任意", "文字 1〜8", "外部連携に使用します。列名・桁数とも変更しません"),
    ("インフルエンザ予防接種対象", "必須", "対象／対象外", "—"),
    ("健診対象", "必須", "対象／対象外", "—"),
    ("企業ID", "任意", "数字", _BELONG),
    ("企業コード", "任意", "文字 100", _BELONG),
    ("事業所ID", "任意", "数字", _BELONG),
    ("事業所コード", "任意", "文字 100", _BELONG),
    ("部署ID", "任意", "数字", _BELONG),
    ("部署コード", "任意", "文字 100", _BELONG),
]

# 様式ごとの定義。detect_format はこの表を順に照合する
CSV_FORMATS = {
    "company": {"label": "企業登録", "columns": COMPANY_COLUMNS, "help": COMPANY_HELP,
                "required": ["企業名"], "unit": "企業1件", "parent": None,
                "back": "companies", "file": "company"},
    "office": {"label": "事業所登録", "columns": OFFICE_COLUMNS, "help": OFFICE_HELP,
               "required": ["事業所名"], "unit": "事業所1件", "parent": "company",
               "back": "offices", "file": "office"},
    "dept": {"label": "部署登録", "columns": DEPT_COLUMNS, "help": DEPT_HELP,
             "required": ["部署名"], "unit": "部署1件", "parent": "dept",
             "back": "departments", "file": "department"},
    "member": {"label": "加入者情報", "columns": MEMBER_COLUMNS, "help": MEMBER_HELP,
               "required": ["被保険者証番号", "対象者氏名（漢字）"], "unit": "加入者1名",
               "parent": None, "back": "members", "file": "member"},
    "bulk": {"label": "企業・事業所・部署一括取込", "columns": BULK_COLUMNS, "help": BULK_HELP,
             "required": ["企業名"], "unit": "企業＋事業所＋部署", "parent": None,
             "back": "companies", "file": "bulk"},
}
# 各マスタ画面から開く取込（入口①）で使う様式
HIER_KINDS = ("company", "office", "dept", "member")

# 企業そのものを作る様式は、企業の登録と同じ扱い（HIAスタッフ・健保担当者だけ）。
# 企業担当者は担当範囲の中しか触れないため、企業を増やす取込は行わせない。
COMPANY_KINDS = ("company", "bulk")


def can_import_kind(acc, kind):
    return kind not in COMPANY_KINDS or acc["role"] in ("system_admin", "kenpo_user")

# 旧様式の見出し → 新しい呼び名。2つ以上見つかったら旧様式として止める
# 旧様式の見出し → いまの扱い。新様式で無くなった／名前が変わった列だけを挙げる。
# 現行の列名をそのまま残したもの（企業名・郵便番号・続柄名称 など）はここに入れない。
# 2つ以上見つかったら旧様式として止め、何がどう変わったかを列挙する。
OLD_HEADERS = {
    "事業所（企業）コード": "企業ID",
    "所属コード": "部署ID（事業所を指すなら 事業所ID／事業所コード）",
    "保険者番号": "（廃止。取込先の健保は画面に出ます）",
    "加入者ID": "（廃止。HIAで採番します）",
    "個人ID": "（廃止）",
    "資格喪失予定日": "（廃止）",
    "削除予定日": "（廃止）",
}

# 1回に取り込める行数の上限。※要件定義書 6.5.2 で値が未決のため暫定
MAX_IMPORT_ROWS = int(os.environ.get("HIA_MAX_IMPORT_ROWS", "5000"))


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
    y, mo, d = (int(x) for x in m.groups())
    try:
        date(y, mo, d)
    except ValueError:
        return None
    return f"{y:04d}-{mo:02d}-{d:02d}"


def detect_format(header):
    """見出しの行から様式を判別する。

    戻り値は (kind, cand, missing)。
      kind    … 判別できた様式。できなければ None
      cand    … 判別できなかったときの「もっとも近い様式」
      missing … cand に足りない列
    様式の列をすべて含んでいれば一致とみなし、複数あれば列数の多いほうを採る
    （一括取込は企業登録の列をすべて含むため）。"""
    have = {h.strip() for h in header if h}
    hit, best, best_missing, best_score = [], None, [], -1.0
    for kind, spec in CSV_FORMATS.items():
        cols_ = spec["columns"]
        missing = [c for c in cols_ if c not in have]
        if not missing:
            hit.append(kind)
        score = (len(cols_) - len(missing)) / len(cols_)
        if score > best_score:
            best, best_missing, best_score = kind, missing, score
    if hit:
        return max(hit, key=lambda k: len(CSV_FORMATS[k]["columns"])), None, []
    return None, best, best_missing


def old_headers_in(header):
    """旧様式の見出しを拾う。[(旧, いまの呼び名), ...]"""
    have = {h.strip() for h in header if h}
    return [(o, n) for o, n in OLD_HEADERS.items() if o in have]


def read_upload(fs):
    """アップロードされたCSVを読む。戻り値は (見出し, 行, エラー文)"""
    if not fs or not fs.filename:
        return None, None, "ファイルを選択してください。"
    raw = fs.read()
    text = None
    for enc in ("utf-8-sig", "cp932", "utf-8"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        return None, None, ("ファイルの文字コードを判別できませんでした。"
                            "UTF-8 または Shift_JIS で保存してください。")
    rd = csv.DictReader(io.StringIO(text))
    header = [h.strip() for h in (rd.fieldnames or []) if h is not None]
    if not header:
        return None, None, "見出しの行がありません。"
    rows = list(rd)
    if not rows:
        return header, [], "データ行がありません（見出しだけのファイルです）。"
    if len(rows) > MAX_IMPORT_ROWS:
        return header, rows, (f"1回に取り込める行数は {MAX_IMPORT_ROWS:,} 行までです"
                              f"（このファイルは {len(rows):,} 行）。分けて取り込んでください。")
    return header, rows, None


# ---------------------------------------------------------------- 取込の下ごしらえ
def _g(row, key):
    """1つの値を取り出す。表計算ソフト向けに ="0123" の形で書かれていれば中身を返す
    （様式のダウンロードで、外部コードの先頭のゼロが落ちないようこの形で出しているため）"""
    v = (row.get(key) or "").strip()
    m = re.fullmatch(r'="(.*)"', v)
    return m.group(1).strip() if m else v


def _norm_code(v):
    """外部コードを照合の前にそろえる。
    前後の空白を落とし、大文字小文字は区別しない。先頭のゼロはそのまま残す。"""
    return (v or "").strip().upper()


def _excel_code(v):
    """外部コードを、表計算ソフトで開いても先頭のゼロが落ちない形にして出す"""
    return f'="{v}"' if v else ""


def _col_error(typ, v):
    """様式が定める型・桁に合っているかを見る。合っていれば None、違えば理由を返す。

    型の書き方は CSVフォーマット.xlsx の「型・桁」欄をそのまま使う。"""
    t = (typ or "").strip()
    if not t or t.startswith("YYYY"):
        return None                       # 日付は norm_date でそろえる
    m = re.fullmatch(r"数字\s*(\d+)桁", t)
    if m:
        return None if re.fullmatch(rf"\d{{{m.group(1)}}}", v) else \
            f"数字{m.group(1)}桁で入力してください（ハイフンなし）"
    m = re.fullmatch(r"数字\s*(\d+)〜(\d+)桁", t)
    if m:
        return None if re.fullmatch(rf"\d{{{m.group(1)},{m.group(2)}}}", v) else \
            f"数字{m.group(1)}〜{m.group(2)}桁で入力してください（ハイフンなし）"
    if t == "数字":
        return None if v.isdigit() else "数字で入力してください"
    m = re.fullmatch(r"文字\s*(?:(\d+)〜)?(\d+)", t)
    if m:
        return f"{m.group(2)}文字までです" if len(v) > int(m.group(2)) else None
    if "／" in t and not t.rstrip().endswith("…"):
        ok = [x.strip() for x in t.split("／")]
        return None if v in ok else "「" + "」「".join(ok) + "」のいずれかで入力してください"
    return None


def _check_columns(help_rows, row, skip=()):
    """様式の定義（列名・必須・型）に沿って1行ぶんを検証する。

    画面の「必要な列を確認する」と同じ表を使うので、出している説明と実際の検証がずれない。"""
    errs = []
    for name, req, typ, _ in help_rows:
        if name in skip:
            continue
        v = _g(row, name)
        if not v:
            if req == "必須":
                errs.append(f"{name}をご入力ください")
            continue
        m = _col_error(typ, v)
        if m:
            errs.append(f"{name}は{m}")
    return errs


def _blanked(who, cur, pairs):
    """既存の値が空になる項目を1つの注意文にまとめる。

    ファイルの空欄はそのまま上書きするため、画面から入れた値が月次の取込で
    消える事故を防げるよう、確定前に知らせる。pairs は [(項目名, DBの列, 新しい値), ...]"""
    if not cur:
        return []
    out = []
    for label, col, val in pairs:
        try:
            before = cur[col]
        except (IndexError, KeyError):
            before = None
        if (before or "") and not (val or ""):
            out.append(label)
    if not out:
        return []
    return [f"{who}：ファイルが空欄のため、登録済みの "
            f"{'・'.join(out)} が消えます"]


def import_kenpo(acc):
    """取込先の健保。運営スタッフは画面で選び、それ以外は自分の所属で決まる"""
    if acc["role"] == "system_admin":
        kid = request.values.get("kenpo_id", type=int)
        return kid or (get_db().execute(
            "SELECT id FROM kenpo ORDER BY code LIMIT 1").fetchone() or {"id": None})["id"]
    return acc["kenpo_id"]


def _scoped_company_map(acc, kenpo_id):
    """担当範囲の企業を ID・コード・(カナ,記号) で引けるようにする"""
    by_id, by_code, by_key, rows = {}, {}, {}, []
    for c in scoped_companies(acc):
        if kenpo_id and c["kenpo_id"] != kenpo_id:
            continue
        rows.append(c)
        by_id[str(c["id"])] = c
        if c["ext_code"]:
            by_code.setdefault(_norm_code(c["ext_code"]), []).append(c)
        by_key.setdefault((c["kana"] or "", c["cert_mark"] or ""), []).append(c)
    return by_id, by_code, by_key, rows


def _match(by_code, by_id, code, ident, out_of_scope=None):
    """① 外部コード → ② 内部ID の順に照合する。

    戻り値は (見つかった行, エラー文)。複数に一致したらエラーにする（勝手に更新しない）。
    両方書かれていて指す先が違う場合もエラーにする（上位の _resolve_ref と同じ規則）。
    out_of_scope は、IDは実在するが探す範囲の外だったときに出す文言。"""
    hit_c = hit_i = None
    if code:
        hits = by_code.get(_norm_code(code)) or []
        if len(hits) > 1:
            # コードが重複していても、IDがそのうちの1件を指していればそれに決める
            same = [h for h in hits if ident and str(h["id"]) == str(ident)]
            if not same:
                return None, (f"コード「{code}」に一致するものが {len(hits)} 件あります。"
                              + ("IDで特定してください" if not ident else "更新できません"))
            hits = same
        if hits:
            hit_c = hits[0]
    if ident:
        hit_i = by_id.get(str(ident))
        if not hit_i:
            return None, (out_of_scope or "").replace("{id}", str(ident)) \
                or f"ID「{ident}」は担当範囲の外です（または登録されていません）"
    if hit_c and hit_i and hit_c["id"] != hit_i["id"]:
        return None, (f"IDとコードが別のものを指しています"
                      f"（ID「{ident}」／コード「{code}」）")
    return (hit_c or hit_i), None


# ---------------------------------------------------------------- 様式ごとの検証
# 企業の項目 → 列名。企業登録は接頭辞なし、一括取込は「企業」を頭に付ける
COMPANY_COLS = {"name": "企業名", "kana": "企業名（フリガナ）", "cert_mark": "被保険者証記号",
                "owner": "代表者名", "zip": "郵便番号", "address": "住所",
                "tel": "電話番号", "email": "担当メールアドレス"}
BULK_COMPANY_COLS = {k: ("企業" + v if k not in ("name", "kana") else v)
                     for k, v in COMPANY_COLS.items()}


def _check_company(db, acc, kenpo_id, rows, help_rows=None, colmap=None, dup_check=True):
    """企業の行を検証する。一括取込からは企業の列だけの定義と列名の対応表を渡す
    （一括取込は同じ企業の行が並ぶのが普通なので、重複の検査はしない）"""
    cm = colmap or COMPANY_COLS
    by_id, by_code, by_key, _ = _scoped_company_map(acc, kenpo_id)
    out, seen = [], {}      # seen: 更新先の企業ID → 先に出てきた行番号
    for i, r in enumerate(rows):
        warn = []
        e = _check_columns(help_rows or COMPANY_HELP, r)
        name = _g(r, cm["name"])
        code, ident = _g(r, "企業コード"), _g(r, "企業ID")
        cur, err = _match(by_code, by_id, code, ident)
        if err:
            e.append("企業：" + err)
        if not cur and not code and not ident and name:
            hits = by_key.get((_g(r, cm["kana"]), _g(r, cm["cert_mark"]))) or []
            cur = hits[0] if len(hits) == 1 else None
        if cur and dup_check:
            _dup_row(e, "企業", cur["id"], seen, i + 2)
        vals = {k: _g(r, col) for k, col in cm.items()}
        warn += _blanked("企業", cur, [
            ("名称（フリガナ）", "kana", vals["kana"]),
            ("被保険者証記号", "cert_mark", vals["cert_mark"]),
            ("代表者名", "owner", vals["owner"]), ("郵便番号", "zip", vals["zip"]),
            ("住所", "address", vals["address"]), ("電話番号", "tel", vals["tel"]),
            ("担当メールアドレス", "email", vals["email"])])
        out.append({"errors": e, "warnings": warn, "mode": "更新" if cur else "新規",
                    "target_id": cur["id"] if cur else None, "ext_code": code or None,
                    "vals": vals, "place": "—"})
    return out


def _dup_row(errs, label, rid, seen, line):
    """同じものを2行以上で更新しようとしていたら止める（後の行が黙って勝つのを防ぐ）。
    seen は 更新先のID → 先に出てきた行番号"""
    if rid in seen:
        errs.append(f"{label}：{seen[rid]} 行目と同じ{label}（{label}ID「{rid}」）を"
                    f"指しています。ファイル内で重複しています")
    else:
        seen[rid] = line


def _resolve_ref(by_code, by_id, code, ident, label):
    """1つの階層を、コードとIDの2列から引き当てる。

    ・どちらか1つが埋まっていればよい
    ・両方書かれていて指す先が違う場合はエラー
    戻り値は (見つかった行, エラー文, 指定があったか)。"""
    if not code and not ident:
        return None, None, False
    hit_c = hit_i = None
    if code:
        hits = by_code.get(_norm_code(code)) or []
        if len(hits) > 1:
            # コードが重複していても、IDがそのうちの1件を指していればそれに決める
            same = [h for h in hits if ident and str(h["id"]) == str(ident)]
            if not same:
                return None, (f"{label}コード「{code}」に一致するものが {len(hits)} 件あります"
                              + ("。IDで特定してください" if not ident else "")), True
            hits = same
        if not hits:
            return None, (f"{label}コード「{code}」は担当範囲の外です"
                          f"（または登録されていません）"), True
        hit_c = hits[0]
    if ident:
        hit_i = by_id.get(str(ident))
        if not hit_i:
            return None, (f"{label}ID「{ident}」は担当範囲の外です"
                          f"（または登録されていません）"), True
    if hit_c and hit_i and hit_c["id"] != hit_i["id"]:
        return None, (f"{label}IDと{label}コードが別の{label}を指しています"
                      f"（ID「{ident}」／コード「{code}」）"), True
    return (hit_c or hit_i), None, True


def _parents_for(acc, kenpo_id):
    """担当範囲の企業・事業所を、IDとコードで引ける形にまとめる"""
    comps = [c for c in scoped_companies(acc) if c["kenpo_id"] == kenpo_id]
    c_id = {str(c["id"]): c for c in comps}
    c_code = {}
    for c in comps:
        if c["ext_code"]:
            c_code.setdefault(_norm_code(c["ext_code"]), []).append(c)
    offs = [o for o in scoped_offices(acc) if str(o["company_id"]) in c_id]
    o_id = {str(o["id"]): o for o in offs}
    o_code = {}
    for o in offs:
        if o["ext_code"]:
            o_code.setdefault(_norm_code(o["ext_code"]), []).append(o)
    return comps, c_id, c_code, offs, o_id, o_code


def resolve_parents(acc, kenpo_id, kind, rows, picks):
    """事業所登録・部署登録の上位を、行ごとに決める。

    上位はまずCSVの列（企業ID／企業コード、事業所ID／事業所コード）で指定する。
    列が空欄の行だけ、確認画面のプルダウンで**行ごとに**選ぶ（同じ企業の行でも
    行ごとに違う事業所を選べる）。
    空欄の事業所を「事業所なし」と決めつけない（入れ忘れが黙って事業所なしで入るのを防ぐ）。

    picks は画面で選ばれた内容 {"c:<行>": 企業ID, "u:<行>": "company"|"o<事業所ID>"}。
    戻り値は (行ごとの上位, 選ばせる行の一覧（1行＝企業と事業所の選択）, すべて選べているか)。"""
    comps, c_id, c_code, offs, o_id, o_code = _parents_for(acc, kenpo_id)
    need_office = (kind == "dept")
    c_options = [{"v": str(c["id"]), "t": f"{c['ext_code'] or c['id']}　{c['name']}"}
                 for c in comps]
    out, pend = [], []

    for i, r in enumerate(rows):
        line = i + 2
        errs = []
        comp, cerr, c_given = _resolve_ref(c_code, c_id, _g(r, "企業コード"),
                                           _g(r, "企業ID"), "企業")
        if cerr:
            errs.append(cerr)
        off, o_given = None, False
        if need_office:
            # 事業所コードは企業ごとに付ける番号。企業が分かっていれば、その企業の事業所だけで引く
            oc, oi = o_code, o_id
            if comp:
                oi = {k: v for k, v in o_id.items() if v["company_id"] == comp["id"]}
                oc = {k: [x for x in v if x["company_id"] == comp["id"]]
                      for k, v in o_code.items()}
            off, oerr, o_given = _resolve_ref(oc, oi, _g(r, "事業所コード"),
                                              _g(r, "事業所ID"), "事業所")
            if oerr and comp:
                # 企業で絞ったから見つからなかっただけなら、そう伝える
                wide, _, _ = _resolve_ref(o_code, o_id, _g(r, "事業所コード"),
                                          _g(r, "事業所ID"), "事業所")
                if wide:
                    oerr = "指定された事業所は、指定された企業の事業所ではありません"
            if oerr:
                errs.append(oerr)
            if off:
                if comp and off["company_id"] != comp["id"]:
                    errs.append("指定された事業所は、指定された企業の事業所ではありません")
                elif not comp:
                    comp = c_id.get(str(off["company_id"]))   # 下位から上位を補う
        # 上位が列で決まらない行は、その行で選ばせる。1行＝企業と事業所の2つのプルダウン
        # （企業が列で決まっていれば企業は名前だけ出す。事業所登録の様式は企業だけ）
        need_c = not comp and not c_given and not o_given and not errs
        need_u = need_office and not o_given and not errs
        under_ok = True
        if need_c or need_u:
            row_pick = {"line": line, "key": f"c:{line}" if need_c else f"u:{line}",
                        "c": None, "u": None, "done": True}
            if need_c:
                sel = (picks.get(f"c:{line}") or "").strip()
                comp = c_id.get(sel)
                row_pick["c"] = {"given": False, "value": sel if comp else "",
                                 "options": c_options}
                if not comp:
                    row_pick["done"] = False
            else:
                row_pick["c"] = {"given": True, "name": comp["name"] if comp else "—"}
            if need_u:
                sel = (picks.get(f"u:{line}") or "").strip()
                if comp:
                    if sel == "company":
                        off = None
                    elif sel.startswith("o") and sel[1:] in o_id \
                            and o_id[sel[1:]]["company_id"] == comp["id"]:
                        off = o_id[sel[1:]]
                    else:
                        sel, under_ok = "", False
                    mine = [o for o in offs if o["company_id"] == comp["id"]]
                else:
                    sel, under_ok, mine = "", False, []
                row_pick["u"] = {"value": sel, "enabled": comp is not None,
                                 "company": str(comp["id"]) if comp else "",
                                 "options": ([{"v": "company", "t": "（事業所なし）"}]
                                             + [{"v": f"o{o['id']}",
                                                 "t": f"{o['ext_code'] or o['id']}　{o['name']}"}
                                                for o in mine])}
                if not under_ok:
                    row_pick["done"] = False
            pend.append(row_pick)
        out.append({"company": comp, "office": off, "errors": errs,
                    "pending": not comp and not errs,     # 企業はこれから画面で選ぶ
                    "ready": bool(comp) and under_ok and not errs})
    return out, pend, all(p["done"] for p in pend)


def _check_office(db, acc, kenpo_id, rows, parents):
    """事業所の行を検証する。parents は行ごとの上位（resolve_parents の結果）"""
    cache = {}

    def idx(cid):
        """企業ごとに、既存の事業所を ID・コード・名称で引ける形にする"""
        if cid not in cache:
            by_id, by_code, by_name = {}, {}, {}
            for o in db.execute("SELECT * FROM office WHERE company_id=?", (cid,)):
                by_id[str(o["id"])] = o
                if o["ext_code"]:
                    by_code.setdefault(_norm_code(o["ext_code"]), []).append(o)
                by_name.setdefault(o["name"], []).append(o)
            cache[cid] = (by_id, by_code, by_name)
        return cache[cid]

    out, seen = [], {}      # seen: 更新先の事業所ID → 先に出てきた行番号
    for i, (r, p) in enumerate(zip(rows, parents)):
        warn = []
        e = _check_columns(OFFICE_HELP, r) + list(p["errors"])
        comp = p["company"]
        if not comp and not p["errors"] and not p.get("pending"):
            e.append("上位の企業が決まっていません")
        name = _g(r, "事業所名")
        code, ident = _g(r, "事業所コード"), _g(r, "事業所ID")
        cur = None
        if comp:
            by_id, by_code, by_name = idx(comp["id"])
            cur, err = _match(by_code, by_id, code, ident, out_of_scope=(
                "事業所ID「{id}」の事業所は " + comp["name"] + " の事業所ではありません"))
            if err:
                e.append("事業所：" + err)
            if not cur and not code and not ident and name:
                hits = by_name.get(name) or []
                cur = hits[0] if len(hits) == 1 else None
        if cur:
            _dup_row(e, "事業所", cur["id"], seen, i + 2)
        vals = {"name": name, "kana": _g(r, "事業所名（フリガナ）"),
                "cert_mark": _g(r, "被保険者証記号"), "owner": _g(r, "代表者名"),
                "zip": _g(r, "郵便番号"), "address": _g(r, "住所"),
                "tel": _g(r, "電話番号"), "email": _g(r, "担当メールアドレス")}
        warn += _blanked("事業所", cur, [
            ("名称（フリガナ）", "kana", vals["kana"]),
            ("被保険者証記号", "cert_mark", vals["cert_mark"]),
            ("代表者名", "owner", vals["owner"]), ("郵便番号", "zip", vals["zip"]),
            ("住所", "address", vals["address"]), ("電話番号", "tel", vals["tel"]),
            ("担当メールアドレス", "email", vals["email"])])
        out.append({"errors": e, "warnings": warn, "mode": "更新" if cur else "新規",
                    "target_id": cur["id"] if cur else None, "ext_code": code or None,
                    "vals": vals, "company_id": comp["id"] if comp else None,
                    "place": comp["name"] if comp else "—",
                    "place_c": comp["name"] if comp else None})
    return out


def _check_dept(db, acc, kenpo_id, rows, parents):
    """部署の行を検証する。parents は行ごとの上位（企業と、事業所またはNone）"""
    cache = {}

    def idx(cid, oid):
        key = (cid, oid or 0)
        if key not in cache:
            by_id, by_code, by_name = {}, {}, {}
            for d in db.execute(
                    "SELECT * FROM department WHERE company_id=? AND IFNULL(office_id,0)=?",
                    (cid, oid or 0)):
                by_id[str(d["id"])] = d
                if d["ext_code"]:
                    by_code.setdefault(_norm_code(d["ext_code"]), []).append(d)
                by_name.setdefault(d["name"], []).append(d)
            cache[key] = (by_id, by_code, by_name)
        return cache[key]

    allowed = {str(c["id"]) for c in scoped_companies(acc)}
    out, seen = [], {}      # seen: 更新先の部署ID → 先に出てきた行番号
    for i, (r, p) in enumerate(zip(rows, parents)):
        warn = []
        e = _check_columns(DEPT_HELP, r) + list(p["errors"])
        comp, off = p["company"], p["office"]
        if not comp and not p["errors"] and not p.get("pending"):
            e.append("上位の企業が決まっていません")
        name = _g(r, "部署名")
        code, ident = _g(r, "部署コード"), _g(r, "部署ID")
        place = _dept_place(comp["id"], off["id"] if off else None) if comp else "—"
        cur = None
        if comp and p["ready"]:
            by_id, by_code, by_name = idx(comp["id"], off["id"] if off else None)
            cur, err = _match(by_code, by_id, code, ident, out_of_scope=(
                "部署ID「{id}」の部署は " + place + " の部署ではありません"))
            if err:
                # IDが担当範囲内の別の場所にあるなら、本来の所属を添えて迷わないようにする
                if ident and not by_id.get(str(ident)):
                    real = db.execute(
                        "SELECT company_id, office_id FROM department WHERE id=?",
                        (ident,)).fetchone()
                    if real and str(real["company_id"]) in allowed:
                        err += ("（この部署は "
                                + _dept_place(real["company_id"], real["office_id"])
                                + " の配下です）")
                e.append("部署：" + err)
            if not cur and not code and not ident and name:
                hits = by_name.get(name) or []
                if len(hits) == 1:
                    cur = hits[0]
                elif len(hits) > 1:
                    warn.append(f"部署：同じ名前の部署が {len(hits)} 件あるため、"
                                f"新しく登録します")
        if cur:
            _dup_row(e, "部署", cur["id"], seen, i + 2)
        vals = {"name": name, "kana": _g(r, "部署名（フリガナ）")}
        warn += _blanked("部署", cur, [("名称（フリガナ）", "kana", vals["kana"])])
        out.append({"errors": e, "warnings": warn, "mode": "更新" if cur else "新規",
                    "target_id": cur["id"] if cur else None, "ext_code": code or None,
                    "vals": vals, "company_id": comp["id"] if comp else None,
                    "office_id": off["id"] if off else None,
                    "place": place if p["ready"] else ("—" if e else "（これから選びます）"),
                    "place_c": comp["name"] if comp else None,
                    "place_o": ((off["name"] if off else "（事業所なし）") if p["ready"] else None)})
    return out


def _check_bulk(db, acc, kenpo_id, rows):
    """一括取込。1行に企業＋事業所＋部署が入る。上位は行の中の階層で決まる"""
    # 企業の列は企業登録と同じ定義で見る。事業所・部署の列は、書かれている行だけ見る
    comp = _check_company(db, acc, kenpo_id, rows, help_rows=BULK_HELP[:len(COMPANY_HELP)],
                          colmap=BULK_COMPANY_COLS, dup_check=False)
    o_help = BULK_HELP[len(COMPANY_HELP):len(COMPANY_HELP) + len(OFFICE_HELP)]
    d_help = BULK_HELP[len(COMPANY_HELP) + len(OFFICE_HELP):]
    by_id, by_code, _, _ = _scoped_company_map(acc, kenpo_id)
    allowed = {c["id"] for c in scoped_companies(acc)}
    ocache, dcache = {}, {}

    def oidx(cid):
        """企業ごとの既存の事業所（ID・コード・名称）"""
        if cid not in ocache:
            by_i, by_c, by_n = {}, {}, {}
            for o in db.execute("SELECT * FROM office WHERE company_id=?", (cid,)):
                by_i[str(o["id"])] = o
                if o["ext_code"]:
                    by_c.setdefault(_norm_code(o["ext_code"]), []).append(o)
                by_n.setdefault(o["name"], []).append(o)
            ocache[cid] = (by_i, by_c, by_n)
        return ocache[cid]

    def didx(cid, oid):
        """企業＋事業所ごとの既存の部署（ID・コード・名称）"""
        key = (cid, oid or 0)
        if key not in dcache:
            by_i, by_c, by_n = {}, {}, {}
            for d in db.execute(
                    "SELECT * FROM department WHERE company_id=? AND IFNULL(office_id,0)=?",
                    (cid, oid or 0)):
                by_i[str(d["id"])] = d
                if d["ext_code"]:
                    by_c.setdefault(_norm_code(d["ext_code"]), []).append(d)
                by_n.setdefault(d["name"], []).append(d)
            dcache[key] = (by_i, by_c, by_n)
        return dcache[key]

    def find(label, maps, code, ident, name, place):
        """階層別の様式と同じ規則で1件に決める。戻り値は (既存の行, エラー文)"""
        by_i, by_c, by_n = maps
        cur, err = _match(by_c, by_i, code, ident, out_of_scope=(
            label + "ID「{id}」の" + label + "は " + place + " の" + label + "ではありません"))
        if err:
            return None, label + "：" + err
        if not cur and not code and not ident:
            hits = by_n.get(name) or []
            cur = hits[0] if len(hits) == 1 else None
        return cur, None

    out, seen_company = [], {}
    for r, cr in zip(rows, comp):
        e = list(cr["errors"])
        warn = list(cr["warnings"])
        oname, dname = _g(r, "事業所名"), _g(r, "部署名")
        cur_c = cr["target_id"]
        if cur_c and cur_c not in allowed:
            e.append("この企業は担当範囲の外です")
        # 事業所・部署も、階層別の様式と同じ規則で既存を引く（IDとコードの食い違いはエラー）。
        # 企業が新規なら、その下はすべて新規
        cname = cr["vals"]["name"]
        cur_o = cur_d = None
        o_code, o_id = _g(r, "事業所コード"), _g(r, "事業所ID")
        d_code, d_id = _g(r, "部署コード"), _g(r, "部署ID")
        if oname and cur_c:
            cur_o, err = find("事業所", oidx(cur_c), o_code, o_id, oname, cname)
            if err:
                e.append(err)
        if dname and cur_c and (cur_o or not oname):
            cur_d, err = find("部署", didx(cur_c, cur_o["id"] if cur_o else None),
                              d_code, d_id, dname,
                              cname + ("／" + oname if oname else "（事業所なし）"))
            if err:
                e.append(err)
        levels = ["企業：" + cr["mode"]]
        if oname:
            levels.append("事業所：" + ("更新" if cur_o else "新規"))
        if dname:
            levels.append("部署：" + ("更新" if cur_d else "新規"))
        # 行の判定は、いちばん下の階層（その行が主に登録するもの）で決める
        mode = ("更新" if cur_d else "新規") if dname else (
            ("更新" if cur_o else "新規") if oname else cr["mode"])
        # ファイル内で同じ企業の行どうしが食い違っていないか
        ckey = cr["ext_code"] or _g(r, "企業ID") or cr["vals"]["name"]
        sig = tuple(sorted(cr["vals"].items()))
        if ckey in seen_company and seen_company[ckey] != sig:
            e.append("同じ企業の行が、ファイル内で違う内容になっています")
        seen_company.setdefault(ckey, sig)
        if oname:
            e += _check_columns(o_help, r)
        if dname:
            e += _check_columns(d_help, r)
        if not oname and dname:
            warn.append("事業所が空欄のため、部署は事業所なしで登録します")
        place = cr["vals"]["name"] + ("／" + oname if oname else "（事業所なし）")
        out.append({
            "errors": e, "warnings": warn, "mode": mode, "levels": "／".join(levels),
            "target_id": cur_c, "ext_code": cr["ext_code"], "vals": cr["vals"],
            "place": place if (oname or dname) else cr["vals"]["name"],
            "office": ({"name": oname, "code": o_code or None, "ident": o_id,
                        "target_id": cur_o["id"] if cur_o else None,
                        "vals": {"name": oname, "kana": _g(r, "事業所名（フリガナ）"),
                                 "cert_mark": _g(r, "事業所被保険者証記号"),
                                 "owner": _g(r, "事業所代表者名"),
                                 "zip": _g(r, "事業所郵便番号"),
                                 "address": _g(r, "事業所住所"),
                                 "tel": _g(r, "事業所電話番号"),
                                 "email": _g(r, "事業所担当メールアドレス")}}
                       if oname else None),
            "dept": ({"name": dname, "code": d_code or None, "ident": d_id,
                      "target_id": cur_d["id"] if cur_d else None,
                      "vals": {"name": dname, "kana": _g(r, "部署名（フリガナ）")}}
                     if dname else None)})
    return out


def _check_member(db, acc, kenpo_id, rows):
    """加入者の行を検証する。所属先はすべて任意。下位だけの指定でも上位を補う"""
    by_id, by_code, _, _ = _scoped_company_map(acc, kenpo_id)
    offs_id, offs_code = {}, {}
    for o in db.execute("SELECT o.* FROM office o JOIN company c ON c.id=o.company_id"
                        " WHERE c.kenpo_id=?", (kenpo_id,)):
        offs_id[str(o["id"])] = o
        if o["ext_code"]:
            offs_code.setdefault(_norm_code(o["ext_code"]), []).append(o)
    deps_id, deps_code = {}, {}
    for d in db.execute("SELECT d.* FROM department d JOIN company c ON c.id=d.company_id"
                        " WHERE c.kenpo_id=?", (kenpo_id,)):
        deps_id[str(d["id"])] = d
        if d["ext_code"]:
            deps_code.setdefault(_norm_code(d["ext_code"]), []).append(d)
    out, seen = [], set()
    for r in rows:
        warn = []
        e = _check_columns(MEMBER_HELP, r)
        no, name = _g(r, "被保険者証番号"), _g(r, "対象者氏名（漢字）")
        mark, branch = _g(r, "被保険者証記号"), _g(r, "被保険者証枝番")
        birth = norm_date(_g(r, "生年月日"))
        qual = norm_date(_g(r, "資格取得日（家族認定日）"))
        lost = norm_date(_g(r, "資格喪失日（家族削除日）"))
        for v, label in ((birth, "生年月日"), (qual, "資格取得日（家族認定日）"),
                         (lost, "資格喪失日（家族削除日）")):
            if v is None:
                e.append(f"{label}は 年-月-日 の形式で入力してください")
        flu = 1 if _g(r, "インフルエンザ予防接種対象") != "対象外" else 0
        kenshin = 1 if _g(r, "健診対象") != "対象外" else 0

        # 所属先。まず企業を決め、決まっていればその企業の事業所・部署に限って探す。
        # 外部コードは企業ごとに付けるため、健保の全体から探すと別の企業の同じコードに
        # ぶつかる。上位が分かっているときは、そこへ絞り込んでから照合する。
        comp, err, _ = _resolve_ref(by_code, by_id, _g(r, "企業コード"), _g(r, "企業ID"), "企業")
        if err:
            e.append(err)
        o_code, o_id = offs_code, offs_id
        d_code, d_id = deps_code, deps_id
        if comp:
            o_id = {k: v for k, v in offs_id.items() if v["company_id"] == comp["id"]}
            o_code = {k: [x for x in v if x["company_id"] == comp["id"]]
                      for k, v in offs_code.items()}
            d_id = {k: v for k, v in deps_id.items() if v["company_id"] == comp["id"]}
            d_code = {k: [x for x in v if x["company_id"] == comp["id"]]
                      for k, v in deps_code.items()}
        off, err, _ = _resolve_ref(o_code, o_id, _g(r, "事業所コード"), _g(r, "事業所ID"),
                                   "事業所")
        if err:
            e.append(err)
        if off:
            # 事業所まで決まったら、部署はその事業所のものに限る
            d_id = {k: v for k, v in d_id.items() if v["office_id"] == off["id"]}
            d_code = {k: [x for x in v if x["office_id"] == off["id"]]
                      for k, v in d_code.items()}
        dept, err, _ = _resolve_ref(d_code, d_id, _g(r, "部署コード"), _g(r, "部署ID"), "部署")
        if err:
            e.append(err)
        if dept:
            if off and dept["office_id"] and dept["office_id"] != off["id"]:
                e.append("指定された部署は、指定された事業所の部署ではありません")
            if comp and dept["company_id"] != comp["id"]:
                e.append("指定された部署は、指定された企業の部署ではありません")
            off = off or (offs_id.get(str(dept["office_id"])) if dept["office_id"] else None)
            comp = comp or by_id.get(str(dept["company_id"]))
        if off:
            if comp and off["company_id"] != comp["id"]:
                e.append("指定された事業所は、指定された企業の事業所ではありません")
            comp = comp or by_id.get(str(off["company_id"]))
        if not comp and not off and not dept:
            warn.append("所属先の指定がないため、未紐づけで登録します")

        cur = cur_id(db, kenpo_id, mark, no, branch)
        key = (mark, no, branch)
        if key in seen:
            e.append("同じ被保険者証記号・番号・枝番の行がファイル内で重複しています")
        seen.add(key)
        vals = {"cert_mark": mark, "cert_branch": branch, "relation": _g(r, "続柄名称"),
                "kana": _g(r, "対象者氏名（カナ）"), "sex": _g(r, "性別"),
                "birth": birth or "", "qualified_at": qual or "", "lost_at": lost or "",
                "attr": _g(r, "被保険者属性名"), "zip": _g(r, "郵便番号"),
                # CSVの「住所」は1列。DBは都道府県・市区町村・番地に分けて持つ（8-53）ため、
                # ここで分割する。分けられない住所は都道府県・市区町村を空にして丸ごと残す。
                **dict(zip(("pref", "city", "address"), split_address(_g(r, "住所")))),
                "address2": _g(r, "住所（建物名）"),
                "tel": _g(r, "電話番号"), "email": _g(r, "メールアドレス"),
                "employee_code": _g(r, "社員コード"),
                "billing_code": _g(r, "配付先コード"),
                "connect_id": _g(r, "connectID"),
                "influenza": flu, "excluded": 0 if kenshin else 1}
        cur_row = db.execute("SELECT * FROM member WHERE id=?",
                             (cur["id"],)).fetchone() if cur else None
        warn += _blanked("加入者", cur_row, [
            ("被保険者属性", "attr", vals["attr"]), ("郵便番号", "zip", vals["zip"]),
            ("住所", "address", vals["address"]), ("建物名", "address2", vals["address2"]),
            ("電話番号", "tel", vals["tel"]), ("メールアドレス", "email", vals["email"]),
            ("社員コード", "employee_code", vals["employee_code"]),
            ("配付先コード", "billing_code", vals["billing_code"]),
            ("資格喪失日", "lost_at", vals["lost_at"])])
        place = "／".join(x for x in (comp["name"] if comp else None,
                                     off["name"] if off else None,
                                     dept["name"] if dept else None) if x) or "未紐づけ"
        out.append({"errors": e, "warnings": warn, "mode": "更新" if cur else "新規",
                    "target_id": cur["id"] if cur else None,
                    "member_no": no, "name": name, "vals": vals, "place": place,
                    "company_id": comp["id"] if comp else None,
                    "office_id": off["id"] if off else None,
                    "dept_id": dept["id"] if dept else None})
    return out


# ---------------------------------------------------------------- 検証のまとめ
def verify_rows(kind, acc, kenpo_id, rows, picks=None):
    """様式に応じて行を検証し、明細・件数の要約・上位の選択欄を返す。

    picks は確認画面で行ごとに選んだ上位 {"c:<行>": 企業ID, "u:<行>": ...}。
    事業所登録・部署登録では、列で上位が決まらない行があるときだけ選択欄（groups＝行の一覧）が出る。"""
    db = get_db()
    groups, ready = [], True
    if kind == "company":
        det = _check_company(db, acc, kenpo_id, rows)
    elif kind == "office":
        parents, groups, ready = resolve_parents(acc, kenpo_id, kind, rows, picks or {})
        det = _check_office(db, acc, kenpo_id, rows, parents)
    elif kind == "dept":
        parents, groups, ready = resolve_parents(acc, kenpo_id, kind, rows, picks or {})
        det = _check_dept(db, acc, kenpo_id, rows, parents)
    elif kind == "bulk":
        det = _check_bulk(db, acc, kenpo_id, rows)
    else:
        det = _check_member(db, acc, kenpo_id, rows)
    cols = CSV_FORMATS[kind]["columns"]
    out = []
    for i, (raw, d) in enumerate(zip(rows, det), start=2):
        judge = ("エラー" if d["errors"] else ("注意" if d["warnings"] else d["mode"]))
        out.append(dict(d, line=i, judge=judge, cells=[_g(raw, c) for c in cols]))
    summary = {k: 0 for k in ("新規", "更新", "注意", "エラー")}
    for r in out:
        summary[r["judge"]] += 1
    return out, summary, groups, ready


def _stage(kind, rows, filename, kenpo_id):
    """確認した内容を一時的に置く。確定できるのはアップロードした本人だけ・1回だけ"""
    tok = uuid.uuid4().hex
    STAGING[tok] = {"kind": kind, "rows": rows, "email": current_account()["email"],
                    "filename": filename, "kenpo_id": kenpo_id,
                    "at": datetime.now()}
    return tok


def _staged(token, kind=None):
    stg = STAGING.get(token or "")
    if not stg or stg["email"] != current_account()["email"]:
        return None
    if kind and stg["kind"] != kind:
        return None
    return stg


# ---------------------------------------------------------------- S1 アップロード
def _import_ctx(acc, kind):
    db = get_db()
    kenpo_id = import_kenpo(acc)
    kenpo = db.execute("SELECT * FROM kenpo WHERE id=?", (kenpo_id,)).fetchone()
    return {"kind": kind, "spec": CSV_FORMATS[kind],
            "kenpo": kenpo, "kenpo_id": kenpo_id,
            "kenpos": (db.execute("SELECT * FROM kenpo ORDER BY code").fetchall()
                       if acc["role"] == "system_admin" else []),
            "max_rows": MAX_IMPORT_ROWS}


@app.route("/import")
@login_required
def import_upload():
    """入口①：各マスタ画面からの取込。様式は見出しから自動で判別する"""
    acc = current_account()
    kind = request.args.get("kind") or "company"
    if kind not in HIER_KINDS:
        kind = "company"
    if not can_import_kind(acc, kind):
        log("import", "企業の取込をブロック", "blocked", target=kind,
            detail=f"role={role_key(acc)}")
        flash("企業の取込は健康保険組合のご担当者が行います。"
              "事業所・部署・加入者の取込はお使いいただけます。", "error")
        return redirect(url_for("orgs"))
    return render_template("import_upload.html", bulk=False, **_import_ctx(acc, kind))


@app.route("/import/bulk")
@roles_required("system_admin", "kenpo_user")
def import_upload_bulk():
    """入口②：企業・事業所・部署一括取込（初期投入・全体の入れ替え用）"""
    return render_template("import_upload.html", bulk=True,
                           **_import_ctx(current_account(), "bulk"))


@app.route("/import/upload", methods=["POST"])
@login_required
def import_receive():
    """ファイルを受け取り、様式を判別して確認画面へ進む"""
    acc = current_account()
    bulk = request.form.get("bulk") == "1"
    back = url_for("import_upload_bulk") if bulk else url_for(
        "import_upload", kind=request.form.get("kind") or "company")
    fs = request.files.get("file")
    header, rows, err = read_upload(fs)
    fname = fs.filename if fs else "—"
    if err:
        log("import", "取込ファイルの読込失敗", "failure", target=fname, detail=err)
        flash(err, "error")
        return redirect(back)

    olds = old_headers_in(header)
    kind, cand, missing = detect_format(header)
    if kind is None and olds:
        detail = "／".join(f"旧「{o}」→ {n}" for o, n in olds[:8])
        log("import", "旧様式のファイル", "failure", target=fname, detail=detail)
        flash("旧様式のファイルです。新しい様式をダウンロードしてください。"
              "変わった呼び名：" + "、".join(f"「{o}」→「{n}」" for o, n in olds[:8]), "error")
        return redirect(back)
    if kind is None:
        log("import", "様式を判別できない", "failure", target=fname,
            detail=f"近い様式={CSV_FORMATS[cand]['label']}／不足={','.join(missing)}")
        flash(f"どの様式にも一致しません。もっとも近いのは"
              f"「{CSV_FORMATS[cand]['label']}フォーマット」です"
              f"（不足：{'、'.join(missing)}）。", "error")
        return redirect(back)
    if bulk and kind != "bulk":
        flash(f"この画面は企業・事業所・部署一括取込の様式だけを受け付けます"
              f"（読み取った様式：{CSV_FORMATS[kind]['label']}）。"
              f"階層ごとの取込は各マスタ画面から行ってください。", "error")
        return redirect(back)
    if not can_import_kind(acc, kind):
        log("import", "企業の取込をブロック", "blocked", target=fname,
            detail="様式=" + CSV_FORMATS[kind]["label"] + "／role=" + role_key(acc))
        flash("企業を作る様式のため、取り込めません。"
              "企業の登録は健康保険組合のご担当者が行います。", "error")
        return redirect(back)
    if not bulk and kind == "bulk":
        flash("企業・事業所・部署一括取込の様式です。「企業・事業所・部署一括取込」の画面から取り込んでください。",
              "error")
        return redirect(back)

    kenpo_id = import_kenpo(acc)
    tok = _stage(kind, rows, fname, kenpo_id)
    log("import", "取込ファイルを受領", "success", target=fname,
        detail=f"様式={CSV_FORMATS[kind]['label']}／{len(rows)}行")
    return redirect(url_for("import_preview", token=tok))


def _import_picks():
    """確認画面のプルダウンで行ごとに選んだ上位を読み取る。
    pick_c_<行> … その行の企業ID
    pick_u_<行> … その行の事業所（company＝事業所なし／o<事業所ID>）"""
    picks = {}
    for k, v in request.values.items():
        if k.startswith("pick_c_") and k[7:].isdigit():
            picks["c:" + k[7:]] = (v or "").strip()
        elif k.startswith("pick_u_") and k[7:].isdigit():
            picks["u:" + k[7:]] = (v or "").strip()
    return picks


def _pick_param(key):
    """picks のキー（c:<行>／u:<行>）を、画面のパラメータ名に戻す"""
    return ("pick_c_" if key.startswith("c:") else "pick_u_") + key.split(":")[1]


# ---------------------------------------------------------------- S2 確認
@app.route("/import/preview", methods=["GET", "POST"])
@login_required
def import_preview():
    """確認（プレビュー）。事業所・部署は、ここで上位（親）を選ぶ"""
    db, acc = get_db(), current_account()
    token = request.values.get("token")
    stg = _staged(token)
    if not stg:
        flash("確認した内容の有効期限が切れています。もう一度アップロードしてください。",
              "error")
        return redirect(url_for("import_upload"))
    kind, spec = stg["kind"], CSV_FORMATS[stg["kind"]]
    kenpo_id = stg["kenpo_id"]
    picks = _import_picks()
    rows, summary, groups, ready = verify_rows(kind, acc, kenpo_id, stg["rows"], picks)
    can_commit = ready and rows and not summary["エラー"]
    kenpo = db.execute("SELECT * FROM kenpo WHERE id=?", (kenpo_id,)).fetchone()
    # 見出しに出す「どこへ入るか」。行ごとに違うので、1つに決まるときだけ名前を出す
    places = {r["place"] for r in rows if r.get("place") and r["place"] != "—"}
    place = places.pop() if len(places) == 1 else None
    return render_template(
        "import_preview.html", kind=kind, spec=spec, token=token, rows=rows,
        summary=summary, kenpo=kenpo, groups=groups, picks=picks, ready=ready,
        can_commit=can_commit, place=place, n_places=len(places) + (1 if place else 0),
        filename=stg["filename"], columns=spec["columns"], total=len(stg["rows"]))


# ---------------------------------------------------------------- S3 確定・結果
@app.route("/import/commit", methods=["POST"])
@login_required
def import_commit():
    db, acc = get_db(), current_account()
    token = request.form.get("token")
    stg = _staged(token)
    if not stg:
        flash("確認した内容の有効期限が切れています。もう一度アップロードしてください。",
              "error")
        return redirect(url_for("import_upload"))
    kind, spec = stg["kind"], CSV_FORMATS[stg["kind"]]
    if not can_import_kind(acc, kind):
        log("import", "企業の取込の確定をブロック", "blocked", target=stg["filename"],
            detail="role=" + role_key(acc))
        flash("企業を作る様式のため、確定できません。", "error")
        return redirect(url_for("orgs"))
    kenpo_id = stg["kenpo_id"]
    # 上位の選択も検証も、確認画面と同じ関数でもう一度通す（確定だけ緩くならないように）
    picks = _import_picks()
    rows, summary, groups, ready = verify_rows(kind, acc, kenpo_id, stg["rows"], picks)
    back = url_for("import_preview", token=token,
                   **{_pick_param(k): v for k, v in picks.items()})
    if not ready:
        log("import", "取込の確定をブロック", "blocked", target=stg["filename"],
            detail="上位（親）が未選択")
        flash("取り込む先を選んでください。担当範囲にあるものだけを選べます。", "error")
        return redirect(back)
    if summary["エラー"] or not rows:
        flash("エラーのある行が残っています。確定できません。", "error")
        return redirect(back)
    STAGING.pop(token, None)      # 確定は1回だけ
    counts = _apply_rows(db, kind, kenpo_id, rows)
    db.commit()
    log("import", f"{spec['label']}を取込", "success", target=stg["filename"],
        detail="／".join(f"{k} 新規{v['ins']}・更新{v['upd']}" for k, v in counts.items()))
    return render_template("import_result.html", spec=spec, kind=kind, counts=counts,
                           filename=stg["filename"], total=len(rows),
                           back=spec["back"], warned=summary["注意"])


def _upsert(db, table, target_id, vals, extra_new=None):
    """既存があれば更新、なければ登録する。戻り値は (id, 新規かどうか)"""
    keys = list(vals.keys())
    if target_id:
        sets = ", ".join(f"{k}=?" for k in keys)
        db.execute(f"UPDATE {table} SET {sets}, updated_at=? WHERE id=?",
                   [vals[k] for k in keys] + [now(), target_id])
        return target_id, False
    allv = dict(vals, **(extra_new or {}))
    keys = list(allv.keys())
    rid = db.execute(
        f"INSERT INTO {table} ({', '.join(keys)}) VALUES ({', '.join('?' * len(keys))})",
        [allv[k] for k in keys]).lastrowid
    return rid, True


def _apply_rows(db, kind, kenpo_id, rows):
    """確定。様式ごとに登録・更新を行い、件数を返す。上位は行ごとに決まっている"""
    c = {"企業": {"ins": 0, "upd": 0}, "事業所": {"ins": 0, "upd": 0},
         "部署": {"ins": 0, "upd": 0}, "加入者": {"ins": 0, "upd": 0}}
    kname = (db.execute("SELECT name FROM kenpo WHERE id=?",
                        (kenpo_id,)).fetchone() or {"name": ""})["name"]
    for r in rows:
        if kind in ("company", "bulk"):
            cid, new = _upsert(db, "company", r["target_id"], r["vals"],
                               {"kenpo_id": kenpo_id, "ext_code": r["ext_code"],
                                "code": internal_company_code(kname, r["vals"]["name"])})
            c["企業"]["ins" if new else "upd"] += 1
        if kind == "office":
            company_id = r["company_id"]
            _, new = _upsert(db, "office", r["target_id"], r["vals"],
                             {"company_id": company_id, "ext_code": r["ext_code"],
                              "code": next_code("office", str(company_id), width=3)})
            c["事業所"]["ins" if new else "upd"] += 1
        if kind == "dept":
            company_id, office_id = r["company_id"], r["office_id"]
            _, new = _upsert(db, "department", r["target_id"], r["vals"],
                             {"company_id": company_id, "office_id": office_id,
                              "ext_code": r["ext_code"],
                              "code": next_code("dept", str(office_id or f"c{company_id}"),
                                                width=3)})
            c["部署"]["ins" if new else "upd"] += 1
        if kind == "bulk":
            oid = None
            if r.get("office"):
                o = r["office"]
                cur = o["target_id"] or _find_child(db, "office", cid, o["code"],
                                                    o["ident"], o["name"])
                oid, new = _upsert(db, "office", cur, o["vals"],
                                   {"company_id": cid, "ext_code": o["code"],
                                    "code": next_code("office", str(cid), width=3)})
                c["事業所"]["ins" if new else "upd"] += 1
            if r.get("dept"):
                d = r["dept"]
                cur = d["target_id"] or _find_dept(db, cid, oid, d["code"],
                                                   d["ident"], d["name"])
                _, new = _upsert(db, "department", cur, d["vals"],
                                 {"company_id": cid, "office_id": oid,
                                  "ext_code": d["code"],
                                  "code": next_code("dept", str(oid or f"c{cid}"), width=3)})
                c["部署"]["ins" if new else "upd"] += 1
        if kind == "member":
            vals = dict(r["vals"], company_id=r["company_id"], office_id=r["office_id"],
                        dept_id=r["dept_id"], name=r["name"])
            _, new = _upsert(db, "member", r["target_id"], vals,
                             {"kenpo_id": kenpo_id, "member_no": r["member_no"],
                              "subscriber_id": next_code("member", str(kenpo_id), width=8)})
            c["加入者"]["ins" if new else "upd"] += 1
    return {k: v for k, v in c.items() if v["ins"] or v["upd"]}


def _find_child(db, table, cid, code, ident, name):
    """一括取込で、確定のときに事業所をもう一度引き直す"""
    if code:
        r = db.execute(f"SELECT id FROM {table} WHERE company_id=? AND ext_code=?",
                       (cid, code)).fetchone()
        if r:
            return r["id"]
    if ident:
        r = db.execute(f"SELECT id FROM {table} WHERE company_id=? AND id=?",
                       (cid, ident)).fetchone()
        if r:
            return r["id"]
    r = db.execute(f"SELECT id FROM {table} WHERE company_id=? AND name=?",
                   (cid, name)).fetchone()
    return r["id"] if r else None


def _find_dept(db, cid, oid, code, ident, name):
    """一括取込で、確定のときに部署をもう一度引き直す（同じ事業所の中だけで探す）"""
    if code:
        r = db.execute("SELECT id FROM department WHERE company_id=?"
                       " AND IFNULL(office_id,0)=? AND ext_code=?",
                       (cid, oid or 0, code)).fetchone()
        if r:
            return r["id"]
    if ident:
        r = db.execute("SELECT id FROM department WHERE company_id=?"
                       " AND IFNULL(office_id,0)=? AND id=?",
                       (cid, oid or 0, ident)).fetchone()
        if r:
            return r["id"]
    rows = db.execute("SELECT id FROM department WHERE company_id=? AND IFNULL(office_id,0)=?"
                      " AND name=?", (cid, oid or 0, name)).fetchall()
    return rows[0]["id"] if len(rows) == 1 else None


# ---------------------------------------------------------------- 様式・見本の出力
@app.route("/import/format/<kind>.csv")
@login_required
def import_format(kind):
    """様式のダウンロード。担当範囲の全件が入った状態で出る（IDが埋まって出てくる）"""
    if kind not in CSV_FORMATS:
        flash("様式が見つかりません。", "error")
        return redirect(url_for("import_upload"))
    acc = current_account()
    spec = CSV_FORMATS[kind]
    rows = _format_rows(kind, acc, import_kenpo(acc))
    log("download", "様式をダウンロード", "success", target=spec["label"],
        detail=f"{len(rows)}件")
    return csv_response(f"{spec['file']}_format.csv", spec["columns"], rows)


@app.route("/import/blank/<kind>.csv")
@login_required
def import_blank(kind):
    """見本のダウンロード。見出しだけの雛形（新規に作るとき用）"""
    if kind not in CSV_FORMATS:
        flash("様式が見つかりません。", "error")
        return redirect(url_for("import_upload"))
    spec = CSV_FORMATS[kind]
    log("download", "見本をダウンロード", "success", target=spec["label"])
    return csv_response(f"{spec['file']}_blank.csv", spec["columns"], [])


def _digits(v):
    """様式に出す郵便番号・電話番号は数字だけにそろえる。
    様式は「ダウンロード → 直す → 取り込む」で使うため、そのまま取り込める形で出す
    （取込はハイフンを受け付けない）。"""
    return re.sub(r"[^0-9]", "", v or "")


def _format_rows(kind, acc, kenpo_id):
    """様式のダウンロードに載せる、いまの登録内容"""
    db = get_db()
    comps = [c for c in scoped_companies(acc) if c["kenpo_id"] == kenpo_id]
    cmap = {c["id"]: c for c in comps}

    def crow(c):
        return [c["id"], _excel_code(c["ext_code"]), c["name"], c["kana"] or "",
                c["cert_mark"] or "", c["owner"] or "", _digits(c["zip"]),
                c["address"] or "", _digits(c["tel"]), c["email"] or ""]

    def orow(o):
        """事業所の自身の列（上位の列は含まない）"""
        return [o["id"], _excel_code(o["ext_code"]), o["name"], o["kana"] or "",
                o["cert_mark"] or "", o["owner"] or "", _digits(o["zip"]),
                o["address"] or "", _digits(o["tel"]), o["email"] or ""]

    def drow(d):
        """部署の自身の列（上位の列は含まない）"""
        return [d["id"], _excel_code(d["ext_code"]), d["name"], d["kana"] or ""]

    def up_c(cid):
        """上位の企業（企業ID／企業コード）。様式をそのまま戻せるようにIDを埋める"""
        c = cmap.get(cid)
        return [cid, _excel_code(c["ext_code"] if c else "")]

    if kind == "company":
        return [crow(c) for c in comps]
    if kind == "office":
        return [up_c(o["company_id"]) + orow(o)
                for o in scoped_offices(acc) if o["company_id"] in cmap]
    if kind == "dept":
        omap = {o["id"]: o for o in scoped_offices(acc)}
        out = []
        for d in scoped_departments(acc):
            if d["company_id"] not in cmap:
                continue
            o = omap.get(d["office_id"]) if d["office_id"] else None
            # 事業所なしの部署は事業所の列を空欄で出す（取り込むときは画面で「（事業所なし）」を選ぶ）
            out.append(up_c(d["company_id"])
                       + [o["id"] if o else "", _excel_code(o["ext_code"] if o else "")]
                       + drow(d))
        return out
    if kind == "bulk":
        offs = {}
        for o in scoped_offices(acc):
            offs.setdefault(o["company_id"], []).append(o)
        deps = {}
        for d in scoped_departments(acc):
            deps.setdefault((d["company_id"], d["office_id"] or 0), []).append(d)
        blank_o, blank_d = [""] * len(OFFICE_COLUMNS), [""] * len(DEPT_COLUMNS)
        out = []
        for c in comps:
            wrote = False
            for d in deps.get((c["id"], 0), []):     # 事業所なしの部署
                out.append(crow(c) + blank_o + drow(d))
                wrote = True
            for o in offs.get(c["id"], []):
                ds = deps.get((c["id"], o["id"]), [])
                for d in ds:
                    out.append(crow(c) + orow(o) + drow(d))
                    wrote = True
                if not ds:
                    out.append(crow(c) + orow(o) + blank_d)
                    wrote = True
            if not wrote:
                out.append(crow(c) + blank_o + blank_d)
        return out
    # 加入者。取込先の健保に絞る（他の健保の行が混ざると取り込み直せないため）
    where, params = member_where(acc)
    rows = db.execute(
        "SELECT m.*, c.ext_code AS c_ext, o.ext_code AS o_ext, d.ext_code AS d_ext"
        " FROM member m LEFT JOIN company c ON c.id=m.company_id"
        " LEFT JOIN office o ON o.id=m.office_id"
        " LEFT JOIN department d ON d.id=m.dept_id"
        " WHERE m.kenpo_id=? AND " + where
        + " ORDER BY m.id", [kenpo_id] + list(params)).fetchall()

    def branch(v):
        """枝番は様式の定めどおり2桁で出す（そのまま取り込めるように）"""
        v = (v or "").strip()
        return v.zfill(2) if v.isdigit() else v

    return [[m["cert_mark"] or "", m["member_no"], branch(m["cert_branch"]),
             m["relation"] or "", m["name"], m["kana"] or "", m["sex"] or "",
             m["birth"] or "", m["qualified_at"] or "", m["lost_at"] or "",
             m["attr"] or "", _digits(m["zip"]),
             # 住所は都道府県＋市区町村＋番地を1列に戻して出す（取込の様式は1列のため）
             "".join(x for x in (m["pref"], m["city"], m["address"]) if x),
             m["address2"] or "",
             _digits(m["tel"]), m["email"] or "", m["employee_code"] or "",
             _excel_code(m["billing_code"]), m["connect_id"] or "",
             "対象" if m["influenza"] else "対象外",
             "対象外" if m["excluded"] else "対象",
             m["company_id"] or "", _excel_code(m["c_ext"]), m["office_id"] or "",
             _excel_code(m["o_ext"]), m["dept_id"] or "",
             _excel_code(m["d_ext"])] for m in rows]


# ================================================================ 出力
def export_csv(filename, header, rows, kind, cap=True):
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
    # 操作ログのように「全件を出せること」が要件の出力では cap=False で上限を外します
    if cap and len(rows) > MAX_EXPORT_ROWS:
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
    """取込フォーマット（加入者情報の様式）と同じ並びで出力する（出力したものをそのまま取込に使える）"""
    acc = current_account()
    rows = _format_rows("member", acc, import_kenpo(acc))
    return export_csv("subscriber.csv", MEMBER_COLUMNS, rows,
                      "加入者情報") or redirect(url_for("members"))


@app.route("/orgs/export")
@login_required
def orgs_export():
    """企業・事業所・部署を1つのCSVで出力する（1行＝1レコード。種別で区別）。8-76"""
    acc = current_account()
    comps = scoped_companies(acc)
    offs = scoped_offices(acc)
    depts = scoped_departments(acc)
    n_off = {}
    for o in offs:
        n_off[o["company_id"]] = n_off.get(o["company_id"], 0) + 1
    n_dep_c, n_dep_o = {}, {}
    for d in depts:
        n_dep_c[d["company_id"]] = n_dep_c.get(d["company_id"], 0) + 1
        n_dep_o[d["office_id"]] = n_dep_o.get(d["office_id"], 0) + 1
    mem_c = {r["id"]: r["c"] for r in get_db().execute(
        "SELECT company_id AS id, COUNT(*) c FROM member WHERE company_id IS NOT NULL GROUP BY company_id")}
    mem_o, mem_d = _office_counts(), _dept_counts()
    rows = []
    for c in comps:
        rows.append(("企業", c["kenpo_code"], c["ext_code"] or "", c["name"], "", "", "", "",
                     c["zip"] or "", c["address"] or "", c["tel"] or "",
                     n_off.get(c["id"], 0), n_dep_c.get(c["id"], 0), mem_c.get(c["id"], 0)))
        for o in [x for x in offs if x["company_id"] == c["id"]]:
            rows.append(("事業所", c["kenpo_code"], c["ext_code"] or "", c["name"],
                         o["ext_code"] or "", o["name"], "", "",
                         o["zip"] or "", o["address"] or "", o["tel"] or "",
                         "", n_dep_o.get(o["id"], 0), mem_o.get(o["id"], 0)))
            for d in [x for x in depts if x["office_id"] == o["id"]]:
                rows.append(("部署", c["kenpo_code"], c["ext_code"] or "", c["name"],
                             o["ext_code"] or "", o["name"], d["ext_code"] or "", d["name"],
                             "", "", "", "", "", mem_d.get(d["id"], 0)))
    header = ["種別", "保険者番号", "企業コード", "企業名", "事業所コード", "事業所名",
              "部署コード", "部署名", "郵便番号", "住所", "電話番号", "事業所数", "部署数", "加入者数"]
    return export_csv("orgs.csv", header, rows, "企業・事業所・部署") or redirect(url_for("orgs"))


# ================================================================ 機能制御の設定
@app.route("/settings/features")
@roles_required("system_admin")
def feature_settings():
    """ロール・サブロールごとの機能制御（HIAスタッフのみ）"""
    ov = feature_overrides()
    matrix = {}
    for rk in ROLE_KEYS:
        matrix[rk] = {}
        for key, _grp, _label, _desc, fixed in FEATURES:
            if feature_na(key, rk):
                matrix[rk][key] = False     # 役割上の対象外（切り替えません）
            elif fixed:
                matrix[rk][key] = (rk in fixed_roles(key))
            elif rk == ALL_FEATURE_ROLE:
                matrix[rk][key] = True      # HIAスタッフは常に利用可（切替不可）
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
            continue            # HIAスタッフは機能制御の対象外（常に全機能）
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
def _accounts_query(acc):
    """アカウント一覧の絞り込み条件からSQLを組み立てる。
    他の一覧と同じく、絞り込みはサーバ側で全件を対象に行う（表示中の件数に縛られない）。"""
    f = {k: (request.args.get(k) or "").strip()
         for k in ("name", "email", "company", "kenpo", "role", "status")}
    sql = (" FROM account a LEFT JOIN company c ON c.id=a.company_id"
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
    if f["name"]:
        sql += " AND a.name LIKE ?"
        p.append(f"%{f['name']}%")
    if f["email"]:
        sql += " AND a.email LIKE ?"
        p.append(f"%{f['email']}%")
    if f["company"]:
        # 担当範囲（企業・事業所・部署）のどれかの名前に一致する
        like = f"%{f['company']}%"
        sql += (" AND (a.id IN (SELECT ac.account_id FROM account_company ac"
                "   JOIN company x ON x.id=ac.company_id WHERE x.name LIKE ?)"
                " OR a.id IN (SELECT s.account_id FROM account_scope s"
                "   JOIN office o ON o.id=s.ref_id WHERE s.kind='office' AND o.name LIKE ?)"
                " OR a.id IN (SELECT s.account_id FROM account_scope s"
                "   JOIN department d ON d.id=s.ref_id WHERE s.kind='dept' AND d.name LIKE ?))")
        p += [like, like, like]
    if f["kenpo"]:
        sql += " AND k.name=?"
        p.append(f["kenpo"])
    if f["role"]:
        # 選択肢はラベル（「健保担当者」「企業担当者（産業医）」）で受ける
        hit = [r for r, l in ROLE_LABELS.items() if l == f["role"]]
        sub = [k for k, l in SUB_ROLE_LABELS.items() if f"企業担当者（{l}）" == f["role"]]
        if hit:
            sql += " AND a.role=?"
            p.append(hit[0])
        elif sub:
            sql += " AND a.role='company_user' AND a.sub_role=?"
            p.append(sub[0])
    if f["status"]:
        hit = [k for k, l in STATUS_LABELS.items() if l == f["status"]]
        if hit:
            sql += " AND a.status=?"
            p.append(hit[0])
    return sql, p, f


def _accounts_page(acc, offset, limit):
    db = get_db()
    sql, p, f = _accounts_query(acc)
    total = db.execute("SELECT COUNT(*) c" + sql, p).fetchone()["c"]
    rows = db.execute(
        "SELECT a.*, c.name AS company_name, k.name AS kenpo_name" + sql
        + " ORDER BY CASE a.status WHEN 'deleted' THEN 1 ELSE 0 END,"
          " a.id DESC LIMIT ? OFFSET ?", p + [limit, offset]).fetchall()
    # 担当範囲（企業・事業所・部署）を行ごとに付ける
    ids = [r["id"] for r in rows]
    comp_map, scope_map = {}, {}
    if ids:
        q = ",".join("?" * len(ids))
        for r in db.execute(
                "SELECT ac.account_id, c.name FROM account_company ac"
                f" JOIN company c ON c.id=ac.company_id WHERE ac.account_id IN ({q})"
                " ORDER BY c.id", ids):
            comp_map.setdefault(r["account_id"], []).append(r["name"])
        for r in db.execute(
                "SELECT s.account_id, o.name AS name, c.name AS pname, 'office' AS kind"
                " FROM account_scope s JOIN office o ON o.id=s.ref_id"
                f" JOIN company c ON c.id=o.company_id WHERE s.kind='office' AND s.account_id IN ({q})"
                " UNION ALL"
                " SELECT s.account_id, d.name AS name,"
                " IFNULL(o.name, c.name) AS pname, 'dept' AS kind"
                " FROM account_scope s JOIN department d ON d.id=s.ref_id"
                " JOIN company c ON c.id=d.company_id"
                f" LEFT JOIN office o ON o.id=d.office_id WHERE s.kind='dept' AND s.account_id IN ({q})",
                ids + ids):
            scope_map.setdefault(r["account_id"], []).append(
                ("事業所" if r["kind"] == "office" else "部署") + f"：{r['pname']}／{r['name']}")
    rows = [dict(r, companies=comp_map.get(r["id"], []),
                 scopes=scope_map.get(r["id"], [])) for r in rows]
    return rows, total, f


@app.route("/accounts")
@login_required
def accounts():
    """アカウント一覧。他の一覧と同じ作法（検索ボタンなし・自動絞り込み・30件ずつ読み込み）"""
    db, acc = get_db(), current_account()
    rows, total, f = _accounts_page(acc, 0, PAGE_ROWS)
    kenpos = db.execute("SELECT * FROM kenpo ORDER BY name").fetchall()
    n_invited = db.execute(
        "SELECT COUNT(*) c" + _accounts_query(acc)[0] + " AND a.status='invited'",
        _accounts_query(acc)[1]).fetchone()["c"]
    # 登録直後は案内リンク送信モーダルを開く
    inv, inv_link = None, None
    iid = request.args.get("invite", type=int)
    if iid:
        cand = db.execute("SELECT * FROM account WHERE id=?", (iid,)).fetchone()
        if cand and can_manage_account(acc, cand) and cand["status"] == "invited" \
                and cand["invite_token"]:
            inv = cand
            inv_link = ext_url("invite", token=cand["invite_token"])
    role_opts = (["すべて"] + list(ROLE_LABELS.values())
                 + [f"企業担当者（{l}）" for l in SUB_ROLE_LABELS.values()])
    return render_template("accounts.html", rows=rows, total=total, f=f, kenpos=kenpos,
                           role_opts=role_opts, n_invited=n_invited, inv=inv,
                           inv_link=inv_link)


@app.route("/accounts/rows")
@login_required
def accounts_rows():
    """スクロールで続きを読み込むための行だけを返す"""
    acc = current_account()
    offset = max(request.args.get("offset", type=int) or 0, 0)
    rows, total, f = _accounts_page(acc, offset, PAGE_ROWS)
    return render_template("_rows_accounts.html", rows=rows,
                           more=1 if offset + len(rows) < total else 0)


@app.route("/accounts/export")
@login_required
def accounts_export():
    db, acc = get_db(), current_account()
    sql = ("SELECT a.email, a.name, a.role, a.view_scope, a.can_download, a.status, 0,"
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
             r[7] or "", r[8] or "", r[9], r[10] or "")
            for r in db.execute(sql + " ORDER BY a.id", p)]
    return export_csv("accounts.csv",
                      ["メールアドレス", "利用者名", "権限ロール", "サブロール", "閲覧範囲",
                       "ダウンロード", "状態", "健康保険組合", "企業名", "作成日時",
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
                               " JOIN company c ON c.id=d.company_id WHERE d.id=?")):
        for rid in scopes.get(kind) or []:
            r = db.execute(sql, (rid,)).fetchone()
            if r:
                return r["kenpo_id"]
    return acc["kenpo_id"]


def _resolve_scope(acc, role, company_ids, office_ids=None, dept_ids=None):
    """ロールから閲覧範囲を決定する。手動指定はさせない。
    企業担当者は「健保 → 企業 → 事業所 → 部署」の連動で担当範囲を選ぶ。
    - 事業所・部署は選択企業のものに限る（企業を跨いだ指定はできない）。
    - 部署は事業所を選ばなくても直接指定できる。ただしその企業の事業所を選んでいる
      場合は、選んだ事業所の部署に限る。
    - その企業の事業所・部署を1つも選ばなければ、その企業まるごとが担当範囲。
      1つ以上選んだ場合は企業まるごとの担当を外し、選んだ事業所・部署だけに絞る。
    指定できるのは発行者の操作範囲内のものだけ。
    戻り値は (view_scope, scopes, rejected, rel_companies)。
    - scopes … 実際の閲覧範囲。company は「まるごと担当」の企業だけ（絞り込んだ企業は含めない）。
    - rel_companies … この担当が関わる企業すべて（まるごと＋絞り込み対象の親企業）。
      account_company・確認画面・編集画面の企業選択の復元に使う。確認画面の再送信で
      親企業が失われて絞り込みが消えるのを防ぐ。"""
    if role == "system_admin":
        return "all", {"company": [], "office": [], "dept": []}, [], []
    if role == "kenpo_user":
        return "kenpo_all", {"company": [], "office": [], "dept": []}, [], []
    offs = scoped_offices(acc)
    depts = scoped_departments(acc)
    ok_c = {c["id"] for c in scoped_companies(acc)}
    ok_o = {o["id"] for o in offs}
    ok_d = {d["id"] for d in depts}
    want_c = list(company_ids or [])
    want_o = list(office_ids or [])
    want_d = list(dept_ids or [])
    # 発行者の操作範囲の外にあるものは弾く（なりすまし・改ざん対策）
    rejected = ([i for i in want_c if i not in ok_c]
                + [i for i in want_o if i not in ok_o]
                + [i for i in want_d if i not in ok_d])
    comp_sel = [i for i in want_c if i in ok_c]
    cset = set(comp_sel)
    off_sel = [o["id"] for o in offs if o["id"] in set(want_o) and o["company_id"] in cset]
    oset = set(off_sel)
    # 事業所を選んだ企業では、部署はその事業所のものだけ。選んでいない企業では直接指定できる
    off_narrowed = {o["company_id"] for o in offs if o["id"] in oset}
    dept_sel = [d["id"] for d in depts
                if d["id"] in set(want_d) and d["company_id"] in cset
                and (d["office_id"] in oset if d["company_id"] in off_narrowed else True)]
    # 選択した企業すべて（まるごと＋絞り込み対象の親企業）。account_company に入れる。
    rel_companies = list(comp_sel)
    # 事業所か部署を選んだ企業は「まるごと担当」から外す（選んだ事業所・部署だけに絞る）
    narrowed = off_narrowed | {d["company_id"] for d in depts if d["id"] in set(dept_sel)}
    scope_c = [c for c in comp_sel if c not in narrowed]
    return ("own_company", {"company": scope_c, "office": off_sel, "dept": dept_sel},
            rejected, rel_companies)


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
    view_scope, scopes, rejected, rel_c = _resolve_scope(acc, role, company_ids, office_ids, dept_ids)
    company_ids = rel_c
    if view_scope == "own_company":
        if rejected:
            errs.append("選択した企業・事業所・部署のうち、操作する権限のないものが含まれています。")
        if not any(scopes.values()):
            errs.append("担当する企業を選択してください（特定の事業所・部署だけに絞ることもできます）。")
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
                           view_scope=view_scope, can_dl=can_dl,
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

    if request.form.get("confirmed") != "1":
        flash("閲覧できる加入者の範囲を確認してから発行してください。", "error")
        return redirect(url_for("accounts_new"))
    if role not in issuable_roles(acc):
        log("account", "アカウント発行をブロック", "blocked", target=email,
            detail=f"権限外のロール（{ROLE_LABELS.get(role, role)}）を指定")
        flash("そのロールを発行する権限がありません。", "error")
        return redirect(url_for("accounts_new"))
    office_ids = request.form.getlist("office_ids", type=int)
    dept_ids = request.form.getlist("dept_ids", type=int)
    view_scope, scopes, rejected, rel_c = _resolve_scope(acc, role, company_ids, office_ids, dept_ids)
    company_ids = rel_c
    if view_scope == "own_company":
        if rejected:
            log("account", "アカウント発行をブロック", "blocked", target=email,
                detail=f"スコープ外の企業・事業所・部署が指定された（{rejected}）")
            flash("選択された企業・事業所・部署のうち、操作する権限のないものが含まれています。", "error")
            return redirect(url_for("accounts_new"))
        if not any(scopes.values()):
            flash("担当する企業を選択してください。", "error")
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
        " kenpo_id, company_id, status, invite_token, invite_expire, created_by)"
        " VALUES (?,?,?,?,?,?,?,?,'invited',?,?,?)",
        (email, name, role, srole, view_scope, can_dl, kenpo_id, None,
         token, expire, acc["email"]))
    set_account_scopes(cur.lastrowid, scopes, rel_c)
    db.commit()
    log("account", "アカウントを発行", "success", target=email,
        detail=(f"ロール={ROLE_LABELS[role]}"
                + (f"（{SUB_ROLE_LABELS[srole]}）" if srole else "")
                + f"／閲覧範囲={SCOPE_LABELS[view_scope]}"
                + (f"（{company_names(scopes['company'])}）" if scopes.get("company") else "")
                + f"／担当範囲={scope_summary(scopes)}"
                + f"／閲覧対象={vis['total']}件（{vis['companies']}社・{vis['offices']}事業所）"
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
    errs = []
    if not name:
        errs.append("利用者名を入力してください。")
    if role not in roles:
        errs.append(f"「{ROLE_LABELS.get(role, role)}」へ変更する権限がありません。")
    office_ids = request.form.getlist("office_ids", type=int)
    dept_ids = request.form.getlist("dept_ids", type=int)
    view_scope, scopes, rejected, rel_c = _resolve_scope(acc, role, company_ids, office_ids, dept_ids)
    company_ids = rel_c
    if view_scope == "own_company":
        if rejected:
            errs.append("選択した企業・事業所・部署のうち、操作する権限のないものが含まれています。")
        if not any(scopes.values()):
            errs.append("担当する企業を選択してください（特定の事業所・部署だけに絞ることもできます）。")
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
        ("閲覧できる加入者", f"{vb['total']:,}件（{vb['companies']}社）",
         f"{va['total']:,}件（{va['companies']}社）"),
    ]
    return render_template("accounts_edit_confirm.html", row=row, name=name, role=role,
                           srole=srole,
                           view_scope=view_scope, can_dl=can_dl,
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
    if not name or role not in issuable_roles(acc):
        log("account", "権限変更をブロック", "blocked", target=row["email"],
            detail=f"権限外のロール（{ROLE_LABELS.get(role, role)}）への変更を試行")
        flash("そのロールへ変更する権限がありません。", "error")
        return redirect(url_for("accounts_edit", aid=aid))
    office_ids = request.form.getlist("office_ids", type=int)
    dept_ids = request.form.getlist("dept_ids", type=int)
    view_scope, scopes, rejected, rel_c = _resolve_scope(acc, role, company_ids, office_ids, dept_ids)
    company_ids = rel_c
    if view_scope == "own_company":
        if rejected:
            log("account", "権限変更をブロック", "blocked", target=row["email"],
                detail=f"スコープ外の企業・事業所・部署が指定された（{rejected}）")
            flash("選択された企業・事業所・部署のうち、操作する権限のないものが含まれています。", "error")
            return redirect(url_for("accounts_edit", aid=aid))
        if not any(scopes.values()):
            flash("担当する企業を選択してください。", "error")
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

    db.execute("UPDATE account SET name=?, role=?, sub_role=?, view_scope=?, kenpo_id=?,"
               " can_download=?, updated_at=? WHERE id=?",
               (name, role, srole, view_scope, kenpo_id, can_dl, now(), aid))
    set_account_scopes(aid, scopes, rel_c)
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
        flash("自分のパスワードは「マイアカウント」の「パスワードの変更」から変更してください。", "error")
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


def log_is_km():
    """いま HIA総合管理（当社）側で見ているか。
    HIA健保管理（青）で開いたときは、当社側（km）の操作ログは出しません。"""
    return session.get("shell") == "km"


def _log_filters(acc, args):
    """操作ログの絞り込み条件をSQLに組み立てる（画面とCSV出力で同じ条件を使う）"""
    km = log_is_km()
    f = {"category": (args.get("category") or "").strip() if km else "",
         "shell": (args.get("shell") or "").strip() if km else "",
         "q": (args.get("q") or "").strip(),
         "from": (args.get("from") or "").strip(),
         "to": (args.get("to") or "").strip()}
    sql, p = "", []
    sc, sp = _log_scope(acc)
    sql += sc
    p += sp
    if not km:
        # HIA健保管理側は、健保管理の画面での操作だけを出す
        sql += " AND shell=?"
        p.append("kenpo")
    if f["category"]:
        sql += " AND category=?"
        p.append(f["category"])
    if f["shell"]:
        sql += " AND shell=?"
        p.append(f["shell"])
    if f["q"]:
        sql += " AND (actor_email LIKE ? OR action LIKE ? OR target LIKE ? OR detail LIKE ?)"
        p += [f"%{f['q']}%"] * 4
    if f["from"]:
        sql += " AND ts >= ?"
        p.append(f["from"] + " 00:00:00")
    if f["to"]:
        sql += " AND ts <= ?"
        p.append(f["to"] + " 23:59:59")
    return f, sql, p


@app.route("/logs")
@login_required
def logs():
    db, acc = get_db(), current_account()
    f, where, p = _log_filters(acc, request.args)
    sql = "SELECT * FROM audit_log WHERE 1=1" + where
    total = db.execute("SELECT COUNT(*) c FROM (" + sql + ")", p).fetchone()["c"]
    rows = db.execute(sql + " ORDER BY id DESC LIMIT 300", p).fetchall()
    return render_template("logs.html", rows=rows, total=total, cat=f["category"],
                           shell=f["shell"], kw=f["q"], dfrom=f["from"], dto=f["to"],
                           KM_LOG=log_is_km())


LOG_EXPORT_COLUMNS = ["日時", "画面", "区分", "操作", "結果", "実行者", "実行者ロール",
                      "IPアドレス", "対象", "ディレクトリ", "詳細"]


@app.route("/logs/export")
@login_required
def logs_export():
    """操作ログをCSVで出力する。

    画面の一覧は最大300件までしか出しませんが、CSVは**該当する全件**を出します。
    `all=1` を付けると絞り込みを無視して、権限の範囲のログをすべて出力します。
    件数の上限（HIA_MAX_EXPORT_ROWS）は、記録の保全のため操作ログには適用しません。
    """
    db, acc = get_db(), current_account()
    everything = request.args.get("all") in ("1", "true", "on")
    args = {} if everything else request.args
    f, where, p = _log_filters(acc, args)
    sql = ("SELECT ts, shell, category, action, result, actor_email, actor_role, ip,"
           " target, path, detail FROM audit_log WHERE 1=1" + where)
    rows = [(r[0], {"km": "HIA総合管理", "kenpo": "HIA健保管理"}.get(r[1], r[1] or ""),
             r[2], r[3],
             {"success": "成功", "failure": "失敗", "blocked": "ブロック"}.get(r[4], r[4]),
             r[5] or "", ROLE_LABELS.get(r[6], r[6] or ""), r[7] or "", r[8] or "",
             r[9] or "", r[10] or "")
            for r in db.execute(sql + " ORDER BY id", p)]
    # 絞り込みの語句（氏名など）は記録に残さないため、条件は項目名だけを書きます
    if everything:
        cond = "すべて"
    else:
        names = {"category": "区分", "shell": "画面", "q": "キーワード",
                 "from": "開始日", "to": "終了日"}
        used = [f"{names[k]}={v}" if k != "q" else "キーワード指定あり"
                for k, v in f.items() if v]
        cond = "／".join(used) or "絞り込みなし"
    return export_csv("audit_log.csv", LOG_EXPORT_COLUMNS, rows,
                      f"操作ログ（{cond}）", cap=False) or redirect(url_for("logs"))


@app.errorhandler(404)
def nf(e):
    return render_template("denied.html", path=request.path, notfound=True), 404


LOG_DIR = os.path.join(BASE_DIR, "logs")


@app.errorhandler(500)
def server_error(e):
    """画面の処理でエラーになったとき（8-68）。原因（トレースバック）を logs/error.log に残し、
    画面には日本語の案内と「エラーID」を出す。エラーIDで error.log の該当箇所を探せる"""
    import traceback
    err_id = datetime.now().strftime("%Y%m%d-%H%M%S-") + secrets.token_hex(2)
    orig = getattr(e, "original_exception", None) or e
    tb = "".join(traceback.format_exception(type(orig), orig, orig.__traceback__))
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        with open(os.path.join(LOG_DIR, "error.log"), "a", encoding="utf-8") as f:
            f.write(f"===== {err_id}  {datetime.now():%Y-%m-%d %H:%M:%S}  {request.method} {request.path}\n"
                    f"{tb}\n")
    except Exception:
        pass
    summary = f"{type(orig).__name__}: {orig}"[:300]
    try:
        html = render_template("error500.html", err_id=err_id, path=request.path, summary=summary)
    except Exception:
        html = (f"<h1>エラーが発生しました</h1><p>エラーID: {err_id}</p><p>{summary}</p>"
                f"<p>logs/error.log に詳細を記録しました。</p>")
    return html, 500


# ================================================================ 産業医面談管理
# 別システム「産業医面談管理システム」の機能を組み込んだモジュール（/oh …）。
# 認証・ロール・担当範囲・操作ログ・CSV出力は、このファイルの共通処理をそのまま使う。
import sys as _sys           # noqa: E402
import sanmen               # noqa: E402

sanmen.init_app(app, _sys.modules[__name__])

# ================================================================ 健康管理
# 加入者マスタとは別に、加入者一人ひとりの健康を管理する画面（/health …）。
import health                # noqa: E402

health.init_app(app, _sys.modules[__name__])


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


# 起動方法（run.bat の waitress／python app.py）に関わらず、版の不一致は起動時のコンソールに出す（8-70）
if build_mismatch():
    print("[HIA] ******** 注意 ******** " + build_mismatch())

if __name__ == "__main__":
    for line in BOOTSTRAP_LINES:
        print("[HIA] " + line)
    if _DB_WAS_MISSING:
        print("[HIA] seed.py で初期データを投入してください。")
    host = os.environ.get("HIA_HOST", "0.0.0.0")
    port = pick_port(host, int(os.environ.get("HIA_PORT", "8000")))
    ip = local_ipv4() if host == "0.0.0.0" else host
    print(f"[HIA] build {BUILD} / {BUILD_ID}")
    if build_mismatch():
        print("[HIA] ******** 注意 ******** " + build_mismatch())
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
    print(f"[HIA] HIA総合管理       http://{ip}:{port}/km")
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
