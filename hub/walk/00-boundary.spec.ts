// The browser boundary controls (decision 0028) for the C10-WALK, against the walk's own hub:
// the walk proves the boundary of the browser it then drives. This file is the `boundary` project,
// a dependency of walk.spec.ts: a failing control means the walk does not run.
import { readFileSync } from "node:fs";
import { registerBoundaryChecks } from "../e2e/boundary-checks";

const info = JSON.parse(readFileSync(process.env.WALK_INFO ?? "", "utf-8")) as { url: string };

registerBoundaryChecks(() => info.url.replace(/\/$/, ""));
