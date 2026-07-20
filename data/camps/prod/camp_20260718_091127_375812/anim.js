/* ============================================================
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
      s.textContent = (c === ' ') ? '\u00A0' : c;
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
    var m = (el.textContent || '').match(/[-+]?[\d,]+(?:\.\d+)?/);
    if (!m) return;
    var keep = el.innerHTML, raw = m[0];
    var tgt = parseFloat(raw.replace(/,/g, '')), dec = (raw.split('.')[1] || '').length;
    var com = raw.indexOf(',') >= 0;
    var pre = (el.textContent || '').slice(0, m.index), suf = (el.textContent || '').slice(m.index + raw.length);
    var dur = (+el.getAttribute('data-dur') || 1200), t0 = null;
    function fmt(v) {
      var s = v.toFixed(dec);
      if (com) { var p = s.split('.'); p[0] = p[0].replace(/\B(?=(\d{3})+(?!\d))/g, ','); s = p.join('.'); }
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
