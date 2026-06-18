(function () {
  const toast = document.querySelector("[data-copy-toast]");

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

})();
