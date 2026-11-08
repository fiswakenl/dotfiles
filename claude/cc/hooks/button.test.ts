import { test, expect } from "claude-code/testing";

// options приезжают в register один раз, при загрузке, — из теста режим не
// переключить. Проверяем тот, что по умолчанию (auto): кнопки быть не должно,
// там чистит сам Enter. Ниже по цепочке что-то рисует сам движок, поэтому
// хук в auto обязан вернуть next(e), иначе mount упадёт на пустой отрисовке.
test("в auto кнопка над полем не рисуется", async ($, on) => {
  on("ui.render", { component: "AbovePrompt" }, ($$, e) => {
    const { Text } = $$.ui.resolve(e);
    return Text({ key: "низ", children: "низ цепочки" });
  });

  for (const surface of ["terminal", "desktop"] as const) {
    const ui = await $.ui.mount({ plugin: "cc", surface, component: "AbovePrompt", props: {} });
    expect(await ui.find({ key: "clean" })).toBeUndefined();
    await ui.unmount();
  }
});
