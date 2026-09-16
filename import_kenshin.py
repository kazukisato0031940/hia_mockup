# -*- coding: utf-8 -*-
"""健診結果XMLを取込む（予約管理システムからの夜間バッチ用）

  python import_kenshin.py <保険者番号> <XMLのあるフォルダ> [対象年度]

例）
  python import_kenshin.py 6139166 D:\\kenshin\\2026 2026

画面からは実行しません。Windowsのタスクスケジューラから、
予約管理システムがファイルを置くフォルダを指定して呼び出してください。
取込んだ内容は「疾患予測」の予測結果一覧に反映されます。
"""
import io
import os
import sys


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        return 1
    code = sys.argv[1].strip()
    folder = sys.argv[2]
    fy = sys.argv[3].strip() if len(sys.argv) > 3 else None

    if not os.path.isdir(folder):
        print(f"フォルダが見つかりません：{folder}")
        return 1

    base = os.path.dirname(os.path.abspath(__file__))
    sys.path.insert(0, base)
    os.environ.setdefault("HIA_SECRET_KEY", "batch")
    from app import app, get_db, import_kenshin_xml

    files = sorted(f for f in os.listdir(folder) if f.lower().endswith(".xml"))
    if not files:
        print(f"XMLファイルがありません：{folder}")
        return 1

    texts = []
    for name in files:
        try:
            texts.append((name, io.open(os.path.join(folder, name),
                                        encoding="utf-8", errors="replace").read()))
        except OSError as e:
            print(f"  読み込めません：{name}（{e}）")

    with app.app_context():
        db = get_db()
        row = db.execute("SELECT id, name FROM kenpo WHERE code=?", (code,)).fetchone()
        if not row:
            print(f"保険者番号 {code} の健康保険組合が登録されていません。")
            return 1
        print(f"取込先：{row['name']}（{code}）")
        r = import_kenshin_xml(row["id"], texts, fiscal_year=fy, actor="batch")

    print(f"  ファイル      : {r['files']} 件")
    print(f"  取込んだ加入者 : {r['matched']} 名")
    if r["unmatched"]:
        print(f"  突合できず    : {r['unmatched']} 件（被保険者証が一致しません）")
    if r["ng"]:
        print(f"  読み取れず    : {r['ng']} 件")
    return 0 if r["matched"] else 1


if __name__ == "__main__":
    sys.exit(main())
