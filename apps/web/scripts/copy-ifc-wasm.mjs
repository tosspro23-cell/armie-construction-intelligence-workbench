import { cp, mkdir } from "node:fs/promises";
import { resolve } from "node:path";

const source = resolve("node_modules/web-ifc/web-ifc.wasm");
const destinationDirectory = resolve("public/wasm");
await mkdir(destinationDirectory, { recursive: true });
await cp(source, resolve(destinationDirectory, "web-ifc.wasm"));

const workerDirectory = resolve("public/ifc-worker");
await mkdir(workerDirectory, { recursive: true });
await cp(resolve("node_modules/web-ifc-three/IFCWorker.js"), resolve(workerDirectory, "IFCWorker.js"));
