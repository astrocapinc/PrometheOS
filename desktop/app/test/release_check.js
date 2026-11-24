// Reports how a built app is configured (decision 0072): the inspector fuse, whether the
// packaged package.json is stamped as a release build, and whether the notices ship.
// Usage: node desktop/app/test/release_check.js <unpacked app folder>
const fs = require("node:fs");
const path = require("node:path");
const { getCurrentFuseWire, FuseV1Options } = require("@electron/fuses");
const { FuseState } = require("@electron/fuses/dist/constants");
const asar = require("@electron/asar");

async function main(folder) {
  const exe = path.join(folder, "PrometheOS.exe");
  const resources = path.join(folder, "resources");
  const wire = await getCurrentFuseWire(exe);
  const manifest = JSON.parse(asar.extractFile(path.join(resources, "app.asar"), "package.json"));
  const report = {
    inspect_arguments_fuse: wire[FuseV1Options.EnableNodeCliInspectArguments] === FuseState.ENABLE,
    run_as_node_fuse: wire[FuseV1Options.RunAsNode] === FuseState.ENABLE,
    release_build: manifest.prometheosRelease === true,
    notices: ["LICENSE", "NOTICE", "THIRD-PARTY-NOTICES.txt"].filter((n) =>
      fs.existsSync(path.join(resources, n))),
    electron_notices: ["LICENSE.electron.txt", "LICENSES.chromium.html"].filter((n) =>
      fs.existsSync(path.join(folder, n))),
  };
  process.stdout.write(JSON.stringify(report) + "\n");
}

main(process.argv[2]).catch((err) => {
  process.stderr.write(`${err.stack || err}\n`);
  process.exit(1);
});
