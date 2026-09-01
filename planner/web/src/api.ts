import { Plan, SaveSlot, Language } from "./types";

// In dev, vite proxies /api → :8765. In prod the FastAPI server serves the
// built files itself, so relative URLs work either way.
const API = "";

// Share token (SEC-1): in --share/--tunnel mode, game-mutating endpoints
// require the per-run token. It arrives in the share URL as #t=<token> (or
// ?token=); we send it back as the X-Share-Token header on every write.
let shareToken = "";
{
  const m = location.hash.match(/[#&]t=([^&]+)/);
  if (m) shareToken = m[1];
  const q = new URLSearchParams(location.search).get("token");
  if (q) shareToken = q;
}
const authHeaders = (): Record<string, string> => (shareToken ? { "X-Share-Token": shareToken } : {});

export async function fetchSaves(): Promise<SaveSlot[]> {
  const r = await fetch(`${API}/api/saves`);
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}

export async function fetchLanguages(): Promise<Language[]> {
  const r = await fetch(`${API}/api/languages`);
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}

export async function fetchPlan(slot: string, lang: string): Promise<Plan> {
  const params = new URLSearchParams();
  if (slot) params.set("slot", slot);
  if (lang) params.set("lang", lang);
  const r = await fetch(`${API}/api/plan?${params}`);
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}

export async function cheatMoney(slot: string, copper: number, action: "set" | "add" = "set"): Promise<any> {
  const r = await fetch(`${API}/api/cheat/money`, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...authHeaders() },
    body: JSON.stringify({ slot, copper, action }),
  });
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}

export async function cheatSeed(slot: string, itemId: number, count: number = 10): Promise<any> {
  const r = await fetch(`${API}/api/cheat/seed`, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...authHeaders() },
    body: JSON.stringify({ slot, itemId, count }),
  });
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}

export async function fetchInventoryGrouped(slot: string): Promise<any> {
  const r = await fetch(`${API}/api/inventory/grouped?slot=${encodeURIComponent(slot)}`);
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}

export async function shopBuy(slot: string, itemId: number, count: number, price: number): Promise<any> {
  const r = await fetch(`${API}/api/shop/buy`, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...authHeaders() },
    body: JSON.stringify({ slot, itemId, count, price }),
  });
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}
export async function shopSell(slot: string, itemId: number, count: number, price: number): Promise<any> {
  const r = await fetch(`${API}/api/shop/sell`, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...authHeaders() },
    body: JSON.stringify({ slot, itemId, count, price }),
  });
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}

export async function fetchBridgeStatus(): Promise<any> {
  const r = await fetch(`${API}/api/bridge/status`, { cache: "no-store" });
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}
export async function fetchBridgeEvents(): Promise<any> {
  const r = await fetch(`${API}/api/bridge/events`, { cache: "no-store" });
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}
export async function fetchDebugSaves(): Promise<any> {
  const r = await fetch(`${API}/api/debug/saves`);
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}
