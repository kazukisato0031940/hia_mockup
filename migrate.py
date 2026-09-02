# -*- coding: utf-8 -*-
"""データベースのスキーマを確認し、必要なら移行する。

旧バージョン（部署・reset列なし・shell列なし）のデータベースを、
データを保持したまま新スキーマへ移行する。何度実行しても安全（冪等）。

使い方:
  python migrate.py          移行を実行して結果を表示
app.py / seed.py からも起動時に自動で呼ばれる。
"""
import os
import sqlite3
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(BASE, "hia.db")
SCHEMA = os.path.join(BASE, "schema.sql")
# 産業医面談管理の追加テーブル（新規作成でも移行でも、常にこのファイルを適用する）
SCHEMA_SANMEN = os.path.join(BASE, "schema_sanmen.sql")

# 旧ロール名 → 新ロール名
ROLE_MAP = {
    "kenpo_admin": "kenpo_user",
    "company_admin": "company_user",
    "kenpo_user": "kenpo_user",
    "company_user": "company_user",
    "system_admin": "system_admin",
}


def cols(con, table):
    try:
        return {r[1] for r in con.execute(f"PRAGMA table_info({table})")}
    except sqlite3.Error:
        return set()


def tables(con):
    return {r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}


def add_col(con, table, col, decl, log):
    if col not in cols(con, table):
        con.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
        log.append(f"{table}.{col} を追加")


def apply_sanmen(con, log):
    """産業医面談管理のテーブルを作成する（CREATE TABLE IF NOT EXISTS のため何度でも安全）"""
    if not os.path.exists(SCHEMA_SANMEN):
        return
    before = tables(con)
    with open(SCHEMA_SANMEN, encoding="utf-8") as f:
        con.executescript(f.read())
    # 深夜業従事区分（深夜健診の対象者判定に使う）
    if "night_work" not in cols(con, "member"):
        con.execute("ALTER TABLE member ADD COLUMN night_work INTEGER NOT NULL DEFAULT 0")
        log.append("member に night_work（深夜業従事）を追加")
    added = sorted(t for t in tables(con) - before if t.startswith("oh_"))
    if added:
        log.append(f"産業医面談管理のテーブルを追加（{len(added)}件）")
    con.commit()


def ensure_schema(db_path=DB, verbose=False):
    """新規なら schema.sql で作成、既存なら不足分を移行する。戻り値は実施内容のリスト。"""
    fresh = not os.path.exists(db_path)
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    log = []

    if fresh or "account" not in tables(con):
        with open(SCHEMA, encoding="utf-8") as f:
            con.executescript(f.read())
        con.commit()
        apply_sanmen(con, log)
        con.close()
        return ["データベースを新規作成しました"] + log

    con.execute("PRAGMA foreign_keys = OFF")

    # ---------- 1. アカウント ----------
    add_col(con, "account", "is_primary", "INTEGER NOT NULL DEFAULT 0", log)
    add_col(con, "account", "reset_token", "TEXT", log)
    add_col(con, "account", "reset_expire", "TEXT", log)
    add_col(con, "account", "reset_at", "TEXT", log)
    add_col(con, "account", "updated_at", "TEXT", log)
    # 企業担当者のサブロール（産業医・人事）
    add_col(con, "account", "sub_role", "TEXT NOT NULL DEFAULT ''", log)
    # 旧構成（産業医・人事を独立したロールにしていたもの）を
    # 「企業担当者＋サブロール」へ付け替える
    for old_role in ("doctor", "hr"):
        n = con.execute("SELECT COUNT(*) c FROM account WHERE role=?",
                        (old_role,)).fetchone()["c"]
        if n:
            con.execute("UPDATE account SET role='company_user', sub_role=? WHERE role=?",
                        (old_role, old_role))
            log.append(f"ロール {old_role} を 企業担当者＋サブロール へ付け替え（{n} 件）")

    # ロール名の付け替え
    for old, new in ROLE_MAP.items():
        if old == new:
            continue
        n = con.execute("SELECT COUNT(*) c FROM account WHERE role=?", (old,)).fetchone()["c"]
        if n:
            con.execute("UPDATE account SET role=? WHERE role=?", (new, old))
            log.append(f"ロール {old} → {new} を {n} 件変更")
    # 当社スタッフの閲覧範囲を all に
    n = con.execute("SELECT COUNT(*) c FROM account WHERE role='system_admin'"
                    " AND view_scope<>'all'").fetchone()["c"]
    if n:
        con.execute("UPDATE account SET view_scope='all' WHERE role='system_admin'")
        log.append(f"当社スタッフの閲覧範囲を all に変更（{n} 件）")

    # ---------- 2. 企業 ----------
    for c, d in (("kana", "TEXT"), ("tel", "TEXT"), ("address", "TEXT"),
                 ("updated_at", "TEXT")):
        add_col(con, "company", c, d, log)

    # ---------- 3. 事業所（旧 department から移行） ----------
    tb = tables(con)
    if "office" not in tb:
        con.execute("""
            CREATE TABLE office (
              id         INTEGER PRIMARY KEY AUTOINCREMENT,
              company_id INTEGER NOT NULL REFERENCES company(id),
              code       TEXT NOT NULL,
              name       TEXT NOT NULL,
              tel        TEXT,
              address    TEXT,
              created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
              updated_at TEXT,
              UNIQUE (company_id, code),
              UNIQUE (company_id, name)
            )""")
        log.append("office テーブルを作成")
        if "department" in tb:
            con.execute("INSERT INTO office (id, company_id, code, name, created_at)"
                        " SELECT id, company_id, code, name, created_at FROM department")
            n = con.execute("SELECT COUNT(*) c FROM office").fetchone()["c"]
            log.append(f"部署 {n} 件を事業所へ移行")
    else:
        for c, d in (("tel", "TEXT"), ("address", "TEXT"), ("updated_at", "TEXT")):
            add_col(con, "office", c, d, log)

    # ---------- 4. 加入者（department_id を office_id へ。テーブル再作成） ----------
    mc = cols(con, "member")
    if "office_id" not in mc:
        con.execute("""
            CREATE TABLE member_new (
              id         INTEGER PRIMARY KEY AUTOINCREMENT,
              kenpo_id   INTEGER NOT NULL REFERENCES kenpo(id),
              company_id INTEGER NOT NULL REFERENCES company(id),
              office_id  INTEGER NOT NULL REFERENCES office(id),
              member_no  TEXT NOT NULL,
              name       TEXT NOT NULL,
              kana       TEXT,
              birth      TEXT,
              sex        TEXT,
              created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
              updated_at TEXT,
              UNIQUE (kenpo_id, member_no)
            )""")
        src = "department_id" if "department_id" in mc else "office_id"
        con.execute(f"INSERT INTO member_new (id, kenpo_id, company_id, office_id, member_no,"
                    f" name, kana, birth, sex, created_at)"
                    f" SELECT id, kenpo_id, company_id, {src}, member_no, name, kana, birth,"
                    f" sex, created_at FROM member")
        n = con.execute("SELECT COUNT(*) c FROM member_new").fetchone()["c"]
        con.execute("DROP TABLE member")
        con.execute("ALTER TABLE member_new RENAME TO member")
        log.append(f"加入者テーブルを再作成し {n} 件を移行（部署→事業所）")
    else:
        add_col(con, "member", "updated_at", "TEXT", log)

    # ---------- 5. 旧 department（企業直下の部署）だけを削除 ----------
    # 現在の department は「事業所の下の部署」なので、
    # 旧構造（company_id を持つもの）のときだけ削除する
    if "department" in tables(con) and "company_id" in cols(con, "department"):
        con.execute("DROP TABLE department")
        log.append("旧 department テーブル（企業直下の部署）を削除")

    # ---------- 5.4 企業・事業所の追加項目（実際の登録フォーマットに合わせる） ----------
    for table, col, label in (("company", "cert_mark", "被保険者証記号"),
                              ("company", "zip", "郵便番号"),
                              ("company", "email", "担当メールアドレス"),
                              ("office", "kana", "部署名（フリガナ）"),
                              ("office", "zip", "郵便番号")):
        if col not in cols(con, table):
            con.execute(f"ALTER TABLE {table} ADD COLUMN {col} TEXT")
            log.append(f"{table} に {col}（{label}）を追加")

    # ---------- 5.41 疾患予測のテーブルを追加 ----------
    if "nsips_sync" not in tables(con):
        con.executescript("""
        CREATE TABLE nsips_sync (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          kenpo_id INTEGER NOT NULL REFERENCES kenpo(id),
          started_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
          finished_at TEXT, status TEXT NOT NULL DEFAULT 'running',
          mode TEXT NOT NULL DEFAULT 'batch',
          fetched INTEGER NOT NULL DEFAULT 0, imported INTEGER NOT NULL DEFAULT 0,
          excluded INTEGER NOT NULL DEFAULT 0, message TEXT, actor TEXT);
        CREATE TABLE nsips_record (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          sync_id INTEGER NOT NULL REFERENCES nsips_sync(id),
          kenpo_id INTEGER NOT NULL REFERENCES kenpo(id),
          age_band TEXT NOT NULL, sex TEXT NOT NULL, drug_class TEXT NOT NULL,
          persons INTEGER NOT NULL DEFAULT 0, months REAL NOT NULL DEFAULT 0,
          gap_rate REAL NOT NULL DEFAULT 0);
        CREATE TABLE risk_run (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          kenpo_id INTEGER NOT NULL REFERENCES kenpo(id),
          run_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
          engine TEXT NOT NULL, horizon INTEGER NOT NULL DEFAULT 3,
          n_groups INTEGER NOT NULL DEFAULT 0, n_members INTEGER NOT NULL DEFAULT 0,
          message TEXT, actor TEXT);
        CREATE TABLE risk_score (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          run_id INTEGER NOT NULL REFERENCES risk_run(id),
          kenpo_id INTEGER NOT NULL REFERENCES kenpo(id),
          company_id INTEGER REFERENCES company(id),
          age_band TEXT NOT NULL, sex TEXT NOT NULL, disease TEXT NOT NULL,
          n_members INTEGER NOT NULL DEFAULT 0, score REAL NOT NULL DEFAULT 0,
          level TEXT NOT NULL, priority TEXT NOT NULL);
        CREATE INDEX ix_risk_score_run ON risk_score (run_id);""")
        log.append("疾患予測のテーブル（NSIPS連携・予測結果）を追加")

    # ---------- 5.415 健診結果の連携テーブルを追加 ----------
    if "kenshin_sync" not in tables(con):
        con.executescript("""
        CREATE TABLE kenshin_sync (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          kenpo_id INTEGER NOT NULL REFERENCES kenpo(id),
          started_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
          finished_at TEXT, status TEXT NOT NULL DEFAULT 'running',
          fiscal_year TEXT, fetched INTEGER NOT NULL DEFAULT 0,
          imported INTEGER NOT NULL DEFAULT 0, excluded INTEGER NOT NULL DEFAULT 0,
          message TEXT, actor TEXT);
        CREATE TABLE kenshin_record (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          sync_id INTEGER NOT NULL REFERENCES kenshin_sync(id),
          kenpo_id INTEGER NOT NULL REFERENCES kenpo(id),
          age_band TEXT NOT NULL, sex TEXT NOT NULL, item TEXT NOT NULL,
          normal INTEGER NOT NULL DEFAULT 0, caution INTEGER NOT NULL DEFAULT 0,
          medical INTEGER NOT NULL DEFAULT 0);
        CREATE INDEX ix_kenshin_record_sync ON kenshin_record (sync_id);""")
        log.append("健診結果の連携テーブル（予約管理システムからのバッチ）を追加")

    # ---------- 5.416 疾患予測を個人単位にする ----------
    if "kenshin_result" not in tables(con):
        con.executescript("""
        CREATE TABLE kenshin_result (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          sync_id INTEGER NOT NULL REFERENCES kenshin_sync(id),
          kenpo_id INTEGER NOT NULL REFERENCES kenpo(id),
          member_id INTEGER NOT NULL REFERENCES member(id),
          exam_date TEXT, item TEXT NOT NULL, judge TEXT NOT NULL, detail TEXT);
        CREATE INDEX ix_kenshin_result_m ON kenshin_result (member_id);
        CREATE INDEX ix_kenshin_result_s ON kenshin_result (sync_id);""")
        log.append("個人単位の健診結果テーブルを追加")
    if "risk_score" in tables(con) and "member_id" not in cols(con, "risk_score"):
        con.execute("ALTER TABLE risk_score ADD COLUMN member_id INTEGER")
        con.execute("DELETE FROM risk_score")
        con.execute("DELETE FROM risk_run")
        log.append("risk_score を個人単位に変更（過去の予測結果は作り直しが必要）")

    # ---------- 5.417 健保ごとの機能フラグを追加 ----------
    for col, label in (("publish_auth", "公開権限"), ("kenshin_auth", "健診代行権限"),
                       ("guidance_auth", "保健指導権限"), ("flu_enabled", "インフル機能"),
                       ("n_hospital", "登録医療機関数")):
        if col not in cols(con, "kenpo"):
            con.execute(f"ALTER TABLE kenpo ADD COLUMN {col} INTEGER NOT NULL DEFAULT 0")
            log.append(f"kenpo に {col}（{label}）を追加")

    # ---------- 5.418 判定グループのテーブルを追加 ----------
    if "judge_group" not in tables(con):
        con.executescript("""
        CREATE TABLE judge_group (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          name TEXT NOT NULL UNIQUE,
          method TEXT NOT NULL DEFAULT 'worst',
          note TEXT, sort INTEGER NOT NULL DEFAULT 0,
          updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime')));
        CREATE TABLE judge_group_item (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          group_id INTEGER NOT NULL REFERENCES judge_group(id),
          code TEXT, name TEXT NOT NULL,
          direction TEXT NOT NULL DEFAULT 'high',
          caution_min REAL, medical_min REAL, unit TEXT,
          sort INTEGER NOT NULL DEFAULT 0);
        CREATE INDEX ix_jgi_group ON judge_group_item (group_id);""")
        log.append("判定グループのテーブル（複合検査の判定）を追加")

    # ---------- 5.42 先方が管理する番号（ext_code）を追加 ----------
    for table, label in (("company", "事業所（企業）コード"),
                         ("office", "所属コード"),
                         ("department", "部署コード")):
        if table in tables(con) and "ext_code" not in cols(con, table):
            con.execute(f"ALTER TABLE {table} ADD COLUMN ext_code TEXT")
            # これまでのコードを先方の番号として引き継ぐ
            con.execute(f"UPDATE {table} SET ext_code = code WHERE ext_code IS NULL")
            con.execute(f"CREATE UNIQUE INDEX IF NOT EXISTS ux_{table}_ext"
                        + (" ON company (kenpo_id, ext_code)" if table == "company" else
                           " ON office (company_id, ext_code)" if table == "office" else
                           " ON department (office_id, ext_code)"))
            log.append(f"{table} に ext_code（{label}）を追加し、既存のコードを引き継ぎ")

    # ---------- 5.43 アカウントの担当範囲（企業・事業所・部署を跨げるように） ----------
    if "account_scope" not in tables(con):
        con.executescript("""
        CREATE TABLE account_scope (
          account_id INTEGER NOT NULL REFERENCES account(id),
          kind       TEXT NOT NULL,
          ref_id     INTEGER NOT NULL,
          UNIQUE (account_id, kind, ref_id)
        );""")
        # 既存の対象企業を引き継ぐ
        if "account_company" in tables(con):
            con.execute("INSERT OR IGNORE INTO account_scope (account_id, kind, ref_id)"
                        " SELECT account_id, 'company', company_id FROM account_company")
        # 部署だけを担当していたアカウントも引き継ぐ
        if "dept_id" in cols(con, "account"):
            con.execute("INSERT OR IGNORE INTO account_scope (account_id, kind, ref_id)"
                        " SELECT id, 'dept', dept_id FROM account WHERE dept_id IS NOT NULL")
        n = con.execute("SELECT COUNT(*) FROM account_scope").fetchone()[0]
        log.append(f"アカウントの担当範囲テーブルを追加（既存{n}件を引き継ぎ）")

    # ---------- 5.44 部署（事業所の下）を追加 ----------
    if "department" not in tables(con):
        con.executescript("""
        CREATE TABLE department (
          id         INTEGER PRIMARY KEY AUTOINCREMENT,
          office_id  INTEGER NOT NULL REFERENCES office(id),
          code       TEXT NOT NULL,
          name       TEXT NOT NULL,
          kana       TEXT,
          created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
          updated_at TEXT,
          UNIQUE (office_id, code),
          UNIQUE (office_id, name)
        );""")
        log.append("部署（事業所の下）のテーブルを追加")
    if "dept_id" not in cols(con, "member"):
        con.execute("ALTER TABLE member ADD COLUMN dept_id INTEGER REFERENCES department(id)")
        log.append("member に dept_id（部署）を追加")
    if "dept_id" not in cols(con, "account"):
        con.execute("ALTER TABLE account ADD COLUMN dept_id INTEGER REFERENCES department(id)")
        log.append("account に dept_id（担当部署）を追加")

    # ---------- 5.45 加入者の追加項目（実際の登録フォーマットに合わせる） ----------
    for col, label in (("cert_mark", "被保険者証記号"), ("cert_branch", "被保険者証枝番"),
                       ("attr", "被保険者属性名"), ("relation", "続柄名称"),
                       ("qualified_at", "資格取得日"), ("lost_at", "資格喪失日"),
                       ("zip", "郵便番号"), ("address", "住所"),
                       ("address2", "住所（建物名）"), ("tel", "電話番号"),
                       ("email", "メールアドレス"), ("delivery_code", "配付先コード"),
                       ("employee_code", "社員コード"), ("connect_id", "connectID"),
                       ("personal_id", "個人ID"), ("subscriber_id", "加入者ID")):
        if col not in cols(con, "member"):
            con.execute(f"ALTER TABLE member ADD COLUMN {col} TEXT")
            log.append(f"member に {col}（{label}）を追加")

    # ---------- 5.455 取込時のコードを加入者に残す ----------
    for col, label in (("src_company_code", "取込時の事業所（企業）コード"),
                       ("src_office_code", "取込時の所属コード")):
        if col not in cols(con, "member"):
            con.execute(f"ALTER TABLE member ADD COLUMN {col} TEXT")
            log.append(f"member に {col}（{label}）を追加")

    # ---------- 5.46 加入者の企業・事業所を任意にする（あとから紐づける運用） ----------
    info = list(con.execute("PRAGMA table_info(member)"))
    notnull = {r[1]: r[3] for r in info}
    ddl = con.execute("SELECT sql FROM sqlite_master WHERE name='member'").fetchone()
    old_unique = "UNIQUE (kenpo_id, member_no)" in ((ddl[0] if ddl else "") or "")
    if notnull.get("company_id") == 1 or old_unique:
        names = [r[1] for r in info if r[1] != "id"]
        defs = []
        for r in info:
            if r[1] == "id":
                defs.append("id INTEGER PRIMARY KEY AUTOINCREMENT")
                continue
            d = f"{r[1]} {r[2]}"
            if r[1] in ("kenpo_id", "member_no", "name"):
                d += " NOT NULL"
            if r[4] is not None:
                # 式の既定値は括弧で囲む必要がある
                d += f" DEFAULT ({r[4]})"
            defs.append(d)
        con.executescript(
            "PRAGMA foreign_keys=OFF;\n"
            "CREATE TABLE member_new (" + ", ".join(defs)
            + ", UNIQUE (kenpo_id, cert_mark, member_no, cert_branch));\n"
            f"INSERT INTO member_new (id, {', '.join(names)})"
            f" SELECT id, {', '.join(names)} FROM member;\n"
            "DROP TABLE member;\n"
            "ALTER TABLE member_new RENAME TO member;\n"
            "PRAGMA foreign_keys=ON;")
        if notnull.get("company_id") == 1:
            log.append("member の企業・事業所を任意に変更（あとから紐づけられるように）")
        if old_unique:
            log.append("member の一意制約を 被保険者証記号＋番号＋枝番 に変更"
                       "（本人と家族を区別できるように）")

    # ---------- 5.5 アカウントが対象とする企業（複数選択） ----------
    if "account_company" not in tables(con):
        con.execute("""
            CREATE TABLE account_company (
              account_id INTEGER NOT NULL REFERENCES account(id),
              company_id INTEGER NOT NULL REFERENCES company(id),
              PRIMARY KEY (account_id, company_id)
            )""")
        con.execute("INSERT INTO account_company (account_id, company_id)"
                    " SELECT id, company_id FROM account WHERE company_id IS NOT NULL")
        n = con.execute("SELECT COUNT(*) c FROM account_company").fetchone()["c"]
        log.append(f"account_company テーブルを作成し、既存の所属企業 {n} 件を移行")

    # ---------- 6. 操作ログ ----------
    add_col(con, "audit_log", "shell", "TEXT", log)
    add_col(con, "audit_log", "actor_role", "TEXT", log)
    add_col(con, "audit_log", "kenpo_id", "INTEGER", log)

    # ---------- 7. 採番キーの付け替え ----------
    rows = con.execute("SELECT key, value FROM setting WHERE key LIKE 'seq:department:%'").fetchall()
    for r in rows:
        newkey = r["key"].replace("seq:department:", "seq:office:")
        con.execute("INSERT INTO setting (key, value) VALUES (?,?)"
                    " ON CONFLICT(key) DO UPDATE SET value=excluded.value", (newkey, r["value"]))
        con.execute("DELETE FROM setting WHERE key=?", (r["key"],))
    if rows:
        log.append(f"採番キーを事業所用へ付け替え（{len(rows)} 件）")

    # ---------- 8. トリガーと索引 ----------
    con.executescript("""
        CREATE TRIGGER IF NOT EXISTS audit_log_no_update
        BEFORE UPDATE ON audit_log
        BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;

        CREATE TRIGGER IF NOT EXISTS audit_log_no_delete
        BEFORE DELETE ON audit_log
        BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;

        CREATE INDEX IF NOT EXISTS idx_log_ts     ON audit_log(ts);
        CREATE INDEX IF NOT EXISTS idx_log_actor  ON audit_log(actor_email);
        CREATE INDEX IF NOT EXISTS idx_log_cat    ON audit_log(category);
        CREATE INDEX IF NOT EXISTS idx_log_shell  ON audit_log(shell);
        CREATE INDEX IF NOT EXISTS idx_member_off ON member(office_id);
        CREATE INDEX IF NOT EXISTS idx_office_cmp ON office(company_id);
        CREATE INDEX IF NOT EXISTS idx_ac_company  ON account_company(company_id);
    """)
    con.commit()

    # ---------- 9. 機能制御（ロール・サブロールごとの利用可否） ----------
    if "role_feature" not in tables(con):
        con.execute("""
            CREATE TABLE role_feature (
              role_key TEXT NOT NULL,
              feature  TEXT NOT NULL,
              allowed  INTEGER NOT NULL DEFAULT 1,
              PRIMARY KEY (role_key, feature)
            )""")
        log.append("機能制御のテーブル（role_feature）を追加")
        con.commit()

    # ---------- 10. 産業医面談管理のテーブル ----------
    apply_sanmen(con, log)
    con.execute("PRAGMA foreign_keys = ON")
    ok = con.execute("PRAGMA foreign_key_check").fetchall()
    if ok:
        log.append(f"※ 整合しない参照が {len(ok)} 件あります（要確認）")
    con.close()
    if not log:
        log.append("移行は不要でした（すでに最新のスキーマです）")
    return log


def main():
    if not os.path.exists(DB):
        print("hia.db がありません。python seed.py を実行してください。")
        return
    print("=" * 62)
    print("データベースの移行")
    print("=" * 62)
    bak = DB + ".bak"
    if not os.path.exists(bak):
        import shutil
        shutil.copy2(DB, bak)
        print(f"バックアップを作成しました: {os.path.basename(bak)}")
    for line in ensure_schema(DB):
        print("  -", line)
    print("=" * 62)
    print("完了しました。サーバーを起動してください。")


if __name__ == "__main__":
    main()
