import { useEffect, useRef, useState } from "react";
import { fetchSaves, fetchLanguages, fetchPlan, cheatMoney, cheatSeed, fetchInventoryGrouped, shopBuy, shopSell, fetchBridgeStatus, fetchDebugSaves, fetchSinceSave } from "./api";
import {
  Plan, SaveSlot, Language, CookSuggestion, PlantSuggestion, WeekPlan, TrendItem,
} from "./types";

type Toast = { id: number; kind: "bridge" | "save" | "error"; text: string };

function fmtMoney(copper: number): string {
  const g = Math.floor(copper / 10000);
  const s = Math.floor((copper % 10000) / 100);
  const c = copper % 100;
  const parts = [];
  if (g) parts.push(`${g}g`);
  if (s || g) parts.push(`${s}s`);
  parts.push(`${c}c`);
  return parts.join(" ");
}

function Today({ today, save_mtime }: { today: Plan["today"]; save_mtime: number }) {
  const [now, setNow] = useState(Date.now());
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(id);
  }, []);
  const mins = Math.max(0, Math.floor((now / 1000 - save_mtime) / 60));
  const secs = Math.max(0, Math.floor((now / 1000 - save_mtime) % 60));
  const ago = mins > 0 ? `${mins}m ${secs}s ago` : `${secs}s ago`;
  return (
    <section className="today">
      <div className="stat">
        <div className="label">Date</div>
        <div className="value">{today.season} W{today.week_in_season}</div>
        <div className="label">{today.day_of_week} · year {today.year}</div>
      </div>
      <div className="stat">
        <div className="label">Trend rotates</div>
        <div className="value">in {today.next_trend_rotation_in_days}d</div>
      </div>
      <div className="stat">
        <div className="label">Money</div>
        <div className="value">{fmtMoney(today.money_copper)}</div>
      </div>
      <div className="stat">
        <div className="label">Tavern Rep</div>
        <div className="value">{today.tavern_rep}</div>
      </div>
      <div className="stat">
        <div className="label">Crops planted</div>
        <div className="value">{today.planted_count}</div>
        <div className="label">{today.unique_planted} unique</div>
      </div>
      <div className="stat">
        <div className="label">Recipes unlocked</div>
        <div className="value">{today.unlocked_recipes}</div>
      </div>
      <div className="stat">
        <div className="label">Tavern</div>
        <div className="value" style={{ fontSize: 14 }}>{today.tavern_name}</div>
        <div className="label">{today.player_name}</div>
      </div>
      <div className="stat">
        <div className="label">Last save</div>
        <div className="value" style={{ fontSize: 14 }}>{ago}</div>
        <div className="label">{new Date(save_mtime * 1000).toLocaleTimeString()}</div>
      </div>
    </section>
  );
}

function GroupedInventory({ slot, mode, onAction, onError, pushToast, bridgeLive }: { slot: string; mode: "buy" | "sell" | null; onAction: () => void; onError: (e: string) => void; pushToast: (k: "bridge"|"save"|"error", t:string)=>void; bridgeLive: boolean }) {
  const [data, setData] = useState<any>(null);
  const [err, setErr] = useState<string | null>(null);
  const reload = () => { if (!slot) return; fetchInventoryGrouped(slot).then(d=>{ setData(d); if(d.live_available) pushToast("bridge", `Live inventory: ${d.total_count} items (bridge realtime)`); }).catch(e => setErr(String(e))); };
  useEffect(() => { reload(); }, [slot]);
  if (err) return <div className="empty" style={{ color: "var(--bad)" }}>{err}</div>;
  if (!data) return <div className="empty">loading inventory…</div>;
  if (!bridgeLive && mode) {
    return <div className="card" style={{ borderColor: "var(--bad)", background: "#2a1a1a" }}><div style={{ fontWeight: 600, color: "var(--bad)" }}>● Read-only — bridge offline</div><div style={{ fontSize: 12, opacity: 0.8, marginTop: 4 }}>Buy/Sell/cheat all go through the in-game PlannerBridge (BepInEx) — it is the only write channel, so none of them work while it is offline (no save-patch fallback). Restart Travellers Rest — check <code>BepInEx/LogOutput.log</code> for <code>PlannerBridge 1.1.x loaded</code> and ensure <code>http://127.0.0.1:8766/ping</code> responds.</div></div>;
  }
  const groups = data.groups as Record<string, any[]>;
  const order = ["Seeds", "Farming (harvest)", "Foraging", "Fishing", "Vendors", "Crafted", "Other"];
  const icons = (id: number) => `/icons/${id}.png`;
  const handleClick = async (it: any) => {
    if (!mode) return;
    if (!bridgeLive) { onError("Bridge offline — cannot buy/sell"); pushToast("error", "✗ Bridge offline — restart TR"); return; }
    const countStr = prompt(`How many to ${mode}? (1-99)`, "1");
    if (!countStr) return;
    const n = parseInt(countStr, 10);
    if (isNaN(n) || n < 1) return;
    // price fallback: if buy_copper 0, estimate 100c per (same as bridge fallback) and warn
    const pricePer = mode === "buy" ? (it.buy_copper || 100) : (it.sell_copper || 50);
    if ((mode==="buy" && !it.buy_copper) || (mode==="sell" && !it.sell_copper)) {
      // confirm estimated price
      if (!confirm(`Price for ${it.name} is estimated ${fmtMoney(pricePer)} each (not in shop table). Continue?`)) return;
    }
    const total = pricePer * n;
    if (mode === "buy" && total > 0) {
      if (!confirm(`Buy ${n}× ${it.name} for ${fmtMoney(total)}? (same as gold cheat Money.-${total})`)) return;
    }
    try {
      if (mode === "buy") {
        const r = await shopBuy(slot, it.item_id, n, total);
        pushToast("bridge", `✓ Bought ${n}× ${it.name} for ${fmtMoney(total)} — in-game overlay will flash`);
        onAction();
        // reload twice: immediate (live) + after save flush 1s
        setTimeout(reload, 400);
        if (r?.queued) pushToast("save", "Queued — waiting for game main thread (~2s), check F8 overlay");
      } else {
        const r = await shopSell(slot, it.item_id, n, total);
        pushToast("bridge", `✓ Sold ${n}× ${it.name} for ${fmtMoney(total)}`);
        onAction();
        setTimeout(reload, 400);
        if (r?.queued) pushToast("save", "Queued sell — will apply on next frame");
      }
    } catch (e) {
      const msg = String(e);
      // Mirror gold cheat error style: surface bridge error (e.g., need X have Y)
      pushToast("error", `✗ ${mode} failed: ${msg.slice(0,220)}`);
      onError(msg);
    }
  };
  return (
    <div>
      <div style={{ fontSize: 12, opacity: 0.7, marginBottom: 8 }}>
        {data.total_items} unique · {data.total_count} total · grouped by where you get it {mode && <span style={{ color: "var(--accent)", fontWeight: 600 }}>· {mode.toUpperCase()} mode: click an item</span>}
        {data.live_available && <span className="realtime-badge" style={{ marginLeft: 8 }}>● LIVE bridge (no save needed)</span>}
        {!data.live_available && <span style={{ marginLeft: 8, opacity: 0.6 }}>· save-only (restart TR for live)</span>}
      </div>
      {order.map(cat => {
        const items = groups[cat];
        if (!items || !items.length) return null;
        return (
          <div key={cat} style={{ marginBottom: 14 }}>
            <h3 style={{ fontSize: 14, margin: "10px 0 6px", borderBottom: "1px solid var(--rule-soft)", paddingBottom: 4 }}>{cat} — {items.length}</h3>
            <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(160px, 1fr))", gap: 8 }}>
              {items.map((it: any) => {
                const clickable = (mode === "buy" && cat === "Vendors") || (mode === "sell");
                const price = mode === "buy" ? it.buy_copper : it.sell_copper;
                const isChanged = !!it.changed;
                return (
                  <div key={it.item_id} className="card" title={isChanged ? `Live changed from ${it.save_count} → ${it.count} (save not yet flushed)` : it.detail} onClick={() => clickable && handleClick(it)} style={{ display: "flex", alignItems: "center", gap: 8, padding: "8px 10px", cursor: clickable ? "pointer" : "default", border: isChanged ? "2px solid #4ade80" : clickable ? "1px solid var(--accent)" : undefined, opacity: clickable || !mode ? 1 : 0.5, background: isChanged ? "#1a3520" : undefined, boxShadow: isChanged ? "0 0 0 2px rgba(74,222,128,0.35)" : undefined }}>
                    <img src={icons(it.item_id)} alt="" width={32} height={32} style={{ imageRendering: "pixelated" }} onError={e => ((e.target as HTMLImageElement).style.display = "none")} />
                    <div style={{ flex: 1 }}>
                      <div style={{ fontWeight: 600, fontSize: 13 }}>{it.name} {isChanged && <span style={{ fontSize: 10, background: "#4ade80", color: "#000", padding: "1px 5px", borderRadius: 8, marginLeft: 6 }}>LIVE +{it.count - it.save_count}</span>}</div>
                      <div style={{ fontSize: 12, opacity: 0.8 }}>x{it.count} {isChanged && <span style={{ opacity: 0.7, fontSize: 11 }}> (save: x{it.save_count})</span>} {price ? <span style={{ opacity: 0.6 }}>· {mode === "buy" ? fmtMoney(price) + " buy" : fmtMoney(price) + " sell"}</span> : null}</div>
                      {it.detail && <div style={{ fontSize: 11, opacity: 0.6 }}>{it.detail}</div>}
                    </div>
                    {mode && clickable && <span style={{ fontSize: 10, background: "var(--accent)", color: "#fff", padding: "2px 6px", borderRadius: 10 }}>{mode}</span>}
                  </div>
                );
              })}
            </div>
          </div>
        );
      })}
    </div>
  );
}

function CookCard({ s, owned }: { s: CookSuggestion; owned?: Record<string, number> }) {
  const have = (id: number, need: number) => {
    if (!owned) return null;
    const got = owned[String(id)] ?? owned[id as any] ?? 0;
    return got >= need;
  };
  return (
    <div className="card">
      <div className="title">
        <span className="name">{s.recipe_name}</span>
        <span className="profit">+{fmtMoney(s.base_profit_with_trend - s.profit_per_craft)} · {fmtMoney(s.base_profit_with_trend)} total</span>
      </div>
      <div className="meta">
        {s.time_hours}h cook · fuel {s.fuel} · {Math.round(s.base_profit_with_trend / Math.max(s.time_hours, 0.01))}c/h
      </div>
      <div className="meta" style={{ display: "flex", flexWrap: "wrap", gap: 4, alignItems: "center" }}>
        <span>ingredients:</span>
        {s.ingredients.map(([id, a, n]) => {
          const ok = have(id, a);
          const got = owned ? (owned[String(id)] ?? owned[id as any] ?? 0) : 0;
          const style: React.CSSProperties =
            ok === null ? {} :
            ok ? { color: "#4ade80", fontWeight: 700, background: "rgba(74,222,128,0.12)", padding: "1px 6px", borderRadius: 6, border: "1px solid #4ade80" } :
                   { color: "#f87171", fontWeight: 700, background: "rgba(248,113,113,0.12)", padding: "1px 6px", borderRadius: 6, border: "1px solid #f87171" };
          const tip = owned ? `${got} / ${a} ${n} ${ok ? "✓ have" : "✗ missing " + (a - got)}` : `${a}× ${n}`;
          return <span key={id} style={style} title={tip}>{a}× {n} {ok !== null && (ok ? "✓" : `(${got}/${a})`)}</span>;
        }).reduce((acc: any[], el, i) => i === 0 ? [el] : [...acc, <span key={`plus-${i}`} style={{ opacity: 0.6 }}>+</span>, el], [] as any[])}
      </div>
      {s.missing_ingredients.length > 0 && <div className="meta" style={{ color: "#f87171" }}>missing in catalog: {s.missing_ingredients.join(", ")}</div>}
      {s.why.length > 0 && <div className="why">{s.why.join(" · ")}</div>}
    </div>
  );
}

function PlantCard({ s, slot, onError, bridgeLive, owned }: { s: PlantSuggestion; slot: string; onError: (e: string) => void; bridgeLive: boolean; owned?: Record<string, number> }) {
  const seedDisplay = s.seed_item_id && owned ? `${s.seed_name} #${s.seed_item_id}(${owned[String(s.seed_item_id)] ?? owned[s.seed_item_id] ?? 0})` : s.seed_item_id && !owned ? `<div className="meta">seed: {s.seed_name} #{s.seed_item_id}</div>` : null;
  const [busy, setBusy] = useState(false);
  const add = async (n: number) => {
    if (!bridgeLive) { onError("Bridge offline — restart TR with PlannerBridge for realtime seeds"); return; }
    if (!s.seed_item_id) { onError("No seed for this crop"); return; }
    setBusy(true);
    try { await cheatSeed(slot, s.seed_item_id, n); } catch (e) { onError(String(e)); } finally { setBusy(false); }
  };
  return (
    <div className="card">
      <div className="title">
        <span className="name">
          {s.crop_name} {s.is_best_now && <span className="badge best">BEST</span>}
        </span>
        <span className="profit">
          {s.plant_by_day === 0 ? "plant TODAY" : `plant within ${s.plant_by_day}d`}
        </span>
      </div>
      <div className="meta">
        {s.days_to_grow}d to grow
        {s.reusable && ` · perennial (regrow ${s.days_until_new_harvest}d)`}
        {" · "}{s.yield_per_harvest}/harvest
        {" · target trend wk+"}{s.target_for_trend_week}
      </div>
      {s.seed_item_id && owned && <div className="meta">{seedDisplay}</div>}
      <div className="why">{s.why.join(" · ")}</div>
      {s.seed_item_id && (
        <div style={{ display: "flex", gap: 6, marginTop: 6 }}>
          <button disabled={busy || !bridgeLive} onClick={() => add(10)}>+10 seeds</button>
          <button disabled={busy || !bridgeLive} onClick={() => add(30)}>+30</button>
          <button disabled={busy || !bridgeLive} onClick={() => add(99)}>+99</button>
          <span style={{ fontSize: 11, opacity: 0.6 }}>{!bridgeLive ? "bridge offline — read-only" : busy ? "adding..." : "needs BepInEx bridge (no Load)"}</span>
        </div>
      )}
    </div>
  );
}

function TrendItemRow({ t }: { t: TrendItem }) {
  let badge: { cls: string; text: string } | null = null;
  if (t.unlocked_recipe_ids.length > 0) badge = { cls: "ok", text: "unlocked" };
  else if (t.recipe_ids.length > 0) badge = { cls: "bad", text: "locked" };
  if (t.grow_crop_id) {
    if (t.grow_best_season_now) badge = { cls: "best", text: "BEST now" };
    else if (t.grow_in_season_now) badge = { cls: "ok", text: "in season" };
    else badge = { cls: "warn", text: "off-season" };
    if (t.is_planted) badge = { cls: "ok", text: `growing ${t.planted_count}` };
  }
  return (
    <div className="item">
      <span>{t.name}</span>
      {badge && <span className={`badge ${badge.cls}`}>{badge.text}</span>}
    </div>
  );
}

function CalendarWeek({ w }: { w: WeekPlan }) {
  return (
    <div className={"week" + (w.week_offset === 0 ? " current" : "")}>
      <h3>
        Week +{w.week_offset}
        <span className="when">{w.season_at_start} · in {w.days_until_start}d</span>
      </h3>
      <div className="group">
        <div className="label">Foods</div>
        {w.food_trends.map((t) => <TrendItemRow key={"f"+t.item_id} t={t} />)}
      </div>
      <div className="group">
        <div className="label">Drinks</div>
        {w.drink_trends.map((t) => <TrendItemRow key={"d"+t.item_id} t={t} />)}
      </div>
      <div className="group">
        <div className="label">Ingredients</div>
        {w.ingredient_trends.map((t) => <TrendItemRow key={"i"+t.item_id} t={t} />)}
      </div>
    </div>
  );
}

function Cheat({ slot, money, onReload, onError, pushToast, bridgeLive }: { slot: string; money: number; onReload: () => void; onError: (e: string) => void; pushToast: (k:"bridge"|"save"|"error", t:string)=>void; bridgeLive: boolean }) {
  const doCheat = async (copper:number, label:string, action:"add"|"set") => {
    // Bridge is the ONLY mutation channel — no save-patch fallback. If the
    // bridge is offline the request refuses (503 "game not running") and we
    // surface that as an error.
    try { const r = await cheatMoney(slot, copper, action);
      const verified = r.before >= 0 && r.after >= 0 ? ` (${fmtMoney(r.before)} → ${fmtMoney(r.after)})` : "";
      pushToast("bridge", `✓ ${label}: realtime${r.queued?" (queued)":""}${verified}`);
      setTimeout(onReload, 350); setTimeout(onReload, 1200); }
    catch(e){ const m=String(e); pushToast("error", `✗ Gold failed: ${m.slice(0,220)}`); onError(m); }
  };
  return (
    <div className="card" style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
      <span style={{ fontWeight: 600 }}>Cheat:</span>
      <button onClick={() => doCheat(50000, "+5g", "add")}>+5g</button>
      <button onClick={() => doCheat(200000, "+20g", "add")}>+20g</button>
      <button onClick={() => doCheat(500000, "+50g", "add")}>+50g</button>
      <button onClick={async () => { const v = prompt("Set copper (1g=10000):", String(money)); if(v!==null){ const n=parseInt(v,10); if(!isNaN(n)){ await doCheat(n, `Set ${fmtMoney(n)}`, "set");}}}}>Set...</button>
      <span style={{ fontSize: 12, opacity: 0.7 }}>→ {fmtMoney(money)} now {`·`} bridge is realtime like buy/sell (no Load, in-game overlay flashes)</span>
    </div>
  );
}

// SLS-1: "things completed since your last save" — TR only persists on sleep,
// so this panel shows what exists ONLY in the live game right now (live diff
// catches in-game play; the action log catches planner-initiated mutations).
function SinceSave({ data }: { data: any }) {
  if (!data) return null;
  const moneyD = data.money?.delta_copper ?? 0;
  const changed: any[] = data.changed_items ?? [];
  const actions: any[] = data.actions ?? [];
  if (!changed.length && !actions.length && moneyD === 0) return null;
  const chip = (text: string, good: boolean) => (
    <span key={text} style={{ fontSize: 12, padding: "2px 8px", borderRadius: 8,
      border: `1px solid ${good ? "var(--good, #4e7030)" : "var(--bad, #983d3d)"}`,
      color: good ? "var(--good, #4e7030)" : "var(--bad, #983d3d)" }}>{text}</span>
  );
  return (
    <div className="card" style={{ marginTop: 10, display: "flex", flexDirection: "column", gap: 6 }}>
      <div>
        <strong>Since last save</strong>{" "}
        <span style={{ fontSize: 12, opacity: 0.7 }}>
          (saved {data.save_time} — sleep to persist; quitting before that loses this)
        </span>
      </div>
      <div style={{ display: "flex", gap: 6, flexWrap: "wrap", alignItems: "center" }}>
        {moneyD !== 0 && chip(`${moneyD > 0 ? "+" : ""}${fmtMoney(moneyD)}`, moneyD > 0)}
        {changed.slice(0, 12).map((c) =>
          chip(`${c.delta > 0 ? "+" : ""}${c.delta} × ${c.name} (${c.save_count} → ${c.live_count})`, c.delta > 0))}
        {actions.length > 0 &&
          <span style={{ fontSize: 12, opacity: 0.7 }}>
            + {actions.length} planner action{actions.length > 1 ? "s" : ""}
          </span>}
      </div>
    </div>
  );
}

export default function App() {
  const [saves, setSaves] = useState<SaveSlot[]>([]);
  const [langs, setLangs] = useState<Language[]>([]);
  const [slot, setSlot] = useState<string>("");
  const [lang, setLang] = useState<string>("English");
  const [plan, setPlan] = useState<Plan | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [wsLive, setWsLive] = useState(false);
  const [bridgeLive, setBridgeLive] = useState(false);
  const [bridgeDebug, setBridgeDebug] = useState<any>(null);
  const [debugSaves, setDebugSaves] = useState<any>(null);
  const [shopMode, setShopMode] = useState<"buy" | "sell" | null>(null);
  const [toasts, setToasts] = useState<Toast[]>([]);
  const [pulseKey, setPulseKey] = useState(0);
  const [sinceSave, setSinceSave] = useState<any>(null);
  const toastId = useRef(0);
  const wsRef = useRef<WebSocket | null>(null);

  const pushToast = (kind: Toast["kind"], text: string) => {
    const id = ++toastId.current;
    setToasts(t => [...t, { id, kind, text }]);
    setTimeout(() => setToasts(t => t.filter(x => x.id !== id)), 3400);
  };

  useEffect(() => {
    fetchSaves().then((s) => {
      setSaves(s);
      if (s.length && !slot) setSlot(s[0].slot_id);
    }).catch((e) => setError(String(e)));
    fetchLanguages().then(setLangs).catch(() => {});
  }, []);

  const reload = async () => {
    if (!slot) return;
    try {
      const p = await fetchPlan(slot, lang);
      setPlan(p);
      setError(null);
    } catch (e) {
      setError(String(e));
    }
  };
  useEffect(() => { reload(); }, [slot, lang]);

  // Realtime single-file watcher: listen for save_changed + bridge_event from same /ws
  // Graphical feedback: toast + pulse glow + auto-reload (200ms for bridge, 300ms for save)
  useEffect(() => {
    const proto = location.protocol === "https:" ? "wss" : "ws";
    const url = `${proto}://${location.host}/ws`;
    let alive = true;
    const open = () => {
      const ws = new WebSocket(url);
      wsRef.current = ws;
      ws.onopen = () => { setWsLive(true); pushToast("save", "Watcher live — single-file File_1 realtime"); };
      ws.onclose = () => {
        setWsLive(false);
        if (alive) setTimeout(open, 2000);
      };
      ws.onmessage = (ev) => {
        try {
          const m = JSON.parse(ev.data);
          if (m.type === "save_changed") {
            // SINGLE FILE — only File_1, so reload immediately with faster 300ms debounce
            const file = m.path?.split("/").pop() ?? "save";
            const reason = m.reason ?? "watcher";
            pushToast("save", `Save ${reason}: ${file} → reloading...`);
            setPulseKey(k => k + 1);
            setTimeout(reload, 300);
            // also refresh debug panel
            fetchDebugSaves().then(setDebugSaves).catch(()=>{});
          } else if (m.type === "bridge_event") {
            // Bridge pushes verified before/after with every mutation — show
            // "2 → 7" instead of raw JSON so the user sees what the game did.
            const ev2 = m.event ?? m;
            const tp = ev2.type ?? ev2.data?.type ?? "change";
            const d = ev2.data ?? {};
            const g = (c: number) => fmtMoney(c ?? -1);
            const label =
              tp === "addItem" || tp === "addSeed"
                ? `+${d.count ?? "?"} x #${d.itemId ?? "?"}${d.after >= 0 ? ` (${d.before} → ${d.after})` : ""}`
                : tp === "addMoney"
                ? `${(Number(d.copper ?? 0) / 10000).toFixed(2)}g${d.after >= 0 ? ` (${g(d.before)} → ${g(d.after)})` : ""}`
                : tp === "shop/buy" || tp === "shop/sell"
                ? `${tp === "shop/buy" ? "bought" : "sold"} ${d.count ?? "?"} x #${d.itemId ?? "?"}${
                    d.after_item >= 0 ? ` (item ${d.before_item} → ${d.after_item} · ${g(d.before_money)} → ${g(d.after_money)})` : ""
                  }`
                : tp === "addItem_error"
                ? `add failed: ${d.error ?? "?"}`
                : tp === "value_read"
                ? null
                : tp;
            if (label) pushToast("bridge", `✓ Bridge ${label}`);
            setPulseKey(k => k + 1);
            // bridge events are realtime — reload sooner (150ms) to show inventory change before save flush
            setTimeout(reload, 150);
            setTimeout(() => fetchDebugSaves().then(setDebugSaves).catch(()=>{}), 600);
          } else if (m.type === "live_status") {
            // Bridge heartbeat flipped (G0) with a reason (DEG-1): live,
            // no_bridge (quiet — game closed is normal), beat_lost (loud —
            // we had beats and they stopped).
            setBridgeLive(!!m.live);
            setBridgeDebug((prev: any) => ({ ...(prev ?? {}), reason: m.reason }));
            if (m.reason === "beat_lost")
              pushToast("error", "✗ Bridge heartbeat lost — save-only mode. If the game is still running, check BepInEx/LogOutput.log");
          } else if (m.type === "cart_updated" || m.type === "menu_updated") {
            pushToast("save", `${m.type}`);
          }
        } catch {}
      };
    };
    open();
    return () => { alive = false; wsRef.current?.close(); };
  }, [slot, lang]);

  // SLS-1: "since last save" — refetch on every save change and bridge event
  // (pulseKey bumps on both), so the panel tracks what's not yet persisted.
  useEffect(() => {
    if (!slot) return;
    fetchSinceSave(slot).then(setSinceSave).catch(() => setSinceSave(null));
  }, [slot, pulseKey]);

  // Bridge liveness via planner proxy (/api/bridge/status) — when down, site is read-only (no buy/sell)
  useEffect(() => {
    let alive = true;
    const check = async () => {
      try {
        const j = await fetchBridgeStatus();
        // Heartbeat freshness (j.live, G0) is the mode authority; fall back to
        // the raw proxy result for older bridges without heartbeats.
        const live = j.live !== undefined ? !!j.live : !!j.bridge;
        setBridgeLive(live);
        setBridgeDebug(j);
        if (!live) setShopMode(null); // force readonly when bridge down
      } catch { setBridgeLive(false); setBridgeDebug(null); setShopMode(null); }
      if (alive) setTimeout(check, 3000);
    };
    check();
    const dbg = setInterval(() => fetchDebugSaves().then(setDebugSaves).catch(()=>{}), 5000);
    fetchDebugSaves().then(setDebugSaves).catch(()=>{});
    return () => { alive = false; clearInterval(dbg); };
  }, []);

  return (
    <>
      {/* Graphical feedback toast stack — bridge saves + watcher */}
      <div className="toast-stack">
        {toasts.map(t => (
          <div key={t.id} className={`toast ${t.kind}`}>{t.text}</div>
        ))}
      </div>
      <header className="top">
        <h1>TRAVELLERS REST PLANNER</h1>
        <span className="realtime-badge" title="Single-file File_1 realtime mode">● SINGLE File_1 realtime</span>
        <select value={slot} onChange={(e) => setSlot(e.target.value)}>
          {saves.map((s) => (
            <option key={s.slot_id} value={s.slot_id}>{s.label}</option>
          ))}
        </select>
        <select value={lang} onChange={(e) => setLang(e.target.value)}>
          {langs.map((l) => (
            <option key={l.code} value={l.name}>{l.name}</option>
          ))}
        </select>
        <span className={"ws-status " + (wsLive ? "live" : "dead")} title="Save watcher (8765) — File_1 only, 200ms debounce">
          {wsLive ? "watcher live" : "watcher offline"}
        </span>
        <span className={"ws-status " + (bridgeLive ? "live" : "dead")} title={bridgeDebug ? `BepInEx bridge ${bridgeDebug.version ?? ""} uptime ${Math.round(bridgeDebug.uptime_s ?? 0)}s, ${bridgeDebug.requests ?? 0} req` : "BepInEx bridge (8766) — realtime buy/sell, in-game overlay F8"}>
          {bridgeLive
            ? `bridge live${bridgeDebug?.requests ? " · " + bridgeDebug.requests + " req" : ""}`
            : bridgeDebug?.reason === "beat_lost"
            ? "bridge lost — check BepInEx log"
            : "bridge offline"}
        </span>
      </header>

      <nav style={{ display: "flex", gap: 12, padding: "8px 16px", background: "var(--parch-soft)", borderBottom: "1px solid var(--rule-soft)", position: "sticky", top: 0, zIndex: 5, flexWrap: "wrap" }}>
        <a href="#today">Today</a>
        <a href="#inventory">Inventory</a>
        <a href="#plant">Plant now</a>
        <a href="#cook">Cook now</a>
        <a href="#brew">Brew now</a>
        <a href="#calendar">Calendar</a>
        <a href="#cheat">Cheat</a>
      </nav>

      <SinceSave data={sinceSave} />

      <div style={{ display: "flex" }}>
        <aside style={{ width: 72, background: "var(--parch)", borderRight: "1px solid var(--rule-soft)", padding: 8, display: "flex", flexDirection: "column", gap: 10, alignItems: "center", position: "sticky", top: 40, height: "calc(100vh - 40px)", alignSelf: "flex-start" }}>
          <div style={{ fontSize: 11, opacity: 0.6, textAlign: "center" }}>Shop<br/>Remote</div>
          <button disabled={!bridgeLive} onClick={() => bridgeLive && setShopMode(shopMode === "buy" ? null : "buy")} title={bridgeLive ? "Buy mode: click vendor items to buy (needs bridge)" : "Read-only — bridge offline: restart TR with PlannerBridge"} style={{ width: 56, height: 56, borderRadius: 12, border: shopMode==="buy" ? "2px solid var(--accent)" : "1px solid var(--rule)", background: !bridgeLive ? "#2a2a2a" : shopMode==="buy" ? "var(--accent)" : "var(--parch-bright)", color: !bridgeLive ? "#777" : shopMode==="buy" ? "#fff" : "inherit", display: "flex", flexDirection: "column", alignItems: "center", justifyContent: "center", gap: 2, opacity: !bridgeLive ? 0.5 : 1, cursor: !bridgeLive ? "not-allowed" : "pointer" }}>
            <i className="fa-solid fa-cart-shopping" style={{ fontSize: 20 }}></i>
            <span style={{ fontSize: 10 }}>{!bridgeLive ? "OFF" : "BUY"}</span>
          </button>
          <button disabled={!bridgeLive} onClick={() => bridgeLive && setShopMode(shopMode === "sell" ? null : "sell")} title={bridgeLive ? "Sell mode: click inventory to sell (needs bridge)" : "Read-only — bridge offline"} style={{ width: 56, height: 56, borderRadius: 12, border: shopMode==="sell" ? "2px solid #c0392b" : "1px solid var(--rule)", background: !bridgeLive ? "#2a2a2a" : shopMode==="sell" ? "#c0392b" : "var(--parch-bright)", color: !bridgeLive ? "#777" : shopMode==="sell" ? "#fff" : "inherit", display: "flex", flexDirection: "column", alignItems: "center", justifyContent: "center", gap: 2, opacity: !bridgeLive ? 0.5 : 1, cursor: !bridgeLive ? "not-allowed" : "pointer" }}>
            <i className="fa-solid fa-coins" style={{ fontSize: 20 }}></i>
            <span style={{ fontSize: 10 }}>{!bridgeLive ? "OFF" : "SELL"}</span>
          </button>
          {shopMode && bridgeLive && <button onClick={() => setShopMode(null)} style={{ fontSize: 11 }}>Exit</button>}
          {!bridgeLive && <div style={{ fontSize: 10, color: "var(--bad)", textAlign: "center", lineHeight: 1.2 }}>Read-only<br/>bridge<br/>offline</div>}
          <div style={{ fontSize: 10, opacity: 0.5, textAlign: "center", marginTop: 8 }}>{bridgeLive ? <>Not cheating<br/>just no walk<br/>to store</> : <>Gold cheat<br/>still works<br/>(save patch)</>}</div>
        </aside>

        <main style={{ flex: 1, minWidth: 0 }}>
          {/* Debug panel — single-file + bridge verbose */}
          {debugSaves && (
            <details className="debug-panel" open={false}>
              <summary style={{ cursor: "pointer", fontWeight: 600 }}>debug · single-file File_1 realtime · click to expand</summary>
              <div style={{ marginTop: 8 }}>watcher: {wsLive ? "live" : "offline"} · single_file={String(debugSaves.single_file_mode)} · slots_scanned={debugSaves.all_slots_scanned} · root={debugSaves.saves_root}</div>
              {debugSaves.slot && <div>File_1: {debugSaves.slot.latest_file} · {debugSaves.slot.size} bytes · age {Math.round(debugSaves.slot.age_s)}s · mtime {debugSaves.slot.mtime_str}</div>}
              {bridgeDebug && <div>bridge: {bridgeDebug.bridge ? "live" : "down"} · ver {bridgeDebug.version} · up {Math.round(bridgeDebug.uptime_s ?? 0)}s · req {bridgeDebug.requests} · events {bridgeDebug.events}</div>}
              <div style={{ opacity: 0.6, marginTop: 6 }}>In-game overlay: F8 tests bridge, green/red toasts on AddItem/Money/Shop. Save watcher only tracks File_1 — SaveAnywhere disabled. Check BepInEx/LogOutput.log for [notify:*] lines.</div>
            </details>
          )}
          <div key={pulseKey} className={pulseKey ? "pulse" : ""} style={{ borderRadius: 8 }}>
          {error && <div className="card" style={{ borderColor: "var(--bad)", color: "var(--bad)" }}>{error}</div>}
          {!plan && !error && <div className="empty">loading…</div>}
          {plan && (
            <>
              <section id="today"><Today today={plan.today} save_mtime={plan.state.save_mtime} /></section>

              <h2 id="inventory" className="section">Inventory — {Object.keys(plan.today.item_counts).length} items { !bridgeLive && <span style={{ fontSize: 11, color: "var(--bad)", fontWeight: 600, marginLeft: 8 }}>● READ-ONLY (bridge offline)</span>} <a href="#top" style={{ marginLeft: 6, fontSize: 12, opacity: 0.5 }}>^</a></h2>
              <GroupedInventory slot={slot} mode={shopMode} onAction={reload} onError={setError} pushToast={pushToast} bridgeLive={bridgeLive} />

              {/* ── Plant now ──
               Shows crops to plant this week, filtered by best season + trending.
               Each card displays days-to-grow, yield, and seed stock (owned count).
               Buttons +10/+30/+99 add seeds via the BepInEx bridge (requires live bridge).
               Seed counts are highlighted green/red against recipe needs.
               */}
              <h2 id="plant" className="section">Plant now <a href="#top" style={{ marginLeft: 6, fontSize: 12, opacity: 0.5 }}>^</a></h2>
              {plan.plant_now.length === 0
                ? <div className="empty">Nothing trending that you can plant in time.</div>
                : <div className="row">
                    <div>{plan.plant_now.filter((_, i) => i % 2 === 0).map((s) => <PlantCard key={s.crop_id} s={s} slot={slot} onError={setError} bridgeLive={bridgeLive} />)}</div>
                    <div>{plan.plant_now.filter((_, i) => i % 2 === 1).map((s) => <PlantCard key={s.crop_id} s={s} slot={slot} onError={setError} bridgeLive={bridgeLive} />)}</div>
                  </div>}

              {/* ── Cook now ──
               Shows trending food recipes unlocked for the current week.
               Each card displays profit, time, fuel, and ingredient availability
               (green ✓ if you have enough, red ✗ if missing).
               Ingredients are color‑coded against your live inventory counts.
               */}
              <h2 id="cook" className="section">Cook now (trending food, unlocked) <a href="#top" style={{ marginLeft: 6, fontSize: 12, opacity: 0.5 }}>^</a></h2>
              {plan.cook_now.length === 0
                ? <div className="empty">No trending food recipes unlocked.</div>
                : plan.cook_now.map((s) => <CookCard key={s.recipe_id} s={s} owned={plan.today.item_counts as any} />)}

              {/* ── Brew now ──
               Shows trending drink recipes unlocked for the current week.
               Same layout as Cook now: profit, time, fuel, and ingredient availability.
               Ingredients highlighted green/red against your live inventory.
               */}
              <h2 id="brew" className="section">Brew now (trending drinks, unlocked) <a href="#top" style={{ marginLeft: 6, fontSize: 12, opacity: 0.5 }}>^</a></h2>
              {plan.brew_now.length === 0
                ? <div className="empty">No trending drinks unlocked.</div>
                : plan.brew_now.map((s) => <CookCard key={s.recipe_id} s={s} owned={plan.today.item_counts as any} />)}

              {/* ── 4‑week trend calendar ──
               Calendar weeks 0 (current) through 3, each with its food/drink/ingredient
               trends, season, and deadlines for planting/crafting.
               Click a week to explore details; helps plan ahead for seed buying,
               crop rotation, and profit maximisation.
               */}
              <h2 id="calendar" className="section">4-week trend calendar <a href="#top" style={{ marginLeft: 6, fontSize: 12, opacity: 0.5 }}>^</a></h2>
              <div className="calendar">
                {plan.calendar.map((w) => <CalendarWeek key={w.week_offset} w={w} />)}
              </div>

              {/* ── Cheat ──
               Gold‑cheat controls. The bridge is the ONLY mutation channel:
               when the bridge is offline the planner refuses (503 "game not
               running") — there is no save‑patch fallback. Every mutation
               returns verified before/after copper.
               F8 toggles in‑game overlay toast (PlannerBridge F8 handler).
               /debug/inventory shows live bridge counts for diagnostics.
               SaveAnywhere removed — only File_1 is tracked in realtime.
               */}
              <h2 id="cheat" className="section">Cheat <a href="#top" style={{ marginLeft: 6, fontSize: 12, opacity: 0.5 }}>^</a></h2>
              <Cheat slot={slot} money={plan.today.money_copper} onReload={reload} onError={setError} pushToast={pushToast} bridgeLive={bridgeLive} />
              <div style={{ fontSize: 12, opacity: 0.6, marginTop: 6 }}>BepInEx bridge 1.1.1 realtime — buy/sell now as reliable as gold cheat (sync main-thread wait + TryForceSave + live inventory). SaveAnywhere removed — only File_1 realtime. F8 overlay, /debug/inventory for diagnostics.</div>
            </>
          )}
          </div>
        </main>
      </div>
    </>
  );
}
