// Значения плагина GV в $.state: какие строки quick в чате раскрыты (по id сообщения).
declare module "claude-code" {
  interface PluginState {
    gv: { quickOpen: string[] };
  }
}
