(function () {
  function initAmPmToggles(root) {
    const scope = root || document;
    const timeInputs = scope.querySelectorAll("input[type='time']");
    if (!timeInputs.length) return;

    function parseTime(value) {
      const v = String(value || "").trim();
      const m = /^(\d{2}):(\d{2})$/.exec(v);
      if (!m) return null;
      const hh = parseInt(m[1], 10);
      const mm = parseInt(m[2], 10);
      if (Number.isNaN(hh) || Number.isNaN(mm)) return null;
      return { hh: Math.max(0, Math.min(23, hh)), mm: Math.max(0, Math.min(59, mm)) };
    }

    function toValue(hh, mm) {
      return String(hh).padStart(2, "0") + ":" + String(mm).padStart(2, "0");
    }

    function getPeriod(hh) {
      return hh >= 12 ? "pm" : "am";
    }

    function setButtonsState(wrapper, period) {
      const amBtn = wrapper.querySelector("[data-period='am']");
      const pmBtn = wrapper.querySelector("[data-period='pm']");
      if (!amBtn || !pmBtn) return;
      amBtn.classList.toggle("active", period === "am");
      pmBtn.classList.toggle("active", period === "pm");
      amBtn.setAttribute("aria-pressed", period === "am" ? "true" : "false");
      pmBtn.setAttribute("aria-pressed", period === "pm" ? "true" : "false");
    }

    function syncFromInput(input, wrapper) {
      const parsed = parseTime(input.value);
      const period = parsed ? getPeriod(parsed.hh) : "am";
      setButtonsState(wrapper, period);
    }

    timeInputs.forEach((input) => {
      if (input.dataset.ampmReady === "1") return;
      if (!input.name || (!input.name.endsWith("-start_time") && !input.name.endsWith("-end_time"))) return;
      input.dataset.ampmReady = "1";

      const wrapper = document.createElement("div");
      wrapper.className = "time-ampm-toggle";
      wrapper.innerHTML = [
        "<button type='button' class='time-period-btn' data-period='am' aria-label='AM'>AM</button>",
        "<button type='button' class='time-period-btn' data-period='pm' aria-label='PM'>PM</button>"
      ].join("");

      input.insertAdjacentElement("afterend", wrapper);
      syncFromInput(input, wrapper);

      wrapper.querySelectorAll(".time-period-btn").forEach((btn) => {
        btn.addEventListener("click", function () {
          if (input.disabled) return;
          const target = btn.getAttribute("data-period");
          let parsed = parseTime(input.value);
          if (!parsed) {
            parsed = { hh: 9, mm: 0 };
          }
          let hh = parsed.hh;
          if (target === "am" && hh >= 12) hh -= 12;
          if (target === "pm" && hh < 12) hh += 12;
          input.value = toValue(hh, parsed.mm);
          input.dispatchEvent(new Event("input", { bubbles: true }));
          input.dispatchEvent(new Event("change", { bubbles: true }));
          syncFromInput(input, wrapper);
        });
      });

      input.addEventListener("change", function () {
        syncFromInput(input, wrapper);
      });
      input.addEventListener("input", function () {
        syncFromInput(input, wrapper);
      });
    });
  }

  function initCopyBookingLink() {
    const input = document.getElementById('bookingLink');
    const btn = document.getElementById('copyBookingLinkBtn');
    if (!input || !btn) return;
    const originalText = btn.textContent;

    btn.addEventListener('click', async function () {
      try {
        await navigator.clipboard.writeText(input.value);
      } catch (_) {
        input.select();
        document.execCommand('copy');
      }

      btn.textContent = 'Copiado';
      btn.classList.add('is-success');
      if (typeof window.appNotify === 'function') {
        window.appNotify('Enlace copiado al portapapeles.', { type: 'success', ms: 2600 });
      }
      window.setTimeout(function () {
        btn.textContent = originalText;
        btn.classList.remove('is-success');
      }, 1400);
    });
  }

  function initSubmitLock() {
    document.querySelectorAll('form[data-submit-lock]').forEach((form) => {
      form.addEventListener('submit', function () {
        const submitText = form.getAttribute('data-submit-text') || 'Guardando...';
        form.querySelectorAll('button[type="submit"]').forEach((btn) => {
          btn.disabled = true;
          btn.dataset.originalText = btn.textContent;
          btn.textContent = submitText;
          btn.classList.add('is-busy');
        });
      });
    });
  }

  function initAvailabilityEditor() {
    const empty = document.getElementById('avail-empty');
    if (!empty) return;

    const totalInput = document.querySelector('input[id^="id_"][id$="-TOTAL_FORMS"]');
    if (!totalInput) return;

    const prefix = totalInput.id.replace(/^id_/, '').replace(/-TOTAL_FORMS$/, '');

    function getVisibleRows(grid) {
      return grid.querySelectorAll('.avail-row:not([style*="display: none"])');
    }

    function setDayEnabled(dayInt, enabled) {
      const grid = document.querySelector('[data-day-grid="' + dayInt + '"]');
      const state = document.querySelector('[data-day-state="' + dayInt + '"]');
      const emptyHint = document.querySelector('[data-day-empty="' + dayInt + '"]');
      if (state) state.textContent = enabled ? 'Disponible' : 'No disponible';

      if (!grid) return;

      grid.querySelectorAll('input, select, textarea').forEach((el) => {
        const name = el.getAttribute('name') || '';
        const type = (el.getAttribute('type') || '').toLowerCase();
        if (name.endsWith('-DELETE')) return;
        if (type === 'hidden' && name.endsWith('-id')) return;
        el.disabled = !enabled;
      });
      grid.querySelectorAll('.time-period-btn').forEach((btn) => {
        btn.disabled = !enabled;
      });

      if (!enabled) {
        grid.querySelectorAll('input[type="checkbox"][name$="-active"]').forEach((cb) => {
          cb.checked = false;
        });
      }

      if (emptyHint) {
        const hasVisibleRow = getVisibleRows(grid).length > 0;
        emptyHint.style.display = hasVisibleRow ? 'none' : 'block';
      }
    }

    function syncDayFromRows(dayInt) {
      const daySection = document.querySelector('.avail-day[data-weekday="' + dayInt + '"]');
      if (!daySection) return;

      const grid = daySection.querySelector('[data-day-grid="' + dayInt + '"]');
      const hasVisibleRow = grid ? getVisibleRows(grid).length > 0 : false;
      const dayToggle = daySection.querySelector('.dayToggle[data-weekday="' + dayInt + '"]');
      if (dayToggle) dayToggle.checked = hasVisibleRow;
      setDayEnabled(dayInt, hasVisibleRow);
    }

    function initDayToggles() {
      document.querySelectorAll('.dayToggle').forEach((toggle) => {
        const dayInt = parseInt(toggle.getAttribute('data-weekday') || '0', 10);
        const grid = document.querySelector('[data-day-grid="' + dayInt + '"]');
        const hasRow = grid ? getVisibleRows(grid).length > 0 : false;
        toggle.checked = hasRow;
        setDayEnabled(dayInt, toggle.checked);

        toggle.addEventListener('change', function () {
          setDayEnabled(dayInt, toggle.checked);
        });
      });
    }

    function addRowForDay(dayInt) {
      const grid = document.querySelector('[data-day-grid="' + dayInt + '"]');
      if (!grid) return;

      const index = parseInt(totalInput.value || '0', 10);
      const html = empty.innerHTML.replaceAll('__prefix__', String(index));

      const wrapper = document.createElement('div');
      wrapper.innerHTML = html;
      const row = wrapper.firstElementChild;
      if (!row) return;

      const weekdaySelect = row.querySelector('select[name="' + prefix + '-' + index + '-weekday"]');
      if (weekdaySelect) {
        weekdaySelect.value = String(dayInt);
      }

      const activeInput = row.querySelector('input[name="' + prefix + '-' + index + '-active"]');
      if (activeInput && activeInput.type === 'checkbox') {
        activeInput.checked = false;
      }

      grid.appendChild(row);
      initAmPmToggles(row);
      totalInput.value = String(index + 1);

      const dayToggle = document.querySelector('.dayToggle[data-weekday="' + dayInt + '"]');
      if (dayToggle && !dayToggle.checked) {
        dayToggle.checked = true;
        setDayEnabled(dayInt, true);
      }

      const emptyHint = document.querySelector('[data-day-empty="' + dayInt + '"]');
      if (emptyHint) emptyHint.style.display = 'none';

      const start = row.querySelector(
        'input[name="' + prefix + '-' + index + '-start_time"], select[name="' + prefix + '-' + index + '-start_time"]'
      );
      if (start) start.focus();
    }

    document.querySelectorAll('.availAddDayBtn').forEach((btn) => {
      btn.addEventListener('click', function () {
        const dayInt = parseInt(btn.getAttribute('data-weekday') || '0', 10);
        addRowForDay(dayInt);
      });
    });

    document.addEventListener('click', function (e) {
      const btn = e.target.closest('.availRemoveBtn');
      if (!btn) return;

      const row = btn.closest('.avail-row');
      if (!row) return;

      const deleteInput = row.querySelector('input[type="checkbox"][name$="-DELETE"]');
      const idInput = row.querySelector('input[name$="-id"]');

      if (idInput && idInput.value) {
        if (deleteInput) {
          deleteInput.checked = true;
        }
        row.querySelectorAll('input, select, textarea').forEach((el) => {
          const name = el.getAttribute('name') || '';
          if (name.endsWith('-DELETE')) return;
          el.disabled = true;
        });
        row.style.display = 'none';

        const daySection = row.closest('.avail-day');
        if (daySection) {
          const dayInt = parseInt(daySection.getAttribute('data-weekday') || '0', 10);
          syncDayFromRows(dayInt);
        }
        return;
      }

      row.querySelectorAll('input, select, textarea').forEach((el) => {
        const type = (el.getAttribute('type') || '').toLowerCase();
        if (type === 'checkbox' || type === 'radio') {
          el.checked = false;
        } else {
          el.value = '';
        }
        el.disabled = true;
      });

      row.style.display = 'none';

      const daySection = row.closest('.avail-day');
      if (daySection) {
        const dayInt = parseInt(daySection.getAttribute('data-weekday') || '0', 10);
        syncDayFromRows(dayInt);
      }
    });

    initDayToggles();
    initAmPmToggles(document);
  }

  function initAgendaFilter() {
    const select = document.getElementById('agendaDaySelect');
    const count = document.getElementById('agendaCount');
    if (!select) return;

    function apply() {
      const value = select.value;
      const blocks = Array.from(document.querySelectorAll('.agenda-day'));
      let shown = 0;

      blocks.forEach((block) => {
        const match = value === 'all' || block.getAttribute('data-day') === value;
        block.style.display = match ? 'block' : 'none';
        if (match) {
          shown += block.querySelectorAll('tbody tr').length;
        }
      });

      if (count) count.textContent = shown ? shown + ' sesión(es)' : '';
    }

    select.addEventListener('change', apply);

    const first = select.querySelector('option[value]:not([value="all"])');
    if (first) {
      select.value = first.value;
    }

    apply();
  }

  function initOnboardingDetailsToggle() {
    const btn = document.getElementById('toggleOnboardingDetailsBtn');
    const details = document.getElementById('onboardingDetails');
    if (!btn || !details) return;

    btn.addEventListener('click', function () {
      const isHidden = details.classList.contains('is-hidden');
      details.classList.toggle('is-hidden', !isHidden);
      btn.textContent = isHidden ? 'Ocultar detalles' : 'Ver detalles';
    });
  }

  initCopyBookingLink();
  document.addEventListener('DOMContentLoaded', function () {
    initSubmitLock();
    initAvailabilityEditor();
    initAgendaFilter();
    initOnboardingDetailsToggle();
    initAmPmToggles(document);

    const availRoot = document.querySelector('.avail-days');
    if (availRoot && typeof MutationObserver !== 'undefined') {
      const obs = new MutationObserver(function () {
        initAmPmToggles(availRoot);
      });
      obs.observe(availRoot, { childList: true, subtree: true });
    }
  });
})();
