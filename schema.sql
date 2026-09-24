-- HIA アカウント・マスタ管理（HIA総合管理／HIA健保管理 共通データベース）
PRAGMA foreign_keys = ON;

-- 健康保険組合
CREATE TABLE IF NOT EXISTS kenpo (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  code       TEXT NOT NULL UNIQUE,
  name       TEXT NOT NULL,
  -- 健保ごとに使える機能（一覧のトグルで切り替える）
  publish_auth  INTEGER NOT NULL DEFAULT 0,   -- 公開権限
  kenshin_auth  INTEGER NOT NULL DEFAULT 0,   -- 健診代行権限
  guidance_auth INTEGER NOT NULL DEFAULT 0,   -- 保健指導権限
  flu_enabled   INTEGER NOT NULL DEFAULT 0,   -- インフル機能
  n_hospital    INTEGER NOT NULL DEFAULT 0,   -- 登録医療機関数
  -- 加入者向けサイトの本人確認（認証）で使う項目のパターン
  --   A：被保険者記号・被保険者番号・カナ・生年月日・性別
  --   B：被保険者番号・カナ・生年月日・性別（記号を使わない組合）
  auth_pattern  TEXT NOT NULL DEFAULT 'A',
  -- 加入者向けサイト（クローズサイト）に表示する同意文（本文・同意必須 の項目の JSON。空なら表示しない）
  consent_text  TEXT NOT NULL DEFAULT '',
  kenshin_site_start TEXT,                     -- 健診代行：サイト公開期間（開始日）
  kenshin_site_end   TEXT,                     -- 健診代行：サイト公開期間（終了日）
  flu_site_start     TEXT,                     -- インフル補助：サイト公開期間（開始日）
  flu_site_end       TEXT,                     -- インフル補助：サイト公開期間（終了日）
  created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

-- 企業（企業コードはシステムが自動採番）
CREATE TABLE IF NOT EXISTS company (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  kenpo_id   INTEGER NOT NULL REFERENCES kenpo(id),
  ext_code   TEXT,                    -- 企業コード（健保が管理する番号）。取込の照合キー
  code       TEXT NOT NULL,           -- 当社内部コード。健保名＋企業名から自動生成
  name       TEXT NOT NULL,
  kana       TEXT,
  cert_mark  TEXT,                    -- 被保険者証記号
  zip        TEXT,                    -- 郵便番号
  tel        TEXT,
  address    TEXT,
  email      TEXT,                    -- 担当メールアドレス
  created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
  updated_at TEXT,
  UNIQUE (kenpo_id, code),
  UNIQUE (kenpo_id, ext_code),
  UNIQUE (kenpo_id, name)
);

-- 事業所（事業所コードはシステムが自動採番）
CREATE TABLE IF NOT EXISTS office (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  company_id INTEGER NOT NULL REFERENCES company(id),
  ext_code   TEXT,                    -- 事業所コード（企業が管理する番号）。取込の照合キー
  code       TEXT NOT NULL,           -- 当社内部コード。登録順に自動発番
  name       TEXT NOT NULL,           -- 事業所名
  kana       TEXT,                    -- 部署名（フリガナ）
  zip        TEXT,                    -- 郵便番号
  tel        TEXT,
  address    TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
  updated_at TEXT,
  UNIQUE (company_id, code),
  UNIQUE (company_id, ext_code),
  UNIQUE (company_id, name)
);

-- アカウントの担当範囲（企業・事業所・部署を跨いで持てる）
CREATE TABLE IF NOT EXISTS account_scope (
  account_id INTEGER NOT NULL REFERENCES account(id),
  kind       TEXT NOT NULL,           -- 'company' / 'office' / 'dept'
  ref_id     INTEGER NOT NULL,
  UNIQUE (account_id, kind, ref_id)
);

-- 部署（事業所の下）
CREATE TABLE IF NOT EXISTS department (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  office_id  INTEGER NOT NULL REFERENCES office(id),
  ext_code   TEXT,                    -- 部署コード（企業が管理する番号）
  code       TEXT NOT NULL,           -- 当社内部コード。登録順に自動発番
  name       TEXT NOT NULL,
  kana       TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
  updated_at TEXT,
  UNIQUE (office_id, code),
  UNIQUE (office_id, ext_code),
  UNIQUE (office_id, name)
);

-- 加入者
CREATE TABLE IF NOT EXISTS member (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  kenpo_id     INTEGER NOT NULL REFERENCES kenpo(id),
  company_id   INTEGER REFERENCES company(id),  -- あとから紐づける運用のため任意
  office_id    INTEGER REFERENCES office(id),   -- 事業所コードが空の場合は未設定
  dept_id      INTEGER REFERENCES department(id),  -- 部署（事業所の下）。任意
  member_no    TEXT NOT NULL,                  -- 被保険者証番号
  cert_mark    TEXT,                            -- 被保険者証記号
  cert_branch  TEXT,                            -- 被保険者証枝番
  attr         TEXT,                            -- 被保険者属性名
  relation     TEXT,                            -- 続柄名称
  name         TEXT NOT NULL,                   -- 対象者氏名（漢字）
  kana         TEXT,                            -- 対象者氏名（カナ）
  sex          TEXT,
  birth        TEXT,
  qualified_at TEXT,                            -- 資格取得日（家族認定日）
  lost_at      TEXT,                            -- 資格喪失日（家族削除日）
  zip          TEXT,
  pref         TEXT,                            -- 都道府県
  city         TEXT,                            -- 市区町村
  address      TEXT,                            -- 住所（番地など）
  address2     TEXT,                            -- 建物名
  tel          TEXT,
  email        TEXT,
  billing_code TEXT,                            -- 請求先コード
  employee_code TEXT,                           -- 社員番号
  kenpo_member_id TEXT,                         -- 健保別加入者管理ID（健保が独自に管理する番号）
  subscriber_id TEXT,                           -- 加入者ID（当システムで採番）
  src_company_code TEXT,                        -- 取込時の企業コード
  src_office_code  TEXT,                        -- 取込時の事業所コード
  memo         TEXT,                            -- メモ（加入者ごとの申し送り。画面で編集）
  night_work   INTEGER NOT NULL DEFAULT 0,      -- 深夜業従事（1＝深夜健診の対象）
  excluded     INTEGER NOT NULL DEFAULT 0,      -- 健診の対象から除外（1＝除外）
  created_at   TEXT NOT NULL DEFAULT (datetime('now','localtime')),
  updated_at   TEXT,
  -- 本人と家族は同じ被保険者証番号で枝番が異なるため、枝番まで含めて一意にする
  UNIQUE (kenpo_id, cert_mark, member_no, cert_branch)
);

-- アカウント
-- role      : system_admin（当社スタッフ）/ kenpo_user（健保担当者）/ company_user（企業担当者）
-- sub_role  : 企業担当者のサブロール。doctor（産業医）/ hr（人事）/ 空（通常）
-- view_scope: all（全健保）/ kenpo_all（自組合全体）/ own_company（自社のみ）
CREATE TABLE IF NOT EXISTS account (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  email         TEXT NOT NULL UNIQUE,
  name          TEXT NOT NULL,
  role          TEXT NOT NULL,
  -- 企業担当者のサブロール（''／doctor＝産業医／hr＝人事）。産業医面談管理で使う
  sub_role      TEXT NOT NULL DEFAULT '',
  -- 加入者本人（role='member'）のログインが、どの加入者のものかを指す
  member_id     INTEGER REFERENCES member(id),
  view_scope    TEXT NOT NULL,
  can_download  INTEGER NOT NULL DEFAULT 0,
  is_primary    INTEGER NOT NULL DEFAULT 0,   -- （旧・代表者アカウントの区分。画面からは廃止。互換のため列だけ残す）
  kenpo_id      INTEGER REFERENCES kenpo(id),
  company_id    INTEGER REFERENCES company(id),
  dept_id       INTEGER REFERENCES department(id),   -- 部署の管理者の場合
  status        TEXT NOT NULL DEFAULT 'invited',   -- invited / active / disabled / deleted
  password_hash TEXT,
  invite_token  TEXT,
  invite_expire TEXT,
  reset_token   TEXT,
  reset_expire  TEXT,
  reset_at      TEXT,
  created_by    TEXT,
  created_at    TEXT NOT NULL DEFAULT (datetime('now','localtime')),
  updated_at    TEXT,
  activated_at  TEXT,
  last_login_at TEXT
);

-- アカウントが対象とする企業（複数選択可）
-- 企業担当者は、ここに登録された企業の加入者だけを閲覧・操作できる
CREATE TABLE IF NOT EXISTS account_company (
  account_id INTEGER NOT NULL REFERENCES account(id),
  company_id INTEGER NOT NULL REFERENCES company(id),
  PRIMARY KEY (account_id, company_id)
);

-- 加入者ごとの写真（採血結果などの画像。個人ごとに取り込み、加入者マスタで閲覧する）
-- ファイル本体は uploads/member_photos/<member_id>/ に保存し、ここには場所と情報だけ持つ
CREATE TABLE IF NOT EXISTS member_photo (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  member_id   INTEGER NOT NULL REFERENCES member(id),
  kind        TEXT,                             -- 採血結果／健診結果票／その他
  filename    TEXT NOT NULL,                    -- 保存したファイル名
  orig_name   TEXT,                             -- 取り込んだときのファイル名
  mime        TEXT,
  bytes       INTEGER NOT NULL DEFAULT 0,
  taken_on    TEXT,                             -- 検査日・撮影日（任意）
  note        TEXT,                             -- 補足
  uploaded_by TEXT,
  created_at  TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_mphoto_member ON member_photo(member_id);

-- 判定マスタ（検査項目）
-- 判定基準は「健保共通」と「企業ごと」の2段で持ちます（企業ごとの設定が優先）
CREATE TABLE IF NOT EXISTS judge_item (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  code       TEXT NOT NULL UNIQUE,             -- 項目コード
  name       TEXT NOT NULL,                    -- 検査項目名
  unit       TEXT,
  sort       INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

-- 判定基準（判定区分・性別・下限値・上限値の組）
CREATE TABLE IF NOT EXISTS judge_criteria (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  kenpo_id   INTEGER NOT NULL REFERENCES kenpo(id),
  company_id INTEGER REFERENCES company(id),   -- NULL＝健保共通の基準
  item_id    INTEGER NOT NULL REFERENCES judge_item(id),
  fiscal_year TEXT NOT NULL,                   -- 適用年度（2026 など）
  judge      TEXT NOT NULL,                    -- A〜E
  sex        TEXT NOT NULL DEFAULT '共通',     -- 共通／男性／女性
  lo         TEXT,                             -- 下限値（以上）。空欄＝下限なし
  hi         TEXT,                             -- 上限値（未満）。空欄＝上限なし
  sort       INTEGER NOT NULL DEFAULT 0,
  updated_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_jc_key
  ON judge_criteria(kenpo_id, company_id, item_id, fiscal_year);

-- 定期健康診断結果報告書（様式第6号）の報告項目
-- eGov「定期健康診断結果報告書 仕様書」の項目に合わせています。
-- 人数は健診結果から集計するため、ここには集計できない情報（事業場・機関・産業医など）を持ちます
CREATE TABLE IF NOT EXISTS form6_report (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  kenpo_id    INTEGER NOT NULL REFERENCES kenpo(id),
  company_id  INTEGER REFERENCES company(id),   -- 事業場（企業）。NULL＝健保全体
  target_year TEXT NOT NULL,                    -- 対象年（報告対象年）
  labor_insurance_no TEXT,                      -- 労働保険番号
  industry_type      TEXT,                      -- 事業の種類
  workplace_name     TEXT,                      -- 事業場の名称
  workplace_zip      TEXT,                      -- 事業場の郵便番号
  workplace_address  TEXT,                      -- 事業場の所在地
  workplace_tel      TEXT,                      -- 事業場の電話番号
  report_count       TEXT,                      -- 報告回目
  examination_date   TEXT,                      -- 健診年月日
  institution_name   TEXT,                      -- 健康診断実施機関名
  institution_address TEXT,                     -- 健康診断実施機関所在地
  employees_count    INTEGER,                   -- 在籍労働者数（未入力なら加入者数）
  physician_name     TEXT,                      -- 産業医氏名
  physician_address  TEXT,                      -- 産業医所在地
  employer_name_title TEXT,                     -- 事業者職氏名
  updated_at  TEXT,
  UNIQUE (kenpo_id, company_id, target_year)
);

-- 操作ログ（両画面のログを1つに集約。追記のみ）
CREATE TABLE IF NOT EXISTS audit_log (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  ts          TEXT NOT NULL DEFAULT (datetime('now','localtime')),
  shell       TEXT,               -- km（HIA総合管理）/ kenpo（HIA健保管理）
  category    TEXT NOT NULL,      -- auth / account / master / import / download / view
  action      TEXT NOT NULL,
  result      TEXT NOT NULL,      -- success / failure / blocked
  actor_email TEXT,
  actor_id    INTEGER,
  actor_role  TEXT,
  kenpo_id    INTEGER,
  ip          TEXT,
  target      TEXT,
  detail      TEXT,
  path        TEXT                -- 操作した画面のURL（ディレクトリ）
);

CREATE TRIGGER IF NOT EXISTS audit_log_no_update
BEFORE UPDATE ON audit_log
BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;

CREATE TRIGGER IF NOT EXISTS audit_log_no_delete
BEFORE DELETE ON audit_log
BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;

CREATE TABLE IF NOT EXISTS setting (
  key TEXT PRIMARY KEY, value TEXT NOT NULL
);

-- 機能制御（ロール・サブロールごとに使える機能を切り替える）
-- role_key: system_admin / kenpo_user / company_user / company_user/doctor
--           / company_user/hr
-- feature : master.write・oh.mail などの機能キー（app.py の FEATURES）
-- 行が無い機能は app.py の既定値（FEATURE_DEFAULTS）に従う
CREATE TABLE IF NOT EXISTS role_feature (
  role_key TEXT NOT NULL,
  feature  TEXT NOT NULL,
  allowed  INTEGER NOT NULL DEFAULT 1,
  PRIMARY KEY (role_key, feature)
);

CREATE INDEX IF NOT EXISTS idx_log_ts     ON audit_log(ts);
CREATE INDEX IF NOT EXISTS idx_log_actor  ON audit_log(actor_email);
CREATE INDEX IF NOT EXISTS idx_log_cat    ON audit_log(category);
CREATE INDEX IF NOT EXISTS idx_log_shell  ON audit_log(shell);
CREATE INDEX IF NOT EXISTS idx_member_off ON member(office_id);
CREATE INDEX IF NOT EXISTS idx_office_cmp ON office(company_id);
CREATE INDEX IF NOT EXISTS idx_ac_company  ON account_company(company_id);

-- ================================================================
-- 疾患予測（健診データ × NSIPSデータ）
-- 個人を特定する情報は保持しない。属性区分（年代・性別）で扱う。
-- ================================================================

-- NSIPS連携の実行記録
CREATE TABLE IF NOT EXISTS nsips_sync (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  kenpo_id    INTEGER NOT NULL REFERENCES kenpo(id),
  started_at  TEXT NOT NULL DEFAULT (datetime('now','localtime')),
  finished_at TEXT,
  status      TEXT NOT NULL DEFAULT 'running',  -- running / ok / error
  mode        TEXT NOT NULL DEFAULT 'batch',    -- batch / realtime
  fetched     INTEGER NOT NULL DEFAULT 0,       -- 取得した調剤実績の件数
  imported    INTEGER NOT NULL DEFAULT 0,       -- 取込んだ件数
  excluded    INTEGER NOT NULL DEFAULT 0,       -- 個人識別項目を検知して除外した件数
  message     TEXT,
  actor       TEXT
);

-- NSIPSから取込んだ非特定の調剤実績（属性単位で集計済み）
CREATE TABLE IF NOT EXISTS nsips_record (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  sync_id    INTEGER NOT NULL REFERENCES nsips_sync(id),
  kenpo_id   INTEGER NOT NULL REFERENCES kenpo(id),
  age_band   TEXT NOT NULL,          -- 20代 / 30代 …
  sex        TEXT NOT NULL,          -- 男 / 女
  drug_class TEXT NOT NULL,          -- 薬効分類（降圧剤・糖尿病用剤 など）
  persons    INTEGER NOT NULL DEFAULT 0,   -- 服薬している人数
  months     REAL NOT NULL DEFAULT 0,      -- 平均の処方継続月数
  gap_rate   REAL NOT NULL DEFAULT 0       -- 服薬の中断（空白期間）の割合
);

-- 予測の実行記録
CREATE TABLE IF NOT EXISTS risk_run (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  kenpo_id   INTEGER NOT NULL REFERENCES kenpo(id),
  run_at     TEXT NOT NULL DEFAULT (datetime('now','localtime')),
  engine     TEXT NOT NULL,          -- hitachi（契約API）/ trial（社内試算）
  horizon    INTEGER NOT NULL DEFAULT 3,   -- 何年後を見るか
  n_groups   INTEGER NOT NULL DEFAULT 0,
  n_members  INTEGER NOT NULL DEFAULT 0,
  message    TEXT,
  actor      TEXT
);

-- 属性区分ごとの予測結果
CREATE TABLE IF NOT EXISTS risk_score (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id     INTEGER NOT NULL REFERENCES risk_run(id),
  kenpo_id   INTEGER NOT NULL REFERENCES kenpo(id),
  member_id  INTEGER REFERENCES member(id),   -- 個人単位の予測
  company_id INTEGER REFERENCES company(id),
  age_band   TEXT NOT NULL,
  sex        TEXT NOT NULL,
  disease    TEXT NOT NULL,          -- 8大生活習慣病のいずれか
  n_members  INTEGER NOT NULL DEFAULT 0,
  score      REAL NOT NULL DEFAULT 0,     -- 0〜100
  level      TEXT NOT NULL,          -- 低 / 中 / 高
  priority   TEXT NOT NULL           -- A / B / C（保健指導の優先度）
);
CREATE INDEX IF NOT EXISTS ix_risk_score_run ON risk_score (run_id);

-- 健診結果の連携（当社の予約管理システムからバッチで取込む）
CREATE TABLE IF NOT EXISTS kenshin_sync (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  kenpo_id    INTEGER NOT NULL REFERENCES kenpo(id),
  started_at  TEXT NOT NULL DEFAULT (datetime('now','localtime')),
  finished_at TEXT,
  status      TEXT NOT NULL DEFAULT 'running',  -- running / ok / error
  fiscal_year TEXT,                             -- 対象年度
  fetched     INTEGER NOT NULL DEFAULT 0,
  imported    INTEGER NOT NULL DEFAULT 0,
  excluded    INTEGER NOT NULL DEFAULT 0,       -- 個人識別項目を検知して除外した件数
  message     TEXT,
  actor       TEXT
);

-- 属性区分ごとの健診結果（判定区分の人数。検査値そのものは保持しない）
CREATE TABLE IF NOT EXISTS kenshin_record (
  id        INTEGER PRIMARY KEY AUTOINCREMENT,
  sync_id   INTEGER NOT NULL REFERENCES kenshin_sync(id),
  kenpo_id  INTEGER NOT NULL REFERENCES kenpo(id),
  age_band  TEXT NOT NULL,
  sex       TEXT NOT NULL,
  item      TEXT NOT NULL,          -- 血圧 / 血糖 / 脂質 / 肝機能 / BMI / 尿蛋白
  normal    INTEGER NOT NULL DEFAULT 0,   -- 基準内
  caution   INTEGER NOT NULL DEFAULT 0,   -- 要注意
  medical   INTEGER NOT NULL DEFAULT 0    -- 要医療
);
CREATE INDEX IF NOT EXISTS ix_kenshin_record_sync ON kenshin_record (sync_id);

-- 個人単位の健診結果（加入者マスタと突合できたもの）
CREATE TABLE IF NOT EXISTS kenshin_result (
  id        INTEGER PRIMARY KEY AUTOINCREMENT,
  sync_id   INTEGER NOT NULL REFERENCES kenshin_sync(id),
  kenpo_id  INTEGER NOT NULL REFERENCES kenpo(id),
  member_id INTEGER NOT NULL REFERENCES member(id),
  exam_date TEXT,                    -- 受診日
  item      TEXT NOT NULL,           -- 血圧 / 血糖 / 脂質 / 肝機能 / BMI / 尿蛋白
  judge     TEXT NOT NULL,           -- normal / caution / medical
  detail    TEXT                     -- 内訳（例：収縮期152・拡張期94）
);
CREATE INDEX IF NOT EXISTS ix_kenshin_result_m ON kenshin_result (member_id);
CREATE INDEX IF NOT EXISTS ix_kenshin_result_s ON kenshin_result (sync_id);

-- ================================================================
-- 判定グループ（複合した検査の値からまとめて判定する）
--   例）血糖グループ ＝ 空腹時血糖 ＋ HbA1c → 重い判定を採用
--       血圧グループ ＝ 収縮期血圧 ＋ 拡張期血圧 → 重い判定を採用
-- ================================================================
CREATE TABLE IF NOT EXISTS judge_group (
  id     INTEGER PRIMARY KEY AUTOINCREMENT,
  name   TEXT NOT NULL UNIQUE,          -- グループ名（血圧・血糖 など）
  method TEXT NOT NULL DEFAULT 'worst', -- worst=最も重い判定／all=すべて該当／first=優先順
  note   TEXT,
  sort   INTEGER NOT NULL DEFAULT 0,
  updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

-- グループに属する検査項目と、その項目のしきい値
CREATE TABLE IF NOT EXISTS judge_group_item (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  group_id    INTEGER NOT NULL REFERENCES judge_group(id),
  code        TEXT,                     -- 検査項目コード（健診XMLの code）
  name        TEXT NOT NULL,            -- 検査項目名（表示名でも照合する）
  direction   TEXT NOT NULL DEFAULT 'high',  -- high=高いほど悪い／low=低いほど悪い／mark=記号
  caution_min REAL,                     -- 要注意の下限
  medical_min REAL,                     -- 要医療の下限
  unit        TEXT,
  sort        INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_jgi_group ON judge_group_item (group_id);
