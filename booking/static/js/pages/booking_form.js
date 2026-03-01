(function () {
  function initDaySelector() {
    const dayButtons = document.querySelectorAll("[data-role='day-button']");
    const dayBlocks = document.querySelectorAll("[data-role='slots-day']");
    const dayLabelEl = document.querySelector("[data-role='current-day-label']");

    if (!dayButtons.length || !dayBlocks.length) {
      return;
    }

    function selectDay(dateStr, labelText) {
      dayButtons.forEach((btn) => {
        if (btn.getAttribute('data-date') === dateStr) {
          btn.classList.add('active');
        } else {
          btn.classList.remove('active');
        }
      });

      dayBlocks.forEach((block) => {
        const d = block.getAttribute('data-date');
        block.style.display = d === dateStr ? 'block' : 'none';
      });

      dayBlocks.forEach((block) => {
        if (block.getAttribute('data-date') === dateStr) {
          const hasSlots = block.querySelectorAll('.slot-item').length > 0;
          const emptyEl = block.querySelector("[data-role='empty-day']");
          if (emptyEl) {
            emptyEl.style.display = hasSlots ? 'none' : 'block';
          }
        }
      });

      if (dayLabelEl) {
        dayLabelEl.textContent = labelText || dateStr;
      }
    }

    const firstWithSlots = Array.from(dayButtons).find((btn) => btn.classList.contains('has-slots'));
    const initialBtn = firstWithSlots || dayButtons[0];

    if (initialBtn) {
      const initialDate = initialBtn.getAttribute('data-date');
      const initialLabel = initialBtn.getAttribute('data-day-label');
      selectDay(initialDate, initialLabel);
    }

    dayButtons.forEach((btn) => {
      btn.addEventListener('click', function () {
        const dateStr = this.getAttribute('data-date');
        const labelTxt = this.getAttribute('data-day-label');
        selectDay(dateStr, labelTxt);
      });
    });
  }

  function initFormValidation() {
    const formEl = document.querySelector('form[action*="/checkout/"]');
    const errorEl = document.getElementById('booking-error');
    if (!formEl) return;

    function showInlineError(message) {
      if (errorEl) {
        errorEl.textContent = message;
        errorEl.classList.remove('is-hidden');
        errorEl.scrollIntoView({ behavior: 'smooth', block: 'center' });
        return;
      }

      const header = document.querySelector('.card-header');
      if (header) {
        const box = document.createElement('div');
        box.className = 'error';
        box.id = 'booking-error';
        box.textContent = message;
        header.insertAdjacentElement('afterend', box);
        box.scrollIntoView({ behavior: 'smooth', block: 'center' });
      } else {
        if (typeof window.appNotify === 'function') {
          window.appNotify(message, { type: 'error', ms: 7000 });
        }
      }
    }

    formEl.addEventListener('submit', function (e) {
      const checkedSlots = document.querySelectorAll("input[name='timeslot_ids']:checked");
      if (!checkedSlots.length) {
        e.preventDefault();
        showInlineError('Selecciona al menos un horario para continuar.');
        return;
      }

      for (const slotCheck of checkedSlots) {
        const slotItem = slotCheck.closest('.slot-item');
        if (!slotItem) continue;
        const attendeeInputs = slotItem.querySelectorAll("input[name^='attendees_']");
        if (!attendeeInputs.length) continue;
        const selectedAttendees = slotItem.querySelectorAll("input[name^='attendees_']:checked");
        if (!selectedAttendees.length) {
          e.preventDefault();
          showInlineError('Selecciona al menos un asistente en cada horario marcado.');
          return;
        }
      }
    });
  }

  function initBulkAttendees() {
    const applyBtn = document.getElementById('applyBulkAttendeesBtn');
    if (!applyBtn) return;

    applyBtn.addEventListener('click', function () {
      const bulkValues = Array.from(
        document.querySelectorAll("[data-role='bulk-attendee']:checked")
      ).map((el) => el.value);

      const finalValues = bulkValues.length ? bulkValues : ['self'];
      const slotAttendeeInputs = document.querySelectorAll(".slot-item input[name^='attendees_']");
      slotAttendeeInputs.forEach((input) => {
        input.checked = finalValues.includes(input.value);
      });
    });
  }

  function scrollToError() {
    const err = document.getElementById('booking-error');
    if (err && !err.classList.contains('is-hidden')) {
      err.scrollIntoView({ behavior: 'smooth', block: 'center' });
    }
  }

  document.addEventListener('DOMContentLoaded', function () {
    initDaySelector();
    initFormValidation();
    initBulkAttendees();
    scrollToError();
  });
})();
