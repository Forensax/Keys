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
        showToast("已复制模型名称");
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

})();
