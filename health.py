# -*- coding: utf-8 -*-
"""加入者の健康管理（一覧とマイページ）

加入者マスタ（company／office／department／member の登録・編集）とは別に、
「加入者一人ひとりの健康状態を管理する」ための画面をまとめた画面群です。

  ・/health/           … 加入者健康一覧（健診・時間外・ストレス・面談を横に並べる）
  ・/health/<加入者>    … 加入者マイページ（その人の健康情報をまとめた1画面）
  ・/health/me         … ログイン中のアカウント本人のマイページ
  ・/health/export     … 一覧のCSV出力

データは産業医面談管理（sanmen.py）と同じテーブルを使います。
  oh_kenshin／oh_kenshin_item … 健診の受診記録と検査値
  oh_overtime                 … 月ごとの時間外労働
  oh_stress                   … ストレスチェックの結果
  oh_interview                … 産業医面談の記録
  oh_memo                     … 担当者の内部メモ（本人には出しません）
  member_photo                … 採血結果などの写真

※ 表示する検査値・判定は参考情報です。受診の必要性や就業上の措置の最終判断は
   産業医・医師が行います（画面にもその旨を表示します）。
"""
from functools import wraps

from flask import (Blueprint, redirect, render_template, request, url_for,
                   flash)

import sanmen as S

bp = Blueprint("hm", __name__, url_prefix="/health")

# 本体（app.py）のモジュール。init_app で受け取る。
H = None


def init_app(flask_app, app_module):
    global H
    H = app_module
    flask_app.register_blueprint(bp)
    return bp


# ================================================================ 一覧の列
# (キー, 見出し, 列幅クラス)
# 対応の履歴の「記録の種類」。ロール別の業務フロー（人事・産業医・保健師）に合わせる
RECORD_KINDS = ["メモ", "対応区分", "就業上の措置", "面談日程",
                "保健指導", "受診勧奨", "フォロー完了", "医学的意見"]

LIST_COLS = [
    ("emp", "加入者ID", "col-num"),
    ("empno", "社員番号", "col-num"),
    ("name", "氏名", "col-name"),
    ("age", "年齢・性別", "col-sex"),
    ("judge", "健診判定", "col-flag"),
    ("reason", "抽出理由", "col-text300"),
    ("memo", "メモ", "col-text"),
    ("status", "ステータス", "col-status-w"),
]
# 一覧の絞り込みで使う「状態」のボタン＝対応区分（産業医面談と同じ区分を使います）
HR_CLASSES = S.HR_CLASSES
# ダッシュボードから渡ってくる絞り込み（画面のボタンには出しませんが、
# サマリーカード・所属名・判定のリンクはこの条件で一覧を開きます）
CONDS = [("", "すべて"), ("abn", "異常値あり"), ("cde", "C〜E判定"),
         ("ot", "時間外80時間超"), ("stress", "高ストレス"),
         ("iv", "面談実施済み"), ("none", "健診未受診")]
COND_LABEL = dict(CONDS)
# BMI・血圧は一覧に数値で出すので、検査項目名の候補を持っておく
BMI_NAMES = ("BMI",)
SBP_NAMES = ("収縮期血圧", "収縮期血圧(その他)", "最高血圧")
DBP_NAMES = ("拡張期血圧", "拡張期血圧(その他)", "最低血圧")
# マイページの検査値の並び（この順で先に出し、残りは取込順）
ITEM_ORDER = ["身長", "体重", "BMI", "腹囲", "収縮期血圧", "拡張期血圧",
              "中性脂肪", "HDLコレステロール", "LDLコレステロール",
              "空腹時血糖", "HbA1c", "AST(GOT)", "ALT(GPT)", "γ-GT(γ-GTP)"]

# 労働安全衛生規則 第44条（定期健康診断）で定める項目。
# マイページの「検査値」は、この区分・この並びで出します。
# (法定の区分, [(表示する項目名, 健診データの項目名を探すことば)])
STATUTORY = [
    ("既往歴・業務歴の調査",
     [("既往歴・業務歴", ["既往歴", "業務歴"])]),
    ("自覚症状・他覚症状の有無の検査",
     [("自覚症状・他覚症状", ["自覚症状", "他覚症状", "問診"])]),
    ("身長・体重・腹囲・視力・聴力の検査",
     [("身長", ["身長"]), ("体重", ["体重"]), ("腹囲", ["腹囲"]),
      ("視力", ["視力"]), ("聴力", ["聴力"])]),
    ("胸部エックス線検査・喀痰検査",
     [("胸部エックス線検査", ["胸部エックス", "胸部X", "胸部レントゲン"]),
      ("喀痰検査", ["喀痰"])]),
    ("血圧の測定",
     [("収縮期血圧", ["収縮期血圧"]), ("拡張期血圧", ["拡張期血圧"])]),
    ("貧血検査",
     [("赤血球数", ["赤血球"]),
      ("血色素量（ヘモグロビン）", ["血色素", "ヘモグロビン"])]),
    ("肝機能検査",
     [("AST（GOT）", ["AST", "GOT"]), ("ALT（GPT）", ["ALT", "GPT"]),
      ("γ-GT（γ-GTP）", ["γ-GT", "ガンマ"])]),
    ("血中脂質検査",
     [("LDLコレステロール", ["LDL"]), ("HDLコレステロール", ["HDL"]),
      ("血清トリグリセライド（中性脂肪）", ["中性脂肪", "トリグリセ"])]),
    ("血糖検査",
     [("空腹時血糖", ["空腹時血糖", "血糖"]), ("HbA1c", ["HbA1c"])]),
    ("尿検査",
     [("尿糖", ["尿糖"]), ("尿蛋白", ["尿蛋白", "尿タンパク"])]),
    ("心電図検査",
     [("心電図", ["心電図"])]),
]


def _norm_item(s):
    """項目名のゆれ（全角カッコ・空白）をそろえて突き合わせます"""
    return (s or "").replace("（", "(").replace("）", ")") \
                    .replace("－", "-").replace("　", "").replace(" ", "").upper()


def statutory_rows(items, prev_items):
    """労働安全衛生規則で定める項目の並びに、取り込んだ検査値を当てはめます。
    どの区分にも当てはまらない項目は「そのほかの検査項目」として後ろに出します。"""
    used = set()
    groups = []
    for gname, defs in STATUTORY:
        rows = []
        for label, kws in defs:
            hit = None
            for kw in kws:
                k = _norm_item(kw)
                for it in items:
                    if id(it) in used:
                        continue
                    if k and k in _norm_item(it["name"]):
                        hit = it
                        used.add(id(it))
                        break
                if hit:
                    break
            rows.append({"label": label, "it": hit,
                         "prev": prev_items.get(hit["name"]) if hit else None})
        groups.append({"name": gname, "rows": rows})
    others = [{"label": it["name"], "it": it, "prev": prev_items.get(it["name"])}
              for it in items if id(it) not in used]
    return groups, others
# マイページに出すメモの件数（多いときは最新のぶんだけ出します）
MEMO_MAX = 50

# ================================================================ メール送信
# 健康管理からのメールは、産業医面談の配信（面談受診勧奨・ストレスチェック案内）とは
# 別の種別で扱い、テンプレート・履歴も同じテーブル（oh_mail_template／oh_mail_log）に
# この種別で残します。
MAIL_KIND = "健康管理のお知らせ"
# 一覧に出す送信履歴の件数
MAIL_LOG_MAX = 20
# 差し込み項目（本文・件名で使えるもの）
MAIL_FIELDS = ["{{氏名}}", "{{年度}}", "{{受診日}}", "{{期限}}", "{{URL}}", "{{担当窓口}}"]
# 既定のテンプレート。検査値・判定は本文に載せず、マイページで確認していただく形にしています
DEFAULT_TEMPLATES = [
    ("健診結果のご確認のお願い",
     "健康診断の結果のご確認について（{{氏名}} 様）",
     "{{氏名}} 様\n\n"
     "{{年度}}年度の健康診断の結果（受診日 {{受診日}}）をご確認いただけます。\n"
     "内容は健康マイページからご覧ください。\n\n"
     "　健康マイページ：{{URL}}\n"
     "　ご確認の期限：{{期限}}\n\n"
     "結果の見かたやご不明な点は、{{担当窓口}}までお気軽にご相談ください。\n"
     "※ 検査値・判定はメールには記載していません。\n"
     "※ 再検査や治療の必要性は、産業医・医師が判断します。\n"),
    ("再検査・精密検査のお願い",
     "再検査（精密検査）のお願い（{{氏名}} 様）",
     "{{氏名}} 様\n\n"
     "健康診断の結果について、医療機関での再検査（精密検査）をお願いしたい項目があります。\n"
     "詳しい内容は健康マイページでご確認のうえ、受診をご検討ください。\n\n"
     "　健康マイページ：{{URL}}\n"
     "　ご返信の期限：{{期限}}\n\n"
     "受診先の相談・受診後のご報告は {{担当窓口}} で承ります。\n"
     "※ 受診の必要性や就業上の措置は、産業医・医師の判断によります。\n"),
    ("生活習慣改善のご案内",
     "健康づくりのご案内（{{氏名}} 様）",
     "{{氏名}} 様\n\n"
     "{{年度}}年度の健康診断の結果をふまえ、生活習慣の見直しについてご案内します。\n"
     "ご自身の結果の推移は健康マイページでご確認いただけます。\n\n"
     "　健康マイページ：{{URL}}\n\n"
     "保健師による個別相談もご利用いただけます（{{担当窓口}}）。\n"),
    ("長時間労働の方へ（面談のご案内）",
     "医師の面接指導のご案内（{{氏名}} 様）",
     "{{氏名}} 様\n\n"
     "時間外労働の状況から、医師の面接指導の対象となりました。\n"
     "面談のご希望・ご都合を {{期限}} までにご連絡ください。\n\n"
     "　お問い合わせ：{{担当窓口}}\n\n"
     "本メールは健康管理の一環としてお送りしています。\n"),
]


def need(*keys):
    """機能制御で画面・操作を制限します。
    複数のキーを渡したときは、どれか1つでも使えれば通します
    （例：加入者マイページは健康管理からでも面談対象者一覧からでも開きます）。"""
    def deco(fn):
        @wraps(fn)
        def wrapper(*a, **kw):
            acc = H.current_account()
            if not acc:
                return redirect(url_for("login", next=request.path))
            if not any(H.feature_allowed(k, acc) for k in keys):
                return H.deny_feature(keys[0])
            return fn(*a, **kw)
        return wrapper
    return deco


# ================================================================ 取得処理
def _first(items, names):
    """検査値の中から、名前が一致する最初の項目を返す"""
    for n in names:
        for it in items:
            if (it["name"] or "").strip() == n:
                return it
    return None


def abn_count(items):
    """C〜Eの判定がついた検査値の件数（異常値の件数）"""
    n = 0
    for it in items:
        j = S.norm_judge(it["judge"])
        if j in ("C", "D", "E"):
            n += 1
    return n


def list_rows(acc, f):
    """加入者健康一覧の行を作る"""
    db = H.get_db()
    fy = f["fy"]
    ms = S.scoped_members(acc, f)
    ids = [m["id"] for m in ms]
    if not ids:
        return []
    ken = S.fetch_map(
        "SELECT * FROM oh_kenshin WHERE member_id IN ({IN})"
        " AND fiscal_year=? AND kind='定期'", ids, params_tail=(fy,))
    kids = [r["id"] for r in ken.values()]
    items = {}
    for r in S.fetch_rows("SELECT * FROM oh_kenshin_item WHERE kenshin_id IN ({IN})"
                          " ORDER BY sort, id", kids):
        items.setdefault(r["kenshin_id"], []).append(r)
    ot = S.overtime_of(ids, fy)
    # 月別の時間外（ダッシュボードの月次推移で使います）
    a, b = S.fy_span(fy)
    ot_months = {}
    for r in S.fetch_rows("SELECT member_id, ym, hours FROM oh_overtime"
                          " WHERE member_id IN ({IN}) AND ym BETWEEN ? AND ?"
                          " ORDER BY ym", ids, params_tail=(a, b)):
        ot_months.setdefault(r["member_id"], []).append((r["ym"], r["hours"] or 0))
    st = S.fetch_map("SELECT * FROM oh_stress WHERE member_id IN ({IN})"
                     " AND fiscal_year=?", ids, params_tail=(fy,))
    iv = S.fetch_map("SELECT member_id, COUNT(*) AS n, MAX(met_on) AS last_on"
                     " FROM oh_interview WHERE member_id IN ({IN}) AND fiscal_year=?"
                     " GROUP BY member_id", ids, params_tail=(fy,))
    ph = S.fetch_map("SELECT member_id, COUNT(*) AS n FROM member_photo"
                     " WHERE member_id IN ({IN}) GROUP BY member_id", ids)
    memo = S.memo_of(ids)
    # 対応区分（未判定／産業医判定済／…）。産業医面談の対象者と同じものを使います
    cand = S.fetch_map("SELECT * FROM oh_candidate WHERE member_id IN ({IN})"
                       " AND fiscal_year=?", ids, params_tail=(fy,))
    out = []
    for m in ms:
        k = ken.get(m["id"])
        its = items.get(k["id"], []) if k else []
        bmi = _first(its, BMI_NAMES)
        sbp, dbp = _first(its, SBP_NAMES), _first(its, DBP_NAMES)
        bp = ""
        if sbp and (sbp["value"] or "").strip():
            bp = (sbp["value"] or "").strip()
            if dbp and (dbp["value"] or "").strip():
                bp += "／" + (dbp["value"] or "").strip()
        s = st.get(m["id"])
        v = iv.get(m["id"])
        out.append({
            "id": m["id"], "emp": m["subscriber_id"] or "",
            "empno": m["employee_code"] or "", "name": m["name"],
            "kana": m["kana"] or "", "sex": m["sex"] or "",
            "email": m["email"] or "",
            "age": S.age_of(m["birth"]),
            "company": m["company_name"] or "", "office": m["office_name"] or "",
            "dept": m["dept_name"] or "",
            "exam_date": (k["exam_date"] if k else "") or "",
            "judge": S.norm_judge(k["judge"]) if k else None,
            "bmi": (bmi["value"] or "").strip() if bmi else "",
            "bmi_judge": S.norm_judge(bmi["judge"]) if bmi else None,
            "bp": bp,
            "bp_judge": S.norm_judge(sbp["judge"]) if sbp else None,
            "abn": abn_count(its),
            "ot": ot.get(m["id"]), "ot_months": ot_months.get(m["id"], []),
            "reason_text": S.reason_display(
                S.reasons_of(S.norm_judge(k["judge"]) if k else None,
                             ot.get(m["id"]), st.get(m["id"]), its)),
            "stress": s, "iv_n": (v["n"] if v else 0),
            "iv_last": (v["last_on"] if v else ""),
            "photo": (ph[m["id"]]["n"] if m["id"] in ph else 0),
            "memo": (memo[m["id"]]["body"] if m["id"] in memo else ""),
            "hr_class": (cand[m["id"]]["hr_class"] if m["id"] in cand
                         and cand[m["id"]]["hr_class"] else S.HR_CLASSES[0]),
            "status": (cand[m["id"]]["status"] if m["id"] in cand else ""),
            "due_on": (cand[m["id"]]["due_on"] if m["id"] in cand else ""),
            "follow_on": (cand[m["id"]]["follow_on"] if m["id"] in cand else ""),
            "done_on": (cand[m["id"]]["done_on"] if m["id"] in cand else ""),
            "night": m["night_work"],
        })
    return out


def apply_filter(rows, f):
    js = set(f.get("judges") or [])
    if js:
        rows = [r for r in rows if (r["judge"] or "") in js]
    hc = f.get("hr_class") or ""
    if hc:
        rows = [r for r in rows if r["hr_class"] == hc]
    elif f.get("nurse"):
        # 保健師の一覧：産業医が 就業制限・要休業 と判定した方（保健指導の対象）と、保健師対応中の方だけを出す
        rows = [r for r in rows if r["hr_class"] in NURSE_HR_CLASSES]
    c = f.get("cond") or ""
    if c == "abn":
        rows = [r for r in rows if r["abn"]]
    elif c == "cde":
        rows = [r for r in rows if (r["judge"] or "") in ("C", "D", "E")]
    elif c == "ot":
        rows = [r for r in rows
                if r["ot"] and (r["ot"]["hours"] or 0) > S.OT_LIMIT]
    elif c == "stress":
        rows = [r for r in rows if r["stress"] and r["stress"]["high"]]
    elif c == "iv":
        rows = [r for r in rows if r["iv_n"]]
    elif c == "none":
        rows = [r for r in rows if not r["exam_date"]]
    return rows


# 保健師が使うステータス（産業医の判定で保健指導の対象になる区分）
NURSE_HR_CLASSES = S.NURSE_HR_CLASSES


def form_filter():
    f = {k: (request.args.get(k) or "").strip()
         for k in ("name", "mid", "dept", "company", "cond", "hr_class")}
    f["judges"] = request.args.getlist("judge")
    f["fy"] = (request.args.get("fy") or S.current_fy()).strip()
    # 保健師はステータスが 就業制限・要休業 の方だけを扱う（それ以外の指定は外す）
    acc = H.current_account()
    f["nurse"] = bool(acc) and H.sub_role(acc) == "nurse"
    if f["nurse"] and f["hr_class"] and f["hr_class"] not in NURSE_HR_CLASSES:
        f["hr_class"] = ""
    return f


def ensure_templates():
    """健康管理のメールテンプレートを用意する（無いときだけ入れます）"""
    db = H.get_db()
    n = db.execute("SELECT COUNT(*) c FROM oh_mail_template WHERE kind=?",
                   (MAIL_KIND,)).fetchone()["c"]
    if n:
        return
    for name, subject, body in DEFAULT_TEMPLATES:
        db.execute("INSERT INTO oh_mail_template (kind, name, subject, body)"
                   " VALUES (?,?,?,?)", (MAIL_KIND, name, subject, body))
    db.commit()


def fill_template(text, m, fy, exam_date, deadline, url, desk):
    """差し込み項目を反映する（検査値・判定は差し込みません）"""
    return ((text or "")
            .replace("{{氏名}}", m["name"] or "")
            .replace("{{年度}}", str(fy or ""))
            .replace("{{受診日}}", exam_date or "（未受診）")
            .replace("{{期限}}", deadline or "（未設定）")
            .replace("{{URL}}", url or "（健康マイページのURLは送信時に入力します）")
            .replace("{{担当窓口}}", desk or "健康管理のご担当窓口"))


def f_query(f):
    """いまの絞り込みをそのままリンク（CSV出力）に引き継ぐためのクエリ"""
    q = {"fy": f["fy"]}
    for k in ("name", "mid", "dept", "company", "cond", "hr_class"):
        if f.get(k):
            q[k] = f[k]
    if f.get("judges"):
        q["judge"] = f["judges"]
    return q


def common(fy):
    acc = H.current_account()
    return {"acc": acc, "fy": fy, "fys": S.fy_choices(), "JUDGES": S.JUDGES,
            "JUDGE_LABEL": S.JUDGE_LABEL, "LIST_COLS": LIST_COLS,
            "CONDS": CONDS, "COND_LABEL": COND_LABEL, "HR_CLASSES": HR_CLASSES,
            "OT_LIMIT": S.OT_LIMIT, "OT_LIMIT2": S.OT_LIMIT2,
            "CAN_DL": H.feature_allowed("download", acc),
            "CAN_MEMO": H.feature_allowed("health.view", acc),
            "CAN_MAIL": H.feature_allowed("health.mail", acc),
            # 一括処理（面談対象者一覧と同じ内容）で使う区分と権限
            "CAN_MED": H.feature_allowed("oh.approve", acc),
            "CAN_HR": H.feature_allowed("oh.hr_class", acc),
            "HR_CLASS_GUIDE": S.HR_CLASS_GUIDE, "GUIDE_ROLES": S.GUIDE_ROLES,
            "HR_DONE": S.HR_DONE,
            "NURSE_HR_CLASSES": NURSE_HR_CLASSES,
            "CAN_IV_VIEW": H.feature_allowed("oh.interview.view", acc),
            "IS_NURSE": H.sub_role(acc) == "nurse",
            "IS_DOCTOR": S.is_doctor(acc),
            "RECORD_KINDS": RECORD_KINDS,
            "WORK_CLASSES": S.WORK_CLASSES,
            "MAIL_KIND": MAIL_KIND, "MAIL_FIELDS": MAIL_FIELDS,
            "mail_on": H.mail_enabled(),
            "ROLE_NAME": S.role_label()}


# ================================================================ 一覧
def stat_of(rows):
    """まとめの数字（ダッシュボードと一覧の絞り込みで共通に使う）"""
    return {
        "all": len(rows),
        "done": sum(1 for r in rows if r["exam_date"]),
        "cde": sum(1 for r in rows if (r["judge"] or "") in ("C", "D", "E")),
        "ot": sum(1 for r in rows
                  if r["ot"] and (r["ot"]["hours"] or 0) > S.OT_LIMIT),
        "ot2": sum(1 for r in rows
                   if r["ot"] and (r["ot"]["hours"] or 0) > S.OT_LIMIT2),
        "stress": sum(1 for r in rows if r["stress"] and r["stress"]["high"]),
        "iv": sum(1 for r in rows if r["iv_n"]),
        "abn": sum(1 for r in rows if r["abn"]),
        "photo": sum(r["photo"] for r in rows),
        "nomail": sum(1 for r in rows if not r["email"]),
    }


def month_series(rows, kind):
    """月別の件数を 4月〜翌3月 の12個の配列にする（ダッシュボードのグラフ用）

    kind="ken" … 健診を受診した月
    kind="ot"  … 時間外が月80時間を超えた月（その月の記録から数えます）
    """
    out = [0] * 12
    for r in rows:
        if kind == "ken":
            d = r["exam_date"]
            if not d or len(str(d)) < 7:
                continue
            try:
                mo = int(str(d)[5:7])
            except ValueError:
                continue
            if 1 <= mo <= 12:
                out[(mo - 4) % 12] += 1
        else:
            for ym, h in (r["ot_months"] or []):
                if (h or 0) <= S.OT_LIMIT or len(str(ym)) < 6:
                    continue
                try:
                    mo = int(str(ym)[4:6])
                except ValueError:
                    continue
                if 1 <= mo <= 12:
                    out[(mo - 4) % 12] += 1
    return out


@bp.route("/dashboard")
@need("health.view")
def hm_dash():
    """健康管理ダッシュボード（サービス全体のダッシュボード）

    ほかのダッシュボード（健診代行・保健指導・産業医面談）と同じ作りにそろえています。
    サマリーカード → 月次推移とドーナツ → 内訳（要注意・判定）→ 所属ごとの状況。
    """
    acc = H.current_account()
    f = form_filter()
    rows = list_rows(acc, f)
    stat = stat_of(rows)
    # 健診判定の内訳（A〜Eと未受診）
    judges = {j: 0 for j in S.JUDGES}
    judges["未受診"] = 0
    for r in rows:
        judges[r["judge"] if r["judge"] in judges else "未受診"] += 1
    # 所属（部署→事業所→企業）ごとの要注意人数
    by = {}
    for r in rows:
        key = r["dept"] or r["office"] or r["company"] or "未紐づけ"
        d = by.setdefault(key, {"name": key, "all": 0, "cde": 0, "ot": 0,
                                "stress": 0, "done": 0})
        d["all"] += 1
        d["done"] += 1 if r["exam_date"] else 0
        d["cde"] += 1 if (r["judge"] or "") in ("C", "D", "E") else 0
        d["ot"] += 1 if r["ot"] and (r["ot"]["hours"] or 0) > S.OT_LIMIT else 0
        d["stress"] += 1 if r["stress"] and r["stress"]["high"] else 0
    depts = sorted(by.values(),
                   key=lambda d: (-(d["cde"] + d["ot"] + d["stress"]), d["name"]))
    # 受診率・面談実施率（0除算を避ける）
    rate = {
        "done": round(stat["done"] * 100 / stat["all"]) if stat["all"] else 0,
        "iv": round(stat["iv"] * 100 / stat["cde"]) if stat["cde"] else 0,
    }
    # 月次推移（4月〜翌3月）。前年度と並べて比較できるようにします。
    pf = dict(f, fy=str(int(f["fy"]) - 1))
    prev_rows = list_rows(acc, pf)
    monthly = {"ken": {"cur": month_series(rows, "ken"),
                       "prev": month_series(prev_rows, "ken")},
               "ot": {"cur": month_series(rows, "ot"),
                      "prev": month_series(prev_rows, "ot")}}
    return render_template("health_dash.html", rows=rows, f=f, stat=stat,
                           judges=judges, depts=depts, rate=rate, monthly=monthly,
                           f_query=f_query(f), **common(f["fy"]))


@bp.route("/")
@need("health.view")
def hm_list():
    """加入者健康一覧（加入者マスタとは別に、健康状態だけを横に並べて見る画面）"""
    acc = H.current_account()
    f = form_filter()
    rows = apply_filter(list_rows(acc, f), f)
    ensure_templates()
    db = H.get_db()
    # テンプレートは送信画面（モーダル）の下書きに使います。
    # テンプレートの編集と配信履歴は「メール配信管理」にまとめています。
    tpls = db.execute("SELECT * FROM oh_mail_template WHERE kind=? ORDER BY id",
                      (MAIL_KIND,)).fetchall()
    sent_map = {r["member_id"]: r["c"] for r in db.execute(
        "SELECT member_id, COUNT(*) c FROM oh_mail_log WHERE kind=? AND result='success'"
        " GROUP BY member_id", (MAIL_KIND,))}
    return render_template("health_list.html", rows=rows, f=f,
                           total=len(rows), f_query=f_query(f), tpls=tpls,
                           sent_map=sent_map, **common(f["fy"]))


CSV_HEADER = ["加入者ID", "社員番号", "氏名", "カナ", "性別", "年齢", "企業",
              "事業所", "部署", "受診日", "健診判定", "判定の意味", "異常値の件数",
              "BMI", "血圧", "時間外（最大）", "対象月", "ストレス点数",
              "高ストレス", "面談回数", "最終面談日", "写真", "対応区分",
              "面談ステータス", "対応期限", "次回フォロー予定日", "対応完了日",
              "抽出理由", "最新メモ"]


@bp.route("/export")
@need("health.view")
def hm_export():
    acc = H.current_account()
    f = form_filter()
    rows = apply_filter(list_rows(acc, f), f)
    out = []
    for r in rows:
        st = r["stress"]
        out.append([
            r["emp"], r["empno"], r["name"], r["kana"], r["sex"],
            "" if r["age"] is None else r["age"], r["company"], r["office"],
            r["dept"], r["exam_date"], r["judge"] or "",
            S.JUDGE_LABEL.get(r["judge"] or "", ""), r["abn"], r["bmi"], r["bp"],
            (r["ot"]["hours"] if r["ot"] else ""),
            (r["ot"]["ym"] if r["ot"] else ""),
            (st["score"] if st else ""),
            ("高ストレス" if st and st["high"] else ""),
            r["iv_n"], r["iv_last"] or "", r["photo"], r["hr_class"],
            r["status"] or "", r["due_on"] or "", r["follow_on"] or "",
            r["done_on"] or "", r["reason_text"], r["memo"],
        ])
    resp = H.export_csv(f"health_{f['fy']}.csv", CSV_HEADER, out, "加入者健康一覧")
    if resp is None:
        return redirect(url_for("hm.hm_list", **f_query(f)))
    return resp


# ================================================================ メールの送信
REC_CSV_HEADER = ["記録日時", "加入者ID", "社員番号", "氏名", "企業", "事業所", "部署",
                  "種類", "対応区分", "対応期限", "次回フォロー予定日", "対応完了日",
                  "内容", "記入者", "ロール"]


@bp.route("/export/records")
@need("health.view")
def hm_export_records():
    """対応の履歴（支援実績）のCSV出力。
    ロール別業務フローの「保健師：支援実績を出力」「人事：対応状況・期限を確認」に対応する。
    担当範囲の加入者の、その年度の記録（メモ・対応区分・保健指導・受診勧奨・フォロー完了・面談日程・医学的意見）。"""
    acc = H.current_account()
    f = form_filter()
    fy = f["fy"]
    where, params = H.member_where(acc)
    kind = (request.args.get("kind") or "").strip()
    sql = ("SELECT x.created_at, m.subscriber_id, m.employee_code, m.name, c.name AS company_name,"
           " o.name AS office_name, d.name AS dept_name, x.kind, x.hr_class, x.due_on,"
           " x.follow_on, x.done_on, x.body, x.author, x.role"
           " FROM oh_memo x JOIN member m ON m.id=x.member_id"
           " LEFT JOIN company c ON c.id=m.company_id"
           " LEFT JOIN office o ON o.id=m.office_id"
           " LEFT JOIN department d ON d.id=m.dept_id"
           f" WHERE x.fiscal_year=? AND {where}")
    args = [fy] + list(params)
    if kind:
        sql += " AND x.kind=?"
        args.append(kind)
    sql += " ORDER BY x.created_at DESC, x.id DESC"
    rows = H.get_db().execute(sql, args).fetchall()
    out = [[r["created_at"], r["subscriber_id"] or "", r["employee_code"] or "", r["name"],
            r["company_name"] or "", r["office_name"] or "", r["dept_name"] or "",
            r["kind"] or "メモ", r["hr_class"] or "", r["due_on"] or "", r["follow_on"] or "",
            r["done_on"] or "", r["body"] or "", r["author"] or "", r["role"] or ""]
           for r in rows]
    resp = H.export_csv(f"records_{fy}.csv", REC_CSV_HEADER, out, "対応の履歴（支援実績）")
    if resp is None:
        return redirect(url_for("hm.hm_list", **f_query(f)))
    return resp


@bp.route("/mail/send", methods=["POST"])
@need("health.mail")
def hm_mail_send():
    """加入者健康一覧からメールを送る（個人別・一括のどちらも同じ処理）

    ・member_ids に1件だけ入れば個人別、複数入れば一括の送信になります
    ・件名・本文は画面で直したものをそのまま使います（テンプレートは下書きです）
    ・メールアドレスが未登録の方は送信せず、履歴に理由を残します
    """
    acc, db = H.current_account(), H.get_db()
    f = form_filter()
    fy = f["fy"]
    back = url_for("hm.hm_list", **f_query(f))
    ids = [int(x) for x in request.form.getlist("member_ids") if str(x).isdigit()]
    ids = [i for i in dict.fromkeys(ids) if S.owns_member(acc, i)]
    tid = request.form.get("template_id", type=int)
    subject = (request.form.get("subject") or "").strip()
    body = (request.form.get("body") or "").strip()
    deadline = (request.form.get("deadline") or "").strip()
    url = (request.form.get("mypage_url") or "").strip()
    desk = (request.form.get("desk") or "").strip()
    if not ids:
        flash("送信する加入者を選んでください。", "error")
        return redirect(back)
    if not (subject and body):
        flash("件名と本文を入力してください。", "error")
        return redirect(back)

    sent = failed = moved = 0
    for mid in ids:
        m = S.member_row(acc, mid)
        if not m:
            continue
        k = db.execute("SELECT exam_date FROM oh_kenshin WHERE member_id=?"
                       " AND fiscal_year=? AND kind='定期'", (mid, fy)).fetchone()
        exam = (k["exam_date"] if k else "") or ""
        sbj = fill_template(subject, m, fy, exam, deadline, url, desk)
        bdy = fill_template(body, m, fy, exam, deadline, url, desk)
        ok, note = False, "メールアドレスが未登録"
        if m["email"]:
            try:
                res = bool(H.send_mail(m["email"], sbj, bdy))
            except Exception:
                res = False
            if res:
                ok, note = True, ""
            elif not H.mail_enabled():
                # SMTPが未設定のときは outbox に控えを書き出します（本体と同じ扱い）
                ok, note = True, "SMTP未設定のため outbox に控えを保存"
            else:
                note = "送信できませんでした（送信設定をご確認ください）"
        db.execute("INSERT INTO oh_mail_log (kind, template_id, member_id, subject,"
                   " actor, result, detail) VALUES (?,?,?,?,?,?,?)",
                   (MAIL_KIND, tid, mid, sbj, S.actor(),
                    "success" if ok else "failure", note))
        sent += 1 if ok else 0
        failed += 0 if ok else 1
        # 産業医の判定結果（通常勤務・就業制限・要休業）の方へ案内を送ったら、
        # ステータスを「保健師対応中」に進めます。
        if ok:
            c = S.ensure_candidate(mid, fy)
            if c["hr_class"] in S.HR_JUDGED:
                db.execute("UPDATE oh_candidate SET hr_class=?, updated_at=?"
                           " WHERE id=?", (S.HR_NURSE, H.now(), c["id"]))
                moved += 1
    db.commit()
    # 件名・本文の中身は操作ログに残しません（個人情報が入る可能性があるため）
    H.log("master", "健康管理のメール送信", "success",
          target=f"{MAIL_KIND}（{fy}年度）",
          detail=f"対象{len(ids)}名／送信{sent}件／失敗{failed}件"
                 f"／{'個人別' if len(ids) == 1 else '一括'}")
    msg = f"メールを{sent}件送信しました。"
    if moved:
        msg += f"{moved}件のステータスを「{S.HR_NURSE}」にしました。"
    if failed:
        msg += f"{failed}件は送信できませんでした（送信履歴の理由をご確認ください）。"
    if not H.mail_enabled():
        msg += "（送信設定が未登録のため、控えを outbox に保存しました）"
    flash(msg, "ok" if sent else "error")
    return redirect(back)


@bp.route("/mail/template", methods=["POST"])
@need("health.mail")
def hm_mail_template():
    """メールのテンプレートを追加・更新する"""
    db = H.get_db()
    f = form_filter()
    back = url_for("hm.hm_list", **f_query(f))
    tid = request.form.get("id", type=int)
    name = (request.form.get("name") or "").strip()
    subject = (request.form.get("subject") or "").strip()
    body = (request.form.get("body") or "").strip()
    if not (name and subject and body):
        flash("テンプレート名・件名・本文を入力してください。", "error")
    elif tid:
        db.execute("UPDATE oh_mail_template SET name=?, subject=?, body=?, updated_at=?"
                   " WHERE id=? AND kind=?", (name, subject, body, H.now(), tid, MAIL_KIND))
        db.commit()
        H.log("master", "健康管理のメールテンプレートを更新", "success", target=name)
        flash("テンプレートを更新しました。", "ok")
    else:
        db.execute("INSERT INTO oh_mail_template (kind, name, subject, body)"
                   " VALUES (?,?,?,?)", (MAIL_KIND, name, subject, body))
        db.commit()
        H.log("master", "健康管理のメールテンプレートを追加", "success", target=name)
        flash("テンプレートを追加しました。", "ok")
    return redirect(back)


# ================================================================ マイページ
def sort_items(items):
    """検査値をよく見る順に並べ替える（残りは取込順のまま後ろへ）"""
    idx = {}
    for i, n in enumerate(ITEM_ORDER):
        idx[n] = i
    def key(it):
        n = (it["name"] or "").strip()
        for base, i in idx.items():
            if n == base or n.startswith(base):
                return (0, i)
        return (1, it["sort"] or 0)
    return sorted(items, key=key)


def page_data(mid, fy, mine=False):
    """マイページに出す情報を集める"""
    db = H.get_db()
    # 健診（年度をまたいだ履歴）
    kens = db.execute("SELECT * FROM oh_kenshin WHERE member_id=?"
                      " ORDER BY fiscal_year DESC, exam_date DESC",
                      (mid,)).fetchall()
    cur = next((k for k in kens if k["fiscal_year"] == fy and k["kind"] == "定期"), None)
    if cur is None:
        cur = next((k for k in kens if k["fiscal_year"] == fy), None)
    prev = next((k for k in kens
                 if k["fiscal_year"] == str(int(fy) - 1) and k["kind"] == "定期"), None)
    items = sort_items(db.execute(
        "SELECT * FROM oh_kenshin_item WHERE kenshin_id=? ORDER BY sort, id",
        (cur["id"],)).fetchall()) if cur else []
    prev_items = {r["name"]: r for r in db.execute(
        "SELECT * FROM oh_kenshin_item WHERE kenshin_id=?",
        (prev["id"],))} if prev else {}
    # 総合判定の推移（古い年度から並べる）
    trend = []
    for k in sorted([k for k in kens if k["kind"] == "定期"],
                    key=lambda r: r["fiscal_year"]):
        trend.append({"fy": k["fiscal_year"], "date": k["exam_date"] or "",
                      "judge": S.norm_judge(k["judge"])})
    # 時間外労働（年度内の月ごと）
    a, b = S.fy_span(fy)
    ots = db.execute("SELECT * FROM oh_overtime WHERE member_id=?"
                     " AND ym BETWEEN ? AND ? ORDER BY ym", (mid, a, b)).fetchall()
    otmax = max([{"ym": r["ym"], "hours": r["hours"] or 0} for r in ots],
                key=lambda x: x["hours"], default=None)
    # ストレスチェック（年度をまたいだ履歴）
    sts = db.execute("SELECT * FROM oh_stress WHERE member_id=?"
                     " ORDER BY fiscal_year DESC", (mid,)).fetchall()
    st = next((r for r in sts if r["fiscal_year"] == fy), None)
    # 面談の記録
    ivs = db.execute("SELECT * FROM oh_interview WHERE member_id=?"
                     " ORDER BY fiscal_year DESC, id DESC", (mid,)).fetchall()
    # 写真
    photos = db.execute("SELECT * FROM member_photo WHERE member_id=?"
                        " ORDER BY IFNULL(taken_on,''), id DESC", (mid,)).fetchall()
    # 内部メモは本人には出しません（担当者どうしの申し送りのため）
    memos = [] if mine else db.execute(
        "SELECT * FROM oh_memo WHERE member_id=? ORDER BY id DESC LIMIT ?",
        (mid, MEMO_MAX)).fetchall()
    memo_n = 0 if mine else db.execute(
        "SELECT COUNT(*) n FROM oh_memo WHERE member_id=?", (mid,)).fetchone()["n"]
    cand = db.execute("SELECT * FROM oh_candidate WHERE member_id=? AND fiscal_year=?",
                      (mid, fy)).fetchone()
    # メールのやり取り（担当者から送ったもの＋ご本人から届いた問い合わせ）
    mails = db.execute("SELECT * FROM oh_mail_log WHERE member_id=?"
                       " ORDER BY id DESC LIMIT ?", (mid, MAIL_LOG_MAX)).fetchall()
    # 対応の履歴（メモ・対応区分の変更・産業医面談を1つの時系列にまとめます）
    recs = []
    if not mine:
        for r in memos:
            recs.append({
                "at": r["created_at"], "kind": (r["kind"] or "メモ"),
                "who": r["author"] or "—", "role": r["role"] or "",
                "hr_class": r["hr_class"] or "", "body": r["body"] or "",
                "due_on": r["due_on"] or "", "follow_on": r["follow_on"] or "",
                "done_on": r["done_on"] or "", "fy": r["fiscal_year"] or "",
                "iv": None})
        for v in ivs:
            recs.append({
                "at": (v["met_on"] or v["created_at"][:10]) + " 00:00",
                "kind": "産業医面談", "who": v["doctor"] or "—", "role": "産業医",
                "hr_class": v["work_class"] or "", "body": v["findings"] or "",
                "due_on": "", "follow_on": v["next_plan"] or "", "done_on": "",
                "fy": v["fiscal_year"] or "", "iv": v})
        recs.sort(key=lambda r: r["at"], reverse=True)
    law_groups, law_others = statutory_rows(items, prev_items)
    return {"recs": recs, "rec_n": len(recs),
            "mails": mails, "kens": kens, "ken": cur, "prev": prev, "items": items,
            "law_groups": law_groups, "law_others": law_others,
            "prev_items": prev_items, "trend": trend, "ots": ots, "otmax": otmax,
            "sts": sts, "st": st, "ivs": ivs, "photos": photos, "memos": memos,
            "memo_n": memo_n, "MEMO_MAX": MEMO_MAX,
            "cand": cand, "abn": abn_count(items)}


def render_page(m, fy, mine=False):
    acc = H.current_account()
    d = page_data(m["id"], fy, mine)
    db = H.get_db()
    # ご本人のログインが発行されているか（担当者の画面に出します）
    login = None if mine else db.execute(
        "SELECT * FROM account WHERE role='member' AND (member_id=?"
        " OR lower(email)=lower(?)) AND status<>'deleted' ORDER BY id LIMIT 1",
        (m["id"], m["email"] or "")).fetchone()
    # 案内メールのテンプレート（マイページのモーダルでも使います）
    ensure_templates()
    tpls = db.execute("SELECT * FROM oh_mail_template WHERE kind=? ORDER BY id",
                      (MAIL_KIND,)).fetchall()
    # 疾患予測の内訳（担当者向け。機能「疾患予測」が使えるアカウントだけ）
    risk = None if mine else risk_detail(m)
    flu = None if mine else flu_detail(m, fy)
    return render_template(
        "health_member.html", m=m, mine=mine, age=S.age_of(m["birth"]), tpls=tpls,
        judge=(S.norm_judge(d["ken"]["judge"]) if d["ken"] else None),
        login=login, PHOTO_KINDS=H.PHOTO_KINDS, PHOTO_EXTS=sorted(H.PHOTO_TYPES),
        MAX_PHOTO_MB=H.MAX_PHOTO_MB, risk=risk, flu=flu,
        init_tab=(request.args.get("tab") or "").strip(),
        CAN_SELF_UPLOAD=H.feature_allowed("health.self_upload", acc),
        CAN_SELF_MAIL=H.feature_allowed("health.self_mail", acc),
        CAN_ACCOUNTS=H.feature_allowed("accounts", acc),
        **d, **common(fy))


def risk_detail(m):
    """加入者ひとりの疾患予測の内訳（最新の予測・健診結果の判定）。
    機能「疾患予測」が使えないアカウントには None を返し、サブタブを出さない"""
    acc = H.current_account()
    if not H.feature_allowed("risk", acc):
        return None
    db = H.get_db()
    kid = m["kenpo_id"]
    run = db.execute("SELECT * FROM risk_run WHERE kenpo_id=? ORDER BY id DESC LIMIT 1",
                     (kid,)).fetchone()
    scores = db.execute("SELECT * FROM risk_score WHERE run_id=? AND member_id=?"
                        " ORDER BY score DESC", (run["id"], m["id"])).fetchall() if run else []
    ks = db.execute("SELECT id, fiscal_year FROM kenshin_sync WHERE kenpo_id=? AND status='ok'"
                    " ORDER BY id DESC LIMIT 1", (kid,)).fetchone()
    kens = db.execute("SELECT * FROM kenshin_result WHERE sync_id=? AND member_id=? ORDER BY item",
                      (ks["id"], m["id"])).fetchall() if ks else []
    return {"run": run, "scores": scores, "ks": ks, "kens": kens,
            "top": scores[0] if scores else None}


# インフルエンザ補助：予約・接種・申請の状況。接種予約システムとの連携先が無いため、
# 加入者ごとに決まった内容を再現する（同じ加入者はいつ見ても同じ状態）
FLU_CLINICS = ["新宿クリニック", "みなと小児科医院", "東京中央病院", "大阪駅前クリニック",
               "博多総合クリニック", "さくら内科", "ひかり調剤薬局（接種会場）"]
FLU_STATUSES = ["未予約", "予約済", "接種済"]
FLU_SUBSIDY_MAX = 3000        # 助成上限（円・1回）


def flu_season(fy):
    """年度 → シーズン表記（例：2026年度 → 2026/27）"""
    try:
        y = int(str(fy)[:4])
    except ValueError:
        y = S.current_fy()
        y = int(str(y)[:4])
    return f"{y}/{str(y + 1)[2:]}", y


def flu_status_for(m, fy=None):
    """加入者ひとりの今シーズンの予約・接種・申請の状況"""
    fy = fy or S.current_fy()
    season, y = flu_season(fy)
    mid = int(m["id"])
    seed = (mid * 37 + y) % 1000
    status = FLU_STATUSES[seed % 3]
    clinic = FLU_CLINICS[seed % len(FLU_CLINICS)] if status != "未予約" else ""
    day = 1 + seed % 28
    month = 10 + (seed // 3) % 3                 # 10〜12月
    resv = f"{y}-{month:02d}-{day:02d}" if status != "未予約" else ""
    shot = resv if status == "接種済" else ""
    fee = 3500 + (seed % 5) * 300                # 接種費用（円）
    subsidy = min(FLU_SUBSIDY_MAX, fee) if status == "接種済" else 0
    applied = "申請済（支給予定）" if status == "接種済" and seed % 4 else (
        "接種済・未申請" if status == "接種済" else "—")
    rel = m["relation"] or "本人"
    # 過去シーズン（2年分）の履歴
    hist = []
    for back in (1, 2):
        sd = (mid * 37 + y - back) % 1000
        st = FLU_STATUSES[sd % 3]
        hist.append({"season": f"{y - back}/{str(y - back + 1)[2:]}", "status": st,
                     "clinic": FLU_CLINICS[sd % len(FLU_CLINICS)] if st != "未予約" else "—",
                     "subsidy": (min(FLU_SUBSIDY_MAX, 3500 + (sd % 5) * 300) if st == "接種済" else 0)})
    return {"season": season, "status": status, "clinic": clinic, "resv_date": resv,
            "shot_date": shot, "fee": fee, "subsidy": subsidy, "applied": applied,
            "rel": "本人" if rel == "本人" else "家族", "relation": rel,
            "max": FLU_SUBSIDY_MAX, "hist": hist}


def flu_detail(m, fy):
    """マイページ「インフル補助」タブの内容（機能が使えないアカウントには None）"""
    acc = H.current_account()
    if not H.feature_allowed("kenpo.influenza", acc):
        return None
    return flu_status_for(m, fy)


def self_member(acc):
    """ログイン中のアカウントに対応する加入者（本人）を返す

    ・加入者本人のログイン（role='member'）は account.member_id で特定します
    ・担当者のアカウントでも、加入者マスタのメールアドレスが一致すれば本人とみなします
    """
    db = H.get_db()
    sql = ("SELECT m.*, c.name AS company_name, o.name AS office_name,"
           " d.name AS dept_name FROM member m"
           " LEFT JOIN company c ON c.id=m.company_id"
           " LEFT JOIN office o ON o.id=m.office_id"
           " LEFT JOIN department d ON d.id=m.dept_id WHERE ")
    mid = H.account_member_id(acc)
    if mid:
        return db.execute(sql + "m.id=?", (mid,)).fetchone()
    if not acc["email"]:
        return None
    return db.execute(sql + "LOWER(IFNULL(m.email,''))=LOWER(?)"
                      " ORDER BY m.id LIMIT 1", (acc["email"],)).fetchone()


@bp.route("/api/flu/reservations")
@need("kenpo.influenza")
def hm_flu_reservations():
    """インフルエンザ補助 予約一覧の行（閲覧範囲の加入者ぶん）。
    加入者ごとに決まった予約・接種の状況を返し、行の「詳細表示」はマイページの「インフル補助」タブを開く"""
    acc = H.current_account()
    fy = (request.args.get("fy") or S.current_fy()).strip()
    rows = []
    for m in S.scoped_members(acc):
        st = flu_status_for(m, fy)
        rows.append({"member_id": m["id"], "subscriber_id": m["subscriber_id"] or "",
                     "kigo": m["cert_mark"] or "", "bango": m["member_no"] or "",
                     "name": m["name"] or "", "rel": st["rel"], "office": m["office_name"] or "",
                     "resvDate": st["resv_date"], "clinic": st["clinic"], "status": st["status"],
                     "email": m["email"] or ""})
    return {"ok": True, "fy": fy, "season": flu_season(fy)[0], "rows": rows}


# ---------------------------------------------------------------- 健診 対象者一覧・特定保健指導 対象者一覧
KL_HOSPITALS = ["新宿メディカル病院", "△△医療センター", "青山クリニック", "代々木健診センター",
                "□□健診センター"]
KL_STATUSES = ["未予約", "予約日調整中", "予約確定"]


def _fy_bounds(fy):
    y = int(str(fy)[:4])
    return y, f"{y}-04-01", f"{y + 1}-03-31"


def _fmt_slash(v):
    return (v or "").replace("-", "/")


def _member_extra(mid):
    """scoped_members に無い列（資格喪失日・除外・住所など）"""
    db = H.get_db()
    return db.execute("SELECT lost_at, excluded, birth FROM member WHERE id=?", [mid]).fetchone()


def kenshin_row_for(m, fy):
    """健康診断 代行管理「対象者一覧」の1行。受診記録があれば実績から、無ければ加入者ごとに決まった予約状況"""
    db = H.get_db()
    y, d0, d1 = _fy_bounds(fy)
    mid = int(m["id"])
    ex = _member_extra(mid)
    age = H._age_of(m["birth"])
    kk = db.execute("SELECT exam_date, judge FROM oh_kenshin WHERE member_id=? AND exam_date BETWEEN ? AND ?"
                    " ORDER BY exam_date DESC LIMIT 1", [mid, d0, d1]).fetchone()
    if not kk:
        r = db.execute("SELECT MAX(exam_date) AS exam_date FROM kenshin_result"
                       " WHERE member_id=? AND exam_date BETWEEN ? AND ?", [mid, d0, d1]).fetchone()
        kk = {"exam_date": r["exam_date"], "judge": None} if r and r["exam_date"] else None
    seed = (mid * 53 + y) % 1000
    course = ("定期健康診断" if (age is None or age < 40)
              else ("人間ドック" if seed % 2 else "生活習慣病予防健診"))
    hospital = KL_HOSPITALS[seed % len(KL_HOSPITALS)]
    if ex and ex["excluded"]:
        status, plan, actual, course, hospital = "除外", "—", "—", "—", "—"
    elif ex and (ex["lost_at"] or "").strip():
        status, plan, actual, course, hospital = "除外", "—", "—", "—", "—"
    elif kk:
        status = "結果受領済み" if kk["judge"] else "受診済み"
        actual = _fmt_slash(kk["exam_date"])
        plan = actual
    else:
        status = KL_STATUSES[seed % 3]
        if status == "未予約":
            plan, actual, hospital = "—", "—", "—"
        else:
            month = 10 + (seed // 3) % 3
            plan = f"{y}/{month:02d}/{1 + seed % 28:02d}"
            actual = "—"
    rel = m["relation"] or "本人"
    return {"member_id": mid, "subscriber_id": m["subscriber_id"] or "",
            "sig": m["cert_mark"] or "", "num": m["member_no"] or "",
            "name": m["name"] or "", "kana": m["kana"] or "", "birth": _fmt_slash(m["birth"]),
            "sex": m["sex"] or "", "status": status, "plan": plan, "actual": actual,
            "corp": m["company_name"] or "未紐づけ", "dept": m["dept_name"] or m["office_name"] or "—",
            "night": bool(m["night_work"]), "insuredType": "被保険者" if rel == "本人" else "被扶養者",
            "email": m["email"] or "", "course": course, "hospital": hospital}


HL_LEVELS = ["積極的支援", "動機付け支援"]
HL_STATUSES = ["未予約", "初回面談", "支援中", "最終評価", "中断・除外"]


def hoken_row_for(m, fy):
    """特定保健指導「対象者一覧」の1行（40〜74歳で、要注意・要医療の項目がある加入者）。
    支援区分・進み具合は加入者ごとに決まった値（支援の記録機能は本モックにはありません）"""
    db = H.get_db()
    y, d0, d1 = _fy_bounds(fy)
    mid = int(m["id"])
    ex = _member_extra(mid)
    age = H._age_of(m["birth"])
    if age is None or not (40 <= age <= 74) or (ex and (ex["lost_at"] or "").strip()):
        return None
    med = db.execute("SELECT COUNT(*) AS n FROM kenshin_result WHERE member_id=?"
                     " AND judge IN ('caution','medical')", [mid]).fetchone()["n"]
    if not med:
        return None
    seed = (mid * 71 + y) % 1000
    level = HL_LEVELS[seed % 2]
    status = HL_STATUSES[seed % 5]
    maxp = 280 if level == "積極的支援" else 20
    pts = {"未予約": 0, "初回面談": 0, "支援中": int(maxp * (0.3 + (seed % 5) / 10)),
           "最終評価": maxp, "中断・除外": int(maxp * 0.2)}[status]
    first = "---" if status in ("未予約", "初回面談") else f"{y}/{4 + seed % 3:02d}/{1 + seed % 28:02d}"
    last = ("---" if status in ("未予約", "初回面談")
            else f"{y}/{9 + seed % 3:02d}/{1 + (seed * 7) % 28:02d}")
    rel = m["relation"] or "本人"
    return {"member_id": mid, "subscriber_id": m["subscriber_id"] or "",
            "sig": m["cert_mark"] or "", "num": m["member_no"] or "",
            "name": m["name"] or "", "kana": m["kana"] or "", "sex": m["sex"] or "",
            "birth": _fmt_slash(m["birth"]), "type": "被保険者" if rel == "本人" else "被扶養者",
            "company": f"{m['company_name'] or '未紐づけ'} {m['office_name'] or '—'}",
            "level": level, "status": status, "firstDate": first, "lastDate": last,
            "points": pts, "maxPoints": maxp, "email": m["email"] or ""}


@bp.route("/api/kenshin/targets")
@need("kenpo.kenshin")
def hm_kenshin_targets():
    """健康診断 代行管理「対象者一覧」の行（閲覧範囲の加入者ぶん）。行の「詳細表示」はマイページ（健診・検査値タブ）"""
    acc = H.current_account()
    fy = (request.args.get("fy") or S.current_fy()).strip()
    rows = [kenshin_row_for(m, fy) for m in S.scoped_members(acc)]
    return {"ok": True, "fy": fy, "rows": rows}


@bp.route("/api/hoken/targets")
@need("kenpo.hoken")
def hm_hoken_targets():
    """特定保健指導「対象者一覧」の行。行の「詳細表示」はマイページ（健診・検査値タブ）"""
    acc = H.current_account()
    fy = (request.args.get("fy") or S.current_fy()).strip()
    rows = [r for r in (hoken_row_for(m, fy) for m in S.scoped_members(acc)) if r]
    return {"ok": True, "fy": fy, "rows": rows}


@bp.route("/me")
@need("health.self")
def hm_me():
    """加入者ご本人のマイページ（自分の情報・写真の取込・メールのやり取り）"""
    acc = H.current_account()
    m = self_member(acc)
    if not m:
        return render_template("health_me_none.html", email=acc["email"],
                               **common(S.current_fy()))
    fy = (request.args.get("fy") or S.current_fy()).strip()
    return render_page(m, fy, mine=True)


@bp.route("/me/photos", methods=["POST"])
@need("health.self_upload")
def hm_me_photo():
    """加入者ご本人が、自分の写真（採血結果など）を取り込む"""
    acc = H.current_account()
    m = self_member(acc)
    if not m:
        flash("ご本人の加入者情報が見つかりません。", "error")
        return redirect(url_for("hm.hm_me"))
    fy = (request.form.get("fy") or S.current_fy()).strip()
    # 取込処理は担当者と同じもの（保存先・上限・操作ログも共通）を使います
    resp = H.save_member_photos(m["id"], back=url_for("hm.hm_me", fy=fy),
                                by_self=True)
    # ご本人が検査結果を取り込んだら、ステータスを「再検査対応済み」に進めます
    db = H.get_db()
    c = S.ensure_candidate(m["id"], fy)
    if c["hr_class"] != S.HR_DONE:
        db.execute("UPDATE oh_candidate SET hr_class=?, done_on=?, updated_at=?"
                   " WHERE id=?",
                   (S.HR_DONE, H.date.today().isoformat(), H.now(), c["id"]))
        db.commit()
        H.log("master", "検査結果の取込でステータスを進めた", target=f"member#{m['id']}",
              detail=f"{fy}年度／{c['hr_class']} → {S.HR_DONE}")
        flash(f"ステータスを「{S.HR_DONE}」にしました。ご担当窓口が内容を確認します。", "ok")
    return resp


@bp.route("/me/inquiry", methods=["POST"])
@need("health.self_mail")
def hm_me_inquiry():
    """加入者ご本人からの問い合わせ（担当者の送信履歴に「本人から」として残ります）"""
    acc, db = H.current_account(), H.get_db()
    m = self_member(acc)
    fy = (request.form.get("fy") or S.current_fy()).strip()
    if not m:
        flash("ご本人の加入者情報が見つかりません。", "error")
        return redirect(url_for("hm.hm_me"))
    subject = (request.form.get("subject") or "").strip() or "健康管理へのお問い合わせ"
    body = (request.form.get("body") or "").strip()
    if not body:
        flash("お問い合わせの内容を入れてください。", "error")
        return redirect(url_for("hm.hm_me", fy=fy))
    db.execute("INSERT INTO oh_mail_log (kind, member_id, subject, actor, result,"
               " detail, direction) VALUES (?,?,?,?, 'success', ?, 'in')",
               (MAIL_KIND, m["id"], subject, m["name"], body))
    db.commit()
    H.log("master", "加入者本人からの問い合わせ", "success",
          target=f"member#{m['id']}", detail=f"{fy}年度／{len(body)}文字")
    flash("お問い合わせを送りました。ご担当窓口からの返信をお待ちください。", "ok")
    return redirect(url_for("hm.hm_me", fy=fy))


@bp.route("/<int:mid>/account", methods=["POST"])
@need("accounts")
def hm_member_account(mid):
    """この加入者の「加入者本人」ログインを発行する（案内メールも送ります）"""
    acc, db = H.current_account(), H.get_db()
    m = S.member_row(acc, mid)
    fy = (request.form.get("fy") or S.current_fy()).strip()
    back = url_for("hm.hm_member", mid=mid, fy=fy)
    if not m:
        return render_template("denied.html", path=request.path, notfound=True), 404
    email = (m["email"] or "").strip().lower()
    if not email:
        flash("加入者マスタにメールアドレスが登録されていないため発行できません。", "error")
        return redirect(back)
    row = db.execute("SELECT * FROM account WHERE lower(email)=lower(?)",
                     (email,)).fetchone()
    if row and row["status"] != "deleted":
        flash(f"{email} のアカウントはすでに登録されています"
              f"（{H.STATUS_LABELS.get(row['status'], row['status'])}）。", "error")
        return redirect(back)
    token = H.secrets.token_urlsafe(32)
    expire = (H.datetime.now() + H.timedelta(hours=H.INVITE_HOURS)
              ).strftime("%Y-%m-%d %H:%M:%S")
    try:
        if row:
            # 以前に削除したアカウントが同じメールアドレスで残っている場合は作り直します
            # （account.email は重複を許さないため、行を使い回します）
            db.execute(
                "UPDATE account SET name=?, role='member', sub_role='',"
                " view_scope='self', can_download=0, kenpo_id=?,"
                " company_id=NULL, dept_id=NULL, member_id=?, status='invited',"
                " password_hash=NULL, invite_token=?, invite_expire=?,"
                " reset_token=NULL, reset_expire=NULL, created_by=?, updated_at=?"
                " WHERE id=?",
                (m["name"], m["kenpo_id"], mid, token, expire, acc["email"],
                 H.now(), row["id"]))
        else:
            db.execute(
                "INSERT INTO account (email, name, role, sub_role, view_scope,"
                " can_download, kenpo_id, member_id, status,"
                " invite_token, invite_expire, created_by)"
                " VALUES (?,?,'member','','self',0,?,?,'invited',?,?,?)",
                (email, m["name"], m["kenpo_id"], mid, token, expire, acc["email"]))
        db.commit()
    except Exception as e:                      # 発行できない理由を画面に出します
        db.rollback()
        H.log("account", "加入者本人のログイン発行に失敗", "failure", target=email,
              detail=str(e)[:200])
        flash(f"ログインを発行できませんでした（{e}）。"
              f"アカウント管理から登録状況をご確認ください。", "error")
        return redirect(back)
    H.log("account", "加入者本人のログインを発行", "success", target=email,
          detail=f"ロール=加入者本人／閲覧範囲=ご本人のみ／member#{mid}")
    link = H.ext_url("invite", token=token)
    sent = H.send_mail(email, "【HIA】健康マイページのご案内",
                       f"{m['name']} 様\n\n"
                       "健康診断の結果などをご確認いただける「健康マイページ」を"
                       "ご利用いただけます。\n"
                       "以下のリンクからパスワードを設定してください。\n\n"
                       f"{link}\n\n"
                       f"有効期限：{expire}（{H.INVITE_HOURS}時間）\n"
                       "リンクは1回だけ使用できます。\n")
    H.log("account", "加入者本人への案内メールを送信",
          "success" if sent else "failure", target=email,
          detail="メール送信済み" if sent else "送信設定が未登録のため outbox に出力")
    # 送信設定が無いときの案内文は出さない（マイページの「案内リンクを表示」から確認できる）
    if sent:
        flash(f"{email} 宛にご案内メールを送信しました。", "ok")
    return redirect(back)


@bp.route("/<int:mid>")
# 産業医は「面談対象者一覧」からこのマイページを開きます（一覧そのものは対象外）
@need("health.view", "oh.list")
def hm_member(mid):
    """加入者マイページ（担当者向け）"""
    acc = H.current_account()
    m = S.member_row(acc, mid)
    if not m:
        return render_template("denied.html", path=request.path, notfound=True), 404
    fy = (request.args.get("fy") or S.current_fy()).strip()
    return render_page(m, fy, mine=False)


@bp.route("/ask-doctor", methods=["POST"])
@need("health.view")
def hm_ask_doctor():
    """産業医へ判定を依頼する（ステータスを「産業医依頼中」にします）

    加入者健康一覧のステータス「未判定」のボタンから使います。
    医学的な判定そのものは産業医が行い、ここでは依頼の記録だけを残します。
    """
    acc = H.current_account()
    mid = request.form.get("member_id", type=int)
    fy = (request.form.get("fy") or S.current_fy()).strip()
    back = request.form.get("back") or url_for("hm.hm_list", fy=fy)
    m = S.member_row(acc, mid) if mid else None
    if not m:
        return render_template("denied.html", path=request.path, notfound=True), 404
    if not H.feature_allowed("oh.hr_class", acc):
        flash("ステータスを変更できる権限がありません。", "error")
        return redirect(back)
    db = H.get_db()
    c = S.ensure_candidate(mid, fy)
    # 依頼しない場合は、対応を終えたものとしてステータスを対応済み（再検査対応済み）にする
    ask = (request.form.get("decision") or "ask") != "no"
    new_hc = S.HR_REQ if ask else S.HR_DONE
    db.execute("UPDATE oh_candidate SET hr_class=?, updated_at=? WHERE id=?",
               (new_hc, H.now(), c["id"]))
    db.commit()
    H.log("master", "産業医へ判定を依頼" if ask else "産業医へ依頼せず対応済みに",
          target=f"member#{mid}", detail=f"{fy}年度／{c['hr_class']} → {new_hc}")
    if ask:
        flash(f"{m['name']} さんの判定を産業医へ依頼しました（ステータス：{S.HR_REQ}）。", "ok")
    else:
        flash(f"{m['name']} さんは産業医へ依頼せず、対応済みにしました（ステータス：{S.HR_DONE}）。", "ok")
    return redirect(back)


@bp.route("/<int:mid>/hr-class", methods=["POST"])
@need("health.view")
def hm_hr_class(mid):
    """マイページから対応区分（人事）を変更する

    面談対象者一覧・加入者健康一覧のステータスと同じ区分（`HR_CLASSES`）です。
    医学的判断（承認・就業区分）は産業医のみで、ここでは変更できません。
    """
    acc = H.current_account()
    m = S.member_row(acc, mid)
    if not m:
        return render_template("denied.html", path=request.path, notfound=True), 404
    fy = (request.form.get("fy") or S.current_fy()).strip()
    back = url_for("hm.hm_member", mid=mid, fy=fy)
    if not H.feature_allowed("oh.hr_class", acc):
        H.log("master", "対応区分の入力を無視", "blocked", target=f"member#{mid}",
              detail=f"role={H.role_key(acc)}／機能=oh.hr_class")
        flash("対応区分を変更できる権限がありません。", "error")
        return redirect(back)
    hc = (request.form.get("hr_class") or "").strip()
    if hc not in HR_CLASSES:
        flash("対応区分を選んでください。", "error")
        return redirect(back)
    db = H.get_db()
    c = S.ensure_candidate(mid, fy)
    # 対応期限・次回フォロー予定日・対応完了日（空欄にすると消せます）
    sets = ["hr_class=?"]
    params = [hc]
    dates = []
    for col in ("due_on", "follow_on", "done_on"):
        if col in request.form:
            v = (request.form.get(col) or "").strip()
            sets.append(f"{col}=?")
            params.append(v or None)
            if v != (c[col] or ""):
                dates.append(col)
    sets.append("updated_at=?")
    params.append(H.now())
    db.execute(f"UPDATE oh_candidate SET {','.join(sets)} WHERE id=?",
               params + [c["id"]])
    db.commit()
    H.log("master", "対応区分を変更", target=f"member#{mid}",
          detail=f"{fy}年度／{c['hr_class']} → {hc}"
                 + (f"／日付{len(dates)}件" if dates else ""))
    if hc != c["hr_class"]:
        flash(f"対応区分を「{hc}」にしました。", "ok")
    elif dates:
        flash("日付を保存しました。", "ok")
    else:
        flash("変更はありませんでした。", "ok")
    return redirect(back)


@bp.route("/<int:mid>/record", methods=["POST"])
# 産業医は「医学的意見」を記録するために使います（面談対象者一覧の権限で入れる）
@need("health.view", "oh.list")
def hm_record(mid):
    """対応の履歴に1件のこす（メモ／対応区分の変更をレコードとして記録します）

    対応区分・対応期限・次回フォロー予定日・対応完了日を入れると、
    いまの状態（`oh_candidate`）にも反映します。
    """
    acc = H.current_account()
    m = S.member_row(acc, mid)
    if not m:
        return render_template("denied.html", path=request.path, notfound=True), 404
    fy = (request.form.get("fy") or S.current_fy()).strip()
    back = url_for("hm.hm_member", mid=mid, fy=fy)
    kind = (request.form.get("kind") or "メモ").strip()
    body = (request.form.get("body") or "").strip()
    hc = (request.form.get("hr_class") or "").strip()
    due = (request.form.get("due_on") or "").strip()
    fol = (request.form.get("follow_on") or "").strip()
    done = (request.form.get("done_on") or "").strip()
    booked = (request.form.get("booked_on") or "").strip()
    can_hr = H.feature_allowed("oh.hr_class", acc)
    # 記録の種類はロール別の業務フローに合わせる（使えない種類はメモとして残す）
    if kind not in RECORD_KINDS:
        kind = "メモ"
    if kind in ("対応区分", "就業上の措置", "面談日程") and not can_hr:
        kind = "メモ"
    if kind == "医学的意見" and not S.is_doctor(acc):
        kind = "メモ"
    if kind != "面談日程":
        booked = ""
    elif not booked:
        flash("面談予定日を入れてください。", "error")
        return redirect(back)
    # フォロー完了：対応完了日が空なら今日。保健師対応中の方は再検査対応済み（対応を終えた）にする
    c0 = S.ensure_candidate(mid, fy)
    if kind == "フォロー完了":
        done = done or H.date.today().isoformat()
        if not hc and c0["hr_class"] == S.HR_NURSE:
            hc = S.HR_DONE
    if hc and not can_hr:
        # 対応区分は人事（運用事務）の項目。使えないロールの入力は記録して無視します。
        H.log("master", "対応区分の入力を無視", "blocked", target=f"member#{mid}",
              detail=f"role={H.role_key(acc)}／機能=oh.hr_class")
        hc = ""
    if hc and hc not in HR_CLASSES:
        hc = ""
    if not body and not hc and not (due or fol or done or booked):
        flash("記録する内容を入れてください（メモ・対応区分・日付のいずれか）。", "error")
        return redirect(back)
    if kind == "対応区分" and not hc:
        kind = "メモ"
    db = H.get_db()
    c = S.ensure_candidate(mid, fy)
    # いまの状態に反映（入力したものだけ）
    sets, params = [], []
    if hc:
        sets.append("hr_class=?")
        params.append(hc)
    for col, v in (("due_on", due), ("follow_on", fol), ("done_on", done),
                   ("booked_on", booked)):
        if v:
            sets.append(f"{col}=?")
            params.append(v)
    # 面談日程を登録したら、面談のステータスを「面談予約済」に進める（案内済み・承認済みの方）
    if booked and c["status"] in (S.ST_APPROVED, S.ST_MAILED):
        sets.append("status=?")
        params.append(S.ST_BOOKED)
    if sets:
        sets.append("updated_at=?")
        params.append(H.now())
        db.execute(f"UPDATE oh_candidate SET {','.join(sets)} WHERE id=?",
                   params + [c["id"]])
    db.execute("INSERT INTO oh_memo (member_id, fiscal_year, kind, hr_class,"
               " due_on, follow_on, done_on, body, author, role)"
               " VALUES (?,?,?,?,?,?,?,?,?,?)",
               (mid, fy, kind, hc or None, due or None, fol or None, done or None,
                body or ("面談予定日を " + booked + " にしました。" if booked
                         else "対応区分を「" + hc + "」にしました。" if hc
                         else "フォローを完了しました。" if kind == "フォロー完了"
                         else "日付を更新しました。"),
                S.actor(), S.role_label()))
    db.commit()
    H.log("master", "対応の履歴を追加", target=f"member#{mid}",
          detail=f"{fy}年度／{kind}"
                 + (f"／対応区分 {c['hr_class']} → {hc}" if hc else "")
                 + (f"／{len(body)}文字" if body else ""))
    flash("対応の履歴に記録しました。"
          + (f"ステータスを「{hc}」にしました。" if hc else ""), "ok")
    return redirect(back)


@bp.route("/<int:mid>/memo", methods=["POST"])
@need("health.view")
def hm_memo(mid):
    """マイページからメモを追記する（担当者どうしの申し送り）"""
    acc = H.current_account()
    m = S.member_row(acc, mid)
    if not m:
        return render_template("denied.html", path=request.path, notfound=True), 404
    fy = (request.form.get("fy") or S.current_fy()).strip()
    body = (request.form.get("body") or "").strip()
    if not body:
        flash("メモの内容を入れてください。", "error")
    else:
        db = H.get_db()
        db.execute("INSERT INTO oh_memo (member_id, fiscal_year, body, author, role)"
                   " VALUES (?,?,?,?,?)",
                   (mid, fy, body, S.actor(), S.role_label()))
        db.commit()
        H.log("master", "健康管理のメモを追記", target=f"member#{mid}",
              detail=f"{fy}年度／{len(body)}文字")
        flash("メモを追記しました。", "ok")
    return redirect(url_for("hm.hm_member", mid=mid, fy=fy))
