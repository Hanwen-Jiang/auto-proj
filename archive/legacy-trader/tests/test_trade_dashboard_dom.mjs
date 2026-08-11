import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import vm from "node:vm";

class ClassList {
  constructor(owner) {
    this.owner = owner;
    this.values = new Set();
  }

  add(...names) {
    for (const name of names) if (name) this.values.add(name);
  }

  remove(...names) {
    for (const name of names) this.values.delete(name);
  }

  toggle(name, force) {
    const enabled = force === undefined ? !this.values.has(name) : Boolean(force);
    if (enabled) this.values.add(name);
    else this.values.delete(name);
    return enabled;
  }

  [Symbol.iterator]() {
    return this.values[Symbol.iterator]();
  }

  toString() {
    return [...this.values].join(" ");
  }
}

class MiniNode {
  constructor(tagName = "", nodeType = 1) {
    this.tagName = tagName.toUpperCase();
    this.nodeType = nodeType;
    this.childNodes = [];
    this.parentNode = null;
    this.dataset = {};
    this.attributes = new Map();
    this.classList = new ClassList(this);
    this.listeners = new Map();
    this.hidden = false;
    this.disabled = false;
    this.value = "";
    this.id = "";
    this._text = "";
  }

  set className(value) {
    this.classList.values = new Set(String(value).split(/\s+/).filter(Boolean));
  }

  get className() {
    return this.classList.toString();
  }

  set textContent(value) {
    this.childNodes = [];
    this._text = String(value ?? "");
  }

  get textContent() {
    return this._text + this.childNodes.map((child) => child.textContent).join("");
  }

  append(...children) {
    for (let child of children) {
      if (typeof child === "string") child = new TextNode(child);
      if (child.nodeType === 11) {
        this.append(...child.childNodes);
        child.childNodes = [];
        continue;
      }
      child.parentNode = this;
      this.childNodes.push(child);
    }
  }

  replaceChildren(...children) {
    this.childNodes = [];
    this._text = "";
    this.append(...children);
  }

  setAttribute(name, value) {
    this.attributes.set(name, String(value));
  }

  getAttribute(name) {
    return this.attributes.get(name) ?? null;
  }

  addEventListener(name, callback) {
    this.listeners.set(name, callback);
  }

  dispatch(name, target = this) {
    return this.listeners.get(name)?.({ target, preventDefault() {} });
  }

  matches(selector) {
    if (selector.startsWith("#")) return this.id === selector.slice(1);
    if (selector.startsWith(".")) return this.classList.values.has(selector.slice(1));
    const match = /^([a-z]+)(?:\[data-([a-z-]+)\])?$/i.exec(selector);
    if (!match || this.tagName !== match[1].toUpperCase()) return false;
    if (!match[2]) return true;
    const key = match[2].replace(/-([a-z])/g, (_all, letter) => letter.toUpperCase());
    return Object.hasOwn(this.dataset, key);
  }

  closest(selector) {
    for (let node = this; node; node = node.parentNode) {
      if (node.matches?.(selector)) return node;
    }
    return null;
  }

  querySelectorAll(selector) {
    const result = [];
    const visit = (node) => {
      for (const child of node.childNodes) {
        if (child.matches?.(selector)) result.push(child);
        visit(child);
      }
    };
    visit(this);
    return result;
  }

  querySelector(selector) {
    return this.querySelectorAll(selector)[0] ?? null;
  }
}

class TextNode extends MiniNode {
  constructor(text) {
    super("", 3);
    this._text = String(text);
  }
}

class MiniDocument {
  constructor() {
    this.baseURI = "https://fixture.invalid/a6-trader/";
    this.visibilityState = "visible";
    this.root = new MiniNode("body");
    this.listeners = new Map();
  }

  createElement(tagName) {
    return new MiniNode(tagName);
  }

  createTextNode(text) {
    return new TextNode(text);
  }

  createDocumentFragment() {
    return new MiniNode("", 11);
  }

  querySelector(selector) {
    return this.root.querySelector(selector);
  }

  getElementById(id) {
    return this.querySelector(`#${id}`);
  }

  addEventListener(name, callback) {
    this.listeners.set(name, callback);
  }
}

function seedDocument() {
  const document = new MiniDocument();
  const ids = [
    "add-account",
    "alert",
    "alert-text",
    "account-cancel",
    "account-dialog",
    "account-form",
    "account-password",
    "account-status",
    "account-submit",
    "account-username",
    "collector-dot",
    "collector-text",
    "data-time",
    "economic-channel-rows",
    "economic-freshness",
    "economic-gross-floor",
    "economic-mode",
    "economic-model",
    "economic-profit-form",
    "economic-profit-percent",
    "economic-profit-save",
    "economic-profit-status",
    "economic-profit-threshold-heading",
    "economic-source-rows",
    "economic-summary",
    "freshness-filter",
    "merchant-count",
    "metrics",
    "provider-freshness",
    "provider-mode",
    "provider-slot-rows",
    "provider-summary",
    "refresh",
    "duty-filter",
    "merchant-rows",
    "search",
    "status-filter",
    "traffic-band-rows",
    "traffic-freshness",
    "traffic-failure-rows",
    "traffic-model",
    "traffic-position-count",
    "traffic-position-filter",
    "traffic-position-rows",
    "traffic-summary",
    "traffic-windows",
    "visible-count",
    "windows"
  ];
  for (const id of ids) {
    const tagName = id.endsWith("filter") || ["economic-model", "traffic-model"].includes(id)
      ? "select"
      : id === "account-dialog"
        ? "dialog"
      : ["account-form", "economic-profit-form"].includes(id)
          ? "form"
          : ["account-password", "account-username", "economic-profit-percent", "search"].includes(id)
            ? "input"
            : ["add-account", "account-cancel", "account-submit", "economic-profit-save", "refresh"].includes(id)
              ? "button"
              : "div";
    const element = new MiniNode(tagName);
    element.id = id;
    if (["economic-model", "traffic-model"].includes(id)) {
      element.value = "gpt-5.6-sol";
    }
    if (id === "traffic-position-filter") element.value = "listed";
    document.root.append(element);
  }
  return document;
}

function findByDataset(root, key) {
  const result = [];
  const visit = (node) => {
    if (Object.hasOwn(node.dataset ?? {}, key)) result.push(node);
    for (const child of node.childNodes ?? []) visit(child);
  };
  visit(root);
  return result;
}

async function settle() {
  await new Promise((resolve) => setTimeout(resolve, 0));
  await new Promise((resolve) => setTimeout(resolve, 0));
}

test("credentials load only after reveal and are removed when masked", async () => {
  const html = readFileSync(
    new URL("../src/a6api_auto/data/trade_dashboard.html", import.meta.url),
    "utf8"
  );
  const script = html.match(/<script>([\s\S]*?)<\/script>/)?.[1];
  assert.ok(script, "dashboard inline script is present");
  assert.match(html, /<select class="traffic-model-select" id="economic-model">/);
  assert.match(html, /<th scope="col">最低净结算 K<\/th>/);
  assert.match(html, /<th scope="col">最低 Listing K<\/th>/);
  assert.match(html, /<th scope="col">实际流量<\/th>/);

  const document = seedDocument();
  const fetchCalls = [];
  const failingPaths = new Set();
  const trafficModels = [];
  const merchant = {
    merchant_id: "1",
    display_name: "merchant-0001",
    roles: ["seller"],
    status: "active",
    session_status: "healthy",
    proxy_status: "healthy",
    freshness: "fresh",
    retirement_state: "earning",
    retirement_phase: "pre_retirement",
    retirement_archive_mode: true,
    retirement_updated_at: "2026-07-27T10:00:00Z",
    pin_rates: [{ exchange_rate: 6.7551 }]
  };
  const fetch = async (input) => {
    const url = new URL(String(input));
    fetchCalls.push(url.pathname);
    if (failingPaths.has(url.pathname)) throw new Error("fixture request failed");
    if (url.pathname.endsWith("/api/market-traffic")) {
      trafficModels.push(url.searchParams.get("model"));
    }
    let payload;
    if (url.pathname.endsWith("/api/overview")) {
      payload = { merchant_count: 1, stocks: {}, flows: {}, freshness: {} };
    } else if (url.pathname.endsWith("/api/merchants")) {
      payload = { items: [merchant] };
    } else if (url.pathname.endsWith("/api/market-traffic")) {
      payload = {
        freshness: {},
        totals: {},
        bands: [
          {
            band_id: "k_cny_00050_00060",
            minimum_k_cny: 0.05,
            maximum_k_cny: 0.06,
            current_channel_count: 2,
            event_lower_bound: 2,
            managed_listing_count: 99,
            series: []
          }
        ],
        managed_positions: [
          {
            rank: 1,
            merchant_id: "1",
            channel_id: "1148",
            listing_id: "4100",
            channel_name: "OmniAPI SOL Route",
            operational_state: "active",
            blocked_reason: "",
            merchant_status: "supplier_submitted",
            merchant_roles: ["seller", "observer"],
            binding_present: true,
            binding_control_state: "verified",
            listing_present: true,
            listing_channel_id: "1148",
            listing_platform_model: "gpt-5.6-sol",
            listing_status: 2,
            listing_availability: 1,
            listing_visibility: 1,
            current_k_cny: 0.0594,
            market_available: true,
            health_state: "healthy",
            route_status: "default",
            recent_success_rate: 9875,
            recent_p50_ms: 812.5,
            p50_ttft_ms: 155.25,
            smart_routing_labels: ["fast", "stable"],
            seller_call_count: 14,
            seller_income_micros: 42000,
            seller_income_per_call_micros: 3000,
            delta: 0.5,
            panel_multiplier: 0.03511,
            target_final_multiplier: 0.052665,
            delta_sample_status: "matched_count"
          },
          {
            rank: 2,
            merchant_id: "2",
            channel_id: "1149",
            listing_id: "4101",
            channel_name: "Paused Managed Listing",
            operational_state: "blocked",
            blocked_reason: "listing_status_inactive",
            merchant_status: "supplier_submitted",
            merchant_roles: ["seller"],
            binding_present: true,
            binding_control_state: "verified",
            listing_present: true,
            listing_channel_id: "1149",
            listing_platform_model: "gpt-5.6-sol",
            listing_status: 0,
            listing_availability: 0,
            listing_visibility: 0,
            current_k_cny: 0.0594,
            market_available: false,
            health_state: "unknown",
            seller_call_count: 0,
            seller_income_micros: 0,
            delta_sample_status: "no_sample"
          }
        ],
        channel_failures: [
          {
            channel_id: "790",
            listing_id: "1790",
            market_rank: 7,
            health_state: "healthy",
            available: true,
            recent_success_rate: 7300,
            recent_failure_count: 4,
            recent_client_failure_count: 4,
            recent_provider_failure_count: 0,
            recent_platform_failure_count: 0,
            recent_unknown_failure_count: 0,
            last_failure_code: "invalid_prompt",
            last_failure_responsibility: "client",
            last_failure_at: "2026-07-25T13:59:57Z",
            top_failure_codes: [
              {
                code: "invalid_prompt",
                responsibility: "client",
                count: 4,
                last_failure_at: "2026-07-25T13:59:57Z"
              }
            ]
          }
        ]
      };
    } else if (url.pathname.endsWith("/internal/provider-pools/gpt-5.6-sol")) {
      payload = {
        model: "gpt-5.6-sol",
        routing: { generation: 42, updated_at: "2026-07-29T02:00:00Z" },
        cost: { generation: 19 },
        slots: [
          {
            slot_no: 1,
            slot_name: "slot-1",
            state: "READY",
            channel_id: 1008,
            account_id: 45895,
            token_id: 4,
            priority: 2,
            desired_cost_multiplier: "0.100000",
            observed_cost_multiplier: "0.100000",
            funding_eligible: true,
            observed_schedulable: true,
            pin_confirmed: true,
            probe_confirmed: true,
            last_success_at: "2026-07-29T01:59:58Z"
          }
        ],
        publication: {
          desired_state: "published",
          observed_state: "published",
          converged: true,
          updated_at: "2026-07-29T02:00:00Z",
          last_real_probe_at: "2026-07-29T01:59:59Z"
        },
        preview: {
          kind: "failed",
          routing: { generation: 43, state: "failed", updated_at: "2026-07-29T02:01:00Z" },
          cost: { generation: 20, state: "failed" },
          slots: [
            {
              slot_no: 1,
              slot_name: "candidate-1",
              state: "READY",
              channel_id: 1012,
              desired_cost_multiplier: "0.110000"
            }
          ],
          summary: { ready_slot_count: 1, vacant_slot_count: 0 }
        },
        recent_failures: [{ failure_code: "fixed_merchant_unavailable" }],
        recent_generation_failure_count: 1,
        summary: {
          ready_slot_count: 1,
          vacant_slot_count: 0,
          schedulable_slot_count: 1
        }
      };
    } else if (url.pathname.endsWith("/internal/provider-pools/gpt-preview")) {
      payload = {
        model: "gpt-preview",
        routing: null,
        cost: null,
        slots: [],
        publication: null,
        summary: { ready_slot_count: 0, vacant_slot_count: 0 },
        preview: {
          kind: "planned",
          routing: {
            generation: 77,
            state: "planned",
            updated_at: "2026-07-29T03:00:00Z"
          },
          cost: { generation: 33, state: "planned" },
          slots: [
            {
              slot_no: 1,
              slot_name: "a6-provider-1",
              state: "READY",
              channel_id: 1012,
              desired_cost_multiplier: "0.110000"
            },
            {
              slot_no: 2,
              slot_name: "a6-provider-2",
              state: "VACANT",
              channel_id: null,
              desired_cost_multiplier: null
            }
          ],
          summary: { ready_slot_count: 1, vacant_slot_count: 1 }
        },
        recent_failures: []
      };
    } else if (url.pathname.endsWith("/internal/provider-inventory/gpt-5.6-sol")) {
      payload = {
        model: "gpt-5.6-sol",
        items: [
          {
            account_id: 45895,
            token_id: 4,
            inventory_state: "WARM_STANDBY",
            schedulable: false
          }
        ],
        summary: { schedulable_count: 0 }
      };
    } else if (url.pathname.endsWith("/internal/provider-inventory/gpt-preview")) {
      payload = {
        model: "gpt-preview",
        items: [],
        summary: { schedulable_count: 0 }
      };
    } else if (url.pathname.endsWith("/internal/economic-priority1/gpt-5.6-sol")) {
      payload = {
        model: "gpt-5.6-sol",
        profit_setting: {
          profit_percent: "60",
          minimum_profit_on_cost_bps: 6000,
          net_multiplier: "1.6",
          gross_listing_multiplier: "1.92",
          updated_at: "2026-08-04T05:00:00Z",
          updated_by: "dashboard"
        },
        generation: {
          generation: 7,
          state: "active",
          plan_only: true,
          minimum_profit_on_cost_bps: 6000,
          evaluated_at: "2026-08-03T08:00:00Z"
        },
        projection: {
          generation: 7,
          state: "plan_only",
          updated_at: "2026-08-03T08:00:01Z"
        },
        projection_matches_generation: true,
        sources: [
          {
            source_id: 11,
            source_key: "owned-weekly-11",
            source_kind: "account",
            account_id: 501,
            health_state: "HEALTHY",
            capacity_state: "AVAILABLE",
            contract: {
              cost_mode: "weekly_amortized",
              cycle_cost_cny: "120",
              state: "active"
            },
            quota: {
              quota_epoch: "week-31",
              used_a: "18",
              remaining_a: "82",
              total_a: "100",
              reset_at: "2026-08-08T00:00:00Z",
              estimate_state: "PROVISIONAL",
              evidence_point_count: 1,
              raw_utilization_percent: "18"
            },
            source_k_cny: "1.2",
            minimum_channel_k_cny: "1.92",
            minimum_listing_k_cny: "2.304",
            evaluated_channel_count: 2,
            eligible_channel_count: 1,
            evaluated_channel_ids: [1841, 1842],
            eligible_channel_ids: [1841],
            attempts: {
              attempt_count: 3,
              negative_profit_attempt_count: 1,
              realized_a_1h: "1",
              realized_profit_cny: "0.5"
            },
            runtime_usage: {
              request_count_1h: 7,
              consumed_a_1h: "3.25",
              group_ids: [3, 26],
              last_used_at: "2026-08-03T07:59:57Z"
            }
          }
        ],
        channels: [
          {
            channel_id: 1841,
            channel_net_k_cny: "2.0",
            minimum_source_k_cny: "1.2",
            minimum_required_channel_k_cny: "1.92",
            headroom_cny: "0.08",
            headroom_bps: 666,
            eligible_sources: [{ source_id: 11, source_key: "owned-weekly-11" }]
          }
        ],
        summary: {
          source_count: 1,
          eligible_channel_count: 1,
          eligible_pair_count: 1,
          negative_profit_attempt_count: 1,
          realized_profit_cny: "0.5"
        }
      };
    } else if (url.pathname.endsWith("/internal/economic-priority1/gpt-preview")) {
      payload = {
        model: "gpt-preview",
        generation: null,
        projection: null,
        projection_matches_generation: false,
        sources: [],
        channels: [],
        summary: {}
      };
    } else if (url.pathname.endsWith("/api/merchants/1/credentials")) {
      payload = {
        merchant_id: "1",
        username: "fixture-account",
        email: "fixture@example.invalid",
        password: "fixture-password-secret"
      };
    } else if (url.pathname.endsWith("/api/merchants/1/inventory")) {
      payload = {
        merchant_id: "1",
        supplier_state: "active",
        supplier: {
          id: "308",
          name: "OmniAPI",
          description: "多模型 API 中转服务"
        },
        channels: [
          {
            id: "1148",
            name: "SOL Route",
            nickname: "SOL Alias",
            description: "优质稳定线路",
            models: ["gpt-5.6-sol"],
            listings: [
              {
                id: "4100",
                own_model: "gpt-5.6-sol",
                platform_model: "gpt-5.6-sol",
                display_name: "SOL",
                description: "Team+Pro混池",
                status: "0",
                availability: "0",
                visibility: "0"
              }
            ]
          }
        ]
      };
    } else {
      throw new Error(`unexpected fetch: ${url.pathname}`);
    }
    return { ok: true, json: async () => payload };
  };

  const context = vm.createContext({
    AbortController,
    console,
    document,
    encodeURIComponent,
    fetch,
    Intl,
    URL,
    window: {
      clearTimeout,
      setInterval: () => 1,
      setTimeout
    }
  });
  vm.runInContext(script, context, { filename: "trade_dashboard.html" });
  await settle();

  const rows = document.getElementById("merchant-rows");
  assert.deepEqual(fetchCalls, [
    "/a6-trader/api/overview",
    "/a6-trader/api/merchants",
    "/a6-trader/api/market-traffic",
    "/a6-trader/internal/provider-pools/gpt-5.6-sol",
    "/a6-trader/internal/provider-inventory/gpt-5.6-sol",
    "/a6-trader/internal/economic-priority1/gpt-5.6-sol"
  ]);
  assert.deepEqual(trafficModels, ["gpt-5.6-sol"]);
  assert.match(document.getElementById("traffic-position-rows").textContent, /OmniAPI SOL Route/);
  assert.match(document.getElementById("traffic-position-rows").textContent, /经营中/);
  assert.match(document.getElementById("traffic-position-rows").textContent, /98.75%/);
  assert.match(document.getElementById("traffic-position-rows").textContent, /fast/);
  assert.match(document.getElementById("traffic-position-rows").textContent, /商户 supplier_submitted/);
  assert.match(document.getElementById("traffic-position-rows").textContent, /绑定 verified/);
  assert.doesNotMatch(document.getElementById("traffic-position-rows").textContent, /Paused Managed Listing/);
  assert.equal(
    findByDataset(document.getElementById("traffic-position-rows"), "operationalState").length,
    1
  );
  assert.equal(document.getElementById("traffic-position-count").textContent, "1 / 2");
  assert.equal(document.getElementById("traffic-band-rows").childNodes[0].childNodes[2].textContent, "1");

  const positionFilter = document.getElementById("traffic-position-filter");
  positionFilter.value = "all";
  positionFilter.dispatch("change");
  assert.match(document.getElementById("traffic-position-rows").textContent, /OmniAPI SOL Route/);
  assert.match(document.getElementById("traffic-position-rows").textContent, /Paused Managed Listing/);
  assert.equal(
    findByDataset(document.getElementById("traffic-position-rows"), "operationalState").length,
    2
  );
  assert.equal(document.getElementById("traffic-position-count").textContent, "2 / 2");

  positionFilter.value = "unlisted";
  positionFilter.dispatch("change");
  assert.doesNotMatch(document.getElementById("traffic-position-rows").textContent, /OmniAPI SOL Route/);
  assert.match(document.getElementById("traffic-position-rows").textContent, /Paused Managed Listing/);
  assert.match(document.getElementById("traffic-position-rows").textContent, /未经营/);
  assert.match(
    document.getElementById("traffic-position-rows").textContent,
    /Listing 未处于上架状态/
  );
  assert.match(document.getElementById("traffic-position-rows").textContent, /Listing 状态 0/);
  assert.equal(
    findByDataset(document.getElementById("traffic-position-rows"), "operationalState").length,
    1
  );
  assert.equal(document.getElementById("traffic-position-count").textContent, "1 / 2");
  assert.equal(document.getElementById("traffic-band-rows").childNodes[0].childNodes[2].textContent, "1");
  assert.match(document.getElementById("traffic-failure-rows").textContent, /invalid_prompt/);
  assert.match(document.getElementById("traffic-failure-rows").textContent, /用户请求/);
  assert.match(document.getElementById("provider-summary").textContent, /42/);
  assert.match(document.getElementById("provider-summary").textContent, /published/);
  assert.equal(document.getElementById("provider-mode").hidden, false);
  assert.equal(document.getElementById("provider-mode").textContent, "最近切换失败 · r43");
  assert.match(document.getElementById("provider-summary").textContent, /1 \/ 1/);
  assert.match(document.getElementById("provider-slot-rows").textContent, /READY · READY/);
  assert.match(document.getElementById("provider-slot-rows").textContent, /可调度/);
  assert.match(document.getElementById("provider-slot-rows").textContent, /1008/);
  assert.doesNotMatch(document.getElementById("provider-slot-rows").textContent, /1012/);
  assert.match(document.getElementById("provider-slot-rows").textContent, /x0\.67551 \/ x0\.67551/);
  assert.equal(document.getElementById("economic-mode").textContent, "Plan only");
  assert.match(document.getElementById("economic-summary").textContent, /plan_only/);
  assert.match(document.getElementById("economic-source-rows").textContent, /owned-weekly-11/);
  assert.match(document.getElementById("economic-source-rows").textContent, /18 \/ 82 \/ 100/);
  assert.match(document.getElementById("economic-source-rows").textContent, /临时 100 A/);
  assert.match(document.getElementById("economic-source-rows").textContent, /1 points · 18%/);
  assert.match(document.getElementById("economic-source-rows").textContent, /¥1\.92/);
  assert.match(document.getElementById("economic-source-rows").textContent, /¥2\.304/);
  assert.match(document.getElementById("economic-source-rows").textContent, /#1841/);
  assert.match(document.getElementById("economic-source-rows").textContent, /1h 3\.25 A/);
  assert.match(document.getElementById("economic-source-rows").textContent, /7 请求 · Group 3, 26/);
  assert.equal(
    document.getElementById("economic-source-rows").childNodes[0].childNodes.length,
    9
  );
  assert.match(document.getElementById("economic-channel-rows").textContent, /1841/);
  assert.equal(document.getElementById("economic-profit-percent").value, "60");
  assert.equal(document.getElementById("economic-gross-floor").textContent, "1.92x");
  assert.match(document.getElementById("economic-profit-threshold-heading").textContent, /60%/);
  assert.doesNotMatch(rows.textContent, /fixture-password-secret/);

  const merchantToggle = findByDataset(rows, "merchantToggle")[0];
  rows.dispatch("click", merchantToggle);
  assert.match(rows.textContent, /账号密码已遮罩/);
  await settle();
  assert.equal(fetchCalls.at(-1), "/a6-trader/api/merchants/1/inventory");
  assert.equal(fetchCalls.length, 7);
  assert.match(rows.textContent, /OmniAPI/);
  assert.match(rows.textContent, /多模型 API 中转服务/);
  assert.match(rows.textContent, /SOL Route/);
  assert.match(rows.textContent, /1 可辖 · 0 上架 · 1 下架/);
  assert.match(rows.textContent, /已下架/);
  assert.doesNotMatch(rows.textContent, /1 已上架/);
  const editPaths = findByDataset(rows, "editPath").map((form) => form.dataset.editPath);
  assert.deepEqual(editPaths, [
    "api/merchants/1/channels/1148",
    "api/merchants/1/listings/4100"
  ]);
  assert.ok(editPaths.every((path) => !path.endsWith("/profile")));

  const reveal = findByDataset(rows, "credentialToggle")[0];
  rows.dispatch("click", reveal);
  await settle();
  assert.equal(fetchCalls.at(-1), "/a6-trader/api/merchants/1/credentials");
  assert.equal(fetchCalls.length, 8);
  assert.match(rows.textContent, /fixture-account/);
  assert.match(rows.textContent, /fixture-password-secret/);

  const mask = findByDataset(rows, "credentialToggle")[0];
  rows.dispatch("click", mask);
  assert.equal(fetchCalls.length, 8);
  assert.match(rows.textContent, /账号密码已遮罩/);
  assert.doesNotMatch(rows.textContent, /fixture-account/);
  assert.doesNotMatch(rows.textContent, /fixture-password-secret/);

  failingPaths.add("/a6-trader/api/overview");
  document.getElementById("refresh").dispatch("click");
  await settle();
  assert.match(rows.textContent, /merchant-0001/);
  assert.match(document.getElementById("alert-text").textContent, /商户数据已更新/);
  assert.equal(document.getElementById("collector-text").textContent, "总览指标读取失败");

  const model = document.getElementById("traffic-model");
  const economicFetchesBeforeTrafficChange = fetchCalls.filter((path) => (
    path.includes("/internal/economic-priority1/")
  )).length;
  model.value = "gpt-preview";
  model.dispatch("change");
  await settle();
  assert.equal(fetchCalls.at(-1), "/a6-trader/internal/provider-inventory/gpt-preview");
  assert.equal(
    fetchCalls.filter((path) => path.includes("/internal/economic-priority1/")).length,
    economicFetchesBeforeTrafficChange
  );
  assert.equal(document.getElementById("provider-mode").hidden, false);
  assert.equal(document.getElementById("provider-mode").textContent, "等待应用 · r77");
  assert.match(document.getElementById("provider-freshness").textContent, /等待应用 r77/);
  assert.match(document.getElementById("provider-summary").textContent, /0 \/ 0/);
  assert.match(document.getElementById("provider-slot-rows").textContent, /当前没有激活槽位/);

  const economicModel = document.getElementById("economic-model");
  economicModel.value = "gpt-preview";
  economicModel.dispatch("change");
  await settle();
  assert.equal(fetchCalls.at(-1), "/a6-trader/internal/economic-priority1/gpt-preview");
  assert.equal(document.getElementById("economic-mode").hidden, true);
  assert.match(document.getElementById("economic-freshness").textContent, /尚无激活 generation/);
});
