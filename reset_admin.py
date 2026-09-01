# -*- coding: utf-8 -*-
"""管理者アカウントの確認とパスワードの再設定

パスワードはハッシュ化して保存しているため、後から見ることはできません。
分からなくなった場合はこのツールで再設定してください。

使い方:
  python reset_admin.py                     アカウント一覧を表示
  python reset_admin.py --admin             システム管理者のパスワードを強制的に再設定
  python reset_admin.py admin@example.local パスワードを再設定（存在しなければ新規作成）

環境変数 HIA_ADMIN_PASSWORD を指定すると、入力を求めずにその値を設定します。
"""
import getpass
import os
import re
import sqlite3
import sys

from werkzeug.security import generate_password_hash

BASE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(BASE, "hia.db")

ROLE_LABELS = {
    "system_admin": "システム管理者（当社）",
    "kenpo_admin": "健保管理者",
    "company_user": "企業担当",
}
STATUS_LABELS = {"active": "有効", "invited": "PW未設定", "disabled": "無効"}


def show_accounts(con):
    rows = con.execute(
        "SELECT a.email, a.name, a.role, a.status, c.name AS cname"
        " FROM account a LEFT JOIN company c ON c.id=a.company_id"
        " ORDER BY CASE a.role WHEN 'system_admin' THEN 0 WHEN 'kenpo_admin' THEN 1"
        " ELSE 2 END, a.id").fetchall()
    if not rows:
        print("アカウントが登録されていません。先に  python seed.py  を実行してください。")
        return rows
    print("登録されているアカウント")
    print("-" * 74)
    print(f"{'ログインID':34}{'ロール':22}{'状態':10}")
    print("-" * 74)
    for r in rows:
        print(f"{r['email']:34}{ROLE_LABELS.get(r['role'], r['role']):22}"
              f"{STATUS_LABELS.get(r['status'], r['status']):10}")
    print("-" * 74)
    return rows


def ask_password():
    env = os.environ.get("HIA_ADMIN_PASSWORD")
    if env:
        return env
    while True:
        p1 = getpass.getpass("新しいパスワード（12文字以上・英字と数字を含む）: ")
        if len(p1) < 12:
            print("  12文字以上で入力してください。")
            continue
        if not re.search(r"[A-Za-z]", p1) or not re.search(r"\d", p1):
            print("  英字と数字をそれぞれ1文字以上含めてください。")
            continue
        p2 = getpass.getpass("確認のためもう一度入力: ")
        if p1 != p2:
            print("  一致しませんでした。もう一度入力してください。")
            continue
        return p1


def main():
    if not os.path.exists(DB):
        print("hia.db が見つかりません。先に  python seed.py  を実行してください。")
        return
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row

    rows = show_accounts(con)
    if len(sys.argv) < 2:
        print()
        print("パスワードを再設定する場合は、ログインIDを指定して実行してください。")
        print("  例)  python reset_admin.py admin@example.local")
        print("  管理者を強制リセットする場合は  python reset_admin.py --admin")
        con.close()
        return

    arg = sys.argv[1].strip()
    if arg in ("--admin", "-a"):
        r = con.execute("SELECT email FROM account WHERE role='system_admin'"
                        " ORDER BY id LIMIT 1").fetchone()
        if not r:
            print("システム管理者のアカウントがありません。ログインIDを指定してください。")
            con.close()
            return
        email = r["email"]
    else:
        email = arg.lower()
    row = con.execute("SELECT * FROM account WHERE lower(email)=lower(?)",
                      (email,)).fetchone()
    print()
    if row:
        print(f"対象: {email}（{ROLE_LABELS.get(row['role'], row['role'])}）のパスワードを再設定します。")
    else:
        print(f"対象: {email} は未登録です。システム管理者として新規作成します。")
        if con.execute("SELECT COUNT(*) c FROM kenpo").fetchone()["c"] == 0:
            print("健康保険組合が登録されていません。先に  python seed.py  を実行してください。")
            con.close()
            return

    pw = ask_password()
    h = generate_password_hash(pw)
    if row:
        con.execute(
            "UPDATE account SET password_hash=?, status='active', invite_token=NULL,"
            " invite_expire=NULL WHERE id=?", (h, row["id"]))
        action = "パスワードを再設定しました"
    else:
        con.execute(
            "INSERT INTO account (email, name, role, view_scope, can_download,"
            " status, password_hash, created_by) VALUES (?,?,?,?,?,'active',?,?)",
            (email, "当社スタッフ", "system_admin", "all", 1, h, "reset_admin"))
        action = "アカウントを作成しました"
    con.execute(
        "INSERT INTO audit_log (category, action, result, actor_email, ip, target, detail)"
        " VALUES ('auth', ?, 'success', 'reset_admin.py', 'local', ?, 'コンソールから実行')",
        ("管理者パスワードを再設定" if row else "管理者アカウントを作成", email))
    con.commit()
    con.close()

    print()
    print("=" * 62)
    print(action)
    print(f"  ログインID : {email}")
    print("  パスワード : 入力した値（画面には表示しません）")
    print("=" * 62)
    print("http://10.0.105.122:8000/login からログインしてください。")


if __name__ == "__main__":
    main()
