# -*- coding: utf-8 -*-
"""
産業医面談管理（別システムの機能を本システムへ組み込んだモジュール）

労働安全衛生法に基づく「健康診断」「長時間労働」「ストレスチェック」の情報を横断し、
産業医面談の対象者抽出から面談記録・労基署報告までを支援する。

  A．認証・共通基盤        本体（app.py）の認証・ロール・操作ログをそのまま使う
  B．対象者スクリーニング  /oh              面談候補の自動抽出と確認・処理
  C．健診受診管理          /oh/kenshin      定期健診・深夜健診の受診状況
  D．メール配信            /oh/mail         受診勧奨・ストレスチェック案内
  E．面談結果入力          /oh/interview    面談記録（産業医のみ）
  F．労基署報告            /oh/report       様式第6号・ストレスチェック結果等報告書
  G．データ取込            /oh/upload       労働時間・ストレスチェック・健診結果

ロール
  産業医（doctor）… 医学的判断（面談対象の承認・就業区分の判定・面談所見・報告書の記名）
  人事  （hr）    … 運用事務（取込・マスタ・メール配信・対応区分・報告書の出力）
  当社スタッフ・健保担当者・企業担当者は「人事」と同じ運用事務の範囲で操作できる。
  医学的判断の項目は、人事側から送信されてもサーバ側で無視する。

※ 医薬品・疾患・治療に関する表示は参考情報であり、就業判定・措置の最終判断は
   産業医・医師が行う前提の画面構成にしている。
"""
import csv
import io
import re
from datetime import date, datetime
from functools import wraps

from flask import (Blueprint, Response, flash, redirect, render_template,
                   request, url_for)

bp = Blueprint("oh", __name__, url_prefix="/oh")

# 本体（app.py）のモジュール。init_app で受け取る。
H = None


def init_app(flask_app, app_module):
    """app.py から呼び出して、共通処理を受け取りブループリントを登録する"""
    global H
    H = app_module
    flask_app.register_blueprint(bp)
    return bp


# ================================================================ 区分の定義
# 総合判定は日本人間ドック・予防医療学会の判定区分（2026年4月1日改定）に準拠する
JUDGES = ["A", "B", "C", "D", "E"]
JUDGE_LABEL = {"A": "異常なし", "B": "軽度異常", "C": "要再検査・生活改善",
               "D": "要精密検査・治療", "E": "治療中"}
FINDING_JUDGES = ("C", "D", "E")          # 有所見（C判定以上）

WORK_CLASSES = ["通常勤務", "就業制限", "要休業"]
HR_CLASSES = ["未判定", "産業医判定済", "要精査・加療指示", "保健師対応中", "再検査対応指示"]

ST_WAIT = "産業医確認待ち"
ST_APPROVED = "面談対象（承認済）"
ST_MAILED = "勧奨メール送信済"
ST_BOOKED = "面談予約済"
ST_DONE = "面談完了"
ST_OUT = "対象外"
STATUSES = [ST_WAIT, ST_APPROVED, ST_MAILED, ST_BOOKED, ST_DONE, ST_OUT]

KINDS = ["定期", "深夜"]
MAIL_KINDS = ["面談受診勧奨", "ストレスチェック案内"]
METHODS = ["対面", "オンライン", "電話"]
PURPOSES = ["健診有所見", "長時間労働", "高ストレス", "本人からの申出", "その他"]

OT_LIMIT = 80        # 面談候補として抽出する時間外労働（月）
OT_LIMIT2 = 100      # 医師の面接指導が特に必要な水準

# 一覧の表示列（A-06 表示列カスタマイズ）
LIST_COLS = [("emp", "社員ID"), ("name", "氏名"), ("dept", "部署"), ("age", "年齢"),
             ("judge", "健診判定"), ("reason", "抽出理由"), ("overtime", "時間外"),
             ("stress", "ストレス"), ("memo", "メモ"), ("status", "ステータス")]
LIST_COL_KEYS = [k for k, _ in LIST_COLS]

# 旧表記のデータを学会区分へ自動でマッピングする（B-04）
JUDGE_MAP = {
    "a": "A", "b": "B", "c": "C", "d": "D", "e": "E",
    "normal": "A", "基準内": "A", "異常なし": "A", "異常無し": "A", "1": "A",
    "caution": "C", "要注意": "C", "軽度異常": "B", "経過観察": "B", "2": "B",
    "要再検査": "C", "要再検": "C", "生活改善": "C", "要指導": "C", "3": "C",
    "medical": "D", "要医療": "D", "要精密検査": "D", "要精検": "D", "要治療": "D", "4": "D",
    "治療中": "E", "医療中": "E", "5": "E",
}


def norm_judge(v):
    """判定の表記を学会区分（A〜E）にそろえる。判別できなければ None"""
    v = (v or "").strip()
    if not v:
        return None
    if v in JUDGES:
        return v
    key = v.lower()
    if key in JUDGE_MAP:
        return JUDGE_MAP[key]
    for k, mapped in JUDGE_MAP.items():
        if k and k in key:
            return mapped
    return None


def worst_judge(vals):
    """複数の判定のうち、いちばん重いものを返す"""
    best = None
    for v in vals:
        j = norm_judge(v)
        if j and (best is None or JUDGES.index(j) > JUDGES.index(best)):
            best = j
    return best


# ================================================================ 年度
def fy_of(d):
    """日付（YYYY-MM-DD）から年度（4月〜翌3月）を求める"""
    m = re.match(r"(\d{4})-(\d{2})", (d or "").replace("/", "-"))
    if not m:
        return None
    y, mo = int(m.group(1)), int(m.group(2))
    return str(y - 1 if mo <= 3 else y)


def current_fy():
    t = date.today()
    return str(t.year - 1 if t.month <= 3 else t.year)


def fy_span(fy):
    """年度の開始年月・終了年月（2026 → 2026-04, 2027-03）"""
    y = int(fy)
    return f"{y}-04", f"{y + 1}-03"


def fy_choices(n=5):
    cur = int(current_fy())
    return [str(cur - i) for i in range(n)]


def age_of(birth, at=None):
    m = re.match(r"(\d{4})-(\d{1,2})-(\d{1,2})", (birth or "").replace("/", "-"))
    if not m:
        return None
    b = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    t = at or date.today()
    return t.year - b.year - ((t.month, t.day) < (b.month, b.day))


# ================================================================ 権限
def login_required(fn):
    @wraps(fn)
    def wrapper(*a, **kw):
        if not H.current_account():
            return redirect(url_for("login", next=request.path))
        return fn(*a, **kw)
    return wrapper


def is_doctor(acc=None):
    """医学的判断（承認・就業区分・面談記録・記名）ができるか。
    企業担当者のサブロールが「産業医（doctor）」のアカウントだけが行える。"""
    acc = acc or H.current_account()
    return bool(acc) and H.sub_role(acc) == "doctor"


def is_ops(acc=None):
    """運用事務（取込・メール配信・対応区分・報告書の出力）ができるか。
    産業医以外のアカウント（人事・企業担当者・健保担当者・当社スタッフ）が行える。"""
    acc = acc or H.current_account()
    return bool(acc) and not is_doctor(acc)


def need(key):
    """機能制御（ロール・サブロールごとの利用可否）で画面・操作を制限する。
    医学的判断（oh.approve／oh.interview／oh.sign）は設定で変更できず、産業医のみ。"""
    def deco(fn):
        @wraps(fn)
        def wrapper(*a, **kw):
            acc = H.current_account()
            if not acc:
                return redirect(url_for("login", next=request.path))
            if not H.feature_allowed(key, acc):
                return H.deny_feature(key)
            return fn(*a, **kw)
        return wrapper
    return deco


# 医学的判断の操作（産業医のみ・設定で変更できない）
def doctor_only(fn):
    return need("oh.approve")(fn)


# 運用事務の操作（既定では産業医以外）
def ops_only(fn):
    return need("oh.upload")(fn)


def actor():
    acc = H.current_account()
    return acc["name"] if acc else "—"


def role_label():
    """画面・メモ・報告書に出す肩書き（サブロールがあればそれを使う）"""
    acc = H.current_account()
    if not acc:
        return ""
    s = H.sub_role(acc)
    if s:
        return H.SUB_ROLE_LABELS[s]
    return H.ROLE_LABELS.get(acc["role"], acc["role"])


# ================================================================ 共通の取得処理
def chunks(seq, n=400):
    seq = list(seq)
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def scoped_members(acc, f=None):
    """閲覧できる加入者を、氏名・部署・企業で絞り込んで取得する"""
    db = H.get_db()
    where, params = H.member_where(acc)
    sql = ("SELECT m.id, m.name, m.kana, m.birth, m.sex, m.email, m.employee_code,"
           " m.member_no, m.night_work, m.kenpo_id,"
           " c.name AS company_name, o.name AS office_name, d.name AS dept_name"
           " FROM member m"
           " LEFT JOIN company c ON c.id=m.company_id"
           " LEFT JOIN office o ON o.id=m.office_id"
           " LEFT JOIN department d ON d.id=m.dept_id"
           " WHERE " + where)
    p = list(params)
    f = f or {}
    if f.get("name"):
        sql += " AND (m.name LIKE ? OR m.kana LIKE ? OR IFNULL(m.employee_code,'') LIKE ?)"
        p += [f"%{f['name']}%"] * 3
    if f.get("dept"):
        sql += " AND (IFNULL(d.name,'') LIKE ? OR IFNULL(o.name,'') LIKE ?)"
        p += [f"%{f['dept']}%"] * 2
    if f.get("company"):
        sql += " AND IFNULL(c.name,'') LIKE ?"
        p.append(f"%{f['company']}%")
    return db.execute(sql + " ORDER BY c.code, o.code, m.employee_code, m.member_no",
                      p).fetchall()


def fetch_map(sql, ids, key="member_id", params_head=(), params_tail=()):
    """member_id をキーにした辞書を作る（IDが多い場合は分割して問い合わせる）"""
    db, out = H.get_db(), {}
    for part in chunks(ids):
        q = sql.replace("{IN}", ",".join("?" * len(part)))
        for r in db.execute(q, list(params_head) + list(part) + list(params_tail)):
            out[r[key]] = r
    return out


def fetch_rows(sql, ids, params_head=(), params_tail=()):
    db, out = H.get_db(), []
    for part in chunks(ids):
        q = sql.replace("{IN}", ",".join("?" * len(part)))
        out += db.execute(q, list(params_head) + list(part) + list(params_tail)).fetchall()
    return out


def overtime_of(ids, fy):
    """年度内で最も長い月の時間外労働を、加入者ごとに求める"""
    a, b = fy_span(fy)
    rows = fetch_rows("SELECT member_id, ym, hours, holiday_hours FROM oh_overtime"
                      " WHERE member_id IN ({IN}) AND ym BETWEEN ? AND ?", ids,
                      params_tail=(a, b))
    out = {}
    for r in rows:
        cur = out.get(r["member_id"])
        if not cur or (r["hours"] or 0) > (cur["hours"] or 0):
            out[r["member_id"]] = {"ym": r["ym"], "hours": r["hours"] or 0}
    return out


def memo_of(ids):
    """最新のメモを加入者ごとに求める"""
    rows = fetch_rows(
        "SELECT m1.member_id, m1.body, m1.author, m1.role, m1.created_at FROM oh_memo m1"
        " WHERE m1.member_id IN ({IN}) AND m1.id ="
        " (SELECT MAX(m2.id) FROM oh_memo m2 WHERE m2.member_id=m1.member_id)", ids)
    return {r["member_id"]: r for r in rows}


def reasons_of(judge, ot, stress):
    """抽出理由を組み立てる（複数該当時は併記）"""
    rs = []
    if judge in FINDING_JUDGES:
        rs.append(f"健診有所見（{judge}判定：{JUDGE_LABEL[judge]}）")
    if ot and (ot["hours"] or 0) > OT_LIMIT:
        lv = "100時間超" if (ot["hours"] or 0) > OT_LIMIT2 else "80時間超"
        rs.append(f"長時間労働（{ot['ym']} 時間外{ot['hours']:.0f}時間・{lv}）")
    if stress and stress["high"]:
        s = f"高ストレス（点数{stress['score']}）" if stress["score"] is not None else "高ストレス"
        if stress["applied"]:
            s += "・本人から面接指導の申出あり"
        rs.append(s)
    return rs


def build_rows(acc, fy, f=None, kind="定期"):
    """対象者一覧の行を作る。面談候補は自動抽出して oh_candidate に反映する。"""
    db = H.get_db()
    members = scoped_members(acc, f)
    ids = [m["id"] for m in members]
    if not ids:
        return []
    ken = fetch_map("SELECT * FROM oh_kenshin WHERE member_id IN ({IN})"
                    " AND fiscal_year=? AND kind=?", ids, params_tail=(fy, kind))
    st = fetch_map("SELECT * FROM oh_stress WHERE member_id IN ({IN}) AND fiscal_year=?",
                   ids, params_tail=(fy,))
    cand = fetch_map("SELECT * FROM oh_candidate WHERE member_id IN ({IN})"
                     " AND fiscal_year=?", ids, params_tail=(fy,))
    iv = fetch_map("SELECT * FROM oh_interview WHERE member_id IN ({IN})"
                   " AND fiscal_year=?", ids, params_tail=(fy,))
    ot = overtime_of(ids, fy)
    memo = memo_of(ids)

    rows, changed = [], False
    for m in members:
        k = ken.get(m["id"])
        judge = norm_judge(k["judge"]) if k else None
        s = st.get(m["id"])
        o = ot.get(m["id"])
        rs = reasons_of(judge, o, s)
        c = cand.get(m["id"])
        # 面談候補の自動抽出（B-01）。理由が変わったら更新する。
        if rs:
            text = "／".join(rs)
            if not c:
                db.execute("INSERT INTO oh_candidate (member_id, fiscal_year, reasons,"
                           " status, updated_at) VALUES (?,?,?,?,?)",
                           (m["id"], fy, text, ST_WAIT, H.now()))
                c = db.execute("SELECT * FROM oh_candidate WHERE member_id=?"
                               " AND fiscal_year=?", (m["id"], fy)).fetchone()
                changed = True
            elif (c["reasons"] or "") != text:
                db.execute("UPDATE oh_candidate SET reasons=?, updated_at=? WHERE id=?",
                           (text, H.now(), c["id"]))
                c = db.execute("SELECT * FROM oh_candidate WHERE id=?", (c["id"],)).fetchone()
                changed = True
        rows.append({
            "m": m, "id": m["id"], "age": age_of(m["birth"]),
            "judge": judge, "kenshin": k, "exam_date": k["exam_date"] if k else None,
            "stress": s, "overtime": o, "reasons": rs,
            "reason_text": "／".join(rs), "cand": c, "iv": iv.get(m["id"]),
            "memo": memo.get(m["id"]),
            "status": (c["status"] if c else "—"),
            "work_class": (c["work_class"] if c else None),
            "hr_class": (c["hr_class"] if c else "未判定"),
        })
    if changed:
        db.commit()
    return rows


def apply_row_filter(rows, f):
    """健診判定・抽出条件・ステータス・受診状況で絞り込む（画面の条件）"""
    js = [j for j in (f.get("judges") or []) if j in JUDGES]
    if js:
        rows = [r for r in rows if r["judge"] in js]
    cond = f.get("cond")
    if cond == "kenshin":
        rows = [r for r in rows if r["judge"] in FINDING_JUDGES]
    elif cond == "overtime":
        rows = [r for r in rows if r["overtime"] and r["overtime"]["hours"] > OT_LIMIT]
    elif cond == "stress":
        rows = [r for r in rows if r["stress"] and r["stress"]["high"]]
    elif cond == "any":
        rows = [r for r in rows if r["reasons"]]
    elif cond == "none":
        rows = [r for r in rows if not r["reasons"]]
    if f.get("status"):
        rows = [r for r in rows if r["status"] == f["status"]]
    if f.get("hr_class"):
        rows = [r for r in rows if r["hr_class"] == f["hr_class"]]
    if f.get("done") == "done":
        rows = [r for r in rows if r["exam_date"]]
    elif f.get("done") == "not":
        rows = [r for r in rows if not r["exam_date"]]
    return rows


def flow_counts(rows):
    """業務フローナビの件数（A-04）"""
    c = {"cand": 0, "wait": 0, "approved": 0, "mailed": 0, "done": 0, "out": 0}
    for r in rows:
        if r["reasons"]:
            c["cand"] += 1
        s = r["status"]
        if s == ST_WAIT:
            c["wait"] += 1
        elif s == ST_APPROVED:
            c["approved"] += 1
        elif s in (ST_MAILED, ST_BOOKED):
            c["mailed"] += 1
        elif s == ST_DONE:
            c["done"] += 1
        elif s == ST_OUT:
            c["out"] += 1
    return c


def view_key(acc):
    """表示列の保存キー。ロール＋サブロール単位で保存する
    （産業医と人事で見たい列が違うため）"""
    s = H.sub_role(acc)
    return acc["role"] + ("/" + s if s else "")


def visible_cols(acc):
    """ロール（サブロール）別の表示列（A-06）"""
    row = H.get_db().execute("SELECT cols FROM oh_view_col WHERE role=? AND screen=?",
                             (view_key(acc), "list")).fetchone()
    if not row:
        return list(LIST_COL_KEYS)
    keep = [c for c in row["cols"].split(",") if c in LIST_COL_KEYS]
    return keep or list(LIST_COL_KEYS)


def ensure_candidate(mid, fy):
    db = H.get_db()
    row = db.execute("SELECT * FROM oh_candidate WHERE member_id=? AND fiscal_year=?",
                     (mid, fy)).fetchone()
    if row:
        return row
    db.execute("INSERT INTO oh_candidate (member_id, fiscal_year, status, updated_at)"
               " VALUES (?,?,?,?)", (mid, fy, ST_WAIT, H.now()))
    return db.execute("SELECT * FROM oh_candidate WHERE member_id=? AND fiscal_year=?",
                      (mid, fy)).fetchone()


def owns_member(acc, mid):
    """自分の閲覧範囲の加入者かどうか（URLを直接呼ばれた場合の防御）"""
    where, params = H.member_where(acc)
    return bool(H.get_db().execute(
        f"SELECT 1 FROM member m WHERE m.id=? AND {where}", [mid] + list(params)).fetchone())


def member_row(acc, mid):
    where, params = H.member_where(acc)
    return H.get_db().execute(
        "SELECT m.*, c.name AS company_name, o.name AS office_name, d.name AS dept_name"
        " FROM member m LEFT JOIN company c ON c.id=m.company_id"
        " LEFT JOIN office o ON o.id=m.office_id"
        " LEFT JOIN department d ON d.id=m.dept_id"
        f" WHERE m.id=? AND {where}", [mid] + list(params)).fetchone()


def form_filter():
    f = {k: (request.args.get(k) or "").strip()
         for k in ("name", "dept", "company", "cond", "status", "hr_class", "done")}
    f["judges"] = request.args.getlist("judge")
    f["fy"] = (request.args.get("fy") or current_fy()).strip()
    return f


def common(fy):
    acc = H.current_account()
    return {"acc": acc, "fy": fy, "fys": fy_choices(), "JUDGES": JUDGES,
            "JUDGE_LABEL": JUDGE_LABEL, "WORK_CLASSES": WORK_CLASSES,
            "HR_CLASSES": HR_CLASSES, "STATUSES": STATUSES, "METHODS": METHODS,
            "PURPOSES": PURPOSES, "OT_LIMIT": OT_LIMIT, "OT_LIMIT2": OT_LIMIT2,
            # 画面に出す操作は機能制御に合わせる
            "CAN_MED": H.feature_allowed("oh.approve", acc),
            "CAN_IV": H.feature_allowed("oh.interview", acc),
            "CAN_SIGN": H.feature_allowed("oh.sign", acc),
            "CAN_HR": H.feature_allowed("oh.hr_class", acc),
            "CAN_UPLOAD": H.feature_allowed("oh.upload", acc),
            "CAN_MAIL": H.feature_allowed("oh.mail", acc),
            "CAN_KENSHIN": H.feature_allowed("oh.kenshin", acc),
            "CAN_OPS": is_ops(acc),
            "ROLE_NAME": role_label(), "LIST_COLS": LIST_COLS}


# ================================================================ B．対象者スクリーニング
@bp.route("/")
@need("oh.list")
def oh_list():
    """面談対象者一覧（B-01〜B-08）"""
    acc = H.current_account()
    f = form_filter()
    rows = build_rows(acc, f["fy"], f)
    flow_c = flow_counts(rows)
    rows = apply_row_filter(rows, f)
    total = len(rows)
    done = sum(1 for r in rows if r["exam_date"])
    return render_template("sanmen_list.html", rows=rows, f=f, flow_c=flow_c,
                           cols=visible_cols(acc), total=total, done=done,
                           notdone=total - done, **common(f["fy"]))


@bp.route("/cols", methods=["POST"])
@need("oh.list")
def oh_cols():
    """表示列の保存・初期化（A-06）"""
    acc, db = H.current_account(), H.get_db()
    if request.form.get("reset"):
        db.execute("DELETE FROM oh_view_col WHERE role=? AND screen='list'",
                   (view_key(acc),))
        db.commit()
        flash("表示列を初期状態に戻しました。", "ok")
    else:
        keep = [c for c in request.form.getlist("cols") if c in LIST_COL_KEYS]
        # 氏名は必ず表示する
        if "name" not in keep:
            keep.insert(0, "name")
        db.execute("INSERT INTO oh_view_col (role, screen, cols) VALUES (?,?,?)"
                   " ON CONFLICT(role, screen) DO UPDATE SET cols=excluded.cols",
                   (view_key(acc), "list", ",".join(keep)))
        db.commit()
        flash("表示列を保存しました。", "ok")
    return redirect(request.form.get("back") or url_for("oh.oh_list"))


@bp.route("/save", methods=["POST"])
@need("oh.list")
def oh_save():
    """確認・処理モーダルの保存（B-08〜B-14）。一括処理にも同じ入口を使う。

    医学的判断（承認・就業区分・面談不要）は産業医のみ。人事から送信された場合は
    サーバ側で無視する（A-03）。対応区分は人事のみが設定できる（B-11）。
    """
    acc, db = H.current_account(), H.get_db()
    fy = (request.form.get("fy") or current_fy()).strip()
    ids = [int(x) for x in request.form.getlist("member_ids") if str(x).isdigit()]
    if request.form.get("member_id", type=int):
        ids.append(request.form.get("member_id", type=int))
    ids = [i for i in dict.fromkeys(ids) if owns_member(acc, i)]
    if not ids:
        flash("対象者が選択されていません。", "error")
        return redirect(request.form.get("back") or url_for("oh.oh_list"))

    action = request.form.get("action") or "save"
    work_class = (request.form.get("work_class") or "").strip()
    hr_class = (request.form.get("hr_class") or "").strip()
    memo = (request.form.get("memo") or "").strip()
    ex_reason = (request.form.get("exclude_reason") or "").strip()
    med = is_doctor(acc)

    # 人事が医学的入力値を送ってきた場合は記録して無視する
    if not med and (work_class or action in ("approve", "unapprove", "exclude")):
        H.log("master", "医学的判断の入力を無視", "blocked", target="産業医面談",
              detail=f"role={acc['role']}／sub_role={H.sub_role(acc) or 'なし'}"
                     f"／就業区分・承認は産業医のみ")
        work_class = ""
        if action in ("approve", "unapprove", "exclude"):
            action = "save"
    if hr_class and not H.feature_allowed("oh.hr_class", acc):
        # 対応区分は人事（運用事務）の項目。使えないロールから送信された場合は無視する。
        H.log("master", "対応区分の入力を無視", "blocked", target="産業医面談",
              detail=f"role={H.role_key(acc)}／機能=oh.hr_class")
        hr_class = ""

    n_app = n_work = n_hr = n_memo = n_out = 0
    for mid in ids:
        c = ensure_candidate(mid, fy)
        sets, params = [], []
        if action == "approve":
            sets += ["status=?", "approved_by=?", "approved_at=?"]
            params += [ST_APPROVED, actor(), H.now()]
            n_app += 1
        elif action == "unapprove":
            sets += ["status=?", "approved_by=NULL", "approved_at=NULL"]
            params += [ST_WAIT]
        elif action == "exclude":
            sets += ["status=?", "exclude_reason=?"]
            params += [ST_OUT, ex_reason or "産業医の判断により面談不要"]
            n_out += 1
        if work_class in WORK_CLASSES:
            sets.append("work_class=?")
            params.append(work_class)
            n_work += 1
            # 承認前に就業区分を判定した場合も、面談対象として扱う
            if action == "save" and c["status"] == ST_WAIT:
                sets.append("status=?")
                params.append(ST_APPROVED)
        if hr_class in HR_CLASSES:
            sets.append("hr_class=?")
            params.append(hr_class)
            n_hr += 1
        # 「産業医判定済」は承認・就業判定から自動で反映する（コード定義）
        if med and (action == "approve" or work_class) and c["hr_class"] == "未判定":
            sets.append("hr_class=?")
            params.append("産業医判定済")
        if sets:
            sets.append("updated_at=?")
            params.append(H.now())
            db.execute(f"UPDATE oh_candidate SET {','.join(sets)} WHERE id=?",
                       params + [c["id"]])
        if memo:
            db.execute("INSERT INTO oh_memo (member_id, fiscal_year, body, author, role)"
                       " VALUES (?,?,?,?,?)", (mid, fy, memo, actor(), role_label()))
            n_memo += 1
    db.commit()

    detail = "／".join(x for x in [
        f"承認{n_app}件" if n_app else "", f"就業区分{n_work}件" if n_work else "",
        f"対応区分{n_hr}件" if n_hr else "", f"メモ{n_memo}件" if n_memo else "",
        f"対象外{n_out}件" if n_out else ""] if x) or "変更なし"
    H.log("master", "産業医面談の確認・処理", "success",
          target=f"{len(ids)}名（{fy}年度）", detail=detail)
    flash(f"{len(ids)}名に反映しました（{detail}）。", "ok")
    return redirect(request.form.get("back") or url_for("oh.oh_list"))


@bp.route("/member/<int:mid>")
@need("oh.list")
def oh_member(mid):
    """対象者の詳細（確認・処理モーダルの中身とメモ履歴・健診結果票）"""
    acc = H.current_account()
    m = member_row(acc, mid)
    if not m:
        return render_template("denied.html", path=request.path, notfound=True), 404
    fy = (request.args.get("fy") or current_fy()).strip()
    db = H.get_db()
    ken = db.execute("SELECT * FROM oh_kenshin WHERE member_id=? AND fiscal_year=?"
                     " AND kind='定期'", (mid, fy)).fetchone()
    prev = db.execute("SELECT * FROM oh_kenshin WHERE member_id=? AND fiscal_year=?"
                      " AND kind='定期'", (mid, str(int(fy) - 1))).fetchone()
    items = db.execute("SELECT * FROM oh_kenshin_item WHERE kenshin_id=?"
                       " ORDER BY sort, id", (ken["id"],)).fetchall() if ken else []
    prev_items = {r["name"]: r for r in db.execute(
        "SELECT * FROM oh_kenshin_item WHERE kenshin_id=?", (prev["id"],))} if prev else {}
    st = db.execute("SELECT * FROM oh_stress WHERE member_id=? AND fiscal_year=?",
                    (mid, fy)).fetchone()
    a, b = fy_span(fy)
    ots = db.execute("SELECT * FROM oh_overtime WHERE member_id=? AND ym BETWEEN ? AND ?"
                     " ORDER BY ym", (mid, a, b)).fetchall()
    memos = db.execute("SELECT * FROM oh_memo WHERE member_id=? ORDER BY id DESC",
                       (mid,)).fetchall()
    cand = db.execute("SELECT * FROM oh_candidate WHERE member_id=? AND fiscal_year=?",
                      (mid, fy)).fetchone()
    iv = db.execute("SELECT * FROM oh_interview WHERE member_id=? AND fiscal_year=?",
                    (mid, fy)).fetchone()
    otmax = max([dict(ym=r["ym"], hours=r["hours"] or 0) for r in ots],
                key=lambda x: x["hours"], default=None)
    judge = norm_judge(ken["judge"]) if ken else None
    return render_template("sanmen_member.html", m=m, ken=ken, items=items,
                           prev=prev, prev_items=prev_items, st=st, ots=ots,
                           memos=memos, cand=cand, iv=iv, judge=judge,
                           reasons=reasons_of(judge, otmax, st), age=age_of(m["birth"]),
                           **common(fy))


# ---------------------------------------------------------------- B-16 CSV出力
KENSHIN_CSV_HEADER = ["社員ID", "氏名", "企業", "事業所", "部署", "年齢", "性別",
                      "受診年度", "健診区分", "受診日", "総合判定", "判定の意味",
                      "抽出理由", "時間外（最大）", "高ストレス", "就業区分",
                      "対応区分", "ステータス", "産業医所見", "最新メモ"]


def csv_rows_for(acc, fy, ids=None, kind="定期"):
    rows = build_rows(acc, fy, None, kind)
    if ids:
        keep = set(ids)
        rows = [r for r in rows if r["id"] in keep]
    out = []
    for r in rows:
        m = r["m"]
        out.append((
            m["employee_code"] or "", m["name"], m["company_name"] or "",
            m["office_name"] or "", m["dept_name"] or "", r["age"] if r["age"] is not None else "",
            m["sex"] or "", fy, kind, r["exam_date"] or "",
            r["judge"] or "", JUDGE_LABEL.get(r["judge"] or "", ""),
            r["reason_text"], f"{r['overtime']['hours']:.0f}" if r["overtime"] else "",
            "該当" if (r["stress"] and r["stress"]["high"]) else "",
            r["work_class"] or "", r["hr_class"] or "", r["status"],
            (r["kenshin"]["findings"] if r["kenshin"] else "") or "",
            (r["memo"]["body"] if r["memo"] else "") or ""))
    return out


@bp.route("/export/csv", methods=["GET", "POST"])
@need("oh.list")
def oh_export_csv():
    """健診結果・面談状況の一括CSV出力（B-16）"""
    acc = H.current_account()
    src = request.form if request.method == "POST" else request.args
    fy = (src.get("fy") or current_fy()).strip()
    kind = src.get("kind") if src.get("kind") in KINDS else "定期"
    ids = [int(x) for x in src.getlist("member_ids") if str(x).isdigit()]
    ids = [i for i in ids if owns_member(acc, i)]
    rows = csv_rows_for(acc, fy, ids, kind)
    return H.export_csv(f"sanmen_{kind}_{fy}.csv", KENSHIN_CSV_HEADER, rows,
                        f"産業医面談 健診結果一覧（{fy}年度・{kind}）") \
        or redirect(url_for("oh.oh_list", fy=fy))


# ---------------------------------------------------------------- B-17 PDF（印刷）出力
@bp.route("/export/pdf", methods=["GET", "POST"])
@need("oh.list")
def oh_export_pdf():
    """健診結果票の一括PDF出力（B-17）。印刷用の画面を開き、PDFとして保存する。"""
    acc = H.current_account()
    src = request.form if request.method == "POST" else request.args
    fy = (src.get("fy") or current_fy()).strip()
    ids = [int(x) for x in src.getlist("member_ids") if str(x).isdigit()]
    ids = [i for i in ids if owns_member(acc, i)]
    rows = build_rows(acc, fy, None, "定期")
    if ids:
        keep = set(ids)
        rows = [r for r in rows if r["id"] in keep]
    rows = rows[:100]
    db = H.get_db()
    for r in rows:
        r["ken_items"] = db.execute(
            "SELECT * FROM oh_kenshin_item WHERE kenshin_id=? ORDER BY sort, id",
            (r["kenshin"]["id"],)).fetchall() if r["kenshin"] else []
        prev = db.execute("SELECT id FROM oh_kenshin WHERE member_id=? AND fiscal_year=?"
                          " AND kind='定期'", (r["id"], str(int(fy) - 1))).fetchone()
        r["prev_items"] = {x["name"]: x for x in db.execute(
            "SELECT * FROM oh_kenshin_item WHERE kenshin_id=?", (prev["id"],))} if prev else {}
    H.log("download", "健診結果票の印刷・PDF出力", "success",
          target=f"{len(rows)}名（{fy}年度）")
    return render_template("sanmen_kenshin_print.html", rows=rows, **common(fy))


# ================================================================ C．健診受診管理
@bp.route("/kenshin")
@need("oh.kenshin")
def oh_kenshin():
    """定期健診・深夜健診の受診一覧（C-01〜C-06）"""
    acc = H.current_account()
    f = form_filter()
    kind = request.args.get("kind") if request.args.get("kind") in KINDS else "定期"
    rows = build_rows(acc, f["fy"], f, kind)
    if kind == "深夜":
        # 深夜業従事区分が立っている加入者だけが対象（C-02・C-03）
        rows = [r for r in rows if r["m"]["night_work"]]
    rows = apply_row_filter(rows, f)
    total = len(rows)
    done = sum(1 for r in rows if r["exam_date"])
    finding = sum(1 for r in rows if r["judge"] in FINDING_JUDGES)
    return render_template("sanmen_kenshin.html", rows=rows, f=f, kind=kind,
                           total=total, done=done, notdone=total - done,
                           finding=finding,
                           rate=(done / total * 100 if total else 0),
                           **common(f["fy"]))


@bp.route("/night", methods=["POST"])
@need("oh.kenshin")
def oh_night():
    """深夜業従事区分の設定（C-03）"""
    acc, db = H.current_account(), H.get_db()
    mid = request.form.get("member_id", type=int)
    if not mid or not owns_member(acc, mid):
        flash("対象者を特定できませんでした。", "error")
        return redirect(url_for("oh.oh_kenshin", kind="深夜"))
    v = 1 if request.form.get("night_work") else 0
    db.execute("UPDATE member SET night_work=?, updated_at=? WHERE id=?", (v, H.now(), mid))
    db.commit()
    H.log("master", "深夜業従事区分を変更", "success", target=f"member:{mid}",
          detail="対象" if v else "対象外")
    flash("深夜業従事区分を変更しました。", "ok")
    return redirect(request.form.get("back") or url_for("oh.oh_kenshin", kind="深夜"))


# ================================================================ E．面談結果入力
@bp.route("/interview")
@need("oh.list")
def oh_interview():
    """面談結果入力の一覧（E-01・E-02）"""
    acc = H.current_account()
    f = form_filter()
    rows = build_rows(acc, f["fy"], f)
    flow_c = flow_counts(rows)
    # 面談対象（承認済以降）と、すでに面談記録があるものを表示する
    rows = [r for r in rows
            if r["status"] in (ST_APPROVED, ST_MAILED, ST_BOOKED, ST_DONE) or r["iv"]]
    rows = apply_row_filter(rows, f)
    return render_template("sanmen_interview.html", rows=rows, f=f, flow_c=flow_c,
                           done=sum(1 for r in rows if r["iv"]), **common(f["fy"]))


@bp.route("/interview/save", methods=["POST"])
@need("oh.interview")
def oh_interview_save():
    """面談記録の保存（E-01）。保存すると面談実施済として集計・報告へ連携する（E-02）。"""
    acc, db = H.current_account(), H.get_db()
    mid = request.form.get("member_id", type=int)
    fy = (request.form.get("fy") or current_fy()).strip()
    if not mid or not owns_member(acc, mid):
        flash("対象者を特定できませんでした。", "error")
        return redirect(url_for("oh.oh_interview", fy=fy))
    v = {k: (request.form.get(k) or "").strip()
         for k in ("met_on", "method", "purpose", "work_class", "measure",
                   "findings", "next_plan")}
    met_on = H.norm_date(v["met_on"]) if v["met_on"] else ""
    if met_on is None:
        flash("面談日の形式が正しくありません（例 2026-08-20）。", "error")
        return redirect(url_for("oh.oh_interview", fy=fy))
    ot = request.form.get("overtime", type=float)
    work = v["work_class"] if v["work_class"] in WORK_CLASSES else None
    db.execute(
        "INSERT INTO oh_interview (member_id, fiscal_year, met_on, method, purpose,"
        " work_class, measure, findings, next_plan, overtime, doctor, updated_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)"
        " ON CONFLICT(member_id, fiscal_year) DO UPDATE SET"
        " met_on=excluded.met_on, method=excluded.method, purpose=excluded.purpose,"
        " work_class=excluded.work_class, measure=excluded.measure,"
        " findings=excluded.findings, next_plan=excluded.next_plan,"
        " overtime=excluded.overtime, doctor=excluded.doctor, updated_at=excluded.updated_at",
        (mid, fy, met_on, v["method"], v["purpose"], work, v["measure"], v["findings"],
         v["next_plan"], ot, actor(), H.now()))
    c = ensure_candidate(mid, fy)
    sets = ["status=?", "updated_at=?"]
    params = [ST_DONE, H.now()]
    if work:
        sets.append("work_class=?")
        params.append(work)
    if c["hr_class"] == "未判定":
        sets.append("hr_class=?")
        params.append("産業医判定済")
    db.execute(f"UPDATE oh_candidate SET {','.join(sets)} WHERE id=?", params + [c["id"]])
    db.commit()
    H.log("master", "面談記録を登録", "success", target=f"member:{mid}（{fy}年度）",
          detail=f"面談日{met_on}／{v['method']}／就業区分{work or '—'}")
    flash("面談記録を保存しました。面談実施済として報告に反映します。", "ok")
    return redirect(url_for("oh.oh_interview", fy=fy))


# ================================================================ F．労基署報告
def report_summary(acc, fy):
    """報告書に載せる集計。定期健康診断結果報告書（様式第6号）の項目に合わせる。"""
    rows = build_rows(acc, fy, None, "定期")
    night = [r for r in rows if r["m"]["night_work"]]
    nrows = build_rows(acc, fy, None, "深夜") if night else []
    ndone = sum(1 for r in nrows if r["m"]["night_work"] and r["exam_date"])
    judges = {j: 0 for j in JUDGES}
    for r in rows:
        if r["judge"]:
            judges[r["judge"]] += 1
    done = sum(1 for r in rows if r["exam_date"])
    finding = sum(1 for r in rows if r["judge"] in FINDING_JUDGES)
    iv_target = sum(1 for r in rows if r["reasons"])
    iv_done = sum(1 for r in rows if r["iv"] and r["iv"]["met_on"])
    work = {w: sum(1 for r in rows if r["work_class"] == w) for w in WORK_CLASSES}
    st = [r for r in rows if r["stress"]]
    st_high = sum(1 for r in st if r["stress"]["high"])
    st_apply = sum(1 for r in st if r["stress"]["applied"])
    st_iv = sum(1 for r in rows if r["stress"] and r["stress"]["high"]
                and r["iv"] and r["iv"]["met_on"])
    camp = H.get_db().execute("SELECT * FROM oh_sc_campaign WHERE fiscal_year=?"
                              " ORDER BY id DESC LIMIT 1", (fy,)).fetchone()
    return {
        "total": len(rows), "done": done, "notdone": len(rows) - done,
        "rate": (done / len(rows) * 100 if rows else 0),
        "finding": finding, "judges": judges,
        "night_total": len(night), "night_done": ndone,
        "iv_target": iv_target, "iv_done": iv_done,
        "iv_rest": iv_target - iv_done, "work": work,
        "st_total": len(rows), "st_done": len(st), "st_high": st_high,
        "st_apply": st_apply, "st_iv": st_iv, "camp": camp,
        "kenpo": (H.get_db().execute("SELECT * FROM kenpo WHERE id=?",
                                     (acc["kenpo_id"],)).fetchone()
                  if acc["kenpo_id"] else None),
    }


@bp.route("/report")
@need("oh.report")
def oh_report():
    """労基署報告画面（F-01・F-05）。社内管理用サマリーと提出書類2種を表示する。"""
    acc = H.current_account()
    fy = (request.args.get("fy") or current_fy()).strip()
    db = H.get_db()
    signs = {r["kind"]: r for r in db.execute(
        "SELECT * FROM oh_sign WHERE fiscal_year=?", (fy,))}
    return render_template("sanmen_report.html", s=report_summary(acc, fy),
                           signs=signs, **common(fy))


@bp.route("/report/sign", methods=["POST"])
@need("oh.sign")
def oh_report_sign():
    """産業医の記名・サイン（F-04）と署名情報の登録（I-04）"""
    acc, db = H.current_account(), H.get_db()
    fy = (request.form.get("fy") or current_fy()).strip()
    kind = request.form.get("kind") if request.form.get("kind") in ("kenshin", "stress") \
        else "kenshin"
    name = (request.form.get("name") or "").strip()
    title = (request.form.get("title") or "").strip()
    if not name:
        flash("記名する産業医の氏名を入力してください。", "error")
        return redirect(url_for("oh.oh_report", fy=fy))
    db.execute("INSERT INTO oh_sign (fiscal_year, kind, name, title, signed_at, account_id)"
               " VALUES (?,?,?,?,?,?)"
               " ON CONFLICT(fiscal_year, kind) DO UPDATE SET name=excluded.name,"
               " title=excluded.title, signed_at=excluded.signed_at,"
               " account_id=excluded.account_id",
               (fy, kind, name, title, H.now(), acc["id"]))
    # 次回以降の初期値として保持する（I-04 署名情報の登録）
    for k, v in (("oh_sign_name", name), ("oh_sign_title", title)):
        db.execute("INSERT INTO setting (key, value) VALUES (?,?)"
                   " ON CONFLICT(key) DO UPDATE SET value=excluded.value", (k, v))
    db.commit()
    H.log("master", "報告書へ産業医が記名", "success",
          target=f"{fy}年度／{'様式第6号' if kind == 'kenshin' else 'ストレスチェック'}",
          detail=name)
    flash("記名・サインを登録しました。報告書に反映します。", "ok")
    return redirect(url_for("oh.oh_report", fy=fy))


@bp.route("/report/print/<kind>")
@need("oh.report")
def oh_report_print(kind):
    """報告書の印刷・PDF出力（F-02・F-03）"""
    if kind not in ("kenshin", "stress"):
        return render_template("denied.html", path=request.path, notfound=True), 404
    acc = H.current_account()
    fy = (request.args.get("fy") or current_fy()).strip()
    db = H.get_db()
    sign = db.execute("SELECT * FROM oh_sign WHERE fiscal_year=? AND kind=?",
                      (fy, kind)).fetchone()
    H.log("download", "報告書を出力", "success",
          target=f"{'定期健康診断結果報告書' if kind == 'kenshin' else 'ストレスチェック結果等報告書'}"
                 f"（{fy}年度）")
    return render_template("sanmen_report_print.html", kind=kind, sign=sign,
                           s=report_summary(acc, fy), **common(fy))


@bp.route("/report/csv")
@need("oh.report")
def oh_report_csv():
    """報告用集計のCSV出力（F-06）"""
    acc = H.current_account()
    fy = (request.args.get("fy") or current_fy()).strip()
    s = report_summary(acc, fy)
    rows = [
        ("在籍労働者数", s["total"]), ("受診労働者数", s["done"]),
        ("未受診者数", s["notdone"]), ("受診率（％）", f"{s['rate']:.1f}"),
        ("有所見者数（C判定以上）", s["finding"]),
    ]
    rows += [(f"判定区分 {j}（{JUDGE_LABEL[j]}）", s["judges"][j]) for j in JUDGES]
    rows += [
        ("深夜健診 対象者数", s["night_total"]), ("深夜健診 受診者数", s["night_done"]),
        ("面接指導 対象者数", s["iv_target"]), ("面接指導 実施数", s["iv_done"]),
        ("面接指導 未実施数", s["iv_rest"]),
    ]
    rows += [(f"就業区分 {w}", s["work"][w]) for w in WORK_CLASSES]
    rows += [
        ("ストレスチェック 対象者数", s["st_total"]), ("ストレスチェック 受検者数", s["st_done"]),
        ("高ストレス者数", s["st_high"]), ("面接指導の申出", s["st_apply"]),
        ("高ストレス者のうち面接指導実施", s["st_iv"]),
    ]
    return H.export_csv(f"sanmen_report_{fy}.csv", ["項目", "値"], rows,
                        f"労基署報告 集計（{fy}年度）") or redirect(url_for("oh.oh_report", fy=fy))


# ================================================================ G．データ取込
OT_COLUMNS = ["社員コード", "対象年月", "時間外労働時間", "休日労働時間"]
OT_SAMPLE = ["E0001", "2026-07", "92.5", "8.0"]
SC_COLUMNS = ["社員コード", "実施年度", "受検日", "合計点", "高ストレス者", "面接指導の申出"]
SC_SAMPLE = ["E0001", "2026", "2026-09-10", "78", "1", "1"]
KEN_COLUMNS = ["社員コード", "受診年度", "健診区分", "受診日", "総合判定", "産業医所見"]
KEN_SAMPLE = ["E0001", "2026", "定期", "2026-06-12", "C", "血圧・脂質で再検査が必要"]

TEMPLATES = {
    "overtime": ("overtime_format.csv", OT_COLUMNS, OT_SAMPLE, "労働時間"),
    "stress": ("stresscheck_format.csv", SC_COLUMNS, SC_SAMPLE, "ストレスチェック"),
    "kenshin": ("kenshin_result_format.csv", KEN_COLUMNS, KEN_SAMPLE, "健診結果"),
}


@bp.route("/upload")
@need("oh.upload")
def oh_upload():
    """データ取込のハブ（G-01）"""
    acc, db = H.current_account(), H.get_db()
    fy = (request.args.get("fy") or current_fy()).strip()
    a, b = fy_span(fy)
    where, params = H.member_where(acc)
    n_ot = db.execute("SELECT COUNT(*) c FROM oh_overtime t JOIN member m ON m.id=t.member_id"
                      f" WHERE {where} AND t.ym BETWEEN ? AND ?",
                      list(params) + [a, b]).fetchone()["c"]
    n_st = db.execute("SELECT COUNT(*) c FROM oh_stress t JOIN member m ON m.id=t.member_id"
                      f" WHERE {where} AND t.fiscal_year=?",
                      list(params) + [fy]).fetchone()["c"]
    n_ken = db.execute("SELECT COUNT(*) c FROM oh_kenshin t JOIN member m ON m.id=t.member_id"
                       f" WHERE {where} AND t.fiscal_year=?",
                       list(params) + [fy]).fetchone()["c"]
    n_link = db.execute("SELECT COUNT(*) c FROM kenshin_result r JOIN member m ON m.id=r.member_id"
                        f" WHERE {where}", list(params)).fetchone()["c"]
    qs = db.execute("SELECT * FROM oh_sc_question ORDER BY category, no, id").fetchall()
    return render_template("sanmen_upload.html", n_ot=n_ot, n_st=n_st, n_ken=n_ken,
                           n_link=n_link, questions=qs, **common(fy))


@bp.route("/template/<kind>.csv")
@need("oh.upload")
def oh_template(kind):
    """取込テンプレートの提供（G-06・H-04）"""
    if kind not in TEMPLATES:
        return render_template("denied.html", path=request.path, notfound=True), 404
    fn, cols, sample, label = TEMPLATES[kind]
    H.log("download", "取込フォーマットを出力", "success", target=f"産業医面談／{label}")
    return H.csv_response(fn, cols, [sample])


def find_member(acc, key):
    """社員コード・被保険者証番号・氏名で加入者を突合する（G-07 加入者名寄せ）"""
    key = (key or "").strip()
    if not key:
        return None
    where, params = H.member_where(acc)
    db = H.get_db()
    for sql in ("IFNULL(m.employee_code,'')=?", "m.member_no=?", "m.name=?",
                "IFNULL(m.subscriber_id,'')=?"):
        r = db.execute(f"SELECT m.id FROM member m WHERE {sql} AND {where}",
                       [key] + list(params)).fetchall()
        if len(r) == 1:
            return r[0]["id"]
        if len(r) > 1:
            return None
    return None


def import_csv(kind):
    """取込の共通処理。1行ずつ突合して登録し、未マッチは要確認として返す。"""
    acc, db = H.current_account(), H.get_db()
    fs = request.files.get("file")
    if not fs or not fs.filename:
        return None, ["ファイルが選択されていません。"], []
    try:
        table = H.read_table(fs)
    except ValueError as e:
        return None, [str(e)], []
    ok, errs, unmatched = 0, [], []
    for i, row in enumerate(table, start=2):
        row = {(k or "").strip(): (v or "").strip() for k, v in row.items()}
        key = row.get("社員コード") or row.get("被保険者証番号") or row.get("氏名")
        mid = find_member(acc, key)
        if not mid:
            unmatched.append({"line": i, "key": key or "（空欄）",
                              "detail": "加入者マスタに一致する行がありません（または複数該当）"})
            continue
        try:
            if kind == "overtime":
                ym = (row.get("対象年月") or "").replace("/", "-")[:7]
                if not re.fullmatch(r"\d{4}-\d{2}", ym):
                    raise ValueError("対象年月の形式が正しくありません（例 2026-07）")
                db.execute("INSERT INTO oh_overtime (member_id, ym, hours, holiday_hours)"
                           " VALUES (?,?,?,?) ON CONFLICT(member_id, ym) DO UPDATE SET"
                           " hours=excluded.hours, holiday_hours=excluded.holiday_hours",
                           (mid, ym, float(row.get("時間外労働時間") or 0),
                            float(row.get("休日労働時間") or 0)))
            elif kind == "stress":
                fy = (row.get("実施年度") or current_fy()).strip()[:4]
                d = H.norm_date(row.get("受検日")) or ""
                high = 1 if (row.get("高ストレス者") or "").strip() in ("1", "該当", "はい",
                                                                  "有", "Y", "y") else 0
                ap = 1 if (row.get("面接指導の申出") or "").strip() in ("1", "有", "はい",
                                                                 "申出あり", "Y", "y") else 0
                score = row.get("合計点")
                db.execute("INSERT INTO oh_stress (member_id, fiscal_year, exam_date,"
                           " score, high, applied) VALUES (?,?,?,?,?,?)"
                           " ON CONFLICT(member_id, fiscal_year) DO UPDATE SET"
                           " exam_date=excluded.exam_date, score=excluded.score,"
                           " high=excluded.high, applied=excluded.applied",
                           (mid, fy, d, int(score) if (score or "").isdigit() else None,
                            high, ap))
            else:
                fy = (row.get("受診年度") or current_fy()).strip()[:4]
                k = row.get("健診区分") if row.get("健診区分") in KINDS else "定期"
                d = H.norm_date(row.get("受診日")) or ""
                j = norm_judge(row.get("総合判定"))
                if not j:
                    raise ValueError("総合判定を判別できません（A〜E で指定してください）")
                db.execute("INSERT INTO oh_kenshin (member_id, fiscal_year, kind,"
                           " exam_date, judge, findings, source, updated_at)"
                           " VALUES (?,?,?,?,?,?, 'csv', ?)"
                           " ON CONFLICT(member_id, fiscal_year, kind) DO UPDATE SET"
                           " exam_date=excluded.exam_date, judge=excluded.judge,"
                           " findings=excluded.findings, updated_at=excluded.updated_at",
                           (mid, fy, k, d, j, row.get("産業医所見") or "", H.now()))
            ok += 1
        except (ValueError, TypeError) as e:
            errs.append(f"{i}行目：{e}")
    db.commit()
    return ok, errs, unmatched


@bp.route("/upload/<kind>", methods=["POST"])
@need("oh.upload")
def oh_upload_do(kind):
    """労働時間CSV（G-03）・ストレスチェックCSV（G-04）・健診結果CSV（G-02）の取込"""
    if kind not in TEMPLATES:
        return render_template("denied.html", path=request.path, notfound=True), 404
    fy = (request.form.get("fy") or current_fy()).strip()
    ok, errs, unmatched = import_csv(kind)
    label = TEMPLATES[kind][3]
    if ok is None:
        for e in errs:
            flash(e, "error")
        H.log("import", f"{label}の取込に失敗", "failure", target="産業医面談",
              detail="／".join(errs)[:200])
        return redirect(url_for("oh.oh_upload", fy=fy))
    for e in errs[:10]:
        flash(e, "error")
    if unmatched:
        flash(f"{len(unmatched)}行は加入者と突合できませんでした（"
              + "／".join(f"{u['line']}行目：{u['key']}" for u in unmatched[:5])
              + ("…" if len(unmatched) > 5 else "") + "）。加入者マスタをご確認ください。", "error")
    flash(f"{label}を{ok}件取込みました。", "ok")
    H.log("import", f"{label}を取込", "success", target="産業医面談",
          detail=f"{ok}件／未突合{len(unmatched)}件／エラー{len(errs)}件")
    return redirect(url_for("oh.oh_upload", fy=fy))


@bp.route("/upload/link", methods=["POST"])
@need("oh.upload")
def oh_upload_link():
    """本システムの健診結果連携（疾患予測で取込んだ個人の判定）から総合判定を作る。

    既存の kenshin_result は検査項目ごとの判定（基準内・要注意・要医療）なので、
    いちばん重い判定を学会区分（A〜E）へ読み替えて総合判定として登録する。
    """
    acc, db = H.current_account(), H.get_db()
    fy = (request.form.get("fy") or current_fy()).strip()
    where, params = H.member_where(acc)
    rows = db.execute(
        "SELECT r.member_id, r.exam_date, r.item, r.judge FROM kenshin_result r"
        f" JOIN member m ON m.id=r.member_id WHERE {where}", list(params)).fetchall()
    by = {}
    for r in rows:
        d = by.setdefault(r["member_id"], {"judges": [], "date": None, "items": []})
        d["judges"].append(r["judge"])
        d["items"].append(r)
        if r["exam_date"] and (not d["date"] or r["exam_date"] > d["date"]):
            d["date"] = r["exam_date"]
    n = 0
    for mid, d in by.items():
        j = worst_judge(d["judges"])
        if not j:
            continue
        exam = d["date"] or ""
        y = fy_of(exam) or fy
        db.execute("INSERT INTO oh_kenshin (member_id, fiscal_year, kind, exam_date,"
                   " judge, source, updated_at) VALUES (?,?, '定期', ?,?, 'kenshin_result', ?)"
                   " ON CONFLICT(member_id, fiscal_year, kind) DO UPDATE SET"
                   " exam_date=excluded.exam_date, judge=excluded.judge,"
                   " source=excluded.source, updated_at=excluded.updated_at",
                   (mid, y, exam, j, H.now()))
        kid = db.execute("SELECT id FROM oh_kenshin WHERE member_id=? AND fiscal_year=?"
                         " AND kind='定期'", (mid, y)).fetchone()["id"]
        db.execute("DELETE FROM oh_kenshin_item WHERE kenshin_id=?", (kid,))
        for i, it in enumerate(d["items"]):
            db.execute("INSERT INTO oh_kenshin_item (kenshin_id, name, value, judge, sort)"
                       " VALUES (?,?,?,?,?)",
                       (kid, it["item"], "", norm_judge(it["judge"]), i))
        n += 1
    db.commit()
    H.log("import", "健診結果の連携から総合判定を生成", "success", target="産業医面談",
          detail=f"{n}名")
    flash(f"本システムの健診結果から{n}名の総合判定を作成しました。", "ok")
    return redirect(url_for("oh.oh_upload", fy=fy))


@bp.route("/question", methods=["POST"])
@need("oh.upload")
def oh_question():
    """ストレスチェック設問マスタの追加・削除（H-03）"""
    db = H.get_db()
    fy = (request.form.get("fy") or current_fy()).strip()
    if request.form.get("delete", type=int):
        db.execute("DELETE FROM oh_sc_question WHERE id=?",
                   (request.form.get("delete", type=int),))
        db.commit()
        flash("設問を削除しました。", "ok")
        return redirect(url_for("oh.oh_upload", fy=fy))
    cat = (request.form.get("category") or "").strip()
    body = (request.form.get("body") or "").strip()
    no = request.form.get("no", type=int) or 0
    if not cat or not body:
        flash("カテゴリと設問内容を入力してください。", "error")
    else:
        db.execute("INSERT INTO oh_sc_question (category, no, body) VALUES (?,?,?)",
                   (cat, no, body))
        db.commit()
        H.log("master", "ストレスチェック設問を追加", "success", target=cat)
        flash("設問を追加しました。", "ok")
    return redirect(url_for("oh.oh_upload", fy=fy))


# ================================================================ D．メール配信
DEFAULT_TEMPLATES = [
    ("面談受診勧奨", "産業医面談のご案内",
     "産業医面談のご案内（{{氏名}} 様）",
     "{{氏名}} 様\n\n"
     "健康診断の結果等にもとづき、産業医面談の対象となりました。\n"
     "下記より面談のご予約をお願いいたします。\n\n"
     "　ご予約：{{予約URL}}\n"
     "　回答期限：{{回答期限}}\n\n"
     "本メールは健康管理の一環としてお送りしています。内容は産業医と人事担当者のみが確認します。\n"
     "※ 面談内容にもとづく就業上の措置は、産業医の判断により決定します。\n"),
    ("ストレスチェック案内", "ストレスチェック受検のご案内",
     "ストレスチェック受検のご案内（{{氏名}} 様）",
     "{{氏名}} 様\n\n"
     "今年度のストレスチェックを実施します。下記より受検をお願いいたします。\n\n"
     "　受検：{{予約URL}}\n"
     "　回答期限：{{回答期限}}\n\n"
     "回答内容は法令にもとづき保護され、人事評価には一切利用されません。\n"),
]


def ensure_templates():
    db = H.get_db()
    if db.execute("SELECT COUNT(*) c FROM oh_mail_template").fetchone()["c"]:
        return
    for kind, name, subject, body in DEFAULT_TEMPLATES:
        db.execute("INSERT INTO oh_mail_template (kind, name, subject, body)"
                   " VALUES (?,?,?,?)", (kind, name, subject, body))
    db.commit()


def fill_template(text, m, deadline, url):
    """差し込み項目を反映する（D-09）"""
    return (text or "").replace("{{氏名}}", m["name"] or "") \
        .replace("{{予約URL}}", url or "（予約URLは配信設定で指定します）") \
        .replace("{{回答期限}}", deadline or "（未設定）")


@bp.route("/mail")
@need("oh.mail")
def oh_mail():
    """メール配信（統合画面。D-01〜D-10）"""
    acc = H.current_account()
    ensure_templates()
    f = form_filter()
    kind = request.args.get("kind") if request.args.get("kind") in MAIL_KINDS \
        else MAIL_KINDS[0]
    rows = build_rows(acc, f["fy"], f)
    flow_c = flow_counts(rows)
    db = H.get_db()
    tpls = db.execute("SELECT * FROM oh_mail_template ORDER BY kind, id").fetchall()
    logs = db.execute(
        "SELECT l.*, m.name FROM oh_mail_log l LEFT JOIN member m ON m.id=l.member_id"
        " ORDER BY l.id DESC LIMIT 50").fetchall()
    camps = db.execute("SELECT * FROM oh_sc_campaign ORDER BY id DESC LIMIT 10").fetchall()
    sent_map = {r["member_id"]: r["c"] for r in db.execute(
        "SELECT member_id, COUNT(*) c FROM oh_mail_log WHERE kind=? AND result='success'"
        " GROUP BY member_id", (kind,))}
    return render_template("sanmen_mail.html", rows=rows, f=f, kind=kind, flow_c=flow_c,
                           tpls=tpls, logs=logs, camps=camps, sent_map=sent_map,
                           MAIL_KINDS=MAIL_KINDS, mail_on=H.mail_enabled(),
                           **common(f["fy"]))


@bp.route("/mail/template", methods=["POST"])
@need("oh.mail")
def oh_mail_template():
    """配信テンプレートの編集（D-08・D-09）"""
    db = H.get_db()
    fy = (request.form.get("fy") or current_fy()).strip()
    tid = request.form.get("id", type=int)
    name = (request.form.get("name") or "").strip()
    subject = (request.form.get("subject") or "").strip()
    body = (request.form.get("body") or "").strip()
    kind = request.form.get("kind") if request.form.get("kind") in MAIL_KINDS \
        else MAIL_KINDS[0]
    if not (name and subject and body):
        flash("テンプレート名・件名・本文を入力してください。", "error")
    elif tid:
        db.execute("UPDATE oh_mail_template SET name=?, subject=?, body=?, kind=?,"
                   " updated_at=? WHERE id=?", (name, subject, body, kind, H.now(), tid))
        db.commit()
        H.log("master", "配信テンプレートを更新", "success", target=name)
        flash("テンプレートを更新しました。", "ok")
    else:
        db.execute("INSERT INTO oh_mail_template (kind, name, subject, body)"
                   " VALUES (?,?,?,?)", (kind, name, subject, body))
        db.commit()
        H.log("master", "配信テンプレートを追加", "success", target=name)
        flash("テンプレートを追加しました。", "ok")
    return redirect(url_for("oh.oh_mail", fy=fy, kind=kind))


@bp.route("/mail/send", methods=["POST"])
@need("oh.mail")
def oh_mail_send():
    """メール送信・リマインド（D-06・D-07・D-10）

    面談受診勧奨は面談対象（承認済以降）のみ送信し、非対象はスキップする。
    ストレスチェック案内は全加入者へ送信する。
    """
    acc, db = H.current_account(), H.get_db()
    fy = (request.form.get("fy") or current_fy()).strip()
    kind = request.form.get("kind") if request.form.get("kind") in MAIL_KINDS \
        else MAIL_KINDS[0]
    tid = request.form.get("template_id", type=int)
    deadline = (request.form.get("deadline") or "").strip()
    url = (request.form.get("book_url") or "").strip()
    ids = [int(x) for x in request.form.getlist("member_ids") if str(x).isdigit()]
    ids = [i for i in dict.fromkeys(ids) if owns_member(acc, i)]
    tpl = db.execute("SELECT * FROM oh_mail_template WHERE id=?", (tid,)).fetchone()
    if not tpl:
        flash("配信テンプレートを選択してください。", "error")
        return redirect(url_for("oh.oh_mail", fy=fy, kind=kind))
    if not ids:
        flash("送信対象を1名以上選択してください。", "error")
        return redirect(url_for("oh.oh_mail", fy=fy, kind=kind))

    sent = skipped = failed = 0
    for mid in ids:
        m = member_row(acc, mid)
        if not m:
            continue
        c = db.execute("SELECT * FROM oh_candidate WHERE member_id=? AND fiscal_year=?",
                       (mid, fy)).fetchone()
        if kind == "面談受診勧奨" and (
                not c or c["status"] not in (ST_APPROVED, ST_MAILED, ST_BOOKED)):
            db.execute("INSERT INTO oh_mail_log (kind, template_id, member_id, subject,"
                       " actor, result, detail) VALUES (?,?,?,?,?, 'skipped', ?)",
                       (kind, tpl["id"], mid, tpl["subject"], actor(),
                        "面談対象ではないためスキップ"))
            skipped += 1
            continue
        subject = fill_template(tpl["subject"], m, deadline, url)
        body = fill_template(tpl["body"], m, deadline, url)
        ok, note = False, "メールアドレスが未登録"
        if m["email"]:
            try:
                res = bool(H.send_mail(m["email"], subject, body))
            except Exception:
                res = False
            if res:
                ok, note = True, ""
            elif not H.mail_enabled():
                # SMTPが未設定のときは outbox に控えを書き出す（本体と同じ扱い）
                ok, note = True, "SMTP未設定のため outbox に控えを保存"
            else:
                note = "送信できませんでした（送信設定をご確認ください）"
        db.execute("INSERT INTO oh_mail_log (kind, template_id, member_id, subject,"
                   " actor, result, detail) VALUES (?,?,?,?,?,?,?)",
                   (kind, tpl["id"], mid, subject, actor(),
                    "success" if ok else "failure", note))
        if ok:
            sent += 1
            if kind == "面談受診勧奨":
                cur = ensure_candidate(mid, fy)
                db.execute("UPDATE oh_candidate SET status=?, mail_count=mail_count+1,"
                           " last_mail_at=?, updated_at=? WHERE id=?",
                           (ST_MAILED, H.now(), H.now(), cur["id"]))
        else:
            failed += 1
    if kind == "ストレスチェック案内":
        db.execute("INSERT INTO oh_sc_campaign (fiscal_year, period_from, period_to,"
                   " deadline, sent, actor) VALUES (?,?,?,?,?,?)",
                   (fy, (request.form.get("period_from") or "").strip(),
                    (request.form.get("period_to") or "").strip(), deadline, sent, actor()))
    db.commit()
    H.log("master", "産業医面談のメール配信", "success", target=f"{kind}（{fy}年度）",
          detail=f"送信{sent}件／スキップ{skipped}件／失敗{failed}件")
    msg = f"{kind}を{sent}件送信しました。"
    if skipped:
        msg += f"（面談対象外{skipped}件はスキップしました）"
    if failed:
        msg += f"（{failed}件は送信できませんでした。メールアドレス・送信設定をご確認ください）"
    flash(msg, "ok" if sent else "error")
    return redirect(url_for("oh.oh_mail", fy=fy, kind=kind))
