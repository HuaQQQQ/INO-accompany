import re
import pypinyin

# Fast, natural Katakana phonetic map (without dragged-out 'ー' long vowels)
KATAKANA_MAP = {
    'a': 'ア', 'ai': 'アイ', 'an': 'アン', 'ang': 'アン', 'ao': 'アオ',
    'ba': 'バ', 'bai': 'バイ', 'ban': 'バン', 'bang': 'バン', 'bao': 'バオ', 'bei': 'ベイ', 'ben': 'ベン', 'beng': 'ブン', 'bi': 'ビ', 'bian': 'ビエン', 'biao': 'ビアオ', 'bie': 'ビエ', 'bin': 'ビン', 'bing': 'ビン', 'bo': 'ボ', 'bu': 'ブ',
    'ca': 'ツァ', 'cai': 'ツアイ', 'can': 'ツアン', 'cang': 'ツアン', 'cao': 'ツアオ', 'ce': 'ツァ', 'cen': 'ツェン', 'ceng': 'ツェン', 'cha': 'チャ', 'chai': 'チャイ', 'chan': 'チャン', 'chang': 'チャン', 'chao': 'チャオ', 'che': 'チェ', 'chen': 'チェン', 'cheng': 'チェン', 'chi': 'チ', 'chong': 'チョン', 'chou': 'チョウ', 'chu': 'チュ', 'chuai': 'チュアイ', 'chuan': 'チュアン', 'chuang': 'チュアン', 'chui': 'チュイ', 'chun': 'チュン', 'chuo': 'チュオ', 'ci': 'ツ', 'cong': 'ツォン', 'cou': 'ツオウ', 'cu': 'ツゥ', 'cuan': 'ツアン', 'cui': 'ツイ', 'cun': 'ツン', 'cuo': 'ツオ',
    'da': 'ダ', 'dai': 'ダイ', 'dan': 'ダン', 'dang': 'ダン', 'dao': 'ダオ', 'de': 'ダ', 'dei': 'デイ', 'den': 'デン', 'deng': 'デン', 'di': 'ディ', 'dian': 'ディエン', 'diao': 'ディアオ', 'die': 'ディエ', 'ding': 'ディン', 'diu': 'ディウ', 'dong': 'ドン', 'dou': 'ドウ', 'du': 'ドゥ', 'duan': 'ドゥアン', 'dui': 'ドゥイ', 'dun': 'ドゥン', 'duo': 'ドゥオ',
    'e': 'ア', 'ei': 'エイ', 'en': 'エン', 'eng': 'エン', 'er': 'アル',
    'fa': 'ファ', 'fan': 'ファン', 'fang': 'ファン', 'fei': 'フェイ', 'fen': 'フェン', 'feng': 'フォン', 'fo': 'フォ', 'fou': 'フォウ', 'fu': 'フ',
    'ga': 'ガ', 'gai': 'ガイ', 'gan': 'ガン', 'gang': 'ガン', 'gao': 'ガオ', 'ge': 'ガ', 'gei': 'ゲイ', 'gen': 'ゲン', 'geng': 'ゲン', 'gong': 'ゴン', 'gou': 'ゴウ', 'gu': 'グ', 'gua': 'グア', 'guai': 'グアイ', 'guan': 'グアン', 'guang': 'グアン', 'gui': 'グイ', 'gun': 'グン', 'guo': 'グオ',
    'ha': 'ハ', 'hai': 'ハイ', 'han': 'ハン', 'hang': 'ハン', 'hao': 'ハオ', 'he': 'ハ', 'hei': 'ヘイ', 'hen': 'ヘン', 'heng': 'ヘン', 'hong': 'ホン', 'hou': 'ホウ', 'hu': 'フ', 'hua': 'ファ', 'huai': 'ファイ', 'huan': 'ファン', 'huang': 'ファン', 'hui': 'フイ', 'hun': 'フン', 'huo': 'フォ',
    'ji': 'ジ', 'jia': 'ジア', 'jian': 'ジエン', 'jiang': 'ジャン', 'jiao': 'ジアオ', 'jie': 'ジエ', 'jin': 'ジン', 'jing': 'ジン', 'jiong': 'ジオン', 'jiu': 'ジウ', 'ju': 'ジュ', 'juan': 'ジュアン', 'jue': 'ジュエ', 'jun': 'ジュン',
    'ka': 'カ', 'kai': 'カイ', 'kan': 'カン', 'kang': 'カン', 'kao': 'カオ', 'ke': 'カ', 'ken': 'ケン', 'keng': 'ケン', 'kong': 'コン', 'kou': 'コウ', 'ku': 'ク', 'kua': 'クア', 'kuai': 'クアイ', 'kuan': 'クアン', 'kuang': 'クアン', 'kui': 'クイ', 'kun': 'クン', 'kuo': 'クオ',
    'la': 'ラ', 'lai': 'ライ', 'lan': 'ラン', 'lang': 'ラン', 'lao': 'ラオ', 'le': 'ラ', 'lei': 'レイ', 'leng': 'レン', 'li': 'リ', 'lia': 'リア', 'lian': 'リエン', 'liang': 'リアン', 'liao': 'リアオ', 'lie': 'リエ', 'lin': 'リン', 'ling': 'リン', 'liu': 'リウ', 'long': 'ロン', 'lou': 'ロウ', 'lu': 'ル', 'luan': 'ルアン', 'lun': 'ルン', 'luo': 'ルオ', 'lv': 'リュ', 'lue': 'リュエ',
    'ma': 'マ', 'mai': 'マイ', 'man': 'マン', 'mang': 'マン', 'mao': 'マオ', 'me': 'モ', 'mei': 'メイ', 'men': 'メン', 'meng': 'モン', 'mi': 'ミ', 'mian': 'ミエン', 'miao': 'ミアン', 'mie': 'ミエ', 'min': 'ミン', 'ming': 'ミン', 'miu': 'ミウ', 'mo': 'モ', 'mou': 'モウ', 'mu': 'ム',
    'na': 'ナ', 'nai': 'ナイ', 'nan': 'ナン', 'nang': 'ナン', 'nao': 'ナオ', 'ne': 'ネ', 'nei': 'ネイ', 'nen': 'ネン', 'neng': 'ノン', 'ni': 'ニ', 'nian': 'ニエン', 'niang': 'ニアン', 'niao': 'ニアオ', 'nie': 'ニエ', 'nin': 'ニン', 'ning': 'ニン', 'niu': 'ニウ', 'nong': 'ノン', 'nou': 'ノウ', 'nu': 'ヌ', 'nuan': 'ヌアン', 'nuo': 'ヌオ', 'nv': 'ニュー', 'nue': 'ニュエ',
    'ou': 'オウ',
    'pa': 'パ', 'pai': 'パイ', 'pan': 'パン', 'pang': 'パン', 'pao': 'パオ', 'pei': 'ペイ', 'pen': 'ペン', 'peng': 'ポン', 'pi': 'ピ', 'pian': 'ピエン', 'piao': 'ピアオ', 'pie': 'ピエ', 'pin': 'ピン', 'ping': 'ピン', 'po': 'ポ', 'pou': 'ポウ', 'pu': 'プ',
    'qi': 'チ', 'qia': 'チア', 'qian': 'チェン', 'qiang': 'チャン', 'qiao': 'チャオ', 'qie': 'チエ', 'qin': 'チン', 'qing': 'チン', 'qiong': 'チョン', 'qiu': 'チウ', 'qu': 'チュ', 'quan': 'チュアン', 'que': 'チュエ', 'qun': 'チュン',
    'ran': 'ラン', 'rang': 'ラン', 'rao': 'ラオ', 're': 'ラ', 'ren': 'レン', 'reng': 'レン', 'ri': 'リ', 'rong': 'ロン', 'rou': 'ロウ', 'ru': 'ル', 'ruan': 'ルアン', 'rui': 'ルイ', 'run': 'ルン', 'ruo': 'ルオ',
    'sa': 'サ', 'sai': 'サイ', 'san': 'サン', 'sang': 'サン', 'sao': 'サオ', 'se': 'サ', 'sen': 'セン', 'seng': 'セン', 'sha': 'シャ', 'shai': 'シャイ', 'shan': 'シャン', 'shang': 'シャン', 'shao': 'シャオ', 'she': 'シェ', 'shen': 'シェン', 'sheng': 'シェン', 'shi': 'シ', 'shou': 'ショウ', 'shu': 'シュ', 'shua': 'シュア', 'shuai': 'シュアイ', 'shuan': 'シュアン', 'shuang': 'シュアン', 'shui': 'シュイ', 'shun': 'シュン', 'shuo': 'シュオ', 'si': 'ス', 'song': 'ソン', 'sou': 'ソウ', 'su': 'ス', 'suan': 'スアン', 'sui': 'スイ', 'sun': 'スン', 'suo': 'スオ',
    'ta': 'タ', 'tai': 'タイ', 'tan': 'タン', 'tang': 'タン', 'tao': 'タオ', 'te': 'タ', 'teng': 'トン', 'ti': 'ティ', 'tian': 'ティエン', 'tiao': 'ティアオ', 'tie': 'ティエ', 'ting': 'ティン', 'tong': 'トン', 'tou': 'トウ', 'tu': 'トゥ', 'tuan': 'トゥアン', 'tui': 'トゥイ', 'tun': 'トゥン', 'tuo': 'トゥオ',
    'wa': 'ワ', 'wai': 'ワイ', 'wan': 'ワン', 'wang': 'ワン', 'wei': 'ウェイ', 'wen': 'ウェン', 'weng': 'ウォン', 'wo': 'ウォ', 'wu': 'ウ',
    'xi': 'シ', 'xia': 'シア', 'xian': 'シエン', 'xiang': 'シャン', 'xiao': 'シアオ', 'xie': 'シエ', 'xin': 'シン', 'xing': 'シン', 'xiong': 'シオン', 'xiu': 'シウ', 'xu': 'シュ', 'xuan': 'シュアン', 'xue': 'シュエ', 'xun': 'シュン',
    'ya': 'ヤ', 'yan': 'イエン', 'yang': 'ヤン', 'yao': 'ヤオ', 'ye': 'イエ', 'yi': 'イ', 'yin': 'イン', 'ying': 'イン', 'yo': 'ヨ', 'yong': 'ヨン', 'you': 'ヨウ', 'yu': 'ユ', 'yuan': 'ユアン', 'yue': 'ユエ', 'yun': 'ユン',
    'za': 'ザ', 'zai': 'ザイ', 'zan': 'ザン', 'zang': 'ザン', 'zao': 'ザオ', 'ze': 'ザ', 'zei': 'ゼイ', 'zen': 'ゼン', 'zeng': 'ゼン', 'zha': 'ジャ', 'zhai': 'ジャイ', 'zhan': 'ジャン', 'zhang': 'ジャン', 'zhao': 'ジャオ', 'zhe': 'ジェ', 'zhen': 'ジェン', 'zheng': 'ジェン', 'zhi': 'ジ', 'zhong': 'ジョン', 'zhou': 'ジョウ', 'zhu': 'ジュ', 'zhua': 'ジュア', 'zhuai': 'ジュアイ', 'zhuan': 'ジュアン', 'zhuang': 'ジュアン', 'zhui': 'ジュイ', 'zhun': 'ジュン', 'zhuo': 'ジュオ', 'zi': 'ズ', 'zong': 'ゾン', 'zou': 'ゾウ', 'zu': 'ズ', 'zuan': 'ズアン', 'zui': 'ズイ', 'zun': 'ズン', 'zuo': 'ズオ',
}

def chinese_to_voicevox_katakana(text: str) -> str:
    """Convert Chinese characters in text into Japanese Katakana phonetic approximations for Voicevox."""
    out = []
    # 移除表情符号与不可发音的特殊符号
    clean_text = re.sub(r'[\U00010000-\U0010ffff\u2600-\u27ff\u2300-\u23ff\u2b50\u2b55\u200d\ufe0f]', '', text)
    tokens = re.findall(r'[\u4e00-\u9fff]+|[^\u4e00-\u9fff]+', clean_text)
    for token in tokens:
        if re.match(r'[\u4e00-\u9fff]+', token):
            pinyin_list = pypinyin.pinyin(token, style=pypinyin.Style.NORMAL)
            for py_item in pinyin_list:
                syl = py_item[0].lower()
                out.append(KATAKANA_MAP.get(syl, syl))
        else:
            # 保留基本日式/中文标点，过滤其他杂质字符
            sub_tok = re.sub(r'[^\w\s，。！？、…～~\-?!.,]', '', token)
            out.append(sub_tok)
    return ''.join(out)
