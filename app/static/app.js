// Small progressive enhancements. Every page works without JavaScript.
(function () {
  "use strict";

  // Live "is this an existing customer?" check on the quick-entry form.
  var phone = document.querySelector("[data-phone-lookup]");
  var hint = document.getElementById("phone-hint");
  if (phone && hint) {
    var timer = null;
    var nameInput = document.getElementById("customer_name");
    var render = function (data) {
      hint.className = "lookup-hint";
      hint.textContent = "";
      if (!data) return;
      if (!data.valid) {
        hint.classList.add("is-invalid");
        hint.textContent = "Not a valid phone number yet.";
        return;
      }
      if (!data.customer) {
        hint.classList.add("is-new");
        hint.textContent = "New customer: " + data.display;
        return;
      }
      var c = data.customer;
      hint.classList.add("is-known");
      var text = "Existing customer" + (c.name ? " " + c.name : "") + " (" + c.total_inquiries + " inquiries).";
      hint.appendChild(document.createTextNode(text + " "));
      if (c.open_inquiries.length) {
        hint.appendChild(document.createTextNode("Open: "));
        c.open_inquiries.forEach(function (i, idx) {
          var a = document.createElement("a");
          a.href = "/inquiries/" + i.id;
          a.textContent = "#" + i.id + " " + i.product + " (" + i.status + (i.assigned_to ? ", " + i.assigned_to : "") + ")";
          hint.appendChild(a);
          if (idx < c.open_inquiries.length - 1) hint.appendChild(document.createTextNode(", "));
        });
      }
      if (nameInput && !nameInput.value && c.name) nameInput.value = c.name;
    };
    var lookup = function () {
      var value = phone.value.trim();
      if (value.replace(/\D/g, "").length < 9) { render(null); return; }
      fetch("/customers/lookup?phone=" + encodeURIComponent(value), {
        headers: { Accept: "application/json" },
        credentials: "same-origin",
      })
        .then(function (r) { return r.ok ? r.json() : null; })
        .then(render)
        .catch(function () { render(null); });
    };
    phone.addEventListener("input", function () {
      clearTimeout(timer);
      timer = setTimeout(lookup, 350);
    });
    if (phone.value) lookup();
  }

  // Show fields only when they apply (lost reason, next follow-up date, purchase date).
  // An element with data-show-for="<field name>" data-show-when="a,b" is shown only when
  // that field (a <select> or a radio group) has one of those values.
  var currentValue = function (name) {
    var checked = document.querySelector('input[type="radio"][name="' + name + '"]:checked');
    if (checked) return checked.value;
    var field = document.querySelector('[name="' + name + '"]');
    return field && field.type !== "radio" ? field.value : "";
  };
  var syncDependents = function () {
    document.querySelectorAll("[data-show-for]").forEach(function (el) {
      var wanted = el.getAttribute("data-show-when").split(",");
      var match = wanted.indexOf(currentValue(el.getAttribute("data-show-for"))) !== -1;
      var negate = el.hasAttribute("data-show-negate");
      el.classList.toggle("is-hidden", negate ? match : !match);
    });
  };
  document.addEventListener("change", syncDependents);
  syncDependents();

  // Confirmation for destructive actions.
  document.querySelectorAll("form[data-confirm]").forEach(function (form) {
    form.addEventListener("submit", function (e) {
      if (!window.confirm(form.getAttribute("data-confirm"))) e.preventDefault();
    });
  });

  // Auto-submit filter forms when a select changes.
  document.querySelectorAll("form[data-autosubmit] select").forEach(function (select) {
    select.addEventListener("change", function () { select.form.submit(); });
  });
})();
