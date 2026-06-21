(function () {
  const toast = document.querySelector("[data-copy-toast]");
  const responseTooltip = document.querySelector("[data-response-tooltip-popup]");
  let activeResponseTarget = null;

  function positionResponseTooltip() {
    if (!responseTooltip || !activeResponseTarget || responseTooltip.hidden) {
      return;
    }

    const targetRect = activeResponseTarget.getBoundingClientRect();
    const tooltipRect = responseTooltip.getBoundingClientRect();
    const viewportPadding = 12;
    const gap = 8;
    const maxLeft = Math.max(viewportPadding, window.innerWidth - tooltipRect.width - viewportPadding);
    const left = Math.min(Math.max(targetRect.left, viewportPadding), maxLeft);
    const above = targetRect.top - tooltipRect.height - gap;
    const top = above >= viewportPadding
      ? above
      : Math.min(targetRect.bottom + gap, window.innerHeight - tooltipRect.height - viewportPadding);

    responseTooltip.style.left = `${left}px`;
    responseTooltip.style.top = `${Math.max(viewportPadding, top)}px`;
  }

  function showResponseTooltip(target) {
    if (!responseTooltip || !target) {
      return;
    }
    const text = target.dataset.responseTooltip;
    if (!text) {
      return;
    }
    activeResponseTarget = target;
    responseTooltip.textContent = text;
    responseTooltip.hidden = false;
    positionResponseTooltip();
  }

  function hideResponseTooltip(target) {
    if (!responseTooltip || (target && target !== activeResponseTarget)) {
      return;
    }
    responseTooltip.hidden = true;
    responseTooltip.textContent = "";
    activeResponseTarget = null;
  }

  function showToast(message, isError) {
    if (!toast) {
      return;
    }
    toast.textContent = message;
    toast.classList.toggle("error", Boolean(isError));
    toast.classList.add("show");
    window.clearTimeout(showToast.timer);
    showToast.timer = window.setTimeout(() => {
      toast.classList.remove("show");
    }, 1800);
  }

  async function copyText(text) {
    if (!text) {
      throw new Error("没有可复制的内容");
    }
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(text);
      return;
    }
    const input = document.createElement("textarea");
    input.value = text;
    input.setAttribute("readonly", "");
    input.style.position = "fixed";
    input.style.left = "-9999px";
    document.body.appendChild(input);
    input.select();
    document.execCommand("copy");
    input.remove();
  }

  async function fetchSecret(url) {
    const response = await fetch(url, {
      headers: { Accept: "application/json" },
      credentials: "same-origin",
    });
    if (!response.ok) {
      throw new Error("读取 Key 失败，请重新登录");
    }
    const payload = await response.json();
    if (!payload.api_key) {
      throw new Error("没有可复制的 Key");
    }
    return payload.api_key;
  }

  async function saveTestPreferences(providerId, values) {
    const body = new URLSearchParams();
    Object.entries(values).forEach(([key, value]) => {
      if (value !== undefined && value !== null) {
        body.set(key, value);
      }
    });
    const response = await fetch(`/providers/${providerId}/test-preferences`, {
      method: "POST",
      headers: {
        Accept: "application/json",
        "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
      },
      credentials: "same-origin",
      body,
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) {
      throw new Error(payload.error || "测试配置保存失败");
    }
    return payload;
  }

  function renderTestResult(cell, payload) {
    if (!cell) {
      return;
    }
    cell.replaceChildren();
    const line = document.createElement("div");
    line.className = "test-line";
    const status = document.createElement("span");
    const statusName = payload.status || "failed";
    const statusLabels = {
      success: "成功",
      failed: "失败",
      skipped: "跳过",
      testing: "测试中",
    };
    status.className = `status ${statusName}`;
    status.textContent = statusLabels[statusName] || "失败";
    const summary = document.createElement("span");
    summary.className = "test-summary muted small";
    if (statusName === "testing") {
      summary.textContent = "正在连接";
    } else if (statusName === "skipped") {
      summary.textContent = payload.error_message || "中转站已跳过";
      summary.title = summary.textContent;
    } else {
      const latency = payload.latency_ms === null || payload.latency_ms === undefined
        ? ""
        : ` · ${payload.latency_ms} ms`;
      summary.textContent = `${payload.model_id || "未知模型"}${latency}`;
      summary.title = statusName === "failed" && payload.error_message
        ? payload.error_message
        : summary.textContent;
    }
    line.append(status, summary);
    cell.appendChild(line);
  }

  async function runSavedProviderTest(row, options) {
    const settings = options || {};
    const failureMessage = settings.failureMessage || "测试请求失败";
    const cell = row.querySelector("[data-test-result]");
    if (settings.renderTesting !== false) {
      renderTestResult(cell, { status: "testing" });
    }
    try {
      const response = await fetch(`/providers/${row.dataset.providerId}/test-saved`, {
        method: "POST",
        headers: { Accept: "application/json" },
        credentials: "same-origin",
      });
      const payload = await response.json().catch(() => ({}));
      if (payload.status === "skipped") {
        const skippedPayload = {
          status: "skipped",
          model_id: "已跳过",
          error_message: payload.error_message || payload.error || "中转站已跳过",
        };
        renderTestResult(cell, skippedPayload);
        return { status: "skipped", payload: skippedPayload };
      }
      if (!response.ok) {
        const failurePayload = {
          status: "failed",
          model_id: "请求失败",
          error_message: payload.error || payload.error_message || failureMessage,
        };
        renderTestResult(cell, failurePayload);
        return { status: "failed", payload: failurePayload };
      }
      renderTestResult(cell, payload);
      return {
        status: payload.status === "success" ? "success" : "failed",
        payload,
      };
    } catch (error) {
      const failurePayload = {
        status: "failed",
        model_id: "请求失败",
        error_message: error.message || failureMessage,
      };
      renderTestResult(cell, failurePayload);
      return { status: "failed", payload: failurePayload };
    }
  }

  document.addEventListener("click", async (event) => {
    const target = event.target.closest("[data-copy-value], [data-copy-secret-url]");
    if (!target) {
      if (!event.target.closest("[data-model-picker]")) {
        document.querySelectorAll("[data-model-picker][open]").forEach((picker) => {
          picker.removeAttribute("open");
        });
      }
      return;
    }

    if (target.matches("a") || target.matches("[data-model-current-copy]")) {
      event.preventDefault();
    }

    try {
      target.classList.add("copying");
      const text = target.dataset.copySecretUrl
        ? await fetchSecret(target.dataset.copySecretUrl)
        : target.dataset.copyValue;
      await copyText(text);
      const modelOption = target.closest("[data-model-option]");
      if (modelOption) {
        const picker = modelOption.closest("[data-model-picker]");
        const current = picker.querySelector("[data-model-current]");
        current.textContent = text;
        current.dataset.copyValue = text;
        current.title = `点击复制 ${text}`;
        picker.removeAttribute("open");
        try {
          await saveTestPreferences(modelOption.dataset.providerId, { model_id: text });
          showToast("已复制并保存测试模型");
        } catch (saveError) {
          showToast(`模型已复制，但${saveError.message || "保存失败"}`, true);
        }
      } else {
        showToast("已复制到剪贴板");
      }
    } catch (error) {
      showToast(error.message || "复制失败", true);
    } finally {
      target.classList.remove("copying");
    }
  });

  document.addEventListener("keydown", (event) => {
    const current = event.target.closest("[data-model-current-copy]");
    if (!current || (event.key !== "Enter" && event.key !== " ")) {
      return;
    }
    event.preventDefault();
    current.click();
  });

  document.addEventListener("mouseover", (event) => {
    const target = event.target.closest("[data-response-tooltip]");
    if (target && !target.contains(event.relatedTarget)) {
      showResponseTooltip(target);
    }
  });

  document.addEventListener("mouseout", (event) => {
    const target = event.target.closest("[data-response-tooltip]");
    if (target && !target.contains(event.relatedTarget)) {
      hideResponseTooltip(target);
    }
  });

  document.addEventListener("focusin", (event) => {
    const target = event.target.closest("[data-response-tooltip]");
    if (target) {
      showResponseTooltip(target);
    }
  });

  document.addEventListener("focusout", (event) => {
    const target = event.target.closest("[data-response-tooltip]");
    if (target) {
      hideResponseTooltip(target);
    }
  });

  window.addEventListener("resize", positionResponseTooltip);
  window.addEventListener("scroll", positionResponseTooltip, true);

  document.addEventListener("click", (event) => {
    const button = event.target.closest("[data-proxy-url-parse]");
    if (!button) {
      return;
    }
    const input = document.querySelector("[data-proxy-url-input]");
    const form = document.querySelector("[data-proxy-form]");
    const message = document.querySelector("[data-proxy-url-message]");
    try {
      const parsed = new URL(input.value.trim());
      const scheme = parsed.protocol.replace(":", "").toLowerCase();
      if (!["http", "https", "socks5", "socks5h"].includes(scheme)) {
        throw new Error("仅支持 HTTP、HTTPS、SOCKS5 和 SOCKS5H");
      }
      if (!parsed.hostname || !parsed.port) {
        throw new Error("代理 URL 必须包含主机和端口");
      }
      form.querySelector("[data-proxy-scheme]").value = scheme;
      form.querySelector("[data-proxy-host]").value = parsed.hostname;
      form.querySelector("[data-proxy-port]").value = parsed.port;
      form.querySelector("[data-proxy-username]").value = decodeURIComponent(parsed.username);
      form.querySelector("[data-proxy-password]").value = decodeURIComponent(parsed.password);
      message.textContent = "代理 URL 已解析，请确认字段后保存。";
      message.classList.remove("error-text");
    } catch (error) {
      message.textContent = error.message || "代理 URL 无效";
      message.classList.add("error-text");
    }
  });

  const manualModelToggle = document.querySelector("[data-manual-model-toggle]");
  const manualModelForm = document.querySelector("[data-manual-model-form]");
  if (manualModelToggle && manualModelForm) {
    const manualModelInput = manualModelForm.querySelector("[data-manual-model-input]");
    const manualModelCancel = manualModelForm.querySelector("[data-manual-model-cancel]");

    function setManualModelFormOpen(open) {
      manualModelForm.hidden = !open;
      manualModelToggle.setAttribute("aria-expanded", open ? "true" : "false");
      if (open) {
        manualModelInput.focus();
      } else {
        manualModelInput.value = "";
        manualModelToggle.focus();
      }
    }

    manualModelToggle.addEventListener("click", () => {
      setManualModelFormOpen(manualModelForm.hidden);
    });
    manualModelCancel.addEventListener("click", () => setManualModelFormOpen(false));
  }

  const preferenceForm = document.querySelector("[data-test-preferences]");
  if (preferenceForm) {
    const providerId = preferenceForm.dataset.providerId;
    const status = preferenceForm.querySelector("[data-preference-save-status]");
    const modelInput = preferenceForm.querySelector('[name="model_id"]');
    const modelFillRows = Array.from(document.querySelectorAll("[data-model-fill]"));
    let modelSaveTimer = null;
    let preferenceSaveQueue = Promise.resolve();
    let preferenceSaveSequence = 0;

    function syncSelectedModel(value) {
      const selectedValue = value.trim();
      modelFillRows.forEach((row) => {
        const selected = row.dataset.modelValue === selectedValue;
        row.classList.toggle("is-selected", selected);
        row.setAttribute("aria-pressed", selected ? "true" : "false");
      });
    }

    function savePreference(values) {
      const sequence = ++preferenceSaveSequence;
      status.textContent = "保存中";
      status.classList.remove("error-text");
      const operation = preferenceSaveQueue
        .catch(() => undefined)
        .then(() => saveTestPreferences(providerId, values));
      preferenceSaveQueue = operation;
      operation.then(() => {
        if (sequence === preferenceSaveSequence) {
          status.textContent = "测试配置已保存";
        }
      }).catch((error) => {
        if (sequence === preferenceSaveSequence) {
          status.textContent = error.message || "测试配置保存失败";
          status.classList.add("error-text");
        }
      });
      return operation;
    }

    preferenceForm.querySelector('[name="client_profile"]').addEventListener("change", (event) => {
      savePreference({ client_profile: event.target.value });
    });
    preferenceForm.querySelector('[name="network_route"]').addEventListener("change", (event) => {
      savePreference({ network_route: event.target.value });
    });
    modelFillRows.forEach((row) => {
      row.addEventListener("click", () => {
        modelInput.value = row.dataset.modelValue;
        syncSelectedModel(modelInput.value);
        modelInput.dispatchEvent(new Event("change", { bubbles: true }));
      });
    });
    modelInput.addEventListener("input", () => {
      syncSelectedModel(modelInput.value);
      window.clearTimeout(modelSaveTimer);
      modelSaveTimer = window.setTimeout(() => savePreference({ model_id: modelInput.value }), 500);
    });
    modelInput.addEventListener("change", () => {
      syncSelectedModel(modelInput.value);
      window.clearTimeout(modelSaveTimer);
      savePreference({ model_id: modelInput.value });
    });
    modelInput.addEventListener("blur", () => {
      syncSelectedModel(modelInput.value);
      window.clearTimeout(modelSaveTimer);
      savePreference({ model_id: modelInput.value });
    });
    syncSelectedModel(modelInput.value);
  }

  const testAllButton = document.querySelector("[data-test-all]");
  if (testAllButton) {
    testAllButton.addEventListener("click", async () => {
      const rows = Array.from(document.querySelectorAll('[data-provider-row][data-provider-enabled="true"]'));
      if (!rows.length) {
        showToast("没有已启用的中转站", true);
        return;
      }
      testAllButton.disabled = true;
      const originalText = testAllButton.textContent;
      testAllButton.textContent = "测试中";
      rows.forEach((row) => renderTestResult(row.querySelector("[data-test-result]"), { status: "testing" }));
      let nextIndex = 0;
      let succeeded = 0;
      let failed = 0;
      let skipped = 0;

      async function worker() {
        while (nextIndex < rows.length) {
          const row = rows[nextIndex];
          nextIndex += 1;
          const result = await runSavedProviderTest(row, {
            failureMessage: "批量测试请求失败",
            renderTesting: false,
          });
          if (result.status === "success") {
            succeeded += 1;
          } else if (result.status === "skipped") {
            skipped += 1;
          } else {
            failed += 1;
          }
        }
      }

      const workers = Array.from({ length: Math.min(5, rows.length) }, () => worker());
      await Promise.all(workers);
      testAllButton.disabled = false;
      testAllButton.textContent = originalText;
      showToast(`测试完成：成功 ${succeeded}，失败 ${failed}，跳过 ${skipped}`, failed > 0);
    });
  }

  document.querySelectorAll("[data-test-single]").forEach((button) => {
    button.addEventListener("click", async () => {
      const row = button.closest("[data-provider-row]");
      if (!row) {
        return;
      }
      button.disabled = true;
      const originalText = button.textContent;
      button.textContent = "测试中";
      const result = await runSavedProviderTest(row, { failureMessage: "单站测试请求失败" });
      button.disabled = false;
      button.textContent = originalText;
      if (result.status === "success") {
        showToast("测试成功");
      } else if (result.status === "skipped") {
        showToast(result.payload.error_message || "中转站已跳过", true);
      } else {
        showToast(result.payload.error_message ? `测试失败：${result.payload.error_message}` : "测试失败", true);
      }
    });
  });

})();
