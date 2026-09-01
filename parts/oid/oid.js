const OID=(()=>{
  // 厚生労働省 OID(オブジェクトID)表 準拠
  // 出典: https://www.mhlw.go.jp/content/12400000/001082788.pdf
  const data = [
    // 基本情報
    { cat:'基本情報', code:'1.2.392.200119.6.101',    name:'保険者番号',                     values:'8桁',                                                                                       note:'8桁に満たない場合は先頭ゼロをつけて8桁化して使用。自治体健診でも使用' },
    { cat:'基本情報', code:'1.2.392.200119.6.102',    name:'特定健診機関番号・特定保健指導機関番号', values:'10桁',                                                                             note:'このOIDは自治体健診でも使用' },
    { cat:'基本情報', code:'1.2.392.200119.6.103',    name:'代行機関番号',                   values:'8桁',                                                                                       note:'' },
    { cat:'基本情報', code:'1.2.392.200119.6.104',    name:'国・支払基金区分',               values:'1：国、2：支払基金',                                                                       note:'' },
    { cat:'基本情報', code:'1.2.392.200119.6.105',    name:'地方公共団体コード',             values:'財団法人地方自治情報センターが公開する自治体コード',                                       note:'このOIDは自治体健診でも使用' },
    { cat:'基本情報', code:'1.2.392.200119.6.204',    name:'被保険者記号',                   values:'',                                                                                          note:'このOIDは自治体健診でも使用' },
    { cat:'基本情報', code:'1.2.392.200119.6.205',    name:'被保険者番号',                   values:'',                                                                                          note:'このOIDは自治体健診でも使用' },
    { cat:'基本情報', code:'1.2.392.200119.6.206',    name:'資格区分コード',                 values:'1：強制被保険者、2：強制被扶養者、3：任意継続被保険者、4：任意継続被扶養者、5：特例退職被保険者、6：特例退職被扶養者、7：国保被保険者', note:'健診は健診実施日、保健指導は初回面接実施日の資格を記録。国保は必須化しない' },
    { cat:'基本情報', code:'1.2.392.200119.6.208',    name:'券面種別',                       values:'1：受診券、2：利用券',                                                                     note:'' },
    { cat:'基本情報', code:'1.2.392.200119.6.211',    name:'被保険者証等枝番',               values:'',                                                                                          note:'このOIDは自治体健診でも使用' },
    { cat:'基本情報', code:'1.2.392.200119.6.299',    name:'当事者間固有の利用者ID',         values:'当事者間で合意して記述する利用者識別番号',                                                 note:'このOIDは自治体健診でも使用' },
    // 報告関連
    { cat:'報告',     code:'1.2.392.200119.6.1001',   name:'報告区分コード',                 values:'10：特定健診情報、19：削除依頼、21：特定保健指導(開始時)、22：(実績評価時)、23：(途中終了時)、24：(その他)、25：(初回未完了)、30：国への実施結果報告、40〜44：他健診結果送付、49：削除依頼、90：そのほか', note:'このOIDは自治体健診でも使用' },
    { cat:'報告',     code:'1.2.392.200119.6.1002',   name:'プログラム種別コード',           values:'000：不明、010：特定健診、020：広域連合の保健事業、030：事業者健診、040：学校健診、060：がん検診、090：肝炎検診、990：上記以外、100：特定保健指導', note:'' },
    { cat:'報告',     code:'1.2.392.200119.6.1010',   name:'CDA セクションコード',           values:'01010：特定健診・問診結果、01020：広域連合保健事業、01030：労働安全衛生法健診、01040：学校保健安全法健診、01060：がん検診、01090：肝炎検診、01990：任意追加項目、90010〜90080：保健指導関連', note:'' },
    { cat:'報告',     code:'1.2.392.200119.6.1101',   name:'種別コード',                     values:'1：健診機関→代行機関、2：代行機関→健診機関、3：代行機関→保険者、4：保険者→代行機関(未決済)、5：(決済済)、6：健診機関→保険者、7：保険者→健診機関、8：保険者→保険者、9：その他、10：保険者→国、11：確認依頼、12：閲覧用、13：予備', note:'' },
    { cat:'報告',     code:'1.2.392.200119.6.1103',   name:'実施区分コード',                 values:'1：特定健診情報、2：特定保健指導情報、3：国への実績報告、4：他の健診結果の受領分、5：国への実績報告(匿名化前)', note:'3は保険者では設定不要、4は事業者健診結果受領時' },
    // 区分
    { cat:'区分',     code:'1.2.392.200119.6.1104',   name:'男女区分コード',                 values:'1：男、2：女',                                                                             note:'このOIDは自治体健診でも使用' },
    { cat:'区分',     code:'1.2.392.200119.6.1106',   name:'窓口負担コード',                 values:'1：負担なし、2：定額負担、3：定率負担、4：保険者の負担上限額',                              note:'' },
    { cat:'区分',     code:'1.2.392.200119.6.1107',   name:'請求区分コード',                 values:'1：基本的な健診、2：+詳細な健診、3：+追加健診項目、4：+詳細+追加、5：人間ドック',           note:'' },
    { cat:'区分',     code:'1.2.392.200119.6.1108',   name:'詳細な健診項目コード',           values:'1：貧血検査、2：心電図検査、3：眼底検査、4：血清クレアチニン検査',                          note:'' },
    { cat:'区分',     code:'1.2.392.200119.6.1109',   name:'返戻理由コード',                 values:'01：記録形式不備、02：記録もれ、03：健診結果異常、04：契約対象外、05：整理番号不備、06：有効期限外、07：窓口負担金額不備、08：取下げ依頼、09：その他', note:'' },
    { cat:'区分',     code:'1.2.392.200119.6.1110',   name:'過誤返戻理由コード',             values:'01：記号番号誤り、02：整理番号誤り、03：氏名誤り、04：該当者なし、05：保険者番号と記号不一致、06：資格喪失後受診、07：重複請求、08：取下げ依頼、09：その他', note:'' },
    // 検査項目
    { cat:'検査',     code:'1.2.392.200119.6.1005',   name:'特定健診項目コード表',           values:'XML健診コード表の項目コード（JLAC10-17桁コード）',                                          note:'' },
    { cat:'検査',     code:'1.2.392.200119.6.1006',   name:'特定保健指導項目コード表',       values:'XML保健指導コード表の項目コード',                                                          note:'' },
    { cat:'検査',     code:'1.2.392.200119.6.1007',   name:'検査方法 10桁コード',            values:'XML健診コード表の検査方法コード欄を参照',                                                  note:'' },
    { cat:'検査',     code:'1.2.392.200119.6.1205',   name:'検査項目独自ローカルコード',     values:'JLAC10準拠でない独自コードを使用する場合のOID',                                             note:'厚労省手引書附属資料7の指針に準拠' },
    { cat:'検査',     code:'1.2.392.200119.6.2001',   name:'健診検査特記事項有無コード',     values:'1：特記事項あり、2：特記事項なし',                                                         note:'' },
    { cat:'検査',     code:'1.2.392.200119.6.2002',   name:'健診検査所見解釈コード',         values:'1：異常所見あり、2：異常所見なし、3：要再検査、4：検査不適',                                note:'' },
    // 判定・結果
    { cat:'判定',     code:'1.2.392.200119.6.1008',   name:'メタボリックシンドローム判定',   values:'1：基準該当、2：予備群該当、3：非該当、4：判定不能',                                       note:'' },
    { cat:'判定',     code:'1.2.392.200119.6.2100',   name:'定性検査結果',                   values:'1：陽性、2：陰性',                                                                          note:'' },
    { cat:'判定',     code:'1.2.392.200119.6.2101',   name:'定性検査結果（逆順）',           values:'1：陰性、2：陽性',                                                                          note:'' },
    { cat:'判定',     code:'1.2.392.200119.6.2102',   name:'健診定性検査結果コード',         values:'1：−、2：±、3：1+、4：2+、5：3+',                                                          note:'' },
    { cat:'判定',     code:'1.2.392.200119.6.2103',   name:'ウイルス等のタイター',           values:'1：陰性、2：低力価、3：中力価、4：高力価',                                                 note:'' },
    { cat:'判定',     code:'1.2.392.200119.6.2110',   name:'眼底検査KW分類',                 values:'1：0、2：Ⅰ、3：Ⅱa、4：Ⅱb、5：Ⅲ、6：Ⅳ',                                                    note:'' },
    { cat:'判定',     code:'1.2.392.200119.6.2111',   name:'眼底検査シェイエ分類H',          values:'1：0、2：1、3：2、4：3、5：4',                                                              note:'' },
    { cat:'判定',     code:'1.2.392.200119.6.2112',   name:'眼底検査シェイエ分類S',          values:'1：0、2：1、3：2、4：3、5：4',                                                              note:'' },
    { cat:'判定',     code:'1.2.392.200119.6.2113',   name:'眼底検査SCOTT分類S',             values:'1：Ⅰ(a)、2：Ⅰ(b)、3：Ⅱ、4：Ⅲ(a)、5：Ⅲ(b)、6：Ⅳ、7：Ⅴ(a)、8：Ⅴ(b)、9：Ⅵ',                     note:'' },
    { cat:'判定',     code:'1.2.392.200119.6.18080',  name:'眼底検査Wong-Mitchell分類',     values:'1：所見なし、2：軽度、3：中等度、4：重度',                                                 note:'' },
    { cat:'判定',     code:'1.2.392.200119.6.18090',  name:'眼底検査改変Davis分類',         values:'1：網膜症なし、2：単純網膜症、3：増殖前網膜症、4：増殖網膜症',                              note:'' },
    { cat:'判定',     code:'1.2.392.200119.6.2115',   name:'血液型（ABO)',                   values:'1：A、2：B、3：AB、4：O',                                                                   note:'' },
    { cat:'判定',     code:'1.2.392.200119.6.2116',   name:'血液型（Rh)',                    values:'1：+、2：-',                                                                                note:'' },
    { cat:'判定',     code:'1.2.392.200119.6.2120',   name:'子宮頸部細胞診(日母分類)',       values:'1：classⅠ〜6：classⅤ、7：検体不良',                                                        note:'' },
    { cat:'判定',     code:'1.2.392.200119.6.18100',  name:'子宮頸部細胞診(ベセスダ2001)',  values:'1：NILM、2：ASC-US、3：ASC-H、4：LSIL、5：HSIL、6：SCC、7：AGC、8：AIS、9：Adenocarcinoma、10：other', note:'' },
    { cat:'判定',     code:'1.2.392.200119.6.2122',   name:'子宮体部細胞診',                 values:'1：陽性、2：疑陽性、3：陰性、4：検体不良',                                                 note:'' },
    { cat:'判定',     code:'1.2.392.200119.6.2130',   name:'喀痰検査細胞診',                 values:'1：A、2：B、3：C、4：D、5：E',                                                             note:'' },
    { cat:'判定',     code:'1.2.392.200119.6.2131',   name:'喀痰検査ガフキー',               values:'1：0号 〜 11：10号',                                                                        note:'' },
    { cat:'判定',     code:'1.2.392.200119.6.2140',   name:'がん検診',                       values:'1：A、2：B、3：C、4：D、5：E',                                                             note:'' },
    { cat:'判定',     code:'1.2.392.200119.6.2141',   name:'がん検診判定',                   values:'1：精密検査必要、2：精密検査不要',                                                         note:'' },
    { cat:'判定',     code:'1.2.392.200119.6.2150',   name:'C型肝炎ウイルス検診の判定',      values:'1：感染していない可能性が極めて高い、2：感染している可能性が極めて高い',                    note:'' },
    // 問診
    { cat:'問診',     code:'1.2.392.200119.6.2003',   name:'問診結果コード',                 values:'1：はい、2：いいえ',                                                                        note:'' },
    { cat:'問診',     code:'1.2.392.200119.6.2004',   name:'食事の速さコード',               values:'1：速い、2：ふつう、3：遅い',                                                              note:'' },
    { cat:'問診',     code:'1.2.392.200119.6.24040',  name:'飲酒習慣',                       values:'1：毎日、2：週5〜6日、3：週3〜4日、4：週1〜2日、5：月に1〜3日、6：月に1日未満、7：やめた、8：飲まない(飲めない)', note:'' },
    { cat:'問診',     code:'1.2.392.200119.6.24050',  name:'飲酒量区分',                     values:'1：1合未満、2：1〜2合未満、3：2〜3合未満、4：3〜5合未満、5：5合以上',                       note:'' },
    { cat:'問診',     code:'1.2.392.200119.6.24060',  name:'喫煙',                           values:'1：はい、2：以前は吸っていたが最近1ヶ月間は吸っていない、3：いいえ',                        note:'' },
    { cat:'問診',     code:'1.2.392.200119.6.2007',   name:'生活習慣改善意志区分',           values:'1：意志なし、2：意志あり(6か月以内)、3：意志あり(近いうち)、4：取組済み(6ヵ月未満)、5：取組済み(6ヵ月以上)', note:'' },
    { cat:'問診',     code:'1.2.392.200119.6.2008',   name:'問診結果コード(0/1)',            values:'0：はい、1：いいえ',                                                                        note:'' },
    { cat:'問診',     code:'1.2.392.200119.6.2009',   name:'問診結果コード(0/1 逆)',         values:'0：いいえ、1：はい',                                                                        note:'' },
    { cat:'問診',     code:'1.2.392.200119.6.18030',  name:'咀嚼コード',                     values:'1：何でも、2：かみにくい、3：ほとんどかめない',                                             note:'' },
    { cat:'問診',     code:'1.2.392.200119.6.18040',  name:'間食コード',                     values:'1：毎日、2：時々、3：ほとんど摂取しない',                                                  note:'' },
    // 保健指導
    { cat:'保健指導', code:'1.2.392.200119.6.1111',   name:'保健指導実施時点コード',         values:'1：開始時、2：実績評価時、3：途中終了時、4：その他、5：初回未完了',                          note:'2：集合契約の最終決済時、3：資格喪失、4：個別契約、5：初回面接①のみ' },
    { cat:'保健指導', code:'1.2.392.200119.6.1112',   name:'保健指導区分コード',             values:'1：積極的支援、2：動機付け支援、3：動機付け支援相当',                                       note:'' },
    { cat:'保健指導', code:'1.2.392.200119.6.24010',  name:'保健指導支援形態コード',         values:'1：個別支援(対面)、2：個別支援(遠隔)、3：グループ支援(対面)、4：グループ支援(遠隔)、5：電話、6：電子メール等', note:'5・6は初回面接では入力不可' },
    { cat:'保健指導', code:'1.2.392.200119.6.1114',   name:'窓口負担徴収コード',             values:'1：初回指導時全額徴収、2：それ以外',                                                       note:'' },
    { cat:'保健指導', code:'1.2.392.200119.6.24020',  name:'再確認コード',                   values:'1：質問票の記載違い(服薬中)を確認、2：健診以後に服薬開始を確認',                            note:'' },
    { cat:'保健指導', code:'1.2.392.200119.6.24030',  name:'保健指導時服薬確認コード',       values:'1：保健指導以後に服薬開始を確認',                                                          note:'対象から除外時に記載、継続時は記載しない' },
    { cat:'保健指導', code:'1.2.392.200119.6.18050',  name:'情報提供コード',                 values:'1：付加価値の高い情報提供、2：専門職による対面説明、3：1と2両方実施',                        note:'' },
    { cat:'保健指導', code:'1.2.392.200119.6.18060',  name:'初回面接',                       values:'1：健診1週間以内に初回面接実施',                                                           note:'セット券運用で健診実施日を0日として7日後までに初回面接を実施した場合のみ' },
    { cat:'保健指導', code:'1.2.392.200119.6.24070',  name:'健診後早期の初回面接',           values:'0：実施なし、1：当日、2：1週間以内(当日は除く)',                                            note:'2は健診実施日を0日として1〜7日後に初回面接を実施した場合' },
    { cat:'保健指導', code:'1.2.392.200119.6.24080',  name:'検査未実施の理由',               values:'1：生理中、2：腎疾患等の基礎疾患があるため排尿障害を有する、3：その他',                     note:'' },
    { cat:'保健指導', code:'1.2.392.200119.6.3001',   name:'支援レベルコード',               values:'1：積極的支援、2：動機付け支援、3：なし(情報提供)、4：判定不能',                            note:'' },
    { cat:'保健指導', code:'1.2.392.200119.6.3002',   name:'生活習慣の改善意思区分',         values:'1：意志なし、2：意志あり(6か月以内)、3：意志あり(近いうち)、4：取組済み(6ヶ月未満)、5：取組済み(6ヶ月以上)', note:'行動変容ステージ区分' },
    { cat:'保健指導', code:'1.2.392.200119.6.24090',  name:'腹囲・体重の改善',               values:'0：未達成、1：1cm・1kg、2：2cm・2kg',                                                       note:'' },
    { cat:'保健指導', code:'1.2.392.200119.6.24100',  name:'生活習慣の改善',                 values:'0：未達成、1：達成、9：目標なし',                                                          note:'' },
    { cat:'保健指導', code:'1.2.392.200119.6.24130',  name:'喫煙習慣の改善',                 values:'0：禁煙未達成、1：禁煙達成、8：非喫煙、9：禁煙目標なし',                                    note:'' },
    { cat:'保健指導', code:'1.2.392.200119.6.24110',  name:'腹囲・体重の計画',               values:'0：計画なし、1：1cm・1kg、2：2cm・2kg',                                                     note:'' },
    { cat:'保健指導', code:'1.2.392.200119.6.24120',  name:'行動変容の計画',                 values:'0：計画なし、1：計画あり',                                                                  note:'' },
    { cat:'保健指導', code:'1.2.392.200119.6.3020',   name:'保健指導関係者区分',             values:'1：医師、2：保健師、3：管理栄養士、4：その他',                                             note:'' },
    { cat:'保健指導', code:'1.2.392.200119.6.18150',  name:'実施内容',                       values:'1：初回面接(分割実施以外)、2：初回面接①、3：初回面接②、4：中間評価、5：継続的支援、6：実績評価', note:'' },
    // その他
    { cat:'その他',   code:'1.2.392.200119.6.2202',   name:'食後時間区分',                   values:'2：10時間以上、3：食後3.5時間以上10時間未満、4：食後3.5時間未満',                            note:'' },
    { cat:'その他',   code:'1.2.392.200119.6.2301',   name:'聴力検査方法',                   values:'1：オージオメトリー、2：その他',                                                           note:'' },
    { cat:'その他',   code:'1.2.392.200119.6.2501',   name:'生活機能評価の結果1',            values:'1：介護予防事業の利用が望ましい、2：医学的理由により介護予防の利用は不適当、3：生活機能の低下なし', note:'' },
    { cat:'その他',   code:'1.2.392.200119.6.2502',   name:'生活機能評価の結果2',            values:'1：すべて、2：運動器の機能向上、3：栄養改善、4：口腔機能の向上、5：その他',                 note:'' },
    { cat:'その他',   code:'1.2.392.200119.6.18110',  name:'血清クレアチニン（対象者）',     values:'0：詳細健診以外で実施、1：検査結果による対象者',                                            note:'' },
    { cat:'その他',   code:'1.2.392.200119.6.18120',  name:'心電図（対象者）',               values:'0：詳細健診以外で実施、1：検査結果による対象者、2：不整脈による対象者',                     note:'' },
    { cat:'その他',   code:'1.2.392.200119.6.18130',  name:'眼底検査（対象者）',             values:'0：詳細健診以外で実施、1：検査結果による対象者',                                            note:'' },
    // 後期高齢者質問票
    { cat:'後期',     code:'1.2.392.200119.6.19010',  name:'現在の健康状態(後期)',           values:'1：よい、2：まあよい、3：ふつう、4：あまりよくない、5：よくない',                            note:'' },
    { cat:'後期',     code:'1.2.392.200119.6.19020',  name:'毎日の生活の満足度(後期)',       values:'1：満足、2：やや満足、3：やや不満、4：不満',                                               note:'' },
    { cat:'後期',     code:'1.2.392.200119.6.19030',  name:'1日3食きちんと食べていますか(後期)', values:'1：はい、2：いいえ',                                                                    note:'' },
    { cat:'後期',     code:'1.2.392.200119.6.19040',  name:'固いものが食べにくくなりましたか(後期)', values:'1：はい、2：いいえ',                                                                note:'' },
    { cat:'後期',     code:'1.2.392.200119.6.19050',  name:'お茶や汁物等でむせることがありますか(後期)', values:'1：はい、2：いいえ',                                                            note:'' },
    { cat:'後期',     code:'1.2.392.200119.6.19060',  name:'6カ月間で2〜3kg以上の体重減少(後期)', values:'1：はい、2：いいえ',                                                                    note:'' },
    { cat:'後期',     code:'1.2.392.200119.6.19070',  name:'歩く速度が遅くなってきましたか(後期)', values:'1：はい、2：いいえ',                                                                   note:'' },
    { cat:'後期',     code:'1.2.392.200119.6.19080',  name:'この1年間に転んだことがありますか(後期)', values:'1：はい、2：いいえ',                                                               note:'' },
    { cat:'後期',     code:'1.2.392.200119.6.19090',  name:'ウォーキング等の運動を週に1回以上(後期)', values:'1：はい、2：いいえ',                                                               note:'' },
    { cat:'後期',     code:'1.2.392.200119.6.19100',  name:'物忘れがあると言われていますか(後期)', values:'1：はい、2：いいえ',                                                                   note:'' },
    { cat:'後期',     code:'1.2.392.200119.6.19110',  name:'今日が何月何日かわからない時(後期)', values:'1：はい、2：いいえ',                                                                     note:'' },
    { cat:'後期',     code:'1.2.392.200119.6.19120',  name:'たばこを吸いますか(後期)',       values:'1：吸っている、2：吸っていない、3：やめた',                                                note:'' },
    { cat:'後期',     code:'1.2.392.200119.6.19130',  name:'週に1回以上は外出していますか(後期)', values:'1：はい、2：いいえ',                                                                    note:'' },
    { cat:'後期',     code:'1.2.392.200119.6.19140',  name:'家族や友人と付き合いがありますか(後期)', values:'1：はい、2：いいえ',                                                                note:'' },
    { cat:'後期',     code:'1.2.392.200119.6.19150',  name:'身近に相談できる人がいますか(後期)', values:'1：はい、2：いいえ',                                                                    note:'' }
  ];
  let editingIdx = null;

  const catColors = {
    '基本情報':'#dbeafe','報告':'#fef3c7','区分':'#e0f2fe','検査':'#dcfce7',
    '判定':'#fce7f3','問診':'#e0e7ff','保健指導':'#ede9fe','後期':'#ffedd5','その他':'#f1f5f9'
  };
  const catTextColors = {
    '基本情報':'#1e40af','報告':'#b45309','区分':'#0369a1','検査':'#15803d',
    '判定':'#be185d','問診':'#4338ca','保健指導':'#6d28d9','後期':'#c2410c','その他':'#475569'
  };

  function getFiltered(){
    const cat = document.getElementById('oid-filter-cat')?.value || '';
    const kw = (document.getElementById('oid-filter-kw')?.value || '').toLowerCase();
    return data.filter(r => {
      if(cat && r.cat !== cat) return false;
      if(kw){
        return r.code.toLowerCase().includes(kw) || r.name.toLowerCase().includes(kw);
      }
      return true;
    });
  }

  function render(){
    const tbody = document.getElementById('oid-tbody');
    if(!tbody) return;
    const arr = getFiltered();
    tbody.innerHTML = '';
    arr.forEach((r,i)=>{
      const origIdx = data.indexOf(r);
      const bg = catColors[r.cat] || '#f1f5f9';
      const cl = catTextColors[r.cat] || '#475569';
      const tr = document.createElement('tr');
      tr.innerHTML =
        '<td>'+(i+1)+'</td>' +
        '<td><span style="display:inline-flex;align-items:center;justify-content:center;padding:3px 10px;background:'+bg+';color:'+cl+';border-radius:4px;font-weight:600;font-size:11px;letter-spacing:0.02em;">'+r.cat+'</span></td>' +
        '<td class="tbl-left" style="font-family:monospace;font-size:12px;color:#0f172a;letter-spacing:0.02em;">'+esc(r.code)+'</td>' +
        '<td class="tbl-left" style="font-weight:600;color:#0f172a;">'+esc(r.name)+'</td>' +
        '<td class="tbl-left" style="font-size:12px;color:#334155;line-height:1.5;">'+esc(r.values||'—')+'</td>' +
        '<td class="tbl-left" style="font-size:11px;color:#64748b;line-height:1.5;">'+esc(r.note||'—')+'</td>' +
        '<td>' +
          '<button type="button" class="tbl-btn tbl-btn-edit" style="width:auto;padding:0 10px;font-size:12px;" onclick="OID.edit('+origIdx+')">編集</button>' +
          ' <button type="button" class="tbl-btn tbl-btn-edit" style="width:auto;padding:0 10px;font-size:12px;background:#fef2f2;color:#b91c1c;border:1px solid #fecaca;" onmouseover="this.style.background=\'#fecaca\';" onmouseout="this.style.background=\'#fef2f2\';" onclick="OID.remove('+origIdx+')">削除</button>' +
        '</td>';
      tbody.appendChild(tr);
    });
    const cnt = document.getElementById('oid-count');
    if(cnt) cnt.textContent = '件数: ' + arr.length + ' / 全' + data.length + '件';
  }

  function openNewModal(){
    editingIdx = null;
    document.getElementById('oid-modalTitle').textContent = 'OIDコードの新規作成';
    document.getElementById('oid-inp-cat').value = '基本情報';
    document.getElementById('oid-inp-code').value = '';
    document.getElementById('oid-inp-name').value = '';
    document.getElementById('oid-inp-values').value = '';
    document.getElementById('oid-inp-note').value = '';
    const ov = document.getElementById('oid-modal');
    ov.style.display = 'flex';
    setTimeout(()=>ov.classList.add('open'),10);
    setTimeout(()=>document.getElementById('oid-inp-name').focus(),100);
  }

  function edit(idx){
    editingIdx = idx;
    const r = data[idx];
    if(!r) return;
    document.getElementById('oid-modalTitle').textContent = 'OIDコードの編集';
    document.getElementById('oid-inp-cat').value = r.cat;
    document.getElementById('oid-inp-code').value = r.code;
    document.getElementById('oid-inp-name').value = r.name;
    document.getElementById('oid-inp-values').value = r.values || '';
    document.getElementById('oid-inp-note').value = r.note || '';
    const ov = document.getElementById('oid-modal');
    ov.style.display = 'flex';
    setTimeout(()=>ov.classList.add('open'),10);
  }

  function closeModal(){
    const ov = document.getElementById('oid-modal');
    ov.classList.remove('open');
    setTimeout(()=>ov.style.display='none', 200);
    editingIdx = null;
  }

  function save(){
    const cat = document.getElementById('oid-inp-cat').value;
    const code = document.getElementById('oid-inp-code').value.trim();
    const name = document.getElementById('oid-inp-name').value.trim();
    const values = document.getElementById('oid-inp-values').value.trim();
    const note = document.getElementById('oid-inp-note').value.trim();
    if(!name){ alert('名称を入力してください。'); return; }
    if(!code){ alert('OIDコードを入力してください。'); return; }
    if(!/^[0-9.]+$/.test(code)){ alert('OIDコードは数字とドット(.)のみで入力してください。'); return; }
    if(editingIdx !== null){
      data[editingIdx] = { cat, code, name, values, note };
    } else {
      data.push({ cat, code, name, values, note });
    }
    closeModal();
    render();
  }

  function remove(idx){
    const r = data[idx];
    if(!r) return;
    if(!confirm('「'+r.name+'」を削除しますか？')) return;
    data.splice(idx, 1);
    render();
  }

  function esc(s){ return String(s==null?'':s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }

  return { render, openNewModal, closeModal, edit, save, remove, _data: data };
})();