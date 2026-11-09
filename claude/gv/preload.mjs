// Startup module for the unmodified Claude 2.1.319 executable.
// Reads the mounted Ink renderer's React state; never rewrites the app bundle.
import fs from 'node:fs';
import path from 'node:path';
import os from 'node:os';
import {installNativeBridge} from './native/bridge.mjs';
const directory=path.join(os.tmpdir(),`gv-${process.getuid?.()??'user'}`);
const socketPath=path.join(directory,`${process.pid}.sock`);
process.env.GV_NATIVE_SOCKET=socketPath;
if(!globalThis.__gvNativePreloadInstalled){
 globalThis.__gvNativePreloadInstalled=true;
 const diagnostic=socketPath+'.preload.json';
 const report=value=>{try{fs.mkdirSync(path.dirname(diagnostic),{recursive:true,mode:0o700});fs.writeFileSync(diagnostic,JSON.stringify(value),{mode:0o600});}catch{}};
 function findValue(value,depth,seen){
  if(!value||typeof value!=='object'||seen.has(value))return null;
  seen.add(value);
  if(typeof value.handleRestoreMessage==='function'&&typeof value.transcript?.getSnapshot==='function'&&value.draft)return value;
  if(depth<=0)return null;
  if(Array.isArray(value)){
   for(const item of value){const found=findValue(item,depth-1,seen);if(found)return found;}
  }else{
   for(const key of ['memoizedState','current','value']){
    const descriptor=Object.getOwnPropertyDescriptor(value,key);
    if(descriptor&&'value'in descriptor){const found=findValue(descriptor.value,depth-1,seen);if(found)return found;}
   }
  }
  return null;
 }
 function findController(root){
  const stack=[root.current],seen=new Set();
  while(stack.length){
   const fiber=stack.pop();if(!fiber||seen.has(fiber))continue;seen.add(fiber);
   let hook=fiber.memoizedState;const hooks=new Set();
   while(hook&&typeof hook==='object'&&!hooks.has(hook)){
    hooks.add(hook);const found=findValue(hook.memoizedState,4,new Set());if(found)return found;hook=hook.next;
   }
   for(const cache of fiber.updateQueue?.memoCache?.data??[]){const found=findValue(cache,3,new Set());if(found)return found;}
   if(fiber.child)stack.push(fiber.child);if(fiber.sibling)stack.push(fiber.sibling);
  }
 }
 let inspecting=false;
 const timer=setInterval(async()=>{
  if(inspecting)return;inspecting=true;
  try{
   // Version-specific internal export: the live terminal/Ink instance registry.
   const {Ii}=await import('/$bunfs/root/chunk-3zptncnh.js');
   const terminal=Ii().get(process.stdout);
   const controller=terminal?.container&&findController(terminal.container);
   if(controller){installNativeBridge(controller,socketPath);clearInterval(timer);fs.rmSync(diagnostic,{force:true});}
  }catch(error){report({error:String(error),pid:process.pid});}
  finally{inspecting=false;}
 },150);
 timer.unref();
}
