"""🎬 アニメ実装キットの書き出し（カンプ → コーダーがそのまま使える汎用コード）。

ねらい：カンプの動きはこのツール内部の仕組み（fxa_*クラス＋専用ランタイム）で動いているため、
そのまま渡してもコーダーが読み解けない。ここで「ツール語 → 標準Web語」に機械翻訳して、
  ① 汎用CSS（anim.css相当）＋ ② 汎用JS（IntersectionObserver・依存ゼロ）＋
  ③ どの要素に何のクラスを付けるかの対応表 ＋ ④ その場で動くデモ
を1枚の自己完結HTMLに書き出す。AIは使わない（無料・一瞬・ブレない）。

静的なテキスト解析だけで完結する（Playwright不要）：
アニメの正体は class属性（fxa_*）と data-cedelay / inlineの--fxa-dur等に全部書いてあるため。
"""

from __future__ import annotations

import html as _html
import re
from datetime import datetime
from itertools import groupby

from . import camp, config
from .utils import get_logger

log = get_logger("animkit")

KIT_DIR = config.CAMP_DIR / "kits"

# ── ツール語(fxa_*) → キット語の翻訳表 ─────────────────────────────────────
# k=検出するfxaクラス / ja=人間向けの動き名 / kit=コーダーが付けるクラス / note=補足
_FX_MAP = [
    ("fxa_y",    "ふわっと出現（下から）",   "rv rv-up",        ""),
    ("fxa_yd",   "上から降りてくる",         "rv rv-down",      ""),
    ("fxa_xl",   "左からスライドイン",       "rv rv-left",      ""),
    ("fxa_xr",   "右からスライドイン",       "rv rv-right",     ""),
    ("fxa_s",    "ズームしながら出現",       "rv rv-zoom",      ""),
    ("fxa_bl",   "ぼかしから出現",           "rv rv-blur",      ""),
    ("fxa_ry",   "3D回転で出現",             "rv rv-flip",      ""),
    ("fxa_clip", "下から出現",               "rv rv-up",        ""),
    ("fxa_fl",   "📖ページめくり",           "rv rv-page",      ""),
    ("fxa_wp",   "カーテンワイプ",           "rv rv-curtain-l", "元は色帯が走る演出。キットは幕開きで近似（帯が必要なら要相談）"),
    ("fxa_cl",   "カーテン開き（左から）",   "rv rv-curtain-l", ""),
    ("fxa_cc",   "カーテン開き（真ん中）",   "rv rv-curtain-c", ""),
    ("fxa_lines","行マスク（行ごとにせり上がる）", "rv-lines",  "HTML側を行ごとに <span class=\"ln\"><span class=\"lni\">…</span></span> で包む"),
    ("fxa_cpre", "1文字ずつ跳ねて出る",      "chars",           "JSが自動で1文字ずつspan分割する"),
    ("fxa_tw",   "タイプライター",           "chars",           "文字間隔はJSの40msを60ms程度に"),
    ("fxa_wave", "文字が波打つ（ループ）",   "chars lp-wave",   ""),
    ("fxa_lp_pulse",  "鼓動（ループ）",      "lp-pulse",        ""),
    ("fxa_lp_float",  "ふわふわ浮遊（ループ）", "lp-float",     ""),
    ("fxa_lp_bounce", "跳ねる（ループ）",    "lp-bounce",       ""),
    ("fxa_lp_glow",   "光る（ループ）",      "lp-glow",         ""),
    ("fxa_hl",   "🖍マーカーが左から伸びる", "mk",              "色は --mk-c で指定"),
    ("fxa_ud",   "〰点線下線が左から引かれる", "ud",            "色は --ud-c で指定"),
    ("fxa_cnt",  "🔢カウントアップ",         "cnt",             "文字中の最初の数字を0→目標値へ"),
    ("reveal",   "スクロールで出現",         "rv rv-up",        ""),
]

# 検出はするが対応表に載せない部品クラス（分割の破片）
_SKIP_CLASSES = {"fxa_ch", "fxa_ln", "fxa_lni", "fxa_in", "fxa_pre", "fxa_cpre_done"}

# ── コーダーに渡す汎用CSS（コピペで動く・依存ゼロ） ────────────────────────
KIT_CSS = """/* ============================================================
   anim.css — スクロール出現アニメ一式（依存ゼロ）
   使い方：動かしたい要素にクラスを付けるだけ（例 class="rv rv-up"）
   ・遅らせたい : data-delay="400"（ms）
   ・速さ変更   : data-dur="1200"（ms）
   ・JSが html に .anim-on を付ける＝JS無効環境では全部そのまま表示（消えない保険）
   ============================================================ */

/* 出現アニメ（.is-in が付くと再生される） */
html.anim-on .rv{opacity:0;transition:opacity var(--dur,.8s) ease,transform var(--dur,.8s) ease,filter var(--dur,.8s) ease,clip-path var(--dur,.8s) ease;transition-delay:var(--delay,0ms)}
html.anim-on .rv-up{transform:translateY(28px)}
html.anim-on .rv-down{transform:translateY(-36px)}
html.anim-on .rv-left{transform:translateX(-48px)}
html.anim-on .rv-right{transform:translateX(48px)}
html.anim-on .rv-zoom{transform:scale(.86)}
html.anim-on .rv-blur{filter:blur(14px)}
html.anim-on .rv-flip{transform:perspective(800px) rotateY(90deg)}
html.anim-on .rv-page{transform-origin:left center;transform:perspective(1200px) rotateY(80deg)}
html.anim-on .rv-curtain-l{opacity:1;clip-path:inset(0 100% 0 0)}
html.anim-on .rv-curtain-c{opacity:1;clip-path:inset(0 50% 0 50%)}
html.anim-on .rv.is-in{opacity:1;transform:none;filter:none;clip-path:inset(0 0 0 0)}

/* 行マスク（行ごとに下からせり上がる）。HTMLは .ln>.lni で行を包む */
html.anim-on .rv-lines .ln{display:block;overflow:hidden}
html.anim-on .rv-lines .lni{display:block;transform:translateY(112%);transition:transform var(--dur,.7s) cubic-bezier(.22,1,.36,1)}
html.anim-on .rv-lines.is-in .lni{transform:none}

/* 1文字ずつ（JSが .ch に分割する） */
html.anim-on .chars .ch{display:inline-block;opacity:0;transform:translateY(20px);transition:opacity .3s cubic-bezier(.34,1.56,.64,1),transform .3s cubic-bezier(.34,1.56,.64,1)}
html.anim-on .chars.is-in .ch{opacity:1;transform:none}

/* 🖍マーカー（左から伸びる）。色は --mk-c */
html.anim-on .mk{background-image:linear-gradient(transparent 79%,var(--mk-c,#ffe66d) 79%,var(--mk-c,#ffe66d) 91%,transparent 91%);background-repeat:no-repeat;background-size:0% 100%;-webkit-box-decoration-break:slice;box-decoration-break:slice;transition:background-size var(--dur,.6s) cubic-bezier(.25,.6,.3,1) var(--delay,0ms)}
html.anim-on .mk.is-in{background-size:100% 100%}

/* 〰点線下線（左から引かれる）。色は --ud-c */
html.anim-on .ud{background-image:repeating-linear-gradient(90deg,var(--ud-c,#0b6bcb) 0 6px,transparent 6px 11px);background-repeat:no-repeat;background-position:left 100%;background-size:0% 3px;padding-bottom:.15em;transition:background-size var(--dur,.6s) ease var(--delay,0ms)}
html.anim-on .ud.is-in{background-size:100% 3px}

/* ループ系 */
@keyframes kit-pulse{0%,100%{transform:scale(1)}50%{transform:scale(1.06)}}
@keyframes kit-float{0%,100%{transform:translateY(0)}50%{transform:translateY(-12px)}}
@keyframes kit-bounce{0%,100%{transform:translateY(0)}30%{transform:translateY(-18px)}60%{transform:translateY(0)}80%{transform:translateY(-7px)}}
@keyframes kit-glow{0%,100%{text-shadow:0 0 4px currentColor;filter:brightness(1)}50%{text-shadow:0 0 16px currentColor,0 0 30px currentColor;filter:brightness(1.16)}}
@keyframes kit-wave{0%,100%{transform:translateY(0)}50%{transform:translateY(-10px)}}
.lp-pulse{animation:kit-pulse 1.4s ease-in-out infinite}
.lp-float{animation:kit-float 2.2s ease-in-out infinite}
.lp-bounce{animation:kit-bounce 1.2s ease infinite}
.lp-glow{animation:kit-glow 1.8s ease-in-out infinite}
html.anim-on .lp-wave.is-in .ch{animation:kit-wave 1.6s ease-in-out infinite;animation-delay:calc(var(--i,0)*90ms)}
"""

KIT_JS = """/* ============================================================
   anim.js — スクロール出現の再生役（依存ゼロ・これ1つでOK）
   仕組み：IntersectionObserverで「画面に入った要素」に .is-in を付けるだけ。
   アニメの見た目は全部 anim.css 側にある。
   ============================================================ */
(function () {
  var d = document, h = d.documentElement;
  if (!('IntersectionObserver' in window)) return;  // 古い環境では全部表示のまま（保険）
  h.classList.add('anim-on');

  // data-delay / data-dur をCSS変数に橋渡し
  [].slice.call(d.querySelectorAll('[data-delay]')).forEach(function (el) {
    el.style.setProperty('--delay', (+el.getAttribute('data-delay') || 0) + 'ms');
  });
  [].slice.call(d.querySelectorAll('[data-dur]')).forEach(function (el) {
    el.style.setProperty('--dur', (+el.getAttribute('data-dur') || 800) + 'ms');
  });

  // .chars ＝中身を1文字ずつ<span class="ch">に分割（40ms間隔で時間差）
  [].slice.call(d.querySelectorAll('.chars')).forEach(function (el) {
    if (el.querySelector('.ch')) return;
    var text = el.textContent; el.textContent = '';
    [].slice.call(text).forEach(function (c, i) {
      var s = d.createElement('span'); s.className = 'ch';
      s.style.setProperty('--i', i);
      s.style.transitionDelay = 'calc(var(--delay,0ms) + ' + (i * 40) + 'ms)';
      s.textContent = (c === ' ') ? '\\u00A0' : c;
      el.appendChild(s);
    });
  });

  // .rv-lines ＝行(.lni)ごとに130msずつ時間差
  [].slice.call(d.querySelectorAll('.rv-lines')).forEach(function (el) {
    [].slice.call(el.querySelectorAll('.lni')).forEach(function (li, i) {
      li.style.transitionDelay = 'calc(var(--delay,0ms) + ' + (i * 130) + 'ms)';
    });
  });

  // .cnt ＝文字中の最初の数字を0→目標値（カンマ・小数の書式は維持）
  function countUp(el) {
    var m = (el.textContent || '').match(/[-+]?[\\d,]+(?:\\.\\d+)?/);
    if (!m) return;
    var keep = el.innerHTML, raw = m[0];
    var tgt = parseFloat(raw.replace(/,/g, '')), dec = (raw.split('.')[1] || '').length;
    var com = raw.indexOf(',') >= 0;
    var pre = (el.textContent || '').slice(0, m.index), suf = (el.textContent || '').slice(m.index + raw.length);
    var dur = (+el.getAttribute('data-dur') || 1200), t0 = null;
    function fmt(v) {
      var s = v.toFixed(dec);
      if (com) { var p = s.split('.'); p[0] = p[0].replace(/\\B(?=(\\d{3})+(?!\\d))/g, ','); s = p.join('.'); }
      return s;
    }
    function step(ts) {
      if (t0 === null) t0 = ts;
      var p = Math.min(1, (ts - t0) / dur), e = 1 - Math.pow(1 - p, 3);
      el.textContent = pre + fmt(tgt * e) + suf;
      if (p < 1) requestAnimationFrame(step); else el.innerHTML = keep;
    }
    requestAnimationFrame(step);
  }
  window.__kitCountUp = countUp;  // キットページのデモ再生用（本番サイトでは未使用・消してもOK）

  var io = new IntersectionObserver(function (entries) {
    entries.forEach(function (en) {
      if (!en.isIntersecting) return;
      var el = en.target; io.unobserve(el);
      el.classList.add('is-in');
      if (el.classList.contains('cnt')) {
        setTimeout(function () { countUp(el); }, +el.getAttribute('data-delay') || 0);
      }
    });
  }, { threshold: 0.25, rootMargin: '0px 0px -8% 0px' });

  [].slice.call(d.querySelectorAll('.rv,.rv-lines,.mk,.ud,.chars,.cnt')).forEach(function (el) { io.observe(el); });
})();
"""

# ── カンプの静的スキャン ─────────────────────────────────────────────────
_TAG_RE = re.compile(r"<([a-zA-Z][a-zA-Z0-9]*)((?:\"[^\"]*\"|'[^']*'|[^>\"'])*)>")
_CLASS_RE = re.compile(r"""class\s*=\s*("([^"]*)"|'([^']*)')""", re.IGNORECASE)
_DELAY_RE = re.compile(r"""data-cedelay\s*=\s*["']?(\d+)""", re.IGNORECASE)
_STYLE_RE = re.compile(r"""style\s*=\s*("([^"]*)"|'([^']*)')""", re.IGNORECASE)
_KF_RE = re.compile(r"@keyframes\s+([\w-]+)\s*\{", re.IGNORECASE)


def _esc(t) -> str:
    return _html.escape(str(t or ""))


def _style_var(style: str, name: str):
    m = re.search(re.escape(name) + r"\s*:\s*([^;\"']+)", style or "")
    return m.group(1).strip() if m else None


def _dur_ms(v: str):
    """'1.2s' / '800ms' / '0.45'(秒) / '1200'(ms) を ms に揃える。"""
    if not v:
        return None
    v = v.strip()
    try:
        if v.endswith("ms"):
            return int(float(v[:-2]))
        if v.endswith("s"):
            return int(float(v[:-1]) * 1000)
        f = float(v)
        return int(f * 1000) if f < 30 else int(f)  # 30未満は秒とみなす
    except ValueError:
        return None


def _snippet(html: str, pos: int) -> str:
    raw = html[pos:pos + 200]
    txt = re.sub(r"<[^>]+>", " ", raw)
    txt = re.sub(r"\s+", " ", txt).strip()
    return txt[:22]


def scan_camp(html: str) -> list[dict]:
    """カンプHTMLからアニメ付き要素を列挙する（対応表の行データ）。"""
    spans = [(m.start(), m.end()) for m in camp._SEC_RE.finditer(html)]

    def sec_of(pos: int) -> str:
        for i, (s, e) in enumerate(spans):
            if s <= pos < e:
                return f"セクション{i + 1}"
        hm = re.search(r"<header\b.*?</header>", html, flags=re.DOTALL | re.IGNORECASE)
        if hm and hm.start() <= pos < hm.end():
            return "ヘッダー"
        fm = re.search(r"<footer\b.*?</footer>", html, flags=re.DOTALL | re.IGNORECASE)
        if fm and fm.start() <= pos < fm.end():
            return "フッター"
        return "ページ"

    rows = []
    for m in _TAG_RE.finditer(html):
        tag, attrs = m.group(1).lower(), m.group(2)
        if tag in ("style", "script", "html", "body", "head", "meta", "link"):
            continue
        cm = _CLASS_RE.search(attrs)
        classes = (cm.group(2) if cm and cm.group(2) is not None else (cm.group(3) if cm else "") or "").split()
        dm = _DELAY_RE.search(attrs)
        has_anim = any(c.startswith("fxa_") or c == "reveal" for c in classes)
        if not has_anim and not dm:
            continue
        # 分割の破片（1文字span・行の窓）は載せない
        if any(c in _SKIP_CLASSES for c in classes) and not any(
                c.startswith("fxa_") and c not in _SKIP_CLASSES for c in classes):
            continue
        sm = _STYLE_RE.search(attrs)
        style = (sm.group(2) if sm and sm.group(2) is not None else (sm.group(3) if sm else "") or "")

        ja, kit, notes = [], [], []
        has_fxa = any(c.startswith("fxa_") and c not in _SKIP_CLASSES for c in classes)
        for key, jname, kcls, note in _FX_MAP:
            if key == "reveal" and has_fxa:
                continue  # fxaの具体的な動きがある要素に、汎用reveal(rv-up)を重ねない
            if key in classes:
                ja.append(jname)
                kit.append(kcls)
                if note:
                    notes.append(note)
        if not ja and dm:
            ja.append("（遅らせ指定のみ）")
        # 速さ・遅らせ・色をdata属性/変数へ翻訳
        extra = []
        if dm:
            extra.append(f'data-delay="{dm.group(1)}"')
        dur = _dur_ms(_style_var(style, "--fxa-dur") or "") or _dur_ms(_style_var(style, "--hldur") or "")
        if dur:
            extra.append(f'data-dur="{dur}"')
        hlc = _style_var(style, "--hlc")
        if hlc and "fxa_hl" in classes:
            notes.append(f"マーカー色: --mk-c:{hlc}")
        udc = _style_var(style, "--udc")
        if udc and "fxa_ud" in classes:
            notes.append(f"下線色: --ud-c:{udc}")

        label_cls = [c for c in classes if not c.startswith("fxa_") and not c.startswith("__ce")
                     and c not in ("reveal", "in", "is-visible", "show")][:2]
        label = tag + (("." + ".".join(label_cls)) if label_cls else "")
        kit_txt = " ".join(dict.fromkeys(" ".join(kit).split())) if kit else "—"
        if extra:
            kit_txt += " ＋ " + " ".join(extra)
        rows.append({
            "sec": sec_of(m.start()),
            "el": label,
            "text": _snippet(html, m.end()),
            "ja": "／".join(dict.fromkeys(ja)),
            "kit": kit_txt,
            "note": "。".join(dict.fromkeys(notes)),
        })
    return rows


def _custom_keyframes(html: str) -> list[str]:
    """カンプ独自の@keyframes（fxa/kit以外）をブロックごと抜き出す。"""
    out = []
    for m in _KF_RE.finditer(html):
        name = m.group(1)
        if name.startswith(("fxa", "kit-", "__ce")):
            continue
        i = html.find("{", m.end() - 1)
        depth, j = 0, i
        while j < len(html):
            if html[j] == "{":
                depth += 1
            elif html[j] == "}":
                depth -= 1
                if depth == 0:
                    break
            j += 1
        out.append(html[m.start():j + 1])
    return out


# クラス名カタログ（グループ名, [(キットのクラス, 表示名, 一言メモ, サンプル文字)]）
# ⑤で全種類を「押すと動く」形で見せる。このカンプで使用中のものにはバッジが付く
_CATALOG = [
    ("✨ 出現系（rv とセットで使う・スクロールで画面に入ると1回だけ動く）", [
        ("rv rv-up", "ふわっと出現（下から）", "一番よく使う基本形", "見出しテキスト"),
        ("rv rv-down", "上から降りる", "ヘッダー向き", "ヘッダーなど"),
        ("rv rv-left", "左からスライド", "", "テキスト"),
        ("rv rv-right", "右からスライド", "", "テキスト"),
        ("rv rv-zoom", "ズーム出現", "カード・写真向き", "カード"),
        ("rv rv-blur", "ピンぼけ→くっきり", "", "ぼかし出現"),
        ("rv rv-flip", "3D回転", "", "パタッと回転"),
        ("rv rv-page", "📖ページめくり", "左端が軸", "ページが開く"),
        ("rv rv-curtain-l", "カーテン開き（左から）", "写真向き", "写真の代わり"),
        ("rv rv-curtain-c", "カーテン開き（真ん中から）", "写真向き", "写真の代わり"),
    ]),
    ("📝 文字系", [
        ("rv-lines", "行ごとに下からせり上がる", "HTML側で行を .ln>.lni で包む（rv不要）",
         "<span class='ln'><span class='lni'>1行目がせり上がり</span></span>"
         "<span class='ln'><span class='lni'>2行目が続く</span></span>"),
        ("chars", "1文字ずつ出現", "分割はJSが自動でやる（rv不要）", "ようこそ"),
        ("cnt", "🔢カウントアップ", "文中の最初の数字が0→目標値（rv不要）", "1,250件の実績"),
    ]),
    ("🖍 線・強調系（rv不要）", [
        ("mk", "マーカー（左から伸びる）", "色は style=\"--mk-c:#ffe66d\" で変更", "大事なところに線"),
        ("ud", "〰点線下線（左から引かれる）", "色は style=\"--ud-c:#0b6bcb\" で変更", "さりげない強調"),
    ]),
    ("🔁 ループ系（ずっと動き続ける・rv不要・付けるだけ）", [
        ("lp-pulse", "ドクドク拡縮", "ボタン・CTA向き", "ボタン"),
        ("lp-float", "ふわふわ浮遊", "イラスト・バッジ向き", "ふわふわ"),
        ("lp-bounce", "ぴょんぴょん", "", "ぴょん"),
        ("lp-glow", "光る", "", "キラッと光る"),
        ("chars lp-wave", "波打つ文字", "chars とセットで使う", "なみなみもじ"),
    ]),
]
# rv-linesだけサンプルが生HTML（.ln>.lni構造が必要）。エスケープせず出す
_RAW_SAMPLE = {"rv-lines"}


def build_kit(filename: str) -> dict:
    """カンプをスキャンして実装キットHTMLを書き出す。戻り値 {file, rows}。"""
    src = config.CAMP_DIR / filename
    if not src.exists():
        raise FileNotFoundError(f"カンプが見つかりません: {filename}")
    KIT_DIR.mkdir(parents=True, exist_ok=True)
    html = src.read_text(encoding="utf-8")
    rows = scan_camp(html)
    kfs = _custom_keyframes(html)

    # セクションごとに「まとめ行」を挟む（1行ずつ読まなくても全体像が掴めるように）
    parts = []
    for sec, grp_iter in groupby(rows, key=lambda r: r["sec"]):
        grp = list(grp_iter)
        combos: dict[str, int] = {}
        for r in grp:
            base = r["kit"].split("＋")[0].strip()
            combos[base] = combos.get(base, 0) + 1
        summary = "　".join(
            f"<code>{_esc(k)}</code>×{v}" if v > 1 else f"<code>{_esc(k)}</code>"
            for k, v in combos.items()
        )
        parts.append(f"<tr class='secrow'><td colspan='5'>📍 {_esc(sec)}（{len(grp)}個）　使う動き: {summary}</td></tr>")
        for r in grp:
            parts.append(
                f"<tr><td>{_esc(r['sec'])}</td><td><code>{_esc(r['el'])}</code><br><span class='dim'>{_esc(r['text'])}</span></td>"
                f"<td>{_esc(r['ja'])}</td><td><code>{_esc(r['kit'])}</code></td><td class='dim'>{_esc(r['note'])}</td></tr>"
            )
    tr = "".join(parts) or "<tr><td colspan='5' class='dim'>アニメ付きの要素は見つかりませんでした</td></tr>"
    kinds = len({r["kit"].split("＋")[0].strip() for r in rows})

    demos_used = set()
    for r in rows:
        for cls in r["kit"].split("＋")[0].split():
            demos_used.add(cls)
    demo_cards = []
    for group, items in _CATALOG:
        cards = []
        for kcls, name, note, sample in items:
            badge = "<span class='badge'>このカンプで使用</span>" if set(kcls.split()) & demos_used else ""
            body = sample if kcls in _RAW_SAMPLE else _esc(sample)
            note_html = f"<div class='dnote'>{_esc(note)}</div>" if note else ""
            cards.append(
                f"<div class='dcard'><div class='dhead'><code>{_esc(kcls)}</code> {_esc(name)}{badge}"
                f"<button class='replay'>▶</button></div>"
                f"<div class='stage'><span class='{_esc(kcls)} demo'>{body}</span></div>{note_html}</div>"
            )
        demo_cards.append(f"<h3 class='grp'>{_esc(group)}</h3><div class='dgrid'>{''.join(cards)}</div>")

    kf_block = ""
    if kfs:
        kf_block = (
            "<h2>④ このカンプ独自の@keyframes（そのままCSSへコピー）</h2>"
            f"<textarea readonly rows='8'>{_esc(chr(10).join(kfs))}</textarea>"
        )

    title = re.search(r"<title>(.*?)</title>", html, flags=re.DOTALL | re.IGNORECASE)
    title_txt = re.sub(r"\s+", " ", title.group(1)).strip()[:40] if title else filename

    page = _PAGE_TMPL
    for k, v in {
        "%TITLE%": _esc(title_txt),
        "%FILE%": _esc(filename),
        "%DATE%": datetime.now().strftime("%Y-%m-%d"),
        "%COUNT%": str(len(rows)),
        "%KINDS%": str(kinds),
        "%TABLE%": tr,
        "%CSS%": _esc(KIT_CSS),
        "%JS%": _esc(KIT_JS),
        "%KF%": kf_block,
        "%DEMOS%": "".join(demo_cards),
        "%LIVECSS%": KIT_CSS,
        "%LIVEJS%": KIT_JS,
    }.items():
        page = page.replace(k, v)

    out = KIT_DIR / (src.stem + "_animkit.html")
    tmp = out.with_suffix(".html.tmp")
    tmp.write_text(page, encoding="utf-8")
    tmp.replace(out)
    log.info("アニメ実装キットを保存: %s（%d要素）", out.name, len(rows))
    return {"file": out.name, "rows": len(rows)}


_PAGE_TMPL = """<!DOCTYPE html><html lang="ja"><head><meta charset="utf-8">
<title>🎬 アニメ実装キット: %TITLE%</title>
<style>
*{box-sizing:border-box}
body{font-family:'Hiragino Sans','Yu Gothic',sans-serif;margin:0;padding:28px;background:#f4f6f8;color:#223;line-height:1.8}
h1{font-size:20px;margin:0 0 4px}
h2{font-size:16px;margin:28px 0 10px;border-left:4px solid #0b6bcb;padding-left:10px}
.note{font-size:13px;color:#567}
.step{background:#fff;border-radius:10px;padding:14px 18px;font-size:13.5px;box-shadow:0 2px 10px rgba(20,40,80,.08)}
textarea{width:100%;min-height:180px;font:12px/1.6 Consolas,monospace;padding:12px;border:1px solid #cdd;border-radius:8px;background:#1e252e;color:#cde}
.copy{margin:6px 0 0;padding:8px 20px;border:0;border-radius:6px;background:#0b6bcb;color:#fff;font-size:13px;cursor:pointer}
table{width:100%;border-collapse:collapse;background:#fff;border-radius:10px;overflow:hidden;box-shadow:0 2px 10px rgba(20,40,80,.08);font-size:12.5px}
th{background:#25314a;color:#fff;padding:8px 10px;text-align:left;font-weight:600}
td{padding:8px 10px;border-top:1px solid #e8ecf1;vertical-align:top}
code{background:#eef2f7;padding:1px 6px;border-radius:4px;font-size:12px;color:#0b4d8c}
td code{background:#e8f3e8;color:#1b5e20}
.secrow td{background:#eef4fb;font-weight:600;font-size:12.5px;border-top:2px solid #cfe0f2}
.dim{color:#8a97a5;font-size:11.5px}
pre{background:#1e252e;color:#cde;padding:10px 14px;border-radius:8px;font-size:12px;line-height:1.7;overflow:auto}
.mini{font-size:12.5px;margin-top:6px}
.mini td{padding:5px 8px}
.mini td:first-child{white-space:nowrap}
h3.grp{font-size:13.5px;margin:20px 0 8px;color:#456}
.badge{background:#e8f3e8;color:#1b5e20;border-radius:10px;padding:1px 8px;font-size:10.5px;margin-left:6px;white-space:nowrap}
.dnote{padding:6px 12px;font-size:11px;color:#8a97a5;background:#fafbfc;border-top:1px solid #eef1f5}
.flow{display:flex;flex-wrap:wrap;gap:6px;align-items:center;margin:8px 0;font-size:12.5px}
.flow span{background:#eef4fb;border:1px solid #cfe0f2;border-radius:8px;padding:5px 10px}
.flow b.ar{color:#0b6bcb}
.dgrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:12px}
.dcard{background:#fff;border-radius:10px;box-shadow:0 2px 10px rgba(20,40,80,.08);overflow:hidden}
.dhead{display:flex;align-items:center;gap:6px;padding:8px 12px;font-size:12px;background:#f0f3f7}
.dhead .replay{margin-left:auto;border:0;background:#0b6bcb;color:#fff;border-radius:5px;padding:2px 10px;cursor:pointer}
.stage{padding:34px 16px;text-align:center;font-size:18px;font-weight:700;min-height:96px}
.stage .mk{--mk-c:#ffe66d}
%LIVECSS%
</style></head><body>
<h1>🎬 アニメ実装キット</h1>
<p class="note">元カンプ: %FILE%（%DATE% 生成・実測ベース）／ アニメ付き要素 %COUNT% 件。<br>
このページの内容だけで、カンプと同じ動きを<b>依存ライブラリなし</b>で再現できます。</p>

<h2>⓪ 1分でわかる仕組み（はじめての人はここから）</h2>
<div class="step">
<b>結論：自分のHTMLに「動き用のクラス」を追加で付けるだけ。JSは書かない・触らない。</b><br><br>
<b>🔍 何が起きているか</b>
<div class="flow"><span>ページをスクロール</span><b class="ar">→</b><span>要素が画面に入る</span><b class="ar">→</b><span>anim.js がその要素に <code>is-in</code> を自動で付ける</span><b class="ar">→</b><span>anim.css の「is-inが付いたら見せる」スタイルが効く</span><b class="ar">→</b><span>ふわっと出現</span></div>
<b>✍ 書き方（Before → After）</b>　自分のクラス名（BEM等）はそのまま、後ろに足すだけ
<pre>Before: &lt;h2 class="about__title"&gt;私たちについて&lt;/h2&gt;
After : &lt;h2 class="about__title rv rv-up" data-delay="200"&gt;私たちについて&lt;/h2&gt;</pre>
<b>📋 それぞれの役割（自分で書くのは上3つだけ）</b>
<table class="mini"><tbody>
<tr><td><code>rv</code></td><td>「スクロールで出現させる」合図。これが無いと動かない（出現系のみ必要）</td></tr>
<tr><td><code>rv-up</code></td><td>動き方の種類。⑤のカタログから好きなものに変えられる</td></tr>
<tr><td><code>data-delay="200"</code></td><td>200ms（0.2秒）待ってから動く。順番に出したい時に使う。省略OK</td></tr>
<tr><td><code>is-in</code></td><td>❌自分では書かない。JSが「画面に入ったよ」の印として自動で付ける</td></tr>
<tr><td><code>html.anim-on</code></td><td>❌自分では書かない。anim.jsが起動時に&lt;html&gt;へ付ける「JSが動いた証明」。<br>
CSSの隠す指定を全部この中に入れてあるので、<b>JSが無効・読み込み失敗でも要素が消えず全部表示される</b>（保険）</td></tr>
</tbody></table>
<br>⚠ <b>よくあるつまずき</b>：①<code>is-in</code>や<code>anim-on</code>をHTMLに手で書く（→最初から出現済みになりアニメしない）
②anim.jsの読み込み忘れ（→保険が働き全部最初から表示＝動かないけど消えもしない。動かない時はまずこれを疑う）
③<code>rv-up</code>だけ付けて<code>rv</code>を忘れる（→何も起きない）
</div>

<h2>① 使い方（3ステップ）</h2>
<div class="step">
1. 下の <b>anim.css</b> をコピーしてCSSファイルに貼る（または&lt;style&gt;で読み込む）<br>
2. 下の <b>anim.js</b> をコピーして&lt;/body&gt;直前の&lt;script&gt;に貼る<br>
3. ③の対応表どおり、各要素にクラスと data-delay 等を付ける → スクロールで同じ動きになります
</div>

<h2>② コピーするコード</h2>
<b style="font-size:13px">anim.css</b>
<textarea readonly id="css">%CSS%</textarea>
<button class="copy" data-for="css">📋 CSSをコピー</button>
<br><br><b style="font-size:13px">anim.js</b>
<textarea readonly id="js">%JS%</textarea>
<button class="copy" data-for="js">📋 JSをコピー</button>

<h2>③ どの要素に何を付けるか（対応表）</h2>
<p class="note">長く見えますが、<b>使う動きは実質 %KINDS% 種類だけ</b>です。青い「📍まとめ行」でセクションごとの全体像が掴めます。<br>
おすすめの進め方：全部を先に対応させようとせず、<b>HTMLを書きながら「今作っている場所のまとめ行」だけ見る</b>→同じ動きの要素はクラスをコピペ。<br>
「要素」列のクラス名はカンプ内の名前（＝場所を探すための住所）。自分のHTMLのクラス名はそのままでOK、足すのは「付けるクラス・属性」列だけです。</p>
<table><thead><tr><th style="width:90px">場所</th><th>要素</th><th style="width:170px">動き</th><th>付けるクラス・属性</th><th style="width:180px">メモ</th></tr></thead>
<tbody>%TABLE%</tbody></table>

%KF%

<h2>⑤ クラス名カタログ（全種類・▶でその場再生）</h2>
<p class="note">どんな動きか迷ったらここ。カードの<code>クラス名</code>をそのままHTMLに付ければ同じ動きになります。「このカンプで使用」バッジ付きが対応表に出てくるものです。</p>
%DEMOS%

<script>%LIVEJS%</script>
<script>
document.querySelectorAll('.copy').forEach(function(b){
  b.addEventListener('click',function(){
    var t=document.getElementById(b.getAttribute('data-for'));
    t.select(); document.execCommand('copy');
    var o=b.textContent; b.textContent='コピーしました ✅'; setTimeout(function(){b.textContent=o;},1200);
  });
});
document.querySelectorAll('.replay').forEach(function(b){
  b.addEventListener('click',function(){
    var el=b.closest('.dcard').querySelector('.demo');
    el.classList.remove('is-in');
    el.style.animation='none'; void el.offsetWidth; el.style.animation='';
    el.classList.add('is-in');
    if(el.classList.contains('cnt') && window.__kitCountUp) window.__kitCountUp(el);
  });
});
</script>
</body></html>"""
