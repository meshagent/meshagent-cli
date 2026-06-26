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

function envWithGithubHttpsGitConfig(env) {
  const parsedCount = Number.parseInt(env.GIT_CONFIG_COUNT ?? "0", 10);
  const configIndex =
    Number.isFinite(parsedCount) && parsedCount >= 0 ? parsedCount : 0;
  return {
    ...env,
    GIT_CONFIG_COUNT: String(configIndex + 1),
    [`GIT_CONFIG_KEY_${configIndex}`]: "url.https://github.com/.insteadOf",
    [`GIT_CONFIG_VALUE_${configIndex}`]: "ssh://git@github.com/",
  };
}

console.log(`Installing missing npm dependencies: ${missing.join(", ")}`);
const result = spawnSync("npm", ["install"], {
  cwd: root,
  env: envWithGithubHttpsGitConfig(process.env),
  shell: process.platform === "win32",
  stdio: "inherit",
});
process.exit(result.status ?? 1);
