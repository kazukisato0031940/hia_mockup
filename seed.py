# -*- coding: utf-8 -*-
"""初期データを投入する。

- HIA総合管理にログインする「当社スタッフ」アカウントを1件作成する
- 動作確認用に、健康保険組合・企業・部署（事業所）・加入者と
  健保担当者・企業担当者のアカウントを投入する（samples/ のCSVと同じ内容）

サンプルを入れたくない場合は環境変数 HIA_SAMPLE=0 を指定してください。
"""
import csv
import io
import os
from datetime import datetime
import secrets
import sqlite3
import string

import hashlib
import re

from werkzeug.security import generate_password_hash

BASE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(BASE, "hia.db")
SAMPLES = os.path.join(BASE, "samples")
KENPO_NAME = os.environ.get("HIA_KENPO_NAME", "サンプル健康保険組合")
# 当社スタッフ（システム管理者）の既定値。
# 環境変数 HIA_ADMIN_EMAIL / HIA_ADMIN_PASSWORD を指定するとそちらを使います。
ADMIN_EMAIL = os.environ.get("HIA_ADMIN_EMAIL", "Jun.Ito@kusurinomadoguchi.co.jp")
ADMIN_NAME = os.environ.get("HIA_ADMIN_NAME", "伊藤 惇")
ADMIN_DEFAULT_PASSWORD = "EPARK1234567890-"
WITH_SAMPLE = os.environ.get("HIA_SAMPLE", "1") != "0"
SAMPLE_PW = os.environ.get("HIA_SAMPLE_PASSWORD", "Sample1234pass")


KANA_TRIM = ("株式会社", "有限会社", "合同会社", "健康保険組合", "組合", "（株）", "(株)")


def internal_company_code(kenpo_name, company_name):
    """当社内部コード。app.py と同じ計算式にする"""
    def slug(v):
        v = (v or "").strip()
        for t in KANA_TRIM:
            v = v.replace(t, "")
        return re.sub(r"[\s　]+", "", v)

    key = f"{slug(kenpo_name)}|{slug(company_name)}"
    return "C" + hashlib.sha1(key.encode("utf-8")).hexdigest().upper()[:7]


def gen_password(n=16):
    alpha = string.ascii_letters + string.digits
    while True:
        p = "".join(secrets.choice(alpha) for _ in range(n))
        if any(c.isdigit() for c in p) and any(c.isalpha() for c in p):
            return p


def read_sample(name):
    path = os.path.join(SAMPLES, name)
    if not os.path.exists(path):
        return []
    raw = open(path, "rb").read()
    for enc in ("cp932", "utf-8-sig", "utf-8"):
        try:
            return list(csv.DictReader(io.StringIO(raw.decode(enc))))
        except UnicodeDecodeError:
            continue
    return []


def norm_date(v):
    v = (v or "").strip()
    if not v:
        return ""
    p = v.replace("-", "/").replace(".", "/").split("/")
    if len(p) != 3:
        return ""
    return f"{int(p[0]):04d}-{int(p[1]):02d}-{int(p[2]):02d}"


def set_seq(con, kind, key, n):
    con.execute("INSERT INTO setting (key, value) VALUES (?,?)"
                " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (f"seq:{kind}:{key}", str(n)))


def load_samples(con):
    """samples/ のCSVを、画面から取り込んだのと同じ結果になるように投入する"""
    con.execute("INSERT INTO kenpo (code, name, publish_auth, kenshin_auth,"
                " guidance_auth, flu_enabled, n_hospital)"
                " VALUES ('6139166', 'ひかり健康保険組合',1,1,1,0,8)")
    kid = con.execute("SELECT id FROM kenpo WHERE code='6139166'").fetchone()["id"]
    cseq, oseq, mseq, dseq = 0, {}, 0, {}

    def get_company(name, row):
        nonlocal cseq
        cur = con.execute("SELECT id FROM company WHERE kenpo_id=? AND name=?",
                          (kid, name)).fetchone()
        if cur:
            return cur["id"]
        cseq += 1
        con.execute(
            "INSERT INTO company (kenpo_id, ext_code, code, name, kana, cert_mark, zip,"
            " tel, address, email) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (kid, (row.get("企業コード") or "").strip() or None,
             internal_company_code("ひかり健康保険組合", name),
             name, row.get("企業名（フリガナ）", ""),
             row.get("被保険者証記号", ""), row.get("郵便番号", ""),
             row.get("電話番号", ""), row.get("住所", ""),
             row.get("担当メールアドレス", "")))
        return con.execute("SELECT id FROM company WHERE kenpo_id=? AND name=?",
                           (kid, name)).fetchone()["id"]

    def add_dept(oid, name, kana=""):
        cur = con.execute("SELECT id FROM department WHERE office_id=? AND name=?",
                          (oid, name)).fetchone()
        if cur:
            return cur["id"]
        dseq[oid] = dseq.get(oid, 0) + 1
        con.execute("INSERT INTO department (office_id, ext_code, code, name, kana)"
                    " VALUES (?,?,?,?,?)",
                    (oid, str(dseq[oid]).zfill(2), str(dseq[oid]).zfill(3), name, kana))
        return con.execute("SELECT id FROM department WHERE office_id=? AND name=?",
                           (oid, name)).fetchone()["id"]

    def add_office(cid, name, kana="", zip_="", addr="", tel="", ext=None):
        cur = con.execute("SELECT id FROM office WHERE company_id=? AND name=?",
                          (cid, name)).fetchone()
        if cur:
            return cur["id"]
        oseq[cid] = oseq.get(cid, 0) + 1
        con.execute("INSERT INTO office (company_id, ext_code, code, name, kana, zip,"
                    " address, tel) VALUES (?,?,?,?,?,?,?,?)",
                    (cid, ext or None, str(oseq[cid]).zfill(3), name, kana, zip_,
                     addr, tel))
        return con.execute("SELECT id FROM office WHERE company_id=? AND name=?",
                           (cid, name)).fetchone()["id"]

    # 1) 企業＋事業所（＋各事業所に部署を2つ）
    for r in read_sample("company_sample.csv"):
        cid = get_company((r.get("企業名") or "").strip(), r)
        oid = add_office(cid, (r.get("部署名") or "").strip(),
                         r.get("部署名（フリガナ）", ""),
                         ext=(r.get("所属コード") or "").strip() or None)
        add_dept(oid, "総務部", "ソウムブ")
        add_dept(oid, "営業部", "エイギョウブ")
    # 2) 部署の追加分
    for r in read_sample("office_sample.csv"):
        c = con.execute("SELECT id FROM company WHERE kenpo_id=? AND ext_code=?",
                        (kid, (r.get("企業コード") or "").strip())).fetchone()
        if c:
            oid = add_office(c["id"], (r.get("部署名") or "").strip(),
                             r.get("部署名（フリガナ）", ""), r.get("郵便番号", ""),
                             r.get("住所", ""), r.get("電話番号", ""),
                             ext=(r.get("所属コード") or "").strip() or None)
            add_dept(oid, "総務部", "ソウムブ")
    # 3) 加入者（前半は紐づけ済み、後半は未紐づけにして紐づけページを試せるようにする）
    rows = read_sample("subscriber_sample.csv")
    for i, r in enumerate(rows):
        g = lambda k: (r.get(k) or "").strip()
        cid = oid = did = None
        if i < len(rows) - 3:      # 最後の3件は未紐づけのまま残す
            c = con.execute("SELECT id FROM company WHERE kenpo_id=? AND ext_code=?",
                            (kid, g("企業コード"))).fetchone()
            if c:
                cid = c["id"]
                o = con.execute("SELECT id FROM office WHERE company_id=? AND ext_code=?",
                                (cid, g("所属コード"))).fetchone()
                oid = o["id"] if o else None
                if oid:      # 先頭の部署に入れておく
                    d = con.execute("SELECT id FROM department WHERE office_id=?"
                                    " ORDER BY code", (oid,)).fetchone()
                    did = d["id"] if d else None
        mseq += 1
        con.execute(
            "INSERT INTO member (kenpo_id, company_id, office_id, dept_id, member_no,"
            " cert_mark, cert_branch, attr, relation, name, kana, sex, birth,"
            " qualified_at, lost_at, zip, address, address2, tel, email, employee_code,"
            " subscriber_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (kid, cid, oid, did, g("被保険者証番号"), g("被保険者証記号"), g("被保険者証枝番"),
             g("被保険者属性名"), g("続柄名称"), g("対象者氏名（漢字）"),
             g("対象者氏名（カナ）"), g("性別"), norm_date(g("生年月日")),
             norm_date(g("資格取得日（家族認定日）")), norm_date(g("資格喪失日（家族削除日）")),
             g("郵便番号"), g("住所"), g("住所（建物名）"), g("電話番号"),
             g("メールアドレス"), (g("社員番号") or g("社員コード")), str(mseq).zfill(8)))

    set_seq(con, "company", str(kid), cseq)
    for cid, n in oseq.items():
        set_seq(con, "office", str(cid), n)
    for oid, n in dseq.items():
        set_seq(con, "dept", str(oid), n)
    set_seq(con, "member", str(kid), mseq)

    # 動作確認用のアカウント
    ph = generate_password_hash(SAMPLE_PW)
    con.execute(
        "INSERT INTO account (email, name, role, view_scope, can_download,"
        " kenpo_id, status, password_hash, created_by)"
        " VALUES (?,?,'kenpo_user','kenpo_all',1,?,'active',?,'seed')",
        ("kenpo@example.local", "ひかり健保 担当", kid, ph))
    first = con.execute("SELECT id FROM company WHERE kenpo_id=? ORDER BY id",
                        (kid,)).fetchone()
    con.execute(
        "INSERT INTO account (email, name, role, view_scope, can_download,"
        " kenpo_id, company_id, status, password_hash, created_by)"
        " VALUES (?,?,'company_user','own_company',0,?,?,'active',?,'seed')",
        ("company@example.local", "光通信 担当", kid, first["id"], ph))
    aid = con.execute("SELECT id FROM account WHERE email='company@example.local'"
                      ).fetchone()["id"]
    con.execute("INSERT INTO account_company (account_id, company_id) VALUES (?,?)",
                (aid, first["id"]))
    con.execute("INSERT INTO account_scope (account_id, kind, ref_id)"
                " VALUES (?, 'company', ?)", (aid, first["id"]))
    n_all = con.execute("SELECT COUNT(*) c FROM member WHERE kenpo_id=?",
                        (kid,)).fetchone()["c"]
    n_un = con.execute("SELECT COUNT(*) c FROM member WHERE kenpo_id=?"
                       " AND company_id IS NULL", (kid,)).fetchone()["c"]
    n_off = con.execute("SELECT COUNT(*) c FROM office o JOIN company c ON c.id=o.company_id"
                        " WHERE c.kenpo_id=?", (kid,)).fetchone()["c"]
    n_dep = con.execute("SELECT COUNT(*) c FROM department d JOIN office o ON o.id=d.office_id"
                        " JOIN company c ON c.id=o.company_id"
                        " WHERE c.kenpo_id=?", (kid,)).fetchone()["c"]
    return {"kenpo": "ひかり健康保険組合", "companies": cseq, "offices": n_off,
            "departments": n_dep, "members": n_all, "unlinked": n_un}


def load_kenshin_xml(con):
    """マスタの加入者ぶんの健診XMLを作り、取込済みの状態にする（疾患予測の確認用）"""
    import sys
    base = os.path.dirname(os.path.abspath(__file__))
    sys.path.insert(0, base)
    try:
        os.environ.setdefault("HIA_SECRET_KEY", "seed")
        from app import parse_kenshin_xml
        import make_kenshin_xml
    except Exception as e:
        print(f"  健診XMLの取込はスキップしました（{e}）")
        return None

    # 登録済みの加入者ぶんのXMLを作る（マスタと必ず一致させる）
    con.commit()
    try:
        make_kenshin_xml.main()
    except Exception as e:
        print(f"  健診XMLの作成に失敗しました（{e}）")
        return None

    d = os.path.join(base, "samples", "kenshin_xml")
    if not os.path.isdir(d):
        return None
    files = sorted(f for f in os.listdir(d) if f.lower().endswith(".xml"))
    if not files:
        return None

    # 健保ごとに、被保険者証の記号・番号・枝番で突合して取込む
    idx = {}
    for m in con.execute("SELECT id, kenpo_id, cert_mark, member_no, cert_branch"
                         " FROM member"):
        idx[(m["kenpo_id"], (m["cert_mark"] or "").strip(),
             (m["member_no"] or "").strip(), (m["cert_branch"] or "").strip())] = m["id"]

    fy = str(datetime.now().year)
    syncs, total, unmatched = {}, 0, 0
    agg = {}
    for name in files:
        data, err = parse_kenshin_xml(io.open(os.path.join(d, name),
                                              encoding="utf-8").read())
        if err:
            unmatched += 1
            continue
        # ファイル名の先頭が保険者番号
        kcode = name.split("_")[0]
        k = con.execute("SELECT id FROM kenpo WHERE code=?", (kcode,)).fetchone()
        if not k:
            unmatched += 1
            continue
        kid = k["id"]
        mid = idx.get((kid, data["cert_mark"], data["member_no"], data["cert_branch"]))
        if mid is None:
            unmatched += 1
            continue
        if kid not in syncs:
            cur = con.execute(
                "INSERT INTO kenshin_sync (kenpo_id, fiscal_year, finished_at, status,"
                " fetched, imported, excluded, message, actor)"
                " VALUES (?,?,datetime('now','localtime'),'ok',0,0,0,'','seed')",
                (kid, fy))
            syncs[kid] = {"id": cur.lastrowid, "n": 0, "files": 0}
        sid = syncs[kid]["id"]
        syncs[kid]["n"] += 1
        syncs[kid]["files"] += 1
        total += 1
        for item, j2 in data["items"].items():
            con.execute(
                "INSERT INTO kenshin_result (sync_id, kenpo_id, member_id, exam_date,"
                " item, judge, detail) VALUES (?,?,?,?,?,?,?)",
                (sid, kid, mid, data["exam_date"], item, j2,
                 data["detail"].get(item, "")))
            if data["age_band"] != "年代不明" and data["sex"] in ("男", "女"):
                row = agg.setdefault((kid, data["age_band"], data["sex"], item),
                                     {"normal": 0, "caution": 0, "medical": 0})
                row[j2] += 1
    for (kid, band, sex, item), c in sorted(agg.items()):
        con.execute(
            "INSERT INTO kenshin_record (sync_id, kenpo_id, age_band, sex, item,"
            " normal, caution, medical) VALUES (?,?,?,?,?,?,?,?)",
            (syncs[kid]["id"], kid, band, sex, item,
             c["normal"], c["caution"], c["medical"]))
    for kid, v in syncs.items():
        con.execute("UPDATE kenshin_sync SET fetched=?, imported=?, message=?"
                    " WHERE id=?",
                    (v["files"], v["n"],
                     f"マスタの加入者ぶんのサンプルXML {v['files']} 件を取込済み（初期データ）。",
                     v["id"]))
    con.commit()
    if not total:
        return None
    return {"files": len(files), "members": total, "kenpos": len(syncs),
            "unmatched": unmatched}


def main():
    from migrate import ensure_schema
    for line in ensure_schema(DB):
        print("[スキーマ] " + line)
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    if con.execute("SELECT COUNT(*) c FROM account").fetchone()["c"] > 0:
        print("既にデータが存在します。初期データの投入は行いません。")
        # 当社スタッフのアカウントだけは、無ければ作る（更新した直後でも入れるように）
        cur = con.execute("SELECT id, status FROM account WHERE lower(email)=lower(?)",
                          (ADMIN_EMAIL,)).fetchone()
        if cur is None:
            pw = os.environ.get("HIA_ADMIN_PASSWORD") or ADMIN_DEFAULT_PASSWORD
            con.execute(
                "INSERT INTO account (email, name, role, view_scope, can_download,"
                " status, password_hash, created_by)"
                " VALUES (?,?,?,?,?,'active',?,'seed')",
                (ADMIN_EMAIL, ADMIN_NAME, "system_admin", "all", 1,
                 generate_password_hash(pw)))
            con.execute("INSERT INTO audit_log (shell, category, action, result,"
                        " actor_email, ip, target, detail) VALUES"
                        " ('km','account','当社スタッフを追加','success','seed','-',?,?)",
                        (ADMIN_EMAIL, "既存データに当社スタッフのアカウントを追加"))
            con.commit()
            print("=" * 68)
            print("当社スタッフのアカウントを追加しました")
            print("  ログインID   : " + ADMIN_EMAIL)
            print("  パスワード   : " + pw)
            print("  ※ログイン後に「パスワード変更」から変更してください。")
            print("=" * 68)
        else:
            print(f"当社スタッフ（{ADMIN_EMAIL}）は登録済みです"
                  f"（状態：{cur['status']}）。")
            print("パスワードが分からない場合は admin.bat で再設定できます。")
        con.close()
        return

    con.execute("INSERT INTO kenpo (code, name, publish_auth, kenshin_auth,"
                " guidance_auth, flu_enabled, n_hospital) VALUES ('K001', ?,1,1,0,1,12)",
                (KENPO_NAME,))
    kid = con.execute("SELECT id FROM kenpo WHERE code='K001'").fetchone()["id"]

    # 既定の健保にも一通りのデータを入れる
    data = {
        "サンプル商事株式会社": ["本社", "大阪支店", "仙台工場"],
        "テスト工業株式会社": ["本社", "第一工場"],
        "見本サービス株式会社": ["本社"],
    }
    cseq = n = 0
    for cn, offices in data.items():
        cseq += 1
        ccode = str(cseq).zfill(4)
        con.execute("INSERT INTO company (kenpo_id, ext_code, code, name, tel)"
                    " VALUES (?,?,?,?,?)",
                    (kid, ccode, internal_company_code(KENPO_NAME, cn), cn,
                     "03-0000-0000"))
        cid = con.execute("SELECT id FROM company WHERE kenpo_id=? AND ext_code=?",
                          (kid, ccode)).fetchone()["id"]
        oseq = 0
        for on in offices:
            oseq += 1
            con.execute("INSERT INTO office (company_id, ext_code, code, name)"
                        " VALUES (?,?,?,?)",
                        (cid, str(oseq).zfill(3), str(oseq).zfill(3), on))
            oid = con.execute("SELECT id FROM office WHERE company_id=? AND name=?",
                              (cid, on)).fetchone()["id"]
            con.execute("INSERT INTO department (office_id, ext_code, code, name, kana)"
                        " VALUES (?,'01','001','総務部','ソウムブ')", (oid,))
            did = con.execute("SELECT id FROM department WHERE office_id=?",
                              (oid,)).fetchone()["id"]
            set_seq(con, "dept", str(oid), 1)
            for _ in range(5):
                n += 1
                con.execute(
                    "INSERT INTO member (kenpo_id, company_id, office_id, dept_id,"
                    " member_no, name, kana, birth, sex, cert_mark, cert_branch,"
                    " relation, subscriber_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (kid, cid, oid, did, str(100000 + n), f"見本 {n:03d}", "ミホン",
                     # 年代がばらけるように生年を振り分ける（20代〜70代）
                     f"{1950 + (n * 7) % 50}-{(n % 12) + 1:02d}-{(n % 27) + 1:02d}",
                     "男" if n % 2 else "女", "9000", "0", "本人",
                     str(n).zfill(8)))
        set_seq(con, "office", str(cid), oseq)
    set_seq(con, "company", str(kid), cseq)
    set_seq(con, "member", str(kid), n)

    sample = load_samples(con) if WITH_SAMPLE else None
    kenshin = load_kenshin_xml(con) if WITH_SAMPLE else None

    pw = os.environ.get("HIA_ADMIN_PASSWORD") or ADMIN_DEFAULT_PASSWORD
    con.execute(
        "INSERT INTO account (email, name, role, view_scope, can_download,"
        " status, password_hash, created_by) VALUES (?,?,?,?,?,'active',?,'seed')",
        (ADMIN_EMAIL, ADMIN_NAME, "system_admin", "all", 1,
         generate_password_hash(pw)))
    con.execute("INSERT INTO audit_log (shell, category, action, result, actor_email, ip,"
                " target, detail) VALUES ('km','master','初期データを投入','success','seed',"
                " '-', ?, ?)", (KENPO_NAME, f"企業{cseq}社・加入者{n}件・当社スタッフ1件"))
    con.commit()
    con.close()

    print("=" * 68)
    print("初期データを投入しました")
    print(f"  健康保険組合 : {KENPO_NAME}（企業{cseq}社・事業所6件・加入者{n}件）")
    if sample:
        print(f"  動作確認用   : {sample['kenpo']}"
              f"（企業{sample['companies']}社・事業所{sample['offices']}件・"
              f"部署{sample['departments']}件・加入者{sample['members']}件）")
        print(f"                 うち{sample['unlinked']}件は未紐づけ"
              f"（「企業・部署の紐づけ」で試せます）")
    if kenshin:
        print(f"  健診結果     : マスタの加入者ぶんのXML {kenshin['files']}件を作成し、"
              f"{kenshin['kenpos']}健保・{kenshin['members']}名を取込済み")
        print("                 疾患予測の「予測を実行する」でそのまま試せます")
    print("-" * 68)
    print("■ 当社スタッフ（HIA総合管理）")
    print("  ログインID   : " + ADMIN_EMAIL)
    print("  パスワード   : " + pw)
    if pw == ADMIN_DEFAULT_PASSWORD:
        print("  ※既定のパスワードです。ログイン後に「パスワード変更」から変更してください。")
    else:
        print("  ※このパスワードは再表示できません。今すぐ控えてください。")
    if sample:
        print("-" * 68)
        print("■ 動作確認用アカウント（HIA健保管理）")
        print(f"  健保担当者   : kenpo@example.local   / {SAMPLE_PW}")
        print(f"  企業担当者   : company@example.local / {SAMPLE_PW}")
        print("  ※本番で使う前に、この2件は削除してください。")
        print("  ※サンプルを入れずに始める場合は HIA_SAMPLE=0 を指定してください。")
    print("=" * 68)


if __name__ == "__main__":
    main()
