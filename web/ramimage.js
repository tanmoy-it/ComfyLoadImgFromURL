import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";

const NODE_CLASS = "LoadImageRam";
const UPLOAD_ROUTE = "/ramimage/upload";
const PREVIEW_HEIGHT = 150;

function formatBytes(bytes) {
  if (!Number.isFinite(bytes) || bytes < 0) return "";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function setupNode(node) {
  if (node._ramImageSetupDone) return;
  node._ramImageSetupDone = true;

  const tokenWidget = node.widgets?.find((w) => w.name === "token");

  // -- DOM: picker button, status, preview -------------------------------
  const button = document.createElement("button");
  button.type = "button";
  button.textContent = "Choose image…";
  button.style.cssText =
    "font-size:11px;padding:3px 8px;cursor:pointer;white-space:nowrap;";

  const status = document.createElement("span");
  status.textContent = "empty";
  status.style.cssText =
    "font-size:11px;line-height:1.2;opacity:0.75;min-width:0;overflow-wrap:anywhere;";

  const fileInput = document.createElement("input");
  fileInput.type = "file";
  fileInput.accept = "image/*";
  fileInput.style.display = "none";

  const row = document.createElement("div");
  row.style.cssText =
    "display:flex;gap:8px;align-items:center;flex-wrap:wrap;width:100%;";
  row.append(button, status, fileInput);

  const placeholder = document.createElement("span");
  placeholder.textContent = "No image loaded";
  placeholder.style.cssText = "font-size:11px;opacity:0.5;";

  const preview = document.createElement("img");
  preview.alt = "";
  preview.style.cssText =
    "max-width:100%;max-height:100%;object-fit:contain;display:none;";

  const previewBox = document.createElement("div");
  previewBox.style.cssText =
    `width:100%;height:${PREVIEW_HEIGHT}px;box-sizing:border-box;` +
    "display:flex;align-items:center;justify-content:center;overflow:hidden;" +
    "border:1px solid var(--border-color, #555);border-radius:4px;";
  previewBox.append(placeholder, preview);

  const root = document.createElement("div");
  root.style.cssText =
    "display:flex;flex-direction:column;gap:6px;padding:2px;width:100%;box-sizing:border-box;";
  root.append(row, previewBox);

  const widget = node.addDOMWidget("ram_image", "ram_image", root, {
    hideOnZoom: true,
    getMinHeight: () => 190,
    getHeight: () => 190,
    serialize: false,
  });
  if (widget) {
    // widget.serialize controls workflow persistence; options.serialize only
    // controls the API prompt. This widget is UI-only, so exclude it from both.
    widget.serialize = false;
    if (widget.options) widget.options.serialize = false;
  }

  let previewUrl = null;
  let uploadedToken = null;
  let uploading = false;
  let disposed = false;

  const setStatus = (text, title) => {
    status.textContent = text;
    status.title = title || text;
    node.setDirtyCanvas?.(true, false);
  };

  const clearPreview = () => {
    if (previewUrl) {
      URL.revokeObjectURL(previewUrl);
      previewUrl = null;
    }
    preview.removeAttribute("src");
    preview.style.display = "none";
    placeholder.style.display = "";
  };

  const showPreview = (blob) => {
    if (previewUrl) URL.revokeObjectURL(previewUrl);
    previewUrl = URL.createObjectURL(blob);
    preview.src = previewUrl;
    preview.style.display = "block";
    placeholder.style.display = "none";
  };

  const setToken = (token) => {
    uploadedToken = token;
    if (!tokenWidget) return;
    tokenWidget.value = token;
    node.setDirtyCanvas?.(true, true);
  };

  const upload = async (blob) => {
    if (uploading) return;
    uploading = true;
    button.disabled = true;
    setStatus("uploading…");
    try {
      const response = await api.fetchApi(UPLOAD_ROUTE, {
        method: "POST",
        headers: { "Content-Type": "application/octet-stream" },
        body: blob,
      });

      let payload = null;
      try {
        payload = await response.json();
      } catch {
        // fall through to the status-based error below
      }

      if (!response.ok) {
        throw new Error(
          payload?.error || `Upload failed (HTTP ${response.status}).`
        );
      }
      if (!payload?.token) {
        throw new Error("Upload failed (no token returned).");
      }

      if (disposed) return;
      setToken(payload.token);
      showPreview(blob);
      setStatus(`ready (${formatBytes(payload.bytes ?? blob.size)})`);
    } catch (error) {
      if (!disposed) {
        setStatus("upload failed", String(error?.message || error));
        console.warn("RAM image upload failed:", String(error?.message || error));
      }
    } finally {
      uploading = false;
      button.disabled = false;
      fileInput.value = "";
    }
  };

  const onPaste = (event) => {
    const selected = app.canvas?.selected_nodes;
    if (!selected) return;

    const nodes =
      selected instanceof Set ? Array.from(selected) : Object.values(selected);
    if (nodes.length !== 1 || nodes[0] !== node) return;

    const active = document.activeElement;
    if (
      active &&
      (active.tagName === "INPUT" ||
        active.tagName === "TEXTAREA" ||
        active.isContentEditable)
    ) {
      return;
    }

    const items = event.clipboardData?.items;
    if (!items) return;

    for (const item of items) {
      if (item.kind !== "file" || !item.type.startsWith("image/")) continue;
      const blob = item.getAsFile();
      if (!blob) continue;
      event.preventDefault();
      event.stopPropagation();
      void upload(blob);
      return;
    }
  };

  button.addEventListener("click", () => fileInput.click());
  fileInput.addEventListener("change", () => {
    const file = fileInput.files?.[0];
    if (file) void upload(file);
  });
  document.addEventListener("paste", onPaste);

  node._ramImageCleanup = () => {
    disposed = true;
    document.removeEventListener("paste", onPaste);
    clearPreview();
  };

  node._ramImageOnConfigure = () => {
    // Widget values are applied before onConfigure runs on current frontends,
    // but the microtask makes this robust on older ones too.
    queueMicrotask(() => {
      if (tokenWidget?.value !== uploadedToken) clearPreview();
      if (tokenWidget?.value) setStatus("unknown (from workflow)");
      else setStatus("empty");
    });
  };

  if (tokenWidget) {
    const previousCallback = tokenWidget.callback;
    tokenWidget.callback = function (...args) {
      if (tokenWidget.value !== uploadedToken) {
        uploadedToken = null;
        clearPreview();
      }
      if (tokenWidget.value) setStatus("token set (not verified)");
      else setStatus("empty");
      return previousCallback?.apply(this, args);
    };
  }

  const size = node.computeSize?.();
  if (size) {
    node.setSize([Math.max(size[0], 320), Math.max(size[1], 280)]);
  }
  node.setDirtyCanvas?.(true, true);
}

app.registerExtension({
  name: "RamImage.LoadImageRam",
  async beforeRegisterNodeDef(nodeType, nodeData) {
    if (nodeData.name !== NODE_CLASS) return;

    const onNodeCreated = nodeType.prototype.onNodeCreated;
    nodeType.prototype.onNodeCreated = function () {
      const result = onNodeCreated?.apply(this, arguments);
      setupNode(this);
      return result;
    };

    const onRemoved = nodeType.prototype.onRemoved;
    nodeType.prototype.onRemoved = function () {
      try {
        this._ramImageCleanup?.();
      } finally {
        this._ramImageCleanup = null;
      }
      return onRemoved?.apply(this, arguments);
    };

    const onConfigure = nodeType.prototype.onConfigure;
    nodeType.prototype.onConfigure = function () {
      const result = onConfigure?.apply(this, arguments);
      this._ramImageOnConfigure?.();
      return result;
    };
  },
});
