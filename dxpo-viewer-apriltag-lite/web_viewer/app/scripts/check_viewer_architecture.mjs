import { readFile } from "node:fs/promises";
import { resolve } from "node:path";

const root = resolve(import.meta.dirname, "..");
const budgets = [
  ["src/ViewerApplication.ts", 1_950],
  ["src/renderer/GaussianSplatRuntime.ts", 200],
  ["src/ui/ViewerChrome.ts", 150],
  ["src/camera/RendererInteractionQuality.ts", 100],
  ["src/preview/AprilTagLiveCheck.ts", 1_550],
  ["src/preview/VideoFrameClock.ts", 100],
];
const failures = [];
const measurements = [];
for (const [relative, maximumLines] of budgets) {
  const source = await readFile(resolve(root, relative), "utf8");
  const lines = source.split(/\r?\n/u).length;
  measurements.push({ path: relative, lines, maximum_lines: maximumLines });
  if (lines > maximumLines) failures.push(`${relative}:${lines}>${maximumLines}`);
}
const application = await readFile(resolve(root, "src/ViewerApplication.ts"), "utf8");
if (application.includes('from "@sparkjsdev/spark"')) {
  failures.push("ViewerApplication must not eagerly import the Spark runtime");
}
console.log(JSON.stringify({ status: failures.length ? "failed" : "verified", measurements, failures }, null, 2));
if (failures.length) process.exitCode = 1;
