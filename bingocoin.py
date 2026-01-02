#!/usr/bin/env python3
from __future__ import annotations

import json
import secrets
import time
from typing import Dict, Any, List, Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, Body
from fastapi.responses import HTMLResponse
import uvicorn

import os

# ==========================
# CONFIG
# ==========================
APP_TITLE = "BingoCoin"
CASHIER_PIN = "4321"   # Cambialo
PORT = 9090

# ==========================
# IN-MEMORY STATE (NO DB)
# ==========================
GAME: Dict[str, Any] = {
    "players": {},   # user_id -> {name, role, saldo}
    "ledger": {},    # user_id -> list of {delta, note, ts}
    "prizes": [],    # list of {name, amount, winner_id, paid, ts}
    "pot": 0,        # montepremi
    "log": [],
}

# ==========================
# UTIL
# ==========================
def now_ts() -> int:
    return int(time.time())

def add_log(msg: str) -> None:
    GAME["log"].insert(0, {"ts": now_ts(), "msg": msg})
    GAME["log"] = GAME["log"][:300]

def ensure_ledger(uid: str) -> None:
    if uid not in GAME["ledger"]:
        GAME["ledger"][uid] = []

def add_ledger_delta(uid: str, delta: int, note: str) -> None:
    ensure_ledger(uid)
    GAME["ledger"][uid].insert(0, {"delta": int(delta), "note": note, "ts": now_ts()})
    GAME["ledger"][uid] = GAME["ledger"][uid][:1500]

def get_cashier_id() -> Optional[str]:
    for uid, p in GAME["players"].items():
        if p.get("role") == "cassiere":
            return uid
    return None

def require_cashier(req: Request) -> str:
    uid = req.headers.get("X-User")
    if not uid or uid not in GAME["players"]:
        raise PermissionError("Non autorizzato")
    if GAME["players"][uid]["role"] != "cassiere":
        raise PermissionError("Solo cassiere")
    return uid

def require_player(req: Request) -> str:
    uid = req.headers.get("X-User")
    if not uid or uid not in GAME["players"]:
        raise PermissionError("Non autorizzato")
    if GAME["players"][uid]["role"] != "giocatore":
        raise PermissionError("Solo giocatore")
    return uid

def total_prizes_defined_unpaid() -> int:
    return sum(int(p.get("amount", 0)) for p in GAME["prizes"] if not p.get("paid", False))

def total_prizes_paid() -> int:
    return sum(int(p.get("amount", 0)) for p in GAME["prizes"] if p.get("paid"))

# ==========================
# PRIVACY / STATE EXPORT
# ==========================
def _players_public_for(viewer_id: Optional[str]) -> List[Dict[str, Any]]:
    viewer = GAME["players"].get(viewer_id) if viewer_id else None
    viewer_role = viewer["role"] if viewer else None

    out: List[Dict[str, Any]] = []
    for uid, pdata in GAME["players"].items():
        item = {"id": uid, "name": pdata["name"], "role": pdata["role"]}
        if viewer_role == "cassiere":
            item["saldo"] = int(pdata.get("saldo", 0))
        else:
            item["saldo"] = int(pdata.get("saldo", 0)) if (viewer_id and uid == viewer_id) else None
        out.append(item)

    out.sort(key=lambda x: x["name"].lower())
    return out

def _my_history_public_for(viewer_id: Optional[str]) -> List[Dict[str, Any]]:
    if not viewer_id or viewer_id not in GAME["players"]:
        return []
    if GAME["players"][viewer_id]["role"] != "giocatore":
        return []
    ensure_ledger(viewer_id)
    return [{"delta": int(x["delta"]), "note": x["note"], "ts": int(x["ts"])} for x in GAME["ledger"][viewer_id]][:250]

def public_state_for(viewer_id: Optional[str]) -> Dict[str, Any]:
    viewer = GAME["players"].get(viewer_id) if viewer_id else None
    role = viewer["role"] if viewer else None

    # Premi:
    # - cassiere: tutti
    # - giocatore: solo non assegnati (paid=False)
    if role == "cassiere":
        prizes_out = GAME["prizes"]
    elif role == "giocatore":
        prizes_out = [p for p in GAME["prizes"] if not p.get("paid", False)]
    else:
        prizes_out = []

    pot = int(GAME["pot"])
    unpaid = total_prizes_defined_unpaid()
    pot_remaining = pot - unpaid
    if pot_remaining < 0:
        pot_remaining = 0

    pot_block = (
        {
            "pot": pot,
            "prizes_defined_total": unpaid,
            "prizes_paid_total": total_prizes_paid(),
            "pot_remaining": int(pot_remaining),
        }
        if role == "cassiere"
        else {"pot": pot}
    )

    return {
        "title": APP_TITLE,
        "players": _players_public_for(viewer_id),
        "my_history": _my_history_public_for(viewer_id),
        "prizes": prizes_out,
        "pot": pot_block,
        "log": GAME["log"],
    }

# ==========================
# WS MANAGER
# ==========================
class WSManager:
    def __init__(self) -> None:
        self.clients: List[WebSocket] = []
        self.ws_user: Dict[WebSocket, Optional[str]] = {}

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        self.clients.append(ws)
        self.ws_user[ws] = None

    def disconnect(self, ws: WebSocket) -> None:
        if ws in self.clients:
            self.clients.remove(ws)
        self.ws_user.pop(ws, None)

    def set_user(self, ws: WebSocket, user_id: Optional[str]) -> None:
        self.ws_user[ws] = user_id if (user_id in GAME["players"]) else None

    async def send_state(self, ws: WebSocket) -> None:
        uid = self.ws_user.get(ws)
        await ws.send_text(json.dumps(public_state_for(uid)))

    async def broadcast(self) -> None:
        dead: List[WebSocket] = []
        for ws in self.clients:
            try:
                await self.send_state(ws)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)

ws_manager = WSManager()

# ==========================
# FASTAPI
# ==========================
app = FastAPI(title=APP_TITLE)

# ==========================
# HTML UI
# ==========================
HTML = """<!doctype html>
<html lang="it">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width,initial-scale=1" />
  <title>BingoCoin</title>
  <style>
    :root { font-family: system-ui, -apple-system, Segoe UI, Roboto, Arial, sans-serif; }
    body { margin:0; background:#f6f7f9; }
    header { position:sticky; top:0; background:#111; color:#fff; padding:12px; z-index:10; }
    main { padding:12px; max-width:980px; margin:0 auto; }
    .card { background:#fff; border:1px solid #e5e7eb; border-radius:12px; padding:12px; margin-bottom:12px; }
    h2 { margin:0 0 10px 0; font-size:16px; }
    .small { font-size:12px; color:#6b7280; }
    .row { display:flex; justify-content:space-between; gap:10px; align-items:center; flex-wrap:wrap; }
    input, select, button { font-size:16px; width:100%; padding:12px; border-radius:10px; }
    input, select { border:1px solid #d1d5db; background:#fff; }
    button { border:0; background:#111; color:#fff; font-weight:800; }
    button.secondary { background:#374151; }
    button.danger { background:#991b1b; }
    .grid2 { display:grid; grid-template-columns: 1fr; gap:10px; }
    @media (min-width:560px){ .grid2{ grid-template-columns: 1fr 1fr; } }
    .list { display:grid; gap:8px; }
    .pill { border:1px solid #e5e7eb; border-radius:12px; padding:10px; display:flex; justify-content:space-between; gap:10px; align-items:center; }
    .hide { display:none; }
    .badge { padding:6px 10px; border-radius:999px; background:#111; color:#fff; font-weight:800; font-size:13px; }
    .mono { font-variant-numeric: tabular-nums; }
    .sectionTitle { font-size:13px; font-weight:900; margin:0 0 10px 0; }
  </style>
</head>
<body>
<header>
  <div class="row">
    <div>
      <div style="font-weight:900;">BingoCoin</div>
      <div id="status" class="small">Connessione…</div>
    </div>
    <div class="badge" id="roleBadge">—</div>
  </div>
</header>

<main>
  <div class="card" id="joinCard">
    <h2>Entra</h2>
    <div class="grid2">
      <div>
        <div class="small">Nome</div>
        <input id="name" placeholder="Es. Lucia" autocomplete="off" />
      </div>
      <div>
        <div class="small">Ruolo</div>
        <select id="role">
          <option value="giocatore">Giocatore</option>
          <option value="cassiere">Cassiere</option>
        </select>
      </div>
    </div>
    <div id="pinWrap" class="hide" style="margin-top:10px;">
      <div class="small">PIN cassiere</div>
      <input id="pin" placeholder="PIN" inputmode="numeric" />
    </div>
    <div style="margin-top:10px;">
      <button onclick="doJoin()">Entra</button>
    </div>
    <div class="small" style="margin-top:8px;">Il cassiere può rientrare col PIN.</div>
  </div>

  <div class="card hide" id="meCard">
    <div class="row">
      <div>
        <div class="small">Utente</div>
        <div style="font-weight:900;" id="meLine">—</div>
      </div>
      <div style="text-align:right;">
        <div class="small">Saldo</div>
        <div class="mono" style="font-weight:900; font-size:20px;" id="meSaldo">0 b€</div>
      </div>
    </div>
  </div>

  <!-- PLAYER -->
  <div class="card hide" id="playCard">
    <h2>Gioca</h2>
    <div class="grid2" style="margin-top:10px;">
      <div>
        <div class="small">Importo (b€)</div>
        <input id="playAmount" placeholder="Es. 10" inputmode="numeric" />
      </div>
      <div style="display:flex; align-items:end;">
        <button class="secondary" onclick="playerPlay()">Gioca</button>
      </div>
    </div>
  </div>

  <div class="card hide" id="playerPrizesCard">
    <h2>Premi disponibili</h2>
    <div class="small">Solo premi non ancora assegnati.</div>
    <div id="playerPrizesList" class="list" style="margin-top:10px;">—</div>
  </div>

  <div class="card hide" id="historyCard">
    <h2>Movimenti</h2>
    <div class="small" style="margin-bottom:8px;">Solo il tuo saldo e i tuoi movimenti.</div>
    <div id="historyList" class="list">—</div>
  </div>

  <!-- CASHIER -->
  <div class="card hide" id="cashierCard">
    <h2>Console Cassiere</h2>

    <div class="card" style="margin-top:10px;">
      <div class="sectionTitle">Giocatori</div>

      <h2>Saldi</h2>
      <div class="small" style="margin-bottom:8px;">Il cassiere vede tutti i saldi (incluso il proprio).</div>
      <div id="balancesList" class="list">—</div>

      <div style="height:12px;"></div>

      <h2>Accredita soldi</h2>
      <div class="small">Effetto: giocatore +importo, cassiere -importo.</div>
      <div class="grid2" style="margin-top:10px;">
        <div>
          <div class="small">Giocatore</div>
          <select id="creditPlayer"></select>
        </div>
        <div>
          <div class="small">Importo (b€)</div>
          <input id="creditAmount" placeholder="Es. 50" inputmode="numeric" />
        </div>
      </div>
      <div style="margin-top:10px;">
        <button class="secondary" onclick="cashierCredit()">Accredita</button>
      </div>
    </div>

    <div class="card">
      <div class="sectionTitle">Premi</div>

      <h2>Premi / Montepremi</h2>
      <div class="small" id="potBox">Montepremi: 0 b€ · Premi (non pagati) creati: 0 b€ · Residuo: 0 b€</div>

      <div class="grid2" style="margin-top:10px;">
        <div>
          <div class="small">Nome premio</div>
          <input id="prizeName" placeholder="Es. Tombola" />
        </div>
        <div>
          <div class="small">Importo (b€)</div>
          <input id="prizeAmount" placeholder="Es. 80" inputmode="numeric" />
        </div>
      </div>
      <div style="margin-top:10px;">
        <button class="secondary" onclick="cashierAddPrize()">Crea premio</button>
      </div>

      <div style="height:12px;"></div>

      <h2>Assegna premi</h2>
      <div class="small">Pagamento dal montepremi: vincitore +importo, montepremi -importo.</div>
      <div id="assignWrap" class="list" style="margin-top:10px;"></div>
    </div>

    <div class="grid2">
      <button class="danger" onclick="cashierReset()">Reset (invalida giocatori)</button>
      <button class="secondary" onclick="cashierExit()">Esci cassiere</button>
    </div>

    <div style="height:12px;"></div>

    <h2>Log</h2>
    <div id="logList" class="list small">—</div>
  </div>
</main>

<script>
let ws = null;
let snapshot = null;
let me = { id: null, name: null, role: null };
const el = (id) => document.getElementById(id);

function fmtBE(n){
  if(n === null || typeof n === "undefined") return "—";
  return `${n} b€`;
}

function clearSession(){
  localStorage.removeItem("bingocoin_me");
  me = { id: null, name: null, role: null };
}
function saveSession(){
  if(me.id && me.name && me.role){
    localStorage.setItem("bingocoin_me", JSON.stringify(me));
  }
}
function loadSession(){
  try{
    const raw = localStorage.getItem("bingocoin_me");
    if(!raw) return;
    const obj = JSON.parse(raw);
    if(obj && obj.id && obj.role && obj.name){
      me = obj;
    }
  }catch(e){}
}

el("role").addEventListener("change", () => {
  el("pinWrap").classList.toggle("hide", el("role").value !== "cassiere");
});

function wsUrl(){
  const proto = (location.protocol === "https:") ? "wss" : "ws";
  return `${proto}://${location.host}/ws`;
}

function connectWS(){
  ws = new WebSocket(wsUrl());
  ws.onopen = () => {
    el("status").textContent = "Connesso";
    if(me.id){
      ws.send(JSON.stringify({auth: me.id}));
    }
  };
  ws.onclose = () => {
    el("status").textContent = "Disconnesso (ritento...)";
    setTimeout(connectWS, 1200);
  };
  ws.onmessage = (ev) => {
    snapshot = JSON.parse(ev.data);
    render();
  };
}

async function doJoin(){
  const payload = {
    name: (el("name").value || "").trim(),
    role: el("role").value,
    pin:  (el("pin").value || "").trim()
  };
  const res = await fetch("/join", {
    method:"POST",
    headers: {"Content-Type":"application/json"},
    body: JSON.stringify(payload)
  });
  if(!res.ok){
    alert(await res.text());
    return;
  }
  const data = await res.json();
  me = { id: data.id, name: data.name, role: data.role };
  saveSession();
  if(ws && ws.readyState === 1){
    ws.send(JSON.stringify({auth: me.id}));
  }
  render();
}

function render(){
  if(!snapshot) return;

  // se la sessione è stata invalidata lato server
  if(me.id){
    const exists = (snapshot.players || []).some(p => p.id === me.id);
    if(!exists){
      clearSession();
    }
  }

  if(!me.id){
    el("roleBadge").textContent = "NON AUTENTICATO";
    el("joinCard").classList.remove("hide");
    el("meCard").classList.add("hide");
    el("playCard").classList.add("hide");
    el("playerPrizesCard").classList.add("hide");
    el("historyCard").classList.add("hide");
    el("cashierCard").classList.add("hide");
    return;
  }

  el("joinCard").classList.add("hide");
  el("meCard").classList.remove("hide");
  el("meLine").textContent = me.role === "cassiere" ? `${me.name} (cassiere)` : me.name;
  el("roleBadge").textContent = me.role.toUpperCase();

  const meEntry = (snapshot.players || []).find(p => p.id === me.id);
  if(meEntry && meEntry.saldo !== null && typeof meEntry.saldo !== "undefined"){
    el("meSaldo").textContent = fmtBE(meEntry.saldo);
  } else {
    el("meSaldo").textContent = "—";
  }

  if(me.role === "giocatore"){
    el("cashierCard").classList.add("hide");
    el("playCard").classList.remove("hide");
    el("playerPrizesCard").classList.remove("hide");
    el("historyCard").classList.remove("hide");

    // premi non assegnati
    const prizes = snapshot.prizes || [];
    const pw = el("playerPrizesList");
    pw.innerHTML = "";
    if(prizes.length === 0){
      pw.innerHTML = `<div class="pill"><div>Nessun premio disponibile.</div></div>`;
    } else {
      prizes.forEach(pr => {
        const div = document.createElement("div");
        div.className = "pill";
        div.innerHTML = `<div>${pr.name}</div><div class="mono" style="font-weight:900;">${fmtBE(pr.amount)}</div>`;
        pw.appendChild(div);
      });
    }

    const h = snapshot.my_history || [];
    const wrap = el("historyList");
    wrap.innerHTML = "";
    if(h.length === 0){
      wrap.innerHTML = `<div class="pill"><div>Nessun movimento.</div></div>`;
    } else {
      h.forEach(x => {
        const sign = x.delta >= 0 ? "+" : "";
        const div = document.createElement("div");
        div.className = "pill";
        div.innerHTML = `<div>${x.note || "Movimento"}</div><div class="mono" style="font-weight:900;">${sign}${x.delta} b€</div>`;
        wrap.appendChild(div);
      });
    }
    return;
  }

  // CASSIERE
  el("playCard").classList.add("hide");
  el("playerPrizesCard").classList.add("hide");
  el("historyCard").classList.add("hide");
  el("cashierCard").classList.remove("hide");

  const players = (snapshot.players || []);
  const onlyPlayers = players.filter(p => p.role === "giocatore");

  // mappa id->nome per mostrare vincitore
  const idToName = {};
  players.forEach(p => { idToName[p.id] = p.name; });

  // saldi
  const bal = el("balancesList");
  bal.innerHTML = "";
  if(players.length === 0){
    bal.innerHTML = `<div class="pill"><div>Nessun utente.</div></div>`;
  } else {
    players.forEach(p => {
      const div = document.createElement("div");
      div.className = "pill";
      div.innerHTML = `<div>${p.name} <span class="small">${p.role}</span></div><div class="mono" style="font-weight:900;">${fmtBE(p.saldo)}</div>`;
      bal.appendChild(div);
    });
  }

  // dropdown accrediti
  const sel = el("creditPlayer");
  sel.innerHTML = "";
  onlyPlayers.forEach(p => {
    const opt = document.createElement("option");
    opt.value = p.id;
    opt.textContent = p.name;
    sel.appendChild(opt);
  });

  // pot box
  const potVal = snapshot.pot?.pot ?? 0;
  const unpaidTotal = snapshot.pot?.prizes_defined_total ?? 0;
  const remaining = snapshot.pot?.pot_remaining ?? 0;
  el("potBox").textContent = `Montepremi: ${fmtBE(potVal)} · Premi (non pagati) creati: ${fmtBE(unpaidTotal)} · Residuo: ${fmtBE(remaining)}`;

  // premi + assegnazione
  const assign = el("assignWrap");
  assign.innerHTML = "";
  const prizes = snapshot.prizes || [];
  if(prizes.length === 0){
    assign.innerHTML = `<div class="pill"><div>Nessun premio definito.</div></div>`;
  } else {
    prizes.forEach((pr, idx) => {
      const paid = !!pr.paid;

      const div = document.createElement("div");
      div.className = "pill";

      if(paid){
        const winnerName = idToName[pr.winner_id] || "—";
        div.innerHTML = `
          <div style="flex:1;">
            <div style="font-weight:900;">${pr.name} — <span class="mono">${fmtBE(pr.amount)}</span></div>
            <div class="small">Assegnato a: <b>${winnerName}</b></div>
          </div>
          <div class="mono" style="font-weight:900;">PAGATO</div>
        `;
      } else {
        const options = [
          `<option value="" selected>Seleziona vincitore…</option>`,
          ...onlyPlayers.map(p => `<option value="${p.id}">${p.name}</option>`)
        ].join("");

        div.innerHTML = `
          <div style="flex:1;">
            <div style="font-weight:900;">${pr.name} — <span class="mono">${fmtBE(pr.amount)}</span></div>
            <div class="small">Stato: DA PAGARE</div>
          </div>
          <div style="min-width:220px;">
            <select>${options}</select>
          </div>
        `;

        const s = div.querySelector("select");
        s.addEventListener("change", async (e) => {
          const winner_id = e.target.value || null;
          if(!winner_id) return;
          await postAuthed("/cashier/assign_prize", {index: idx, winner_id});
        });
      }

      assign.appendChild(div);
    });
  }

  // log
  const logWrap = el("logList");
  logWrap.innerHTML = "";
  (snapshot.log || []).forEach(e => {
    const dt = new Date(e.ts * 1000);
    const div = document.createElement("div");
    div.className = "pill";
    div.innerHTML = `<div>${e.msg}</div><div class="small">${dt.toLocaleTimeString()}</div>`;
    logWrap.appendChild(div);
  });
}

async function postAuthed(path, body){
  const res = await fetch(path, {
    method: "POST",
    headers: {"Content-Type":"application/json", "X-User": me.id},
    body: JSON.stringify(body || {})
  });
  if(!res.ok){
    alert(await res.text());
    return null;
  }
  try { return await res.json(); } catch(e){ return {}; }
}

// player
async function playerPlay(){
  const amt = parseInt((el("playAmount").value || "").trim(), 10);
  if(!Number.isFinite(amt) || amt <= 0){ alert("Importo non valido"); return; }
  await postAuthed("/player/play", {amount: amt});
  el("playAmount").value = "";
}

// cashier
async function cashierCredit(){
  const pid = el("creditPlayer").value;
  const amt = parseInt((el("creditAmount").value || "").trim(), 10);
  if(!pid){ alert("Seleziona un giocatore"); return; }
  if(!Number.isFinite(amt) || amt <= 0){ alert("Importo non valido"); return; }
  await postAuthed("/cashier/credit", {player_id: pid, amount: amt});
  el("creditAmount").value = "";
}

async function cashierAddPrize(){
  const name = (el("prizeName").value || "").trim();
  const amt = parseInt((el("prizeAmount").value || "").trim(), 10);
  if(!name){ alert("Nome premio mancante"); return; }
  if(!Number.isFinite(amt) || amt <= 0){ alert("Importo non valido"); return; }
  await postAuthed("/cashier/add_prize", {name, amount: amt});
  el("prizeName").value = "";
  el("prizeAmount").value = "";
}

async function cashierReset(){
  if(!confirm("Reset: invalida i giocatori e azzera premi/montepremi. Continuare?")) return;
  await postAuthed("/cashier/reset_players", {});
}

async function cashierExit(){
  if(!confirm("Uscire? La sessione del cassiere verrà invalidata.")) return;
  await postAuthed("/cashier/logout", {});
  clearSession();
}

// boot
loadSession();
connectWS();

// keepalive
setInterval(() => {
  try { if(ws && ws.readyState === 1) ws.send("ping"); } catch(e) {}
}, 25000);
</script>
</body>
</html>
"""

@app.get("/", response_class=HTMLResponse)
async def index():
    return HTMLResponse(HTML)

@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws_manager.connect(ws)
    await ws_manager.send_state(ws)
    try:
        while True:
            msg = await ws.receive_text()
            try:
                data = json.loads(msg)
                if isinstance(data, dict) and "auth" in data:
                    ws_manager.set_user(ws, str(data.get("auth", "")).strip())
                    await ws_manager.send_state(ws)
            except Exception:
                pass
    except WebSocketDisconnect:
        ws_manager.disconnect(ws)
    except Exception:
        ws_manager.disconnect(ws)

# ==========================
# JOIN
# ==========================
@app.post("/join")
async def join(data: Dict[str, Any] = Body(...)):
    name = str(data.get("name", "")).strip()
    role = str(data.get("role", "giocatore")).strip()
    pin = str(data.get("pin", "")).strip()

    if not name:
        return HTMLResponse("Nome mancante", status_code=400)
    if role not in ("giocatore", "cassiere"):
        return HTMLResponse("Ruolo non valido", status_code=400)

    if role == "cassiere":
        if pin != CASHIER_PIN:
            return HTMLResponse("PIN cassiere errato", status_code=403)
        existing = get_cashier_id()
        if existing:
            GAME["players"][existing]["name"] = name
            add_log(f"Cassiere rientrato: {name}")
            await ws_manager.broadcast()
            return {"id": existing, "name": name, "role": "cassiere"}

    uid = secrets.token_hex(8)
    GAME["players"][uid] = {"name": name, "role": role, "saldo": 0}
    ensure_ledger(uid)
    add_log(f"{name} è entrato ({role})")
    await ws_manager.broadcast()
    return {"id": uid, "name": name, "role": role}

# ==========================
# PLAYER
# ==========================
@app.post("/player/play")
async def player_play(req: Request, data: Dict[str, Any] = Body(...)):
    """
    Gioca: versa nel montepremi.
      - giocatore -X
      - pot +X
    vincolo: X <= saldo giocatore
    """
    try:
        player_id = require_player(req)
    except PermissionError as e:
        return HTMLResponse(str(e), status_code=403)

    try:
        amount = int(data.get("amount"))
    except Exception:
        return HTMLResponse("Importo non valido", status_code=400)
    if amount <= 0:
        return HTMLResponse("Importo deve essere > 0", status_code=400)

    saldo = int(GAME["players"][player_id].get("saldo", 0))
    if amount > saldo:
        return HTMLResponse("Importo superiore al saldo disponibile", status_code=400)

    GAME["players"][player_id]["saldo"] = saldo - amount
    add_ledger_delta(player_id, -amount, "Gioca")

    GAME["pot"] = int(GAME["pot"]) + amount
    add_log(f"Montepremi +{amount} (da {GAME['players'][player_id]['name']})")
    await ws_manager.broadcast()
    return {"ok": True, "pot": int(GAME["pot"])}

# ==========================
# CASHIER
# ==========================
@app.post("/cashier/credit")
async def cashier_credit(req: Request, data: Dict[str, Any] = Body(...)):
    """
    Accredito:
      - giocatore +X
      - cassiere -X
    """
    try:
        cashier_id = require_cashier(req)
    except PermissionError as e:
        return HTMLResponse(str(e), status_code=403)

    player_id = str(data.get("player_id", "")).strip()
    if not player_id or player_id not in GAME["players"]:
        return HTMLResponse("Giocatore non valido", status_code=400)
    if GAME["players"][player_id]["role"] != "giocatore":
        return HTMLResponse("Seleziona un giocatore", status_code=400)

    try:
        amount = int(data.get("amount"))
    except Exception:
        return HTMLResponse("Importo non valido", status_code=400)
    if amount <= 0:
        return HTMLResponse("Importo deve essere > 0", status_code=400)

    GAME["players"][player_id]["saldo"] += amount
    add_ledger_delta(player_id, amount, "Accredito")

    GAME["players"][cashier_id]["saldo"] -= amount
    add_ledger_delta(cashier_id, -amount, f"Accredito a {GAME['players'][player_id]['name']}")

    add_log(f"Accredito: {GAME['players'][player_id]['name']} +{amount} · Cassiere -{amount}")
    await ws_manager.broadcast()
    return {"ok": True}

@app.post("/cashier/add_prize")
async def cashier_add_prize(req: Request, data: Dict[str, Any] = Body(...)):
    """
    Crea premio vincolato al montepremi ATTUALE:
      somma(premi NON pagati) + amount <= pot
    """
    try:
        require_cashier(req)
    except PermissionError as e:
        return HTMLResponse(str(e), status_code=403)

    name = str(data.get("name", "")).strip()
    if not name:
        return HTMLResponse("Nome premio mancante", status_code=400)

    try:
        amount = int(data.get("amount"))
    except Exception:
        return HTMLResponse("Importo non valido", status_code=400)
    if amount <= 0:
        return HTMLResponse("Importo deve essere > 0", status_code=400)

    defined_unpaid = total_prizes_defined_unpaid()
    pot = int(GAME["pot"])
    if defined_unpaid + amount > pot:
        return HTMLResponse(
            f"Premi (non pagati) eccedono il montepremi: {defined_unpaid}+{amount} > {pot}",
            status_code=400,
        )

    GAME["prizes"].append({
        "name": name,
        "amount": amount,
        "winner_id": None,
        "paid": False,
        "ts": now_ts(),
    })
    add_log(f"Premio definito: {name} = {amount}")
    await ws_manager.broadcast()
    return {"ok": True}

@app.post("/cashier/assign_prize")
async def cashier_assign_prize(req: Request, data: Dict[str, Any] = Body(...)):
    """
    Assegna e paga dal montepremi:
      - vincitore +amount
      - pot -amount
    """
    try:
        require_cashier(req)
    except PermissionError as e:
        return HTMLResponse(str(e), status_code=403)

    try:
        idx = int(data.get("index"))
    except Exception:
        return HTMLResponse("Indice premio non valido", status_code=400)
    if idx < 0 or idx >= len(GAME["prizes"]):
        return HTMLResponse("Premio non trovato", status_code=404)

    prize = GAME["prizes"][idx]
    if prize.get("paid", False):
        return HTMLResponse("Premio già pagato", status_code=400)

    winner_id = str(data.get("winner_id", "")).strip()
    if not winner_id or winner_id not in GAME["players"]:
        return HTMLResponse("Vincitore non valido", status_code=400)
    if GAME["players"][winner_id]["role"] != "giocatore":
        return HTMLResponse("Il vincitore deve essere un giocatore", status_code=400)

    amount = int(prize["amount"])
    pname = str(prize["name"])

    if int(GAME["pot"]) < amount:
        return HTMLResponse("Montepremi insufficiente per pagare questo premio", status_code=400)

    GAME["players"][winner_id]["saldo"] += amount
    add_ledger_delta(winner_id, amount, f"Premio: {pname}")

    GAME["pot"] = int(GAME["pot"]) - amount

    prize["winner_id"] = winner_id
    prize["paid"] = True

    add_log(f"Premio assegnato: {pname} -> {GAME['players'][winner_id]['name']} ({amount})")
    await ws_manager.broadcast()
    return {"ok": True}

@app.post("/cashier/reset_players")
async def cashier_reset_players(req: Request):
    """
    Reset:
      - invalida sessioni giocatori (li rimuove)
      - azzera premi e montepremi
      - azzera saldo cassiere
    """
    try:
        cashier_id = require_cashier(req)
    except PermissionError as e:
        return HTMLResponse(str(e), status_code=403)

    to_remove = [uid for uid, p in GAME["players"].items() if p["role"] == "giocatore"]
    for uid in to_remove:
        GAME["players"].pop(uid, None)
        GAME["ledger"].pop(uid, None)

    GAME["prizes"] = []
    GAME["pot"] = 0

    if cashier_id in GAME["players"]:
        GAME["players"][cashier_id]["saldo"] = 0
        GAME["ledger"][cashier_id] = []

    add_log("Reset: giocatori invalidati, premi e montepremi azzerati, saldo cassiere azzerato.")
    await ws_manager.broadcast()
    return {"ok": True}

@app.post("/cashier/logout")
async def cashier_logout(req: Request):
    """
    Invalida sessione cassiere.
    """
    try:
        cashier_id = require_cashier(req)
    except PermissionError as e:
        return HTMLResponse(str(e), status_code=403)

    name = GAME["players"][cashier_id]["name"]
    GAME["players"].pop(cashier_id, None)
    GAME["ledger"].pop(cashier_id, None)

    add_log(f"Cassiere uscito: {name} (sessione invalidata)")
    await ws_manager.broadcast()
    return {"ok": True}

# ==========================
# START
# ==========================
#if __name__ == "__main__":
#    uvicorn.run(app, host="0.0.0.0", port=PORT)
if __name__ == "__main__": 
	port = int(os.environ.get("PORT", "9090"))
	uvicorn.run(app, host="0.0.0.0", port=port)
