// Мост в окно Claude: Unix-сокет, строка JSON {name, args} → строка JSON с результатом.
// Имена те же, что у /call в voice.py: status, list, send {text}, rewind {target, text?}, cancel {text}, said {text}, draft {text}.
// cancel — остановить идущий ход, как Esc; text — сообщение этого хода.
// target — номер сообщения пользователя с начала ветки (1, 2…) или его UUID; text — сразу отправить после отката.
import net from 'node:net';
import fs from 'node:fs';
import path from 'node:path';

let controller, server, busy = false;
const textOf = m => {
 const c = m.message?.content;
 return typeof c === 'string' ? c : Array.isArray(c) ? c.map(b=>b.type==='text'?b.text:'').join('\n') : '';
};
const rows = () => controller.transcript.getSnapshot();
const users = () => rows().filter(m=>m.type==='user' && !m.isMeta && !textOf(m).startsWith('<local-command-') && !textOf(m).startsWith('<command-name>'));

// Отправка самого поля ввода, без подписи плагина. Недописанное человеком (текст, вставки, режим) отправка стёрла бы:
// оно запомнено до отправки (у отката — до него) и возвращается в поле, если человек за это время не начал новое.
async function send(text, kept = draftOf()) {
 if (typeof text !== 'string' || !text.trim()) return {error:'text_required'};
 const draft = controller.draft;
 const before = new Set(users().map(m=>m.uuid));
 draft.setMode('prompt'); // в режиме bash поле выполнило бы текст командой
 draft.replaceValue(text);
 if (!draft.submit(text)) { restore(kept); return {error:'composer_not_mounted'}; }
 let result = {queued:true}; // Claude занят: поле ввода поставило сообщение в очередь, в истории оно появится, когда до него дойдёт
 for (let i = 0; i < 150; i++) {
  const m = users().find(m=>!before.has(m.uuid) && textOf(m)===text);
  if (m) { result = {uuid:m.uuid}; break; }
  await new Promise(r=>setTimeout(r,20));
 }
 restore(kept);
 return result;
}

// Черновик голоса: распознанное показываем в поле ввода, пока человек говорит. Поле трогаем, только когда оно пустое или в нём
// наш же черновик: набранное руками важнее. Для отправки и отката наш черновик — пустое поле: возвращать его туда незачем.
let voiced = '';
function draft({text = ''}) {
 const d = controller.draft;
 if (d.value ? d.value !== voiced : d.mode !== 'prompt') return {skipped:true};
 d.replaceValue(text);
 d.setCursorOffset(text.length);
 voiced = text;
 return {shown:true};
}

const draftOf = () => {
 const d = controller.draft;
 if (voiced && d.value === voiced) d.replaceValue('');
 voiced = '';
 return {value:d.value, cursor:d.cursorOffset, mode:d.mode, pasted:d.pastedContents};
};

function put({value, cursor, mode, pasted}) {
 const draft = controller.draft;
 draft.setMode(mode);
 if (pasted && Object.keys(pasted).length) draft.setPastedContents(pasted);
 draft.replaceValue(value);
 draft.setCursorOffset(cursor);
}

function restore(kept) {
 if (kept.value && !controller.draft.value) put(kept); // иначе нечего возвращать или человек уже пишет новое
}

// Ход, остановленный до первого ответа, Claude убирает из истории, а его сообщение кладёт обратно в поле ввода, как будто
// человек хочет его дописать. Голосовое сообщение дописывают голосом: поле возвращается к тому, что в нём было до остановки.
async function cancel({text}) {
 const kept = draftOf();
 await controller.turn.cancel();
 for (let i = 0; i < 25 && text; i++) {
  if (controller.draft.value === text) { put(kept); break; }
  await new Promise(r=>setTimeout(r,20));
 }
 return {cancelled:true};
}

async function rewind({target, text}) {
 const list = users();
 const found = /^\d+$/.test(String(target)) ? list[Number(target)-1] : list.find(m=>m.uuid===target);
 if (!found) return {error:'target_not_found'};
 if (busy) return {error:'rewind_in_progress'};
 busy = true;
 try {
  const kept = draftOf(); // откат кладёт в поле текст стёртого сообщения: недописанное человеком — до него
  const before = rows();
  const index = before.findIndex(m=>m.uuid===found.uuid);
  await controller.turn.cancel(); // то же, что делает меню Rewind
  await controller.handleRestoreMessage(found, 'message_selector');
  await new Promise(r=>setImmediate(r));
  if (rows().some(m=>m.uuid===found.uuid)) return {error:'restore_did_not_apply'};
  const removed = before.slice(index).filter(m=>m.type==='user').map(m=>m.uuid);
  return {removed, ...(text === undefined ? {} : await send(text, kept))};
 } finally { busy = false; }
}

// Что Claude уже написал в ответ на голосовое сообщение с этим текстом: готовые куски из истории и то, что печатается прямо сейчас.
// Зацепка во внутренность: turn.stream.peekStreamingText() — текст, который окно показывает, пока ответ идёт (проверено на 2.1.323).
function said({text}) {
 const all = rows();
 const at = all.findLastIndex(m=>m.type==='user' && !m.isMeta && textOf(m).startsWith('🎙') && (!text || textOf(m).endsWith(text)));
 if (at < 0) return {text:''};
 const done = all.slice(at+1).filter(m=>m.type==='assistant').map(textOf);
 const now = controller.turn?.stream?.peekStreamingText?.() ?? '';
 return {text:[...done, now].filter(s=>s.trim()).join('\n').trim()};
}

async function dispatch({name, args = {}}) {
 if (!controller?.transcript) return {error:'controller_not_ready'};
 if (name === 'status') return {ready:true, pid:process.pid, sessionId:controller.session.id};
 if (name === 'list') return {messages:users().map(m=>({uuid:m.uuid, text:textOf(m)}))};
 if (name === 'send') return send(args.text);
 if (name === 'rewind') return rewind(args);
 if (name === 'cancel') return cancel(args);
 if (name === 'said') return said(args);
 if (name === 'draft') return draft(args);
 return {error:`unknown_name: ${name}`};
}

export function installNativeBridge(instance, socketPath) {
 if (!socketPath || server) return;
 controller = instance;
 fs.mkdirSync(path.dirname(socketPath), {recursive:true, mode:0o700});
 if (fs.existsSync(socketPath)) fs.unlinkSync(socketPath); // остался от упавшего процесса с тем же PID
 server = net.createServer(socket=>{
  let data = '';
  socket.setEncoding('utf8');
  socket.on('data', chunk=>{
   data += chunk;
   if (data.length > 65536) return socket.destroy();
   for (let end; (end = data.indexOf('\n')) >= 0;) {
    const line = data.slice(0, end); data = data.slice(end+1);
    Promise.resolve().then(()=>dispatch(JSON.parse(line))).catch(e=>({error:String(e)})).then(r=>socket.write(JSON.stringify(r)+'\n'));
   }
  });
  socket.on('error', ()=>{});
 });
 server.listen(socketPath, ()=>fs.chmodSync(socketPath, 0o600));
 server.unref();
 process.once('exit', ()=>{ try { fs.unlinkSync(socketPath); } catch {} });
}
