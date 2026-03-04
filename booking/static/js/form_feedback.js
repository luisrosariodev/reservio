document.addEventListener("DOMContentLoaded", function () {
  var forms = document.querySelectorAll("form[data-submit-lock]");
  if (!forms.length) return;

  forms.forEach(function (form) {
    form.addEventListener("submit", function (event) {
      if (form.dataset.submitting === "1") return;
      form.dataset.submitting = "1";

      // Preserve clicked submit button value (name=value) before disabling controls.
      var submitter = event.submitter || null;
      if (submitter && submitter.name) {
        var hidden = document.createElement("input");
        hidden.type = "hidden";
        hidden.name = submitter.name;
        hidden.value = submitter.value || "";
        form.appendChild(hidden);
      }

      var submitButtons = form.querySelectorAll('button[type="submit"], input[type="submit"]');
      submitButtons.forEach(function (btn) {
        if (!btn.dataset.originalLabel) {
          btn.dataset.originalLabel = btn.tagName === "INPUT" ? btn.value : btn.textContent;
        }
        var nextLabel = form.getAttribute("data-submit-text") || "Enviando...";
        if (btn.tagName === "INPUT") {
          btn.value = nextLabel;
        } else {
          btn.textContent = nextLabel;
        }
        btn.disabled = true;
        btn.classList.add("is-busy");
      });
    });
  });
});
