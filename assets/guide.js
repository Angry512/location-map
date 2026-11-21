(() => {
  'use strict';
  const tabs = [...document.querySelectorAll('[role="tab"][data-step]')];
  const panels = [...document.querySelectorAll('[role="tabpanel"]')];
  const previous = document.getElementById('previous-step');
  const next = document.getElementById('next-step');
  const position = document.getElementById('guide-position');
  let selected = 0;
  if (/iPhone|iPad|Android/i.test(navigator.userAgent) || (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1)) {
    document.getElementById('platform-hint').hidden = false;
  }
  function showStep(index, moveFocus = false) {
    selected = Math.max(0, Math.min(tabs.length - 1, index));
    tabs.forEach((tab, i) => {
      tab.setAttribute('aria-selected', String(i === selected));
      tab.tabIndex = i === selected ? 0 : -1;
      panels[i].hidden = i !== selected;
    });
    previous.disabled = selected === 0;
    next.disabled = selected === tabs.length - 1;
    position.textContent = `Step ${selected + 1} of ${tabs.length}`;
    if (moveFocus) {
      panels[selected].focus({preventScroll:true});
      panels[selected].scrollIntoView({block:'start',behavior:matchMedia('(prefers-reduced-motion: reduce)').matches?'auto':'smooth'});
    }
  }
  tabs.forEach((tab, index) => {
    tab.addEventListener('click', () => showStep(index));
    tab.addEventListener('keydown', event => {
      let target;
      if (['ArrowDown','ArrowRight'].includes(event.key)) target = (index + 1) % tabs.length;
      if (['ArrowUp','ArrowLeft'].includes(event.key)) target = (index - 1 + tabs.length) % tabs.length;
      if (event.key === 'Home') target = 0;
      if (event.key === 'End') target = tabs.length - 1;
      if (target !== undefined) {event.preventDefault();showStep(target);tabs[target].focus();}
    });
  });
  previous.addEventListener('click', () => showStep(selected - 1, true));
  next.addEventListener('click', () => showStep(selected + 1, true));
  showStep(0);
})();
