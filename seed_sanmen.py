# -*- coding: utf-8 -*-
"""産業医面談管理の動作確認用データを投入する。

  1. 企業担当者＋サブロール（産業医・人事）のアカウントを作成する
  2. 加入者に社員コード・深夜業従事区分を割り当てる
  3. 健診結果（総合判定・明細・前年分）、月次の労働時間、ストレスチェックを投入する
  4. 配信テンプレートとストレスチェック設問を投入する

投入するデータはすべて架空のもので、実在の個人情報は含みません。
何度実行しても同じ結果になります（冪等）。

使い方:
  python seed_sanmen.py
"""
import os
import random
import sqlite3
from datetime import date, timedelta

from werkzeug.security import generate_password_hash

BASE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(BASE, "hia.db")
SAMPLE_PW = os.environ.get("HIA_SAMPLE_PASSWORD", "Sample1234pass")

DOCTOR_EMAIL = "doctor@example.local"
HR_EMAIL = "hr@example.local"

# 検査項目（値の範囲・単位・判定のしきい値）。判定は学会区分 A〜E で持つ。
ITEMS = [
    ("収縮期血圧", "mmHg", (105, 175), [(120, "A"), (130, "B"), (140, "C"), (160, "D")]),
    ("拡張期血圧", "mmHg", (62, 108), [(80, "A"), (85, "B"), (90, "C"), (100, "D")]),
    ("HbA1c", "％", (50, 82), [(56, "A"), (60, "B"), (65, "C"), (70, "D")]),   # 値は1/10
    ("LDLコレステロール", "mg/dL", (85, 190), [(120, "A"), (140, "B"), (160, "C"), (180, "D")]),
    ("中性脂肪", "mg/dL", (60, 320), [(150, "A"), (200, "B"), (300, "C"), (400, "D")]),
    ("AST（GOT）", "U/L", (14, 72), [(31, "A"), (40, "B"), (51, "C"), (61, "D")]),
    ("γ-GT（γ-GTP）", "U/L", (16, 160), [(51, "A"), (80, "B"), (101, "C"), (150, "D")]),
    ("BMI", "kg/m2", (185, 320), [(250, "A"), (270, "B"), (300, "C"), (350, "D")]),  # 値は1/10
]


def judge_of(v, steps):
    for limit, j in steps:
        if v < limit:
            return j
    return "E"


def value_for(rnd, lo, hi, steps, judge):
    """指定した判定になる検査値を作る"""
    bands = {}
    prev = lo
    for limit, j in steps:
        bands[j] = (prev, max(prev, limit - 1))
        prev = limit
    bands["E"] = (prev, max(prev + 5, hi))
    a, b = bands.get(judge, (lo, hi))
    return rnd.randint(int(a), int(max(a, b)))


def fmt(name, v):
    return f"{v / 10:.1f}" if name in ("HbA1c", "BMI") else str(v)


def worst(js):
    order = ["A", "B", "C", "D", "E"]
    return max(js, key=lambda x: order.index(x)) if js else "A"


def age_ok(birth, today):
    """18歳以上かどうか（学生・子どもの被扶養者に健診データを作らないため）"""
    if not birth:
        return True
    try:
        y, mo, d = (int(x) for x in str(birth)[:10].split("-"))
    except ValueError:
        return True
    age = today.year - y - ((today.month, today.day) < (mo, d))
    return age >= 18


def fiscal_year(d):
    return d.year - 1 if d.month <= 3 else d.year


def main():
    if not os.path.exists(DB):
        print("hia.db がありません。先に python seed.py を実行してください。")
        return
    from migrate import ensure_schema
    for line in ensure_schema(DB):
        print("  -", line)

    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    rnd = random.Random(20260901)      # 毎回同じデータになるよう固定する

    kenpo = con.execute("SELECT * FROM kenpo ORDER BY id LIMIT 1").fetchone()
    if not kenpo:
        print("健康保険組合が登録されていません。python seed.py を実行してください。")
        return
    comps = con.execute("SELECT * FROM company WHERE kenpo_id=? ORDER BY id",
                        (kenpo["id"],)).fetchall()

    # ---------- 1. 産業医・人事のアカウント ----------
    # 産業医・人事は「企業担当者＋サブロール」で作る
    ph = generate_password_hash(SAMPLE_PW)
    for email, name, srole in ((DOCTOR_EMAIL, "田中 一郎", "doctor"),
                               (HR_EMAIL, "佐藤 花子", "hr")):
        row = con.execute("SELECT * FROM account WHERE lower(email)=lower(?)",
                          (email,)).fetchone()
        if row:
            con.execute("UPDATE account SET role='company_user', sub_role=?,"
                        " status='active', password_hash=?, view_scope='own_company',"
                        " kenpo_id=? WHERE id=?",
                        (srole, ph, kenpo["id"], row["id"]))
            aid = row["id"]
        else:
            cur = con.execute(
                "INSERT INTO account (email, name, role, sub_role, view_scope,"
                " can_download, kenpo_id, status, password_hash, created_by)"
                " VALUES (?,?, 'company_user', ?, 'own_company', 1, ?, 'active', ?,"
                " 'seed_sanmen')",
                (email, name, srole, kenpo["id"], ph))
            aid = cur.lastrowid
        # 担当範囲＝この健保のすべての企業
        con.execute("DELETE FROM account_scope WHERE account_id=?", (aid,))
        con.execute("DELETE FROM account_company WHERE account_id=?", (aid,))
        for c in comps:
            con.execute("INSERT OR IGNORE INTO account_scope (account_id, kind, ref_id)"
                        " VALUES (?, 'company', ?)", (aid, c["id"]))
            con.execute("INSERT OR IGNORE INTO account_company (account_id, company_id)"
                        " VALUES (?,?)", (aid, c["id"]))
    con.commit()

    # ---------- 2-1. 健診・労働時間・ストレスチェックを見るための人数を確保する ----------
    # 健保ごとに従業員（本人）が少ないと、集計（受診率・判定の内訳・時間外の該当者・
    # 高ストレス者）がほとんど出ません。20名に足りない健保には架空の従業員を足します。
    n_add = 0
    for kp in con.execute("SELECT * FROM kenpo ORDER BY id").fetchall():
        n_own = con.execute(
            "SELECT COUNT(*) c FROM member WHERE kenpo_id=?"
            " AND (relation IS NULL OR relation='本人')", (kp["id"],)).fetchone()["c"]
        if n_own >= 20:
            continue
        places = con.execute(
            "SELECT c.id company_id, o.id office_id, d.id dept_id FROM company c"
            " LEFT JOIN office o ON o.company_id=c.id"
            " LEFT JOIN department d ON d.office_id=o.id"
            " WHERE c.kenpo_id=? ORDER BY c.id, o.id, d.id", (kp["id"],)).fetchall()
        if not places:
            continue
        seq = con.execute("SELECT COUNT(*) c FROM member WHERE kenpo_id=?",
                          (kp["id"],)).fetchone()["c"]
        for j in range(20 - n_own):
            seq += 1
            pl = places[j % len(places)]
            code = f"S{j + 101:04d}"
            # 氏名は架空であることが分かる形にします（実在の個人情報は入れません）
            name = f"見本 {j + 101:03d}"
            birth = date(1962 + (j * 17) % 36, 1 + (j * 5) % 12, 1 + (j * 7) % 27)
            # 加入者ID は本システムの採番と同じ形（8桁）で入れる
            sk = f"seq:member:{kp['id']}"
            row = con.execute("SELECT value FROM setting WHERE key=?", (sk,)).fetchone()
            n_seq = (int(row["value"]) if row else 0) + 1
            con.execute("INSERT INTO setting (key, value) VALUES (?,?)"
                        " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                        (sk, str(n_seq)))
            con.execute(
                "INSERT INTO member (kenpo_id, company_id, office_id, dept_id, member_no,"
                " attr, relation, name, kana, sex, birth, qualified_at, email,"
                " employee_code, subscriber_id, night_work, excluded)"
                " VALUES (?,?,?,?,?, '一般', '本人', ?,?,?,?,?,?,?,?,?,0)",
                (kp["id"], pl["company_id"], pl["office_id"], pl["dept_id"],
                 f"{9000 + seq}", name, f"ミホン{j + 101:03d}",
                 "1" if j % 2 else "2", birth.isoformat(),
                 date(fiscal_year(date.today()) - 3, 4, 1).isoformat(),
                 f"sample+{kp['id']}{code}@example.local", code,
                 str(n_seq).zfill(8), 1 if j % 7 == 0 else 0))
            n_add += 1
    con.commit()

    # ---------- 2-2. 加入者の社員コード・深夜業従事区分 ----------
    # 健診・労働時間・ストレスチェックは「従業員（本人）」に対して登録します。
    members = con.execute("SELECT * FROM member ORDER BY kenpo_id, id").fetchall()
    if not members:
        print("加入者が登録されていません。python seed.py を実行してください。")
        return
    for i, m in enumerate(members, start=1):
        code = m["employee_code"] or f"E{i:04d}"
        night = m["night_work"] or (1 if i % 7 == 0 else 0)
        # 架空の連絡先（example.local は試験用ドメインで実際には届きません）
        email = m["email"] or f"sample+{code}@example.local"
        con.execute("UPDATE member SET employee_code=?, night_work=?, email=? WHERE id=?",
                    (code, night, email, m["id"]))
    con.commit()

    today = date.today()
    fy = fiscal_year(today)
    n_ken = n_ot = n_st = 0

    # 被扶養者（家族）は労働安全衛生法の健診・面談の対象ではないため除きます
    members = [m for m in members
               if (m["relation"] or "本人") == "本人" and age_ok(m["birth"], today)]

    for i, m in enumerate(members, start=1):
        # ---------- 3-1. 健診結果（今年度・前年度） ----------
        cur_values = {}
        for y_off in (0, 1):
            y = fy - y_off
            # 8割の方が受診済み（未受診の方も残して受診管理の確認ができるようにする）
            if (i + y_off) % 5 == 0:
                continue
            # 受診日は年度内（4月〜翌1月）にばらして、月次推移が見えるようにする
            exam = date(y, 4, 15) + timedelta(days=(i * 37) % 285)
            vals, judges = [], []
            if y_off and cur_values:
                # 前年は今年の値を少し動かして作る（前年比較が自然になる）
                for name, unit, (lo, hi), steps in ITEMS:
                    v = max(lo, int(cur_values[name] * rnd.uniform(0.88, 1.08)))
                    j = judge_of(v, steps)
                    vals.append((name, fmt(name, v), unit, j))
                    judges.append(j)
            else:
                # 総合判定の分布を実際に近づける（A・Bが多く、D・Eは少数）
                total = rnd.choices(["A", "B", "C", "D", "E"],
                                    weights=[36, 32, 20, 9, 3])[0]
                if i % 17 == 0:
                    total = "E"      # 治療中の方も確認できるようにする
                # 1〜2項目だけがその判定になるように検査値を作る
                idx = set(rnd.sample(range(len(ITEMS)),
                                     1 if total in ("A", "B") else 2))
                for n_i, (name, unit, (lo, hi), steps) in enumerate(ITEMS):
                    if total in ("A", "B"):
                        j = total if n_i in idx else "A"
                    else:
                        j = total if n_i in idx else rnd.choices(["A", "B"],
                                                                weights=[7, 3])[0]
                    v = value_for(rnd, lo, hi, steps, j)
                    if not y_off:
                        cur_values[name] = v
                    vals.append((name, fmt(name, v), unit, judge_of(v, steps)))
                    judges.append(judge_of(v, steps))
            total = worst(judges)
            findings = {
                "A": "", "B": "生活習慣の改善を継続してください。",
                "C": "血圧・脂質について再検査が必要です。生活改善の指導を行います。",
                "D": "要精密検査。医療機関の受診状況を面談で確認します。",
                "E": "治療中。主治医の指示内容を面談で確認します。",
            }[total]
            con.execute(
                "INSERT INTO oh_kenshin (member_id, fiscal_year, kind, exam_date, judge,"
                " findings, source, updated_at) VALUES (?,?, '定期', ?,?,?, 'seed',"
                " datetime('now','localtime'))"
                " ON CONFLICT(member_id, fiscal_year, kind) DO UPDATE SET"
                " exam_date=excluded.exam_date, judge=excluded.judge,"
                " findings=excluded.findings",
                (m["id"], str(y), exam.isoformat(), total, findings))
            kid = con.execute("SELECT id FROM oh_kenshin WHERE member_id=? AND"
                              " fiscal_year=? AND kind='定期'",
                              (m["id"], str(y))).fetchone()["id"]
            con.execute("DELETE FROM oh_kenshin_item WHERE kenshin_id=?", (kid,))
            for s, (name, value, unit, j) in enumerate(vals):
                con.execute("INSERT INTO oh_kenshin_item (kenshin_id, name, value, unit,"
                            " judge, sort) VALUES (?,?,?,?,?,?)",
                            (kid, name, value, unit, j, s))
            if not y_off:
                n_ken += 1

        # 深夜健診（深夜業従事の方は6か月ごと）
        if m["night_work"] or i % 7 == 0:
            if i % 3:
                exam = date(fy, 10, 1) + timedelta(days=(i * 5) % 60)
                con.execute(
                    "INSERT INTO oh_kenshin (member_id, fiscal_year, kind, exam_date,"
                    " judge, source, updated_at) VALUES (?,?, '深夜', ?,?, 'seed',"
                    " datetime('now','localtime'))"
                    " ON CONFLICT(member_id, fiscal_year, kind) DO UPDATE SET"
                    " exam_date=excluded.exam_date, judge=excluded.judge",
                    (m["id"], str(fy), exam.isoformat(),
                     rnd.choice(["A", "B", "B", "C"])))

        # ---------- 3-2. 月次の労働時間 ----------
        base = rnd.randint(8, 45)
        for k in range(12):
            y2 = fy + (0 if 4 + k <= 12 else 1)
            mo = 4 + k if 4 + k <= 12 else 4 + k - 12
            if date(y2, mo, 1) > today:
                break
            h = base + rnd.randint(-6, 18)
            # 1割の方に繁忙月を作る（面談候補の抽出を確認するため）
            if i % 9 == 0 and k in (2, 3):
                h = rnd.randint(82, 118)
            elif i % 13 == 0 and k == 5:
                h = rnd.randint(81, 95)
            con.execute("INSERT INTO oh_overtime (member_id, ym, hours, holiday_hours)"
                        " VALUES (?,?,?,?) ON CONFLICT(member_id, ym) DO UPDATE SET"
                        " hours=excluded.hours, holiday_hours=excluded.holiday_hours",
                        (m["id"], f"{y2}-{mo:02d}", float(h),
                         float(rnd.randint(0, 8) if h > 60 else 0)))
            n_ot += 1

        # ---------- 3-3. ストレスチェック ----------
        if i % 4:      # 7割強が受検済み
            score = rnd.randint(30, 88)
            high = 1 if score >= 76 else 0
            con.execute(
                "INSERT INTO oh_stress (member_id, fiscal_year, exam_date, score, high,"
                " applied) VALUES (?,?,?,?,?,?)"
                " ON CONFLICT(member_id, fiscal_year) DO UPDATE SET"
                " exam_date=excluded.exam_date, score=excluded.score,"
                " high=excluded.high, applied=excluded.applied",
                (m["id"], str(fy), date(fy, 9, 10).isoformat(), score, high,
                 1 if (high and i % 3 == 0) else 0))
            n_st += 1
    con.commit()

    # ---------- 4. 配信テンプレート・ストレスチェック設問 ----------
    if not con.execute("SELECT COUNT(*) c FROM oh_mail_template").fetchone()["c"]:
        import sanmen
        for kind, name, subject, body in sanmen.DEFAULT_TEMPLATES:
            con.execute("INSERT INTO oh_mail_template (kind, name, subject, body)"
                        " VALUES (?,?,?,?)", (kind, name, subject, body))
    if not con.execute("SELECT COUNT(*) c FROM oh_sc_question").fetchone()["c"]:
        qs = [
            ("仕事のストレス要因", 1, "非常にたくさんの仕事をしなければならない"),
            ("仕事のストレス要因", 2, "時間内に仕事が処理しきれない"),
            ("仕事のストレス要因", 3, "自分のペースで仕事ができる"),
            ("心身のストレス反応", 4, "ひどく疲れた"),
            ("心身のストレス反応", 5, "よく眠れない"),
            ("周囲のサポート", 6, "上司はどのくらい気軽に話ができますか"),
            ("周囲のサポート", 7, "職場の同僚はどのくらい頼りになりますか"),
        ]
        for cat, no, body in qs:
            con.execute("INSERT INTO oh_sc_question (category, no, body) VALUES (?,?,?)",
                        (cat, no, body))

    # ---------- 5. 配信ログのサンプル（操作ログ管理の「配信ログ」に出ます） ----------
    if not con.execute("SELECT COUNT(*) c FROM oh_mail_log").fetchone()["c"]:
        tpl = {r["kind"]: r for r in con.execute(
            "SELECT * FROM oh_mail_template ORDER BY kind, id")}
        mem = [r["id"] for r in con.execute(
            "SELECT id FROM member ORDER BY id LIMIT 12")]
        # 1回の配信＝同じ日時・種別・テンプレートで複数名ぶんの記録が残ります
        plan = [("面談受診勧奨", f"{fy}-08-20 09:15", 8, 1, 0),
                ("ストレスチェック案内", f"{fy}-09-01 09:20", 10, 0, 2)]
        for kind, sent_at, n_ok, n_ng, n_skip in plan:
            t = tpl.get(kind)
            rows = []
            for i in range(n_ok + n_ng + n_skip):
                result = "success" if i < n_ok else \
                    ("failure" if i < n_ok + n_ng else "skipped")
                detail = {"success": "メールで送付",
                          "failure": "宛先不明で戻ってきました",
                          "skipped": "対象外のため送信しません"}[result]
                rows.append((kind, t["id"] if t else None,
                             mem[i % len(mem)] if mem else None,
                             t["subject"] if t else kind, sent_at,
                             "hr@example.local", result, detail))
            con.executemany(
                "INSERT INTO oh_mail_log (kind, template_id, member_id, subject,"
                " sent_at, actor, result, detail) VALUES (?,?,?,?,?,?,?,?)", rows)
    con.commit()

    # ---------- 6. 面談ワークフローの進み具合のサンプル ----------
    # ダッシュボードの月次推移・ステータス内訳が確認できるように、
    # 面談候補のうち何名かを「承認済」「勧奨メール送信済」「面談完了」まで進めておきます。
    # （面談候補の抽出そのものは画面表示時に自動で作られます）
    n_iv = 0
    prev_fy_str = str(fy - 1)
    if not con.execute("SELECT COUNT(*) c FROM oh_candidate").fetchone()["c"]:
        # 面談候補になる人（健診C以上・時間外80時間超・高ストレスのいずれか）
        cand_ids = [r["id"] for r in con.execute(
            "SELECT DISTINCT m.id FROM member m"
            " LEFT JOIN oh_kenshin k ON k.member_id=m.id AND k.fiscal_year=?"
            " LEFT JOIN oh_stress s  ON s.member_id=m.id AND s.fiscal_year=?"
            " WHERE k.judge IN ('C','D','E') OR s.high=1"
            "    OR EXISTS (SELECT 1 FROM oh_overtime o WHERE o.member_id=m.id"
            "               AND o.ym LIKE ? AND o.hours > 80)"
            " ORDER BY m.id", (str(fy), str(fy), f"{fy}-%"))]
        # 面談完了（実施月をばらして月次推移が見えるようにします）
        done_plan = [(5, "対面"), (6, "オンライン"), (7, "対面"), (9, "対面"),
                     (10, "オンライン"), (11, "対面"), (12, "電話")]
        # 「産業医確認待ち」を挟み、どの担当範囲で見ても各ステータスが並ぶようにします。
        # None は画面表示時に「産業医確認待ち」で自動作成させます。
        cycle = ["面談完了", None, "面談対象（承認済）", "面談完了", None,
                 "勧奨メール送信済", "面談予約済", None, "面談完了",
                 "面談対象（承認済）", None]
        n_done = 0
        for pos, mid in enumerate(cand_ids):
            status = cycle[pos % len(cycle)]
            if not status:
                continue
            di = None
            if status == "面談完了":
                di = (n_done + pos) % len(done_plan)
                n_done += 1
            wc = "就業制限" if status == "面談完了" and pos % 3 == 0 else "通常勤務"
            mailed = status in ("勧奨メール送信済", "面談予約済", "面談完了")
            con.execute(
                "INSERT INTO oh_candidate (member_id, fiscal_year, reasons, status,"
                " work_class, hr_class, approved_by, approved_at, mail_count,"
                " last_mail_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (mid, str(fy), "", status, wc,
                 "経過観察" if status == "面談完了" else "未判定",
                 "山田 一郎", f"{fy}-07-01 10:00", 1 if mailed else 0,
                 f"{fy}-08-20 09:15" if mailed else None, f"{fy}-08-20 09:15"))
            if status == "面談完了" and di is not None:
                mo, method = done_plan[di]
                y = fy if mo >= 4 else fy + 1
                con.execute(
                    "INSERT INTO oh_interview (member_id, fiscal_year, met_on, method,"
                    " purpose, work_class, measure, findings, next_plan, overtime,"
                    " doctor) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (mid, str(fy), date(y, mo, 10 + (di % 15)).isoformat(), method,
                     "健診有所見", wc,
                     "時間外労働を月45時間以内に制限" if wc == "就業制限"
                     else "生活習慣の改善を指導",
                     "所見にもとづき経過を確認します。", date(y, mo, 20).isoformat(),
                     float(60 + di * 5), "山田 一郎"))
                n_iv += 1
                # 前年度の記録も入れて、月次推移の「前年度比較」が見えるようにする
                if di % 2 == 0:
                    pmo = 5 + (di % 6)
                    con.execute(
                        "INSERT OR IGNORE INTO oh_interview (member_id, fiscal_year,"
                        " met_on, method, purpose, work_class, measure, findings,"
                        " overtime, doctor) VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (mid, prev_fy_str, date(fy - 1, pmo, 12).isoformat(), method,
                         "健診有所見", "通常勤務", "生活習慣の改善を指導",
                         "前年度の面談記録です。", float(55 + di * 4), "山田 一郎"))
                    n_iv += 1
    con.commit()
    con.close()

    print("=" * 62)
    print("産業医面談管理の確認用データを投入しました（すべて架空のデータです）")
    print("=" * 62)
    print(f"  対象年度      : {fy}年度")
    print(f"  加入者        : {len(members)}名")
    print(f"  健診結果      : {n_ken}件（前年分も投入）")
    print(f"  労働時間      : {n_ot}件")
    print(f"  ストレスチェック: {n_st}件")
    print(f"  面談記録      : {n_iv}件（ダッシュボードの月次推移の確認用）")
    print("  ログイン用アカウント（いずれも 企業担当者＋サブロール）")
    print(f"    産業医 : {DOCTOR_EMAIL} / {SAMPLE_PW}")
    print(f"    人事   : {HR_EMAIL} / {SAMPLE_PW}")


if __name__ == "__main__":
    main()
