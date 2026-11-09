// Мост в окно Claude: Unix-сокет, строка JSON {name, args} → строка JSON с результатом.
// Имена те же, что у /call в voice.py: status, list, send {text}, rewind {target, text?}.
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

const draftOf = () => {
 const d = controller.draft;
 return {value:d.value, cursor:d.cursorOffset, mode:d.mode, pasted:d.pastedContents};
};

function restore({value, cursor, mode, pasted}) {
 const draft = controller.draft;
 if (!value || draft.value) return; // нечего возвращать или человек уже пишет новое
 draft.setMode(mode);
 if (pasted && Object.keys(pasted).length) draft.setPastedContents(pasted);
 draft.replaceValue(value);
 draft.setCursorOffset(cursor);
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

async function dispatch({name, args = {}}) {
 if (!controller?.transcript) return {error:'controller_not_ready'};
 if (name === 'status') return {ready:true, pid:process.pid, sessionId:controller.session.id};
 if (name === 'list') return {messages:users().map(m=>({uuid:m.uuid, text:textOf(m)}))};
 if (name === 'send') return send(args.text);
 if (name === 'rewind') return rewind(args);
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
