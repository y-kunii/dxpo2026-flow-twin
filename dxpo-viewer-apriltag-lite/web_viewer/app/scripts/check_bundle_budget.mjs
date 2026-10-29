import { readdirSync, readFileSync, statSync } from "node:fs";
import { resolve } from "node:path";
import { gzipSync } from "node:zlib";

const root = resolve(process.argv[2] ?? "dist");
const NORMAL_CHUNK_GZIP_LIMIT = 256 * 1024;
const LAZY_SPARK_GZIP_LIMIT = 2 * 1024 * 1024;

function files(directory) {
  return readdirSync(directory, { withFileTypes: true }).flatMap((entry) => {
    const path = resolve(directory, entry.name);
    return entry.isDirectory() ? files(path) : [path];
  });
}

if (!statSync(root).isDirectory()) throw new Error(`bundle output is not a directory: ${root}`);
const javascript = files(root).filter((path) => path.endsWith(".js"));
if (javascript.length === 0) throw new Error(`bundle output contains no JavaScript: ${root}`);

const failures = [];
const report = javascript.map((path) => {
  const source = readFileSync(path);
  const gzipBytes = gzipSync(source).byteLength;
  const lazySpark = path.includes("GaussianSplatRuntime-");
  const limit = lazySpark ? LAZY_SPARK_GZIP_LIMIT : NORMAL_CHUNK_GZIP_LIMIT;
  if (gzipBytes > limit) failures.push(`${path}: gzip ${gzipBytes} > ${limit}`);
  return { path: path.slice(root.length + 1), bytes: source.byteLength, gzip_bytes: gzipBytes, budget_bytes: limit };
});

console.log(JSON.stringify({ status: failures.length ? "failed" : "verified", chunks: report }, null, 2));
if (failures.length) {
  for (const failure of failures) console.error(failure);
  process.exitCode = 1;
}
