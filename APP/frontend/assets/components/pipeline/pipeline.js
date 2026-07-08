/**
 * Lightweight Pipeline canvas renderer.
 *
 * Usage:
 *   new Pipeline({ container, nodes, connections, stages, onNodeClick }).render()
 */
export class Pipeline {
  constructor(opts = {}) {
    this.container =
      typeof opts.container === "string"
        ? document.querySelector(opts.container)
        : opts.container;
    this.nodes = opts.nodes || [];
    this.connections = opts.connections || [];
    this.stages = opts.stages || [];
    this.onNodeClick = opts.onNodeClick || null;
  }

  render() {
    if (!this.container) return;
    this.container.innerHTML = this._renderHtml();
    this.container.querySelectorAll("[data-node-id]").forEach((el) => {
      el.addEventListener("click", () => {
        const node = this.nodes.find((n) => n.id === el.dataset.nodeId);
        if (node && this.onNodeClick) this.onNodeClick(node);
      });
    });
  }

  refresh() {
    this.render();
  }

  _renderHtml() {
    const stages = this.stages.length
      ? this.stages
      : [...new Set(this.nodes.map((n) => n.stage || 1))].map((num) => ({
          num,
          label: `Stage ${num}`,
        }));

    const stageHtml = stages
      .map((stage) => {
        const stageNodes = this.nodes.filter((n) => (n.stage || 1) === stage.num);
        return `<section class="pl-stage">
          <div class="pl-stage-label">${this._esc(stage.label || `Stage ${stage.num}`)}</div>
          ${stageNodes.map((node) => this._renderNode(node)).join("")}
        </section>`;
      })
      .join("");

    const connections = this.connections
      .map(
        (c) =>
          `<span class="pl-connection ${c.flowing ? "pl-connection-flowing" : ""}" title="${this._esc(
            `${c.from || ""} -> ${c.to || ""}`,
          )}"></span>`,
      )
      .join("");

    return `<div class="pl-stage-row">${stageHtml}</div>
      ${connections ? `<div class="pl-connection-row">${connections}</div>` : ""}`;
  }

  _renderNode(node) {
    if (node.type === "mission-control") {
      return `<article class="pl-node" data-node-id="${this._esc(node.id)}">${node.html || ""}</article>`;
    }

    const statusClass =
      node.accent === "danger"
        ? "pl-node-danger"
        : node.status === "off"
          ? "pl-node-off"
          : node.status === "alert"
            ? "pl-node-alert"
            : "";
    const chips = (node.chips || [])
      .map((chip) => `<span class="pl-chip">${this._esc(chip)}</span>`)
      .join("");
    const pass = node.metrics && typeof node.metrics.pass === "number"
      ? Math.max(0, Math.min(1, node.metrics.pass))
      : null;

    return `<article class="pl-node ${statusClass}" data-node-id="${this._esc(node.id)}">
      <div class="pl-node-header">
        <span class="pl-node-icon">${node.icon || ""}</span>
        <div>
          <div class="pl-node-title">${this._esc(node.title || "")}</div>
          <div class="pl-node-subtitle">${this._esc(node.subtitle || "")}</div>
        </div>
      </div>
      <div class="pl-node-body">
        ${chips ? `<div class="pl-chip-row">${chips}</div>` : ""}
        ${pass == null ? "" : `<div class="pl-metric"><div class="pl-metric-bar" style="width:${Math.round(pass * 100)}%"></div></div>`}
      </div>
    </article>`;
  }

  _esc(value) {
    return String(value ?? "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }
}
