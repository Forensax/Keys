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
    cell.replaceChildren();
    const line = document.createElement("div");
    line.className = "test-line";
    const status = document.createElement("span");
    status.className = `status ${payload.status}`;
    status.textContent = payload.status === "success" ? "成功" : payload.status === "testing" ? "测试中" : "失败";
    const summary = document.createElement("span");
    summary.className = "test-summary muted small";
    if (payload.status === "testing") {
      summary.textContent = "正在连接";
    } else {
      const latency = payload.latency_ms === null || payload.latency_ms === undefined
        ? ""
        : ` · ${payload.latency_ms} ms`;
      summary.textContent = `${payload.model_id || "未知模型"}${latency}`;
      summary.title = payload.status === "failed" && payload.error_message
        ? payload.error_message
        : summary.textContent;
    }
    line.append(status, summary);
    cell.appendChild(line);
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
        current.title = text;
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

  const preferenceForm = document.querySelector("[data-test-preferences]");
  if (preferenceForm) {
    const providerId = preferenceForm.dataset.providerId;
    const status = preferenceForm.querySelector("[data-preference-save-status]");
    const modelInput = preferenceForm.querySelector('[name="model_id"]');
    let modelSaveTimer = null;

    async function savePreference(values) {
      status.textContent = "保存中";
      status.classList.remove("error-text");
      try {
        await saveTestPreferences(providerId, values);
        status.textContent = "测试配置已保存";
      } catch (error) {
        status.textContent = error.message || "测试配置保存失败";
        status.classList.add("error-text");
      }
    }

    preferenceForm.querySelector('[name="client_profile"]').addEventListener("change", (event) => {
      savePreference({ client_profile: event.target.value });
    });
    preferenceForm.querySelector('[name="network_route"]').addEventListener("change", (event) => {
      savePreference({ network_route: event.target.value });
    });
    modelInput.addEventListener("input", () => {
      window.clearTimeout(modelSaveTimer);
      modelSaveTimer = window.setTimeout(() => savePreference({ model_id: modelInput.value }), 500);
    });
    modelInput.addEventListener("change", () => {
      window.clearTimeout(modelSaveTimer);
      savePreference({ model_id: modelInput.value });
    });
    modelInput.addEventListener("blur", () => {
      window.clearTimeout(modelSaveTimer);
      savePreference({ model_id: modelInput.value });
    });
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
          const cell = row.querySelector("[data-test-result]");
          try {
            const response = await fetch(`/providers/${row.dataset.providerId}/test-saved`, {
              method: "POST",
              headers: { Accept: "application/json" },
              credentials: "same-origin",
            });
            const payload = await response.json().catch(() => ({}));
            if (payload.status === "skipped") {
              skipped += 1;
              renderTestResult(cell, {
                status: "failed",
                model_id: "已跳过",
                error_message: payload.error_message || "中转站已跳过",
              });
            } else if (!response.ok) {
              failed += 1;
              renderTestResult(cell, {
                status: "failed",
                model_id: "请求失败",
                error_message: payload.error || payload.error_message || "批量测试请求失败",
              });
            } else {
              renderTestResult(cell, payload);
              if (payload.status === "success") {
                succeeded += 1;
              } else {
                failed += 1;
              }
            }
          } catch (error) {
            failed += 1;
            renderTestResult(cell, {
              status: "failed",
              model_id: "请求失败",
              error_message: error.message || "批量测试请求失败",
            });
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

})();
