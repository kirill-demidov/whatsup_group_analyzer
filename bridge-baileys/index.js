/**
 * Multi-tenant мост на Baileys: каждый пользователь подключает свой WhatsApp-аккаунт.
 * sessions Map: username → session object (sock, chatStore, messagesByChat, …).
 * REST API: все эндпоинты принимают ?user=<username>.
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
const AUTH_BASE_DIR = path.join(__dirname, "auth_baileys");
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
pushLog("INFO", "Мост Baileys (multi-tenant): запуск, буфер логов готов.");

const BACKEND_URL = process.env.BACKEND_URL || "http://localhost:8080";
const TARGET_GROUP_ID = process.env.GROUP_ID || null;
const WEB_PORT = parseInt(process.env.WEB_PORT || "3080", 10);

// ===================== Multi-tenant sessions =====================

/** @type {Map<string, object>} username → session */
const sessions = new Map();

function createEmptySession() {
  return {
    sock: null,
    chatStore: new Map(),
    messagesByChat: new Map(),
    messageById: new Map(),
    contactNames: new Map(),
    connected: false,
    lastQrDataUrl: null,
    currentAccountJid: null,
    dirty: false,
    starting: false,
  };
}

/** Безопасное имя папки из username */
function sanitizeUsername(username) {
  return (username || "unknown").replace(/[^a-zA-Z0-9@._-]/g, "_").substring(0, 100);
}

// ===================== Per-session functions =====================

/** Извлекает номер телефона из JID */
function phoneFromJid(jid) {
  if (!jid) return undefined;
  const match = jid.match(/^(\d+)@/);
  return match ? "+" + match[1] : undefined;
}

function resolveSessionContact(session, jid, pushName) {
  return session.contactNames.get(jid) || pushName || phoneFromJid(jid) || undefined;
}

function chatType(jid, isGroup) {
  if (isGroup) return "group";
  if (jid.endsWith("@newsletter") || jid.includes("newsletter")) return "channel";
  return "direct";
}

function normalizeMessage(session, msg) {
  const key = msg.key;
  const chatId = getChatId(key);
  const from = key.remoteJid || chatId;
  // В group history sync key.participant может отсутствовать
  const fromId = key.participant || msg.participant || key.remoteJid || chatId;
  const fromName = resolveSessionContact(session, fromId, msg.pushName || msg.verifiedBizName);
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
    keyId,
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

function addMessageToSession(session, msg) {
  const norm = normalizeMessage(session, msg);
  const chatId = getChatId(msg.key);
  const key = messageKey(msg);
  session.messageById.set(key, msg);

  if (!session.messagesByChat.has(chatId)) session.messagesByChat.set(chatId, []);
  const list = session.messagesByChat.get(chatId);
  const existing = list.find((m) => m.id === norm.id);
  if (!existing) {
    list.push(norm);
    list.sort((a, b) => a.timestamp - b.timestamp);
    const last = list[list.length - 1];
    const chat = session.chatStore.get(chatId);
    if (chat) chat.lastActive = last.timestamp;
    session.dirty = true;
  }
}

function ensureSessionChat(session, jid, name, isGroup, lastActiveOpt) {
  // Для direct чатов подтягиваем имя из contactNames если не передано
  const resolvedName = name || (!isGroup ? session.contactNames.get(jid) : null);
  if (!session.chatStore.has(jid)) {
    session.chatStore.set(jid, {
      id: jid,
      name: resolvedName || "—",
      isGroup: !!isGroup,
      type: chatType(jid, !!isGroup),
      lastActive: lastActiveOpt != null ? Number(lastActiveOpt) : 0,
    });
    session.dirty = true;
  } else {
    const c = session.chatStore.get(jid);
    if (resolvedName && resolvedName !== "—" && c.name !== resolvedName) { c.name = resolvedName; session.dirty = true; }
    if (lastActiveOpt != null) {
      const ts = Number(lastActiveOpt);
      if (ts > 0 && (c.lastActive == null || c.lastActive < ts)) { c.lastActive = ts; session.dirty = true; }
    }
  }
}

function updateGroupMeta(session, jid, meta) {
  const chat = session.chatStore.get(jid);
  if (!chat) return;
  if (meta.description !== undefined) chat.description = meta.description || "";
  if (meta.owner !== undefined) chat.owner = meta.owner || null;
  if (meta.participants !== undefined) chat.participants = meta.participants;
  session.dirty = true;
}

// ===================== Persistence =====================

function getSessionDataDir(username) {
  return path.join(DATA_BASE_DIR, sanitizeUsername(username));
}

function getSessionAuthDir(username) {
  return path.join(AUTH_BASE_DIR, sanitizeUsername(username));
}

function saveSessionToDisk(session, username) {
  const dir = getSessionDataDir(username);
  fs.mkdirSync(dir, { recursive: true });

  const chatsData = Object.fromEntries(session.chatStore);
  const messagesData = Object.fromEntries(session.messagesByChat);
  const contactsData = Object.fromEntries(session.contactNames);

  for (const [name, data] of [["chats.json", chatsData], ["messages.json", messagesData], ["contacts.json", contactsData]]) {
    const target = path.join(dir, name);
    const tmp = target + ".tmp";
    fs.writeFileSync(tmp, JSON.stringify(data));
    fs.renameSync(tmp, target);
  }
  session.dirty = false;
  pushLog("INFO", `[${username}] Данные сохранены: ${session.chatStore.size} чатов, ${Array.from(session.messagesByChat.values()).reduce((s, a) => s + a.length, 0)} сообщений, ${session.contactNames.size} контактов`);
}

function loadSessionFromDisk(session, username) {
  const dir = getSessionDataDir(username);
  if (!fs.existsSync(dir)) return;

  try {
    const chatsFile = path.join(dir, "chats.json");
    if (fs.existsSync(chatsFile)) {
      const data = JSON.parse(fs.readFileSync(chatsFile, "utf-8"));
      for (const [k, v] of Object.entries(data)) {
        if (!session.chatStore.has(k)) session.chatStore.set(k, v);
      }
    }
  } catch (e) { pushLog("WARN", `[${username}] Ошибка загрузки chats.json: ${e.message}`); }

  try {
    const msgsFile = path.join(dir, "messages.json");
    if (fs.existsSync(msgsFile)) {
      const data = JSON.parse(fs.readFileSync(msgsFile, "utf-8"));
      for (const [k, v] of Object.entries(data)) {
        if (!session.messagesByChat.has(k)) session.messagesByChat.set(k, v);
        else {
          const existing = session.messagesByChat.get(k);
          const existingIds = new Set(existing.map(m => m.id));
          for (const msg of v) {
            if (!existingIds.has(msg.id)) existing.push(msg);
          }
          existing.sort((a, b) => a.timestamp - b.timestamp);
        }
      }
    }
  } catch (e) { pushLog("WARN", `[${username}] Ошибка загрузки messages.json: ${e.message}`); }

  try {
    const contactsFile = path.join(dir, "contacts.json");
    if (fs.existsSync(contactsFile)) {
      const data = JSON.parse(fs.readFileSync(contactsFile, "utf-8"));
      for (const [k, v] of Object.entries(data)) {
        if (!session.contactNames.has(k)) session.contactNames.set(k, v);
      }
    }
  } catch (e) { pushLog("WARN", `[${username}] Ошибка загрузки contacts.json: ${e.message}`); }

  // Обогащаем имена direct чатов из contactNames
  let enriched = 0;
  for (const [jid, chat] of session.chatStore) {
    if (!chat.isGroup && (!chat.name || chat.name === "—")) {
      const name = session.contactNames.get(jid);
      if (name) { chat.name = name; enriched++; }
    }
  }
  if (enriched > 0) session.dirty = true;

  const totalMsg = Array.from(session.messagesByChat.values()).reduce((s, a) => s + a.length, 0);
  pushLog("INFO", `[${username}] Loaded ${session.chatStore.size} chats, ${totalMsg} messages, ${session.contactNames.size} contacts from disk (enriched ${enriched} chat names)`);
}

// ===================== startSock(username) =====================

async function startSock(username) {
  if (!username) throw new Error("username is required for startSock");

  let session = sessions.get(username);
  if (!session) {
    session = createEmptySession();
    sessions.set(username, session);
  }

  // Защита от двойного запуска
  if (session.starting) {
    pushLog("INFO", `[${username}] startSock уже запущен, пропуск`);
    return session.sock;
  }
  session.starting = true;

  try {
    const authDir = getSessionAuthDir(username);
    const hadAuth = fs.existsSync(authDir) && fs.readdirSync(authDir).length > 0;
    const { state, saveCreds } = await useMultiFileAuthState(authDir);
    if (!hadAuth) {
      pushLog("INFO", `[${username}] Сессии нет — ожидаю QR от WhatsApp`);
    }
    const { version } = await fetchLatestBaileysVersion();

    const noop = () => {};
    const logger = {
      level: "warn",
      trace: noop,
      debug: noop,
      info: (...a) => pushLog("INFO", `[${username}]`, ...a),
      warn: (...a) => pushLog("WARN", `[${username}]`, ...a),
      error: (...a) => pushLog("ERROR", `[${username}]`, ...a),
      fatal: (...a) => pushLog("FATAL", `[${username}]`, ...a),
      child: () => logger,
    };

    const browserType = (process.env.BROWSER_TYPE || "desktop").toLowerCase();
    const browser = browserType === "chrome" ? Browsers.ubuntu("Chrome") : Browsers.macOS("Desktop");

    const socket = makeWASocket({
      version,
      auth: { creds: state.creds, keys: state.keys },
      waWebSocketUrl: process.env.SOCKET_URL ?? DEFAULT_CONNECTION_CONFIG.waWebSocketUrl,
      logger,
      browser,
      syncFullHistory: true,
      getMessage: async (key) => {
        const k = `${key.remoteJid}_${key.id}`;
        return session.messageById.get(k) || null;
      },
      shouldSyncHistoryMessage: () => true,
    });

    socket.ev.on("creds.update", saveCreds);

    socket.ev.on("connection.update", async (update) => {
      const { connection, lastDisconnect, qr } = update;
      const hasQr = !!qr;
      if (connection !== undefined || hasQr) {
        pushLog("INFO", `[${username}] connection.update: connection=${connection} qr=${hasQr ? "да" : "нет"}`);
      }
      if (qr) {
        session.connected = false;
        try {
          session.lastQrDataUrl = await toDataURL(qr);
          pushLog("INFO", `[${username}] QR сгенерирован, длина ${session.lastQrDataUrl?.length || 0}`);
        } catch (e) {
          session.lastQrDataUrl = null;
          pushLog("ERROR", `[${username}] Ошибка генерации QR: ${e?.message}`);
        }
        qrcodeTerminal.generate(qr, { small: true });
      }
      if (connection === "open") {
        session.connected = true;
        session.lastQrDataUrl = null;
        if (socket.user?.id) {
          session.currentAccountJid = socket.user.id;
          loadSessionFromDisk(session, username);
        }
        pushLog("INFO", `[${username}] Подключено. Ожидание истории (syncFullHistory=true).`);
        // Подгружаем список групп с полными метаданными
        if (socket.groupFetchAllParticipating) {
          socket.groupFetchAllParticipating().then((groups) => {
            if (groups && typeof groups === "object") {
              for (const [jid, meta] of Object.entries(groups)) {
                if (!jid || !meta) continue;
                const lastActive = meta.subjectTime > 0 ? meta.subjectTime : (meta.creation > 0 ? meta.creation : undefined);
                ensureSessionChat(session, jid, meta.subject || meta.name || "—", true, lastActive);

                // Сохраняем имена участников в contactNames
                for (const p of meta.participants || []) {
                  const pName = p.notify || p.name;
                  if (p.id && pName) session.contactNames.set(p.id, pName);
                }

                // Участники с именами и ролями (fallback на contactNames для LID)
                const participants = (meta.participants || []).map((p) => ({
                  id: p.id,
                  name: p.notify || p.name || session.contactNames.get(p.id) || null,
                  admin: p.admin || null,
                }));

                updateGroupMeta(session, jid, {
                  description: meta.desc || "",
                  owner: meta.owner || meta.subjectOwner || null,
                  participants,
                });
              }
              pushLog("INFO", `[${username}] Загружено групп: ${Object.keys(groups).length} (с метаданными)`);
            }
          }).catch((e) => pushLog("ERROR", `[${username}] groupFetchAllParticipating: ${e.message}`));
        }
      }
      if (connection === "close") {
        const err = lastDisconnect?.error;
        const statusCode = (err && err.output && err.output.statusCode) || 0;
        if (statusCode !== DisconnectReason.loggedOut) {
          pushLog("INFO", `[${username}] Переподключение через 3 сек…`);
          setTimeout(() => startSock(username), 3000);
        } else {
          session.connected = false;
          session.sock = null;
        }
      }
    });

    socket.ev.on("messaging-history.set", ({ chats, messages, syncType }) => {
      const nChats = chats?.length ?? 0;
      const nMsg = messages?.length ?? 0;
      pushLog("INFO", `[${username}] messaging-history.set: чатов=${nChats}, сообщений=${nMsg}${syncType != null ? ", syncType=" + syncType : ""}`);
      if (nMsg === 0 && nChats > 0) {
        pushLog("WARN", `[${username}] История без сообщений — новые будут в messages.upsert.`);
      }
      if (chats && Array.isArray(chats)) {
        for (const c of chats) {
          const jid = c.id || c.jid;
          if (!jid) continue;
          const name = c.name || "—";
          const lastActive = c.lastMessageRecvTimestamp ?? c.conversationTimestamp;
          ensureSessionChat(session, jid, name, (c.id || jid).toString().endsWith("@g.us"), lastActive);
        }
      }
      if (messages && Array.isArray(messages)) {
        for (const msg of messages) {
          try {
            addMessageToSession(session, msg);
            const chatId = getChatId(msg.key);
            ensureSessionChat(session, chatId, null, chatId.endsWith("@g.us"));
          } catch (e) { /* skip broken */ }
        }
      }
      // Для чатов с lastActive=0 — обновить из последнего сообщения
      for (const [cid, chat] of session.chatStore) {
        if (chat.lastActive > 0) continue;
        const msgs = session.messagesByChat.get(cid);
        if (msgs && msgs.length > 0) {
          const lastTs = msgs[msgs.length - 1].timestamp;
          if (lastTs > 0) chat.lastActive = lastTs;
        }
      }
      const totalMsg = Array.from(session.messagesByChat.values()).reduce((s, arr) => s + arr.length, 0);
      pushLog("INFO", `[${username}] History sync итого: чатов=${session.chatStore.size}, сообщений=${totalMsg}`);
    });

    socket.ev.on("messages.upsert", async ({ messages, type }) => {
      if (messages?.length) pushLog("INFO", `[${username}] messages.upsert: ${messages.length} шт., type=${type}`);
      for (const msg of messages || []) {
        try {
          addMessageToSession(session, msg);
          const chatId = getChatId(msg.key);
          ensureSessionChat(session, chatId, null, chatId.endsWith("@g.us"));
        } catch (e) {}
      }
      // Webhook в бэкенд только для новых входящих (type === 'notify')
      if (type === "notify" && TARGET_GROUP_ID) {
        for (const msg of messages || []) {
          const chatId = getChatId(msg.key);
          if (chatId !== TARGET_GROUP_ID) continue;
          const norm = normalizeMessage(session, msg);
          const text = (norm.body || "").trim();
          if (text.length < 5) continue;
          try {
            const res = await fetch(`${BACKEND_URL}/webhook/bridge`, {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ text, chat_id: chatId, from_name: norm.from_name }),
            });
            if (res.ok) {
              const data = await res.json().catch(() => ({}));
              pushLog("INFO", `[${username}] Бэкенд: ${data.processed !== undefined ? `extracted=${data.extracted}, added=${data.added}` : JSON.stringify(data)}`);
            }
          } catch (e) {
            pushLog("ERROR", `[${username}] Bridge webhook error: ${e.message}`);
          }
        }
      }
    });

    socket.ev.on("chats.upsert", (chats) => {
      for (const c of chats || []) {
        const jid = c.id || c.jid;
        if (jid) {
          const lastActive = c.lastMessageRecvTimestamp ?? c.conversationTimestamp ?? c.lastMessageTimestamp;
          ensureSessionChat(session, jid, c.name || c.subject, jid.endsWith("@g.us"), lastActive);
        }
      }
    });

    socket.ev.on("chats.update", (updates) => {
      for (const u of updates || []) {
        const jid = u.id;
        if (jid) {
          const lastActive = u.lastMessageRecvTimestamp ?? u.conversationTimestamp ?? u.lastMessageTimestamp;
          ensureSessionChat(session, jid, u.name || u.subject, jid.endsWith("@g.us"), lastActive);
        }
      }
    });

    socket.ev.on("contacts.upsert", (contacts) => {
      for (const c of contacts || []) {
        const jid = c.id;
        const name = c.name || c.notify || c.verifiedName;
        if (jid && name) {
          session.contactNames.set(jid, name);
          // Обновляем имя direct чата если было "—"
          const chat = session.chatStore.get(jid);
          if (chat && !chat.isGroup && (chat.name === "—" || !chat.name)) { chat.name = name; }
          session.dirty = true;
        }
      }
      if (contacts?.length) pushLog("INFO", `[${username}] contacts.upsert: ${contacts.length} контактов`);
    });

    socket.ev.on("contacts.update", (updates) => {
      for (const c of updates || []) {
        const jid = c.id;
        const name = c.name || c.notify || c.verifiedName;
        if (jid && name) {
          session.contactNames.set(jid, name);
          const chat = session.chatStore.get(jid);
          if (chat && !chat.isGroup && (chat.name === "—" || !chat.name)) { chat.name = name; }
          session.dirty = true;
        }
      }
    });

    socket.ev.on("groups.update", (updates) => {
      for (const u of updates || []) {
        const jid = u.id;
        if (!jid) continue;
        if (u.subject) ensureSessionChat(session, jid, u.subject, true);
        const patch = {};
        if (u.desc !== undefined) patch.description = u.desc || "";
        if (Object.keys(patch).length > 0) updateGroupMeta(session, jid, patch);
      }
      if (updates?.length) pushLog("INFO", `[${username}] groups.update: ${updates.length} групп обновлено`);
    });

    socket.ev.on("group-participants.update", async ({ id: jid, participants: pIds, action }) => {
      if (!jid) return;
      pushLog("INFO", `[${username}] group-participants.update: ${action} ${pIds?.length || 0} участников в ${jid}`);
      // Перезагружаем полные метаданные группы
      if (socket.groupMetadata) {
        try {
          const meta = await socket.groupMetadata(jid);
          if (meta) {
            for (const p of meta.participants || []) {
              const pName = p.notify || p.name;
              if (p.id && pName) session.contactNames.set(p.id, pName);
            }
            const participants = (meta.participants || []).map((p) => ({
              id: p.id,
              name: p.notify || p.name || session.contactNames.get(p.id) || null,
              admin: p.admin || null,
            }));
            updateGroupMeta(session, jid, {
              description: meta.desc || "",
              owner: meta.owner || meta.subjectOwner || null,
              participants,
            });
          }
        } catch (e) {
          pushLog("ERROR", `[${username}] groupMetadata(${jid}): ${e.message}`);
        }
      }
    });

    session.sock = socket;
    return socket;
  } finally {
    session.starting = false;
  }
}

// ===================== Restore existing sessions on startup =====================

async function restoreExistingSessions() {
  if (!fs.existsSync(AUTH_BASE_DIR)) return;
  const dirs = fs.readdirSync(AUTH_BASE_DIR, { withFileTypes: true })
    .filter((d) => d.isDirectory())
    .map((d) => d.name);
  if (dirs.length === 0) return;
  pushLog("INFO", `Найдено ${dirs.length} сохранённых сессий, восстанавливаю…`);
  for (let i = 0; i < dirs.length; i++) {
    const username = dirs[i];
    try {
      pushLog("INFO", `Восстановление сессии: ${username}`);
      await startSock(username);
      // Задержка между стартами, чтобы не перегрузить WhatsApp
      if (i < dirs.length - 1) await new Promise((r) => setTimeout(r, 2000));
    } catch (e) {
      pushLog("ERROR", `Ошибка восстановления сессии ${username}: ${e.message}`);
    }
  }
}

// ===================== Dirty timer + graceful shutdown =====================

setInterval(() => {
  for (const [username, session] of sessions) {
    if (session.dirty) saveSessionToDisk(session, username);
  }
}, 30_000);

function gracefulShutdown(signal) {
  pushLog("INFO", `${signal} получен, сохраняю все сессии…`);
  for (const [username, session] of sessions) {
    if (session.dirty) {
      try { saveSessionToDisk(session, username); } catch (e) { /* best effort */ }
    }
  }
  process.exit(0);
}
process.on("SIGINT", () => gracefulShutdown("SIGINT"));
process.on("SIGTERM", () => gracefulShutdown("SIGTERM"));

// ===================== HTTP API =====================

const webApp = express();
webApp.use(express.json());

webApp.use((req, res, next) => {
  if (req.path === "/api/logs" || req.path === "/api/qr-image") return next();
  pushLog("INFO", req.method + " " + req.path + (req.query.user ? ` [user=${req.query.user}]` : ""));
  next();
});

/** Helper: получить или создать сессию по ?user= */
function getSession(req) {
  const username = req.query.user;
  if (!username) return { username: null, session: null };
  let session = sessions.get(username);
  return { username, session };
}

webApp.get("/api/status", async (req, res) => {
  const { username, session } = getSession(req);
  if (!username) {
    return res.status(400).json({ error: "Missing ?user= parameter" });
  }
  // Если сессии нет — запускаем автоматически
  if (!session) {
    try {
      await startSock(username);
      const s = sessions.get(username);
      return res.json({ connected: s?.connected || false, qr: s?.lastQrDataUrl, hasQrImage: !!s?.lastQrDataUrl });
    } catch (e) {
      return res.status(500).json({ error: e.message });
    }
  }
  res.json({ connected: session.connected, qr: session.lastQrDataUrl, hasQrImage: !!session.lastQrDataUrl });
});

webApp.get("/api/qr-image", (req, res) => {
  const { session } = getSession(req);
  if (!session || session.connected || !session.lastQrDataUrl || !session.lastQrDataUrl.startsWith("data:image")) {
    res.status(204).end();
    return;
  }
  const base64 = session.lastQrDataUrl.replace(/^data:image\/\w+;base64,/, "");
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

webApp.get("/api/history-stats", (req, res) => {
  const { username, session } = getSession(req);
  if (!username) return res.status(400).json({ error: "Missing ?user= parameter" });
  if (!session) return res.json({ connected: false, totalChats: 0, totalMessages: 0, byChat: [] });
  const byChat = Array.from(session.chatStore.values()).map((c) => {
    const msgList = session.messagesByChat.get(c.id) || [];
    return { id: c.id, name: c.name, isGroup: c.isGroup, messageCount: msgList.length };
  });
  const totalMessages = byChat.reduce((s, c) => s + c.messageCount, 0);
  res.json({
    connected: session.connected,
    totalChats: session.chatStore.size,
    totalMessages,
    byChat: byChat.sort((a, b) => b.messageCount - a.messageCount),
  });
});

webApp.get("/api/chats", (req, res) => {
  const { username, session } = getSession(req);
  if (!username) return res.status(400).json({ error: "Missing ?user= parameter" });
  if (!session || !session.connected) {
    return res.status(503).json({ error: "Not connected" });
  }
  const list = Array.from(session.chatStore.values()).map((c) => {
    const msgList = session.messagesByChat.get(c.id) || [];
    return {
      id: c.id, name: c.name, isGroup: c.isGroup, type: c.type,
      lastActive: c.lastActive || null, messageCount: msgList.length,
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

webApp.get("/api/chat/:id/metadata", (req, res) => {
  const { username, session } = getSession(req);
  if (!username) return res.status(400).json({ error: "Missing ?user= parameter" });
  if (!session) return res.status(503).json({ error: "Session not found" });
  const chatId = req.params.id;
  const chat = session.chatStore.get(chatId);
  if (!chat) return res.status(404).json({ error: "Chat not found" });

  // Собираем имена отправителей из сообщений группы (пропускаем телефонные номера)
  const msgs = session.messagesByChat.get(chatId) || [];
  const senderNames = new Map();
  for (const m of msgs) {
    if (m.from_id && m.from_name && !/^\+?\d+$/.test(m.from_name)) {
      senderNames.set(m.from_id, m.from_name);
    }
  }

  res.json({
    id: chat.id,
    name: chat.name,
    isGroup: chat.isGroup,
    type: chat.type,
    lastActive: chat.lastActive || null,
    description: chat.description || null,
    owner: chat.owner || null,
    participants: (chat.participants || []).map((p) => ({
      ...p,
      name: p.name || session.contactNames.get(p.id) || null,
    })),
    knownSenders: Array.from(senderNames, ([id, name]) => ({ id, name })),
  });
});

webApp.get("/api/chat/:id/messages", (req, res) => {
  const { username, session } = getSession(req);
  if (!username) return res.status(400).json({ error: "Missing ?user= parameter" });
  if (!session || !session.connected) {
    return res.status(503).json({ error: "Not connected" });
  }
  const chatId = req.params.id;
  const limitRaw = parseInt(req.query.limit || "500", 10);
  const limit = Math.min(limitRaw > 0 ? limitRaw : 500, 100000);
  const syncFirst = req.query.sync === "1" || req.query.sync === "true";

  const send = () => {
    const list = session.messagesByChat.get(chatId) || [];
    const sorted = [...list].sort((a, b) => b.timestamp - a.timestamp);
    const out = sorted.slice(0, limit).reverse();
    res.json({ messages: out });
  };

  if (syncFirst && session.sock && typeof session.sock.fetchMessageHistory === "function") {
    const list = session.messagesByChat.get(chatId) || [];
    const byTime = [...list].sort((a, b) => a.timestamp - b.timestamp);
    const oldest = byTime[0];
    const keyId = oldest?.keyId || (oldest?.id?.includes("_") ? oldest.id.split("_").pop() : oldest?.id);
    if (oldest && keyId) {
      const rawKey = { remoteJid: chatId, id: keyId };
      (async () => {
        try {
          pushLog("INFO", `[${username}] Запрос on-demand history для чата ${chatId}`);
          await session.sock.fetchMessageHistory(100, rawKey, oldest.timestamp);
          await new Promise((r) => setTimeout(r, 20000));
        } catch (e) {
          pushLog("ERROR", `[${username}] fetchMessageHistory error: ${e.message}`);
        }
        const list2 = session.messagesByChat.get(chatId) || [];
        const sorted = [...list2].sort((a, b) => b.timestamp - a.timestamp);
        res.json({ messages: sorted.slice(0, limit).reverse() });
      })();
      return;
    }
  }
  send();
});

webApp.post("/api/chat/:id/sync", (req, res) => {
  const { username, session } = getSession(req);
  if (!username) return res.status(400).json({ error: "Missing ?user= parameter" });
  if (!session || !session.connected) {
    return res.status(503).json({ error: "Not connected" });
  }
  const list = session.messagesByChat.get(req.params.id) || [];
  const byTime = [...list].sort((a, b) => a.timestamp - b.timestamp);
  const oldest = byTime[0];
  const keyId = oldest?.keyId || (oldest?.id?.includes("_") ? oldest.id.split("_").pop() : oldest?.id);
  if (session.sock && typeof session.sock.fetchMessageHistory === "function" && oldest && keyId) {
    session.sock
      .fetchMessageHistory(100, { remoteJid: req.params.id, id: keyId }, oldest.timestamp)
      .then(() => {
        pushLog("INFO", `[${username}] Запрошена история для ${req.params.id}`);
        res.json({ ok: true, syncing: true });
      })
      .catch((e) => res.status(500).json({ error: String(e.message) }));
  } else {
    res.json({ ok: true, syncing: false });
  }
});

webApp.post("/api/logout", async (req, res) => {
  const { username, session } = getSession(req);
  if (!username) return res.status(400).json({ error: "Missing ?user= parameter" });
  if (!session || !session.connected) {
    return res.json({ ok: true, message: "Уже отключён" });
  }
  try {
    if (session.dirty) saveSessionToDisk(session, username);
    if (session.sock) {
      await session.sock.logout();
      session.sock = null;
    }
    session.connected = false;
    session.lastQrDataUrl = null;
    session.currentAccountJid = null;
    session.chatStore.clear();
    session.messagesByChat.clear();
    session.messageById.clear();
    session.contactNames.clear();
    // Удалить auth-данные этой сессии
    const authDir = getSessionAuthDir(username);
    if (fs.existsSync(authDir)) {
      fs.rmSync(authDir, { recursive: true, force: true });
    }
    sessions.delete(username);
  } catch (e) {
    pushLog("ERROR", `[${username}] Logout error: ${e.message}`);
  }
  try {
    await new Promise((r) => setTimeout(r, 1500));
    pushLog("INFO", `[${username}] Перезапуск сокета после logout (ожидай QR)…`);
    await startSock(username);
    res.json({ ok: true, message: "Отключён. Через 5–15 сек открой /app/qr.html и обнови страницу (F5)." });
  } catch (e) {
    pushLog("ERROR", `[${username}] startSock после logout: ${e?.message}`);
    res.status(500).json({ error: String(e.message) });
  }
});

// Новый эндпоинт: явный запуск сессии
webApp.post("/api/session/start", async (req, res) => {
  const { username } = getSession(req);
  if (!username) return res.status(400).json({ error: "Missing ?user= parameter" });
  try {
    await startSock(username);
    const session = sessions.get(username);
    res.json({ ok: true, connected: session?.connected || false });
  } catch (e) {
    pushLog("ERROR", `[${username}] session/start error: ${e.message}`);
    res.status(500).json({ error: e.message });
  }
});

webApp.listen(WEB_PORT, "0.0.0.0", () => {
  console.log(`Веб-API моста (Baileys multi-tenant): http://localhost:${WEB_PORT}`);
});

// Восстановить существующие сессии при старте
restoreExistingSessions().catch((e) => {
  console.error("Ошибка восстановления сессий:", e);
});
