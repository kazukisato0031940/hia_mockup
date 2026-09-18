# -*- coding: utf-8 -*-
"""ロールごとのサンプルアカウントを作る（何度実行しても同じ結果になります）

  python seed_samples.py            hia.db に作成・更新
  python seed_samples.py hia_initial.db   別のデータベースに作成・更新

作るアカウント（パスワードは共通： Hia-Sample-2026 ）
  当社スタッフ      admin.sample@example.local    HIA総合管理（全健保）
  健保担当者        kenpo.sample@example.local    HIA健保管理（サンプル健康保険組合）
  企業担当者        company.sample@example.local  HIA健保管理（サンプル商事株式会社）
  企業担当者・人事  hr.sample@example.local       HIA健保管理（サンプル健保の3社）
  企業担当者・産業医 doctor.sample@example.local  HIA健保管理（同上。面談対象者一覧・面談結果入力）
  企業担当者・保健師 nurse.sample@example.local   HIA健保管理（同上。加入者健康一覧・面談結果入力）
  加入者本人        member.sample@example.local   健康マイページ（見本 004）

いずれも status=active（初回パスワード設定は不要）。既にあるときは名前・権限・パスワードを上書きします。

起動時（migrate.ensure_schema）にも ensure_samples(con) を呼び、無いものだけ作り直します。
プログラムを新しい版に差し替えてもサンプルアカウントは残り、画面で変えた名前やパスワードも保たれます。
"""
import os
import sqlite3
import sys

from werkzeug.security import generate_password_hash

BASE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(BASE, sys.argv[1] if len(sys.argv) > 1 else "hia.db")
PASSWORD = os.environ.get("HIA_SAMPLE_PASSWORD", "Hia-Sample-2026")

# サンプル健康保険組合（K001）とその3社を使う（面談・健診の見本データが入っている）
KENPO_CODE = "K001"
COMPANY_CODES = ["C2C1E62B", "C38380BC", "C882D17F"]   # サンプル商事／テスト工業／見本サービス
MEMBER_SUBSCRIBER_ID = "00000004"                       # 見本 004（加入者本人のログイン用）


def sample_rows(con):
    """(email, 名前, role, sub_role, view_scope, can_download, kenpo_id, company_id, member_id, 担当企業) の一覧"""
    kenpo = con.execute("SELECT id, name FROM kenpo WHERE code=?", (KENPO_CODE,)).fetchone()
    if not kenpo:
        kenpo = con.execute("SELECT id, name FROM kenpo ORDER BY id LIMIT 1").fetchone()
    if not kenpo:
        return None, [], None, []
    kid = kenpo["id"]
    comps = [con.execute("SELECT id FROM company WHERE kenpo_id=? AND code=?", (kid, c)).fetchone()
             for c in COMPANY_CODES]
    comps = [c["id"] for c in comps if c]
    if not comps:
        comps = [r["id"] for r in con.execute("SELECT id FROM company WHERE kenpo_id=? ORDER BY id LIMIT 3", (kid,))]
    member = con.execute("SELECT id, name FROM member WHERE kenpo_id=? AND subscriber_id=?",
                         (kid, MEMBER_SUBSCRIBER_ID)).fetchone()
    if not member:
        member = con.execute(
            "SELECT id, name FROM member WHERE kenpo_id=? AND id NOT IN"
            " (SELECT member_id FROM account WHERE member_id IS NOT NULL) ORDER BY id LIMIT 1", (kid,)).fetchone()
    c0 = comps[0] if comps else None
    rows = [
        ("admin.sample@example.local", "当社スタッフ 見本", "system_admin", "", "all", 1, None, None, None, []),
        ("kenpo.sample@example.local", "健保担当者 見本", "kenpo_user", "", "kenpo_all", 1, kid, None, None, []),
        ("company.sample@example.local", "企業担当者 見本", "company_user", "", "own_company", 0, kid, c0, None, comps[:1]),
        ("hr.sample@example.local", "人事 見本", "company_user", "hr", "own_company", 1, kid, None, None, comps),
        ("doctor.sample@example.local", "産業医 見本", "company_user", "doctor", "own_company", 1, kid, None, None, comps),
        ("nurse.sample@example.local", "保健師 見本", "company_user", "nurse", "own_company", 1, kid, None, None, comps),
    ]
    if member:
        rows.append(("member.sample@example.local", "加入者本人 見本", "member", "", "self", 0, kid, None,
                     member["id"], []))
    return kenpo, comps, member, rows


def ensure_samples(con, overwrite=False, password=None):
    """サンプルアカウントをそろえる。overwrite=False なら無いものだけ作る（起動時の呼び出し用）。
    戻り値は [(作成|更新|そのまま, email, 名前)]"""
    con.row_factory = sqlite3.Row
    if "account" not in {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}:
        return []
    kenpo, comps, member, rows = sample_rows(con)
    if not rows:
        return []
    pw = generate_password_hash(password or PASSWORD)
    made = []
    for email, name, role, sub, scope, dl, k, c, m, cs in rows:
        cur = con.execute("SELECT id FROM account WHERE lower(email)=lower(?)", (email,)).fetchone()
        if cur and not overwrite:
            made.append(("そのまま", email, name))
            continue
        if cur:
            aid = cur["id"]
            con.execute(
                "UPDATE account SET name=?, role=?, sub_role=?, view_scope=?, can_download=?,"
                " kenpo_id=?, company_id=?, member_id=?, status='active', password_hash=?,"
                " invite_token=NULL, invite_expire=NULL, reset_token=NULL, reset_expire=NULL,"
                " updated_at=datetime('now','localtime') WHERE id=?",
                (name, role, sub, scope, dl, k, c, m, pw, aid))
            act = "更新"
        else:
            con.execute(
                "INSERT INTO account (email, name, role, sub_role, view_scope, can_download,"
                " kenpo_id, company_id, member_id, status, password_hash, created_by, activated_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,'active',?, 'seed_samples', datetime('now','localtime'))",
                (email, name, role, sub, scope, dl, k, c, m, pw))
            aid = con.execute("SELECT id FROM account WHERE email=?", (email,)).fetchone()["id"]
            act = "作成"
        con.execute("DELETE FROM account_company WHERE account_id=?", (aid,))
        for cid in cs:
            con.execute("INSERT OR IGNORE INTO account_company (account_id, company_id) VALUES (?,?)", (aid, cid))
        made.append((act, email, name))
    con.commit()
    return made


def main():
    if not os.path.exists(DB):
        print(f"{os.path.basename(DB)} がありません。")
        return 1
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    kenpo, comps, member, rows = sample_rows(con)
    if not rows:
        print("健康保険組合がありません。seed.py を先に実行してください。")
        return 1
    made = ensure_samples(con, overwrite=True)
    print(f"データベース: {os.path.basename(DB)}　健康保険組合: {kenpo['name']}　担当企業: {len(comps)}社"
          + (f"　加入者本人: {member['name']}" if member else "　（加入者本人のサンプルは作れませんでした）"))
    for act, email, name in made:
        print(f"  {act}  {email:32s} {name}")
    print(f"パスワード（共通）: {PASSWORD}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
