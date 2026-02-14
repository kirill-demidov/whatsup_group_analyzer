/**
 * Мост на Baileys: полная история через messaging-history.set и messages.upsert,
 * тот же REST API, что и bridge (whatsapp-web.js): status, chats, messages, sync, logout.
 * Запуск: BACKEND_URL=http://localhost:8080 WEB_PORT=3080 node index.js
 */

import makeWASocket, {
  useMultiFileAuthState,
  getChatId,
  fetchLatestBaileysVersion,
  DEFAULT_CONNECTION_CONFIG,
  DisconnectReason,
  Browsers,
} from "@whiskeysockets/baileys";
import express from "express";
import qrcode from "qrcode";
import qrcodeTerminal from "qrcode-terminal";
import path from "path";
import fs from "fs";
import { fileURLToPath } from "url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const DATA_BASE_DIR = path.join(__dirname, "data");
const toDataURL = (qr) => (qrcode.toDataURL ? qrcode.toDataURL(qr) : Promise.resolve(""));

// --- Кольцевой буфер логов для вывода в UI (последние 500 строк) ---
const LOG_MAX = 500;
const logBuffer = [];
function pushLog(level, ...args) {
  const msg = args.map((a) => (typeof a === "object" ? JSON.stringify(a) : String(a))).join(" ");
  const line = new Date().toISOString() + " [" + level + "] " + msg;
  logBuffer.push(line);
  if (logBuffer.length > LOG_MAX) logBuffer.shift();
}
const origLog = console.log;
const origError = console.error;
console.log = (...args) => { origLog.apply(console, args); pushLog("INFO", ...args); };
console.error = (...args) => { origError.apply(console, args); pushLog("ERROR", ...args); };
pushLog("INFO", "Мост Baileys: запуск, буфер логов готов.");

const BACKEND_URL = process.env.BACKEND_URL || "http://localhost:8080";
const TARGET_GROUP_ID = process.env.GROUP_ID || null;
const WEB_PORT = parseInt(process.env.WEB_PORT || "3080", 10);

// --- Store: чаты и сообщения по chatId (совместимый формат с текущим API) ---
/** @type {Map<string, { id: string, name: string, isGroup: boolean, type: string, lastActive: number }>} */
const chatStore = new Map();
/** @type {Map<string, Array<{ id: string, from: string, from_name?: string, from_id?: string, body: string, timestamp: number, date: string | null }>>} */
const messagesByChat = new Map();
/** Сообщения по key (remoteJid_id) для getMessage */
const messageById = new Map();
/** Хранилище имён контактов: jid → name */
const contactNames = new Map();

// --- Persistence: сохранение данных на диск ---
let dirty = false;
let currentAccountJid = null;

function sanitizeJid(jid) {
  return (jid || "unknown").replace(/[^a-zA-Z0-9@._-]/g, "_");
}

function getAccountDataDir() {
  const jid = currentAccountJid || "unknown";
  return path.join(DATA_BASE_DIR, sanitizeJid(jid));
}

function saveToDisk() {
  if (!currentAccountJid) return;
  const dir = getAccountDataDir();
  fs.mkdirSync(dir, { recursive: true });

  const chatsData = Object.fromEntries(chatStore);
  const messagesData = Object.fromEntries(messagesByChat);
  const contactsData = Object.fromEntries(contactNames);

  for (const [name, data] of [["chats.json", chatsData], ["messages.json", messagesData], ["contacts.json", contactsData]]) {
    const target = path.join(dir, name);
    const tmp = target + ".tmp";
    fs.writeFileSync(tmp, JSON.stringify(data));
    fs.renameSync(tmp, target);
  }
  dirty = false;
  pushLog("INFO", `Данные сохранены на диск: ${chatStore.size} чатов, ${Array.from(messagesByChat.values()).reduce((s, a) => s + a.length, 0)} сообщений, ${contactNames.size} контактов`);
}

function loadFromDisk(jid) {
  const dir = path.join(DATA_BASE_DIR, sanitizeJid(jid));
  if (!fs.existsSync(dir)) return;

  try {
    const chatsFile = path.join(dir, "chats.json");
    if (fs.existsSync(chatsFile)) {
      const data = JSON.parse(fs.readFileSync(chatsFile, "utf-8"));
      for (const [k, v] of Object.entries(data)) {
        if (!chatStore.has(k)) chatStore.set(k, v);
      }
    }
  } catch (e) { pushLog("WARN", "Ошибка загрузки chats.json: " + e.message); }

  try {
    const msgsFile = path.join(dir, "messages.json");
    if (fs.existsSync(msgsFile)) {
      const data = JSON.parse(fs.readFileSync(msgsFile, "utf-8"));
      for (const [k, v] of Object.entries(data)) {
        if (!messagesByChat.has(k)) messagesByChat.set(k, v);
        else {
          const existing = messagesByChat.get(k);
          const existingIds = new Set(existing.map(m => m.id));
          for (const msg of v) {
            if (!existingIds.has(msg.id)) existing.push(msg);
          }
          existing.sort((a, b) => a.timestamp - b.timestamp);
        }
      }
    }
  } catch (e) { pushLog("WARN", "Ошибка загрузки messages.json: " + e.message); }

  try {
    const contactsFile = path.join(dir, "contacts.json");
    if (fs.existsSync(contactsFile)) {
      const data = JSON.parse(fs.readFileSync(contactsFile, "utf-8"));
      for (const [k, v] of Object.entries(data)) {
        if (!contactNames.has(k)) contactNames.set(k, v);
      }
    }
  } catch (e) { pushLog("WARN", "Ошибка загрузки contacts.json: " + e.message); }

  const totalMsg = Array.from(messagesByChat.values()).reduce((s, a) => s + a.length, 0);
  pushLog("INFO", `Loaded ${chatStore.size} chats, ${totalMsg} messages, ${contactNames.size} contacts from disk`);
}

// Dirty-флаг: сохраняем каждые 30 секунд если были изменения
setInterval(() => {
  if (dirty) saveToDisk();
}, 30_000);

// Graceful shutdown
function gracefulShutdown(signal) {
  pushLog("INFO", `${signal} получен, сохраняю данные…`);
  if (dirty) saveToDisk();
  process.exit(0);
}
process.on("SIGINT", () => gracefulShutdown("SIGINT"));
process.on("SIGTERM", () => gracefulShutdown("SIGTERM"));

/** Извлекает номер телефона из JID: 79161234567@s.whatsapp.net → +79161234567 */
function phoneFromJid(jid) {
  if (!jid) return undefined;
  const match = jid.match(/^(\d+)@/);
  return match ? "+" + match[1] : undefined;
}

/** Резолвит имя для JID: контакты → pushName → номер телефона */
function resolveContactName(jid, pushName) {
  return contactNames.get(jid) || pushName || phoneFromJid(jid) || undefined;
}

let sock = null;
let connected = false;
let lastQrDataUrl = null;

function chatType(jid, isGroup) {
  if (isGroup) return "group";
  if (jid.endsWith("@newsletter") || jid.includes("newsletter")) return "channel";
  return "direct";
}

function normalizeMessage(msg) {
  const key = msg.key;
  const chatId = getChatId(key);
  const id = key.id || `${chatId}_${msg.messageTimestamp || 0}`;
  const from = key.remoteJid || chatId;
  // Для групповых сообщений отправитель — key.participant, не remoteJid (JID чата)
  const fromId = key.participant || key.remoteJid || chatId;
  const fromName = resolveContactName(fromId, msg.pushName);
  let body = "";
  const m = msg.message;
  if (m) {
    if (m.conversation) body = m.conversation;
    else if (m.extendedTextMessage?.text) body = m.extendedTextMessage.text;
    else if (m.imageMessage?.caption) body = m.imageMessage.caption;
    else if (m.videoMessage?.caption) body = m.videoMessage.caption;
    else if (m.documentMessage?.caption) body = m.documentMessage.caption;
  }
  const timestamp = msg.messageTimestamp
    ? (typeof msg.messageTimestamp === "number"
        ? msg.messageTimestamp
        : Number(msg.messageTimestamp?.low ?? 0) || 0)
    : 0;
  const keyId = key.id || "";
  return {
    id: keyId ? `${from}_${keyId}` : `${from}_${timestamp}_${Date.now()}`,
    keyId, // сырой id для fetchMessageHistory
    from,
    from_id: fromId,
    from_name: fromName,
    body: (body || "").trim(),
    timestamp,
    date: timestamp ? new Date(timestamp * 1000).toISOString() : null,
  };
}

function messageKey(msg) {
  const chatId = getChatId(msg.key);
  const id = msg.key.id || "";
  return `${chatId}_${id}`;
}

function addMessageToStore(msg) {
  const norm = normalizeMessage(msg);
  const chatId = getChatId(msg.key);
  const key = messageKey(msg);
  messageById.set(key, msg);

  if (!messagesByChat.has(chatId)) messagesByChat.set(chatId, []);
  const list = messagesByChat.get(chatId);
  const existing = list.find((m) => m.id === norm.id);
  if (!existing) {
    list.push(norm);
    list.sort((a, b) => a.timestamp - b.timestamp);
    const last = list[list.length - 1];
    const chat = chatStore.get(chatId);
    if (chat) chat.lastActive = last.timestamp;
    dirty = true;
  }
}

function ensureChat(jid, name, isGroup, lastActiveOpt) {
  if (!chatStore.has(jid)) {
    chatStore.set(jid, {
      id: jid,
      name: name || "—",
      isGroup: !!isGroup,
      type: chatType(jid, !!isGroup),
      lastActive: lastActiveOpt != null ? Number(lastActiveOpt) : 0,
    });
    dirty = true;
  } else {
    const c = chatStore.get(jid);
    if (name && c.name !== name) { c.name = name; dirty = true; }
    if (lastActiveOpt != null) {
        const ts = Number(lastActiveOpt);
        if (ts > 0 && (c.lastActive == null || c.lastActive < ts)) { c.lastActive = ts; dirty = true; }
    }
  }
}

async function startSock() {
  const authDir = path.join(__dirname, "auth_baileys");
  const hadAuth = fs.existsSync(authDir) && fs.readdirSync(authDir).length > 0;
  const { state, saveCreds } = await useMultiFileAuthState(authDir);
  if (!hadAuth) {
    pushLog("INFO", "Сессии нет — ожидаю QR от WhatsApp (подожди 10–20 сек, затем обнови /app/qr.html)");
    console.log("Сессии нет — скоро появится QR в терминале и на /app/qr.html");
  }
  const { version } = await fetchLatestBaileysVersion();

  const noop = () => {};
  const logger = {
    level: "warn",
    trace: noop,
    debug: noop,
    info: (...a) => pushLog("INFO", ...a),
    warn: (...a) => pushLog("WARN", ...a),
    error: (...a) => pushLog("ERROR", ...a),
    fatal: (...a) => pushLog("FATAL", ...a),
    child: () => logger,
  };

  // BROWSER_TYPE=chrome — привязать как «Chrome» (иногда приходит история); desktop — как «Desktop» (часто пусто)
  const browserType = (process.env.BROWSER_TYPE || "desktop").toLowerCase();
  const browser = browserType === "chrome" ? Browsers.ubuntu("Chrome") : Browsers.macOS("Desktop");
  pushLog("INFO", "Тип устройства: " + (browserType === "chrome" ? "Chrome (Ubuntu)" : "Desktop (macOS)") + ". Сменить: BROWSER_TYPE=chrome или BROWSER_TYPE=desktop, затем отвязать устройство в WhatsApp и отсканировать QR заново.");

  const socket = makeWASocket({
    version,
    auth: {
      creds: state.creds,
      keys: state.keys,
    },
    waWebSocketUrl:
      process.env.SOCKET_URL ?? DEFAULT_CONNECTION_CONFIG.waWebSocketUrl,
    logger,
    browser,
    syncFullHistory: true,
    getMessage: async (key) => {
      const k = `${key.remoteJid}_${key.id}`;
      return messageById.get(k) || null;
    },
    shouldSyncHistoryMessage: () => true,
  });

  socket.ev.on("creds.update", saveCreds);

  socket.ev.on("connection.update", async (update) => {
    const { connection, lastDisconnect, qr } = update;
    const hasQr = !!qr;
    if (connection !== undefined || hasQr) {
      pushLog("INFO", "connection.update: connection=" + connection + " qr=" + (hasQr ? "да" : "нет"));
    }
    if (qr) {
      connected = false;
      try {
        lastQrDataUrl = await toDataURL(qr);
        pushLog("INFO", "QR сгенерирован для веб-интерфейса, длина " + (lastQrDataUrl ? lastQrDataUrl.length : 0) + " символов");
      } catch (e) {
        lastQrDataUrl = null;
        pushLog("ERROR", "Ошибка генерации QR: " + (e && e.message));
      }
      console.log(
        "Отсканируй QR в терминале или открой в браузере: http://localhost:8080/app/qr.html"
      );
      qrcodeTerminal.generate(qr, { small: true });
    }
    if (connection === "open") {
      connected = true;
      lastQrDataUrl = null;
      // Persistence: запомнить JID и загрузить данные с диска
      if (socket.user?.id) {
        currentAccountJid = socket.user.id;
        loadFromDisk(currentAccountJid);
      }
      console.log("Мост (Baileys) подключён. BACKEND_URL=", BACKEND_URL);
      pushLog("INFO", "Подключено. Ожидание истории от WhatsApp (syncFullHistory=true). Обычно 1–2 мин; в логах появится «messaging-history.set». Если сообщений так и будет 0 — для этого аккаунта/устройства WhatsApp может не отдавать историю.");
      if (TARGET_GROUP_ID) console.log("Фильтр по группе:", TARGET_GROUP_ID);
      // Сразу подгружаем список групп, чтобы они появились в интерфейсе
      if (socket.groupFetchAllParticipating) {
        socket.groupFetchAllParticipating().then((groups) => {
          if (groups && typeof groups === "object") {
            for (const [jid, meta] of Object.entries(groups)) {
              if (jid && meta) {
                const lastActive = meta.subjectTime > 0 ? meta.subjectTime : (meta.creation > 0 ? meta.creation : undefined);
                ensureChat(jid, meta.subject || meta.name || "—", true, lastActive);
              }
            }
            console.log("Загружено групп:", Object.keys(groups).length);
          }
        }).catch((e) => console.error("groupFetchAllParticipating:", e.message));
      }
    }
    if (connection === "close") {
      const err = lastDisconnect?.error;
      const statusCode = (err && err.output && err.output.statusCode) || 0;
      if (statusCode !== DisconnectReason.loggedOut) {
        console.log("Переподключение Baileys…");
        setTimeout(() => startSock(), 3000);
      } else {
        connected = false;
        sock = null;
      }
    }
  });

  socket.ev.on("messaging-history.set", ({ chats, messages, syncType }) => {
    const nChats = chats?.length ?? 0;
    const nMsg = messages?.length ?? 0;
    pushLog("INFO", "messaging-history.set: чатов=" + nChats + ", сообщений=" + nMsg + (syncType != null ? ", syncType=" + syncType : ""));
    if (nMsg === 0 && nChats > 0) {
      pushLog("WARN", "История от WhatsApp пришла без сообщений — для этого устройства сервер мог отдать только список чатов. Новые сообщения будут появляться в реальном времени (messages.upsert).");
    }
    if (chats && Array.isArray(chats)) {
      for (const c of chats) {
        const jid = c.id || c.jid;
        if (!jid) continue;
        const name = c.name || "—";
        const lastActive = c.lastMessageRecvTimestamp ?? c.conversationTimestamp;
        ensureChat(jid, name, (c.id || jid).toString().endsWith("@g.us"), lastActive);
      }
    }
    if (messages && Array.isArray(messages)) {
      for (const msg of messages) {
        try {
          addMessageToStore(msg);
          const chatId = getChatId(msg.key);
          ensureChat(chatId, null, chatId.endsWith("@g.us"));
        } catch (e) {
          // skip broken messages
        }
      }
    }
    // Для чатов с lastActive=0 — обновить из последнего сообщения
    for (const [cid, chat] of chatStore) {
      if (chat.lastActive > 0) continue;
      const msgs = messagesByChat.get(cid);
      if (msgs && msgs.length > 0) {
        const lastTs = msgs[msgs.length - 1].timestamp;
        if (lastTs > 0) chat.lastActive = lastTs;
      }
    }
    const totalMsg = Array.from(messagesByChat.values()).reduce((s, arr) => s + arr.length, 0);
    pushLog("INFO", "History sync итого: чатов=" + chatStore.size + ", сообщений=" + totalMsg);
  });

  socket.ev.on("messages.upsert", async ({ messages, type }) => {
    if (messages?.length) pushLog("INFO", "messages.upsert: " + messages.length + " шт., type=" + type);
    for (const msg of messages || []) {
      try {
        addMessageToStore(msg);
        const chatId = getChatId(msg.key);
        ensureChat(chatId, null, chatId.endsWith("@g.us"));
      } catch (e) {}
    }
    // Webhook в бэкенд только для новых входящих (type === 'notify')
    if (type === "notify" && TARGET_GROUP_ID) {
      for (const msg of messages || []) {
        const chatId = getChatId(msg.key);
        if (chatId !== TARGET_GROUP_ID) continue;
        const norm = normalizeMessage(msg);
        const text = (norm.body || "").trim();
        if (text.length < 5) continue;
        try {
          const res = await fetch(`${BACKEND_URL}/webhook/bridge`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              text,
              chat_id: chatId,
              from_name: norm.from_name,
            }),
          });
          if (res.ok) {
            const data = await res.json().catch(() => ({}));
            console.log(
              "Бэкенд:",
              data.processed !== undefined
                ? `extracted=${data.extracted}, added=${data.added}`
                : data
            );
          }
        } catch (e) {
          console.error("Bridge webhook error:", e.message);
        }
      }
    }
  });

  socket.ev.on("chats.upsert", (chats) => {
    for (const c of chats || []) {
      const jid = c.id || c.jid;
      if (jid) {
        const lastActive = c.lastMessageRecvTimestamp ?? c.conversationTimestamp ?? c.lastMessageTimestamp;
        ensureChat(jid, c.name || c.subject, jid.endsWith("@g.us"), lastActive);
      }
    }
  });

  socket.ev.on("chats.update", (updates) => {
    for (const u of updates || []) {
      const jid = u.id;
      if (jid) {
        const lastActive = u.lastMessageRecvTimestamp ?? u.conversationTimestamp ?? u.lastMessageTimestamp;
        ensureChat(jid, u.name || u.subject, jid.endsWith("@g.us"), lastActive);
      }
    }
  });

  // Контакты: сохраняем имена для резолва отправителей
  socket.ev.on("contacts.upsert", (contacts) => {
    for (const c of contacts || []) {
      const jid = c.id;
      const name = c.name || c.notify || c.verifiedName;
      if (jid && name) { contactNames.set(jid, name); dirty = true; }
    }
    if (contacts?.length) pushLog("INFO", "contacts.upsert: " + contacts.length + " контактов");
  });

  socket.ev.on("contacts.update", (updates) => {
    for (const c of updates || []) {
      const jid = c.id;
      const name = c.name || c.notify || c.verifiedName;
      if (jid && name) { contactNames.set(jid, name); dirty = true; }
    }
  });

  sock = socket;
  return socket;
}

// --- HTTP API (тот же контракт, что и bridge/index.js) ---
const webApp = express();
webApp.use(express.json());

webApp.use((req, res, next) => {
  if (req.path === "/api/logs" || req.path === "/api/qr-image") return next();
  pushLog("INFO", req.method + " " + req.path);
  next();
});

webApp.get("/api/status", (req, res) => {
  res.json({ connected, qr: lastQrDataUrl, hasQrImage: !!lastQrDataUrl });
});

// QR как картинка (обход проблем с большим data URL в JSON)
webApp.get("/api/qr-image", (req, res) => {
  if (connected || !lastQrDataUrl || !lastQrDataUrl.startsWith("data:image")) {
    res.status(204).end();
    return;
  }
  const base64 = lastQrDataUrl.replace(/^data:image\/\w+;base64,/, "");
  const buf = Buffer.from(base64, "base64");
  res.setHeader("Content-Type", "image/png");
  res.setHeader("Cache-Control", "no-store");
  res.send(buf);
});

webApp.get("/api/logs", (req, res) => {
  const tail = parseInt(req.query.tail || "200", 10);
  const lines = logBuffer.slice(-Math.min(Math.max(1, tail), LOG_MAX));
  res.json({ lines });
});

// Статистика загрузки истории — для проверки, что messaging-history.set пришёл
webApp.get("/api/history-stats", (req, res) => {
  const byChat = Array.from(chatStore.values()).map((c) => {
    const msgList = messagesByChat.get(c.id) || [];
    return { id: c.id, name: c.name, isGroup: c.isGroup, messageCount: msgList.length };
  });
  const totalMessages = byChat.reduce((s, c) => s + c.messageCount, 0);
  res.json({
    connected,
    totalChats: chatStore.size,
    totalMessages,
    byChat: byChat.sort((a, b) => b.messageCount - a.messageCount),
  });
});

webApp.get("/api/chats", (req, res) => {
  if (!connected) {
    return res.status(503).json({ error: "Not connected" });
  }
  const list = Array.from(chatStore.values()).map((c) => {
    const msgList = messagesByChat.get(c.id) || [];
    return {
      id: c.id,
      name: c.name,
      isGroup: c.isGroup,
      type: c.type,
      lastActive: c.lastActive || null,
      messageCount: msgList.length,
    };
  });
  list.sort((a, b) => {
    const ta = b.lastActive || 0;
    const tb = a.lastActive || 0;
    if (ta !== tb) return ta - tb;
    return (a.name || "").localeCompare(b.name || "", undefined, { sensitivity: "base" });
  });
  res.json({ chats: list });
});

webApp.get("/api/chat/:id/messages", (req, res) => {
  if (!connected) {
    return res.status(503).json({ error: "Not connected" });
  }
  const chatId = req.params.id;
  const limitRaw = parseInt(req.query.limit || "500", 10);
  const limit = Math.min(limitRaw > 0 ? limitRaw : 500, 100000);
  const syncFirst = req.query.sync === "1" || req.query.sync === "true";

  const send = () => {
    const list = messagesByChat.get(chatId) || [];
    const sorted = [...list].sort((a, b) => b.timestamp - a.timestamp);
    const out = sorted.slice(0, limit).reverse();
    res.json({ messages: out });
  };

  if (syncFirst && sock && typeof sock.fetchMessageHistory === "function") {
    const list = messagesByChat.get(chatId) || [];
    const byTime = [...list].sort((a, b) => a.timestamp - b.timestamp);
    const oldest = byTime[0];
    const keyId = oldest?.keyId || (oldest?.id?.includes("_") ? oldest.id.split("_").pop() : oldest?.id);
    if (oldest && keyId) {
      const rawKey = { remoteJid: chatId, id: keyId };
      (async () => {
        try {
          console.log("Запрос on-demand history для чата " + chatId + " (старейшее: " + oldest.keyId + ")…");
          await sock.fetchMessageHistory(100, rawKey, oldest.timestamp);
          await new Promise((r) => setTimeout(r, 20000));
        } catch (e) {
          console.error("fetchMessageHistory error:", e.message);
        }
        const list2 = messagesByChat.get(chatId) || [];
        const sorted = [...list2].sort((a, b) => b.timestamp - a.timestamp);
        res.json({ messages: sorted.slice(0, limit).reverse() });
      })();
      return;
    }
  }
  send();
});

webApp.post("/api/chat/:id/sync", (req, res) => {
  if (!connected) {
    return res.status(503).json({ error: "Not connected" });
  }
  const list = messagesByChat.get(req.params.id) || [];
  const byTime = [...list].sort((a, b) => a.timestamp - b.timestamp);
  const oldest = byTime[0];
  const keyId = oldest?.keyId || (oldest?.id?.includes("_") ? oldest.id.split("_").pop() : oldest?.id);
  if (sock && typeof sock.fetchMessageHistory === "function" && oldest && keyId) {
    sock
      .fetchMessageHistory(100, { remoteJid: req.params.id, id: keyId }, oldest.timestamp)
      .then(() => {
        console.log("Запрошена история для " + req.params.id + ", ждём 20 сек…");
        res.json({ ok: true, syncing: true });
      })
      .catch((e) => res.status(500).json({ error: String(e.message) }));
  } else {
    res.json({ ok: true, syncing: false });
  }
});

webApp.post("/api/logout", async (req, res) => {
  if (!connected) {
    return res.json({ ok: true, message: "Уже отключён" });
  }
  try {
    // Сохранить данные на диск перед logout (они НЕ удаляются)
    if (dirty) saveToDisk();
    if (sock) {
      await sock.logout();
      sock = null;
    }
    connected = false;
    lastQrDataUrl = null;
    currentAccountJid = null;
    chatStore.clear();
    messagesByChat.clear();
    messageById.clear();
    contactNames.clear();
    const authDir = path.join(__dirname, "auth_baileys");
    if (fs.existsSync(authDir)) {
      for (const f of fs.readdirSync(authDir)) {
        fs.unlinkSync(path.join(authDir, f));
      }
      fs.rmdirSync(authDir);
    }
  } catch (e) {
    console.error("Logout error:", e.message);
  }
  try {
    await new Promise((r) => setTimeout(r, 1500));
    pushLog("INFO", "Перезапуск сокета после logout (ожидай QR через 5–15 сек)…");
    await startSock();
    res.json({ ok: true, message: "Отключён. Через 5–15 сек открой /app/qr.html и обнови страницу (F5)." });
  } catch (e) {
    pushLog("ERROR", "startSock после logout: " + (e && e.message));
    res.status(500).json({ error: String(e.message) });
  }
});

webApp.listen(WEB_PORT, "127.0.0.1", () => {
  console.log(
    "Веб-API моста (Baileys): http://localhost:" +
      WEB_PORT +
      " (status, chats, messages, logs)"
  );
  console.log("Логи моста доступны в UI: /app/logs.html или по ссылке «Логи моста» на главной.");
});

startSock().catch((e) => {
  console.error("Baileys start error:", e);
  process.exit(1);
});
