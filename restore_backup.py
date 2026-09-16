# -*- coding: utf-8 -*-
"""backup フォルダーの控えから hia.db を復元する。

起動時に作られる控え（backup/hia_YYYYMMDD_HHMMSS.db）を一覧で表示し、
選んだ控えを hia.db に戻します。いまの hia.db は消さずに
backup/hia_before_restore_YYYYMMDD_HHMMSS.db として残します。

使い方:
  python restore_backup.py          … 控えの一覧を出して選ぶ
  python restore_backup.py --latest … いちばん新しい控えに戻す
"""
import glob
import os
import shutil
import sys
from datetime import datetime

BASE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(BASE, "hia.db")
BACKUP_DIR = os.path.join(BASE, "backup")


def backups():
    return sorted(glob.glob(os.path.join(BACKUP_DIR, "hia_*.db")), reverse=True)


def show(files):
    print("=" * 62)
    print("控え（backup フォルダー）の一覧　新しいものが上です")
    print("=" * 62)
    for i, f in enumerate(files, start=1):
        st = os.stat(f)
        print(f"  {i:2d}. {os.path.basename(f):40s}"
              f" {st.st_size / 1024:7.0f} KB"
              f"  {datetime.fromtimestamp(st.st_mtime):%Y-%m-%d %H:%M}")
    print()


def main():
    files = backups()
    if not files:
        print("backup フォルダーに控えがありません。")
        print("（控えは起動時に1日1回作られます。最新10世代を保持します）")
        return 1
    latest = "--latest" in sys.argv
    show(files)
    if latest:
        pick = files[0]
    else:
        ans = input(f"戻す控えの番号を入れてください（1〜{len(files)}／中止は Enter）： ").strip()
        if not ans.isdigit() or not (1 <= int(ans) <= len(files)):
            print("中止しました。")
            return 0
        pick = files[int(ans) - 1]

    if os.path.exists(DB):
        keep = os.path.join(BACKUP_DIR,
                            f"hia_before_restore_{datetime.now():%Y%m%d_%H%M%S}.db")
        shutil.copy2(DB, keep)
        print(f"いまの hia.db を控えました： backup/{os.path.basename(keep)}")
    shutil.copy2(pick, DB)
    print(f"復元しました： {os.path.basename(pick)} → hia.db")
    print("アプリを起動し直してから、画面で内容を確認してください。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
