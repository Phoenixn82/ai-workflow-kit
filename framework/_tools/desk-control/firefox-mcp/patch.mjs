// firefox-devtools-mcp 0.10.4 createNewPage() assumes the new tab is the last window
// handle. Zen inserts new tabs at the top, so the URL loaded into the bottom tab and a
// blank tab piled up per call. Use the handle the driver actually switched to instead.
// Fails loudly if the upstream code changes, so a version bump can't silently skip it.
import { readFileSync, writeFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

const file = fileURLToPath(new URL("./node_modules/@mozilla/firefox-devtools-mcp/dist/index.js", import.meta.url));
const src = readFileSync(file, "utf8");
const buggy = `        await this.driver.switchTo().newWindow("tab");
        const handles = await this.driver.getAllWindowHandles();
        const newIdx = handles.length - 1;
        this.setCurrentContextId(handles[newIdx]);`;
const fixed = `        await this.driver.switchTo().newWindow("tab");
        const newHandle = await this.driver.getWindowHandle();
        const handles = await this.driver.getAllWindowHandles();
        const newIdx = handles.indexOf(newHandle);
        this.setCurrentContextId(newHandle);`;

if (src.includes(fixed)) {
  console.log("patch.mjs: already applied");
} else if (src.includes(buggy)) {
  writeFileSync(file, src.replace(buggy, fixed));
  console.log("patch.mjs: applied new_page tab fix");
} else {
  console.error("patch.mjs: createNewPage() no longer matches; re-check the upstream fix before upgrading");
  process.exit(1);
}
