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

  document.querySelectorAll("[data-schedule-form]").forEach((form) => {
    const target = form.querySelector("[data-schedule-target]");
    const kind = form.querySelector("[data-schedule-kind]");
    const group = form.querySelector("[data-schedule-group]");
    const providers = form.querySelector("[data-schedule-providers]");
    const interval = form.querySelector("[data-schedule-interval]");
    const daily = form.querySelector("[data-schedule-daily]");
    const sync = () => {
      if (target) {
        group.hidden = target.value !== "group";
        providers.hidden = target.value !== "providers";
      }
      if (kind) {
        interval.hidden = kind.value !== "interval";
        daily.hidden = kind.value !== "daily";
      }
    };
    target?.addEventListener("change", sync);
    kind?.addEventListener("change", sync);
    sync();
  });

  document.querySelectorAll("[data-auto-submit-filter]").forEach((form) => {
    let submitting = false;
    form.addEventListener("change", (event) => {
      if (submitting || !event.target.matches("select")) {
        return;
      }
      submitting = true;
      form.classList.add("is-submitting");
      window.setTimeout(() => {
        if (typeof form.requestSubmit === "function") {
          form.requestSubmit();
        } else {
          form.submit();
        }
      }, 0);
    });
  });

  const statisticsDataNode = document.querySelector("#statistics-data");
  if (statisticsDataNode) {
    const NS = "http://www.w3.org/2000/svg";
    const palette = ["#1769aa", "#b57a22", "#557a3d", "#9a5d86", "#54657a"];
    const el = (name, attrs = {}, text = "") => {
      const node = document.createElementNS(NS, name);
      Object.entries(attrs).forEach(([key, value]) => node.setAttribute(key, String(value)));
      if (text !== "") node.textContent = text;
      return node;
    };
    const addTitle = (node, text) => {
      node.setAttribute("tabindex", "0");
      node.append(el("title", {}, text));
    };
    const svgFor = (container, height = 280) => {
      const svg = el("svg", { viewBox: `0 0 960 ${height}`, role: "img", "aria-label": container.getAttribute("aria-label") || "统计图表" });
      container.replaceChildren(svg);
      return svg;
    };
    const shorten = (value, length = 16) => value.length > length ? `${value.slice(0, length - 1)}…` : value;
    let stats = null;
    try { stats = JSON.parse(statisticsDataNode.textContent); } catch (_) { stats = null; }

    const renderTrend = (container, rows) => {
      if (!rows.length) return;
      const svg = svgFor(container);
      const box = { left: 48, top: 16, right: 18, bottom: 42 };
      const width = 960 - box.left - box.right;
      const height = 280 - box.top - box.bottom;
      const max = Math.max(1, ...rows.map((row) => row.success + row.failed));
      [0, 0.5, 1].forEach((ratio) => {
        const y = box.top + height * ratio;
        svg.append(el("line", { x1: box.left, y1: y, x2: 960 - box.right, y2: y, class: "chart-grid-line" }));
        svg.append(el("text", { x: box.left - 8, y: y + 4, "text-anchor": "end", class: "chart-axis-label" }, String(Math.round(max * (1 - ratio)))));
      });
      const step = width / rows.length;
      const barWidth = Math.max(3, Math.min(30, step * 0.64));
      rows.forEach((row, index) => {
        const x = box.left + index * step + (step - barWidth) / 2;
        const successHeight = row.success / max * height;
        const failedHeight = row.failed / max * height;
        const success = el("rect", { x, y: box.top + height - successHeight, width: barWidth, height: successHeight, fill: "#2476b8" });
        addTitle(success, `${row.label}：成功 ${row.success} 次`);
        svg.append(success);
        const failed = el("rect", { x, y: box.top + height - successHeight - failedHeight, width: barWidth, height: failedHeight, fill: "#d1903d" });
        addTitle(failed, `${row.label}：失败 ${row.failed} 次`);
        svg.append(failed);
        const every = Math.max(1, Math.ceil(rows.length / 8));
        if (index % every === 0 || index === rows.length - 1) svg.append(el("text", { x: x + barWidth / 2, y: 263, "text-anchor": "middle", class: "chart-axis-label" }, row.label.slice(5)));
      });
    };

    const renderLatency = (container, series) => {
      const points = series.flatMap((item) => item.points.filter((point) => point.value !== null));
      if (!points.length) return;
      const svg = svgFor(container);
      const labels = series[0]?.points.map((point) => point.label) || [];
      const box = { left: 56, top: 16, right: 20, bottom: 42 };
      const width = 960 - box.left - box.right;
      const height = 280 - box.top - box.bottom;
      const max = Math.max(100, ...points.map((point) => point.value));
      [0, 0.5, 1].forEach((ratio) => {
        const y = box.top + height * ratio;
        svg.append(el("line", { x1: box.left, y1: y, x2: 960 - box.right, y2: y, class: "chart-grid-line" }));
        svg.append(el("text", { x: box.left - 8, y: y + 4, "text-anchor": "end", class: "chart-axis-label" }, `${Math.round(max * (1 - ratio))}`));
      });
      series.forEach((item, seriesIndex) => {
        const color = palette[seriesIndex % palette.length];
        const segments = [];
        let current = [];
        item.points.forEach((point, index) => {
          if (point.value === null) {
            if (current.length) segments.push(current);
            current = [];
            return;
          }
          const x = box.left + (labels.length <= 1 ? width / 2 : index / (labels.length - 1) * width);
          const y = box.top + height - point.value / max * height;
          current.push([x, y, point]);
        });
        if (current.length) segments.push(current);
        segments.forEach((segment) => svg.append(el("polyline", { points: segment.map(([x, y]) => `${x},${y}`).join(" "), fill: "none", stroke: color, "stroke-width": 2 })));
        segments.flat().forEach(([x, y, point]) => {
          const circle = el("circle", { cx: x, cy: y, r: 4, fill: "white", stroke: color, "stroke-width": 2 });
          addTitle(circle, `${item.name} · ${point.label}：${point.value} ms`);
          svg.append(circle);
        });
      });
      const every = Math.max(1, Math.ceil(labels.length / 8));
      labels.forEach((label, index) => {
        if (index % every === 0 || index === labels.length - 1) {
          const x = box.left + (labels.length <= 1 ? width / 2 : index / (labels.length - 1) * width);
          svg.append(el("text", { x, y: 263, "text-anchor": "middle", class: "chart-axis-label" }, label.slice(5)));
        }
      });
      const legend = document.querySelector('[data-stat-legend="latency"]');
      if (legend) {
        legend.replaceChildren(...series.map((item, index) => {
          const span = document.createElement("span");
          const swatch = document.createElement("i");
          swatch.className = "legend-swatch";
          swatch.style.background = palette[index % palette.length];
          span.append(swatch, document.createTextNode(item.name));
          return span;
        }));
      }
    };

    const renderBars = (container, rows, color) => {
      if (!rows.length) return;
      const height = Math.max(180, rows.length * 34 + 24);
      const svg = svgFor(container, height);
      const left = 210;
      const width = 720;
      const max = Math.max(1, ...rows.map((row) => row.value));
      rows.forEach((row, index) => {
        const y = 14 + index * 34;
        svg.append(el("text", { x: left - 10, y: y + 16, "text-anchor": "end", class: "chart-axis-label" }, shorten(row.label, 28)));
        const bar = el("rect", { x: left, y, width: row.value / max * width, height: 20, rx: 2, fill: color });
        addTitle(bar, `${row.label}：${row.value} 次`);
        svg.append(bar);
        svg.append(el("text", { x: left + row.value / max * width + 8, y: y + 15, class: "chart-value-label" }, String(row.value)));
      });
    };

    if (stats) {
      const trend = document.querySelector('[data-stat-chart="trend"]');
      const latency = document.querySelector('[data-stat-chart="latency"]');
      const failures = document.querySelector('[data-stat-chart="failures"]');
      const sources = document.querySelector('[data-stat-chart="sources"]');
      if (trend) renderTrend(trend, stats.trend || []);
      if (latency) renderLatency(latency, stats.latencySeries || []);
      if (failures) renderBars(failures, (stats.failures || []).map((row) => ({ label: row.reason, value: row.count })), "#b57a22");
      if (sources) renderBars(sources, [
        { label: "手动测试", value: stats.sources?.manual || 0 },
        { label: "定时任务", value: stats.sources?.scheduled || 0 },
      ], "#1769aa");
    }
  }

})();
