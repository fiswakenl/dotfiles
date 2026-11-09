// Значения плагина GV в $.state: ответы quick под голосовыми сообщениями и какие из них раскрыты.
declare module "claude-code" {
  interface PluginState {
    gv: {
      quickRows: Record<string, { id: number; q: string; a: string }[]>; // UUID голосового сообщения → quick, заданные во время его хода
      quickOpen: number[]; // номера раскрытых quick
    };
  }
}
