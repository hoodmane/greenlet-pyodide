// Run the greenlet pytest suite inside Pyodide.
//
// Usage:
//   node --experimental-wasm-stack-switching tests/run_tests.mjs [pytest args...]

import { loadPyodide } from "../../pyodide/dist/pyodide.mjs";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";

const __dirname = dirname(fileURLToPath(import.meta.url));
const repoRoot = resolve(__dirname, "..");
const pyodideDist = resolve(repoRoot, "../pyodide/dist");

const userArgs = process.argv.slice(2);

const pyodide = await loadPyodide({
  indexURL: pyodideDist,
  fullStdLib: false,
});

// Mount the repo into the in-memory FS so that `import greenlet`
// resolves to our pure-Python implementation and pytest can collect
// the tests directly from disk.
const FS = pyodide.FS;
FS.mkdir("/repo");
FS.mount(FS.filesystems.NODEFS, { root: repoRoot }, "/repo");

await pyodide.loadPackage("pytest");

const args = userArgs.length
  ? userArgs.map((a) => (a.startsWith("/") ? a : "/repo/tests/" + a))
  : ["/repo/tests"];

const exitCode = await pyodide.runPythonAsync(`
import sys
sys.path.insert(0, "/repo/src")

import pytest
pytest.main(${JSON.stringify(["-v", "-rA", "--color=yes", "--tb=line", ...args])})
`);

process.exit(Number(exitCode) || 0);
