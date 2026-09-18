-- ================================================================
-- 産業医面談管理（労働安全衛生法に基づく健診・長時間労働・ストレスチェック）
--   別システム「産業医面談管理システム」の機能を本システムへ組み込むための追加分。
--   加入者マスタ（member）・企業・事業所・部署は本システムのマスタをそのまま使う。
--   画面のデモデータはすべて架空のものとし、実在の個人情報は保持しない。
-- ================================================================

-- 健康診断の受診記録（総合判定つき）。定期健診と深夜健診を kind で分ける。
CREATE TABLE IF NOT EXISTS oh_kenshin (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  member_id   INTEGER NOT NULL REFERENCES member(id),
  fiscal_year TEXT NOT NULL,                   -- 受診年度（4月〜翌3月）
  kind        TEXT NOT NULL DEFAULT '定期',    -- 定期 / 深夜
  exam_date   TEXT,                            -- 受診日
  judge       TEXT,                            -- 総合判定 A/B/C/D/E（学会区分）
  findings    TEXT,                            -- 産業医所見・特記事項
  source      TEXT,                            -- csv / kenshin_result（既存連携から生成）
  created_at  TEXT NOT NULL DEFAULT (datetime('now','localtime')),
  updated_at  TEXT,
  UNIQUE (member_id, fiscal_year, kind)
);
CREATE INDEX IF NOT EXISTS ix_oh_kenshin_m  ON oh_kenshin (member_id);
CREATE INDEX IF NOT EXISTS ix_oh_kenshin_fy ON oh_kenshin (fiscal_year);

-- 健診結果票の明細（前年比較つき）
CREATE TABLE IF NOT EXISTS oh_kenshin_item (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  kenshin_id INTEGER NOT NULL REFERENCES oh_kenshin(id),
  name       TEXT NOT NULL,                    -- 検査項目名
  value      TEXT,                             -- 今回の値
  unit       TEXT,
  judge      TEXT,                             -- 項目ごとの判定 A〜E
  prev_value TEXT,                             -- 前年の値
  sort       INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_oh_kitem_k ON oh_kenshin_item (kenshin_id);

-- 月次の時間外労働（過重労働）
CREATE TABLE IF NOT EXISTS oh_overtime (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  member_id      INTEGER NOT NULL REFERENCES member(id),
  ym             TEXT NOT NULL,                -- 対象年月（2026-04）
  hours          REAL NOT NULL DEFAULT 0,      -- 時間外労働時間
  holiday_hours  REAL NOT NULL DEFAULT 0,      -- うち休日労働
  created_at     TEXT NOT NULL DEFAULT (datetime('now','localtime')),
  UNIQUE (member_id, ym)
);
CREATE INDEX IF NOT EXISTS ix_oh_ot_m ON oh_overtime (member_id);

-- ストレスチェックの結果（高ストレス者判定・点数）
CREATE TABLE IF NOT EXISTS oh_stress (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  member_id   INTEGER NOT NULL REFERENCES member(id),
  fiscal_year TEXT NOT NULL,
  exam_date   TEXT,
  score       INTEGER,                          -- 合計点
  high        INTEGER NOT NULL DEFAULT 0,       -- 1＝高ストレス者
  applied     INTEGER NOT NULL DEFAULT 0,       -- 1＝本人から面接指導の申出あり
  created_at  TEXT NOT NULL DEFAULT (datetime('now','localtime')),
  UNIQUE (member_id, fiscal_year)
);
CREATE INDEX IF NOT EXISTS ix_oh_stress_m ON oh_stress (member_id);

-- 面談候補（自動抽出された対象者と、その後のワークフロー）
CREATE TABLE IF NOT EXISTS oh_candidate (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  member_id      INTEGER NOT NULL REFERENCES member(id),
  fiscal_year    TEXT NOT NULL,
  reasons        TEXT,                          -- 抽出理由（複数該当は併記）
  status         TEXT NOT NULL DEFAULT '産業医確認待ち',
  work_class     TEXT,                          -- 就業区分（通常勤務／就業制限／要休業）産業医のみ
  hr_class       TEXT NOT NULL DEFAULT '未判定',-- 対応区分（人事）
  exclude_reason TEXT,                          -- 面談不要（対象外）の理由
  approved_by    TEXT,
  approved_at    TEXT,
  mail_count     INTEGER NOT NULL DEFAULT 0,    -- 勧奨メールの送信回数
  last_mail_at   TEXT,
  due_on         TEXT,                          -- 対応期限（判定期限・受診の報告期限など）
  done_on        TEXT,                          -- 対応完了日
  follow_on      TEXT,                          -- 次回フォロー予定日
  booked_on      TEXT,                          -- 面談予定日（人事が日程を調整して登録）
  updated_at     TEXT,
  UNIQUE (member_id, fiscal_year)
);
CREATE INDEX IF NOT EXISTS ix_oh_cand_fy ON oh_candidate (fiscal_year);

-- メモ履歴（産業医・人事の双方が追記。記入者・ロール・日時つき）
CREATE TABLE IF NOT EXISTS oh_memo (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  member_id   INTEGER NOT NULL REFERENCES member(id),
  fiscal_year TEXT,
  kind        TEXT NOT NULL DEFAULT 'メモ',     -- 記録の種類（メモ／対応区分の変更）
  hr_class    TEXT,                             -- そのとき設定した対応区分
  due_on      TEXT,                             -- 対応期限
  done_on     TEXT,                             -- 対応完了日
  follow_on   TEXT,                             -- 次回フォロー予定日
  body        TEXT NOT NULL,
  author      TEXT,
  role        TEXT,
  created_at  TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS ix_oh_memo_m ON oh_memo (member_id);

-- 面談記録（産業医のみ登録）
CREATE TABLE IF NOT EXISTS oh_interview (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  member_id   INTEGER NOT NULL REFERENCES member(id),
  fiscal_year TEXT NOT NULL,
  met_on      TEXT,                             -- 面談日
  method      TEXT,                             -- 対面 / オンライン / 電話
  purpose     TEXT,                             -- 健診有所見 / 長時間労働 / 高ストレス など
  work_class  TEXT,                             -- 就業区分
  measure     TEXT,                             -- 事後措置
  findings    TEXT,                             -- 産業医所見
  next_plan   TEXT,                             -- 次回予定
  overtime    REAL,                             -- 面談時点の直近の時間外労働
  doctor      TEXT,                             -- 記録した産業医
  created_at  TEXT NOT NULL DEFAULT (datetime('now','localtime')),
  updated_at  TEXT,
  UNIQUE (member_id, fiscal_year)
);
CREATE INDEX IF NOT EXISTS ix_oh_iv_fy ON oh_interview (fiscal_year);

-- 配信テンプレート（面談受診勧奨／ストレスチェック案内）
CREATE TABLE IF NOT EXISTS oh_mail_template (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  kind       TEXT NOT NULL,                     -- 面談受診勧奨 / ストレスチェック案内
  name       TEXT NOT NULL,
  subject    TEXT NOT NULL,
  body       TEXT NOT NULL,
  updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

-- 配信履歴
CREATE TABLE IF NOT EXISTS oh_mail_log (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  kind        TEXT NOT NULL,
  template_id INTEGER,
  member_id   INTEGER REFERENCES member(id),
  subject     TEXT,
  sent_at     TEXT NOT NULL DEFAULT (datetime('now','localtime')),
  actor       TEXT,
  result      TEXT NOT NULL DEFAULT 'success',  -- success / skipped / failure
  detail      TEXT,
  -- out＝担当者から加入者へ送ったもの／in＝加入者本人から届いた問い合わせ
  direction   TEXT NOT NULL DEFAULT 'out'
);
CREATE INDEX IF NOT EXISTS ix_oh_mail_log_m ON oh_mail_log (member_id);

-- ストレスチェックの実施記録（キャンペーン）
CREATE TABLE IF NOT EXISTS oh_sc_campaign (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  fiscal_year TEXT NOT NULL,
  period_from TEXT,
  period_to   TEXT,
  deadline    TEXT,
  sent        INTEGER NOT NULL DEFAULT 0,
  actor       TEXT,
  created_at  TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

-- ストレスチェック設問マスタ
CREATE TABLE IF NOT EXISTS oh_sc_question (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  category   TEXT NOT NULL,                     -- 仕事のストレス要因 など
  no         INTEGER NOT NULL DEFAULT 0,
  body       TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

-- 報告書への産業医の記名・サイン
CREATE TABLE IF NOT EXISTS oh_sign (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  fiscal_year TEXT NOT NULL,
  kind        TEXT NOT NULL,                    -- kenshin（様式第6号）/ stress
  name        TEXT NOT NULL,                    -- 産業医氏名
  title       TEXT,                             -- 所属・資格
  signed_at   TEXT NOT NULL DEFAULT (datetime('now','localtime')),
  account_id  INTEGER REFERENCES account(id),
  UNIQUE (fiscal_year, kind)
);

-- 一覧の表示列（ロール別に保存する）
CREATE TABLE IF NOT EXISTS oh_view_col (
  role   TEXT NOT NULL,
  screen TEXT NOT NULL,
  cols   TEXT NOT NULL,
  PRIMARY KEY (role, screen)
);
