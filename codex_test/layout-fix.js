(function () {
  'use strict';

  function important(el, name, value) {
    if (el) el.style.setProperty(name, value, 'important');
  }

  function place(el, x, y, width, height) {
    if (!el) return;
    important(el, 'left', x + 'px');
    important(el, 'top', y + 'px');
    important(el, 'translate', '0px 0px');
    important(el, 'transform', 'none');
    if (width != null) important(el, 'width', width + 'px');
    if (height != null) important(el, 'height', height + 'px');
  }

  function applyCodexTestLayout() {
    var section = document.querySelector('section.ce_newsec');
    if (!section) return;

    document.title = 'codex_test | ' + document.title.replace(/^codex_test \| /, '');
    section.setAttribute('data-codex-test', 'work-items-aligned');
    important(section, 'translate', '37px -110px');
    important(section, 'height', '1180px');
    important(section, 'z-index', '1');
    important(section, 'overflow', 'hidden');

    var photos = Array.from(section.querySelectorAll('img[data-ceframe="1"]')).slice(0, 3);
    var photoPositions = [
      { x: 320, y: 205 },
      { x: 1035, y: 495 },
      { x: 320, y: 785 }
    ];
    photos.forEach(function (photo, index) {
      photo.src = './assets/生成画像' + (index + 1) + '.png';
      place(photo, photoPositions[index].x, photoPositions[index].y, 540, 250);
      important(photo, 'object-fit', 'cover');
      important(photo, 'border', '10px solid #fff');
      important(photo, 'border-radius', '12px 36px 12px 12px');
      important(photo, 'box-shadow', '0 8px 20px rgba(58, 82, 68, 0.10)');
    });

    var numbers = ['01', '02', '03'].map(function (number) {
      var image = section.querySelector('img[src*="section-number-' + number + '-transparent.png"]');
      if (image) image.src = './assets/section-number-' + number + '-transparent.png';
      return image;
    });
    [[930, 278], [645, 568], [930, 858]].forEach(function (position, index) {
      place(numbers[index], position[0], position[1], 78, 78);
      important(numbers[index], 'object-fit', 'contain');
      important(numbers[index], 'z-index', '54');
    });

    var divs = Array.from(section.querySelectorAll('div'));
    var titles = divs.filter(function (el) {
      return el.style.fontSize === '32px' && parseFloat(getComputedStyle(el).fontSize) >= 31;
    }).slice(0, 3);
    var descriptions = divs.filter(function (el) {
      return el.style.fontSize === '23.1px';
    }).slice(0, 3);
    var heading = divs.find(function (el) {
      return parseFloat(getComputedStyle(el).fontSize) > 40;
    });
    var labelBackgrounds = Array.from(section.querySelectorAll('[data-ceshape="rect"]')).slice(0, 3);

    place(heading, 595, 92, 710, 68);
    important(heading, 'text-align', 'center');

    [[1030, 268], [745, 558], [1030, 848]].forEach(function (position, index) {
      place(titles[index], position[0], position[1], null, 53);
      important(titles[index], 'z-index', '54');
    });
    [[1030, 326], [745, 616], [1030, 906]].forEach(function (position, index) {
      place(descriptions[index], position[0], position[1], 270, 73);
      important(descriptions[index], 'z-index', '53');
      important(descriptions[index], 'line-height', '1.5');
    });

    [
      { x: 1012, y: 245, w: 230 },
      { x: 727, y: 535, w: 250 },
      { x: 1012, y: 825, w: 340 }
    ].forEach(function (position, index) {
      place(labelBackgrounds[index], position.x, position.y, position.w, 94);
      important(labelBackgrounds[index], 'z-index', '52');
      important(labelBackgrounds[index], 'filter', 'blur(4px)');
    });

    var leaf = section.querySelector('img[src*="decorative-leaves-transparent.png"]');
    if (leaf) leaf.src = './assets/decorative-leaves-transparent.png';
    Array.from(section.querySelectorAll('img[src*="decorative-dots-lines-transparent.png"]')).forEach(function (image) {
      image.src = './assets/decorative-dots-lines-transparent.png';
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', applyCodexTestLayout, { once: true });
  } else {
    applyCodexTestLayout();
  }
  requestAnimationFrame(applyCodexTestLayout);
  setTimeout(applyCodexTestLayout, 900);
  setTimeout(applyCodexTestLayout, 2800);
})();
