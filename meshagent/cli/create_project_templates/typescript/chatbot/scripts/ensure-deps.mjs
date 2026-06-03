import { spawnSync } from "node:child_process";
import { existsSync, readFileSync } from "node:fs";
import { join } from "node:path";

const root = process.cwd();
const packageJson = JSON.parse(readFileSync(join(root, "package.json"), "utf8"));
const packageNames = [
  ...Object.keys(packageJson.dependencies ?? {}),
  ...Object.keys(packageJson.devDependencies ?? {}),
];

const missing = packageNames.filter((name) => {
  return !existsSync(join(root, "node_modules", ...name.split("/")));
});

if (missing.length === 0) {
  process.exit(0);
}

console.log(`Installing missing npm dependencies: ${missing.join(", ")}`);
const result = spawnSync("npm", ["install"], {
  cwd: root,
  shell: process.platform === "win32",
  stdio: "inherit",
});
process.exit(result.status ?? 1);
